"""Whether RawDataHandler can decode the binary arrays of an mzML file, told from its first few spectra.

RawDataHandler, the reader inside MS-DIAL Console, decodes an mzML binaryDataArray only as a 32- or
64-bit float, zlib-compressed or not (BinaryDataArrayConverter.parseCvParam, Base64StringConverter).
Every other term reaches a default branch that only calls Debug.WriteLine, and the array is then read as
if it were an uncompressed 32-bit float:

- MS-Numpress (MS:1002312-1002314, MS:1002746-1002748) and any other compression (truncation, zstd) give
  garbage peaks, or an m/z and an intensity array of different lengths, which ParseSpectrum answers by
  emptying the spectrum without a word;
- a 64-bit integer array (MS:1000522) is read as 32-bit floats; a 32-bit integer array (MS:1000519) is
  typed Integer over a float[], and the unboxing in ParseSpectrum throws;
- a binaryDataArray whose kind, type or compression comes from a referenceableParamGroupRef has them
  ignored, so the array has no content type and the spectrum comes out empty.

Nothing in the Console reports any of that, so a repository mzML written that way ran and wrote wrong or
empty spectra into the result. This module reads just enough of a file to say so first: a streaming scan
of the binaryDataArray cvParams of the first few spectra, until one MSn spectrum is among them, and of the
first few chromatograms, reached through the index of an indexed mzML, or in sequence when the spectrum
list is short. It stops there, so a file of any size costs a few megabytes of reading.

Only the arrays RawDataHandler uses are judged: m/z and intensity in a spectrum, time and intensity in a
chromatogram. An ion-mobility or charge array it parses and drops is left alone, whatever its encoding.

mzml_encoding_problems(path) is the call for anything that decides whether an mzML is an analysis input:
an empty list, or one record per distinct problem, each with reason unsupported_mzml_encoding and the
accession found. A file that cannot be scanned is not called undecodable here; scan_mzml_encoding says why
it could not be read, and the Console, which reads all of it, is the judge of that.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from xml.parsers import expat


SCAN_SCHEMA = "msdial-mzml-encoding-scan.v1"
UNSUPPORTED_MZML_ENCODING = "unsupported_mzml_encoding"

# What RawDataHandler decodes (BinaryDataArrayConverter.parseCvParam). mzxml_conversion writes only these,
# and its ALLOWED_BINARY_ACCESSIONS lint stays within them.
DECODABLE_BINARY_DATA_TYPES = frozenset({"MS:1000521", "MS:1000523"})
DECODABLE_COMPRESSIONS = frozenset({"MS:1000574", "MS:1000576"})
# The children of MS:1000518 "binary data type" and MS:1000572 "binary data compression type" in the PSI-MS
# CV (4.1.x). A compression term added later is still caught by its name (_compression_like).
BINARY_DATA_TYPES = {
    "MS:1000519": "32-bit integer",
    "MS:1000520": "16-bit float",
    "MS:1000521": "32-bit float",
    "MS:1000522": "64-bit integer",
    "MS:1000523": "64-bit float",
    "MS:1001479": "null-terminated ASCII string",
}
COMPRESSIONS = {
    "MS:1000574": "zlib compression",
    "MS:1000576": "no compression",
    "MS:1002312": "MS-Numpress linear prediction compression",
    "MS:1002313": "MS-Numpress positive integer compression",
    "MS:1002314": "MS-Numpress short logged float compression",
    "MS:1002746": "MS-Numpress linear prediction compression followed by zlib compression",
    "MS:1002747": "MS-Numpress positive integer compression followed by zlib compression",
    "MS:1002748": "MS-Numpress short logged float compression followed by zlib compression",
    "MS:1003088": "truncation and zlib compression",
    "MS:1003089": "truncation, delta prediction and zlib compression",
    "MS:1003090": "truncation, linear prediction and zlib compression",
    "MS:1003780": "zstd compression",
    "MS:1003781": "byte-shuffled zstd compression",
    "MS:1003782": "dictionary-encoded zstd compression",
    "MS:1003783": "MS-Numpress linear prediction compression followed by zstd compression",
    "MS:1003784": "MS-Numpress positive integer compression followed by zstd compression",
    "MS:1003785": "MS-Numpress short logged float compression followed by zstd compression",
    "MS:1003826": "coordinate grid encoding",
}
# The arrays RawDataHandler reads, by where they are. Any other array is parsed and dropped.
ARRAY_KINDS = {"MS:1000514": "m/z array", "MS:1000515": "intensity array", "MS:1000595": "time array"}
USED_ARRAYS = {"spectrum": ("MS:1000514", "MS:1000515"), "chromatogram": ("MS:1000595", "MS:1000515")}
# The array kinds RawDataHandler names and then discards (wavelength, non-standard).
_OTHER_KNOWN_KINDS = frozenset({"MS:1000617", "MS:1000786"})

# How far a scan reads: at least this many spectra with one MSn spectrum among them, never more than the
# cap; this many chromatograms; and never more than the byte budget, a quarter of it for the chromatograms
# reached through the index.
SCAN_SPECTRA = 5
SCAN_SPECTRA_CAP = 50
SCAN_CHROMATOGRAMS = 3
SCAN_BYTE_BUDGET = 64 * 1024 * 1024
# The tail of an indexed mzML read to find its chromatogram index.
INDEX_TAIL_BYTES = 1024 * 1024
_CHUNK = 256 * 1024
_CHROMATOGRAM_INDEX = re.compile(
    rb"<(?:\w+:)?index\s+name\s*=\s*[\"']chromatogram[\"']\s*>(.*?)</(?:\w+:)?index\s*>", re.S
)
_OFFSET = re.compile(rb"<(?:\w+:)?offset\b[^>]*>\s*(\d+)\s*<")
_CHROMATOGRAM_START = re.compile(rb"<(?:\w+:)?chromatogram[\s>]")


def mzml_encoding_problems(path: str | Path) -> list[dict[str, Any]]:
    """The binary encodings of an mzML that RawDataHandler cannot decode; [] when there are none.

    Each record is {reason: unsupported_mzml_encoding, accession, name, term, where, array, first_id,
    count}. term is compression, binary_data_type, param_group or missing_term; accession is "" for the
    last two, which are an absence, and name then says what is absent. array is the accession of the array
    it was found in, where is spectrum or chromatogram, and first_id is the id of the first one it was
    found in. A file that could not be scanned gives [] (see scan_mzml_encoding).
    """
    return list(scan_mzml_encoding(path)["problems"])


def scan_mzml_encoding(path: str | Path) -> dict[str, Any]:
    """Scan the first few spectra and chromatograms of an mzML. Never raises.

    Returns {schema, status, spectra_scanned, chromatograms_scanned, chromatograms_reached, bytes_read,
    encodings, problems}. status is scanned, or unreadable with an error. encodings lists each distinct
    {where, array, binary_data_type, compression} seen, the evidence behind an empty problem list, and
    chromatograms_reached says whether the chromatograms were read in sequence or through the index.
    """
    scan = _Scan()
    result: dict[str, Any] = {"schema": SCAN_SCHEMA, "status": "scanned"}
    try:
        with open(Path(path), "rb") as handle:
            finished = scan.feed_from(handle, 0, SCAN_BYTE_BUDGET, window=False)
            if not finished and scan.chromatograms < SCAN_CHROMATOGRAMS and not scan.chromatogram_list_ended:
                # The scan stopped among the spectra, so the chromatograms lie beyond where it stopped.
                offset = _first_chromatogram_offset(handle, scan)
                if offset is not None:
                    scan.reached = "index"
                    scan.feed_from(handle, offset, SCAN_BYTE_BUDGET // 4, window=True)
    except OSError as error:
        result.update(status="unreadable", error=f"{type(error).__name__}: {error}")
    except _Unreadable as error:
        result.update(status="unreadable", error=str(error))
    result.update(
        spectra_scanned=scan.spectra,
        chromatograms_scanned=scan.chromatograms,
        chromatograms_reached=scan.reached if scan.chromatograms else None,
        bytes_read=scan.bytes_read,
        encodings=[
            {"where": where, "array": array, "binary_data_type": data_type, "compression": compression}
            for where, array, data_type, compression in sorted(scan.encodings)
        ],
        problems=[dict(entry) for entry in scan.problems.values()],
    )
    if result["status"] == "scanned" and scan.root not in {"mzML", "indexedmzML"}:
        result.update(status="unreadable", error=f"the root element is <{scan.root or 'none'}>, not <mzML>")
    return result


class _Unreadable(Exception):
    pass


class _Stop(Exception):
    pass


def _local(name: str) -> str:
    return name.rsplit(":", 1)[-1]


def _compression_like(accession: str, name: str) -> bool:
    text = name.casefold()
    return accession in COMPRESSIONS or "compression" in text or "numpress" in text


def _array_kind_like(accession: str, name: str) -> bool:
    """Whether a cvParam names what an array holds: a known kind, or any term named "... array"."""
    return accession in ARRAY_KINDS or accession in _OTHER_KNOWN_KINDS or name.casefold().endswith(" array")


def _first_chromatogram_offset(handle: Any, scan: "_Scan") -> int | None:
    """The byte offset of the first chromatogram an indexed mzML's index names, when its tail holds it."""
    handle.seek(0, 2)
    size = handle.tell()
    start = max(0, size - INDEX_TAIL_BYTES)
    handle.seek(start)
    tail = handle.read(size - start)
    scan.bytes_read += len(tail)
    match = _CHROMATOGRAM_INDEX.search(tail)
    offsets = [int(value) for value in _OFFSET.findall(match.group(1))] if match else []
    if not offsets:
        return None
    offset = min(offsets)
    handle.seek(offset)
    # An index whose offset does not land on a chromatogram is not followed.
    return offset if _CHROMATOGRAM_START.match(handle.read(64).lstrip()) else None


class _Scan:
    """What the expat passes over one file have seen: counts, encodings and problems."""

    def __init__(self) -> None:
        self.root = ""
        self.spectra = 0
        self.chromatograms = 0
        self.msn_seen = False
        self.bytes_read = 0
        self.reached = "sequence"
        self.chromatogram_list_ended = False
        self.encodings: set[tuple[str, str, str, str]] = set()
        self.problems: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.stack: list[str] = []
        self.item: dict[str, Any] | None = None
        self.array: dict[str, Any] | None = None

    def spectra_done(self) -> bool:
        return self.spectra >= SCAN_SPECTRA_CAP or (self.spectra >= SCAN_SPECTRA and self.msn_seen)

    def feed_from(self, handle: Any, offset: int, budget: int, *, window: bool) -> bool:
        """Parse from offset until the scan has enough, the budget is spent or the file ends.

        Returns True only when the file was read to its end. A window at an index offset is parsed inside
        an element of its own. Namespaces are left unprocessed, since such a window has lost the
        declarations its prefixes would need, and only local names are read.
        """
        self.stack, self.item, self.array = [], None, None
        parser = expat.ParserCreate()
        parser.buffer_text = False
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        handle.seek(offset)
        read = 0
        try:
            if window:
                parser.Parse(b"<window>", False)
            while read < budget:
                chunk = handle.read(min(_CHUNK, budget - read))
                read += len(chunk)
                self.bytes_read += len(chunk)
                parser.Parse(chunk, not chunk)
                if not chunk:
                    return True
            return False
        except _Stop:
            return False
        except expat.ExpatError as error:
            if window or self.spectra or self.chromatograms:
                # A window read past the end of its list, or a file cut short after what was read: what was
                # read stands.
                return False
            raise _Unreadable(f"the file is not well-formed XML: {error}") from error

    def _start(self, name: str, attributes: dict[str, str]) -> None:
        local = _local(name)
        parent = self.stack[-1] if self.stack else ""
        if not self.stack and not self.root:
            self.root = local
        self.stack.append(local)
        if local in ("spectrum", "chromatogram"):
            self.item = {
                "where": local,
                "id": str(attributes.get("id") or attributes.get("index") or ""),
                "length": str(attributes.get("defaultArrayLength") or ""),
                "kinds": set(),
                "ms_level": "",
                "param_group": False,
            }
        elif self.item is None:
            return
        elif local == "binaryDataArray":
            self.array = {"cv": [], "groups": []}
        elif local == "cvParam":
            accession = str(attributes.get("accession") or "")
            if self.array is not None and parent == "binaryDataArray":
                self.array["cv"].append((accession, str(attributes.get("name") or "")))
            elif parent == "spectrum" and accession == "MS:1000511":
                self.item["ms_level"] = str(attributes.get("value") or "")
        elif local == "referenceableParamGroupRef" and self.array is not None and parent == "binaryDataArray":
            self.array["groups"].append(str(attributes.get("ref") or ""))

    def _end(self, name: str) -> None:
        local = _local(name)
        if self.stack:
            self.stack.pop()
        if local == "binaryDataArray" and self.array is not None and self.item is not None:
            self._judge_array(self.item, self.array)
            self.array = None
        elif local in ("spectrum", "chromatogram") and self.item is not None:
            item, self.item = self.item, None
            self._judge_item(item)
            if local == "spectrum":
                self.spectra += 1
                self.msn_seen = self.msn_seen or item["ms_level"] not in ("", "1")
                if self.spectra_done():
                    raise _Stop()
            else:
                self.chromatograms += 1
                if self.chromatograms >= SCAN_CHROMATOGRAMS:
                    raise _Stop()
        elif local == "chromatogramList":
            self.chromatogram_list_ended = True
            raise _Stop()

    def _judge_array(self, item: dict[str, Any], array: dict[str, Any]) -> None:
        where = item["where"]
        cv = array["cv"]
        kinds = [accession for accession, name in cv if _array_kind_like(accession, name)]
        if not kinds:
            if array["groups"]:
                # What it holds, and very likely its type and compression, are in a group RawDataHandler
                # never reads, so there it has no content type.
                self._problem(item, "", "binary data array", "param_group", "", group=array["groups"][0])
            return
        item["kinds"].update(kinds)
        own_kind = next((accession for accession in kinds if accession in USED_ARRAYS[where]), "")
        if not own_kind:
            # An array RawDataHandler parses and drops (ion mobility, charge, wavelength): its encoding
            # decides nothing.
            return
        types = [(accession, name) for accession, name in cv if accession in BINARY_DATA_TYPES]
        compressions = [(accession, name) for accession, name in cv if _compression_like(accession, name)]
        for accession, name in types:
            if accession not in DECODABLE_BINARY_DATA_TYPES:
                self._problem(item, accession, name or BINARY_DATA_TYPES[accession], "binary_data_type", own_kind)
        for accession, name in compressions:
            if accession not in DECODABLE_COMPRESSIONS:
                self._problem(item, accession, name or COMPRESSIONS.get(accession, ""), "compression", own_kind)
        for term, found in (("binary data type", types), ("binary data compression type", compressions)):
            if found:
                continue
            # RawDataHandler would read it as an uncompressed 32-bit float, which nothing says it is.
            if array["groups"]:
                self._problem(item, "", term, "param_group", own_kind, group=array["groups"][0])
            else:
                self._problem(item, "", term, "missing_term", own_kind)
        self.encodings.add((
            where,
            own_kind,
            "+".join(sorted(accession for accession, _ in types)) or "none",
            "+".join(sorted(accession for accession, _ in compressions)) or "none",
        ))

    def _judge_item(self, item: dict[str, Any]) -> None:
        # One with points and no m/z or intensity array RawDataHandler can name comes out empty. One whose
        # array took its kind from a group is already recorded as param_group.
        if item["length"] in ("", "0") or item["param_group"]:
            return
        for kind in USED_ARRAYS[item["where"]]:
            if kind not in item["kinds"]:
                self._problem(item, "", ARRAY_KINDS[kind], "missing_term", kind)

    def _problem(
        self, item: dict[str, Any], accession: str, name: str, term: str, array: str, group: str = ""
    ) -> None:
        if term == "param_group":
            item["param_group"] = True
        key = (item["where"], array, accession or name, term)
        entry = self.problems.get(key)
        if entry is None:
            entry = self.problems[key] = {
                "reason": UNSUPPORTED_MZML_ENCODING,
                "accession": accession,
                "name": name,
                "term": term,
                "where": item["where"],
                "array": array,
                "first_id": item["id"],
                "count": 0,
            }
            if group:
                entry["param_group"] = group
        entry["count"] += 1

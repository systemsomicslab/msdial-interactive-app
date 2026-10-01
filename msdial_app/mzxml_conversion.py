"""Convert an mzXML file into the mzML that MS-DIAL reads, and prove that nothing was lost.

MS-DIAL has no mzXML reader: MsdialCore lists no mzXML format, and RawDataHandler has no parser for
it. A repository sample whose only readable encoding is mzXML therefore could not be analysed at
all. This module writes such a file as plain mzML using only the standard library, then re-reads
what it wrote with a second, independent reader and compares every spectrum with the mzXML.

The mapping is written for the parsers that will actually read the output, not for the PSI mapping
rules alone: RawDataHandler 1.3.9699.469 in the pinned Console, and the raw-metadata extractor's
MzmlMetadataReader. Both read polarity only as a spectrum-level cvParam. RawDataHandler reads the
isolation target (MS:1000827) and the selected ion (MS:1000744) into different fields, decodes only
MS:1000521/523 and MS:1000574/576, and returns from a container only at its matching end tag, so a
self-closed <activation/> makes it read into the following spectra. Every writer rule that looks
fussy is one of those.

What the mzXML does not record is not invented. An absent window width is omitted rather than
written as 0, an absent activation method is a userParam rather than CID, an absent polarity stays
unrecorded. The four places where a value could be supplied - polarity from the unit's declared ion
mode, DIA windows from the precursor ladder, all-ion windows from the scan range, and the collision
energy repeated at spectrum level where RawDataHandler reads the AIF targets - are each behind a
flag that is off by default, and each use is recorded in the conversion record and in the mzML.

A conversion never raises into its caller. It returns a record whose status is "converted" or
"failed", and a failed conversion leaves none of this converter's output at the destination. A file
the converter did not write is never replaced or removed: the conversion fails and leaves it as it
was, since at the destination it may be the repository's own mzML. The output bytes depend only
on the mzXML, its name and repository-relative path, the options and the converter identity (which
names the zlib build), never on the workspace, a clock or the machine, so converting a re-downloaded
file reproduces the same sha256.

RawDataHandler never decodes the last element of any mzML binary array (Base64StringConverter's
loop stops one element early), so MS-DIAL loses the highest-m/z peak of every spectrum, repository
mzML included. The converter does not pad arrays to hide that; it is an upstream defect.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import math
import os
import platform
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zlib
from array import array
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from urllib.parse import quote
from xml.parsers import expat

from . import __version__


CONVERTER_NAME = "MS-DIAL Interactive mzXML to mzML converter"
CONVERSION_RECORD_SCHEMA = "msdial-mzxml-conversion.v1"
VALIDATION_SCHEMA = "msdial-mzxml-conversion-validation.v1"
ZLIB_LEVEL = 6
LADDER_TOLERANCE_MZ = 0.01
_CHUNK = 1 << 20
_SHA1_TAIL = 1 << 16
_MAX_LISTED = 256
_MAX_PROBLEMS = 25

# RawDataHandler 1.3.9699.469 decodes these and nothing else in a binaryDataArray. MS:1000519 is
# read as a 32-bit float and Numpress is not decoded, so neither may ever be written.
ALLOWED_BINARY_ACCESSIONS = frozenset(
    {"MS:1000514", "MS:1000515", "MS:1000521", "MS:1000523", "MS:1000574", "MS:1000576"}
)
# Containers that RawDataHandler walks with its "read to the matching end tag" loop, plus the other
# structural elements this writer emits. None may be self-closed or empty.
CONTAINER_ELEMENTS = frozenset(
    {
        "fileDescription", "fileContent", "sourceFileList", "sourceFile", "softwareList", "software",
        "instrumentConfigurationList", "instrumentConfiguration", "componentList", "source",
        "analyzer", "detector", "dataProcessingList", "dataProcessing", "processingMethod", "run",
        "spectrumList", "spectrum", "scanList", "scan", "scanWindowList", "scanWindow",
        "precursorList", "precursor", "isolationWindow", "selectedIonList", "selectedIon",
        "activation", "binaryDataArrayList", "binaryDataArray",
    }
)
_FLOAT_ACCESSIONS = frozenset(
    {
        "MS:1000016", "MS:1000042", "MS:1000045", "MS:1000285", "MS:1000500", "MS:1000501",
        "MS:1000504", "MS:1000505", "MS:1000527", "MS:1000528", "MS:1000744", "MS:1000827",
        "MS:1000828", "MS:1000829",
    }
)
_INTEGER_ACCESSIONS = frozenset({"MS:1000511", "MS:1000041", "MS:1000633"})

UNIT_SECOND = ("UO", "UO:0000010", "second")
UNIT_MZ = ("MS", "MS:1000040", "m/z")
UNIT_COUNTS = ("MS", "MS:1000131", "number of detector counts")
UNIT_ELECTRONVOLT = ("UO", "UO:0000266", "electronvolt")

TERM_MS1 = ("MS:1000579", "MS1 spectrum")
TERM_MSN = ("MS:1000580", "MSn spectrum")
TERM_CRM = ("MS:1000581", "CRM spectrum")
TERM_SIM = ("MS:1000582", "SIM spectrum")
TERM_SRM = ("MS:1000583", "SRM spectrum")
_FILE_CONTENT_ORDER = (TERM_MS1, TERM_MSN, TERM_CRM, TERM_SIM, TERM_SRM)
_SCAN_TYPE_TERMS = {"SIM": TERM_SIM, "SRM": TERM_SRM, "MRM": TERM_SRM, "CRM": TERM_CRM}

ACTIVATION_TERMS = {
    "CID": ("MS:1000133", "collision-induced dissociation"),
    "HCD": ("MS:1000422", "beam-type collision-induced dissociation"),
    "ETD": ("MS:1000598", "electron transfer dissociation"),
    "ECD": ("MS:1000250", "electron capture dissociation"),
    "PQD": ("MS:1000599", "pulsed q dissociation"),
    "IRMPD": ("MS:1000262", "infrared multiphoton dissociation"),
}
_SOURCE_TERMS = (
    ("nanoelectrospray", ("MS:1000398", "nanoelectrospray")),
    ("nanoesi", ("MS:1000398", "nanoelectrospray")),
    ("nsi", ("MS:1000398", "nanoelectrospray")),
    ("electrospray", ("MS:1000073", "electrospray ionization")),
    ("esi", ("MS:1000073", "electrospray ionization")),
    ("apci", ("MS:1000070", "atmospheric pressure chemical ionization")),
    ("atmosphericpressurechemical", ("MS:1000070", "atmospheric pressure chemical ionization")),
    ("appi", ("MS:1000382", "atmospheric pressure photoionization")),
    ("atmosphericpressurephoto", ("MS:1000382", "atmospheric pressure photoionization")),
    ("maldi", ("MS:1000075", "matrix-assisted laser desorption ionization")),
)
TERM_ORBITRAP = ("MS:1000484", "orbitrap")
TERM_TOF = ("MS:1000084", "time-of-flight")
TERM_FTICR = ("MS:1000079", "fourier transform ion cyclotron resonance mass spectrometer")
TERM_QUADRUPOLE = ("MS:1000081", "quadrupole")
TERM_ION_TRAP = ("MS:1000264", "ion trap")

_MS_CV = (
    '    <cv id="MS" fullName="Proteomics Standards Initiative Mass Spectrometry Ontology"'
    ' version="4.1.0" URI="https://raw.githubusercontent.com/HUPO-PSI/psi-ms-CV/master/psi-ms.obo"/>\n'
)
_UO_CV = (
    '    <cv id="UO" fullName="Unit Ontology" version="09:04:2014"'
    ' URI="https://raw.githubusercontent.com/bio-ontology-research-group/unit-ontology/master/unit.obo"/>\n'
)
_OUR_SOFTWARE_ID = "MSDIAL_Interactive_mzXML_converter"
_DEFAULT_INSTRUMENT_ID = "IC1"
_DATA_PROCESSING_ID = "MSDIAL_Interactive_mzXML_to_mzML"
# Every output starts with these bytes and records its processing this way before the run. A file
# holding both is this converter's output, from whichever source and version; nothing else is ever
# replaced or removed. msconvert and other tools that rewrite such a file change the processing list.
_OUTPUT_PREFIX = b'<?xml version="1.0" encoding="utf-8"?>\n<mzML xmlns="http://psi.hupo.org/ms/mzml"'
_OUTPUT_MARK = (
    '  <dataProcessingList count="1">\n'
    f'    <dataProcessing id="{_DATA_PROCESSING_ID}">\n'
    f'      <processingMethod order="0" softwareRef="{_OUR_SOFTWARE_ID}">\n'
)
_HEADER_LIMIT = 1 << 20

_DECIMAL_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_INTEGER_TEXT = re.compile(r"[+-]?\d+")
_DURATION = re.compile(
    r"P(?:(?P<d>\d+(?:\.\d+)?)D)?"
    r"(?:T(?=[\d.])(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<s>\d+(?:\.\d*)?|\.\d+)S)?)?"
)
_HEX40 = re.compile(r"[0-9a-f]{40}")
_BASE64_TEXT = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_WHITESPACE = re.compile(r"\s")


class ConversionError(Exception):
    """A file the converter will not write, with the reason that goes into the record."""


@dataclass(frozen=True)
class ConversionOptions:
    """What the converter may do beyond copying what the mzXML records. Every inference is off.

    impute_polarity: "positive" or "negative", the unit's declared ion mode, written for scans whose
        polarity is absent or "any". The file fails when any scan records the other polarity.
    infer_dia_windows: give MS2 precursors without windowWideness a symmetric window of the spacing
        of a uniform, repeated precursor ladder (SWATH). Not applied when the ladder is irregular.
    synthesize_all_ion_windows: give MS2 scans without precursorMz a window spanning the scan range,
        as RawDataHandler's Agilent reader does for all-ion data.
    spectrum_level_collision_energy: repeat the collision energy as a spectrum-level cvParam, where
        the pinned RawDataHandler reads the AIF collision-energy targets. This deviates from the PSI
        mapping rules, which place it under activation only.
    fail_on_sha1_mismatch: an embedded mzXML sha1 that matches neither known span fails the file.
    """

    impute_polarity: str | None = None
    infer_dia_windows: bool = False
    synthesize_all_ion_windows: bool = False
    spectrum_level_collision_energy: bool = False
    fail_on_sha1_mismatch: bool = True


def converter_identity() -> dict[str, Any]:
    """What produced an output: enough to know whether a recorded conversion is still reproducible."""
    return {
        "name": CONVERTER_NAME,
        "version": __version__,
        "module_sha256": _sha256_file(Path(__file__)),
        "python": platform.python_version(),
        "zlib": zlib.ZLIB_RUNTIME_VERSION,
        "zlib_level": ZLIB_LEVEL,
        "expat": expat.EXPAT_VERSION,
    }


def convert_mzxml_to_mzml(
    source: str | Path,
    destination: str | Path,
    options: ConversionOptions | dict[str, Any] | None = None,
    *,
    source_relative_path: str | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write ``source`` (mzXML 2.x or 3.x) as mzML at ``destination`` and return the record.

    ``source_relative_path`` is the file's path inside the repository listing; it names the source
    in the record and in the mzML, whose bytes must not depend on where the workspace lives.
    ``previous`` is an earlier record of the same conversion: when its source sha256, name and
    relative path, output sha256, options and converter identity all hold for this call's files,
    the file is not written again. The record returned then names this call's paths and times, and
    keeps where and when the output was written under "reused_from", since the recorded files may
    have been another unit's or the same files under another spelling of their path.

    Never raises. The record's status is "converted" or "failed"; on failure the error says why and
    none of this converter's output is left at the destination. Nothing at the destination is
    touched for an argument error (options, destination, missing source), and a file there that
    this converter did not write is never replaced or removed.
    """
    started_at = _now()
    source = Path(source)
    destination = Path(destination)
    identity = converter_identity()
    record: dict[str, Any] = {
        "schema": CONVERSION_RECORD_SCHEMA,
        "status": "failed",
        "error": None,
        "converter": identity,
        "options": None,
        "source": {
            "path": str(source),
            "relative_path": source_relative_path or source.name,
            "name": source.name,
        },
        "output": {"path": str(destination)},
        "reused_previous_record": False,
        "warnings": [],
        "inferences": [],
        "deviations": [],
        "started_at": started_at,
        "completed_at": None,
    }
    partial = destination.with_name(destination.name + ".partial")
    body = destination.with_name(destination.name + ".body.partial")
    # Only an .mzML that is not the source is ever written, whatever the caller passed.
    acceptable = destination.suffix.casefold() == ".mzml" and _distinct(source, destination)
    # Set once the arguments hold and the destination is absent or this converter's own output.
    # Until then a failure touches nothing at the destination, its partial files included.
    owned = False
    try:
        opts = _options(options)
        record["options"] = asdict(opts)
        if not acceptable:
            raise ConversionError(
                f"the destination must be an .mzML file other than the source, not {destination.name!r}"
            )
        if not source.is_file():
            raise ConversionError(f"the source mzXML does not exist: {source}")
        _refuse_foreign_destination(destination, previous, record)
        owned = True
        integrity = _source_integrity(source)
        record["source"].update(integrity)
        if _reusable(previous, record, destination, identity):
            return _reused(previous, record)
        embedded = integrity["embedded_sha1"]
        if embedded["status"] in {"mismatch", "malformed"}:
            message = (
                f"the mzXML's embedded sha1 ({embedded['declared'] or 'unreadable'}) does not match"
                f" the file ({embedded['computed_including_tag']})"
            )
            if opts.fail_on_sha1_mismatch:
                raise ConversionError(message)
            record["warnings"].append(message)
        windows = _infer_dia_windows(source) if opts.infer_dia_windows else None
        destination.parent.mkdir(parents=True, exist_ok=True)
        summary = _write_mzml(
            source,
            partial,
            body,
            opts,
            windows,
            source_name=source.name,
            source_location=_relative_location(source_relative_path),
            source_sha1=integrity["sha1"],
        )
        for key in ("warnings", "inferences", "deviations"):
            record[key].extend(summary.pop(key))
        record.update(summary)
        record["output"].update({"bytes": partial.stat().st_size, "sha256": _sha256_file(partial)})
        validation = _validate(source, partial, opts, windows)
        record["validation"] = validation
        if validation["status"] != "passed":
            raise ConversionError(
                "the written mzML does not reproduce the mzXML: " + "; ".join(validation["problems"][:3])
            )
        # A file may have appeared at the destination while the conversion ran.
        _refuse_foreign_destination(destination, previous, record)
        partial.replace(destination)
        record["status"] = "converted"
    except Exception as exc:  # noqa: BLE001 - a conversion is recorded, never raised
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        if owned and destination.is_file() and _written_by_converter(destination, previous):
            # A stale output of this converter; leaving it would let input discovery pick up an mzML
            # this record says is not a valid conversion.
            try:
                destination.unlink()
                record["output"]["removed_after_failure"] = True
            except OSError as unlink_error:
                record["warnings"].append(f"could not remove {destination}: {unlink_error}")
    finally:
        if owned:
            for leftover in (partial, body):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
        record["completed_at"] = _now()
    return record


def validate_conversion(
    source: str | Path,
    destination: str | Path,
    options: ConversionOptions | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-read ``destination`` with an independent reader and compare it with ``source``.

    ``options`` must be those the conversion used, since they decide the expected polarity and
    windows. Every spectrum is compared on msLevel, retention time, polarity, representation,
    precursor m/z, window, charge and collision energy, and on the exact bytes of both arrays; the
    file is linted for what the pinned RawDataHandler cannot read. Never raises.
    """
    try:
        opts = _options(options)
        windows = _infer_dia_windows(Path(source)) if opts.infer_dia_windows else None
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return _validation_failure(f"{type(exc).__name__}: {exc}")
    return _validate(Path(source), Path(destination), opts, windows)


# --------------------------------------------------------------------------------------------------
# Options, numbers and small helpers


def _options(options: ConversionOptions | dict[str, Any] | None) -> ConversionOptions:
    if options is None:
        opts = ConversionOptions()
    elif isinstance(options, ConversionOptions):
        opts = options
    elif isinstance(options, dict):
        unknown = sorted(set(options) - set(ConversionOptions.__dataclass_fields__))
        if unknown:
            raise ConversionError(f"unknown conversion options: {', '.join(unknown)}")
        opts = ConversionOptions(**options)
    else:
        raise ConversionError("options must be ConversionOptions or a dict")
    for name in (
        "infer_dia_windows",
        "synthesize_all_ion_windows",
        "spectrum_level_collision_energy",
        "fail_on_sha1_mismatch",
    ):
        if not isinstance(getattr(opts, name), bool):
            raise ConversionError(f"option {name} must be true or false")
    if opts.impute_polarity is not None:
        polarity = {
            "positive": "positive", "pos": "positive", "+": "positive",
            "negative": "negative", "neg": "negative", "-": "negative",
        }.get(str(opts.impute_polarity).strip().casefold())
        if polarity is None:
            raise ConversionError(
                f"impute_polarity must be positive or negative, not {opts.impute_polarity!r}"
            )
        opts = replace(opts, impute_polarity=polarity)
    return opts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _distinct(source: Path, destination: Path) -> bool:
    try:
        return os.path.normcase(source.resolve()) != os.path.normcase(destination.resolve())
    except OSError:
        return False


def _float(text: str | None, what: str) -> float | None:
    if text is None or not text.strip():
        return None
    text = text.strip()
    if not _DECIMAL_TEXT.fullmatch(text):
        raise ConversionError(f"{what} is not a number: {text!r}")
    value = float(text)
    if not math.isfinite(value):
        raise ConversionError(f"{what} is not finite: {text!r}")
    return value


def _int(text: str | None, what: str) -> int | None:
    if text is None or not text.strip():
        return None
    text = text.strip()
    if not _INTEGER_TEXT.fullmatch(text):
        raise ConversionError(f"{what} is not an integer: {text!r}")
    return int(text)


def _bool(text: str | None, what: str) -> bool | None:
    if text is None or not text.strip():
        return None
    value = text.strip().casefold()
    if value in {"1", "true"}:
        return True
    if value in {"0", "false"}:
        return False
    raise ConversionError(f"{what} is not a boolean: {text!r}")


def _seconds(text: str | None, what: str) -> float | None:
    """An xs:duration as seconds, summed exactly before the one rounding to a float."""
    if text is None or not text.strip():
        return None
    match = _DURATION.fullmatch(text.strip())
    if not match or not any(match.groupdict().values()):
        raise ConversionError(f"{what} is not a non-negative xs:duration: {text!r}")
    total = Decimal(0)
    for key, factor in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if match.group(key) is not None:
            total += Decimal(match.group(key)) * factor
    return float(total)


def _num(value: float) -> str:
    """The shortest text that parses back to the same double, in the invariant culture."""
    text = repr(float(value))
    if not math.isfinite(float(value)):
        raise ConversionError(f"refusing to write a non-finite number: {text}")
    return text


def _attr(value: Any) -> str:
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\t", "&#9;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
    )


def _xml_id(text: str, used: set[str]) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_.-]", "_", text) or "_"
    if not re.match(r"[A-Za-z_]", candidate):
        candidate = "_" + candidate
    unique, suffix = candidate, 2
    while unique in used:
        unique = f"{candidate}_{suffix}"
        suffix += 1
    used.add(unique)
    return unique


def _relative_location(relative_path: str | None) -> str:
    """The source's directory inside the repository listing, never the local workspace path."""
    if not relative_path:
        return "."
    parent = PurePosixPath(str(relative_path).replace("\\", "/")).parent.as_posix()
    return quote(parent, safe="/") if parent not in {"", "."} else "."


def _le_bytes(values: array) -> bytes:
    if sys.byteorder == "little":
        return values.tobytes()
    swapped = array(values.typecode, values)
    swapped.byteswap()
    return swapped.tobytes()


def _cv(indent: str, term: tuple[str, str], value: str = "", unit: tuple[str, str, str] | None = None) -> str:
    accession, name = term
    unit_text = ""
    if unit is not None:
        unit_text = f' unitCvRef="{unit[0]}" unitAccession="{unit[1]}" unitName="{_attr(unit[2])}"'
    return (
        f'{indent}<cvParam cvRef="{accession.split(":", 1)[0]}" accession="{accession}"'
        f' name="{_attr(name)}" value="{_attr(value)}"{unit_text}/>\n'
    )


def _user(indent: str, name: str, value: str) -> str:
    return f'{indent}<userParam name="{_attr(name)}" value="{_attr(value)}"/>\n'


def _listed(values: set[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {"count": len(ordered), "values": ordered[:_MAX_LISTED], "truncated": len(ordered) > _MAX_LISTED}


# --------------------------------------------------------------------------------------------------
# Source integrity: sha256 and sha1 of the whole file, and the mzXML's own embedded sha1


def _source_integrity(path: Path) -> dict[str, Any]:
    """Hash the file once, checking the embedded sha1 on the way.

    The mzXML schema says the sha1 covers the file from its first byte up to and including the
    opening <sha1> tag. Writers are not all known, so a digest that stops just before the tag is
    also accepted, and the record says which span matched.

    The md5 is taken in the same pass because it is what the repositories publish: a source whose
    declared MD5 was verified can then be tied to the very bytes this conversion read.
    """
    size = path.stat().st_size
    declared, tag_offset = _embedded_sha1(path, size)
    whole256, whole1, whole5 = hashlib.sha256(), hashlib.sha1(), hashlib.md5()
    prefix = hashlib.sha1() if tag_offset is not None else None
    before_tag = including_tag = None
    boundary_before = tag_offset if tag_offset is not None else -1
    boundary_including = boundary_before + len(b"<sha1>")
    position = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            whole256.update(chunk)
            whole1.update(chunk)
            whole5.update(chunk)
            if prefix is not None and position < boundary_including:
                piece = chunk[: boundary_including - position]
                if before_tag is None and position + len(piece) >= boundary_before:
                    cut = boundary_before - position
                    prefix.update(piece[:cut])
                    before_tag = prefix.copy().hexdigest()
                    prefix.update(piece[cut:])
                else:
                    prefix.update(piece)
                if position + len(piece) >= boundary_including:
                    including_tag = prefix.hexdigest()
            position += len(chunk)
    if tag_offset is None:
        status, span = "absent", None
    elif not declared or not _HEX40.fullmatch(declared):
        status, span = "malformed", None
    elif declared == including_tag:
        status, span = "verified", "through_opening_tag"
    elif declared == before_tag:
        status, span = "verified", "before_opening_tag"
    else:
        status, span = "mismatch", None
    return {
        "bytes": size,
        "sha256": whole256.hexdigest(),
        "sha1": whole1.hexdigest(),
        "md5": whole5.hexdigest(),
        "embedded_sha1": {
            "status": status,
            "declared": declared,
            "span": span,
            "computed_including_tag": including_tag,
            "computed_before_tag": before_tag,
        },
    }


def _embedded_sha1(path: Path, size: int) -> tuple[str | None, int | None]:
    with open(path, "rb") as handle:
        start = max(0, size - _SHA1_TAIL)
        handle.seek(start)
        tail = handle.read()
    at = tail.rfind(b"<sha1>")
    if at < 0:
        return None, None
    end = tail.find(b"</sha1>", at)
    if end < 0:
        return "", start + at
    return tail[at + len(b"<sha1>") : end].strip().decode("ascii", "replace").casefold(), start + at


def _reusable(
    previous: dict[str, Any] | None,
    record: dict[str, Any],
    destination: Path,
    identity: dict[str, Any],
) -> bool:
    if not isinstance(previous, dict) or previous.get("status") != "converted":
        return False
    recorded_output = previous.get("output") or {}
    recorded_source = previous.get("source") or {}
    # The recorded paths are not compared: the bytes are, and every input to them (the source's bytes,
    # name and relative path, the options and the converter) must hold for this call's files.
    if (
        recorded_source.get("sha256") != record["source"]["sha256"]
        or recorded_source.get("name") != record["source"]["name"]
        or recorded_source.get("relative_path") != record["source"]["relative_path"]
        or previous.get("options") != record["options"]
        or previous.get("converter") != identity
        or not destination.is_file()
        or not recorded_output.get("sha256")
    ):
        return False
    return _sha256_file(destination) == recorded_output["sha256"]


def _reused(previous: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """``previous`` as the record of this call, which names this call's files and times.

    Where and when the output was actually written moves to "reused_from", which a chain of reuses
    carries unchanged from the conversion that wrote the bytes. ``previous`` is not modified.
    """
    reused = copy.deepcopy(previous)
    reused["reused_from"] = reused.get("reused_from") or {
        "source_path": (previous.get("source") or {}).get("path"),
        "output_path": (previous.get("output") or {}).get("path"),
        "started_at": previous.get("started_at"),
        "completed_at": previous.get("completed_at"),
    }
    reused["source"] = {**(reused.get("source") or {}), **record["source"]}
    reused["output"] = {**(reused.get("output") or {}), "path": record["output"]["path"]}
    reused.pop("checked_at", None)
    reused.update(reused_previous_record=True, started_at=record["started_at"], completed_at=_now())
    return reused


def _written_by_converter(path: Path, previous: dict[str, Any] | None) -> bool:
    """Whether ``path`` is a regular file this converter wrote.

    It is when it begins as every output does and carries this converter's processing record, or
    when it is byte for byte the output ``previous`` records. A link is never taken as one.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with open(path, "rb") as handle:
            header = handle.read(_HEADER_LIMIT).split(b"\n  <run ", 1)[0]
        if header.startswith(_OUTPUT_PREFIX) and _OUTPUT_MARK.encode("ascii") in header:
            return True
        if not isinstance(previous, dict) or previous.get("status") != "converted":
            return False
        recorded = previous.get("output") or {}
        if not recorded.get("sha256") or recorded.get("bytes") != path.stat().st_size:
            return False
        return _sha256_file(path) == recorded["sha256"]
    except OSError:
        return False


def _refuse_foreign_destination(destination: Path, previous: dict[str, Any] | None, record: dict[str, Any]) -> None:
    """Fail the conversion, leaving the file alone, when something the converter did not write is there."""
    if os.path.lexists(destination) and not _written_by_converter(destination, previous):
        record["output"]["foreign_file_kept"] = True
        raise ConversionError(f"the destination exists and was not written by the converter: {destination}")


# --------------------------------------------------------------------------------------------------
# Reading mzXML


@dataclass
class _Peaks:
    content: str
    precision: int
    values: array


@dataclass
class _MzxmlScan:
    attributes: dict[str, str]
    precursors: list[tuple[dict[str, str], str]] = field(default_factory=list)
    peaks: list[_Peaks] = field(default_factory=list)
    emitted: bool = False


class _MzxmlReader:
    """Streams the scans of an mzXML 2.x or 3.x file in document order, nested scans flattened.

    The namespace is ignored, so every schema revision reads the same way. A scan is handed on when
    its first nested scan starts or when it ends, whichever is first; the schema puts precursorMz
    and peaks before nested scans, and content found after one fails the file. Finished scans and
    index offsets are removed from the tree, so memory does not grow with the file.
    """

    def __init__(self, path: Path, decode_peaks: bool = True) -> None:
        self.path = path
        self.decode_peaks = decode_peaks
        self.namespace = ""
        self.version = ""
        self.declared_scan_count: int | None = None
        self.parent_files: list[dict[str, str]] = []
        self.instruments: list[dict[str, Any]] = []
        self.software: list[dict[str, str]] = []
        self.centroided_default: bool | None = None

    def scans(self) -> Iterator[_MzxmlScan]:
        elements: list[ET.Element] = []
        open_scans: list[_MzxmlScan] = []
        for event, element in ET.iterparse(str(self.path), events=("start", "end")):
            name = _local(element.tag)
            if event == "start":
                if not elements:
                    if name != "mzXML":
                        raise ConversionError(f"the root element is <{name}>, not <mzXML>")
                    self.namespace = element.tag[1:].split("}", 1)[0] if element.tag.startswith("{") else ""
                    match = re.search(r"mzXML_(\d+(?:\.\d+)*)", self.namespace)
                    self.version = match.group(1) if match else ""
                elif name == "msRun":
                    try:
                        self.declared_scan_count = _int(element.get("scanCount"), "msRun scanCount")
                    except ConversionError:
                        self.declared_scan_count = None
                elif name == "scan":
                    if open_scans and not open_scans[-1].emitted:
                        open_scans[-1].emitted = True
                        yield open_scans[-1]
                    open_scans.append(_MzxmlScan(dict(element.attrib)))
                elements.append(element)
                continue

            elements.pop()
            parent = elements[-1] if elements else None
            if name in {"peaks", "precursorMz"}:
                if not open_scans or parent is None or _local(parent.tag) != "scan":
                    raise ConversionError(f"<{name}> found outside a scan")
                current = open_scans[-1]
                if current.emitted:
                    raise ConversionError(
                        f"scan {current.attributes.get('num')} has <{name}> after a nested scan"
                    )
                if name == "peaks":
                    if self.decode_peaks:
                        current.peaks.append(_decode_peaks(element.attrib, element.text))
                    element.text = None
                else:
                    current.precursors.append((dict(element.attrib), element.text or ""))
            elif name == "scan":
                current = open_scans.pop()
                if not current.emitted:
                    current.emitted = True
                    yield current
                element.clear()
                if parent is not None:
                    parent.remove(element)
            elif name == "parentFile":
                self.parent_files.append(dict(element.attrib))
            elif name == "msInstrument":
                self.instruments.append(_read_instrument(element))
            elif name == "instrument":
                # mzXML before 2.0 put the instrument in attributes of one element.
                self.instruments.append(
                    _describe_instrument(
                        element.get("id") or "",
                        manufacturer=element.get("manufacturer") or "",
                        model=element.get("model") or "",
                        ionisation=element.get("ionisation") or "",
                        analyzers=[value for value in [element.get("msType") or ""] if value],
                        detector=element.get("detector") or "",
                        resolution="",
                        software=[],
                    )
                )
            elif name == "dataProcessing":
                centroided = _bool(element.get("centroided"), "dataProcessing centroided")
                if self.centroided_default is None:
                    self.centroided_default = centroided
                for child in element:
                    if _local(child.tag) == "software":
                        self.software.append(_software_entry(child))
            elif name == "offset" and parent is not None:
                parent.remove(element)


def _software_entry(element: ET.Element) -> dict[str, str]:
    return {
        "type": (element.get("type") or "").strip(),
        "name": (element.get("name") or "").strip(),
        "version": (element.get("version") or "").strip(),
    }


def _read_instrument(element: ET.Element) -> dict[str, Any]:
    fields: dict[str, str] = {}
    analyzers: list[str] = []
    software: list[dict[str, str]] = []
    for child in element:
        name = _local(child.tag)
        value = (child.get("value") or "").strip()
        if name == "msMassAnalyzer":
            if value:
                analyzers.append(value)
        elif name == "software":
            software.append(_software_entry(child))
        elif name in {"msManufacturer", "msModel", "msIonisation", "msDetector", "msResolution"}:
            fields[name] = value
    return _describe_instrument(
        element.get("msInstrumentID") or element.get("id") or "",
        manufacturer=fields.get("msManufacturer", ""),
        model=fields.get("msModel", ""),
        ionisation=fields.get("msIonisation", ""),
        analyzers=analyzers,
        detector=fields.get("msDetector", ""),
        resolution=fields.get("msResolution", ""),
        software=software,
    )


def _describe_instrument(identifier: str, **values: Any) -> dict[str, Any]:
    """The instrument as recorded, plus the CV terms the text maps to without guessing."""
    analyzer_terms: list[tuple[str, str]] = []
    for analyzer in values["analyzers"]:
        for term in _analyzer_terms(analyzer):
            if term not in analyzer_terms:
                analyzer_terms.append(term)
    source_term = _source_term(values["ionisation"])
    return {
        "id": identifier,
        **values,
        "analyzer_terms": [list(term) for term in analyzer_terms],
        "analyzer_family": _analyzer_family(values["analyzers"]),
        "source_term": list(source_term) if source_term else None,
    }


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _analyzer_terms(text: str) -> list[tuple[str, str]]:
    squashed = _squash(text)
    if "orbitrap" in squashed:
        return [TERM_ORBITRAP]
    if "iontrap" in squashed or squashed in {"itms", "lit", "it", "lcq", "ltq"}:
        return [TERM_ION_TRAP]
    if "cyclotron" in squashed or "fticr" in squashed:
        return [TERM_FTICR]
    if "tof" in squashed or "timeofflight" in squashed:
        if "quadrupole" in squashed or squashed.startswith("q"):
            return [TERM_QUADRUPOLE, TERM_TOF]
        return [TERM_TOF]
    if "quadrupole" in squashed or squashed in {"q", "qqq", "tq", "tqms"}:
        return [TERM_QUADRUPOLE]
    # "FTMS" names Orbitrap and FT-ICR alike, so it gets no analyzer term, only its family.
    return []


def _analyzer_family(analyzers: list[str]) -> str:
    families = []
    for analyzer in analyzers:
        squashed = _squash(analyzer)
        if any(token in squashed for token in ("orbitrap", "cyclotron", "fticr", "ftms")):
            families.append("fourier_transform")
        elif "tof" in squashed or "timeofflight" in squashed:
            families.append("time_of_flight")
        elif "iontrap" in squashed or squashed in {"itms", "lit", "it", "lcq", "ltq"}:
            families.append("ion_trap")
        elif "quadrupole" in squashed or squashed in {"q", "qqq", "tq", "tqms"}:
            families.append("quadrupole")
    if not families:
        return "unrecorded"
    # The analyser that measures the recorded spectra comes last in a hybrid's description.
    return families[-1]


def _source_term(text: str) -> tuple[str, str] | None:
    squashed = _squash(text)
    if not squashed:
        return None
    for token, term in _SOURCE_TERMS:
        if squashed == token or (len(token) > 4 and token in squashed):
            return term
    return None


def _decode_peaks(attributes: dict[str, str], text: str | None) -> _Peaks:
    precision = (attributes.get("precision") or "32").strip()
    if precision not in {"32", "64"}:
        raise ConversionError(f"peaks precision {precision!r} is not 32 or 64")
    byte_order = (attributes.get("byteOrder") or "network").strip()
    if byte_order != "network":
        raise ConversionError(f"peaks byteOrder {byte_order!r} is not network")
    content = (attributes.get("contentType") or attributes.get("pairOrder") or "m/z-int").strip()
    if content not in {"m/z-int", "m/z", "intensity"}:
        raise ConversionError(f"peaks contentType {content!r} is not m/z-int, m/z or intensity")
    compression = (attributes.get("compressionType") or "none").strip()
    if compression not in {"none", "zlib"}:
        raise ConversionError(f"peaks compressionType {compression!r} is not none or zlib")
    encoded = "".join((text or "").split())
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConversionError(f"peaks are not valid base64: {exc}") from None
    if compression == "zlib" and raw:
        try:
            raw = zlib.decompress(raw)
        except zlib.error as exc:
            raise ConversionError(f"zlib peaks do not decompress: {exc}") from None
    values = array("f" if precision == "32" else "d")
    if len(raw) % values.itemsize:
        raise ConversionError(f"{len(raw)} peak bytes are not a whole number of {precision}-bit values")
    values.frombytes(raw)
    if sys.byteorder == "little":
        values.byteswap()
    return _Peaks(content, int(precision), values)


# --------------------------------------------------------------------------------------------------
# From mzXML scans to the spectra that are written, and compared


@dataclass
class _Precursor:
    selected_mz: float
    target_mz: float
    lower_offset: float | None
    upper_offset: float | None
    window_basis: str
    charge: int | None
    possible_charges: list[int]
    intensity: float | None
    spectrum_ref: str | None
    activation_term: tuple[str, str] | None
    activation_text: str | None
    collision_energy: float | None


@dataclass
class _Spectrum:
    index: int
    num: int
    ms_level: int
    spectrum_term: tuple[str, str]
    polarity: str | None
    polarity_imputed: bool
    centroided: bool | None
    retention_time: float
    filter_line: str | None
    zoom: bool
    scan_window: tuple[float, float] | None
    descriptive: list[tuple[tuple[str, str], float, tuple[str, str, str]]]
    spectrum_collision_energy: float | None
    precursors: list[_Precursor]
    instrument_ref: str | None
    mz: array
    intensity: array
    intensity_bits: int


class _SpectrumBuilder:
    """Turns mzXML scans into spectra under the options, counting everything the record reports.

    The writer and the validator both build spectra through this class, so the validator compares
    the independently re-read mzML with exactly what the converter meant to write.
    """

    def __init__(
        self,
        reader: _MzxmlReader,
        options: ConversionOptions,
        windows: dict[str, Any] | None,
    ) -> None:
        self.reader = reader
        self.options = options
        self.windows = windows if windows and windows.get("applied") else None
        self.window_targets = set(self.windows["target_values"]) if self.windows else set()
        self.instrument_ids: dict[str, str] = {}
        self.nums: set[int] = set()
        self.count = 0
        self.ms_levels: Counter = Counter()
        self.polarity: Counter = Counter()
        self.recorded_polarity: Counter = Counter()
        self.representation: dict[int, Counter] = {}
        self.scan_types: Counter = Counter()
        self.spectrum_terms: Counter = Counter()
        self.activation: Counter = Counter()
        self.precursor_counts: Counter = Counter()
        self.dropped: Counter = Counter()
        self.synthesis_basis: Counter = Counter()
        self.intensity_bits: Counter = Counter()
        self.mz_source_bits: Counter = Counter()
        self.ms1_energies: set[float] = set()
        self.msn_energies: set[float] = set()
        self.empty = 0
        self.placeholders = 0
        self.imputed = 0
        self.inferred_windows = 0
        self.spectrum_level_energies = 0
        self.retention_time_first: float | None = None
        self.retention_time_last: float | None = None
        self.retention_time_range: tuple[float, float] | None = None

    def spectra(self) -> Iterator[_Spectrum]:
        for scan in self.reader.scans():
            if not self.count:
                # The instruments precede the first scan, so their ids are settled by now.
                self.instrument_ids = _instrument_ids(self.reader.instruments)
            yield self._build(scan)
            self.count += 1
        self._finish()

    def _finish(self) -> None:
        if self.count == 0:
            raise ConversionError("the mzXML contains no scans")
        if self.imputed:
            opposite = "negative" if self.options.impute_polarity == "positive" else "positive"
            if self.recorded_polarity[opposite]:
                raise ConversionError(
                    f"polarity imputation refused: {self.recorded_polarity[opposite]} scans record"
                    f" {opposite} polarity, so the declared {self.options.impute_polarity} ion mode"
                    " cannot stand for the scans that record none"
                )

    def _build(self, scan: _MzxmlScan) -> _Spectrum:
        attributes = scan.attributes
        num = _int(attributes.get("num"), "scan num")
        if num is None or num < 0:
            raise ConversionError("a scan has no num")
        if num in self.nums:
            raise ConversionError(f"scan num {num} occurs twice")
        self.nums.add(num)
        where = f"scan {num}"
        ms_level = _int(attributes.get("msLevel"), f"{where} msLevel")
        if ms_level is None or ms_level < 1:
            raise ConversionError(f"{where} has no msLevel of at least 1")
        peaks_count = _int(attributes.get("peaksCount"), f"{where} peaksCount")
        if peaks_count is None or peaks_count < 0:
            raise ConversionError(f"{where} has no peaksCount")
        retention_time = _seconds(attributes.get("retentionTime"), f"{where} retentionTime")
        if retention_time is None:
            raise ConversionError(f"{where} has no retentionTime, which MS-DIAL would read as 0 min")
        if self.retention_time_first is None:
            self.retention_time_first = retention_time
        self.retention_time_last = retention_time
        low, high = self.retention_time_range or (retention_time, retention_time)
        self.retention_time_range = (min(low, retention_time), max(high, retention_time))

        polarity, imputed = self._polarity(attributes.get("polarity"), where)
        centroided = _bool(attributes.get("centroided"), f"{where} centroided")
        if centroided is None:
            centroided = self.reader.centroided_default
        representation = "unrecorded" if centroided is None else ("centroid" if centroided else "profile")
        self.representation.setdefault(ms_level, Counter())[representation] += 1
        self.ms_levels[ms_level] += 1

        scan_type = (attributes.get("scanType") or "").strip() or None
        self.scan_types[scan_type or "unrecorded"] += 1
        spectrum_term = _SCAN_TYPE_TERMS.get((scan_type or "").upper()) or (
            TERM_MS1 if ms_level == 1 else TERM_MSN
        )
        self.spectrum_terms[spectrum_term] += 1

        collision_energy = _float(attributes.get("collisionEnergy"), f"{where} collisionEnergy")
        if collision_energy is not None:
            (self.ms1_energies if ms_level == 1 else self.msn_energies).add(collision_energy)

        start_mz = self._descriptive(attributes, "startMz", where)
        end_mz = self._descriptive(attributes, "endMz", where)
        scan_window = (start_mz, end_mz) if start_mz is not None and end_mz is not None else None
        low_mz = self._descriptive(attributes, "lowMz", where)
        high_mz = self._descriptive(attributes, "highMz", where)
        observed = {"lowMz": low_mz, "highMz": high_mz}
        descriptive = []
        for attribute, term, unit in (
            ("lowMz", ("MS:1000528", "lowest observed m/z"), UNIT_MZ),
            ("highMz", ("MS:1000527", "highest observed m/z"), UNIT_MZ),
            ("basePeakMz", ("MS:1000504", "base peak m/z"), UNIT_MZ),
            ("basePeakIntensity", ("MS:1000505", "base peak intensity"), UNIT_COUNTS),
            ("totIonCurrent", ("MS:1000285", "total ion current"), UNIT_COUNTS),
        ):
            value = observed[attribute] if attribute in observed else self._descriptive(attributes, attribute, where)
            if value is not None:
                descriptive.append((term, value, unit))

        mz, intensity, intensity_bits, mz_bits = self._arrays(scan, peaks_count, where)
        self.intensity_bits[intensity_bits] += 1
        if mz_bits:
            self.mz_source_bits[mz_bits] += 1
        if not len(mz):
            self.empty += 1

        precursors = [
            self._precursor(precursor_attributes, text, ms_level, where)
            for precursor_attributes, text in scan.precursors
        ]
        if ms_level >= 2 and not precursors:
            self.precursor_counts["msn_without_precursor"] += 1
            if self.options.synthesize_all_ion_windows:
                synthesized = self._all_ion_precursor(scan_window, low_mz, high_mz, mz)
                if synthesized is not None:
                    precursors.append(synthesized)
                else:
                    self.precursor_counts["all_ion_window_not_synthesized"] += 1
        if len(precursors) > 1:
            self.precursor_counts["multiple_precursors"] += 1
        if precursors and collision_energy is not None:
            # mzXML records one collision energy per scan: the last activation, which is also the
            # precursor RawDataHandler keeps when a list holds more than one.
            precursors[-1].collision_energy = collision_energy
        spectrum_energy = None
        if self.options.spectrum_level_collision_energy and ms_level >= 2 and collision_energy is not None:
            spectrum_energy = collision_energy
            self.spectrum_level_energies += 1

        instrument_ref = self.instrument_ids.get((attributes.get("msInstrumentID") or "").strip())
        if instrument_ref == _DEFAULT_INSTRUMENT_ID:
            instrument_ref = None

        filter_line = attributes.get("filterLine")
        return _Spectrum(
            index=self.count,
            num=num,
            ms_level=ms_level,
            spectrum_term=spectrum_term,
            polarity=polarity,
            polarity_imputed=imputed,
            centroided=centroided,
            retention_time=retention_time,
            filter_line=filter_line if filter_line and filter_line.strip() else None,
            zoom=(scan_type or "").casefold() == "zoom",
            scan_window=scan_window,
            descriptive=descriptive,
            spectrum_collision_energy=spectrum_energy,
            precursors=precursors,
            instrument_ref=instrument_ref,
            mz=mz,
            intensity=intensity,
            intensity_bits=intensity_bits,
        )

    def _polarity(self, text: str | None, where: str) -> tuple[str | None, bool]:
        value = (text or "").strip()
        if value == "+":
            recorded = "positive"
        elif value == "-":
            recorded = "negative"
        elif value in {"", "any"}:
            recorded = None
        else:
            raise ConversionError(f"{where} polarity {value!r} is not +, - or any")
        self.recorded_polarity[recorded or ("any" if value == "any" else "absent")] += 1
        if recorded is None and self.options.impute_polarity:
            self.imputed += 1
            self.polarity[self.options.impute_polarity] += 1
            return self.options.impute_polarity, True
        self.polarity[recorded or "unrecorded"] += 1
        return recorded, False

    def _descriptive(self, attributes: dict[str, str], name: str, where: str) -> float | None:
        # Values RawDataHandler recomputes from the arrays: an unparsable one is dropped and
        # counted, not allowed to fail the file.
        try:
            return _float(attributes.get(name), f"{where} {name}")
        except ConversionError:
            self.dropped[name] += 1
            return None

    def _arrays(self, scan: _MzxmlScan, peaks_count: int, where: str) -> tuple[array, array, int, int]:
        peaks = scan.peaks
        if not peaks:
            if peaks_count:
                raise ConversionError(f"{where} has peaksCount {peaks_count} but no peaks element")
            return array("d"), array("f"), 32, 0
        if len(peaks) == 1 and peaks[0].content == "m/z-int":
            values = peaks[0].values
            if len(values) % 2:
                raise ConversionError(f"{where} has an odd number of interleaved m/z-int values")
            mz_source, intensity = values[0::2], values[1::2]
            mz_bits = intensity_bits = peaks[0].precision
        else:
            by_content: dict[str, _Peaks] = {}
            for item in peaks:
                if item.content not in {"m/z", "intensity"} or item.content in by_content:
                    raise ConversionError(
                        f"{where} peaks must be one m/z-int array, or one m/z and one intensity array"
                    )
                by_content[item.content] = item
            if set(by_content) != {"m/z", "intensity"}:
                raise ConversionError(f"{where} has an m/z or an intensity array without the other")
            mz_source = by_content["m/z"].values
            intensity = by_content["intensity"].values
            mz_bits, intensity_bits = by_content["m/z"].precision, by_content["intensity"].precision
            if len(mz_source) != len(intensity):
                raise ConversionError(f"{where} m/z and intensity arrays differ in length")
        if len(mz_source) != peaks_count:
            if peaks_count == 0 and len(mz_source) == 1 and mz_source[0] == 0 and intensity[0] == 0:
                # Some writers encode an empty scan as a single (0, 0) pair.
                self.placeholders += 1
                return array("d"), array(intensity.typecode), intensity_bits, mz_bits
            raise ConversionError(
                f"{where} peaksCount is {peaks_count} but its peaks hold {len(mz_source)} pairs"
            )
        # m/z is always written as 64-bit; widening a 32-bit float is exact.
        mz = mz_source if mz_source.typecode == "d" else array("d", mz_source)
        return mz, intensity, intensity_bits, mz_bits

    def _precursor(self, attributes: dict[str, str], text: str, ms_level: int, where: str) -> _Precursor:
        selected = _float(text, f"{where} precursorMz")
        if selected is None or selected <= 0:
            raise ConversionError(f"{where} has a precursorMz of {text.strip()!r}")
        self.precursor_counts["with_mz"] += 1
        width = _float(attributes.get("windowWideness"), f"{where} windowWideness")
        if width is not None and width < 0:
            raise ConversionError(f"{where} has a negative windowWideness")
        lower = upper = None
        if width is not None:
            lower = upper = width / 2
            basis = "recorded"
            self.precursor_counts["with_window"] += 1
        elif self.windows and ms_level >= 2 and selected in self.window_targets:
            lower = upper = self.windows["half_width"]
            basis = "inferred_ladder_spacing"
            self.inferred_windows += 1
            self.precursor_counts["inferred_window"] += 1
        else:
            basis = "unrecorded"
            self.precursor_counts["without_window"] += 1
        charge = _int(attributes.get("precursorCharge"), f"{where} precursorCharge")
        if charge is not None and charge < 0:
            raise ConversionError(f"{where} has a negative precursorCharge")
        if charge == 0:
            # 0 is how writers say "unknown"; it is not a charge state.
            charge = None
            self.precursor_counts["charge_zero_dropped"] += 1
        possible = []
        for item in (attributes.get("possibleCharges") or "").split(","):
            value = _int(item, f"{where} possibleCharges")
            if value is not None and value > 0:
                possible.append(value)
        intensity = self._descriptive(attributes, "precursorIntensity", where)
        try:
            scan_ref = _int(attributes.get("precursorScanNum"), f"{where} precursorScanNum")
        except ConversionError:
            scan_ref = None
            self.dropped["precursorScanNum"] += 1
        method = (attributes.get("activationMethod") or "").strip() or None
        term = ACTIVATION_TERMS.get(method.upper()) if method else None
        self.activation[method or "unrecorded"] += 1
        return _Precursor(
            selected_mz=selected,
            target_mz=selected,
            lower_offset=lower,
            upper_offset=upper,
            window_basis=basis,
            charge=charge,
            possible_charges=possible,
            intensity=intensity,
            spectrum_ref=f"scan={scan_ref}" if scan_ref is not None and scan_ref >= 0 else None,
            activation_term=term,
            activation_text=None if term else (method or "not recorded in mzXML"),
            collision_energy=None,
        )

    def _all_ion_precursor(
        self,
        scan_window: tuple[float, float] | None,
        low_mz: float | None,
        high_mz: float | None,
        mz: array,
    ) -> _Precursor | None:
        # RawDataHandler's Agilent reader gives all-ion scans a precursor centred on the scan's
        # range, with offsets reaching both ends; this follows it, preferring the acquisition range.
        if scan_window is not None and scan_window[1] > scan_window[0]:
            low, high, basis = scan_window[0], scan_window[1], "scan_window"
        elif low_mz is not None and high_mz is not None and high_mz > low_mz:
            low, high, basis = low_mz, high_mz, "observed_range"
        elif len(mz) >= 2 and max(mz) > min(mz):
            low, high, basis = min(mz), max(mz), "peaks"
        else:
            return None
        centre = (low + high) * 0.5
        self.synthesis_basis[basis] += 1
        self.precursor_counts["synthesized"] += 1
        return _Precursor(
            selected_mz=centre,
            target_mz=centre,
            lower_offset=centre - low,
            upper_offset=high - centre,
            window_basis=f"synthesized_{basis}",
            charge=None,
            possible_charges=[],
            intensity=None,
            spectrum_ref=None,
            activation_term=None,
            activation_text="not recorded in mzXML",
            collision_energy=None,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "counts": {
                "spectra": self.count,
                "ms_levels": {str(level): count for level, count in sorted(self.ms_levels.items())},
                "polarity": dict(sorted(self.polarity.items())),
                "polarity_recorded": dict(sorted(self.recorded_polarity.items())),
                "representation_by_ms_level": {
                    str(level): dict(sorted(counter.items()))
                    for level, counter in sorted(self.representation.items())
                },
                "scan_types": dict(sorted(self.scan_types.items())),
                "spectrum_types": {
                    name: count for (accession, name), count in sorted(self.spectrum_terms.items())
                },
                "empty_spectra": self.empty,
                "empty_scan_placeholders": self.placeholders,
                "mz_source_precision_bits": {str(bits): n for bits, n in sorted(self.mz_source_bits.items())},
                "intensity_precision_bits": {str(bits): n for bits, n in sorted(self.intensity_bits.items())},
                "dropped_unparsable_values": dict(sorted(self.dropped.items())),
                "retention_time_seconds": {
                    "first": self.retention_time_first,
                    "last": self.retention_time_last,
                    "min": self.retention_time_range[0] if self.retention_time_range else None,
                    "max": self.retention_time_range[1] if self.retention_time_range else None,
                },
            },
            "precursors": {
                **{
                    key: self.precursor_counts[key]
                    for key in (
                        "with_mz", "with_window", "without_window", "inferred_window", "synthesized",
                        "msn_without_precursor", "all_ion_window_not_synthesized",
                        "multiple_precursors", "charge_zero_dropped",
                    )
                },
                "activation_methods": dict(sorted(self.activation.items())),
            },
            "collision_energies": {"ms1": _listed(self.ms1_energies), "msn": _listed(self.msn_energies)},
        }


def _infer_dia_windows(source: Path) -> dict[str, Any]:
    """Read the MS2 precursor ladder of a file whose windows are unrecorded, without its peaks.

    A window is inferred only for a uniform ladder that repeats (every target seen at least twice),
    which is what a fixed-window SWATH cycle looks like and what DDA precursors never are. The window
    is then the ladder spacing, split evenly. Overlapping or variable windows are not reconstructed.
    """
    reader = _MzxmlReader(source, decode_peaks=False)
    targets: Counter = Counter()
    recorded = 0
    for scan in reader.scans():
        try:
            level = _int(scan.attributes.get("msLevel"), "msLevel") or 0
        except ConversionError:
            continue
        if level < 2:
            continue
        for attributes, text in scan.precursors:
            if (attributes.get("windowWideness") or "").strip():
                recorded += 1
                continue
            try:
                value = _float(text, "precursorMz")
            except ConversionError:
                continue
            if value is not None and value > 0:
                targets[value] += 1
    result: dict[str, Any] = {
        "method": "uniform_repeated_ladder_spacing",
        "tolerance_mz": LADDER_TOLERANCE_MZ,
        "applied": False,
        "distinct_targets": len(targets),
        "precursors_with_recorded_width": recorded,
    }
    values = sorted(targets)
    if len(values) < 2:
        result["reason"] = "fewer than two precursor targets lack a recorded width"
    elif min(targets.values()) < 2:
        result["reason"] = "a precursor target occurs only once, so the precursors are not a repeated ladder"
    else:
        spacing = (values[-1] - values[0]) / (len(values) - 1)
        if any(abs((b - a) - spacing) > LADDER_TOLERANCE_MZ for a, b in zip(values, values[1:])):
            result["reason"] = "the precursor ladder is not uniformly spaced"
        else:
            result.update(
                applied=True,
                spacing=spacing,
                half_width=spacing / 2,
                target_values=values,
            )
    return result


# --------------------------------------------------------------------------------------------------
# Writing mzML


_FOOTER ="    </spectrumList>\n  </run>\n</mzML>\n"
_WINDOW_NOTES = {
    "inferred_ladder_spacing": "isolation window inferred from a uniform precursor ladder",
    "synthesized_scan_window": "all-ion isolation window synthesized from the scan window",
    "synthesized_observed_range": "all-ion isolation window synthesized from the observed m/z range",
    "synthesized_peaks": "all-ion isolation window synthesized from the peak m/z range",
}


def _indent(width: int) -> str:
    return " " * width


def _write_mzml(
    source: Path,
    partial: Path,
    body_path: Path,
    options: ConversionOptions,
    windows: dict[str, Any] | None,
    *,
    source_name: str,
    source_location: str,
    source_sha1: str,
) -> dict[str, Any]:
    """Write the spectra to a body file, then the header whose counts they settle, then join them."""
    reader = _MzxmlReader(source)
    builder = _SpectrumBuilder(reader, options, windows)
    with open(body_path, "w", encoding="utf-8", newline="\n") as body:
        for spectrum in builder.spectra():
            body.write(_spectrum_xml(spectrum))
    inferences, deviations, warnings = _applied(builder, options, windows, reader)
    header = _header_xml(
        reader,
        builder,
        inferences + deviations,
        run_id=_xml_id(Path(source_name).stem, set()),
        source_name=source_name,
        source_location=source_location,
        source_sha1=source_sha1,
    )
    with open(partial, "wb") as output:
        output.write(header.encode("utf-8"))
        with open(body_path, "rb") as body:
            shutil.copyfileobj(body, output, _CHUNK)
        output.write(_FOOTER.encode("ascii"))

    summary = builder.summary()
    summary["mzxml"] = {
        "version": reader.version,
        "namespace": reader.namespace,
        "declared_scan_count": reader.declared_scan_count,
        "centroided_default": reader.centroided_default,
        "parent_files": reader.parent_files,
        "instruments": reader.instruments,
        "software": reader.software,
    }
    summary["inferences"] = inferences
    summary["deviations"] = deviations
    summary["warnings"] = warnings
    return summary


def _applied(
    builder: _SpectrumBuilder,
    options: ConversionOptions,
    windows: dict[str, Any] | None,
    reader: _MzxmlReader,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """What was supplied rather than copied, what departs from the PSI rules, and what to look at."""
    inferences: list[dict[str, Any]] = []
    deviations: list[dict[str, Any]] = []
    warnings: list[str] = []
    if builder.imputed:
        inferences.append(
            {
                "kind": "polarity_imputation",
                "value": options.impute_polarity,
                "basis": "the analysis unit's declared ion mode, supplied by the caller",
                "source": "repository_declared",
                "spectra": builder.imputed,
                "summary": f"{options.impute_polarity} polarity imputed from the declared ion mode"
                f" for {builder.imputed} spectra",
            }
        )
    if windows is not None:
        if windows.get("applied") and builder.inferred_windows:
            inferences.append(
                {
                    "kind": "dia_window_inference",
                    "method": windows["method"],
                    "spacing": windows["spacing"],
                    "half_width": windows["half_width"],
                    "tolerance_mz": windows["tolerance_mz"],
                    "targets": _listed(set(windows["target_values"])),
                    "precursors": builder.inferred_windows,
                    "summary": f"isolation window of {_num(windows['spacing'])} m/z inferred from a"
                    f" uniform precursor ladder for {builder.inferred_windows} precursors",
                }
            )
        elif not windows.get("applied"):
            warnings.append(f"DIA window inference was requested but not applied: {windows.get('reason')}")
    synthesized = builder.precursor_counts["synthesized"]
    if synthesized:
        inferences.append(
            {
                "kind": "all_ion_window_synthesis",
                "basis": dict(sorted(builder.synthesis_basis.items())),
                "precedent": "RawDataHandler AgilentMhdacDataReader all-ion precursor",
                "precursors": synthesized,
                "summary": f"all-ion isolation window synthesized from the scan range for {synthesized}"
                " precursors",
            }
        )
    if builder.spectrum_level_energies:
        deviations.append(
            {
                "kind": "spectrum_level_collision_energy",
                "spectra": builder.spectrum_level_energies,
                "summary": "collision energy repeated at spectrum level for"
                f" {builder.spectrum_level_energies} spectra, where RawDataHandler reads AIF"
                " collision-energy targets; the PSI-MS mapping rules place it under activation only",
            }
        )

    declared = reader.declared_scan_count
    if declared is not None and declared != builder.count:
        warnings.append(f"msRun scanCount is {declared} but the file holds {builder.count} scans")
    if builder.polarity["unrecorded"]:
        warnings.append(
            f"{builder.polarity['unrecorded']} spectra record no polarity; MS-DIAL skips a spectrum"
            " whose polarity is not the method's ion mode"
        )
    without = builder.precursor_counts["msn_without_precursor"] - synthesized
    if without > 0:
        warnings.append(f"{without} MSn spectra have no precursor, so MS-DIAL assigns them to no MS1 feature")
    if len(builder.ms1_energies) > 1:
        warnings.append(
            f"MS1 scans record {len(builder.ms1_energies)} distinct collision energies; all-ion"
            " fragmentation may be written as MS1"
        )
    for term in (TERM_SIM, TERM_SRM, TERM_CRM):
        if builder.spectrum_terms[term]:
            warnings.append(f"{builder.spectrum_terms[term]} spectra are {term[1]}s, outside untargeted LC-MS/MS")
    zoom = sum(count for name, count in builder.scan_types.items() if name.casefold() == "zoom")
    if zoom:
        warnings.append(f"{zoom} zoom scans are written as ordinary spectra of their MS level")
    if builder.dropped:
        dropped = ", ".join(f"{name} ({count})" for name, count in sorted(builder.dropped.items()))
        warnings.append(f"unparsable descriptive values were dropped: {dropped}")
    return inferences, deviations, warnings


def _instrument_ids(instruments: list[dict[str, Any]]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for number, instrument in enumerate(instruments, start=1):
        ids.setdefault(str(instrument.get("id") or ""), f"IC{number}")
    return ids


def _split_parent_name(recorded: str) -> tuple[str, str]:
    cut = max(recorded.rfind("/"), recorded.rfind("\\"))
    if cut < 0:
        return recorded or "not recorded", "."
    return recorded[cut + 1 :] or "not recorded", recorded[:cut] or "."


def _header_xml(
    reader: _MzxmlReader,
    builder: _SpectrumBuilder,
    notes: list[dict[str, Any]],
    *,
    run_id: str,
    source_name: str,
    source_location: str,
    source_sha1: str,
) -> str:
    i6, i8 = _indent(6), _indent(8)
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>\n',
        '<mzML xmlns="http://psi.hupo.org/ms/mzml" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:schemaLocation="http://psi.hupo.org/ms/mzml http://psidev.info/files/ms/mzML/xsd/mzML1.1.0.xsd"'
        f' id="{_attr(run_id)}" version="1.1.0">\n',
        '  <cvList count="2">\n',
        _MS_CV,
        _UO_CV,
        "  </cvList>\n",
        "  <fileDescription>\n",
        "    <fileContent>\n",
    ]
    lines += [_cv(i6, term) for term in _FILE_CONTENT_ORDER if builder.spectrum_terms[term]]
    lines.append("    </fileContent>\n")
    parents = reader.parent_files
    lines.append(f'    <sourceFileList count="{1 + len(parents)}">\n')
    lines.append(f'      <sourceFile id="SF1" name="{_attr(source_name)}" location="{_attr(source_location)}">\n')
    lines.append(_cv(i8, ("MS:1000776", "scan number only nativeID format")))
    lines.append(_cv(i8, ("MS:1000566", "ISB mzXML format")))
    lines.append(_cv(i8, ("MS:1000569", "SHA-1"), source_sha1))
    lines.append("      </sourceFile>\n")
    for number, parent in enumerate(parents, start=2):
        recorded = (parent.get("fileName") or "").strip()
        name, location = _split_parent_name(recorded)
        lines.append(f'      <sourceFile id="SF{number}" name="{_attr(name)}" location="{_attr(location)}">\n')
        parent_sha1 = (parent.get("fileSha1") or "").strip().casefold()
        if _HEX40.fullmatch(parent_sha1):
            # The mzXML's claim about its parent, which is not here to be checked.
            lines.append(_cv(i8, ("MS:1000569", "SHA-1"), parent_sha1))
        lines.append(_user(i8, "mzXML parentFile fileName", recorded or "not recorded"))
        lines.append(_user(i8, "mzXML parentFile fileType", (parent.get("fileType") or "").strip() or "not recorded"))
        lines.append("      </sourceFile>\n")
    lines.append("    </sourceFileList>\n")
    lines.append("  </fileDescription>\n")

    software_lines, acquisition_refs = _software_xml(reader)
    lines += software_lines
    lines += _instrument_xml(reader, acquisition_refs)

    lines += [_OUTPUT_MARK, _cv(i8, ("MS:1000544", "Conversion to mzML"))]
    lines += [_user(i8, f"MS-DIAL Interactive {note['kind']}", note["summary"]) for note in notes]
    lines += ["      </processingMethod>\n", "    </dataProcessing>\n", "  </dataProcessingList>\n"]
    # No startTimeStamp: mzXML records none, and a file time would make the bytes irreproducible.
    lines.append(
        f'  <run id="{_attr(run_id)}" defaultInstrumentConfigurationRef="{_DEFAULT_INSTRUMENT_ID}"'
        ' defaultSourceFileRef="SF1">\n'
    )
    lines.append(f'    <spectrumList count="{builder.count}" defaultDataProcessingRef="{_DATA_PROCESSING_ID}">\n')
    return "".join(lines)


def _software_xml(reader: _MzxmlReader) -> tuple[list[str], dict[int, str]]:
    """The mzXML's own software in document order, then this converter.

    The extractor takes the first software with a version as the instrument's, so the acquisition
    software recorded in msInstrument comes first and this converter comes last.
    """
    i6 = _indent(6)
    entries: list[tuple[int | None, dict[str, str]]] = []
    for number, instrument in enumerate(reader.instruments):
        entries += [(number, entry) for entry in instrument["software"]]
    entries += [(None, entry) for entry in reader.software]
    used = {_OUR_SOFTWARE_ID}
    acquisition_refs: dict[int, str] = {}
    lines = [f'  <softwareList count="{len(entries) + 1}">\n']
    for instrument_number, entry in entries:
        name = entry["name"] or "unnamed mzXML software"
        identifier = _xml_id(name, used)
        if instrument_number is not None:
            acquisition_refs.setdefault(instrument_number, identifier)
        lines.append(f'    <software id="{identifier}" version="{_attr(entry["version"])}">\n')
        squashed = _squash(name)
        if "proteowizard" in squashed:
            lines.append(_cv(i6, ("MS:1000615", "ProteoWizard software")))
        elif "xcalibur" in squashed:
            lines.append(_cv(i6, ("MS:1000532", "Xcalibur")))
        else:
            lines.append(_user(i6, "software name", name))
        if entry["type"]:
            lines.append(_user(i6, "mzXML software type", entry["type"]))
        lines.append("    </software>\n")
    lines.append(f'    <software id="{_OUR_SOFTWARE_ID}" version="{_attr(__version__)}">\n')
    lines.append(_cv(i6, ("MS:1000799", "custom unreleased software tool"), CONVERTER_NAME))
    lines.append("    </software>\n")
    lines.append("  </softwareList>\n")
    return lines, acquisition_refs


def _instrument_xml(reader: _MzxmlReader, acquisition_refs: dict[int, str]) -> list[str]:
    """One configuration per mzXML instrument.

    The model is a userParam, never MS:1000031 with the model as its value, because the extractor
    takes the name of a configuration-level cvParam as the model.
    """
    i6, i10 = _indent(6), _indent(10)
    instruments = reader.instruments
    lines = [f'  <instrumentConfigurationList count="{max(1, len(instruments))}">\n']
    if not instruments:
        lines += [
            f'    <instrumentConfiguration id="{_DEFAULT_INSTRUMENT_ID}">\n',
            _user(i6, "instrument model", "not recorded in mzXML"),
            "    </instrumentConfiguration>\n",
        ]
    for number, instrument in enumerate(instruments):
        lines.append(f'    <instrumentConfiguration id="IC{number + 1}">\n')
        lines.append(_user(i6, "instrument model", instrument["model"] or "not recorded in mzXML"))
        if instrument["manufacturer"]:
            lines.append(_user(i6, "instrument manufacturer", instrument["manufacturer"]))
        if instrument["resolution"]:
            lines.append(_user(i6, "mzXML msResolution", instrument["resolution"]))
        if instrument["ionisation"] or instrument["analyzers"] or instrument["detector"]:
            analyzer_terms = [tuple(term) for term in instrument["analyzer_terms"]] or [None]
            lines.append(f'      <componentList count="{len(analyzer_terms) + 2}">\n')
            lines.append('        <source order="1">\n')
            if instrument["source_term"]:
                lines.append(_cv(i10, tuple(instrument["source_term"])))
            lines.append(_user(i10, "mzXML msIonisation", instrument["ionisation"] or "not recorded"))
            lines.append("        </source>\n")
            for order, term in enumerate(analyzer_terms, start=2):
                lines.append(f'        <analyzer order="{order}">\n')
                if term is not None:
                    lines.append(_cv(i10, term))
                if order == 2:
                    recorded = "; ".join(instrument["analyzers"]) or "not recorded"
                    lines.append(_user(i10, "mzXML msMassAnalyzer", recorded))
                lines.append("        </analyzer>\n")
            lines.append(f'        <detector order="{len(analyzer_terms) + 2}">\n')
            lines.append(_user(i10, "mzXML msDetector", instrument["detector"] or "not recorded"))
            lines.append("        </detector>\n")
            lines.append("      </componentList>\n")
        if number in acquisition_refs:
            lines.append(f'      <softwareRef ref="{acquisition_refs[number]}"/>\n')
        lines.append("    </instrumentConfiguration>\n")
    lines.append("  </instrumentConfigurationList>\n")
    return lines


def _spectrum_xml(spectrum: _Spectrum) -> str:
    i8, i10, i12, i14, i16 = (_indent(width) for width in (8, 10, 12, 14, 16))
    # index, id, defaultArrayLength in msconvert's order: RawDataHandler reads them in turn.
    out = [
        f'      <spectrum index="{spectrum.index}" id="scan={spectrum.num}"'
        f' defaultArrayLength="{len(spectrum.mz)}">\n',
        _cv(i8, ("MS:1000511", "ms level"), str(spectrum.ms_level)),
        _cv(i8, spectrum.spectrum_term),
    ]
    if spectrum.polarity == "positive":
        out.append(_cv(i8, ("MS:1000130", "positive scan")))
    elif spectrum.polarity == "negative":
        out.append(_cv(i8, ("MS:1000129", "negative scan")))
    if spectrum.centroided is True:
        out.append(_cv(i8, ("MS:1000127", "centroid spectrum")))
    elif spectrum.centroided is False:
        out.append(_cv(i8, ("MS:1000128", "profile spectrum")))
    out += [_cv(i8, term, _num(value), unit) for term, value, unit in spectrum.descriptive]
    if spectrum.spectrum_collision_energy is not None:
        energy = _num(spectrum.spectrum_collision_energy)
        out.append(_cv(i8, ("MS:1000045", "collision energy"), energy, UNIT_ELECTRONVOLT))
    if spectrum.polarity_imputed:
        out.append(_user(i8, "MS-DIAL Interactive inference", "polarity imputed from the declared ion mode"))

    reference = f' instrumentConfigurationRef="{spectrum.instrument_ref}"' if spectrum.instrument_ref else ""
    out += [
        f'{i8}<scanList count="1">\n',
        _cv(i10, ("MS:1000795", "no combination")),
        f"{i10}<scan{reference}>\n",
        _cv(i12, ("MS:1000016", "scan start time"), _num(spectrum.retention_time), UNIT_SECOND),
    ]
    if spectrum.filter_line:
        out.append(_cv(i12, ("MS:1000512", "filter string"), spectrum.filter_line))
    if spectrum.zoom:
        out.append(_cv(i12, ("MS:1000497", "zoom scan")))
    if spectrum.scan_window is not None:
        low, high = spectrum.scan_window
        out += [
            f'{i12}<scanWindowList count="1">\n',
            f"{i14}<scanWindow>\n",
            _cv(i16, ("MS:1000501", "scan window lower limit"), _num(low), UNIT_MZ),
            _cv(i16, ("MS:1000500", "scan window upper limit"), _num(high), UNIT_MZ),
            f"{i14}</scanWindow>\n",
            f"{i12}</scanWindowList>\n",
        ]
    out += [f"{i10}</scan>\n", f"{i8}</scanList>\n"]

    if spectrum.precursors:
        out.append(f'{i8}<precursorList count="{len(spectrum.precursors)}">\n')
        for precursor in spectrum.precursors:
            out += _precursor_xml(precursor)
        out.append(f"{i8}</precursorList>\n")

    out += [
        f'{i8}<binaryDataArrayList count="2">\n',
        _binary_xml(spectrum.mz, ("MS:1000514", "m/z array"), UNIT_MZ),
        _binary_xml(spectrum.intensity, ("MS:1000515", "intensity array"), UNIT_COUNTS),
        f"{i8}</binaryDataArrayList>\n",
        "      </spectrum>\n",
    ]
    return "".join(out)


def _precursor_xml(precursor: _Precursor) -> list[str]:
    i10, i12, i14, i16 = (_indent(width) for width in (10, 12, 14, 16))
    reference = f' spectrumRef="{_attr(precursor.spectrum_ref)}"' if precursor.spectrum_ref else ""
    out = [
        f"{i10}<precursor{reference}>\n",
        f"{i12}<isolationWindow>\n",
        # RawDataHandler reads the target and the selected ion into different fields, and MS-DIAL's
        # DIA deconvolution reads the target, so both carry the mzXML precursorMz.
        _cv(i14, ("MS:1000827", "isolation window target m/z"), _num(precursor.target_mz), UNIT_MZ),
    ]
    if precursor.lower_offset is not None and precursor.upper_offset is not None:
        lower, upper = _num(precursor.lower_offset), _num(precursor.upper_offset)
        out.append(_cv(i14, ("MS:1000828", "isolation window lower offset"), lower, UNIT_MZ))
        out.append(_cv(i14, ("MS:1000829", "isolation window upper offset"), upper, UNIT_MZ))
    if precursor.window_basis in _WINDOW_NOTES:
        out.append(_user(i14, "MS-DIAL Interactive inference", _WINDOW_NOTES[precursor.window_basis]))
    out += [
        f"{i12}</isolationWindow>\n",
        f'{i12}<selectedIonList count="1">\n',
        f"{i14}<selectedIon>\n",
        _cv(i16, ("MS:1000744", "selected ion m/z"), _num(precursor.selected_mz), UNIT_MZ),
    ]
    if precursor.charge is not None:
        out.append(_cv(i16, ("MS:1000041", "charge state"), str(precursor.charge)))
    out += [_cv(i16, ("MS:1000633", "possible charge state"), str(charge)) for charge in precursor.possible_charges]
    if precursor.intensity is not None:
        out.append(_cv(i16, ("MS:1000042", "peak intensity"), _num(precursor.intensity), UNIT_COUNTS))
    out += [f"{i14}</selectedIon>\n", f"{i12}</selectedIonList>\n", f"{i12}<activation>\n"]
    if precursor.activation_term is not None:
        out.append(_cv(i14, precursor.activation_term))
    if precursor.collision_energy is not None:
        energy = _num(precursor.collision_energy)
        out.append(_cv(i14, ("MS:1000045", "collision energy"), energy, UNIT_ELECTRONVOLT))
    if precursor.activation_term is None:
        # Never an invented CID, and never an empty <activation/> that RawDataHandler reads past.
        out.append(_user(i14, "mzXML activationMethod", precursor.activation_text or "not recorded in mzXML"))
    out += [f"{i12}</activation>\n", f"{i10}</precursor>\n"]
    return out


def _binary_xml(values: array, term: tuple[str, str], unit: tuple[str, str, str]) -> str:
    i10, i12 = _indent(10), _indent(12)
    raw = _le_bytes(values)
    encoded = base64.b64encode(zlib.compress(raw, ZLIB_LEVEL)).decode("ascii") if raw else ""
    precision = ("MS:1000523", "64-bit float") if values.typecode == "d" else ("MS:1000521", "32-bit float")
    return (
        f'{i10}<binaryDataArray encodedLength="{len(encoded)}">\n'
        + _cv(i12, precision)
        + _cv(i12, ("MS:1000574", "zlib compression"))
        + _cv(i12, term, "", unit)
        + f"{i12}<binary>{encoded}</binary>\n"
        + f"{i10}</binaryDataArray>\n"
    )


# --------------------------------------------------------------------------------------------------
# Validation: an independent mzML reader on expat, and the per-spectrum comparison


class _Frame:
    __slots__ = ("name", "children")

    def __init__(self, name: str) -> None:
        self.name = name
        self.children = 0


class _IndependentMzmlReader:
    """Reads mzML with expat directly, sharing no parsing code with the writer.

    Besides the spectra it lints what the pinned RawDataHandler cannot read: self-closed or empty
    containers, referenceable parameter groups, binary cvParams it does not decode, numbers that are
    not invariant-culture decimals, whitespace inside <binary>, and a wrong encodedLength or
    spectrumList count. A self-closed element is told from an empty pair by the two bytes before
    expat's position at its end event, which for an empty-element tag is the end of that tag.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.problems: list[str] = []
        self.problem_count = 0
        self.root: str | None = None
        self.spectrum_list_count: int | None = None
        self.spectrum_count = 0
        self._stack: list[_Frame] = []
        self._ready: list[dict[str, Any]] = []
        self._spectrum: dict[str, Any] | None = None
        self._precursor: dict[str, Any] | None = None
        self._array: dict[str, Any] | None = None
        self._window = b""
        self._window_start = 0
        self._parser: Any = None

    def problem(self, message: str) -> None:
        self.problem_count += 1
        if len(self.problems) < _MAX_PROBLEMS:
            self.problems.append(message)

    def spectra(self) -> Iterator[dict[str, Any]]:
        parser = expat.ParserCreate(namespace_separator="}")
        parser.buffer_text = True
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = self._text
        self._parser = parser
        previous, offset = b"", 0
        with open(self.path, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                self._window, self._window_start = previous + chunk, offset - len(previous)
                parser.Parse(chunk, not chunk)
                ready, self._ready = self._ready, []
                yield from ready
                if not chunk:
                    break
                previous, offset = chunk, offset + len(chunk)

    def _where(self) -> str:
        if self._spectrum is None:
            return ""
        return f" in spectrum {self._spectrum['attributes'].get('id')}"

    def _self_closed(self) -> bool:
        position = self._parser.CurrentByteIndex - self._window_start
        return position >= 2 and self._window[position - 2 : position] == b"/>"

    def _start(self, name: str, attributes: dict[str, str]) -> None:
        local = name.rsplit("}", 1)[-1]
        parent = self._stack[-1].name if self._stack else None
        if self._stack:
            self._stack[-1].children += 1
        self._stack.append(_Frame(local))
        if parent is None:
            self.root = local
            if local != "mzML":
                self.problem(f"the root element is <{local}>, not <mzML>")
        if local.startswith("referenceableParamGroup"):
            self.problem(f"<{local}> is present; RawDataHandler resolves only two polarity group names")
        if local == "cvParam":
            self._cv_param(parent, attributes)
        elif local == "spectrumList":
            count = attributes.get("count", "")
            self.spectrum_list_count = int(count) if _INTEGER_TEXT.fullmatch(count) else None
        elif local == "spectrum":
            self._spectrum = {
                "attributes": dict(attributes),
                "cv": {},
                "scan_cv": {},
                "window_cv": {},
                "scans": 0,
                "precursors": [],
                "arrays": [],
            }
        elif self._spectrum is not None:
            if local == "scan":
                self._spectrum["scans"] += 1
            elif local == "precursor":
                self._precursor = {
                    "attributes": dict(attributes),
                    "isolation": {},
                    "selected": {},
                    "activation": {},
                    "selected_ions": 0,
                }
                self._spectrum["precursors"].append(self._precursor)
            elif local == "selectedIon" and self._precursor is not None:
                self._precursor["selected_ions"] += 1
            elif local == "binaryDataArray":
                self._array = {"attributes": dict(attributes), "cv": {}, "text": [], "binary": ""}

    def _cv_param(self, parent: str | None, attributes: dict[str, str]) -> None:
        accession = attributes.get("accession", "")
        value = attributes.get("value", "")
        if accession in _FLOAT_ACCESSIONS:
            if not _DECIMAL_TEXT.fullmatch(value) or not math.isfinite(float(value)):
                self.problem(f"{accession} value {value!r} is not an invariant-culture number{self._where()}")
        elif accession in _INTEGER_ACCESSIONS and not _INTEGER_TEXT.fullmatch(value):
            self.problem(f"{accession} value {value!r} is not an integer{self._where()}")
        if self._spectrum is None:
            return
        target = None
        if parent == "spectrum":
            target = self._spectrum["cv"]
        elif parent == "scan":
            target = self._spectrum["scan_cv"]
        elif parent == "scanWindow":
            target = self._spectrum["window_cv"]
        elif parent in {"isolationWindow", "selectedIon", "activation"} and self._precursor is not None:
            key = {"isolationWindow": "isolation", "selectedIon": "selected", "activation": "activation"}[parent]
            target = self._precursor[key]
        elif parent == "binaryDataArray" and self._array is not None:
            target = self._array["cv"]
            if accession not in ALLOWED_BINARY_ACCESSIONS:
                self.problem(f"binaryDataArray cvParam {accession} is not one RawDataHandler decodes{self._where()}")
        if target is not None:
            target.setdefault(accession, []).append((value, attributes.get("unitAccession")))

    def _text(self, data: str) -> None:
        if self._array is not None and self._stack and self._stack[-1].name == "binary":
            self._array["text"].append(data)

    def _end(self, name: str) -> None:
        local = name.rsplit("}", 1)[-1]
        frame = self._stack.pop()
        if local in CONTAINER_ELEMENTS and frame.children == 0:
            kind = "self-closed" if self._self_closed() else "empty"
            self.problem(f"{kind} <{local}>{self._where()}")
        if self._spectrum is None:
            return
        if local == "binary" and self._array is not None:
            text = "".join(self._array["text"])
            if _WHITESPACE.search(text):
                self.problem(f"whitespace inside <binary>{self._where()}")
            self._array["binary"] = text
        elif local == "binaryDataArray" and self._array is not None:
            self._spectrum["arrays"].append(self._decode(self._array))
            self._array = None
        elif local == "precursor":
            self._precursor = None
        elif local == "spectrum":
            self.spectrum_count += 1
            self._ready.append(self._spectrum)
            self._spectrum = None

    def _decode(self, state: dict[str, Any]) -> dict[str, Any]:
        cv = state["cv"]
        result: dict[str, Any] = {"kind": None, "bits": None, "bytes": b""}
        kinds = [accession for accession in ("MS:1000514", "MS:1000515") if accession in cv]
        precisions = [accession for accession in ("MS:1000521", "MS:1000523") if accession in cv]
        compressions = [accession for accession in ("MS:1000574", "MS:1000576") if accession in cv]
        if len(kinds) != 1 or len(precisions) != 1 or len(compressions) != 1:
            self.problem(f"a binaryDataArray does not name one kind, precision and compression{self._where()}")
            return result
        text = state["binary"]
        declared = state["attributes"].get("encodedLength", "")
        if not _INTEGER_TEXT.fullmatch(declared) or int(declared) != len(text):
            self.problem(f"encodedLength {declared!r} is not the {len(text)} characters encoded{self._where()}")
        if not _BASE64_TEXT.fullmatch(text):
            self.problem(f"<binary> is not base64{self._where()}")
            return result
        try:
            raw = base64.b64decode(text, validate=True)
            if compressions[0] == "MS:1000574" and raw:
                raw = zlib.decompress(raw)
        except (binascii.Error, ValueError, zlib.error) as exc:
            self.problem(f"<binary> does not decode: {exc}{self._where()}")
            return result
        size = 8 if precisions[0] == "MS:1000523" else 4
        if len(raw) % size:
            self.problem(f"<binary> holds {len(raw)} bytes, not whole {size * 8}-bit values{self._where()}")
            return result
        result.update(kind="mz" if kinds[0] == "MS:1000514" else "intensity", bits=size * 8, bytes=raw)
        return result


def _validation_failure(message: str) -> dict[str, Any]:
    return {
        "schema": VALIDATION_SCHEMA,
        "status": "failed",
        "spectra_compared": 0,
        "problems": [message],
        "problem_count": 1,
    }


def _validate(
    source: Path,
    destination: Path,
    options: ConversionOptions,
    windows: dict[str, Any] | None,
) -> dict[str, Any]:
    reader = _IndependentMzmlReader(destination)
    digest = hashlib.sha256()
    compared = 0
    expected_spectra = _SpectrumBuilder(_MzxmlReader(source), options, windows).spectra()
    actual_spectra = reader.spectra()
    try:
        complete = True
        for expected in expected_spectra:
            actual = next(actual_spectra, None)
            if actual is None:
                reader.problem(f"the mzML ends before spectrum {expected.index} (scan {expected.num})")
                complete = False
                break
            _compare(expected, actual, reader.problem)
            for item in actual["arrays"]:
                digest.update(item["bytes"])
            compared += 1
        if complete:
            if next(actual_spectra, None) is not None:
                reader.problem("the mzML holds more spectra than the mzXML has scans")
            elif reader.spectrum_list_count != reader.spectrum_count:
                reader.problem(
                    f"spectrumList count is {reader.spectrum_list_count} but the file holds"
                    f" {reader.spectrum_count} spectra"
                )
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        reader.problem(f"{type(exc).__name__}: {exc}")
    finally:
        # Release both files now rather than at garbage collection; Windows will not delete an open one.
        expected_spectra.close()
        actual_spectra.close()
    return {
        "schema": VALIDATION_SCHEMA,
        "status": "passed" if reader.problem_count == 0 else "failed",
        "reader": "expat mzML reader independent of the writer",
        "spectra_compared": compared,
        "spectrum_list_count": reader.spectrum_list_count,
        "arrays_sha256": digest.hexdigest(),
        "problems": reader.problems,
        "problem_count": reader.problem_count,
    }


def _one(
    table: dict[str, list[tuple[str, str | None]]],
    accession: str,
    where: str,
    problem: Any,
) -> tuple[str, str | None] | None:
    values = table.get(accession) or []
    if len(values) > 1:
        problem(f"{where}: {accession} appears {len(values)} times")
    return values[0] if values else None


def _same(entry: tuple[str, str | None] | None, expected: float | None) -> bool:
    if entry is None or expected is None:
        return entry is None and expected is None
    try:
        return float(entry[0]) == expected
    except ValueError:
        return False


def _compare(expected: _Spectrum, actual: dict[str, Any], problem: Any) -> None:
    where = f"spectrum {expected.index} (scan {expected.num})"
    attributes = actual["attributes"]
    if attributes.get("index") != str(expected.index) or attributes.get("id") != f"scan={expected.num}":
        problem(f"{where}: written as index {attributes.get('index')!r}, id {attributes.get('id')!r}")
    if attributes.get("defaultArrayLength") != str(len(expected.mz)):
        problem(f"{where}: defaultArrayLength {attributes.get('defaultArrayLength')!r}, expected {len(expected.mz)}")
    cv = actual["cv"]

    level = _one(cv, "MS:1000511", where, problem)
    if level is None or level[0] != str(expected.ms_level):
        problem(f"{where}: ms level {level[0] if level else None!r}, expected {expected.ms_level}")
    written_types = [term for term in _FILE_CONTENT_ORDER if term[0] in cv]
    if written_types != [expected.spectrum_term]:
        problem(f"{where}: spectrum types {[term[1] for term in written_types]}, expected {expected.spectrum_term[1]}")
    polarity = [accession for accession in ("MS:1000130", "MS:1000129") if accession in cv]
    wanted = {"positive": ["MS:1000130"], "negative": ["MS:1000129"], None: []}[expected.polarity]
    if polarity != wanted:
        problem(f"{where}: polarity terms {polarity}, expected {wanted}")
    representation = [accession for accession in ("MS:1000127", "MS:1000128") if accession in cv]
    wanted = {True: ["MS:1000127"], False: ["MS:1000128"], None: []}[expected.centroided]
    if representation != wanted:
        problem(f"{where}: representation terms {representation}, expected {wanted}")
    if not _same(_one(cv, "MS:1000045", where, problem), expected.spectrum_collision_energy):
        problem(f"{where}: the spectrum-level collision energy differs")

    if actual["scans"] != 1:
        problem(f"{where}: {actual['scans']} scan elements")
    start = _one(actual["scan_cv"], "MS:1000016", where, problem)
    if not _same(start, expected.retention_time) or (start is not None and start[1] != UNIT_SECOND[1]):
        problem(f"{where}: scan start time {start}, expected {expected.retention_time} s")
    low = _one(actual["window_cv"], "MS:1000501", where, problem)
    high = _one(actual["window_cv"], "MS:1000500", where, problem)
    window = expected.scan_window or (None, None)
    if not _same(low, window[0]) or not _same(high, window[1]):
        problem(f"{where}: scan window {low}, {high}, expected {expected.scan_window}")

    if len(actual["precursors"]) != len(expected.precursors):
        problem(f"{where}: {len(actual['precursors'])} precursors, expected {len(expected.precursors)}")
    for number, (want, got) in enumerate(zip(expected.precursors, actual["precursors"])):
        at = f"{where} precursor {number}"
        isolation, selected, activation = got["isolation"], got["selected"], got["activation"]
        if not _same(_one(isolation, "MS:1000827", at, problem), want.target_mz):
            problem(f"{at}: the isolation window target differs")
        if not _same(_one(isolation, "MS:1000828", at, problem), want.lower_offset):
            problem(f"{at}: the isolation window lower offset differs")
        if not _same(_one(isolation, "MS:1000829", at, problem), want.upper_offset):
            problem(f"{at}: the isolation window upper offset differs")
        if got["selected_ions"] != 1:
            problem(f"{at}: {got['selected_ions']} selected ions")
        if not _same(_one(selected, "MS:1000744", at, problem), want.selected_mz):
            problem(f"{at}: the selected ion m/z differs")
        charge = _one(selected, "MS:1000041", at, problem)
        if (charge[0] if charge else None) != (str(want.charge) if want.charge is not None else None):
            problem(f"{at}: charge state {charge}, expected {want.charge}")
        possible = [value for value, _ in selected.get("MS:1000633", [])]
        if possible != [str(value) for value in want.possible_charges]:
            problem(f"{at}: possible charge states {possible}, expected {want.possible_charges}")
        if not _same(_one(activation, "MS:1000045", at, problem), want.collision_energy):
            problem(f"{at}: the activation collision energy differs")
        methods = [accession for accession, _ in ACTIVATION_TERMS.values() if accession in activation]
        if methods != ([want.activation_term[0]] if want.activation_term else []):
            problem(f"{at}: activation terms {methods}, expected {want.activation_term}")
        if (got["attributes"].get("spectrumRef") or None) != want.spectrum_ref:
            problem(f"{at}: spectrumRef {got['attributes'].get('spectrumRef')!r}, expected {want.spectrum_ref!r}")

    arrays = {item["kind"]: item for item in actual["arrays"] if item["kind"]}
    if len(actual["arrays"]) != 2 or set(arrays) != {"mz", "intensity"}:
        problem(f"{where}: arrays {sorted(arrays)}, expected one m/z and one intensity array")
        return
    if arrays["mz"]["bits"] != 64 or arrays["mz"]["bytes"] != _le_bytes(expected.mz):
        problem(f"{where}: the m/z array differs from the mzXML")
    wanted_bits = 64 if expected.intensity.typecode == "d" else 32
    if arrays["intensity"]["bits"] != wanted_bits or arrays["intensity"]["bytes"] != _le_bytes(expected.intensity):
        problem(f"{where}: the intensity array differs from the mzXML")

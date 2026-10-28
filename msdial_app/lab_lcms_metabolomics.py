"""Laboratory LC-MS metabolomics: the annotation libraries, normalization by one internal standard, and
the merge of the polarities by InChIKey.

A hydrophilic metabolomics batch is annotated by an ordered list of libraries, each with its own
retention-time use and score thresholds (presets below). Each feature is then divided by one internal
standard's height in its injection and multiplied by that standard's mean height over the Sample and QC
injections. Last, the rows of one or both polarities become one metabolome, with one row per compound
(the InChIKey's first block). lab_lcms_metabolomics_agent walks a person through these steps; this
module reads, computes and writes, and decides nothing on its own.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Collection, Iterable

from .annotation_codes import is_in_silico_match
from .lab_lipidomics import (
    IS_TEXT_COLUMNS,
    NEGATIVE,
    POLARITIES,
    POSITIVE,
    LabInputError,
    _is_zero_height,
    annotation_method,
    file_sha256,
    parse_adduct,
    parse_number,
    read_alignment_export,
    read_candidate_table,
    representative_candidate,
)
from .lipidome_merge import MergeRefused, compare_sample_order, describe_export, net_double_tostring, strip_polarity
from .sample_grouping import is_blank, is_quality_control
from .workflow import _as_switch, file_identity, is_predicted_library_kind

# ---------------------------------------------------------------------------------------------
# Annotation: an ordered list of libraries
# ---------------------------------------------------------------------------------------------

LIBRARY_KINDS = ("msp", "lbm")
LIBRARY_SUFFIXES = {"msp": (".msp",), "lbm": (".lbm2",)}
RETENTION_TIME_USES = ("none", "use")
THRESHOLD_KEYS = (
    "ms2_tolerance", "weighted_dot_product_cutoff", "simple_dot_product_cutoff", "reverse_dot_product_cutoff",
    "matched_peaks_percentage_cutoff", "minimum_spectrum_match",
)
# standard is a guided run's own MSP row (agent_workflow._msp_row), at the method's MS/MS tolerance;
# strict and mild are the repository campaign's two MSP tiers (annotation_pipeline.tiered_lcms_annotation);
# in_silico is its in-silico MSP tier, GLACIER, as the author decided on 2026-10-25 (at least three matched
# peaks at 0.05 Da: against dense predicted spectra one matched peak at 0.25 Da is chance), which
# annotation_pipeline reads from here.
THRESHOLDS = {
    "standard": {
        "weighted_dot_product_cutoff": 0.6, "simple_dot_product_cutoff": 0.6, "reverse_dot_product_cutoff": 0.8,
        "matched_peaks_percentage_cutoff": 0.1, "minimum_spectrum_match": 3,
    },
    "strict": {
        "ms2_tolerance": 0.05, "weighted_dot_product_cutoff": 0.6, "simple_dot_product_cutoff": 0.6,
        "reverse_dot_product_cutoff": 0.8, "matched_peaks_percentage_cutoff": 0.0, "minimum_spectrum_match": 3,
    },
    "mild": {
        "ms2_tolerance": 0.25, "weighted_dot_product_cutoff": 0.5, "simple_dot_product_cutoff": 0.5,
        "reverse_dot_product_cutoff": 0.5, "matched_peaks_percentage_cutoff": 0.0, "minimum_spectrum_match": 1,
    },
    "in_silico": {
        "ms2_tolerance": 0.05, "weighted_dot_product_cutoff": 0.5, "simple_dot_product_cutoff": 0.5,
        "reverse_dot_product_cutoff": 0.5, "matched_peaks_percentage_cutoff": 0.0, "minimum_spectrum_match": 3,
    },
}
# Highest priority first. A slot names the file the person gives; one file may fill two entries (the
# campaign searches its MSP twice, strictly and mildly).
PRESETS: dict[str, list[dict[str, Any]]] = {
    "single_msp": [
        {"id": "msp", "slot": "msp", "kind": "msp", "thresholds": "standard", "retention_time": "none",
         "label": "MSP"},
    ],
    "campaign": [
        {"id": "lbm", "slot": "lbm", "kind": "lbm", "retention_time": "none", "label": "LBM2 lipids"},
        {"id": "msp_strict", "slot": "msp", "kind": "msp", "thresholds": "strict", "retention_time": "none",
         "label": "MSP strict"},
        {"id": "msp_mild", "slot": "msp", "kind": "msp", "thresholds": "mild", "retention_time": "none",
         "label": "MSP mild"},
        # Suggestions off, as the campaign's in-silico tier (the author's decision Q2): no "no MS2:" or "low
        # score:" name from a bare precursor mass against predicted spectra. Its label stays general, as the
        # slot takes any in-silico library: the GLACIER tier, citation and preprocessing record are the
        # tiered profile's (annotation_pipeline).
        {"id": "in_silico", "slot": "in_silico", "kind": "msp", "thresholds": "in_silico", "retention_time": "none",
         "label": "in-silico MSP", "library_kind": "predicted", "suggestions": False, "optional": True},
    ],
    "lab_rt_tiers": [
        {"id": "in_house", "slot": "in_house", "kind": "msp", "thresholds": "standard", "retention_time": "use",
         "rt_tolerance": 0.5, "label": "in-house RT and MS/MS"},
        {"id": "predicted_rt", "slot": "predicted_rt", "kind": "msp", "thresholds": "standard",
         "retention_time": "use", "rt_tolerance": 2.0, "label": "predicted RT"},
        {"id": "no_rt", "slot": "no_rt", "kind": "msp", "thresholds": "standard", "retention_time": "none",
         "label": "MS/MS without RT"},
    ],
}
PRESET_PURPOSES = {
    "single_msp": "one MSP library, no retention time: what most users and the reanalysis agent run",
    "campaign": "the reanalysis project's tiers: the LBM2 for the few lipids, one MSP searched strictly and "
                "mildly, and optionally an in-silico MSP at its own thresholds with suggestions off, all without "
                "retention time",
    "lab_rt_tiers": "the laboratory's own tiers: in-house standards (RT 0.5 min), predicted RTs (RT 2.0 min), "
                    "and MS/MS without RT",
}
# The internal-standard text row is searched at priority 1, below every library.
INTERNAL_STANDARD_PRIORITY = 1
# Whether an MSP's spectra were acquired or computed (in silico): the Console reads library_kind and
# publishes a match against a predicted library as in-silico evidence, never as a reference spectrum.
LIBRARY_KINDS_OF_SPECTRA = ("acquired", "predicted")
_LABEL = re.compile(r"^[ -~]{1,60}$")
ANNOTATION_KEYS = frozenset({"preset", "libraries"})
SLOT_KEYS = frozenset({"path", "library_kind"})
ENTRY_KEYS = frozenset({"kind", "path", "paths", "thresholds", "retention_time", "rt_tolerance", "label", "library_kind"})
_IDENTIFIER = re.compile(r"[^a-z0-9]+")
_POLARITY_KEYS = {"positive": POSITIVE, "pos": POSITIVE, "negative": NEGATIVE, "neg": NEGATIVE}


def library_identity(path: str | Path) -> dict[str, Any]:
    """A library by name, size and sha256; a large one through Interactive's cached digest."""
    target = Path(path).expanduser().resolve()
    size = target.stat().st_size
    sha256 = file_identity(target).get("sha256", "") if size >= (64 << 20) else file_sha256(target)
    return {"path": str(target), "name": target.name, "size_bytes": size, "sha256": sha256}


def console_lbm_annotator_id(path: str | Path) -> str:
    """What the Console calls the LBM2's annotator in an export's Comment (CommonProcess.LbmAnnotatorId)."""
    stem = Path(str(path)).stem if str(path or "").strip() else ""
    return f"LbmDB: {stem}" if stem.strip() else "LbmDB"


_RETENTION_TIME_CENSUS: dict[tuple[str, int, int], dict[str, int]] = {}
# As MS-DIAL's MSP reader (MspFileParcer.MspFileReader, ReadMspField, SetMspField) reads a record: it starts
# at a line beginning "NAME:" (any case), a field's name is everything before the first ':' as written, the
# last parseable retention time wins, and the record ends at its peak count.
_MSP_RETENTION_TIME_FIELDS = frozenset({"retentiontime", "retention_time", "rt"})
_MSP_PEAK_COUNT_FIELDS = frozenset({"num peaks", "numpeaks", "num_peaks"})


def retention_time_census(path: str | Path) -> dict[str, int]:
    """How many records an MSP holds, and how many MS-DIAL reads a retention time above zero for.

    MS-DIAL keeps a reference without a retention time in every RT window and counts its RT requirement
    as met, so such a record in a library searched with RT is matched on MS/MS alone. Read once per
    file version and kept for the process.
    """
    target = Path(path).expanduser().resolve()
    stat = target.stat()
    key = (str(target).casefold(), stat.st_size, stat.st_mtime_ns)
    if key in _RETENTION_TIME_CENSUS:
        return dict(_RETENTION_TIME_CENSUS[key])
    records = with_rt = 0
    in_record = False
    rt: float | None = None

    def close() -> None:
        nonlocal with_rt, in_record
        if in_record and rt is not None and rt > 0:
            with_rt += 1
        in_record = False

    # utf-8-sig: .NET's StreamReader drops a byte-order mark before the first NAME line.
    with target.open("r", encoding="utf-8-sig", errors="replace", newline=None) as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            if line.startswith("#"):
                continue
            if not in_record:
                if line[:5].casefold() == "name:":
                    records += 1
                    in_record, rt = True, None
                continue
            if line == "" or ":" not in line:
                continue
            name, _, value = line.partition(":")
            field = name.casefold()
            if field in _MSP_RETENTION_TIME_FIELDS:
                number = parse_number(value.strip())
                if number is not None:
                    rt = number
            elif field in _MSP_PEAK_COUNT_FIELDS:
                close()
    close()
    census = {"records": records, "with_retention_time": with_rt}
    _RETENTION_TIME_CENSUS[key] = census
    return dict(census)


_PRECURSOR_PLUS_1008_CENSUS: dict[tuple[str, int, int], dict[str, Any]] = {}
_MSP_PRECURSOR_FIELDS = frozenset({"precursormz", "precursor_mz"})
# The artefact peak of GLACIER's negative-mode records sits at the precursor m/z + 1.008 (+1.007 in a few, as
# written to three decimals): any peak within this much of +1.008 is taken for it. A predicted spectrum has no
# isotope peak (+1.003) for it to be confused with.
PRECURSOR_PLUS_1008 = 1.008
PRECURSOR_PLUS_1008_TOLERANCE = 0.0025
_PEAK_SEPARATOR = re.compile(r"[\s,:]+")
# What the census reads, by version: a result kept from another version is read again, never trusted.
PRECURSOR_PLUS_1008_CENSUS_VERSION = 2
PRECURSOR_PLUS_1008_CENSUS_CACHE = "msp-precursor-plus-1008-census.json"
_CENSUS_CACHE_ENTRIES = 256
_CENSUS_LOCK = threading.Lock()
_CENSUS_SCANS: dict[tuple[str, int, int], "_CensusScan"] = {}
_CENSUS_READ_BYTES = 16 << 20
# A peak-list block made of these bytes alone holds only plain decimal m/z values, whose integer part decides
# whether one can lie within the tolerance of the precursor m/z + 1.008 (_peaks_may_carry). Anything else - a
# sign, an exponent, an underscore, a letter, a non-ASCII byte - has the record read pair by pair.
_PLAIN_PEAK_BYTES = b"0123456789.\t \n;,:"
_INTEGER_PART_PATTERNS: dict[int, re.Pattern[bytes]] = {}
_CENSUS_FIELDS_PRECURSOR = frozenset(name.encode("ascii") for name in _MSP_PRECURSOR_FIELDS)
_CENSUS_FIELDS_PEAK_COUNT = frozenset(name.encode("ascii") for name in _MSP_PEAK_COUNT_FIELDS)


def _integer_part_pattern(integer: int) -> re.Pattern[bytes]:
    """A pair's first token, as _carries_peak reads it, whose integer part is ``integer``."""
    pattern = _INTEGER_PART_PATTERNS.get(integer)
    if pattern is None:
        pattern = re.compile(rb"(?:^|;)[ \t]*0*" + str(integer).encode("ascii") + rb"(?=[.\s,:;]|$)", re.M)
        _INTEGER_PART_PATTERNS[integer] = pattern
    return pattern


def _carries_peak(lines: list[bytes], precursor: float) -> bool:
    """Whether a record's peak lines hold a pair whose m/z lies within the tolerance of the precursor + 1.008.

    A line's pairs are separated by ';' and a pair's m/z is its first token, split off at whitespace, ',' or ':';
    a token that is no number is skipped. The reading the census has always made, line by line.
    """
    for raw in b"\n".join(lines).split(b"\n"):
        line = raw.decode("utf-8", "replace").strip()
        if not line or line.startswith("#"):
            continue
        for pair in line.split(";"):
            # float(), not parse_number, as before: the pair's first token as Python reads a number.
            try:
                mz = float(_PEAK_SEPARATOR.split(pair.strip(), 1)[0])
            except ValueError:
                continue
            if abs(mz - precursor - PRECURSOR_PLUS_1008) <= PRECURSOR_PLUS_1008_TOLERANCE:
                return True
    return False


class _CensusScan:
    """One file's census being read in a thread, so a plan can wait for it a bounded time."""

    def __init__(self, target: Path, size: int) -> None:
        self.target = target
        self.size = size
        self.started = time.time()
        self.progress: dict[str, int] = {"bytes_read": 0}
        self.done = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            # Looked up when it runs, so the census a test or a caller replaced is the one read.
            self.result = precursor_plus_1008_census(self.target, progress=self.progress)
        except BaseException as error:  # handed to the plan that collects it
            self.error = error
        finally:
            self.done.set()


def _census_cache_path() -> Path:
    from .user_settings import user_data_directory

    return user_data_directory() / PRECURSOR_PLUS_1008_CENSUS_CACHE


def _read_census_cache() -> dict[str, Any]:
    try:
        loaded = json.loads(_census_cache_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    entries = loaded.get("entries") if isinstance(loaded, dict) else None
    if not isinstance(entries, dict) or loaded.get("census_version") != PRECURSOR_PLUS_1008_CENSUS_VERSION:
        return {}
    return entries


def _cached_census(target: Path, stat: os.stat_result) -> dict[str, int] | None:
    """A census kept for this file version: by location, size and time, or by a digest already known for it.

    An entry that does not hold two whole counts is no census, and the file is read again.
    """
    entries = _read_census_cache()
    found = _kept_counts(entries.get(_census_key_text(target, stat)))
    if found is not None:
        return found
    # Moved or copied: the digest file_identity already holds for this version, never one taken here to look.
    from .workflow import _identity_cache_key, _read_identity_cache

    digest = _read_identity_cache().get(_identity_cache_key(target, stat), "")
    if not digest:
        return None
    for entry in entries.values():
        if isinstance(entry, dict) and entry.get("sha256") == digest and entry.get("size_bytes") == stat.st_size:
            kept = _kept_counts(entry)
            if kept is not None:
                return kept
    return None


def _kept_counts(entry: Any) -> dict[str, int] | None:
    if not isinstance(entry, dict):
        return None
    counts = (entry.get("records"), entry.get("with_precursor_plus_1008_peak"))
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in counts):
        return None
    if counts[1] > counts[0] + 1:  # a pseudo-record before the first NAME may carry it as well
        return None
    return {"records": counts[0], "with_precursor_plus_1008_peak": counts[1]}


def _census_key_text(target: Path, stat: os.stat_result) -> str:
    return f"{str(target).casefold()}|{stat.st_size}|{stat.st_mtime_ns}"


def _remember_census(target: Path, stat: os.stat_result, census: dict[str, Any]) -> None:
    path = _census_cache_path()
    with _CENSUS_LOCK:
        entries = _read_census_cache()
        key = _census_key_text(target, stat)
        entries.pop(key, None)
        entries[key] = census
        trimmed = dict(list(entries.items())[-_CENSUS_CACHE_ENTRIES:])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps({"census_version": PRECURSOR_PLUS_1008_CENSUS_VERSION, "entries": trimmed}, indent=1),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            pass


def precursor_plus_1008_census(path: str | Path, progress: dict[str, int] | None = None) -> dict[str, Any]:
    """How many records an MSP holds, and how many carry a peak at their precursor m/z + 1.008.

    What libraries.in_silico_msp_preprocessing "precursor_plus_1008_peak_removed" states of a file is checked
    against it, so a report never states a removal the file does not show. Records and fields are read as
    retention_time_census reads them; a record's peaks are the lines after its peak count, each "m/z
    intensity" pair separated by ';'.

    Read once per file version: kept for the process and, for a file of IDENTITY_CACHE_MIN_BYTES or more (one that
    cannot be rewritten at the same size within one clock tick, as file_identity reasons), in the user's data folder
    (census_version, the file's location, size and time, and its sha256, taken in the same read), so that a
    restarted backend does not read a 2 GB file again; a copy of the file is found there by the digest
    file_identity already holds for it. ``progress`` receives bytes_read as the file is read.
    """
    from .workflow import IDENTITY_CACHE_MIN_BYTES

    target = Path(path).expanduser().resolve()
    stat = target.stat()
    key = (str(target).casefold(), stat.st_size, stat.st_mtime_ns)
    if key in _PRECURSOR_PLUS_1008_CENSUS:
        return dict(_PRECURSOR_PLUS_1008_CENSUS[key])
    persist = stat.st_size >= IDENTITY_CACHE_MIN_BYTES
    kept = _cached_census(target, stat) if persist else None
    if kept is not None:
        census = {"records": kept["records"], "with_precursor_plus_1008_peak": kept["with_precursor_plus_1008_peak"]}
        _PRECURSOR_PLUS_1008_CENSUS[key] = census
        return dict(census)
    counted, digest = _read_precursor_plus_1008(target, progress)
    after = target.stat()
    census = {"records": counted[0], "with_precursor_plus_1008_peak": counted[1]}
    if (after.st_size, after.st_mtime_ns) == (stat.st_size, stat.st_mtime_ns):
        # Kept only for the version that was read: a file changed while it was read is read again next time.
        _PRECURSOR_PLUS_1008_CENSUS[key] = census
        if not persist:
            return dict(census)
        _remember_census(target, stat, {**census, "sha256": digest, "size_bytes": stat.st_size,
                                        "census_version": PRECURSOR_PLUS_1008_CENSUS_VERSION})
        from .workflow import remember_file_digest

        remember_file_digest(target, digest, stat)
    return dict(census)


def _peaks_may_carry(lines: list[bytes], precursor: float) -> bool:
    """False only where no pair of the record can carry the peak; True sends the record to _carries_peak.

    A library holds tens of millions of peaks, and reading each as a number took 80 s for a 2 GB file. A block of
    plain decimals (_PLAIN_PEAK_BYTES) has a pair within the tolerance only where a pair's first token has the
    integer part of the window's low or high end: such a token holds that integer's digits, and is then searched
    for as a token (_integer_part_pattern). Only a record that has one, or that is not plain, is read pair by pair,
    so a record is never passed over that _carries_peak would find carrying the peak.
    """
    low = precursor + PRECURSOR_PLUS_1008 - PRECURSOR_PLUS_1008_TOLERANCE
    high = precursor + PRECURSOR_PLUS_1008 + PRECURSOR_PLUS_1008_TOLERANCE
    # Below 1, and above a million (where a decimal's rounding to a float may cross an integer boundary), the record
    # is read pair by pair.
    if not (math.isfinite(low) and math.isfinite(high)) or low < 1 or high > 1e6:
        return True
    block = lines[0] if len(lines) == 1 else b"\n".join(lines)
    if block.translate(None, _PLAIN_PEAK_BYTES):
        return True
    # A margin for rounding at an integer boundary.
    for integer in range(math.floor(low - 1e-6), math.floor(high + 1e-6) + 1):
        digits = str(integer).encode("ascii")
        if digits in block and _integer_part_pattern(integer).search(block):
            return True
    return False


def _read_precursor_plus_1008(target: Path, progress: dict[str, int] | None) -> tuple[tuple[int, int], str]:
    """(records, records carrying the peak) and the file's sha256, in one read of its bytes.

    As the text it always was (utf-8-sig, universal newlines, each line stripped): a line beginning "NAME:" in
    any case opens a record, a field's name is what precedes its first ':', and after the peak count every line
    up to the next NAME is a peak line. A record of plain ASCII lines that begin with no whitespace is read whole
    (_census_record); any other stretch of the file one line at a time (_census_line). A record's peak lines are
    judged by _peaks_may_carry, then _carries_peak, once it ends. The file is read in blocks that end where a
    record begins, so a record read whole is never cut in two.
    """
    hasher = hashlib.sha256()
    state: dict[str, Any] = {"records": 0, "with_peak": 0, "precursor": None, "in_peaks": False, "peaks": []}
    pending = b""
    first = True
    read = 0
    with target.open("rb") as handle:
        while True:
            if progress is not None and progress.get("abandoned"):
                raise InterruptedError(f"the census of {target.name} was abandoned: the file changed while it was read")
            chunk = handle.read(_CENSUS_READ_BYTES)
            if chunk:
                hasher.update(chunk)
                read += len(chunk)
                if progress is not None:
                    progress["bytes_read"] = read
            data = pending + chunk
            if first and data:
                if len(data) < 3 and chunk:
                    pending = data
                    continue
                first = False
                if data.startswith(b"\xef\xbb\xbf"):
                    data = data[3:]
            if chunk:
                # Whole lines only, and a lone '\r' at the end may yet be a "\r\n".
                cut = data.rfind(b"\n") + 1
                if cut == 0 and len(data) > 1:
                    # '\r' line ends alone: cut after the last '\r' but one byte, never holding the whole file.
                    cut = data.rfind(b"\r", 0, len(data) - 1) + 1
                if cut == 0:
                    pending = data
                    continue
                block, pending = data[:cut], data[cut:]
            else:
                block, pending = data, b""
            # Universal newlines, as the text reading had them.
            if b"\r" in block:
                block = block.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            lowered = block.lower()
            if chunk:
                # Up to the last line that opens a record: the record it opens is read with the next block.
                last = lowered.rfind(b"\nname:") + 1
                if last > 0:
                    pending = block[last:] + pending
                    block, lowered = block[:last], lowered[:last]
            _census_block(block, lowered, state)
            if not chunk:
                break
    _close_census_record(state)
    return (state["records"], state["with_peak"]), hasher.hexdigest()


# Bytes that make a record unfit to be read whole: whitespace that str.strip removes at a line's start, which the
# record reading does not look for, and the ASCII separators str.strip removes and bytes.strip keeps.
_NOT_WHOLE_RECORD_BYTES = (b"\n ", b"\n\t", b"\x0b", b"\x0c", b"\x1c", b"\x1d", b"\x1e", b"\x1f")


def _close_census_record(state: dict[str, Any]) -> None:
    peaks, precursor = state["peaks"], state["precursor"]
    if peaks and precursor is not None and _peaks_may_carry(peaks, precursor) and _carries_peak(peaks, precursor):
        state["with_peak"] += 1
    peaks.clear()


def _census_block(block: bytes, lowered: bytes, state: dict[str, Any]) -> None:
    """Read one block of whole lines into the census state; ``lowered`` is block.lower()."""
    position, size = 0, len(block)
    while position < size:
        following = lowered.find(b"\nname:", position)
        end = following + 1 if following >= 0 else size
        if not (lowered.startswith(b"name:", position) and _census_record(block, lowered, position, end, state)):
            _census_lines(block, position, end, state)
        position = end


def _census_record(block: bytes, lowered: bytes, start: int, end: int, state: dict[str, Any]) -> bool:
    """Read the record block[start:end], which begins with its NAME line, whole; False where it is not plain.

    Plain: ASCII, and no line beginning with whitespace, so that every line's stripped text begins where the line
    does and no other line of it opens a record. Its precursor is the last precursor field before its peak count,
    and its peak lines everything after that.
    """
    record = block[start:end]
    if not record.isascii() or any(item in record for item in _NOT_WHOLE_RECORD_BYTES):
        return False
    text = lowered[start:end]
    _close_census_record(state)
    state["records"] += 1
    state["precursor"], state["in_peaks"] = None, False
    header_end = peaks_start = -1
    at = 0
    while True:
        at = text.find(b"\nnum", at)
        if at < 0:
            break
        line_end = text.find(b"\n", at + 1)
        line_end = len(text) if line_end < 0 else line_end
        field, colon, _ = text[at + 1:line_end].partition(b":")
        if colon and field.strip() in _CENSUS_FIELDS_PEAK_COUNT:
            header_end, peaks_start = at + 1, line_end + 1
            break
        at = line_end
    at = 0
    limit = header_end if header_end >= 0 else len(text)
    while True:
        at = text.find(b"\nprecursor", at, limit)
        if at < 0:
            break
        line_end = text.find(b"\n", at + 1)
        line_end = len(text) if line_end < 0 else line_end
        field, colon, value = text[at + 1:line_end].partition(b":")
        if colon and field.strip() in _CENSUS_FIELDS_PRECURSOR:
            state["precursor"] = parse_number(value.decode("ascii").strip())
        at = line_end
    if header_end >= 0:
        state["in_peaks"] = True
        if peaks_start < len(record):
            state["peaks"].append(record[peaks_start:])
    return True


def _census_lines(block: bytes, start: int, end: int, state: dict[str, Any]) -> None:
    position = start
    while position < end:
        newline = block.find(b"\n", position, end)
        newline = end if newline < 0 else newline
        _census_line(block[position:newline], state)
        position = newline + 1


def _census_line(raw: bytes, state: dict[str, Any]) -> None:
    """One line, read as the text census read it: stripped, a NAME, a field, or a peak line."""
    text = raw.decode("utf-8", "replace").strip()
    if not text or text.startswith("#"):
        return
    if text[:5].casefold() == "name:":
        _close_census_record(state)
        state["records"] += 1
        state["precursor"], state["in_peaks"] = None, False
        return
    if state["in_peaks"]:
        state["peaks"].append(text.encode("utf-8"))
        return
    if ":" not in text:
        return
    name, _, value = text.partition(":")
    field = name.strip().casefold()
    if field in _MSP_PRECURSOR_FIELDS:
        state["precursor"] = parse_number(value.strip())
    elif field in _MSP_PEAK_COUNT_FIELDS:
        state["in_peaks"] = True


def precursor_plus_1008_census_within(path: str | Path, wait_seconds: float) -> dict[str, Any]:
    """The census within ``wait_seconds``: {"state": "counted", "census": ...} or {"state": "reading", ...}.

    The first read of a 2 GB library takes longer than a plan may keep its caller waiting: the plan of V2's
    negative-mode run timed out on it. The census is read in a thread of its own, which goes on after the wait;
    "reading" says how far it has come, and a later call collects it (or the kept census). A read that fails
    raises an OSError here, whatever it failed with, so that a file that cannot be read is a blocker, never an
    unchecked library. A census is of one file version: a read of an earlier version of the file is abandoned, and
    one whose file changed before it was collected is read again.
    """
    target = Path(path).expanduser().resolve()
    stat = target.stat()
    key = (str(target).casefold(), stat.st_size, stat.st_mtime_ns)
    with _CENSUS_LOCK:
        for other in [item for item in _CENSUS_SCANS if item[0] == key[0] and item != key]:
            _CENSUS_SCANS.pop(other).progress["abandoned"] = 1
        scan = _CENSUS_SCANS.get(key)
        if scan is None:
            scan = _CensusScan(target, stat.st_size)
            _CENSUS_SCANS[key] = scan
            threading.Thread(target=scan.run, name=f"msp-census-{target.name}", daemon=True).start()
    if not scan.done.wait(max(0.0, float(wait_seconds))):
        return {
            "state": "reading",
            "started_at": scan.started,
            "bytes_read": int(scan.progress.get("bytes_read") or 0),
            "size_bytes": scan.size,
        }
    with _CENSUS_LOCK:
        if _CENSUS_SCANS.get(key) is scan:
            del _CENSUS_SCANS[key]
    if scan.error is not None:
        if isinstance(scan.error, OSError):
            raise scan.error
        raise OSError(f"{type(scan.error).__name__}: {scan.error}") from scan.error
    try:
        after = target.stat()
    except OSError:
        after = None
    if after is None or (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
        # Changed while it was read: the count is of no version the run would search.
        return precursor_plus_1008_census_within(target, 0.0)
    return {"state": "counted", "census": dict(scan.result or {})}


def _printable_ascii(text: str) -> bool:
    """What the Console reads back from its ASCII settings files as written."""
    return all(" " <= character <= "~" for character in str(text))


def _paths_by_polarity(value: Any) -> dict[str, str] | None:
    """A library's file: one path for every polarity, or a path per polarity."""
    if isinstance(value, str):
        text = value.strip()
        return {POSITIVE: text, NEGATIVE: text} if text else {}
    if isinstance(value, dict):
        paths: dict[str, str] = {}
        for key, item in value.items():
            polarity = _POLARITY_KEYS.get(str(key).strip().casefold())
            if polarity is None or not isinstance(item, str):
                return None
            if item.strip():
                paths[polarity] = item.strip()
        return paths
    return None if value not in (None, "") else {}


def read_annotation(spec: Any, polarities: Iterable[str]) -> dict[str, Any]:
    """The annotation libraries a person gave, checked, highest priority first.

    spec is {"preset": name, "libraries": {slot: path}} or {"libraries": [entry, ...]}, an entry being
    {kind, path, thresholds, retention_time, rt_tolerance, label}. A path is one file for both
    polarities or {"Positive": ..., "Negative": ...}. Nothing is hashed here (library_identity).
    """
    wanted = [polarity for polarity in POLARITIES if polarity in set(polarities)]
    blockers: list[str] = []
    warnings: list[str] = []
    missing: list[str] = []
    result: dict[str, Any] = {"preset": None, "entries": [], "blockers": blockers, "warnings": warnings,
                              "missing": missing}
    if not isinstance(spec, dict) or not spec:
        missing.append("annotation")
        return result
    # A setting given where it is not read would otherwise be dropped without a word.
    for key in sorted(set(spec) - ANNOTATION_KEYS):
        blockers.append(
            f"annotation.{key} is not read; the annotation takes preset and libraries. A library's own settings go "
            "in the list form, one entry per library."
        )
    preset = str(spec.get("preset") or "").strip()
    libraries = spec.get("libraries")
    entries: list[dict[str, Any]] = []
    if preset:
        if preset not in PRESETS:
            blockers.append(f"annotation preset '{preset}' is none of {', '.join(PRESETS)}.")
            return result
        result["preset"] = preset
        if libraries in (None, ""):
            libraries = {}
        if not isinstance(libraries, dict):
            blockers.append(f"annotation.libraries for the preset {preset} maps each slot to its file.")
            return result
        slots = {item["slot"] for item in PRESETS[preset]}
        for key in sorted(set(libraries) - slots):
            blockers.append(f"annotation.libraries.{key} is no slot of the preset {preset} ({', '.join(sorted(slots))}).")
        for template in PRESETS[preset]:
            value = libraries.get(template["slot"])
            kind_of_spectra = None
            if isinstance(value, dict) and ("path" in value or "library_kind" in value):
                # A slot may say what its spectra are: {"path": ..., "library_kind": "predicted"}.
                for key in sorted(set(value) - SLOT_KEYS):
                    blockers.append(f"annotation.libraries.{template['slot']}.{key} is not read; a slot takes path and library_kind.")
                kind_of_spectra = value.get("library_kind")
                value = value.get("path")
            paths = _paths_by_polarity(value)
            if paths is None:
                blockers.append(f"annotation.libraries.{template['slot']} is a path, or a path per polarity.")
                continue
            if not paths and template.get("optional"):
                continue
            entry = {**template, "paths": paths}
            if kind_of_spectra not in (None, ""):
                entry["library_kind"] = kind_of_spectra
            entries.append(entry)
    else:
        if not isinstance(libraries, list) or not libraries:
            blockers.append("annotation names no preset and lists no libraries.")
            return result
        for number, item in enumerate(libraries, start=1):
            if not isinstance(item, dict):
                blockers.append(f"annotation.libraries[{number}] is not an object.")
                continue
            for key in sorted(set(item) - ENTRY_KEYS):
                blockers.append(
                    f"annotation.libraries[{number}].{key} is not read; an entry takes {', '.join(sorted(ENTRY_KEYS))}."
                )
            kind = str(item.get("kind") or "").strip().casefold()
            paths = _paths_by_polarity(item.get("path", item.get("paths")))
            if paths is None:
                blockers.append(f"annotation.libraries[{number}].path is a path, or a path per polarity.")
                continue
            entries.append({
                "id": f"{kind}_{str(item.get('thresholds') or 'standard').strip().casefold()}" if kind != "lbm" else "lbm",
                "slot": f"libraries[{number}]", "kind": kind,
                "thresholds": str(item.get("thresholds") or ("standard" if kind == "msp" else "")).strip().casefold(),
                "retention_time": str(item.get("retention_time") or "").strip().casefold(),
                "rt_tolerance": item.get("rt_tolerance"),
                "label": str(item.get("label") or "").strip() or f"{kind.upper()} {number}",
                "library_kind": str(item.get("library_kind") or "").strip().casefold(),
                "paths": paths,
            })

    count = len(entries)
    for index, entry in enumerate(entries, start=1):
        where = f"annotation library {index} ({entry.get('label') or entry['slot']})"
        # Set before anything can stop this entry: the person is shown every entry, refused or not.
        entry["priority"] = count - index + 1 + INTERNAL_STANDARD_PRIORITY
        entry["annotator_id"] = f"lib{index}_" + _IDENTIFIER.sub("_", str(entry["id"]).casefold()).strip("_")
        entry["library_kind"] = str(entry.get("library_kind") or "").strip().casefold()
        if entry["kind"] not in LIBRARY_KINDS:
            blockers.append(f"{where}: kind '{entry['kind']}' is neither msp nor lbm.")
            entry["rt_tolerance"] = None
            entry["paths"] = {polarity: entry["paths"][polarity] for polarity in wanted if entry["paths"].get(polarity)}
            continue
        if entry["kind"] == "lbm" and entry["library_kind"]:
            blockers.append(f"{where}: library_kind is said of an MSP; an LBM2's spectra are the library's own rules.")
        if entry["kind"] == "msp" and entry["library_kind"] not in ("", *LIBRARY_KINDS_OF_SPECTRA):
            blockers.append(f"{where}: library_kind '{entry['library_kind']}' is neither acquired nor predicted.")
        if entry["kind"] == "lbm":
            entry["library_kind"] = ""
        if entry["kind"] == "msp" and entry.get("thresholds") not in THRESHOLDS:
            blockers.append(f"{where}: thresholds '{entry.get('thresholds')}' is none of {', '.join(THRESHOLDS)}.")
        if entry["retention_time"] not in RETENTION_TIME_USES:
            blockers.append(f"{where}: retention_time is 'none' (neither filtering nor scoring) or 'use'.")
        tolerance = parse_number(entry.get("rt_tolerance")) if entry.get("rt_tolerance") not in (None, "") else None
        if entry["retention_time"] == "use":
            if tolerance is None or tolerance <= 0 or not math.isfinite(tolerance):
                blockers.append(f"{where}: retention_time 'use' needs rt_tolerance, a positive number of minutes.")
                tolerance = None
        else:
            tolerance = None
        entry["rt_tolerance"] = tolerance
        if not _LABEL.match(str(entry.get("label") or "")) or "\t" in str(entry.get("label")):
            # The label reaches msp_annotator_settings.tsv, which the Console reads as ASCII.
            blockers.append(f"{where}: label is printable ASCII of at most 60 characters.")
        for polarity in wanted:
            path = entry["paths"].get(polarity)
            if not path:
                # An optional library may serve one polarity only; the other runs without it.
                if not entry.get("optional"):
                    missing.append(f"{entry['slot']} ({polarity.lower()})")
                continue
            target = Path(path).expanduser()
            if not _printable_ascii(str(target.resolve())):
                # msp_annotator_settings.tsv is written and read as ASCII.
                blockers.append(
                    f"{where}: {target} is not an ASCII path, which the Console's annotator settings file cannot carry; "
                    "move the library or give it a path in ASCII."
                )
            elif not target.is_file():
                blockers.append(f"{where}: {target} is not a file.")
            elif target.suffix.casefold() not in LIBRARY_SUFFIXES[entry["kind"]]:
                blockers.append(f"{where}: {target.name} is not an {' or '.join(LIBRARY_SUFFIXES[entry['kind']])} library.")
            elif target.stat().st_size == 0:
                # The Console skips an empty library without a word.
                blockers.append(f"{where}: {target.name} is empty.")
        entry["paths"] = {polarity: entry["paths"][polarity] for polarity in wanted if entry["paths"].get(polarity)}
    lbm = [entry for entry in entries if entry["kind"] == "lbm"]
    if len(lbm) > 1:
        blockers.append("The Console reads one LBM2 per run; the annotation lists more than one.")
    for polarity in wanted:
        by_path: dict[str, list[str]] = {}
        for entry in entries:
            path = entry["paths"].get(polarity)
            if path:
                by_path.setdefault(str(Path(path).expanduser().resolve()).casefold(), []).append(entry)
        for same in by_path.values():
            settings = {(entry.get("thresholds"), entry["retention_time"], entry["rt_tolerance"]) for entry in same}
            if len(same) > 1 and len(settings) < len(same):
                warnings.append(
                    f"{polarity}: {', '.join(entry['label'] for entry in same)} search one file with the same settings."
                )
            # The Console loads a file once and gives every row of it the first row's kind.
            if len({entry.get("library_kind") or "acquired" for entry in same}) > 1:
                blockers.append(
                    f"{polarity}: {', '.join(entry['label'] for entry in same)} search one file as both acquired and "
                    "predicted spectra; a file is one or the other."
                )
    result["entries"] = entries
    return result


def export_annotator_ids(entry: dict[str, Any]) -> dict[str, str]:
    """The annotator ID an export's Comment gives this library, by polarity."""
    if entry.get("kind") == "lbm":
        return {polarity: console_lbm_annotator_id(path) for polarity, path in entry.get("paths", {}).items()}
    return {polarity: str(entry.get("annotator_id") or "") for polarity in entry.get("paths", {})}


def in_silico_annotator_ids(entries: Iterable[dict[str, Any]]) -> set[str]:
    """The annotator IDs of the MSP libraries searched as predicted (in-silico) spectra.

    An LBM2 is never one: its annotation is the rule-based MS/MS diagnostics of the lipid pipeline, not a
    match against predicted spectra. A library of predicted retention times (lab_rt_tiers' predicted_rt) is
    not one either: its spectra are acquired.
    """
    return {
        str(entry.get("annotator_id") or "").strip()
        for entry in entries
        if entry.get("kind") == "msp" and is_predicted_library_kind(entry.get("library_kind"))
        and str(entry.get("annotator_id") or "").strip()
    }


def is_in_silico_representative(
    representative: dict[str, Any] | None,
    *,
    evidence_source: Any = None,
    code: Any = None,
    annotator: str = "",
    in_silico_annotators: Collection[str] = (),
) -> bool:
    """Whether a spot's representative annotation rests on an in-silico (predicted) spectrum.

    Such a match is never a reference match (the MS-DIAL author's decision of 2026-10-28), although the
    candidate table's is_reference_matched is true for an accepted one. It is read as the QA reads a peak
    (quality_assurance.ReferenceMatchReading): the candidate table's evidence_source where the Console writes
    one, else the export's Evidence source, else code 450 (annotation_codes.is_in_silico_match), and, whatever
    the Console wrote, an annotator the contract searched as a library of predicted spectra. An LBM2's
    rule-based match (evidence source RuleBased), a text library's and an acquired MSP's are none of these.
    """
    row = representative or {}
    evidence = str(row.get("evidence_source") or "").strip() or evidence_source
    tag = str(row.get("annotation_tag_vs1") or "").strip() or code
    annotator_id = str(row.get("annotator_id") or "").strip() or str(annotator or "").strip()
    return is_in_silico_match(tag, evidence) or bool(annotator_id and annotator_id in set(in_silico_annotators))


def annotation_summary(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The libraries as the person is shown them: no path, the settings each is searched with."""
    return [
        {
            "priority": entry.get("priority"), "label": entry.get("label"), "kind": entry.get("kind"),
            "annotator_id": entry.get("annotator_id"), "export_annotator_ids": export_annotator_ids(entry),
            "thresholds": entry.get("thresholds") or None,
            "threshold_values": THRESHOLDS.get(entry.get("thresholds") or "", {}) if entry.get("kind") == "msp" else None,
            "library_kind": (entry.get("library_kind") or "acquired") if entry.get("kind") == "msp" else None,
            # As apply_annotators writes the row: a predicted row that leaves it unset does not suggest.
            "suggestions": (
                entry.get("suggestions", not is_predicted_library_kind(entry.get("library_kind")))
                if entry.get("kind") == "msp" else None
            ),
            "retention_time": entry.get("retention_time"), "rt_tolerance": entry.get("rt_tolerance"),
            "files": {polarity: Path(path).name for polarity, path in entry.get("paths", {}).items()},
        }
        for entry in entries
    ]


def annotator_settings(entries: Iterable[dict[str, Any]], polarity: str) -> dict[str, Any]:
    """One polarity's annotators as the guided plan takes them (libraries.annotators).

    Every MSP row is rt_fixed: its retention-time use was decided for that library, so the run-wide
    answer leaves it as it is (agent_workflow._apply_retention_time_use).
    """
    rows: list[dict[str, Any]] = []
    for entry in entries:
        path = entry["paths"].get(polarity)
        if not path:
            continue
        rows.append({
            "kind": entry["kind"], "annotator_id": entry["annotator_id"], "label": entry["label"],
            "path": str(Path(path).expanduser().resolve()), "priority": entry["priority"],
            "thresholds": entry.get("thresholds") or "", "retention_time": entry["retention_time"],
            "rt_tolerance": entry["rt_tolerance"],
            **({"library_kind": entry.get("library_kind") or "acquired"} if entry["kind"] == "msp" else {}),
            **({"suggestions": entry["suggestions"]}
               if entry["kind"] == "msp" and entry.get("suggestions") is not None else {}),
        })
    return {"annotators": rows}


def apply_annotators(state: dict[str, Any], annotators: Iterable[dict[str, Any]]) -> None:
    """Write libraries.annotators into the workflow state: MSP rows, the LBM2 and their provenance.

    An MSP row's thresholds name one of THRESHOLDS; any other name, or none, is refused (ValueError) rather than
    searched at "standard", as it was until 0.5.49. A predicted (in-silico) row that leaves suggestions unset is
    written with suggestions false: in-silico rows do not suggest (the author's decision), and the Console's own
    default is on. The plan refuses a predicted row with suggestions true (agent_workflow._annotator_row_problems).
    """
    state["msp_annotators"] = []
    provenance: list[dict[str, Any]] = []
    for item in annotators:
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        use_rt = str(item.get("retention_time") or "none") == "use"
        preset = str(item.get("thresholds") or "").strip()
        # As the plan reads it (agent_workflow._annotator_row_problems): any case of "lbm" is the LBM2.
        is_lbm = str(item.get("kind") or "msp").strip().casefold() == "lbm"
        if not is_lbm and preset not in THRESHOLDS:
            raise ValueError(
                f"libraries.annotators ({item.get('label') or item.get('annotator_id') or Path(path).name}): thresholds "
                f"'{item.get('thresholds') or ''}' is none of {', '.join(THRESHOLDS)}."
            )
        if is_lbm:
            state["lbm_path"] = path
            state["lbm_annotator"] = {**dict(state.get("lbm_annotator") or {}), "lbm_file_path": path,
                                      "priority": int(item.get("priority") or 1)}
        else:
            row: dict[str, Any] = {
                "annotator_id": str(item.get("annotator_id") or f"msp_annotator_{len(state['msp_annotators']) + 1}"),
                "msp_file_path": path,
                "priority": int(item.get("priority") or 1),
                "target_omics": "Metabolomics",
                "evidence_tier": str(item.get("label") or ""),
                "use_rt_scoring": use_rt,
                "use_rt_filtering": use_rt,
                # Decided for this library: the run-wide retention-time answer does not reach it.
                "rt_fixed": True,
                # A predicted library's matches are published as in silico, never as reference spectra.
                "library_kind": str(item.get("library_kind") or "acquired"),
                **THRESHOLDS[preset],
            }
            if use_rt and item.get("rt_tolerance") not in (None, ""):
                row["rt_tolerance"] = float(item["rt_tolerance"])
            # Only where the library says it: the annotator table writes the column for a row that sets it,
            # and a plan is refused on a Console without the switch (workflow._annotator_capability_issues).
            if item.get("suggestions") is not None:
                row["suggestions"] = _as_switch(item["suggestions"])
            elif is_predicted_library_kind(row["library_kind"]):
                # Every spelling the Console reads as predicted (in_silico, InSilico, ...), as it reads them.
                row["suggestions"] = False
            state["msp_annotators"].append(row)
        provenance.append({"path": path, "version": "", "source": "", "doi": "",
                           "license": "institutional/private", "label": str(item.get("label") or "")})
    state["library_provenance"] = provenance


def lbm_retention_time(entries: Iterable[dict[str, Any]]) -> tuple[bool, float | None]:
    """The run-wide retention-time answer: the LBM2's own, the only library it still reaches."""
    for entry in entries:
        if entry["kind"] == "lbm":
            return entry["retention_time"] == "use", entry["rt_tolerance"]
    return False, None


# ---------------------------------------------------------------------------------------------
# Internal-standard texts
# ---------------------------------------------------------------------------------------------

# MS-DIAL's own text library may be Name, Pos-m/z (or Neg-m/z), RT, Adduct and nothing else: no class is needed
# to normalize by a standard, so Ontology is not required here as it is for lipidomics.
IS_TEXT_COLUMNS_METABOLOMICS = {**IS_TEXT_COLUMNS, "mz": (*IS_TEXT_COLUMNS["mz"], "posmz", "negmz")}
IS_TEXT_REQUIRED_METABOLOMICS = ("name", "mz", "rt", "adduct")


# ---------------------------------------------------------------------------------------------
# Pairing the polarities when their file names differ
# ---------------------------------------------------------------------------------------------

_TOKEN_SPLIT = re.compile(r"[_\-.\s]+")
PAIRED_FIELDS = ("file_type", "class_id", "analytical_order", "batch_order", "factor", "acquisition_type")
# A sample word of letters and a number (A1, QC12): a replicate of the group its letters name.
_REPLICATE_WORD = re.compile(r"^([A-Za-z]+)(\d+)$")
REPLICATE_CLASS_SOURCE = "shared_sample_word_letters"
# Letters of a sample word that name an injection of another kind (Blank1, QC2, Std3, IS1), beside what
# sample_grouping already reads as a blank or a QC.
NON_REPLICATE_LETTERS = frozenset({"blank", "blk", "qc", "pqc", "pool", "pooled", "std", "standard", "sst", "is",
                                   "istd", "mb", "solvent"})


def name_tokens(name: str) -> list[str]:
    """A file name's words, without its extension: 20230125_SWATH_A1_C_CE20_d1.raw -> [20230125, SWATH, A1, ...]."""
    return [token for token in _TOKEN_SPLIT.split(Path(str(name)).stem) if token]


def _names_no_group(letters: str) -> bool:
    """Letters that name a blank, a QC or a standard injection (Blank1, QC2, Std3), never a group of replicates."""
    text = letters.casefold()
    return text in NON_REPLICATE_LETTERS or is_blank(letters) or is_quality_control(letters)


def replicate_classes(shared: dict[str, list[str]], file_types: dict[str, str] | None = None) -> dict[str, str] | None:
    """Each Sample pair's class read from its one letter+number sample word (A1 -> A), or None.

    Only Sample pairs count (file_types by stem, Sample when not given), and letters that name a blank, a QC
    or a standard (Blank1, QC2, Std3) are never a group: such a pair keeps its own class. Every other Sample
    pair must have exactly one such word, and the letters must make at least two groups of at least two pairs
    each: S1 to S16 are sample numbers, not replicates of a group, even beside QC1 to QC3.
    """
    classes: dict[str, str] = {}
    for stem, words in shared.items():
        if str((file_types or {}).get(stem) or "Sample") != "Sample":
            continue
        found = [match.group(1) for match in (_REPLICATE_WORD.match(word) for word in words) if match]
        if len(found) != 1:
            return None
        if _names_no_group(found[0]):
            continue
        classes[stem] = found[0]
    counts = [list(classes.values()).count(name) for name in set(classes.values())]
    if len(counts) < 2 or min(counts) < 2:
        return None
    return classes


def name_classes_fail(rows: list[dict[str, Any]], classes: dict[str, tuple[Any, Any]]) -> bool:
    """Whether the classes the file names proposed fail this pairing: no Sample pair's two files were given one
    class (case aside) other than none or "Sample". Only then do letters decide, for every Sample pair alike;
    one pair that agrees keeps every pair's names' class, so one vocabulary is never mixed with another.

    classes: each pair's (positive, negative) class_id by stem.
    """
    def named(value: Any) -> str:
        text = str(value or "").strip().casefold()
        return "" if text == "sample" else text

    samples = [row["stem"] for row in rows if str(row.get("file_type") or "Sample") == "Sample"]
    return bool(samples) and not any(
        named(classes.get(stem, (None, None))[0]) and named(classes[stem][0]) == named(classes[stem][1])
        for stem in samples
    )


def pair_by_shared_tokens(positive: list[dict[str, Any]], negative: list[dict[str, Any]]) -> dict[str, Any]:
    """Positive and negative files paired by the words that tell the files of each polarity apart.

    Positive and negative runs are often acquired on other days under other names (20230125_SWATH_A1_C_CE20_d1
    and 20230208_SWATH_A1_A_CE20_d2): the words all files of a polarity share say nothing, and the ones left
    (A1) name the sample. A file pairs with the one file of the other polarity that shares the most of them,
    and only when that is mutual. The sample takes the shared words' name. The person confirms the pairing.

    Where the file names give no Sample pair a class both polarities agree on, case aside (one polarity's word d2
    against the other's re-injection d2r2, or no class at all), and the shared words are letter+number replicates
    (A1 to D4), each Sample pair's class is read from the letters (A) (replicate_classes), and the warnings say so.
    Where even one Sample pair's names agree (Ctrl and Treat beside Mouse1 to Mouse12, WT beside wt), every pair
    keeps its names' class, and each pair whose two classes differ is said.
    """
    def varying(rows: list[dict[str, Any]]) -> list[list[str]]:
        words = [name_tokens(row["file_name"]) for row in rows]
        common = set.intersection(*(set(item) for item in words)) if words else set()
        return [[word for word in item if word not in common] for item in words]

    left, right = varying(positive), varying(negative)
    problems: list[str] = []
    warnings: list[str] = []
    best_for_left: dict[int, int] = {}
    for index, words in enumerate(left):
        scores = [(len(set(words) & set(other)), number) for number, other in enumerate(right)]
        top = max((score for score, _ in scores), default=0)
        chosen = [number for score, number in scores if score == top and top > 0]
        if len(chosen) == 1:
            best_for_left[index] = chosen[0]
        else:
            problems.append(
                f"{positive[index]['file_name']} has {'no' if not chosen else 'more than one'} negative counterpart by the "
                f"words that set it apart ({', '.join(words) or 'none'})."
            )
    claimed: dict[int, list[int]] = {}
    for index, number in best_for_left.items():
        claimed.setdefault(number, []).append(index)
    rows: list[dict[str, Any]] = []
    differences: list[tuple[str, str, Any, Any]] = []
    name_classes: dict[str, tuple[Any, Any]] = {}
    for number, indices in sorted(claimed.items(), key=lambda item: min(item[1])):
        if len(indices) > 1:
            problems.append(
                f"{negative[number]['file_name']} would pair with {', '.join(positive[index]['file_name'] for index in indices)}."
            )
            continue
        index = indices[0]
        shared = [word for word in left[index] if word in set(right[number])]
        source, other = positive[index], negative[number]
        row = {"stem": "_".join(shared), "positive_file": source["file_name"], "negative_file": other["file_name"],
               "paired_by": shared}
        for key in PAIRED_FIELDS:
            row[key] = source.get(key)
            if other.get(key) != source.get(key):
                differences.append((row["stem"], key, source.get(key), other.get(key)))
        name_classes[row["stem"]] = (source.get("class_id"), other.get("class_id"))
        rows.append(row)
    # A class both polarities' file names agree on stands: letters decide only where no Sample pair agrees.
    classes = (
        replicate_classes({row["stem"]: row["paired_by"] for row in rows},
                          {row["stem"]: str(row.get("file_type") or "Sample") for row in rows})
        if rows and name_classes_fail(rows, name_classes) else None
    )
    # Every pair whose two files differ is said, the class included when the letters then decide it.
    for stem, key, value, other_value in differences:
        warnings.append(
            f"{stem}: {key} is {value!r} in positive and {other_value!r} in negative; "
            + ("the class is proposed from the letters." if key == "class_id" and classes and stem in classes
               else "the positive value is proposed.")
        )
    class_proposal: dict[str, Any] = {}
    if classes:
        for row in rows:
            row["class_id"] = classes.get(row["stem"], row["class_id"])
        counts = {name: list(classes.values()).count(name) for name in sorted(set(classes.values()))}
        kept = sorted(row["stem"] for row in rows if row["stem"] not in classes)
        class_proposal = {
            "derived_from": REPLICATE_CLASS_SOURCE,
            "reason": "each Sample pair's shared sample word is letters and a number (A1); its letters name the class "
                      "(A), since the file names gave no Sample pair a class both polarities agree on",
            "groups": counts,
            "kept_own_class": kept,
            "polarity_disagreements": [
                {"stem": stem, "positive": value, "negative": other_value}
                for stem, key, value, other_value in differences if key == "class_id"
            ],
            "confirmation_required": True,
        }
        warnings.append(
            "The class of each Sample pair was read from its shared sample word's letters ("
            + ", ".join(f"{name}: {count}" for name, count in counts.items())
            + "), because the file names gave no Sample pair a class both polarities agree on"
            + (f"; {', '.join(kept)} keep their own" if kept else "")
            + ". Confirm it, or correct class_id in sample_table."
        )
    for number, other in enumerate(negative):
        if number not in claimed:
            problems.append(f"{other['file_name']} has no positive counterpart by the words that set it apart.")
    names = [row["stem"] for row in rows]
    for name in sorted({name for name in names if names.count(name) > 1}):
        problems.append(f"Two pairs would both be named {name}.")
    rows.sort(key=lambda row: (row.get("analytical_order") or 0, row["stem"].casefold()))
    return {"rows": rows, "problems": problems, "warnings": warnings, "matched": not problems,
            "class_proposal": class_proposal}


# ---------------------------------------------------------------------------------------------
# Preferred adducts
# ---------------------------------------------------------------------------------------------

# The negative adduct each salt forms besides [M-H]-; the bicarbonate buffer forms none worth preferring.
SALT_NEGATIVE_ADDUCT = {"HCOONH4": "[M+HCOO]-", "CH3COONH4": "[M+CH3COO]-", "NH4HCO3": None}


def preferred_adducts(salt: str | None) -> list[str]:
    adducts = ["[M+H]+", "[M-H]-"]
    extra = SALT_NEGATIVE_ADDUCT.get(str(salt or ""))
    if extra:
        adducts.append(extra)
    return adducts


def adduct_identities(names: Iterable[str]) -> set[tuple[Any, ...]]:
    found = set()
    for name in names:
        try:
            found.add(parse_adduct(name).identity)
        except ValueError:
            continue
    return found


def is_preferred(adduct: str, identities: set[tuple[Any, ...]]) -> bool:
    """[M+FA-H]- is [M+HCOO]-, [M+Hac-H]- is [M+CH3COO]-: an adduct is compared as the ion it names."""
    try:
        return parse_adduct(adduct).identity in identities
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------
# Normalization by one internal standard
# ---------------------------------------------------------------------------------------------

NORMALIZATION_FILE_TYPES = ("sample", "qc")


def _rows_with_cells(path: str | Path) -> dict[str, Any]:
    try:
        export = describe_export(path)
    except MergeRefused as error:
        raise LabInputError(str(error)) from None
    names = export["header"][4]
    try:
        id_column = names.index("Alignment ID")
    except ValueError:
        raise LabInputError(f"{path} has no Alignment ID column.") from None
    export["id_column"] = id_column
    export["count"] = len(export["sample_names"])
    return export


# A Blank or Standard injection whose standard is below this share of the lowest Sample or QC height carries
# no spike: gap filling put noise there, and dividing by it would multiply the injection by its inverse.
RAW_HEIGHT_FRACTION = 0.1


def raw_height_injections(
    standard: list[float], kinds: list[str], names: list[str], not_detected: Iterable[str] = (),
) -> dict[str, str]:
    """The Blank and Standard injections kept at their raw height, each with why.

    No height; not detected (MS-DIAL filled the gap: its peak ID is -1); or below RAW_HEIGHT_FRACTION of the
    lowest Sample or QC height. Samples and QCs are never among them: without the standard they refuse.
    """
    missing = set(not_detected)
    counted = [height for height, kind in zip(standard, kinds) if kind in NORMALIZATION_FILE_TYPES and height > 0]
    floor = min(counted) * RAW_HEIGHT_FRACTION if counted else 0.0
    kept: dict[str, str] = {}
    for height, kind, name in zip(standard, kinds, names):
        if kind in NORMALIZATION_FILE_TYPES:
            continue
        if height <= 0:
            kept[name] = "the standard has no height"
        elif name in missing:
            kept[name] = "the standard was not detected; MS-DIAL filled the gap"
        elif height < floor:
            kept[name] = f"the standard's height {height:g} is below {RAW_HEIGHT_FRACTION:.0%} of the lowest Sample or QC height"
    return kept


def normalize_heights(
    height_export: str | Path, standard_id: str, factors: dict[str, float] | None = None,
    not_detected: Iterable[str] = (),
) -> dict[str, Any]:
    """Every feature's height divided by the standard's in its injection, times the standard's mean.

    normalized[s][i] = height[s][i] / height[IS][i] x mean(height[IS] over the Sample and QC injections),
    times the injection's dilution factor. A Sample or QC whose standard has no height blocks; a Blank or
    Standard injection without the standard (raw_height_injections) is left as its raw height and reported.
    not_detected names the injections where MS-DIAL filled the standard's gap. Nothing is written.
    """
    export = _rows_with_cells(height_export)
    start = export["first_sample_column"]
    count = export["count"]
    names = export["sample_names"]
    kinds = [kind.casefold() for kind in export["file_types"]]
    standard_row = next(
        (row for row in export["rows"] if len(row) > export["id_column"] and row[export["id_column"]].strip() == str(standard_id)),
        None,
    )
    problems: list[str] = []
    if standard_row is None:
        raise LabInputError(f"Alignment ID {standard_id} is not in {Path(height_export).name}.")
    cells = [standard_row[start + offset].strip() if start + offset < len(standard_row) else "" for offset in range(count)]
    standard = [0.0 if _is_zero_height(cell) else float(parse_number(cell)) for cell in cells]
    counted = [index for index, kind in enumerate(kinds) if kind in NORMALIZATION_FILE_TYPES]
    if not counted:
        problems.append("The export lists no Sample or QC injection, so the standard has no mean to scale by.")
    zero_required = [names[index] for index in counted if standard[index] <= 0]
    if zero_required:
        problems.append(
            f"The standard (alignment ID {standard_id}) has no height in {', '.join(zero_required)}; a Sample or "
            "QC divided by nothing is not normalized."
        )
    raw_reasons = raw_height_injections(standard, kinds, names, not_detected)
    raw_kept = [name for name in names if name in raw_reasons]
    mean = sum(standard[index] for index in counted) / len(counted) if counted else 0.0
    multipliers = [float((factors or {}).get(name, 1.0) or 1.0) for name in names]
    values: dict[str, list[float | None]] = {}
    if not problems:
        for row in export["rows"]:
            if len(row) <= export["id_column"] or not row[export["id_column"]].strip():
                continue
            line: list[float | None] = []
            for offset in range(count):
                text = row[start + offset].strip() if start + offset < len(row) else ""
                height = parse_number(text)
                if height is None:
                    line.append(None)
                elif names[offset] in raw_reasons:
                    line.append(height)
                else:
                    line.append(height / standard[offset] * mean * multipliers[offset])
            values[row[export["id_column"]].strip()] = line
    return {
        "height_export": str(Path(height_export).resolve()),
        "height_export_sha256": file_sha256(height_export),
        "standard_id": str(standard_id),
        "standard_heights": dict(zip(names, standard)),
        "standard_mean": mean,
        "mean_over": [names[index] for index in counted],
        "raw_height_kept": raw_kept,
        "raw_height_reasons": raw_reasons,
        "multipliers": {
            name: (None if name in raw_reasons or standard[index] <= 0 else mean / standard[index] * multipliers[index])
            for index, name in enumerate(names)
        },
        "dilution_factors": {name: factor for name, factor in zip(names, multipliers) if factor != 1.0},
        "problems": problems,
        "values": values,
        "export": export,
    }


def normalized_text(normalized: dict[str, Any]) -> str:
    """The normalized matrix in MS-DIAL's export layout: the header lines and metadata as they were.

    Only the sample cells change. MS-DIAL's per-class Average and Stdev columns were computed from
    heights, so they are left empty rather than left wrong.
    """
    export = normalized["export"]
    start = export["first_sample_column"]
    count = export["count"]
    lines = ["\t".join(row) for row in export["header"]]
    for row in export["rows"]:
        identifier = row[export["id_column"]].strip() if len(row) > export["id_column"] else ""
        if identifier not in normalized["values"]:
            lines.append("\t".join(row))
            continue
        out = list(row)
        for offset, value in enumerate(normalized["values"][identifier]):
            if start + offset < len(out):
                out[start + offset] = "" if value is None else net_double_tostring(value)
        for index in range(start + count, len(out)):
            out[index] = ""
        lines.append("\t".join(out))
    return "\r\n".join(lines) + "\r\n"


def verify_normalized(
    path: str | Path, height_export: str | Path, standard_id: str, factors: dict[str, float] | None = None,
    *, expected_height_sha256: str = "", not_detected: Iterable[str] = (),
) -> dict[str, Any]:
    """Every cell of the written matrix recomputed on its own from the Height export, and compared.

    Read through lab_lipidomics.read_alignment_export, not the rows normalize_heights computed from: the
    standard's heights, its Sample and QC mean and each injection's factor are taken again, and each cell
    must be height / standard x mean x factor, or the raw height in a Blank or Standard injection without
    the standard (raw_height_injections, with the same not_detected). expected_height_sha256 binds the
    Height export to the one normalize read.
    """
    problems: list[str] = []
    if expected_height_sha256 and file_sha256(height_export) != expected_height_sha256:
        return {"passed": False, "checked": 0, "failed": [],
                "problems": [f"{Path(height_export).name} is not the Height export normalize read."]}
    heights = read_alignment_export(height_export)
    written = read_alignment_export(path)
    if written["sample_names"] != heights["sample_names"]:
        return {"passed": False, "checked": 0, "failed": [],
                "problems": ["The written matrix lists other samples than the Height export."]}
    names = heights["sample_names"]
    kinds = [kind.casefold() for kind in heights["file_types"]]
    standard_spot = next((spot for spot in heights["spots"] if spot["id"] == str(standard_id)), None)
    if standard_spot is None:
        return {"passed": False, "checked": 0, "failed": [],
                "problems": [f"Alignment ID {standard_id} is not in {Path(height_export).name}."]}
    standard = [0.0 if _is_zero_height(cell) else float(parse_number(cell)) for cell in standard_spot["cells"]]
    counted = [standard[index] for index, kind in enumerate(kinds) if kind in NORMALIZATION_FILE_TYPES]
    mean = sum(counted) / len(counted) if counted else 0.0
    multipliers = [float((factors or {}).get(name, 1.0) or 1.0) for name in names]
    raw = raw_height_injections(standard, kinds, names, not_detected)
    by_id = {spot["id"]: spot for spot in written["spots"]}
    failed: list[dict[str, Any]] = []
    checked = 0
    for spot in heights["spots"]:
        target = by_id.get(spot["id"])
        if target is None:
            failed.append({"alignment_id": spot["id"], "sample": None, "written": None, "expected": "a row"})
            continue
        for offset, cell in enumerate(spot["cells"]):
            height = parse_number(cell)
            text = target["cells"][offset] if offset < len(target["cells"]) else ""
            checked += 1
            if height is None:
                if text != "":
                    failed.append({"alignment_id": spot["id"], "sample": names[offset], "written": text, "expected": ""})
                continue
            if names[offset] in raw:
                expected = height
            elif standard[offset] <= 0:
                failed.append({"alignment_id": spot["id"], "sample": names[offset], "written": text,
                               "expected": "no value: the standard has no height in a Sample or QC"})
                continue
            else:
                expected = height / standard[offset] * mean * multipliers[offset]
            got = parse_number(text)
            if got is None or not math.isclose(got, expected, rel_tol=1e-12, abs_tol=1e-9):
                failed.append({"alignment_id": spot["id"], "sample": names[offset], "written": text, "expected": expected})
    extra = sorted(set(by_id) - {spot["id"] for spot in heights["spots"]})
    if extra:
        problems.append(f"{len(extra)} row(s) of the written matrix are not in the Height export.")
    if failed:
        problems.append(f"{len(failed)} cell(s) or row(s) do not follow the formula.")
    return {"passed": not problems, "checked": checked, "failed": failed[:50], "problems": problems,
            "standard_mean": mean}


# ---------------------------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------------------------

_INCHIKEY = re.compile(r"^([A-Z]{14})(?:-[A-Z]{10}-[A-Z])?$")
UNANNOTATED_NAMES = frozenset({"", "unknown", "null"})
MERGE_FILE_TYPES = ("Sample", "QC")
METABOLOME_NAME = "metabolome.csv"
REMOVED_NAME = "metabolome_removed.tsv"
SAMPLES_NAME = "metabolome_samples.csv"
MERGE_REPORT_NAME = "merge-report.json"
DEFAULT_MERGE_OPTIONS = {"reference_matched_only": True, "preferred_adducts_only": False}
# The metabolome's column, and the removal reason, for a row an accepted in-silico match represents: the QA's
# name for the same cells (quality_assurance.IN_SILICO_MATCHED_COLUMN).
IN_SILICO_MATCHED_COLUMN = "In-silico matched"
IN_SILICO_REASON = "in-silico matched"
REFERENCE_MATCH_DEFINITION = (
    "a row is reference matched where MS-DIAL accepted its representative annotation and that annotation is no "
    "in-silico (predicted-spectrum) match: read from the candidate table's evidence_source, else the export's "
    "Evidence source, else code 450, and from the libraries the contract searched as predicted spectra. An "
    "accepted in-silico match is In-silico matched instead, and reference_matched_only drops it; an LBM2's "
    "rule-based match is a reference match"
)
REMOVED_COLUMNS = (
    "Polarity", "Alignment ID", "Metabolite name", "Adduct type", "InChIKey", "Reason",
    "Kept polarity", "Kept alignment ID", "Kept name", "Kept adduct",
)


def inchikey_first_block(value: str) -> str:
    """The skeleton block of an InChIKey, or "" when the cell holds no InChIKey."""
    match = _INCHIKEY.match(str(value or "").strip())
    return match.group(1) if match else ""


def _column(names: list[str], title: str) -> int:
    try:
        return names.index(title)
    except ValueError:
        return -1


def _read_polarity(item: dict[str, Any], in_silico_annotators: Collection[str] = ()) -> dict[str, Any]:
    export = _rows_with_cells(item["normalized"])
    names = export["header"][4]
    columns = {key: _column(names, title) for key, title in (
        ("name", "Metabolite name"), ("adduct", "Adduct type"), ("inchikey", "INCHIKEY"),
        ("comment", "Comment"), ("total_score", "Total score"), ("rt", "Average Rt(min)"),
        ("rt_matched", "RT matched"), ("ms2_matched", "MS/MS matched"),
        ("evidence_source", "Evidence source"), ("code", "Annotation tag (VS1.0)"),
    )}
    missing = [title for key, title in (("name", "Metabolite name"), ("adduct", "Adduct type"),
                                         ("inchikey", "INCHIKEY"), ("comment", "Comment")) if columns[key] < 0]
    if missing:
        raise LabInputError(f"{item['normalized']} has no {', '.join(missing)} column(s).")
    candidates = read_candidate_table(item["candidate_table"])
    start = export["first_sample_column"]
    counted = [index for index, kind in enumerate(export["file_types"])
               if kind.strip().casefold() in {value.casefold() for value in MERGE_FILE_TYPES}]
    rows = []
    for raw in export["rows"]:
        identifier = raw[export["id_column"]].strip() if len(raw) > export["id_column"] else ""
        if not identifier:
            continue

        def cell(key: str) -> str:
            index = columns[key]
            return raw[index].strip() if 0 <= index < len(raw) else ""

        sample_cells = [raw[start + offset].strip() if start + offset < len(raw) else "" for offset in range(export["count"])]
        values = [parse_number(sample_cells[index]) or 0.0 for index in counted]
        representative = representative_candidate(candidates, identifier) or {}
        accepted = bool(representative.get("is_reference_matched"))
        # An accepted in-silico match is MS-DIAL's is_reference_matched, but never a reference match here.
        in_silico = accepted and is_in_silico_representative(
            representative, evidence_source=cell("evidence_source"), code=cell("code"),
            annotator=annotation_method(cell("comment")), in_silico_annotators=in_silico_annotators,
        )
        rows.append({
            "polarity": item["polarity"], "id": identifier, "raw": raw, "name": cell("name"), "adduct": cell("adduct"),
            "inchikey": cell("inchikey"), "comment": raw[columns["comment"]] if columns["comment"] < len(raw) else "",
            "method": annotation_method(cell("comment")),
            "total_score": parse_number(cell("total_score")) or 0.0,
            "rt": parse_number(cell("rt")),
            "rt_matched": cell("rt_matched").casefold() == "true",
            "ms2_matched": cell("ms2_matched").casefold() == "true",
            "matched": accepted and not in_silico,
            "in_silico": in_silico,
            "priority": int(parse_number(representative.get("priority", "")) or 0),
            "mean": sum(values) / len(values) if values else 0.0,
            "samples": sample_cells,
        })
    return {"export": export, "rows": rows, "candidates": candidates}


def _id_key(value: str) -> tuple[int, str]:
    number = parse_number(value)
    return (int(number), value) if number is not None else (1 << 62, value)


# Rows of one first block further apart than this are reported as possible isomers.
ISOMER_RT_SPREAD = 0.5
# Which row of one compound the merge keeps, and what one compound is: the person's choices, asked at the check.
DUPLICATE_RULES = ("evidence", "intensity", "keep_all")
INCHIKEY_MATCHES = ("first_block", "first_two_blocks")
_MEAN = "the larger mean normalized height over the Sample and QC injections"
RANKINGS = {
    "intensity": ["a preferred adduct", "matched on MS/MS (reference matched; an in-silico match is not)", _MEAN,
                  "the higher library priority", "the higher total score", "positive before negative",
                  "the lower alignment ID"],
    "evidence": ["matched on RT and MS/MS, then on MS/MS alone, then on RT alone (an MS/MS match against an "
                 "in-silico spectrum counts as no MS/MS match)", "the higher library priority",
                 "a preferred adduct", _MEAN, "the higher total score", "positive before negative", "the lower alignment ID"],
    "keep_all": ["every row is kept; groups of one compound are counted (duplicates_kept)"],
}


def compound_key(inchikey: str, match: str) -> str:
    """What one compound is by its InChIKey: the first block, or the first two (stereoisomers apart).

    A key without a second block is its first block either way, and groups only with keys like it.
    """
    block = inchikey_first_block(inchikey)
    if not block or match == "first_block":
        return block
    text = str(inchikey or "").strip()
    return text[:25] if len(text) >= 25 and text[14] == "-" else block


def evidence_level(row: dict[str, Any]) -> int:
    """How a row's annotation was matched: 3 RT and MS/MS, 2 MS/MS alone, 1 RT alone, 0 neither.

    An MS/MS match against an in-silico (predicted) spectrum is no MS/MS match against a reference.
    """
    ms2 = bool(row.get("ms2_matched", False)) and not row.get("in_silico", False)
    return {(True, True): 3, (False, True): 2, (True, False): 1}.get((bool(row.get("rt_matched", False)), ms2), 0)


def _pair_columns(read: dict[str, Any], order: list[str], sample_map: dict[str, dict[str, str]]) -> list[str]:
    """Each polarity's export columns named as the confirmed samples; the negative put in the positive's order."""
    names: dict[str, list[str]] = {}
    for polarity in order:
        mapping = {str(key).casefold(): value for key, value in (sample_map.get(polarity) or {}).items()}
        columns = read[polarity]["export"]["sample_names"]
        unknown = [column for column in columns if column.casefold() not in mapping]
        if unknown:
            raise LabInputError(
                f"The {polarity.lower()} matrix has columns the sample table does not name: {', '.join(unknown)}."
            )
        names[polarity] = [mapping[column.casefold()] for column in columns]
    if len(order) == 2:
        positive, negative = names[POSITIVE], names[NEGATIVE]
        if sorted(positive) != sorted(negative) or len(set(positive)) != len(positive):
            raise LabInputError(
                "The two polarities do not describe the same samples: positive "
                f"{', '.join(positive)}; negative {', '.join(negative)}."
            )
        position = {name: index for index, name in enumerate(negative)}
        permutation = [position[name] for name in positive]
        for row in read[NEGATIVE]["rows"]:
            row["samples"] = [row["samples"][index] for index in permutation]
        negative_types = [read[NEGATIVE]["export"]["file_types"][index] for index in permutation]
        for name, left, right in zip(positive, read[POSITIVE]["export"]["file_types"], negative_types):
            if left.strip() != right.strip():
                raise LabInputError(f"{name} is a {left} in positive and a {right} in negative.")
    return names[order[0]]


def _unconfirmed_report(
    spot: dict[str, Any], polarity: str, kept: list[dict[str, Any]], blocks_kept: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """An unconfirmed standard spot as the merge left it, and any kept row it shares a first block with."""
    row = next((item for item in kept if item["polarity"] == polarity and item["id"] == str(spot.get("alignment_id"))), None)
    sharing = []
    if row is not None and row["block"]:
        sharing = [{"polarity": other["polarity"], "alignment_id": other["id"], "name": other["name"]}
                   for other in blocks_kept.get(row["block"], []) if other is not row]
    return {**spot, "kept": row is not None, "shares_first_block_with": sharing}


def merge_metabolome(
    polarities: list[dict[str, Any]],
    folder: str | Path,
    *,
    salt: str | None,
    reference_matched_only: bool = True,
    preferred_adducts_only: bool = False,
    duplicate_rule: str = "intensity",
    inchikey_match: str = "first_block",
    sample_map: dict[str, dict[str, str]] | None = None,
    labels: dict[str, str] | None = None,
    in_silico_annotators: Collection[str] = (),
    write: bool = False,
) -> dict[str, Any]:
    """One metabolome from the normalized matrices of one or both polarities.

    A row whose representative annotation is an accepted in-silico (predicted-spectrum) match is never
    reference matched (is_in_silico_representative; in_silico_annotators are the annotator IDs of the
    libraries searched as predicted spectra): reference_matched_only drops it as "in-silico matched", counted
    apart from "not reference matched", and a merge without that option keeps it with In-silico matched TRUE
    and Reference matched FALSE. An LBM2's rule-based match stays a reference match.

    duplicate_rule decides which row of one compound is kept (DUPLICATE_RULES): "intensity", "evidence" (a
    library match with RT and MS/MS first, then MS/MS alone, then the library's priority), or "keep_all".
    inchikey_match says what one compound is: the InChIKey's first block, or its first two blocks (stereo
    isomers apart). sample_map gives, by polarity, each export column's sample name, which pairs the
    polarities when their file names differ; the negative columns are put in the positive's sample order.

    polarities: [{polarity, normalized, candidate_table, internal_standard_ids, internal_standard_annotator,
    unconfirmed_standard_spots}]. Rows dropped first: unannotated, and an internal standard (a spot decided
    as one, or one the internal-standard text row annotated); then, as the options say, rows not reference
    matched and adducts not preferred. A spot only searched for a standard nobody confirmed is kept as its
    own row, outside the InChIKey groups, and named in the report (unconfirmed_standard_spots, with any kept
    row it shares a first block with). Groups whose rows differ in stereo block or lie more than
    ISOMER_RT_SPREAD minutes apart are reported (possible_isomers). The matrices are the exports the run wrote, so no
    curation made later in the GUI reaches them; nothing here reads a Comment. Rows sharing an InChIKey's first
    block are one compound, within and across polarities; one is kept, by: a preferred adduct, matched on
    MS/MS, the larger mean normalized height over Sample and QC, then the higher library priority, the higher
    total score, positive before negative, and the lower alignment ID. A row without an InChIKey is kept as
    its own. labels maps an annotator ID to the library label the person knows.
    """
    folder = Path(folder)
    if duplicate_rule not in DUPLICATE_RULES:
        raise LabInputError(f"duplicate_rule '{duplicate_rule}' is none of {', '.join(DUPLICATE_RULES)}.")
    if inchikey_match not in INCHIKEY_MATCHES:
        raise LabInputError(f"inchikey_match '{inchikey_match}' is none of {', '.join(INCHIKEY_MATCHES)}.")
    identities = adduct_identities(preferred_adducts(salt))
    read = {item["polarity"]: {**_read_polarity(item, in_silico_annotators), "input": item} for item in polarities}
    order = [polarity for polarity in POLARITIES if polarity in read]
    if not order:
        raise LabInputError("No polarity to merge.")
    if sample_map is not None:
        sample_names = _pair_columns(read, order, sample_map)
    else:
        if len(order) == 2:
            compared = compare_sample_order(read[POSITIVE]["export"], read[NEGATIVE]["export"])
            if compared.get("problems"):
                raise LabInputError("The two polarities do not describe the same samples: " + " ".join(compared["problems"]))
        sample_names = [strip_polarity(name) if len(order) == 2 else name for name in read[order[0]]["export"]["sample_names"]]
    first = read[order[0]]["export"]
    metadata_titles = first["header"][4][: first["first_sample_column"]]
    removed: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    counts: dict[str, dict[str, int]] = {polarity: {} for polarity in order}

    def drop(row: dict[str, Any], reason: str, kept: dict[str, Any] | None = None) -> None:
        removed.append({"row": row, "reason": reason, "kept": kept})
        counts[row["polarity"]][reason] = counts[row["polarity"]].get(reason, 0) + 1

    for polarity in order:
        item = read[polarity]
        standards = {str(value) for value in item["input"].get("internal_standard_ids", [])}
        annotator = str(item["input"].get("internal_standard_annotator") or "")
        unconfirmed = {str(spot.get("alignment_id")) for spot in item["input"].get("unconfirmed_standard_spots", [])}
        counts[polarity]["rows"] = len(item["rows"])
        for row in item["rows"]:
            row["unconfirmed_standard"] = row["id"] in unconfirmed
            if row["name"].casefold() in UNANNOTATED_NAMES:
                drop(row, "unannotated")
            elif row["id"] in standards or (annotator and row["method"] == annotator):
                drop(row, "internal standard")
            elif reference_matched_only and row["in_silico"]:
                # Accepted by MS-DIAL, but against a predicted spectrum: never an identification.
                drop(row, IN_SILICO_REASON)
            elif reference_matched_only and not row["matched"]:
                drop(row, "not reference matched")
            elif preferred_adducts_only and not is_preferred(row["adduct"], identities):
                drop(row, "not a preferred adduct")
            else:
                row["preferred"] = is_preferred(row["adduct"], identities)
                row["block"] = inchikey_first_block(row["inchikey"])
                row["key"] = compound_key(row["inchikey"], inchikey_match)
                row["evidence"] = evidence_level(row)
                candidates.append(row)

    def rank(row: dict[str, Any]) -> tuple[Any, ...]:
        if duplicate_rule == "evidence":
            # The more reliable library match first: RT and MS/MS, then MS/MS alone, then the library's priority.
            return (-row["evidence"], -row["priority"], not row["preferred"], -row["mean"], -row["total_score"],
                    order.index(row["polarity"]), _id_key(row["id"]))
        return (not row["preferred"], not row["matched"], -row["mean"], -row["priority"], -row["total_score"],
                order.index(row["polarity"]), _id_key(row["id"]))

    groups: dict[str, list[dict[str, Any]]] = {}
    kept: list[dict[str, Any]] = []
    for row in candidates:
        # A spot only searched for a standard nobody confirmed may be the labelled standard itself, which shares
        # the endogenous compound's first block: it is kept as its own row and never removes another.
        if row["key"] and not row["unconfirmed_standard"]:
            groups.setdefault(row["key"], []).append(row)
        else:
            kept.append(row)
    duplicates = {"within_polarity": 0, "across_polarities": 0}
    duplicates_kept = 0
    # Rows one compound key joins that may be different compounds: other stereo blocks, or far apart in RT.
    possible_isomers: list[dict[str, Any]] = []
    reason = "duplicate InChIKey " + ("first block" if inchikey_match == "first_block" else "first two blocks")
    for block, rows in groups.items():
        ranked = sorted(rows, key=rank)
        winner = ranked[0]
        if duplicate_rule == "keep_all":
            kept.extend(ranked)
            duplicates_kept += len(ranked) - 1
        else:
            kept.append(winner)
            for other in ranked[1:]:
                duplicates["across_polarities" if other["polarity"] != winner["polarity"] else "within_polarity"] += 1
                drop(other, reason, winner)
        if len(ranked) > 1:
            second = {row["inchikey"].strip()[15:25] for row in ranked if len(row["inchikey"].strip()) >= 25}
            times = [row["rt"] for row in ranked if row["rt"] is not None]
            spread = max(times) - min(times) if times else 0.0
            if len(second) > 1 or spread > ISOMER_RT_SPREAD:
                possible_isomers.append({
                    "compound_key": block, "rt_spread_min": round(spread, 3), "stereo_blocks": sorted(second),
                    "kept": {"polarity": winner["polarity"], "alignment_id": winner["id"]},
                    "rows": [{"polarity": row["polarity"], "alignment_id": row["id"], "name": row["name"],
                              "adduct": row["adduct"], "rt": row["rt"], "inchikey": row["inchikey"],
                              "library": (labels or {}).get(row["method"], row["method"])} for row in ranked],
                })
    kept.sort(key=lambda row: (order.index(row["polarity"]), _id_key(row["id"])))
    blocks_kept = {}
    for row in kept:
        if row["block"]:
            blocks_kept.setdefault((row["block"]), []).append(row)

    label_of = dict(labels or {})
    header = ["Polarity", "Alignment ID", "InChIKey first block", "Library", "Reference matched",
              IN_SILICO_MATCHED_COLUMN, "Preferred adduct",
              *[title for title in metadata_titles if title != "Alignment ID"], *sample_names]
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(header)
    for row in kept:
        source = read[row["polarity"]]["export"]
        titles = source["header"][4]
        by_title = {title: (row["raw"][index] if index < len(row["raw"]) else "") for index, title in enumerate(titles)}
        writer.writerow([
            row["polarity"], row["id"], row["block"], label_of.get(row["method"], row["method"]),
            "TRUE" if row["matched"] else "FALSE", "TRUE" if row["in_silico"] else "FALSE",
            "TRUE" if row["preferred"] else "FALSE",
            *[by_title.get(title, "") for title in metadata_titles if title != "Alignment ID"],
            *row["samples"],
        ])
    metabolome = buffer.getvalue().encode("utf-8")

    removed_lines = ["\t".join(REMOVED_COLUMNS)]

    def text(value: Any) -> str:
        return "" if value is None else str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")

    for item in removed:
        row, winner = item["row"], item["kept"] or {}
        removed_lines.append("\t".join(text(value) for value in (
            row["polarity"], row["id"], row["name"], row["adduct"], row["inchikey"], item["reason"],
            winner.get("polarity"), winner.get("id"), winner.get("name"), winner.get("adduct"),
        )))
    removed_data = ("\r\n".join(removed_lines) + "\r\n").encode("utf-8")

    samples = io.StringIO()
    sample_writer = csv.writer(samples, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    sample_writer.writerow(["Sample", "Class", "File type", "Injection order", "Batch ID"])
    injection = first["header"][2][first["first_sample_column"]:]
    batch = first["header"][3][first["first_sample_column"]:]
    for offset, name in enumerate(sample_names):
        sample_writer.writerow([name, first["sample_classes"][offset], first["file_types"][offset],
                                injection[offset] if offset < len(injection) else "",
                                batch[offset] if offset < len(batch) else ""])
    samples_data = samples.getvalue().encode("utf-8")

    outputs = {}
    for key, name, data in (("metabolome", METABOLOME_NAME, metabolome), ("removed", REMOVED_NAME, removed_data),
                            ("samples", SAMPLES_NAME, samples_data)):
        path = folder / name
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        outputs[key] = {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    kept_counts = {polarity: sum(1 for row in kept if row["polarity"] == polarity) for polarity in order}
    report = {
        "polarities": order,
        "inputs": {
            polarity: {
                "normalized": str(Path(read[polarity]["input"]["normalized"]).resolve()),
                "normalized_sha256": file_sha256(read[polarity]["input"]["normalized"]),
                "candidate_table": read[polarity]["candidates"]["path"],
                "candidate_table_sha256": read[polarity]["candidates"]["sha256"],
                "internal_standard_ids": sorted(str(value) for value in read[polarity]["input"].get("internal_standard_ids", [])),
            }
            for polarity in order
        },
        "options": {"reference_matched_only": reference_matched_only, "preferred_adducts_only": preferred_adducts_only,
                    "duplicate_rule": duplicate_rule, "inchikey_match": inchikey_match},
        "preferred_adducts": preferred_adducts(salt),
        "salt": salt,
        "ranking": RANKINGS[duplicate_rule],
        "counts": {
            "by_polarity": counts,
            "duplicates_removed": duplicates,
            "duplicates_kept": duplicates_kept,
            "kept": len(kept),
            "kept_by_polarity": kept_counts,
            "kept_without_inchikey": sum(1 for row in kept if not row["block"]),
            "kept_unconfirmed_standard_spots": sum(1 for row in kept if row["unconfirmed_standard"]),
            "possible_isomer_groups": len(possible_isomers),
            "removed": len(removed),
            # Rows an accepted in-silico match represents: dropped by reference_matched_only, kept without it.
            "in_silico_matched_removed": sum(1 for item in removed if item["reason"] == IN_SILICO_REASON),
            "in_silico_matched_kept": sum(1 for row in kept if row["in_silico"]),
        },
        "reference_match": REFERENCE_MATCH_DEFINITION,
        "in_silico_annotators": sorted(set(in_silico_annotators)),
        "samples": sample_names,
        "unconfirmed_standard_spots": {
            polarity: [_unconfirmed_report(spot, polarity, kept, blocks_kept)
                       for spot in read[polarity]["input"].get("unconfirmed_standard_spots", [])]
            for polarity in order if read[polarity]["input"].get("unconfirmed_standard_spots")
        },
        "possible_isomers": possible_isomers,
        "outputs": outputs,
    }
    if write:
        target = folder / MERGE_REPORT_NAME
        target.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        report["report"] = str(target.resolve())
    return report

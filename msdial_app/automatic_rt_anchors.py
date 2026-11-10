"""Named anchors for the automatic alignment RT correction (MsdialWorkbench#845).

The Console's automatic correction can take its anchors from an anchor library instead of, or as well
as, the detected peaks: the anchor mode is Automatic, UserOnly or Hybrid. It can also stop once it has
chosen its anchors and written its audit ("preview only"), so that a person reviews the anchors before
the run that aligns. This module holds what Interactive needs on its side of that contract:

- the anchor library read and checked as strictly as AutomaticRtCorrectionAnchorLibrary.Parse and
  Check read it, because in Hybrid mode a False row is an exclusion, the opposite of a True row, and a
  library the Console refuses must be refused before a Console starts;
- the library written back in the one format the Console reads (seven tab-separated ASCII columns
  under the header, a decimal point, True or False);
- the approval: from a preview's audit and a person's explicit accepted and rejected lists, the
  production anchor library and a record of who approved it, from which preview, and what was written.

The approval is a person's decision. Nothing here accepts or rejects an anchor on its own: every anchor
the preview shows must be named in exactly one of the two lists.

The decisions the author took on 2026-11-07 are implemented as follows:

- D1: an accepted automatic anchor becomes a True row at the reference file's observed RT and m/z, with
  the automatic correction's match RT tolerance, the MS1 tolerance it matched peaks with (MS1 tolerance
  for alignment), and a minimum height of 0;
- D2: the production run defaults to UserOnly with the approved library; with fewer anchors than the
  minimum anchor count UserOnly is refused, and the person has to choose Hybrid explicitly. The anchors
  are counted as the Console counts them (decision 4, 2026-11-10): the distinct anchors detected in the
  preview's reference file. An accepted anchor the preview showed as missing or as a duplicate there, or
  one within two MS1 scans of an anchor already counted, does not count;
- D3: a rejected user anchor stays in the library as a False row and is never deleted; an accepted one
  keeps its own row as it was;
- a rejected automatic anchor is written as a False row too, at the same values an accepted one would
  get: UserOnly ignores it, and in Hybrid it keeps that candidate out of the automatic selection, which
  is what a False row is for ("a candidate rejected on review is not chosen again").
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import struct
import threading
from pathlib import Path
from typing import Any, Iterable

ANCHOR_MODES = ("Automatic", "UserOnly", "Hybrid")
PEAK_SELECTION_MODES = ("HighestIntensity", "ClosestToReferenceRt", "Weighted")
LIBRARY_COLUMNS = ("Name", "RT(min)", "RT tol.(min)", "m/z", "m/z tol.", "Minimum height", "T/F")
LIBRARY_HEADER = "\t".join(LIBRARY_COLUMNS)
# The three audit files the Console writes (AutomaticAlignmentRetentionTimeCorrectionResult); the third
# only in Hybrid mode, when the library has False rows.
SUMMARY = "automatic_alignment_rt_correction_summary.tsv"
ANCHORS = "automatic_alignment_rt_correction_anchors.tsv"
EXCLUSIONS = "automatic_alignment_rt_correction_exclusions.tsv"
# What a preview job leaves in its own directory beside the Console's audit: the settings the approval
# needs (the tolerances an accepted automatic anchor takes, the library the preview read, where the
# production run writes), so that the approval does not depend on a backend still holding the job.
PREVIEW_RECORD = "automatic-rt-preview.json"
APPROVAL_SUFFIX = ".approval.json"
APPROVED_LIBRARY_STEM = "automatic_rt_anchors_approved"
APPROVAL_SCHEMA = "msdial-interactive/automatic-rt-anchor-approval/1"
PREVIEW_SCHEMA = "msdial-interactive/automatic-rt-preview/1"
# The preview job's end, written into the preview record by the backend that ran it: "completed",
# "failed" or "cancelled". An approval needs "completed" when the record says.
PREVIEW_JOB_STATUS = "job_status"
# AutomaticAlignmentRetentionTimeCorrection.CoElutionScans: two peak tops within this many MS1 scan steps
# of each other in the reference file are one anchor. The Console counts scan indices, each RT taken to
# its nearest scan; in minutes that is up to CoElutionScans + 0.5 MS1 cycles apart.
CO_ELUTION_SCANS = 2
# A reference-file status of an anchor that found no single peak there: it carries no RT, so the Console
# counts it nowhere.
NOT_IN_REFERENCE_STATUSES = frozenset({"Missing", "MissingInReference", "Ambiguous"})
# A user anchor whose reference-file peak lies within CoElutionScans scans of an earlier row's: it is the
# same anchor, reported as Duplicate in every file and counted once.
DUPLICATE_STATUS = "Duplicate"
_APPROVAL_LOCK = threading.Lock()

# What double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture) accepts once the text is
# trimmed: a sign, digits with a decimal point, an exponent. No thousands separator, no decimal comma.
_INVARIANT_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)
_LINE_BREAK = re.compile(r"\r\n|\n|\r")


class AnchorLibraryError(ValueError):
    """A library the Console would refuse, with the line or row at fault."""


def canonical_anchor_mode(value: Any) -> str | None:
    """The anchor mode as the Console reads it, or None when it reads none.

    ConfigParser.AnchorMode drops underscores and spaces and takes the enum names in any letter case,
    so "user_only" and "user only" are UserOnly. A number is not a name, so it is not read.
    """
    text = str(value if value is not None else "").replace("_", "").replace(" ", "").strip()
    return next((name for name in ANCHOR_MODES if name.casefold() == text.casefold()), None)


def canonical_peak_selection_mode(value: Any) -> str | None:
    text = str(value if value is not None else "").strip()
    return next((name for name in PEAK_SELECTION_MODES if name.casefold() == text.casefold()), None)


def _as_float32(value: float) -> float:
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def _number(text: str, what: str, line: int) -> float:
    trimmed = text.strip()
    if _INVARIANT_NUMBER.fullmatch(trimmed):
        value = float(trimmed)
        if math.isfinite(value):
            return value
    raise AnchorLibraryError(
        f"Anchor library line {line}: '{text}' is not a number for {what}; write it with a decimal point."
    )


def _flag(text: str, line: int) -> bool:
    value = text.strip().casefold()
    if value in {"true", "t"}:
        return True
    if value in {"false", "f"}:
        return False
    raise AnchorLibraryError(
        f"Anchor library line {line}: the T/F column says '{text}'; write True (an anchor) or False (an exclusion)."
    )


def parse_anchor_library(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Every row of a library's lines, True and False, in file order; as the Console's Parse reads them.

    The first line that is not blank is the header, and one whose second column is a number is refused
    as a row. Every other line that is not blank needs all seven columns, numbers with a decimal point,
    and True, False, T or F in the seventh column. Columns past the seventh are ignored, as they are by
    the Console. The float-typed columns (both tolerances and the minimum height) are compared as the
    Console stores them, as single-precision numbers.
    """
    numbered = list(enumerate(lines, start=1))
    header = next(((number, text) for number, text in numbered if text.strip()), None)
    if header is None:
        raise AnchorLibraryError("The anchor library is empty.")
    header_cells = header[1].split("\t")
    if len(header_cells) >= 2 and _INVARIANT_NUMBER.fullmatch(header_cells[1].strip()):
        raise AnchorLibraryError(
            f"Anchor library line {header[0]} looks like a row, not the header. The first line names the "
            "columns: " + ", ".join(LIBRARY_COLUMNS) + "."
        )
    rows: list[dict[str, Any]] = []
    for number, text in numbered[header[0]:]:
        if not text.strip():
            continue
        cells = text.split("\t")
        if len(cells) < 7:
            raise AnchorLibraryError(
                f"Anchor library line {number} has {len(cells)} column(s); each row needs seven: "
                + ", ".join(LIBRARY_COLUMNS) + "."
            )
        rows.append(
            {
                "row": len(rows) + 1,
                "line": number,
                "name": cells[0].strip(),
                "rt": _number(cells[1], "RT", number),
                "rt_tolerance": _number(cells[2], "RT tolerance", number),
                "mz": _number(cells[3], "m/z", number),
                "mz_tolerance": _number(cells[4], "m/z tolerance", number),
                "minimum_height": _number(cells[5], "minimum height", number),
                "include": _flag(cells[6], number),
            }
        )
    return rows


def decode_as_dotnet(data: bytes) -> str:
    """Text as File.ReadAllLines(path, Encoding.UTF8) decodes it: a BOM names the encoding, else UTF-8.

    That overload opens a StreamReader that detects a byte order mark: UTF-32 (little or big endian),
    UTF-16 (little or big endian) and UTF-8. Excel's "Unicode Text (*.txt)" is tab-separated UTF-16LE with a
    BOM, which the Console reads. Without a BOM the bytes are UTF-8, an invalid sequence read as U+FFFD.
    """
    for bom, codec in (
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
        (b"\xef\xbb\xbf", "utf-8"),
    ):
        if data.startswith(bom):
            return data[len(bom):].decode(codec, errors="replace")
    return data.decode("utf-8", errors="replace")


def method_file_readable_path(path: str | Path) -> bool:
    """Whether the LC-MS Console reads this path back from a method file exactly as it was written.

    ConfigParser.ReadForLcmsParameter reads method.txt as ASCII: every byte above 0x7F comes back as '?',
    and on .NET Framework Path.GetFullPath then refuses the '?'. Printable ASCII is read back as written.
    """
    return all(" " <= character <= "~" for character in str(path))


def unreadable_path_problem(path: str | Path, what: str) -> str:
    return (
        f"{what} {path} is not printable ASCII. The LC-MS MS-DIAL Console reads the method file as ASCII, "
        "so every other character comes back as '?' and the path is not found. Move it to a folder whose "
        "path is ASCII (a Japanese folder name, for one, romanized)."
    )


def read_anchor_library(path: str | Path) -> list[dict[str, Any]]:
    """A library file's rows (parse_anchor_library), read as File.ReadAllLines reads it (decode_as_dotnet)."""
    if not str(path or "").strip():
        raise AnchorLibraryError(
            "Automatic RT correction anchor library file path is required when the anchor mode is UserOnly or Hybrid."
        )
    resource = Path(path).expanduser()
    if not resource.is_file():
        raise FileNotFoundError(f"The automatic RT correction anchor library was not found: {resource}")
    text = decode_as_dotnet(resource.read_bytes())
    lines = _LINE_BREAK.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    return parse_anchor_library(lines)


def anchor_library_issues(rows: list[dict[str, Any]], mode: str, minimum_anchor_count: int) -> list[str]:
    """What the Console's Check refuses in a library for an anchor mode; empty when it accepts it.

    UserOnly needs at least the minimum anchor count of True rows and Hybrid at least one row. Every row
    the mode reads needs a positive m/z, m/z tolerance and RT tolerance and an RT of 0 or more, and a
    True row a minimum height of 0 or more. UserOnly does not read its False rows. Automatic mode reads
    no library.
    """
    issues: list[str] = []
    if mode == "Automatic":
        return issues
    if mode == "UserOnly":
        anchors = sum(1 for row in rows if row["include"])
        if anchors < minimum_anchor_count:
            issues.append(
                f"The anchor library has {anchors} True row(s); UserOnly needs at least the minimum anchor "
                f"count, {minimum_anchor_count}. Accept more anchors, lower the minimum, or choose Hybrid "
                "explicitly so that automatic anchors fill the RT bins the True rows leave empty."
            )
            return issues
    elif mode == "Hybrid":
        if not rows:
            issues.append("The anchor library has no rows; Hybrid needs at least one.")
            return issues
    else:
        issues.append(f"Unknown automatic RT correction anchor mode {mode!r}.")
        return issues
    for row in rows:
        if not row["include"] and mode == "UserOnly":
            continue
        what = f"Anchor library row {row['row']} ({row['name']})"
        if not row["mz"] > 0:
            issues.append(f"{what}: m/z must be positive.")
        elif not _as_float32(row["mz_tolerance"]) > 0:
            issues.append(f"{what}: m/z tolerance must be positive.")
        elif not _as_float32(row["rt_tolerance"]) > 0:
            issues.append(f"{what}: RT tolerance must be positive.")
        elif not row["rt"] >= 0:
            issues.append(f"{what}: RT must be 0 or more.")
        elif row["include"] and not _as_float32(row["minimum_height"]) >= 0:
            issues.append(f"{what}: minimum height must be 0 or more.")
        if issues:
            # The Console stops at the first row it refuses.
            break
    return issues


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _cell_number(value: Any, what: str, index: int) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AnchorLibraryError(f"Anchor row {index}: {what} {value!r} is not a number.") from None
    if not math.isfinite(number):
        raise AnchorLibraryError(f"Anchor row {index}: {what} must be a finite number.")
    # The shortest text that reads back as the same double, with a decimal point and never a comma.
    return repr(number)


def anchor_library_text(rows: list[dict[str, Any]]) -> str:
    """The library as the Console reads it: the header, then one seven-column row per entry.

    Written as UTF-8 without a BOM (write_anchor_library), which the Console's File.ReadAllLines reads: a
    name is kept as the Console keeps it (Parse takes cells[0].Trim(), whatever it holds), so a library the
    Console and the preview read, a non-ASCII or an empty name included, can be approved. Only a tab or a
    line break, which no row the Console reads can hold, is refused.
    """
    lines = [LIBRARY_HEADER]
    for index, row in enumerate(rows, start=1):
        name = str(row.get("name", "")).strip()
        if any(character in name for character in "\t\r\n"):
            raise AnchorLibraryError(f"Anchor row {index}: a name cannot hold a tab or a line break.")
        include = row.get("include")
        if not isinstance(include, bool):
            include = str(include).strip().casefold() in {"true", "t"}
        lines.append(
            "\t".join(
                [
                    name,
                    _cell_number(row.get("rt"), "RT", index),
                    _cell_number(row.get("rt_tolerance"), "RT tolerance", index),
                    _cell_number(row.get("mz"), "m/z", index),
                    _cell_number(row.get("mz_tolerance"), "m/z tolerance", index),
                    _cell_number(row.get("minimum_height"), "minimum height", index),
                    "True" if include else "False",
                ]
            )
        )
    return "\n".join(lines) + "\n"


def _checked_library_text(rows: list[dict[str, Any]]) -> str:
    text = anchor_library_text(rows)
    reread = parse_anchor_library(_LINE_BREAK.split(text)[:-1])
    if len(reread) != len(rows):
        raise AnchorLibraryError("The anchor library did not read back as written.")
    return text


def write_anchor_library(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    """Write a library and read it back with the strict reader, so what is written is what the Console reads."""
    target = Path(path)
    text = _checked_library_text(rows)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return target


def _library_candidates(directory: str | Path, stem: str, now: dt.datetime | None = None) -> Iterable[Path]:
    folder = Path(directory).expanduser().resolve()
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._") or APPROVED_LIBRARY_STEM
    yield folder / f"{clean}_{stamp}.txt"
    suffix = 2
    while True:
        yield folder / f"{clean}_{stamp}_{suffix}.txt"
        suffix += 1


def timestamped_library_path(directory: str | Path, stem: str, now: dt.datetime | None = None) -> Path:
    """The first timestamped name not yet taken; create_timestamped_library claims one atomically."""
    return next(path for path in _library_candidates(directory, stem, now) if not path.exists())


def create_timestamped_library(
    directory: str | Path, stem: str, rows: list[dict[str, Any]], now: dt.datetime | None = None
) -> tuple[Path, str]:
    """Write a library under a timestamped name no other writer holds; its path and the text's SHA-256.

    Each name is created exclusively, so two writers in the same second (two browser tabs, the page and an
    agent) never write one file: the second takes the next suffix. The SHA-256 is of the text written, not
    of the file read back, so it can never describe another writer's library.
    """
    text = _checked_library_text(rows)
    data = text.encode("utf-8")
    Path(directory).expanduser().resolve().mkdir(parents=True, exist_ok=True)
    for path in _library_candidates(directory, stem, now):
        try:
            with path.open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            continue
        return path, hashlib.sha256(data).hexdigest()
    raise AssertionError("unreachable")


def approval_record_path(library_path: str | Path) -> Path:
    library = Path(library_path)
    return library.with_name(library.name + APPROVAL_SUFFIX)


def approval_for_library(library_path: str | Path) -> dict[str, Any] | None:
    """The approval record written beside a library, when it describes the library as it is now.

    A record whose library SHA-256 is not the file's describes another version of it: the library was
    edited after it was approved, and the approval no longer covers it. That record comes back with
    matches_library False, for the caller to say so rather than claim an approval.
    """
    text = str(library_path or "").strip()
    if not text:
        return None
    library = Path(text).expanduser()
    record_path = approval_record_path(library)
    if not library.is_file() or not record_path.is_file():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != APPROVAL_SCHEMA:
        return None
    current = file_sha256(library)
    return {
        **record,
        "approval_record": str(record_path),
        "matches_library": str(record.get("library_sha256") or "").casefold() == current,
        "current_library_sha256": current,
    }


def write_preview_record(directory: str | Path, record: dict[str, Any]) -> Path:
    path = Path(directory) / PREVIEW_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": PREVIEW_SCHEMA, **record}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_preview_record(directory: str | Path) -> dict[str, Any]:
    path = Path(directory) / PREVIEW_RECORD
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{path.parent} holds no {PREVIEW_RECORD}; it is not the directory of an anchor preview "
            "started by MS-DIAL Interactive. Run the preview again."
        ) from None
    if not isinstance(record, dict) or record.get("schema") != PREVIEW_SCHEMA:
        raise ValueError(f"{path} is not an MS-DIAL Interactive anchor preview record.")
    return record


def mark_preview_job(directory: str | Path, status: str, exit_code: Any = None, error: str = "") -> None:
    """Record how the preview job ended in its preview record, so an approval named by directory sees it.

    The record is written before the Console starts; a failed or cancelled preview leaves a partial audit
    beside it that says Run = Preview, as a completed one does. Best effort: a record that cannot be
    rewritten leaves the approval's own checks on the audit (no reference file, "Not corrected" notes).
    """
    path = Path(directory) / PREVIEW_RECORD
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return
    if not isinstance(record, dict) or record.get("schema") != PREVIEW_SCHEMA:
        return
    record[PREVIEW_JOB_STATUS] = str(status)
    record["job_exit_code"] = exit_code
    record["job_error"] = str(error or "")
    record["job_finished_at"] = dt.datetime.now().astimezone().isoformat()
    try:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def distinct_reference_anchors(
    anchors: list[dict[str, Any]], keys_in_library_order: Iterable[str], scan_interval: float | None = None
) -> dict[str, Any]:
    """The accepted anchors the Console counts in the preview's reference file, in the approved library's order.

    As SelectUserReference and DistinctUserAnchorsDetected count them: a row with no peak in the reference
    file (Missing, MissingInReference, Ambiguous) counts for nothing; a row the preview showed as Duplicate
    there took an earlier row's peak; and a row whose reference RT lies within CoElutionScans MS1 scans of a
    row already counted is that row's anchor (in UserOnly an automatic anchor and its isotope, two adducts of
    one standard). The scans are judged from the MS1 cycle the audit records at each anchor, or the reference
    file's estimated scan interval: a peak within CoElutionScans + 0.5 cycles of a counted one shares its
    nearest scan window. ``anchors`` are anchor_review entries; ``keys_in_library_order`` the accepted keys.
    """
    by_key = {item["key"]: item for item in anchors}
    counted: list[dict[str, Any]] = []
    not_counted: list[dict[str, str]] = []
    for key in keys_in_library_order:
        item = by_key.get(key)
        if item is None:
            continue
        status = str(item.get("reference_status") or "")
        label = _anchor_label(item)
        rt = item.get("reference_rt") if status not in NOT_IN_REFERENCE_STATUSES else None
        if status == DUPLICATE_STATUS:
            not_counted.append({"key": key, "reason": f"{label} was a Duplicate in the preview's reference file: "
                                "its peak was another row's, so the Console counts it once with that row."})
            continue
        if rt is None or not math.isfinite(float(rt)):
            not_counted.append({"key": key, "reason": f"{label} was not detected in the preview's reference file "
                                f"({status or 'no reference RT'}), so the Console counts it nowhere."})
            continue
        cycle = item.get("reference_cycle") or scan_interval
        same = next(
            (
                other for other in counted
                if _same_scan(float(rt), float(other["rt"]), max(filter(None, (cycle, other["cycle"], scan_interval)), default=None))
            ),
            None,
        )
        if same is not None:
            not_counted.append({"key": key, "reason": f"{label} lies within {CO_ELUTION_SCANS} MS1 scans of "
                                f"{same['label']} in the preview's reference file (RT {float(rt):.4f} and "
                                f"{float(same['rt']):.4f} min); the Console counts them as one anchor."})
            continue
        counted.append({"key": key, "rt": float(rt), "cycle": cycle, "label": label})
    return {"count": len(counted), "counted": [item["key"] for item in counted], "not_counted": not_counted}


def _same_scan(rt: float, other: float, cycle: float | None) -> bool:
    if not cycle or not math.isfinite(cycle) or cycle <= 0:
        return abs(rt - other) <= 1e-9
    return abs(rt - other) <= (CO_ELUTION_SCANS + 0.5) * cycle + 1e-9


def _anchor_label(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    return f"{item['key']}" + (f" ({name})" if name else "")


def _matches(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= 1e-9 * max(1.0, abs(left), abs(right))


def _audit_checksums(directory: Path) -> dict[str, str]:
    return {
        name: file_sha256(directory / name)
        for name in (SUMMARY, ANCHORS, EXCLUSIONS)
        if (directory / name).is_file()
    }


def automatic_anchor_name(anchor: dict[str, Any]) -> str:
    """A True or False row's name for an automatic anchor, which the Console leaves unnamed."""
    return f"auto{anchor['anchor_id']}_mz{anchor['mz']:.4f}_rt{anchor['reference_rt']:.3f}"


def preview_failure(record: dict[str, Any], review: dict[str, Any]) -> str:
    """Why a preview's audit cannot be approved, or "" when it can.

    A preview that failed or was cancelled leaves a partial audit marked Run = Preview, as a completed one
    does (LcmsProcess writes AutomaticRtCorrectionFailedException.PartialAudit with the preview's run). It
    chose no reference file and no anchors, so approving it would write a library and an approval record
    that claim a review of anchors nobody was shown. The preview's method-key evidence must also show a
    preview that ran with its settings: a setting the Console listed as unusable or unrecognised was not the
    one the anchors were chosen under.
    """
    rerun = " Run the preview again and approve the anchors of one that completed."
    status = str(record.get(PREVIEW_JOB_STATUS) or "").strip()
    if status and status != "completed":
        error = str(record.get("job_error") or "").strip()
        return (
            f"The anchor preview {record.get('job_id') or ''} ended {status}"
            + (f" ({error})" if error else "")
            + "; its audit is a partial one." + rerun
        )
    not_corrected = [
        str(item.get("note") or "").strip()
        for item in review.get("files") or []
        if str(item.get("note") or "").strip().startswith("Not corrected:")
    ]
    if review.get("reference") is None or not_corrected:
        reason = not_corrected[0] if not_corrected else "no summary row has Model source Reference"
        return (
            "The preview's audit is the partial audit of a preview that failed before it chose its reference "
            f"file and anchors ({reason})." + rerun
        )
    method_audit = review.get("method_audit") or {}
    if method_audit.get("status") != "preview_only":
        return (
            "The preview's method-key evidence does not show a preview that ran with its settings "
            f"({method_audit.get('reason') or method_audit.get('status') or 'no method-key record'}); the "
            "anchors were not chosen under the settings the method file names." + rerun
        )
    return ""


def approve_anchors(
    preview_directory: str | Path,
    accepted: Iterable[str],
    rejected: Iterable[str],
    confirmed_by: str = "",
    *,
    write: bool = False,
    output_root: str | Path | None = None,
    preview_job_id: str = "",
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """The production anchor library from a preview and a person's decisions; written only when ``write``.

    ``accepted`` and ``rejected`` hold the review keys of read_automatic_rt_review's anchor_review
    (Source-AnchorID, e.g. "Automatic-3", "User-1"). Every anchor of the preview must be in exactly one
    of them. Without ``write`` nothing is written and the rows are returned for the person to see; with
    it, ``confirmed_by`` must name the person who approved, the library is written into the run's output
    root with a timestamped name, and the approval record beside it.
    """
    from .automatic_rt_review import read_automatic_rt_review

    directory = Path(preview_directory).expanduser().resolve()
    record = read_preview_record(directory)
    review = read_automatic_rt_review(directory)
    anchor_review = review.get("anchor_review") or {}
    if anchor_review.get("run") != "Preview":
        raise ValueError(
            f"The audit in {directory} is not a preview's (its Run column says "
            f"{anchor_review.get('run') or 'nothing'}); anchors are approved from a preview."
        )
    failure = preview_failure(record, review)
    if failure:
        raise ValueError(failure)
    root_text = str(output_root or record.get("output_root") or "").strip()
    if root_text and not method_file_readable_path(Path(root_text).expanduser().resolve()):
        raise ValueError(
            unreadable_path_problem(
                Path(root_text).expanduser().resolve(),
                "The output root, where the approved anchor library would be written,",
            )
        )
    anchors = anchor_review.get("anchors") or []
    keys = [item["key"] for item in anchors]
    accepted_keys = [str(item).strip() for item in accepted]
    rejected_keys = [str(item).strip() for item in rejected]
    both = sorted(set(accepted_keys) & set(rejected_keys))
    unknown = sorted((set(accepted_keys) | set(rejected_keys)) - set(keys))
    undecided = [key for key in keys if key not in set(accepted_keys) | set(rejected_keys)]
    problems = []
    if both:
        problems.append("accepted and also rejected: " + ", ".join(both))
    if unknown:
        problems.append("not anchors of this preview: " + ", ".join(unknown))
    if undecided:
        problems.append("neither accepted nor rejected: " + ", ".join(undecided))
    if problems:
        raise ValueError(
            "Every anchor of the preview must be accepted or rejected by the person reviewing it, once; "
            + "; ".join(problems) + "."
        )

    match_rt_tolerance = float(record["match_rt_tolerance"])
    ms1_tolerance = float(record["alignment_ms1_tolerance"])
    minimum = int(record["minimum_anchors"])
    preview_mode = str(record.get("anchor_mode") or "Automatic")
    library_rows: list[dict[str, Any]] = []
    if preview_mode != "Automatic":
        library_path = str(record.get("anchor_library_path") or "")
        expected = str(record.get("anchor_library_sha256") or "").casefold()
        current = file_sha256(library_path) if Path(library_path).is_file() else ""
        audited = str(anchor_review.get("anchor_library_sha256") or "").casefold()
        if not current or current != expected or (audited and audited != expected):
            raise ValueError(
                f"The anchor library the preview read ({library_path}) is missing or has changed since the "
                "preview, so its user anchors cannot be carried forward. Run the preview again."
            )
        library_rows = read_anchor_library(library_path)

    decision = {key: True for key in accepted_keys}
    decision.update({key: False for key in rejected_keys})
    rows: list[dict[str, Any]] = []
    user_anchors = [item for item in anchors if item["source"] == "User"]
    claimed: set[str] = set()
    for row in library_rows:
        if not row["include"]:
            # An exclusion the person already set; it is theirs, and stays.
            rows.append({**row, "origin": "library_false_row"})
            continue
        match = next(
            (
                item for item in user_anchors
                if item["key"] not in claimed
                and item["name"] == row["name"]
                and _matches(item["library_rt"], row["rt"])
                and _matches(item["mz"], row["mz"])
            ),
            None,
        )
        if match is None:
            raise ValueError(
                f"Anchor library row {row['row']} ({row['name']}) is a True row the preview's audit does not "
                "show; run the preview again with this library."
            )
        claimed.add(match["key"])
        rows.append(
            {
                **row,
                "include": decision[match["key"]],
                "origin": "user_accepted" if decision[match["key"]] else "user_rejected",
                "review_key": match["key"],
            }
        )
    unclaimed = [item["key"] for item in user_anchors if item["key"] not in claimed]
    if unclaimed:
        raise ValueError(
            "User anchor(s) " + ", ".join(unclaimed) + " of the audit match no True row of the library the "
            "preview read; run the preview again."
        )
    automatic = sorted(
        (item for item in anchors if item["source"] == "Automatic"),
        key=lambda item: (item["reference_rt"] if item["reference_rt"] is not None else math.inf, item["key"]),
    )
    for item in automatic:
        if item["reference_rt"] is None or item["mz"] is None:
            raise ValueError(
                f"Automatic anchor {item['key']} has no reference-file RT or m/z in the audit; it cannot be "
                "written as a library row."
            )
        accepted_here = decision[item["key"]]
        rows.append(
            {
                "name": automatic_anchor_name(item),
                "rt": item["reference_rt"],
                "rt_tolerance": match_rt_tolerance,
                "mz": item["mz"],
                "mz_tolerance": ms1_tolerance,
                "minimum_height": 0.0,
                "include": accepted_here,
                "origin": "automatic_accepted" if accepted_here else "automatic_rejected",
                "review_key": item["key"],
            }
        )

    # The file whose RTs the accepted automatic anchors carry. A run with the approved library is checked
    # against it afterwards (automatic_rt_review.approved_reference_check): a Console before
    # MsdialWorkbench#876 can map the run onto another file.
    preview_reference = review.get("reference") or {}
    preview_reference_file = {
        "file_id": str(preview_reference.get("file_id") or ""),
        "name": str(preview_reference.get("name") or ""),
        "selection": str(anchor_review.get("reference_selection") or ""),
    }
    true_rows = sum(1 for row in rows if row["include"])
    user_only_issues = anchor_library_issues(_numbered(rows), "UserOnly", minimum)
    hybrid_issues = anchor_library_issues(_numbered(rows), "Hybrid", minimum)
    # Decision 4 (2026-11-10): the UserOnly gate counts what the Console counts, the distinct anchors
    # detected in the preview's reference file, not the True rows.
    distinct = distinct_reference_anchors(
        anchors,
        [row["review_key"] for row in rows if row["include"] and row.get("review_key")],
        (review.get("reference") or {}).get("estimated_scan_interval"),
    )
    if not user_only_issues and distinct["count"] < minimum:
        user_only_issues = [
            f"Of the {true_rows} True row(s), {distinct['count']} are distinct anchors detected in the preview's "
            f"reference file, and UserOnly needs at least the minimum anchor count, {minimum}: the Console would "
            "refuse the run after picking peaks in every file. "
            + " ".join(item["reason"] for item in distinct["not_counted"])
        ]
    flagged = [
        {"key": item["key"], "flags": list(item.get("flags") or [])}
        for item in anchors
        if item.get("flags") and decision.get(item["key"])
    ]
    result: dict[str, Any] = {
        "written": False,
        "preview_directory": str(directory),
        # The preview record's job, never the caller's word for it.
        "preview_job_id": str(record.get("job_id") or "") or preview_job_id,
        "preview_anchor_mode": preview_mode,
        "preview_reference_file": preview_reference_file,
        "distinct_reference_anchors": distinct["count"],
        "distinct_reference_anchor_keys": distinct["counted"],
        "not_counted_in_reference": distinct["not_counted"],
        "accepted_flagged_anchors": flagged,
        "rows": [
            {key: row[key] for key in ("name", "rt", "rt_tolerance", "mz", "mz_tolerance", "minimum_height", "include", "origin")}
            for row in rows
        ],
        "true_rows": true_rows,
        "false_rows": len(rows) - true_rows,
        "minimum_anchors": minimum,
        "accepted": accepted_keys,
        "rejected": rejected_keys,
        "user_only_allowed": not user_only_issues,
        "hybrid_allowed": not hybrid_issues,
        # D2: UserOnly unless it is refused; then nothing is chosen for the person.
        "production_anchor_mode": "UserOnly" if not user_only_issues else "",
        "issues": {"UserOnly": user_only_issues, "Hybrid": hybrid_issues},
        "tolerances": {
            "automatic_rt_tolerance": match_rt_tolerance,
            "automatic_rt_tolerance_source": "automatic RT correction match RT tolerance",
            "automatic_mz_tolerance": ms1_tolerance,
            "automatic_mz_tolerance_source": "MS1 tolerance for alignment",
            "automatic_minimum_height": 0.0,
        },
    }
    if user_only_issues:
        counted = (
            f"{true_rows} True row(s) are fewer than the minimum anchor count, {minimum}"
            if true_rows < minimum
            else f"{distinct['count']} of the {true_rows} True row(s) are distinct anchors detected in the "
            f"preview's reference file, fewer than the minimum anchor count, {minimum}"
        )
        result["message"] = (
            f"{counted}, so UserOnly is refused. Choose Hybrid explicitly (the True rows are anchors and "
            "automatic anchors fill the RT bins they leave empty; the False rows keep rejected candidates "
            "out), or accept more anchors."
        )
    if flagged:
        result["warnings"] = [
            f"Accepted {item['key']}: " + " Also ".join(flag["message"] for flag in item["flags"]) for item in flagged
        ]
    if hybrid_issues and user_only_issues:
        raise ValueError("The approved library would have no usable rows: " + " ".join(hybrid_issues))
    if not write:
        return result

    person = str(confirmed_by or "").strip()
    if not person:
        raise ValueError("confirmed_by must name the person who approved these anchors.")
    if not root_text:
        raise ValueError("The production output root is unknown; pass output_root.")
    root = Path(root_text).expanduser()
    approved_at = (now or dt.datetime.now().astimezone()).isoformat()
    with _APPROVAL_LOCK:
        library, library_sha256 = create_timestamped_library(root, APPROVED_LIBRARY_STEM, rows, now)
    approval = {
        "schema": APPROVAL_SCHEMA,
        "approved_by": person,
        "approved_at": approved_at,
        "preview_job_id": result["preview_job_id"],
        "preview_directory": str(directory),
        "preview_anchor_mode": preview_mode,
        "preview_anchor_library": {
            "name": anchor_review.get("anchor_library") or "",
            "sha256": anchor_review.get("anchor_library_sha256") or "",
        },
        "preview_audit_sha256": _audit_checksums(directory),
        "preview_reference_file": preview_reference_file,
        "library_path": str(library),
        "library_name": library.name,
        "library_sha256": library_sha256,
        "accepted": accepted_keys,
        "rejected": rejected_keys,
        "rows": {
            "true": true_rows,
            "false": len(rows) - true_rows,
            "user_accepted": sum(1 for row in rows if row["origin"] == "user_accepted"),
            "user_rejected": sum(1 for row in rows if row["origin"] == "user_rejected"),
            "automatic_accepted": sum(1 for row in rows if row["origin"] == "automatic_accepted"),
            "automatic_rejected": sum(1 for row in rows if row["origin"] == "automatic_rejected"),
            "library_false_rows": sum(1 for row in rows if row["origin"] == "library_false_row"),
        },
        "minimum_anchors": minimum,
        # What the UserOnly gate counted (decision 4); workflow validation checks a later minimum against it.
        "distinct_reference_anchors": distinct["count"],
        "distinct_reference_anchor_keys": distinct["counted"],
        "not_counted_in_reference": distinct["not_counted"],
        "accepted_flagged_anchors": flagged,
        "production_anchor_mode": result["production_anchor_mode"],
        "user_only_allowed": result["user_only_allowed"],
        "tolerances": result["tolerances"],
    }
    record_path = approval_record_path(library)
    # The library's name is this request's alone (create_timestamped_library), and so is the record's.
    with record_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(approval, ensure_ascii=False, indent=2))
    result.update(
        {
            "written": True,
            "library_path": str(library),
            "library_sha256": library_sha256,
            "approval_record": str(record_path),
            "approval": approval,
        }
    )
    return result


def _numbered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "row": index,
            "name": row["name"],
            "rt": float(row["rt"]),
            "rt_tolerance": float(row["rt_tolerance"]),
            "mz": float(row["mz"]),
            "mz_tolerance": float(row["mz_tolerance"]),
            "minimum_height": float(row["minimum_height"]),
            "include": bool(row["include"]),
        }
        for index, row in enumerate(rows, start=1)
    ]

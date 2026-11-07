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
- D2: the production run defaults to UserOnly with the approved library; with fewer True rows than the
  minimum anchor count UserOnly is refused, and the person has to choose Hybrid explicitly;
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


def read_anchor_library(path: str | Path) -> list[dict[str, Any]]:
    """A library file's rows (parse_anchor_library), read as File.ReadAllLines reads it: UTF-8, any line break."""
    if not str(path or "").strip():
        raise AnchorLibraryError(
            "Automatic RT correction anchor library file path is required when the anchor mode is UserOnly or Hybrid."
        )
    resource = Path(path).expanduser()
    if not resource.is_file():
        raise FileNotFoundError(f"The automatic RT correction anchor library was not found: {resource}")
    text = resource.read_bytes().decode("utf-8-sig", errors="replace")
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
    """The library as the Console reads it: the header, then one seven-column ASCII row per entry."""
    lines = [LIBRARY_HEADER]
    for index, row in enumerate(rows, start=1):
        name = str(row.get("name", "")).strip()
        if not name:
            raise AnchorLibraryError(f"Anchor row {index} requires a name.")
        if any(character in name for character in "\t\r\n"):
            raise AnchorLibraryError(f"Anchor row {index}: a name cannot hold a tab or a line break.")
        if not name.isascii():
            raise AnchorLibraryError(
                f"Anchor row {index}: the name {name!r} is not ASCII; the anchor library is written as ASCII."
            )
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


def write_anchor_library(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    """Write a library and read it back with the strict reader, so what is written is what the Console reads."""
    target = Path(path)
    text = anchor_library_text(rows)
    reread = parse_anchor_library(_LINE_BREAK.split(text)[:-1])
    if len(reread) != len(rows):
        raise AnchorLibraryError("The anchor library did not read back as written.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="ascii", newline="") as handle:
        handle.write(text)
    return target


def timestamped_library_path(directory: str | Path, stem: str, now: dt.datetime | None = None) -> Path:
    folder = Path(directory).expanduser().resolve()
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._") or APPROVED_LIBRARY_STEM
    path = folder / f"{clean}_{stamp}.txt"
    suffix = 2
    while path.exists():
        path = folder / f"{clean}_{stamp}_{suffix}.txt"
        suffix += 1
    return path


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

    true_rows = sum(1 for row in rows if row["include"])
    user_only_issues = anchor_library_issues(_numbered(rows), "UserOnly", minimum)
    hybrid_issues = anchor_library_issues(_numbered(rows), "Hybrid", minimum)
    result: dict[str, Any] = {
        "written": False,
        "preview_directory": str(directory),
        "preview_job_id": preview_job_id or str(record.get("job_id") or ""),
        "preview_anchor_mode": preview_mode,
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
        result["message"] = (
            f"{true_rows} True row(s) are fewer than the minimum anchor count, {minimum}, so UserOnly is "
            "refused. Choose Hybrid explicitly (the True rows are anchors and automatic anchors fill the RT "
            "bins they leave empty; the False rows keep rejected candidates out), or accept more anchors."
        )
    if hybrid_issues and user_only_issues:
        raise ValueError("The approved library would have no usable rows: " + " ".join(hybrid_issues))
    if not write:
        return result

    person = str(confirmed_by or "").strip()
    if not person:
        raise ValueError("confirmed_by must name the person who approved these anchors.")
    root = Path(output_root or record.get("output_root") or "").expanduser()
    if not str(output_root or record.get("output_root") or "").strip():
        raise ValueError("The production output root is unknown; pass output_root.")
    approved_at = (now or dt.datetime.now().astimezone()).isoformat()
    library = timestamped_library_path(root, APPROVED_LIBRARY_STEM, now)
    write_anchor_library(library, rows)
    library_sha256 = file_sha256(library)
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
        "production_anchor_mode": result["production_anchor_mode"],
        "user_only_allowed": result["user_only_allowed"],
        "tolerances": result["tolerances"],
    }
    record_path = approval_record_path(library)
    record_path.write_text(json.dumps(approval, ensure_ascii=False, indent=2), encoding="utf-8")
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

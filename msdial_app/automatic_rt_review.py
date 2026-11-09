"""Read the Console's alignment-only RT correction audit without changing its results."""

import csv
import json
import math
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from .automatic_rt_evidence import (
    ANCHORS,
    EXCLUSIONS,
    METHOD,
    OUTLIER_STATUSES,
    OUTLIER_TEST_LOCAL,
    OUTLIER_TEST_OFF,
    OUTLIER_TEST_RUN_WIDE_FLOORED,
    OUTLIER_TEST_RUN_WIDE_MAD,
    SUMMARY,
    automatic_rt_correction_proof,
    discarded_method_keys,
    has_local_support_columns,
    proof_reason_phrase,
    read_method_key_record,
)


SUMMARY_COLUMNS = {"File ID", "File name", "File type", "Analytical order", "Used anchors", "Model source"}
ANCHOR_COLUMNS = {"File ID", "Anchor ID", "m/z", "Reference RT (min)", "Original RT (min)", "Quality score", "Used", "Status"}
# Anchor statuses the Console writes for a record it never meant to use. A Blank file's model
# is interpolated from, or copied from, neighbouring injections, or it keeps its original RT;
# its own anchors never fit it, so every anchor record in a Blank is unused by design. Missing
# and Ambiguous records matched no single peak and so never reached the model. Only the others
# (MadOutlier and, from MsdialWorkbench#826, LocalOutlier - OUTLIER_STATUSES - NonMonotonic,
# InsufficientAnchors, and any status this viewer does not know) are rejections a reviewer has to
# look at. The columns #826 appended (automatic_rt_evidence.LOCAL_SUPPORT_ANCHOR_COLUMNS and
# LOCAL_SUPPORT_SUMMARY_COLUMNS) are read by name where present and are None in an older audit;
# the required column sets below are #810's and stay so.
BLANK_ANCHOR_STATUSES = {"BlankInterpolateByOrder", "BlankNotCorrected"}
# MissingInReference (MsdialWorkbench#845) is a user anchor the reference file did not show: it has no
# reference RT, so it never reached a model either.
UNMATCHED_ANCHOR_STATUSES = {"Missing", "Ambiguous", "MissingInReference"}
# MsdialWorkbench#876: how the Console chose the reference file, in every summary row, and each file's
# library RT deviation (the median over its detected user anchors of |observed RT - library RT| / RT
# tolerance). A Console with #876 chooses, in UserOnly and Hybrid with an automatic reference, the file
# with the most user anchors detected and then the smallest deviation: the file an approved library's
# RTs were measured in. An audit without the column is from a Console before #876, which chose by the
# closeness of each file's anchor RTs to their median over the batch.
REFERENCE_SELECTION_COLUMN = "Reference selection"
LIBRARY_RT_DEVIATION_COLUMN = "Library RT deviation (RT tolerances)"
LIBRARY_RT_REFERENCE_PR = "MsdialWorkbench#876"
WORKFLOW_SETTINGS = "workflow-settings.json"
ANCHOR_APPROVAL_KEY = "automatic_rt_correction_anchor_approval"
SMOOTHING_METHOD_KEYS = {"smoothing method", "smoothing level"}
MAX_AUDIT_BYTES = 32 * 1024 * 1024
MAX_ROWS = 200_000
SELECTION_FIELDS = (
    ("Automatic RT correction intensity quantile", "Intensity quantile"),
    ("Automatic RT correction maximum peak width quantile", "Maximum peak-width quantile"),
    ("Automatic RT correction minimum signal to noise", "Minimum S/N"),
    ("Automatic RT correction minimum Gaussian similarity", "Minimum Gaussian similarity"),
    ("Automatic RT correction minimum ideal slope", "Minimum ideal slope"),
    ("Automatic RT correction match RT tolerance", "Cross-file RT tolerance (min)"),
    ("Automatic RT correction minimum sample coverage", "Minimum non-Blank coverage"),
)


def _method_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().casefold()] = value.strip()
    return values


OUTLIER_TEST_DESCRIPTIONS = {
    OUTLIER_TEST_LOCAL: (
        "Each anchor against the median of the offsets of the other compounds matched in the file "
        "within the local support window (LocalOutlier), or of the file's anchors where fewer than "
        "three such compounds are found (MadOutlier). Co-eluting reference candidates (isotope peaks, "
        "adducts) count as one compound and the anchor's own is left out, so Neighbours counts "
        "compounds; the scale is floored at the MS1 cycle around the anchor."
    ),
    OUTLIER_TEST_RUN_WIDE_FLOORED: (
        "Each anchor against the median offset of the file's anchors (MadOutlier); the window is 0, "
        "so no local test; the scale is floored at the MS1 cycle around the anchor."
    ),
    OUTLIER_TEST_RUN_WIDE_MAD: (
        "Each anchor against the median offset of the file's anchors (MadOutlier), skipped where that "
        "MAD is 0. This Console predates the local outlier test (MsdialWorkbench#826)."
    ),
    OUTLIER_TEST_OFF: "No outlier test: the outlier MAD threshold is 0.",
}


def _outlier_settings(proof: dict, anchor_rows: list[dict[str, str]], method_values: dict[str, str]) -> list[dict]:
    """The outlier test's settings as this run's audit and method file show them, for the viewer.

    Read from the rows even when the proof stopped early, so that a run whose evidence is refused
    still shows what its audit says.
    """
    from .automatic_rt_evidence import outlier_test_of

    found = proof if proof.get("outlier_test") else outlier_test_of(anchor_rows, method_values)
    window = found.get("local_support_rt_window")
    if not found.get("local_support_columns"):
        window_text = "not applicable: this Console predates MsdialWorkbench#826"
    elif found.get("local_support_rt_window_source") == "console_default":
        window_text = f"{window:g} (Console default; method.txt has no line)"
    else:
        window_text = f"{window:g}" + (" (run-wide test only)" if not window else "")
    return [
        {"label": "Outlier test", "value": OUTLIER_TEST_DESCRIPTIONS.get(found.get("outlier_test"), "")},
        {"label": "Outlier MAD threshold", "value": method_values.get("automatic rt correction outlier mad threshold")
         or f"{found.get('outlier_mad_threshold'):g} (Console default; method.txt has no line)"},
        {"label": "Local support RT window (min)", "value": window_text},
    ]


def _smooth_eic(points: list[dict], method: str, level: int) -> list[float]:
    """Preview the CommonStandard moving averages on the full, original RT axis."""
    if method not in {"LinearWeightedMovingAverage", "TimeBasedLinearWeightedMovingAverage", "SimpleMovingAverage"}:
        raise ValueError(f"Smoothing preview is not implemented for {method or 'an unrecorded method'}; raw EIC is retained.")
    if not 0 <= level <= 100:
        raise ValueError("Smoothing preview requires an integer level between 0 and 100.")
    if not points:
        return []
    intensities = [point["intensity"] for point in points]
    if method != "TimeBasedLinearWeightedMovingAverage":
        weights = [level + 1 - abs(offset) if method == "LinearWeightedMovingAverage" else 1
                   for offset in range(-level, level + 1)]
        denominator = sum(weights)
        return [sum(weight * (intensities[i + offset] if 0 <= i + offset < len(points) else intensities[i])
                    for offset, weight in zip(range(-level, level + 1), weights)) / denominator
                for i in range(len(points))]
    if len(points) == 1:
        return intensities
    times = [point["original_rt"] for point in points]
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError("Time-based smoothing preview requires strictly increasing MS1 retention times.")
    interval = (times[-1] - times[0]) / (len(times) - 1)
    distance = level + 1
    result, start = [], 0
    for time in times:
        lo, hi = time - interval * distance, time + interval * distance
        while start < len(times) and times[start] <= lo:
            start += 1
        numerator = denominator = 0.0
        for index in range(start, len(times)):
            if times[index] >= hi:
                break
            weight = distance - abs(time - times[index]) / interval
            numerator += intensities[index] * weight
            denominator += weight
        result.append(numerator / denominator)
    return result


def _table(path: Path, required: set[str]) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"RT correction audit not found: {path.name}. Run LC-MS with automatic alignment RT correction enabled.")
    if path.stat().st_size > MAX_AUDIT_BYTES:
        raise ValueError(f"RT correction audit {path.name} exceeds the review limit.")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name} is missing columns: {', '.join(sorted(missing))}")
        rows = []
        for row in reader:
            if len(rows) >= MAX_ROWS:
                raise ValueError(f"{path.name} has too many rows for interactive review.")
            rows.append(row)
    return rows


def _number(row: dict[str, str], column: str) -> float | None:
    try:
        value = float(row.get(column, ""))
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def read_automatic_rt_review(directory: str | Path) -> dict:
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Output directory not found: {root}")
    summary_path, anchors_path = root / SUMMARY, root / ANCHORS
    summary_rows = _table(summary_path, SUMMARY_COLUMNS)
    anchor_rows = _table(anchors_path, ANCHOR_COLUMNS)
    if not summary_rows:
        raise ValueError("RT correction summary contains no analysis files.")

    files = []
    file_ids = set()
    for row in summary_rows:
        file_id = row["File ID"].strip()
        if not file_id or file_id in file_ids:
            raise ValueError("RT correction summary contains an empty or duplicate File ID.")
        file_ids.add(file_id)
        files.append({
            "file_id": file_id,
            "name": row["File name"],
            "type": row["File type"],
            "order": _number(row, "Analytical order"),
            "candidate_count": _number(row, "Candidate count"),
            "matched_anchors": _number(row, "Matched anchors"),
            "used_anchors": _number(row, "Used anchors"),
            "model_source": row["Model source"],
            "reference_score": _number(row, "Reference score"),
            "median_absolute_offset": _number(row, "Median absolute offset (min)"),
            "maximum_absolute_offset": _number(row, "Maximum absolute offset (min)"),
            "note": row.get("Note", ""),
            # MsdialWorkbench#826; None in an audit written before it.
            "estimated_scan_interval": _number(row, "Estimated scan interval (min)"),
            "first_used_anchor_rt": _number(row, "First used anchor RT (min)"),
            "last_used_anchor_rt": _number(row, "Last used anchor RT (min)"),
            "peaks_before_first_used_anchor": _number(row, "Peaks before first used anchor"),
            "peaks_after_last_used_anchor": _number(row, "Peaks after last used anchor"),
            # MsdialWorkbench#845; "" or None in an audit written before it.
            "anchor_mode": str(row.get("Anchor mode") or "").strip(),
            "user_anchors_detected": _number(row, "User anchors detected"),
            "excluded_automatic_candidates": _number(row, "Excluded automatic candidates"),
            "run": str(row.get("Run") or "").strip(),
            # MsdialWorkbench#876; "" or None in an audit written before it.
            "reference_selection": str(row.get(REFERENCE_SELECTION_COLUMN) or "").strip(),
            "library_rt_deviation": _number(row, LIBRARY_RT_DEVIATION_COLUMN),
        })

    anchors = []
    anchors_by_file = {file_id: [] for file_id in file_ids}
    blank_files = {item["file_id"] for item in files if item["type"].strip().casefold() == "blank"}
    for row in anchor_rows:
        file_id = row["File ID"].strip()
        if file_id not in file_ids:
            raise ValueError(f"RT correction anchor refers to unknown file ID {file_id}.")
        original = _number(row, "Original RT (min)")
        reference = _number(row, "Reference RT (min)")
        offset = reference - original if original is not None and reference is not None else None
        if offset is not None and not math.isfinite(offset):
            offset = None
        used = row["Used"].strip().casefold() == "true"
        status = row["Status"].strip()
        if used:
            category = "used"
        elif file_id in blank_files or status in BLANK_ANCHOR_STATUSES:
            category = "blank"
        elif status in UNMATCHED_ANCHOR_STATUSES:
            category = "unmatched"
        else:
            category = "rejected"
        anchor = {
            "file_id": file_id,
            "anchor_id": row["Anchor ID"].strip(),
            "mz": _number(row, "m/z"),
            "reference_rt": reference,
            "original_rt": original,
            "offset": offset,
            "rt_available": original is not None and reference is not None and offset is not None,
            "quality_score": _number(row, "Quality score"),
            "coverage": _number(row, "Non-Blank sample coverage"),
            "used": used,
            "status": status,
            "category": category,
            # MsdialWorkbench#826: which test judged the anchor (Local, Global, or empty when it was
            # not judged), its neighbour count, the offset it was compared with, the scale, and the
            # MS1 cycle floor. None (or "") in an audit written before it.
            "outlier_test": str(row.get("Outlier test") or "").strip(),
            "local_support_count": _number(row, "Local support count"),
            "expected_offset": _number(row, "Expected offset (min)"),
            "outlier_scale": _number(row, "Outlier scale (min)"),
            "ms1_cycle_time": _number(row, "MS1 cycle at anchor (min)"),
            # MsdialWorkbench#845: whether the anchor came from the detected peaks or the anchor library,
            # its library name and RT, whether the outlier test disagreed with a user anchor (which is
            # kept regardless), the matched peak's height and m/z, and how a user anchor's peak was
            # chosen. A user anchor has no quality score, and no reference RT when it was not found in
            # the reference file. An audit before #845 has no source column: every anchor is automatic.
            "source": str(row.get("Anchor source") or "").strip() or "Automatic",
            "anchor_name": str(row.get("Anchor name") or "").strip(),
            "library_rt": _number(row, "Library RT (min)"),
            "outlier_flagged": str(row.get("Outlier flagged") or "").strip().casefold() == "true",
            "peak_height": _number(row, "Peak height"),
            "observed_mz": _number(row, "Observed m/z"),
            "detection": str(row.get("Detection") or "").strip(),
        }
        anchors.append(anchor)
        anchors_by_file[file_id].append(anchor)

    for file in files:
        used = [item for item in anchors_by_file[file["file_id"]] if item["used"]]
        file["model_reconstructable"] = (
            file["model_source"] in {"Reference", "DetectedAnchors"}
            and all(item["rt_available"] for item in used)
            and len({item["original_rt"] for item in used}) >= 2
        )

    model_counts = dict(Counter(item["model_source"] for item in files))
    reference = next((item for item in files if item["model_source"] == "Reference"), None)
    by_category = {
        category: Counter(item["status"] for item in anchors if item["category"] == category)
        for category in ("rejected", "unmatched", "blank")
    }
    rejected = by_category["rejected"]
    warnings, notes = [], []
    if not reference:
        warnings.append("No reference file is recorded. Do not interpret the shift curves as validated correction.")
    if model_counts.get("Uncorrected"):
        warnings.append(f"{model_counts['Uncorrected']} file(s) remained on their original RT axis.")
    if rejected:
        warnings.append("Rejected anchors require review: " + ", ".join(f"{key} ({value})" for key, value in sorted(rejected.items())))
    if by_category["unmatched"]:
        notes.append(
            f"{sum(by_category['unmatched'].values())} anchor record(s) in non-Blank files matched no single peak within the tolerances: "
            + ", ".join(f"{key} ({value})" for key, value in sorted(by_category["unmatched"].items()))
            + ". They never reached a model; they have no sample RT and remain in the table as N/A."
        )
    if by_category["blank"]:
        notes.append(
            f"{sum(by_category['blank'].values())} anchor record(s) are in Blank files, not used by design: "
            + ", ".join(f"{key} ({value})" for key, value in sorted(by_category["blank"].items()))
            + ". A Blank's model comes from neighbouring injections, or it keeps its original RT; its own anchors never fit it."
        )
    exclusions = _exclusions(root / EXCLUSIONS)
    anchor_review = _anchor_review(files, anchors, summary_rows, exclusions, reference)
    reference_check = approved_reference_check(reference, anchor_review, run_anchor_approval(root))
    if reference_check:
        if reference_check["warning"]:
            warnings.append(reference_check["warning"])
        elif reference_check["note"]:
            notes.append(reference_check["note"])
    if anchor_review["run"] == "Preview":
        notes.append(
            "This audit is a preview's (Run = Preview): the Console chose the anchors and stopped before "
            "alignment. Accept or reject each anchor; the approved anchors become the production run's "
            "anchor library. Nothing was aligned, so this audit is not evidence of a correction."
        )
    for exclusion in exclusions:
        if exclusion["excluded_candidates"] == 0:
            notes.append(
                f"Exclusion {exclusion['anchor_name'] or '(unnamed)'} (m/z {exclusion['mz']}, RT "
                f"{exclusion['rt']} min) matched no automatic candidate of the reference file."
            )
    flagged = sorted({item["anchor_name"] or item["anchor_id"] for item in anchors if item["outlier_flagged"]})
    if flagged:
        warnings.append(
            "The outlier test disagrees with user anchor(s) " + ", ".join(flagged)
            + " in at least one file; a user anchor is kept regardless, so review it."
        )
    missing_rt = [item for item in anchors if not item["rt_available"]]
    # A record that matched no peak has no sample RT by construction; any other is unexpected.
    unexplained_missing_rt = [item for item in missing_rt if item["status"] not in UNMATCHED_ANCHOR_STATUSES]
    if unexplained_missing_rt:
        warnings.append(f"{len(unexplained_missing_rt)} anchor record(s) other than Missing or Ambiguous have unavailable RT values. They remain in the table as N/A and are excluded from numeric plots; no RT values are imputed.")
    invalid_used_files = sorted({item["file_id"] for item in missing_rt if item["used"]})
    if invalid_used_files:
        warnings.append("Anchors marked Used have unavailable RT values in File ID(s) " + ", ".join(invalid_used_files) + ". Their correction models cannot be reconstructed safely.")
    if any(item["model_source"] in {"InterpolatedBlank", "NearestBlank"} for item in files):
        warnings.append("Blank models are derived from neighboring samples; their control points are not recorded in the anchor TSV, so the viewer does not reconstruct their shift curves.")

    # The publication report takes its verdict from the same call, so "verified" here is
    # exactly the case in which the report describes the correction.
    proof = automatic_rt_correction_proof(root, summary_rows, anchor_rows)
    record = read_method_key_record(root) or {}
    method_audit = {
        "status": "verified" if proof["performed"] else proof["reason"],
        "reason": proof["reason"],
        "automatic_rt_key_applied": proof["method_key_applied"],
        "discarded_keys": list(proof.get("discarded_keys") or []),
        "applied_count": len(record.get("applied") or []),
        "unrecognised": list(record.get("unrecognised") or []),
        "unusable": list(record.get("unusable") or []),
        "blank": list(record.get("blank") or []),
    }
    # A preview never aligns, so it never proves a correction; the note above says so without alarm.
    if not proof["performed"] and proof["reason"] != "preview_only":
        warnings.append(
            f"The retained records do not prove a correction in this run ({proof['reason']}): "
            f"{proof_reason_phrase(proof)}. The publication report does not describe a correction from them."
        )
    if method_audit["unusable"]:
        warnings.append(f"{len(method_audit['unusable'])} method parameter value(s) were rejected by the Console.")

    method_values = _method_values(root / METHOD)
    outlier_settings = _outlier_settings(proof, anchor_rows, method_values)
    return {
        "run_directory": str(root),
        "summary_file": str(summary_path),
        "anchors_file": str(anchors_path),
        "files": files,
        "anchors": anchors,
        "reference": reference,
        "model_counts": model_counts,
        "rejected_status_counts": dict(rejected),
        "unmatched_status_counts": dict(by_category["unmatched"]),
        "blank_status_counts": dict(by_category["blank"]),
        "method_audit": method_audit,
        "selection_settings": [{"label": label, "value": method_values.get(key.casefold()) or None}
                               for key, label in SELECTION_FIELDS],
        "outlier_settings": outlier_settings,
        "local_support_columns": has_local_support_columns(anchor_rows),
        "outlier_status_counts": {
            status: count for status, count in dict(rejected).items() if status in OUTLIER_STATUSES
        },
        "smoothing_settings": {
            "method": method_values.get("smoothing method") or None,
            "level": method_values.get("smoothing level") or None,
            "ms1_tolerance": method_values.get("ms1 tolerance for centroid") or None,
        },
        "anchor_review": anchor_review,
        "reference_check": reference_check,
        "exclusions": exclusions,
        "exclusions_file": str(root / EXCLUSIONS) if (root / EXCLUSIONS).is_file() else "",
        "warnings": warnings,
        "notes": notes,
    }


EXCLUSION_COLUMNS = {
    "Anchor name",
    "m/z",
    "RT (min)",
    "m/z tolerance",
    "RT tolerance (min)",
    "Reference file ID",
    "Reference file name",
    "Excluded automatic candidates",
}


def _exclusions(path: Path) -> list[dict]:
    """The anchor library's False rows as a Hybrid run applied them (MsdialWorkbench#845); [] otherwise."""
    if not path.is_file():
        return []
    return [
        {
            "anchor_name": str(row.get("Anchor name") or "").strip(),
            "mz": _number(row, "m/z"),
            "rt": _number(row, "RT (min)"),
            "mz_tolerance": _number(row, "m/z tolerance"),
            "rt_tolerance": _number(row, "RT tolerance (min)"),
            "reference_file_id": str(row.get("Reference file ID") or "").strip(),
            "reference_file_name": str(row.get("Reference file name") or "").strip(),
            "excluded_candidates": int(_number(row, "Excluded automatic candidates") or 0),
        }
        for row in _table(path, EXCLUSION_COLUMNS)
    ]


def _anchor_review(
    files: list[dict],
    anchors: list[dict],
    summary_rows: list[dict[str, str]],
    exclusions: list[dict],
    reference: dict | None,
) -> dict:
    """Every anchor of the audit once, as a person reviews it: source, name, RTs, m/z, height, detection.

    The audit has one row per anchor and file. An anchor is the Console's Anchor ID, unique in a run,
    keyed with its source ("Automatic-3", "User-1") so that a key never names the other kind. Its
    reference RT, observed m/z, peak height, detection note and status are the reference file's row's;
    it is outlier-flagged when the test disagreed with a user anchor in any file.
    """
    first = summary_rows[0] if summary_rows else {}
    reference_id = (reference or {}).get("file_id")
    blank = {item["file_id"] for item in files if item["type"].strip().casefold() == "blank"}
    by_key: dict[str, dict] = {}
    for anchor in anchors:
        key = f"{anchor['source']}-{anchor['anchor_id']}"
        entry = by_key.get(key)
        if entry is None:
            entry = by_key[key] = {
                "key": key,
                "anchor_id": anchor["anchor_id"],
                "source": anchor["source"],
                "name": anchor["anchor_name"],
                "library_rt": anchor["library_rt"],
                "mz": anchor["mz"],
                "reference_rt": None,
                "observed_mz": None,
                "peak_height": None,
                "quality_score": None,
                "detection": "",
                "reference_status": "",
                "outlier_flagged": False,
                "files_detected": 0,
                "files_used": 0,
                "non_blank_files": 0,
            }
        if anchor["reference_rt"] is not None and entry["reference_rt"] is None:
            entry["reference_rt"] = anchor["reference_rt"]
        if anchor["file_id"] == reference_id:
            entry["observed_mz"] = anchor["observed_mz"]
            entry["peak_height"] = anchor["peak_height"]
            entry["quality_score"] = anchor["quality_score"]
            entry["detection"] = anchor["detection"]
            entry["reference_status"] = anchor["status"]
        entry["outlier_flagged"] = entry["outlier_flagged"] or anchor["outlier_flagged"]
        if anchor["file_id"] not in blank:
            entry["non_blank_files"] += 1
            if anchor["original_rt"] is not None:
                entry["files_detected"] += 1
            if anchor["used"]:
                entry["files_used"] += 1
    ordered = sorted(
        by_key.values(),
        key=lambda item: (
            item["reference_rt"] if item["reference_rt"] is not None else float("inf"),
            item["source"],
            item["key"],
        ),
    )
    return {
        "run": str(first.get("Run") or "").strip(),
        # An audit before #845 has no Anchor mode column; its correction was automatic.
        "anchor_mode": str(first.get("Anchor mode") or "").strip() or "Automatic",
        "anchor_mode_columns": "Anchor mode" in first,
        "anchor_library": str(first.get("Anchor library") or "").strip(),
        "anchor_library_sha256": str(first.get("Anchor library SHA-256") or "").strip(),
        "reference_file_id": reference_id,
        "reference_file_name": (reference or {}).get("name") or "",
        # MsdialWorkbench#876: how the reference was chosen; "" in an audit before it, or when none was.
        "reference_selection": str(first.get(REFERENCE_SELECTION_COLUMN) or "").strip(),
        "reference_selection_column": REFERENCE_SELECTION_COLUMN in first,
        "anchors": ordered,
        "user_anchor_count": sum(1 for item in ordered if item["source"] == "User"),
        "automatic_anchor_count": sum(1 for item in ordered if item["source"] == "Automatic"),
        "exclusion_count": len(exclusions),
    }


def run_anchor_approval(directory: str | Path) -> dict | None:
    """The anchor-library approval a run's workflow-settings.json records, or None.

    The run keeps it only when the approval still described the library the run read
    (workflow.automatic_rt_anchor_library_record), so a record found here is the approval of that library.
    """
    try:
        settings = json.loads((Path(directory) / WORKFLOW_SETTINGS).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    approval = settings.get(ANCHOR_APPROVAL_KEY) if isinstance(settings, dict) else None
    return approval if isinstance(approval, dict) and approval else None


def preview_reference_from_approval(approval: dict) -> dict | None:
    """The preview's reference file ({"file_id", "name"}) an approval was made against, or None.

    An approval written since MsdialWorkbench#876 records it (preview_reference_file). An earlier one is
    read from the preview's own summary audit while the preview directory still holds it.
    """
    recorded = approval.get("preview_reference_file")
    if isinstance(recorded, dict) and (recorded.get("file_id") not in (None, "") or recorded.get("name")):
        return {"file_id": str(recorded.get("file_id") or ""), "name": str(recorded.get("name") or "")}
    directory = str(approval.get("preview_directory") or "").strip()
    if not directory:
        return None
    try:
        rows = _table(Path(directory) / SUMMARY, SUMMARY_COLUMNS)
    except (OSError, ValueError):
        return None
    row = next((item for item in rows if item["Model source"].strip() == "Reference"), None)
    if row is None:
        return None
    return {"file_id": row["File ID"].strip(), "name": row["File name"].strip()}


def _same_file(left: dict, right: dict) -> bool:
    # The production run and its preview read the same files, but a file's ID is its row in the run's
    # file list; the name says which file it is wherever both are known.
    if left.get("name") and right.get("name"):
        return left["name"] == right["name"]
    return str(left.get("file_id") or "") == str(right.get("file_id") or "")


def approved_reference_check(reference: dict | None, anchor_review: dict, approval: dict | None) -> dict | None:
    """Whether a run that read an approved anchor library mapped onto the preview's reference file.

    An approved automatic anchor is a True row at the RT it had in the preview's reference file, so that is
    the axis the person reviewed. A Console with MsdialWorkbench#876 chooses that file again (the most user
    anchors detected, then the closest to the library's RTs); one before it chose by the closeness of each
    file's anchor RTs to their median over the batch, and can map the run onto another file. A difference is
    a warning, never a refusal: the run is internally consistent, on another file's axis.

    None when it does not apply: no approval, a preview's audit, or a run whose audit names another anchor
    library than the approved one. Otherwise status is "matches", "differs" or "unknown" (the preview's
    reference could not be found), with the warning or note to show.
    """
    if not isinstance(approval, dict) or anchor_review.get("run") == "Preview":
        return None
    approved = str(approval.get("library_sha256") or "").casefold()
    audited = str(anchor_review.get("anchor_library_sha256") or "").casefold()
    if not approved or audited != approved:
        return None
    rule = bool(anchor_review.get("reference_selection_column"))
    selection = str(anchor_review.get("reference_selection") or "")
    run_reference = {"file_id": str((reference or {}).get("file_id") or ""), "name": str((reference or {}).get("name") or "")}
    preview_reference = preview_reference_from_approval(approval)
    check = {
        "status": "unknown",
        "preview_reference": preview_reference,
        "run_reference": run_reference if reference else None,
        "run_reference_selection": selection,
        "console_has_library_rt_reference_rule": rule,
        "warning": "",
        "note": "",
    }

    def label(item: dict) -> str:
        return f"{item['name'] or '(unnamed)'} (file ID {item['file_id'] or '?'})"

    if preview_reference is None or reference is None:
        check["note"] = (
            "The run read an approved anchor library, but "
            + ("the preview's reference file is not recorded in the approval and its audit is gone"
               if preview_reference is None else "the run's audit records no reference file")
            + ", so whether the run was mapped onto the file the anchors were reviewed on cannot be checked."
        )
        return check
    if _same_file(preview_reference, run_reference):
        check["status"] = "matches"
        check["note"] = (
            f"The run's RT correction reference file is the preview's, {label(run_reference)}: every file was "
            "mapped onto the axis the approved anchors were reviewed on."
        )
        return check
    check["status"] = "differs"
    text = (
        f"The run's RT correction reference file is {label(run_reference)}, not the preview's, "
        f"{label(preview_reference)}, whose RTs the approved anchors carry: every file was mapped onto another "
        "file's axis than the one reviewed. The run was not refused."
    )
    if rule:
        if selection == "Requested":
            reason = ": the reference file ID was set in the method."
        elif selection.startswith("Automatic (coverage"):
            reason = ": no eligible file had an approved anchor detected, so the automatic score chose."
        else:
            reason = (
                "; a file with more approved anchors detected wins before the closest to the library's RTs, so "
                "compare 'User anchors detected' and 'Library RT deviation (RT tolerances)' in the summary audit."
            )
        text += f" The Console chose it by '{selection or 'an unrecorded rule'}'{reason}"
    else:
        text += (
            f" The Console that ran may predate the library-RT reference rule ({LIBRARY_RT_REFERENCE_PR}): its audit "
            f"has no '{REFERENCE_SELECTION_COLUMN}' column, so it chose the reference by the closeness of each "
            "file's anchor RTs to their median over the batch. Use a Console with that rule, or set the automatic "
            f"RT correction reference file ID to the preview's ({preview_reference['file_id'] or '?'}), and run again."
        )
    check["warning"] = text
    return check


def _correct_rt(rt: float, anchors: list[dict]) -> float | None:
    if not math.isfinite(rt) or any(item["used"] and not item["rt_available"] for item in anchors):
        return None
    by_rt: dict[float, dict] = {}
    for anchor in anchors:
        if not anchor["used"]:
            continue
        original = anchor["original_rt"]
        if original not in by_rt or (anchor["quality_score"] or 0) > (by_rt[original]["quality_score"] or 0):
            by_rt[original] = anchor
    points = sorted(by_rt.values(), key=lambda item: item["original_rt"])
    if len(points) < 2:
        return None
    if rt <= points[0]["original_rt"]:
        return rt + points[0]["offset"]
    if rt >= points[-1]["original_rt"]:
        return rt + points[-1]["offset"]
    for left, right in zip(points, points[1:]):
        if rt <= right["original_rt"]:
            fraction = (rt - left["original_rt"]) / (right["original_rt"] - left["original_rt"])
            return left["reference_rt"] + fraction * (right["reference_rt"] - left["reference_rt"])
    return None


def extract_anchor_eic(directory: str | Path, file_id: str, anchor_id: str, tolerance: float) -> dict:
    """Extract one chromatogram on demand; never copy or retain the vendor raw file."""
    if not math.isfinite(tolerance) or tolerance <= 0 or tolerance > 1:
        raise ValueError("EIC m/z tolerance must be greater than 0 and at most 1 Da.")
    review = read_automatic_rt_review(directory)
    root = Path(review["run_directory"])
    file = next((item for item in review["files"] if item["file_id"] == str(file_id)), None)
    anchor = next((item for item in review["anchors"] if item["file_id"] == str(file_id) and item["anchor_id"] == str(anchor_id)), None)
    if not file or not anchor or anchor["mz"] is None:
        raise ValueError("Select a file and an anchor recorded in this run's RT audit.")
    if not anchor["rt_available"]:
        raise ValueError(f"Anchor {anchor_id} has no finite original/reference RT ({anchor['status']}); no apex is available for this EIC review.")
    if file["model_source"] not in {"Reference", "DetectedAnchors"}:
        raise ValueError("The Console did not retain a reconstructable RT model for this file.")
    if not file["model_reconstructable"]:
        raise ValueError("The accepted anchors do not provide a complete, reconstructable RT model for this file.")
    if review["method_audit"]["status"] != "verified":
        raise ValueError(
            "Run provenance must be verified before extracting an EIC against its RT model "
            f"({review['method_audit']['reason']})."
        )

    input_csv = root / "analysis_files.csv"
    with input_csv.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    try:
        raw_row = rows[int(file_id)]
    except (IndexError, ValueError):
        raise ValueError("File ID is absent from this run's analysis_files.csv.") from None
    if raw_row.get("file_name", "").strip() != file["name"]:
        raise ValueError("The input CSV no longer matches the RT audit's file ordering.")
    raw_path = Path(raw_row["file_path"]).expanduser().resolve()
    if not raw_path.exists():
        raise FileNotFoundError(f"The original raw file is unavailable: {raw_path}")
    manifest = json.loads((root / "run-manifest.json").read_text(encoding="utf-8-sig"))
    console = Path(str((manifest.get("console") or {}).get("path") or "")).expanduser().resolve()
    if not console.is_file() or console.name.casefold() not in {"msdialcui.exe", "msdialcui.dll"}:
        raise FileNotFoundError("The Console used by this run is unavailable; select a valid run manifest.")
    acquisition = raw_row.get("acquisition_type", "DDA").strip()
    if acquisition not in {"DDA", "SWATH", "AIF", "None"}:
        acquisition = "DDA"

    with tempfile.TemporaryDirectory(prefix=".rt-eic-", dir=root) as temporary:
        output = Path(temporary) / "anchor_eic.csv"
        command = (["dotnet", str(console)] if console.suffix.lower() == ".dll" else [str(console)]) + [
            "eic", "raw", "--input", str(raw_path), "--output", str(output),
            "-target", str(anchor["mz"]), "-target", str(tolerance), "--acquisitiontype", acquisition,
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=300, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("EIC extraction exceeded five minutes. Try another representative raw file.") from None
        if completed.returncode != 0 or not output.is_file():
            detail = (completed.stderr or completed.stdout or "No EIC was produced.").strip()[-1200:]
            raise RuntimeError(f"MS-DIAL EIC extraction failed: {detail}")
        full_points = []
        model_anchors = [item for item in review["anchors"] if item["file_id"] == file_id]
        window = max(0.25, min(1.0, abs(anchor["offset"]) * 5 + 0.25))
        with output.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    rt, intensity = float(row["RT"]), float(row["Intensity"])
                except (KeyError, ValueError):
                    continue
                if not (math.isfinite(rt) and math.isfinite(intensity)):
                    continue
                full_points.append({"original_rt": rt, "intensity": intensity})
                if len(full_points) > 1_000_000:
                    raise ValueError("EIC contains more than 1,000,000 points; interactive smoothing review is bounded.")
        smoothing = {**review["smoothing_settings"], "available": False, "note": ""}
        smoothed = None
        # Match the recorded method only; never substitute another smoother silently. A
        # smoothing key the Console discarded, as an unusable or a blank value, left it on its
        # default, which method.txt does not show. That holds even if another line applied the
        # key: the preview reads method.txt's last line for it, which need not be the value the
        # Console kept.
        discarded = discarded_method_keys(review["method_audit"]) & SMOOTHING_METHOD_KEYS
        try:
            if discarded:
                raise ValueError(
                    f"The Console discarded the recorded {' and '.join(sorted(discarded))}; "
                    "no smoothed preview is inferred."
                )
            level = int(smoothing["level"])
            smoothed = _smooth_eic(full_points, smoothing["method"], level)
            smoothing["available"] = True
            smoothing["level"] = level
            smoothing["note"] = "Reconstructed for this review EIC using the recorded method, before cropping and RT projection. This does not replace the stored detection metrics."
        except (TypeError, ValueError) as error:
            smoothing["note"] = str(error) if discarded or smoothing["level"] is not None else "No smoothing level was recorded; only the raw EIC is shown."
        points = []
        for index, point in enumerate(full_points):
            rt = point["original_rt"]
            if abs(rt - anchor["original_rt"]) > window:
                continue
            points.append({**point, "corrected_rt": _correct_rt(rt, model_anchors),
                           "smoothed_intensity": smoothed[index] if smoothed is not None else None})
            if len(points) > 10_000:
                raise ValueError("EIC review window contains more than 10,000 points.")
    return {
        "file_id": file_id,
        "file_name": file["name"],
        "anchor_id": anchor_id,
        "mz": anchor["mz"],
        "tolerance": tolerance,
        "original_rt": anchor["original_rt"],
        "reference_rt": anchor["reference_rt"],
        "raw_file": str(raw_path),
        "smoothing": smoothing,
        "quality_score": anchor["quality_score"],
        "coverage": anchor["coverage"],
        "points": points,
    }

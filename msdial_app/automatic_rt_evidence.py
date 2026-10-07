"""The one rule for whether a run's retained records prove automatic RT correction ran.

The audit viewer and the publication report read the same three Console records: the
method-key record and the two audit TSVs. They used to judge them with separately written
rules, so the viewer could call a run verified while the report refused to describe its
correction. Both now take their verdict from here.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable


SUMMARY = "automatic_alignment_rt_correction_summary.tsv"
ANCHORS = "automatic_alignment_rt_correction_anchors.tsv"
METHOD_KEYS = "method.keys.json"
METHOD = "method.txt"
AUTOMATIC_RT_KEY = "execute automatic rt correction for alignment"
LOCAL_SUPPORT_KEY = "automatic rt correction local support rt window"
OUTLIER_MAD_THRESHOLD_KEY = "automatic rt correction outlier mad threshold"
# The anchor statuses of an anchor the outlier test rejected: MadOutlier against the median of the
# file's anchors (#810, and #826 where an anchor has too few neighbours), LocalOutlier against the
# median of the file's other matched reference candidates within the local support window (#826).
OUTLIER_STATUSES = ("LocalOutlier", "MadOutlier")
# Columns MsdialWorkbench#826 appended after the ones #810 wrote. Every reader here addresses columns
# by name, so a pre-#826 audit, which has none of them, reads as it always did.
LOCAL_SUPPORT_ANCHOR_COLUMNS = (
    "Outlier test",
    "Local support count",
    "Expected offset (min)",
    "Outlier scale (min)",
    "MS1 cycle at anchor (min)",
)
LOCAL_SUPPORT_SUMMARY_COLUMNS = (
    "Estimated scan interval (min)",
    "First used anchor RT (min)",
    "Last used anchor RT (min)",
    "Peaks before first used anchor",
    "Peaks after last used anchor",
)
# What the Console's outlier test was, as the audit shows it (outlier_test_of).
OUTLIER_TEST_LOCAL = "local_support_with_ms1_cycle_floor"
OUTLIER_TEST_RUN_WIDE_FLOORED = "run_wide_with_ms1_cycle_floor"
OUTLIER_TEST_RUN_WIDE_MAD = "run_wide_mad"
OUTLIER_TEST_OFF = "off"

# What each reason means, for a reader who sees the code in a warning.
PROOF_REASON_PHRASES = {
    "retained_evidence_missing": (
        "method.keys.json, method.txt or an audit TSV is missing or unreadable, so the audit "
        "cannot be tied to this run"
    ),
    "method_key_record_not_from_this_method_file": (
        "method.keys.json does not carry the hash of this directory's method.txt, so the "
        "audit may belong to an earlier run"
    ),
    "method_key_not_applied": (
        "the Console did not record the automatic RT correction method key as applied"
    ),
    "method_key_value_discarded_by_console": (
        "the Console discarded the value it was given for {keys} and ran with its default, "
        "so the recorded settings are not the ones the correction used"
    ),
    "method_key_not_recognised_by_console": (
        "the Console did not know {keys}, so it ran without that setting; a Console older than "
        "the setting does not implement it"
    ),
    "audit_older_than_method_key_record": (
        "an audit TSV is older than method.keys.json, so an earlier run wrote it"
    ),
    "retained_evidence_empty": "an audit TSV has no rows",
    "audit_does_not_show_correction": (
        "no file other than the reference was corrected from its own detected anchors"
    ),
}


def method_key_name(entry: Any) -> str:
    """The key a method-key record entry names.

    The Console records a value it could not use as "<key>: <value>" and a blank or applied
    key as the key alone, spelled as the method file spells it. Its reader ends a key at the
    first ":" or "=", so a key holds neither.
    """
    return str(entry).split(":", 1)[0].strip().casefold()


def discarded_method_keys(record: dict[str, Any]) -> set[str]:
    """Keys the Console read and did not apply: an unusable value, or a blank one.

    It ran with its default for each. A key listed here can also have been applied from
    another line of the same method file.
    """
    return {
        method_key_name(item)
        for item in [*(record.get("unusable") or []), *(record.get("blank") or [])]
    }


def read_method_key_record(root: Path) -> dict[str, Any] | None:
    try:
        record = json.loads((root / METHOD_KEYS).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _method_values(path: Path) -> dict[str, str]:
    """method.txt's values by case-folded key; the last line wins, as the Console keeps the last."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        separators = [index for index in (line.find(":"), line.find("=")) if index >= 0]
        if not separators:
            continue
        cut = min(separators)
        values[line[:cut].strip().casefold()] = line[cut + 1:].strip()
    return values


def _finite(text: Any) -> float | None:
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def has_local_support_columns(anchor_rows: list[dict[str, Any]]) -> bool:
    """True for an anchor audit written by a Console with MsdialWorkbench#826.

    A DictReader row carries every header column as a key, a short row's as None.
    """
    return bool(anchor_rows) and all(column in anchor_rows[0] for column in LOCAL_SUPPORT_ANCHOR_COLUMNS)


def outlier_test_of(
    anchor_rows: list[dict[str, Any]], method_values: dict[str, str]
) -> dict[str, Any]:
    """What outlier test the Console ran on the anchors, read from its audit and the method file.

    A Console with MsdialWorkbench#826 writes the anchor columns in LOCAL_SUPPORT_ANCHOR_COLUMNS. Its
    window is the method file's value, or the Console default of 1.5 min where the file has no line;
    a window of 0 leaves the run-wide test only, still floored at the MS1 cycle. Without those
    columns the Console predates #826: it judged every anchor against the median of the file's
    anchors and skipped the test where that MAD was 0, and no window applies. An outlier threshold of
    0 turns the test off in both.
    """
    from .workflow import (
        AUTOMATIC_RT_CORRECTION_CONSOLE_DEFAULT_LOCAL_SUPPORT_RT_WINDOW,
        AUTOMATIC_RT_CORRECTION_DEFAULTS,
    )

    threshold = _finite(method_values.get(OUTLIER_MAD_THRESHOLD_KEY, ""))
    if threshold is None:
        threshold = float(
            AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_outlier_mad_threshold"]
        )
    local_support = has_local_support_columns(anchor_rows)
    window: float | None = None
    window_source = ""
    if local_support:
        window = _finite(method_values.get(LOCAL_SUPPORT_KEY, ""))
        window_source = "method_file"
        if window is None:
            window = AUTOMATIC_RT_CORRECTION_CONSOLE_DEFAULT_LOCAL_SUPPORT_RT_WINDOW
            window_source = "console_default"
    if not threshold > 0:
        test = OUTLIER_TEST_OFF
    elif not local_support:
        test = OUTLIER_TEST_RUN_WIDE_MAD
    elif window is not None and window > 0:
        test = OUTLIER_TEST_LOCAL
    else:
        test = OUTLIER_TEST_RUN_WIDE_FLOORED
    tests: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for row in anchor_rows:
        judged = str(row.get("Outlier test") or "").strip()
        if judged:
            tests[judged] = tests.get(judged, 0) + 1
        status = str(row.get("Status") or "").strip()
        if status in OUTLIER_STATUSES:
            statuses[status] = statuses.get(status, 0) + 1
    return {
        "outlier_test": test,
        "local_support_columns": local_support,
        "local_support_rt_window": window,
        "local_support_rt_window_source": window_source,
        "outlier_mad_threshold": threshold,
        "outlier_tests": tests,
        "outlier_status_counts": statuses,
    }


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def unproven() -> dict[str, Any]:
    """The verdict before any record is read: nothing performed, the evidence missing."""
    return {
        "performed": False,
        "method_key_applied": False,
        "reference_file_id": "",
        "reference_file_name": "",
        "files_audited": 0,
        "selected_anchor_count": 0,
        "corrected_file_count": 0,
        "model_sources": {},
        "reason": "retained_evidence_missing",
        "outlier_test": "",
        "local_support_columns": False,
        "local_support_rt_window": None,
        "local_support_rt_window_source": "",
        "outlier_mad_threshold": None,
        "outlier_tests": {},
        "outlier_status_counts": {},
        "summary_file": SUMMARY,
        "anchors_file": ANCHORS,
        "method_keys_file": METHOD_KEYS,
    }


def automatic_rt_correction_proof(
    root: str | Path,
    summary_rows: Iterable[dict[str, str]] | None = None,
    anchor_rows: Iterable[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Judge whether the retained records prove that this run's alignment corrected RT.

    A workflow setting records intent. It is not evidence that the selected Console understood
    the key or that alignment produced a correction, so this requires all three independent
    records: the method-key audit and both Console-generated TSV files.

    The three must also belong to this run. The method-key record carries the hash of the
    method file the Console read, so a record left from an earlier preparation of the same
    directory does not match the method.txt there now; and an audit TSV older than that record
    was written by an earlier run. Only a file other than the reference, corrected from its own
    detected anchors, shows a correction: the reference's anchors are always marked used, and a
    run in which every other file kept its original RT would otherwise have read as performed.

    A caller that has already parsed the TSVs passes their rows; they are read otherwise.
    """
    root = Path(root)
    summary_path, anchors_path = root / SUMMARY, root / ANCHORS
    method_keys_path, method_path = root / METHOD_KEYS, root / METHOD
    proof = unproven()

    method_keys = read_method_key_record(root)
    if method_keys is None:
        return proof
    try:
        method_digest = hashlib.sha256(method_path.read_bytes()).hexdigest()
    except OSError:
        return proof
    recorded_digest = str(method_keys.get("method_file_sha256") or "").strip().casefold()
    if not recorded_digest or recorded_digest != method_digest:
        proof["reason"] = "method_key_record_not_from_this_method_file"
        return proof
    applied = {method_key_name(item) for item in method_keys.get("applied") or []}
    proof["method_key_applied"] = AUTOMATIC_RT_KEY in applied
    if not proof["method_key_applied"]:
        proof["reason"] = "method_key_not_applied"
        return proof
    # A discarded setting leaves the Console on its default, so the settings in Table S1
    # would not be the ones the correction used. A key also applied was set after all: the
    # Console keeps the last value it applied.
    from .workflow import AUTOMATIC_RT_CORRECTION_METHOD_KEYS

    discarded = sorted(
        (discarded_method_keys(method_keys) - applied) & AUTOMATIC_RT_CORRECTION_METHOD_KEYS
    )
    if discarded:
        proof["reason"] = "method_key_value_discarded_by_console"
        proof["discarded_keys"] = discarded
        return proof
    # A Console without MsdialWorkbench#826 lists the local support window as unrecognised and runs
    # without the local test, while the method file says the window was set.
    unknown = sorted(
        ({method_key_name(item) for item in method_keys.get("unrecognised") or []} - applied)
        & AUTOMATIC_RT_CORRECTION_METHOD_KEYS
    )
    if unknown:
        proof["reason"] = "method_key_not_recognised_by_console"
        proof["discarded_keys"] = unknown
        return proof

    try:
        record_time = method_keys_path.stat().st_mtime
        if min(summary_path.stat().st_mtime, anchors_path.stat().st_mtime) < record_time:
            proof["reason"] = "audit_older_than_method_key_record"
            return proof
        summary_rows = list(summary_rows) if summary_rows is not None else _rows(summary_path)
        anchor_rows = list(anchor_rows) if anchor_rows is not None else _rows(anchors_path)
    except OSError:
        return proof
    if not summary_rows or not anchor_rows:
        proof["reason"] = "retained_evidence_empty"
        return proof

    reference = next(
        (row for row in summary_rows if row.get("Model source") == "Reference"),
        None,
    )
    reference_id = str((reference or {}).get("File ID") or "").strip()
    model_sources: dict[str, int] = {}
    for row in summary_rows:
        source = str(row.get("Model source") or "Unknown")
        model_sources[source] = model_sources.get(source, 0) + 1
    corrected_ids = {
        str(row.get("File ID") or "").strip()
        for row in summary_rows
        if row.get("Model source") == "DetectedAnchors"
        and str(row.get("File ID") or "").strip() != reference_id
    }
    selected_anchor_ids = {
        str(row.get("Anchor ID") or "").strip()
        for row in anchor_rows
        if str(row.get("Used") or "").strip().casefold() == "true"
        and str(row.get("Anchor ID") or "").strip()
        and str(row.get("File ID") or "").strip() in corrected_ids
    }
    performed = reference is not None and bool(corrected_ids) and bool(selected_anchor_ids)
    proof.update(outlier_test_of(anchor_rows, _method_values(method_path)))
    proof.update(
        {
            "performed": performed,
            "reference_file_id": (reference or {}).get("File ID", ""),
            "reference_file_name": (reference or {}).get("File name", ""),
            "files_audited": len(summary_rows),
            "selected_anchor_count": len(selected_anchor_ids),
            "corrected_file_count": len(corrected_ids),
            "model_sources": model_sources,
            "reason": "performed" if performed else "audit_does_not_show_correction",
        }
    )
    return proof


def proof_reason_phrase(proof: dict[str, Any]) -> str:
    phrase = PROOF_REASON_PHRASES.get(str(proof.get("reason")), str(proof.get("reason")))
    return phrase.format(keys=", ".join(proof.get("discarded_keys") or []) or "a setting")

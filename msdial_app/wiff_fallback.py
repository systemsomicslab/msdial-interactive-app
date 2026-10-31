"""A SCIEX .wiff in place of its .wiff2, after an MS-DIAL Console job failed on the .wiff2: the user's decision of 2026-10-31.

WHY. MB-POST MPST000048 (SCIEX ZenoTOF 7600, declared DDA, Ion mode Both) deposits four files per sample: S.wiff2,
S.wiff, S.wiff.scan and S.timeseries.data. The one encoding rule, held jointly with the Catalog
(tests/vectors/encoding_preference.v1.json: "a .wiff2 always wins over the .wiff of the same sample"), ran every
sample from its .wiff2. The positive part's one-file peak-count diagnostic passed, and its production run then hung
three times out of three: in each run one or two files never wrote their .dcl while the others finished annotation,
and the Console sat at 0 % CPU with 37 GB of working set until the watchdog killed it after 21,600 s idle (exit -3).
Which files hung changed from run to run, which points at the .wiff2 reader (RDH Wiff2Reader or the SCIEX Data API)
when files are read in parallel.

THE DECISION (the user, who wrote MS-DIAL, 2026-10-31): 「パーサー自体はWiff1のほうが安定な印象です（パラレルで解析するとき
ですが）…たしかにWiff2で落ちて、かつWiffが存在しているようであればWiffに切り替える、というような動作が適切なのかもしれません。」
The .wiff reader is the more stable one when files are read in parallel; where a run fails on the .wiff2 and the .wiff
exists, switching to the .wiff is the right behaviour. ZT Scan DIA data can only be analysed from the .wiff2.

THE RULE (WIFF_FALLBACK_RULE). The .wiff2 stays the preferred encoding: the shared preference rule, its vectors and the
Catalog are unchanged, and every unit runs on its .wiff2 first. Where a Console job of a unit or split part - its
peak-count diagnostic or its production run - failed or timed out (FAILED_ATTEMPT), and

- the unit is DDA: its project's acquisition mode, its applied campaign disposition's type and the type decided or
  written for every .wiff2 it analyses say DDA. A DIA, SWATH or AIF .wiff2 unit is never switched (ZT Scan DIA);
- every input it analyses that is a .wiff2 has, beside it in its folder, the .wiff of the same sample (S.wiff2 ->
  S.wiff) with that .wiff's .wiff.scan beside it. A .wiff without its .wiff.scan is no candidate: the Console cannot
  open it (raw_metadata_preflight.missing_vendor_companion, vendor_companion_missing since 0.5.53). It is all or
  nothing: a unit some of whose .wiff2 have no such .wiff is not switched, so one run never mixes the two readers;
- each such .wiff was downloaded as its own repository object (a download record names it, with its checksums), so
  its lineage row says where its bytes came from, as every input's does;

its next attempt runs on those .wiff instead. The switch happens once per unit or part (a unit that records one is
refused, already_switched), and it is recorded with its evidence in manifest.encoding_fallback (RECORD_SCHEMA): the
rule, the failed job(s) with how each ended as the unit's run_attempts record it, every file swapped from and to
(the .wiff2, the .wiff and its .wiff.scan, absolute and under the data root), and the input candidates before the
switch, so the swaps can be checked to be exactly what changed.

WHAT THE SWITCH CHANGES. input_candidates name the .wiff in each .wiff2's place, in the same position. Each .wiff2's
input_lineage row is replaced by a row of its .wiff: the .wiff's own download record (kind file, source and
checksums), the .wiff2 row's sample and pairing, and encoding_fallback {rule, in_place_of: the .wiff2, stands_for: the
file whose naming gives the input its sample, in_place_of_row: the .wiff2's row unchanged}. The .wiff2's
encoding_choice (the one encoding rule's record) stays on that kept row and in manifest.encoding_choices: the rule's
decision is not rewritten, and the fallback says why another file runs. Nothing else is touched - not the raw-header
preflight, not the campaign disposition, not the Class proposal - and the readers that ask which type, header or
sample an input has read a .wiff through the .wiff2 it stands in for (fallback_sources): the analysis-CSV builder,
the execution gate's decided types, the declared-input matching (lineage_stands_for).

WHAT IS MADE AGAIN. Everything that depends on the input list: the analysis CSV and the reviewed metadata
(msdial_prepare_repository_reanalysis), the peak-count diagnostic and its threshold, measured on the files that now
run, and the run bundle. The campaign runner resumes such a unit at its metadata preparation (scripts/campaign in
msdial-public-reanalysis); a person runs the same steps in that order.

WHAT IT NEVER DOES. It copies, moves and deletes no file, starts no Console, and switches no unit whose run finished,
whose raw data were released, that is a split parent, or whose Console may still be running.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

WIFF_FALLBACK_RULE = "wiff2_to_wiff_after_console_failure_2026_10_31"
RECORD_SCHEMA = "msdial-encoding-fallback.v1"
# Where the switch is recorded: in the unit manifest, and on each .wiff's lineage row.
FALLBACK_FIELD = "encoding_fallback"
# The warning a switched unit carries, so the analysis CSV and the reports say it ran on its .wiff.
WIFF_FALLBACK_WARNING = "encoding_fallback_wiff2_to_wiff"
ENTRY_POINT = "msdial_switch_repository_unit_to_wiff"
# The campaign boundary a switch is made under: it changes what the unit's next production run reads.
BOUNDARY = 4
FROM_SUFFIX = ".wiff2"
TO_SUFFIX = ".wiff"
DECISION = (
    "The user's decision of 2026-10-31: the .wiff reader is the more stable one when files are read in parallel, "
    "so where a Console job fails on the .wiff2 and the .wiff exists, the unit's next attempt runs on the .wiff. "
    "DDA only: ZT Scan DIA data can only be analysed from the .wiff2."
)
# How a run attempt that the switch answers ended (repository_reanalysis.record_run_end's reasons): stopped for time,
# or ended in an error or a non-zero exit. A cancel, a Console that never started, and a .wiff.scan the run's own
# validation found missing are no reader failure.
STOPPED_FOR_TIME = frozenset({"timeout", "idle_timeout"})
NO_READER_FAILURE = frozenset({"cancelled", "start_failed", "sciex_scan_sidecar"})
ATTEMPT_KINDS = {"tuning": "diagnostic", "run": "production"}
CONSOLE_ACQUISITION_TYPES = ("DDA", "SWATH", "AIF")

# Why a switch is refused (WiffFallbackRefused.reason).
ALREADY_SWITCHED = "already_switched"
NO_CONSOLE_FAILURE = "no_console_failure"
NO_WIFF2_INPUT = "no_wiff2_input"
NOT_DDA = "not_dda"
WIFF_NOT_BESIDE = "wiff_not_beside"
WIFF_SCAN_MISSING = "wiff_scan_missing"
WIFF_ALREADY_AN_INPUT = "wiff_already_an_input"
WIFF_PROVENANCE_UNRECORDED = "wiff_provenance_unrecorded"
UNIT_NOT_SWITCHABLE = "unit_not_switchable"
RUN_IN_PROGRESS = "run_in_progress"


class WiffFallbackRefused(ValueError):
    """The switch's conditions do not hold for this unit: nothing was written. ``reason`` is one of the codes above."""

    def __init__(self, reason: str, detail: str, **evidence: Any) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.evidence = evidence


def _key(path_text: str) -> str:
    return str(Path(str(path_text)).resolve()).casefold()


def fallback_record(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    """The unit's encoding_fallback, or {} where it records none (or one this module does not read)."""
    record = (manifest or {}).get(FALLBACK_FIELD)
    if not isinstance(record, Mapping) or record.get("rule") != WIFF_FALLBACK_RULE:
        return {}
    return dict(record)


def fallback_sources(manifest: Mapping[str, Any] | None) -> dict[str, str]:
    """{_file_key of each .wiff that runs in place of a .wiff2: that .wiff2's path}, from manifest.encoding_fallback;
    {} for a unit that records none. What a reader asks of an input's header, decided type or sample, it asks of a
    .wiff through the .wiff2 it stands in for: the two are one acquisition."""
    result: dict[str, str] = {}
    for swap in fallback_record(manifest).get("swaps") or []:
        if isinstance(swap, Mapping) and str(swap.get("from") or "").strip() and str(swap.get("to") or "").strip():
            result[_key(str(swap["to"]))] = str(swap["from"])
    return result


def row_stands_for(row: Mapping[str, Any]) -> str:
    """The file whose naming gives a .wiff's lineage row its sample: the stands_for its encoding_fallback records (the
    .wiff2's own stands_for where the one encoding rule paired its sample through another file, else the .wiff2), or
    '' for a row that is no fallback's."""
    fallback = row.get(FALLBACK_FIELD) if isinstance(row, Mapping) else None
    if not isinstance(fallback, Mapping) or fallback.get("rule") != WIFF_FALLBACK_RULE:
        return ""
    return str(fallback.get("stands_for") or fallback.get("in_place_of") or "").strip()


def failed_attempt(attempt: Mapping[str, Any]) -> str:
    """Why a closed Console run attempt is a failure the switch answers ('' where it is none): stopped for time
    (timeout, idle_timeout), an error, or an exit other than 0. An attempt still open, a cancel, a Console that never
    started and a missing .wiff.scan are none."""
    if not attempt.get("ended_at"):
        return ""
    reason = str(attempt.get("reason") or "").strip()
    exit_code = attempt.get("exit_code")
    if reason in STOPPED_FOR_TIME:
        return reason
    if reason in NO_READER_FAILURE:
        return ""
    if reason == "error":
        return "error"
    if exit_code not in (None, 0):
        return f"exit_code_{exit_code}"
    return ""


def _relative(path: Path, root: Path | None) -> str:
    if root is not None:
        for base, item in ((root, path), (root.resolve(), path.resolve())):
            try:
                return item.relative_to(base).as_posix()
            except ValueError:
                continue
    return path.name


def _beside(path: Path, name: str) -> Path | None:
    """The file of this name, compared without case, in the folder of ``path``; None where there is none."""
    wanted = name.casefold()
    try:
        entries = list(path.parent.iterdir())
    except OSError:
        return None
    return next((entry for entry in entries if entry.name.casefold() == wanted and entry.is_file()), None)


def _download_of(manifest: Mapping[str, Any], path: Path) -> dict[str, Any] | None:
    """The download record of a file fetched as its own repository object (no archive), or None."""
    key = _key(str(path))
    for item in manifest.get("downloads") or []:
        if isinstance(item, Mapping) and not item.get("archive") and str(item.get("path") or "").strip() \
                and _key(str(item["path"])) == key:
            return dict(item)
    return None


def _declared_names(manifest: Mapping[str, Any], relative: str) -> list[str]:
    """The names the unit's file listing gives a file under the data root, by the forms the lease compares
    (repository_reanalysis.build_input_lineage: the path, less a leading FILES/, less its first folder)."""
    from .repository_reanalysis import _safe_relative_name

    text = relative.casefold()
    parts = text.split("/")
    forms = {text}
    if text.startswith("files/"):
        forms.add(text[6:])
    if len(parts) > 1:
        forms.add("/".join(parts[1:]))
    names = set()
    for item in (manifest.get("project") or {}).get("files") or []:
        name = str((item or {}).get("name") or "") if isinstance(item, Mapping) else ""
        if not name:
            continue
        try:
            listed = _safe_relative_name(name).as_posix().casefold()
        except ValueError:
            continue
        if listed in forms:
            names.add(name)
    return sorted(names)


def _acquisition_problem(manifest: Mapping[str, Any], wiff2_keys: Iterable[str]) -> str:
    """Why the unit is not DDA for the switch ('' where it is): its project's mode, its applied disposition's type and
    the type decided or written for each .wiff2 it analyses must each say DDA where they say anything."""
    from .repository_reanalysis import _applied_disposition, _decided_acquisition_by_file, _written_acquisition_by_input

    project = manifest.get("project") or {}
    mode = str(project.get("acquisition_mode") or "").strip()
    if mode.upper() != "DDA":
        return (f"the unit's acquisition mode is {mode or 'not recorded'}, not DDA; a DIA, SWATH or AIF .wiff2 is never "
                "switched (ZT Scan DIA data can only be analysed from the .wiff2)")
    applied = _applied_disposition(dict(manifest))
    unit_type = str(applied.get("console_acquisition_type") or "").strip().upper()
    if unit_type and unit_type != "DDA":
        return f"its applied campaign disposition runs it as {unit_type}, not DDA"
    decided = _decided_acquisition_by_file(dict(manifest))
    written = _written_acquisition_by_input(dict(manifest))
    others = sorted({value for key in wiff2_keys for value in (decided.get(key), written.get(key))
                     if value and str(value).upper() != "DDA"})
    if others:
        return f"some of its .wiff2 inputs are decided or written as {', '.join(others)}, not DDA"
    return ""


def plan_wiff_fallback(manifest: Mapping[str, Any], failed_job_ids: Iterable[str]) -> dict[str, Any]:
    """What the switch would do for a unit manifest as it stands, or WiffFallbackRefused with the reason it would not.

    Returns {failed_jobs, swaps, input_candidates_before, input_candidates_after, acquisition_mode}. Reads the
    manifest and the unit's raw folders; writes nothing."""
    from .repository_reanalysis import (
        RAW_RELEASED_STATUSES,
        SPLIT_PARENT_STATUS,
        _campaign_excluded_inputs,
        _live_run_attempt_in,
        finished_production_run,
    )
    from .raw_metadata_preflight import missing_vendor_companion

    manifest = dict(manifest)
    unit = str((manifest.get("project") or {}).get("analysis_unit_id") or "")
    if fallback_record(manifest):
        raise WiffFallbackRefused(
            ALREADY_SWITCHED,
            f"Unit {unit} already switched from its .wiff2 to its .wiff ({FALLBACK_FIELD}); the switch is made once.",
            encoding_fallback=fallback_record(manifest),
        )
    status = str(manifest.get("status") or "")
    if status == SPLIT_PARENT_STATUS or status in RAW_RELEASED_STATUSES or finished_production_run(manifest) is not None:
        raise WiffFallbackRefused(
            UNIT_NOT_SWITCHABLE,
            f"Unit {unit} is {status or 'of no recorded status'}: a split parent never runs, and a unit whose run "
            "finished or whose raw data were released is not run again in place.",
        )
    if _live_run_attempt_in(manifest) is not None:
        raise WiffFallbackRefused(RUN_IN_PROGRESS, f"A Console of unit {unit} may still be running; nothing was switched.")

    attempts = [item for item in manifest.get("run_attempts") or [] if isinstance(item, Mapping)]
    jobs = [str(item).strip() for item in failed_job_ids or [] if str(item).strip()]
    failed: list[dict[str, Any]] = []
    unanswered: list[str] = []
    for job in dict.fromkeys(jobs):
        found = [item for item in attempts if str(item.get("job_id") or "") == job]
        why = failed_attempt(found[-1]) if found else ""
        if not why:
            unanswered.append(job if found else f"{job} (no run attempt of this unit)")
            continue
        attempt = found[-1]
        failed.append({
            "job_id": job,
            "kind": str(attempt.get("kind") or ""),
            "console_job": ATTEMPT_KINDS.get(str(attempt.get("kind") or ""), str(attempt.get("kind") or "")),
            "failure": why,
            "reason": attempt.get("reason"),
            "exit_code": attempt.get("exit_code"),
            "attempt": attempt.get("attempt"),
            "started_at": attempt.get("started_at"),
            "ended_at": attempt.get("ended_at"),
        })
    if not jobs or unanswered:
        raise WiffFallbackRefused(
            NO_CONSOLE_FAILURE,
            "The switch answers a Console job of this unit that failed or timed out, as its run_attempts record it; "
            + (f"none of these is one: {', '.join(unanswered)}." if unanswered else "no job was named."),
        )

    candidates = [str(item) for item in manifest.get("input_candidates") or [] if str(item).strip()]
    excluded = _campaign_excluded_inputs(manifest)
    analysed = [item for item in candidates if _key(item) not in excluded]
    wiff2 = [item for item in analysed if item.rstrip("\\/").casefold().endswith(FROM_SUFFIX) and Path(item).is_file()]
    if not wiff2:
        raise WiffFallbackRefused(NO_WIFF2_INPUT, f"Unit {unit} analyses no .wiff2 file; there is nothing to switch.")
    problem = _acquisition_problem(manifest, [_key(item) for item in wiff2])
    if problem:
        raise WiffFallbackRefused(NOT_DDA, f"Unit {unit} is not switched: {problem}.")

    data_root = Path(str(manifest.get("input_directory") or "")) if str(manifest.get("input_directory") or "") else None
    candidate_keys = {_key(item) for item in candidates}
    lineage = manifest.get("input_lineage") if isinstance(manifest.get("input_lineage"), Mapping) else {}
    rows = {_key(str(row.get("path") or "")): row for row in lineage.get("rows") or [] if isinstance(row, Mapping)}
    missing: list[str] = []
    without_scan: list[str] = []
    already: list[str] = []
    unrecorded: list[str] = []
    swaps: list[dict[str, Any]] = []
    for item in wiff2:
        source = Path(item)
        target = _beside(source, source.name[: -len(FROM_SUFFIX)] + TO_SUFFIX)
        if target is None:
            missing.append(source.name)
            continue
        companion = missing_vendor_companion(str(target))
        if companion:
            without_scan.append(f"{target.name} (no {companion} beside it)")
            continue
        if _key(str(target)) in candidate_keys:
            already.append(target.name)
            continue
        download = _download_of(manifest, target)
        if download is None:
            unrecorded.append(target.name)
            continue
        scan = _beside(target, target.name + ".scan")
        row = rows.get(_key(item)) or {}
        swaps.append({
            "from": str(source),
            "to": str(target),
            "companion": str(scan) if scan is not None else "",
            "from_relative": _relative(source, data_root),
            "to_relative": _relative(target, data_root),
            "sample_id": str(row.get("sample_id") or ""),
        })
    if missing:
        raise WiffFallbackRefused(
            WIFF_NOT_BESIDE,
            f"{len(missing)} of unit {unit}'s {len(wiff2)} .wiff2 input(s) have no .wiff of the same sample beside them "
            f"(the first is {missing[0]}); a unit is switched whole or not at all.",
            inputs=missing[:20],
        )
    if without_scan:
        raise WiffFallbackRefused(
            WIFF_SCAN_MISSING,
            f"{len(without_scan)} .wiff beside unit {unit}'s .wiff2 have no .wiff.scan, which the Console needs to open "
            f"them (the first is {without_scan[0]}); a .wiff without its .wiff.scan is no fallback.",
            inputs=without_scan[:20],
        )
    if already:
        raise WiffFallbackRefused(
            WIFF_ALREADY_AN_INPUT,
            f"{len(already)} .wiff beside unit {unit}'s .wiff2 are inputs of the unit already (the first is "
            f"{already[0]}); switching would analyse one sample twice.",
            inputs=already[:20],
        )
    if unrecorded:
        raise WiffFallbackRefused(
            WIFF_PROVENANCE_UNRECORDED,
            f"{len(unrecorded)} .wiff beside unit {unit}'s .wiff2 were not downloaded as repository objects of their "
            f"own (the first is {unrecorded[0]}), so no record says where their bytes came from.",
            inputs=unrecorded[:20],
        )
    by_source = {_key(swap["from"]): swap["to"] for swap in swaps}
    return {
        "failed_jobs": failed,
        "swaps": swaps,
        "input_candidates_before": candidates,
        "input_candidates_after": [by_source.get(_key(item), item) for item in candidates],
        "acquisition_mode": "DDA",
    }


def _fallback_row(manifest: Mapping[str, Any], old: Mapping[str, Any], swap: Mapping[str, Any]) -> dict[str, Any]:
    """The lineage row of a .wiff that runs in place of a .wiff2: its own download's source and checksums, the .wiff2
    row's sample and pairing, and the record of the switch with the .wiff2's row unchanged."""
    target = Path(str(swap["to"]))
    download = _download_of(manifest, target) or {}
    choice = old.get("encoding_choice") if isinstance(old.get("encoding_choice"), Mapping) else {}
    stand = str(choice.get("stands_for") or "").strip() or str(old.get("path") or swap["from"])
    verified = bool(download.get("declared_checksum_verified"))
    row: dict[str, Any] = {
        "path": str(target),
        "kind": "file",
        "declared_names": _declared_names(manifest, str(swap["to_relative"])) or list(old.get("declared_names") or []),
        "sample_id": str(old.get("sample_id") or ""),
        "file_name": "",
        "source": {"url": download.get("source_url", ""), "download_path": download.get("path", "")},
        "checksums": {
            "sha256": download.get("sha256", ""),
            "md5": download.get("md5", ""),
            "declared": download.get("declared_checksum", ""),
            "declared_algorithm": "md5" if verified else "",
            "declared_verified": True if verified else None,
        },
    }
    for key in ("name_pairing", "sample_row"):
        if key in old:
            row[key] = copy.deepcopy(old[key])
    row[FALLBACK_FIELD] = {
        "rule": WIFF_FALLBACK_RULE,
        "in_place_of": str(old.get("path") or swap["from"]),
        "stands_for": stand,
        "in_place_of_row": copy.deepcopy(dict(old)),
    }
    return row


def fallback_change(plan: Mapping[str, Any], *, crossing: Mapping[str, Any] | None = None, at: str = "") -> Any:
    """The manifest change that makes a planned switch: input_candidates, the lineage rows, the record, the warning
    and any campaign crossing, in one write. It plans again from the manifest as it is under the writer lock and
    refuses (WiffFallbackRefused) where that differs from ``plan``: another writer switched the unit, or its files
    moved, since the preview."""
    at = at or datetime.now(timezone.utc).isoformat()
    job_ids = [item["job_id"] for item in plan["failed_jobs"]]

    def change(manifest: dict[str, Any]) -> None:
        again = plan_wiff_fallback(manifest, job_ids)
        if [(item["from"], item["to"]) for item in again["swaps"]] != [(item["from"], item["to"]) for item in plan["swaps"]]:
            raise WiffFallbackRefused(UNIT_NOT_SWITCHABLE, "The unit's inputs changed since the switch was planned.")
        by_source = {_key(swap["from"]): swap for swap in again["swaps"]}
        manifest["input_candidates"] = list(again["input_candidates_after"])
        lineage = manifest.get("input_lineage")
        if isinstance(lineage, dict):
            lineage["rows"] = [
                _fallback_row(manifest, row, by_source[_key(str(row.get("path") or ""))])
                if isinstance(row, dict) and _key(str(row.get("path") or "")) in by_source else row
                for row in lineage.get("rows") or []
            ]
        record: dict[str, Any] = {
            "schema": RECORD_SCHEMA,
            "rule": WIFF_FALLBACK_RULE,
            "decision": DECISION,
            "switched_at": at,
            "acquisition_mode": again["acquisition_mode"],
            "from_suffix": FROM_SUFFIX,
            "to_suffix": TO_SUFFIX,
            "failed_jobs": again["failed_jobs"],
            "swaps": again["swaps"],
            "input_candidates_before": again["input_candidates_before"],
        }
        if crossing:
            record["campaign_authorization"] = {
                key: crossing.get(key) for key in ("approval_id", "campaign_id", "boundary", "unit_id", "entry_point",
                                                    "validated_at")
            }
            manifest["campaign_authorizations"] = [*(manifest.get("campaign_authorizations") or []), dict(crossing)]
        manifest[FALLBACK_FIELD] = record
        warnings = list(manifest.get("warnings") or [])
        if WIFF_FALLBACK_WARNING not in warnings:
            manifest["warnings"] = [*warnings, WIFF_FALLBACK_WARNING]

    return change


def switch_to_wiff(
    manifest_path: str | Path, failed_job_ids: Iterable[str], *, crossing: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Make the switch in the unit manifest at ``manifest_path``: the record (manifest.encoding_fallback) as written.
    Raises WiffFallbackRefused, writing nothing, where the conditions do not hold."""
    from .repository_reanalysis import read_manifest, update_manifest

    path = Path(manifest_path).resolve()
    plan = plan_wiff_fallback(read_manifest(path), failed_job_ids)
    written = update_manifest(path, fallback_change(plan, crossing=crossing))
    return fallback_record(written)


def swap_summary(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """The swaps of a record as a report names them: under the data root, never an absolute path."""
    return [
        {"from": str(item.get("from_relative") or Path(str(item.get("from") or "")).name),
         "to": str(item.get("to_relative") or Path(str(item.get("to") or "")).name),
         "sample_id": str(item.get("sample_id") or "")}
        for item in record.get("swaps") or [] if isinstance(item, Mapping)
    ]

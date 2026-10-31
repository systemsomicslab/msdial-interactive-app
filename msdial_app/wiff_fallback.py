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
  its lineage row says where its bytes came from, as every input's does. A split part downloads nothing of its own:
  its raw owner's (the parent's) download records are read, as its raw tree is the parent's;
- each such .wiff's own raw header, read at the switch by the pinned raw-metadata extractor as the preflight runs it
  (read_wiff_headers), can be read, holds one sample, gives DDA, and states the polarity the unit runs in (its ion
  mode, or for a unit that names none, the polarity recorded for its .wiff2). The header is direct evidence of the file
  that will run, where the .wiff2's (unreadable for every ZenoTOF .wiff2) was not; it is recorded beside the switch
  (encoding_fallback.wiff_headers), never in raw_metadata_preflight.per_file, so the disposition's decided types stay as
  the disposition decided them and the .wiff2's disposition remains the decision record;

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
run, and the run bundle. The switch marks what stands of them as superseded (encoding_fallback.superseded: the
analysis CSV record, whose status becomes superseded, the run bundle prepared from it and the latest diagnostic), so
nothing reads them as the unit's own. The campaign runner resumes such a unit at its metadata preparation (scripts/campaign in
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
WIFF2_NOT_A_FILE = "wiff2_not_a_file"
# A .wiff whose own header does not support the switch: unreadable, a sample count other than one, not DDA, or another polarity.
WIFF_HEADER_REFUSED = "wiff_header_refused"
# Where the extractor's reads of the .wiff headers are written, under the unit's provenance.
HEADER_READS_DIRECTORY = "wiff-fallback-header-reads"


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


def raw_owner(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """A split part's raw owner (its parent: raw_owned_by, else split_from.manifest_path), read; {} for a unit that
    owns its raw tree, or an owner that cannot be read."""
    from .repository_reanalysis import read_manifest

    owner = str(manifest.get("raw_owned_by") or "").strip() or str(
        (manifest.get("split_from") or {}).get("manifest_path") or "" if isinstance(manifest.get("split_from"), Mapping)
        else "").strip()
    if not owner:
        return {}
    try:
        value = read_manifest(Path(owner))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _downloads(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The download records that vouch for the files of a unit's raw tree: its own, and a split part's raw owner's (a
    part downloads nothing of its own; repository_reanalysis._split_unit_locked writes it no downloads)."""
    found = [dict(item) for item in manifest.get("downloads") or [] if isinstance(item, Mapping)]
    if isinstance(manifest.get("split_from"), Mapping) or str(manifest.get("raw_owned_by") or "").strip():
        found += [dict(item) for item in raw_owner(manifest).get("downloads") or [] if isinstance(item, Mapping)]
    return found


def _download_of(manifest: Mapping[str, Any], path: Path, downloads: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The download record of a file fetched as its own repository object (no archive), or None."""
    key = _key(str(path))
    for item in _downloads(manifest) if downloads is None else downloads:
        if not item.get("archive") and str(item.get("path") or "").strip() and _key(str(item["path"])) == key:
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
    if manifest.get(FALLBACK_FIELD) is not None:
        # Under any rule, as the gate reads it: one switch per unit, and a record this module does not read is not
        # written over.
        raise WiffFallbackRefused(
            ALREADY_SWITCHED,
            f"Unit {unit} already records a switch of its encoding ({FALLBACK_FIELD}); the switch is made once.",
            encoding_fallback=manifest.get(FALLBACK_FIELD),
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
    wiff2 = [item for item in analysed if item.rstrip("\\/").casefold().endswith(FROM_SUFFIX)]
    if not wiff2:
        raise WiffFallbackRefused(NO_WIFF2_INPUT, f"Unit {unit} analyses no .wiff2 file; there is nothing to switch.")
    absent = [Path(item).name for item in wiff2 if not Path(item).is_file()]
    if absent:
        # Never dropped in silence: a .wiff2 that is no file would be left out of the switch, and the unit would run
        # one sample short, or on two readers.
        raise WiffFallbackRefused(
            WIFF2_NOT_A_FILE,
            f"{len(absent)} .wiff2 input(s) of unit {unit} are no file on disk (the first is {absent[0]}); the switch is "
            "of the whole unit or nothing.",
            inputs=absent[:20],
        )
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
    downloads = _downloads(manifest)
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
        download = _download_of(manifest, target, downloads)
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


def _fallback_row(
    manifest: Mapping[str, Any], old: Mapping[str, Any], swap: Mapping[str, Any],
    downloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The lineage row of a .wiff that runs in place of a .wiff2: its own download's source and checksums (a split
    part's from its raw owner's records), the .wiff2 row's sample and pairing, and the record of the switch with the
    .wiff2's row unchanged."""
    target = Path(str(swap["to"]))
    download = _download_of(manifest, target, downloads) or {}
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


HeaderReader = Any  # (extractor path, list of paths, work directory) -> {_key: {"outcome", "record", "extractor_sha256"}}


def read_wiff_headers(
    extractor_path: str | Path, paths: Iterable[str | Path], work_directory: str | Path, *, runner: Any = None
) -> dict[str, dict[str, Any]]:
    """Each .wiff's own raw header, read by the raw-metadata extractor as the preflight runs it
    (raw_metadata_preflight.run_extractor: bounded chunks, per-input time limits), by _key: {"outcome", "record"
    (the extractor's record, or None), "extractor_sha256"}. Never raises for what the extractor does."""
    from .raw_metadata_preflight import file_key, run_extractor
    from .repository_reanalysis import raw_metadata_extractor_identity

    extractor = Path(extractor_path).resolve()
    sha256 = str(raw_metadata_extractor_identity(extractor).get("binary_sha256") or "")
    inputs = [Path(item) for item in paths]
    execution = run_extractor(extractor, inputs, Path(work_directory), extractor_sha256=sha256, runner=runner)
    records: dict[str, dict[str, Any]] = {}
    for record in execution.get("records") or []:
        source = (record.get("source") or {}) if isinstance(record, Mapping) else {}
        if str(source.get("filePath") or "").strip():
            records[_key(str(source["filePath"]))] = dict(record)
    outcomes = execution.get("outcomes") or {}
    return {
        _key(str(path)): {"outcome": str((outcomes.get(file_key(path)) or {}).get("outcome") or ""),
                          "record": records.get(_key(str(path))), "extractor_sha256": sha256}
        for path in inputs
    }


def _expected_polarity(manifest: Mapping[str, Any], source: str) -> str:
    """The polarity a .wiff must state to run in place of ``source``: the unit's ion mode where it names one, else the
    polarity recorded for that .wiff2 (its header record, or a file-name inference the disposition or the split
    recorded); '' where nothing records one."""
    mode = str((manifest.get("project") or {}).get("ion_mode") or "").strip().capitalize()
    if mode in ("Positive", "Negative"):
        return mode
    key = _key(source)
    summary = (manifest.get("raw_metadata_preflight") or {}).get("summary") or {}
    for item in [*(summary.get("per_file") or []), *(manifest.get("header_verdicts_from_parent") or [])]:
        if isinstance(item, Mapping) and _key(str(item.get("file") or "")) == key:
            value = str(item.get("polarity") or "").strip()
            if value in ("Positive", "Negative"):
                return value
    disposition = manifest.get("campaign_disposition") if isinstance(manifest.get("campaign_disposition"), Mapping) else {}
    for item in [*(manifest.get("polarity_inferences") or []), *(disposition.get("polarity_inferences") or [])]:
        if isinstance(item, Mapping) and _key(str(item.get("path") or "")) == key:
            value = str(item.get("polarity") or "").strip()
            if value in ("Positive", "Negative"):
                return value
    return ""


def wiff_headers(
    manifest: Mapping[str, Any], plan: Mapping[str, Any], reads: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The .wiff headers the switch rests on, as encoding_fallback.wiff_headers records them, or WiffFallbackRefused
    (WIFF_HEADER_REFUSED) where one does not support it: a header the extractor could not read, one that lists other
    than exactly one sample (none, several, or no samples at all), one whose acquisition is not DDA (IDA), or one that
    states another polarity than the unit runs in.

    Each entry: the .wiff and the .wiff2 it stands in for (under the data root), the read's outcome, the polarity and
    the one expected, the header's acquisition mode and the Console type it gives, the MS levels, the sample count
    (None where the record lists no samples, which refuses the switch as a count other than one does), the acquisition
    start time, the reader and the extractor's sha256."""
    from .raw_metadata_preflight import READ_OUTCOMES
    from .repository_reanalysis import _summarize_raw_metadata

    unit = str((manifest.get("project") or {}).get("analysis_unit_id") or "")
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    for swap in plan["swaps"]:
        read = reads.get(_key(str(swap["to"]))) or {}
        record = read.get("record") if isinstance(read.get("record"), Mapping) else None
        name = Path(str(swap["to"])).name
        outcome = str(read.get("outcome") or "")
        if outcome not in READ_OUTCOMES or record is None:
            problems.append(f"{name}: its header could not be read ({outcome or 'not read'})")
            continue
        summary = (_summarize_raw_metadata([dict(record)]).get("per_file") or [{}])[0]
        samples = record.get("samples")
        count = len(samples) if isinstance(samples, list) else None
        expected = _expected_polarity(manifest, str(swap["from"]))
        polarity = str(summary.get("polarity") or "")
        console = str(summary.get("header_console_acquisition_type") or "")
        entries.append({
            "file": str(swap["to"]),
            "file_relative": str(swap["to_relative"]),
            "in_place_of": str(swap["from"]),
            "in_place_of_relative": str(swap["from_relative"]),
            "outcome": outcome,
            "polarity": polarity,
            "expected_polarity": expected,
            "acquisition_mode": str(summary.get("acquisition_mode") or ""),
            "header_console_acquisition_type": console or None,
            "ms_levels": summary.get("ms_levels"),
            "sample_count": count,
            "acquisition_start_time": str(summary.get("acquisition_start_time") or ""),
            "reader": str(summary.get("reader") or ""),
            "extractor_sha256": str(read.get("extractor_sha256") or ""),
        })
        if count != 1:
            # Exactly one (review of #88, low 1): a header that lists no sample, or none at all, does not show that the
            # file is the one injection its .wiff2 was, any more than one that lists several.
            problems.append(f"{name}: its header lists {'no sample count' if count is None else f'{count} samples'}, "
                            "where a sample's input is exactly one injection")
        if console != "DDA":
            problems.append(f"{name}: its header gives {console or 'no'} Console acquisition type "
                            f"({summary.get('acquisition_mode') or 'no acquisition mode'}), not DDA")
        if polarity not in ("Positive", "Negative"):
            problems.append(f"{name}: its header states no single polarity ({polarity or 'none'})")
        elif not expected:
            problems.append(f"{name}: nothing records the polarity its .wiff2 runs in, to compare its header's with")
        elif polarity != expected:
            problems.append(f"{name}: its header states {polarity}, and the unit runs in {expected}")
    if problems:
        raise WiffFallbackRefused(
            WIFF_HEADER_REFUSED,
            f"Unit {unit} is not switched: the .wiff headers do not support it ({'; '.join(problems[:5])}).",
            problems=problems[:20], wiff_headers=entries[:20],
        )
    return entries


def _superseded(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """What stands of the unit's input-list-dependent artifacts, which the switch supersedes: the analysis CSV record,
    the run bundle prepared from it (the output's run-manifest.json, where it exists) and the latest diagnostic."""
    found: dict[str, Any] = {}
    csv = manifest.get("analysis_csv") if isinstance(manifest.get("analysis_csv"), Mapping) else None
    if csv:
        found["analysis_csv"] = {"path": str(csv.get("path") or ""), "status": str(csv.get("status") or "")}
    output = str(manifest.get("output_directory") or "").strip()
    if output and (Path(output) / "run-manifest.json").is_file():
        found["prepared_run"] = {"run_manifest": str(Path(output) / "run-manifest.json")}
    diagnostics = [item for item in manifest.get("peak_height_diagnostics") or [] if isinstance(item, Mapping)]
    if diagnostics:
        last = diagnostics[-1]
        found["diagnostic"] = {"job_id": str(last.get("job_id") or ""),
                               "minimum_peak_height": last.get("minimum_peak_height"),
                               "recorded_at": last.get("recorded_at")}
    return found


def fallback_change(
    plan: Mapping[str, Any],
    *,
    crossing: Mapping[str, Any] | None = None,
    at: str = "",
    headers: list[dict[str, Any]] | None = None,
    extractor: Mapping[str, Any] | None = None,
) -> Any:
    """The manifest change that makes a planned switch: input_candidates, the lineage rows, the record (with the .wiff
    headers it rests on, ``headers``, and the extractor that read them), the superseded artifacts, the warning and any
    campaign crossing, in one write. It plans again from the manifest as it is under the writer lock and refuses
    (WiffFallbackRefused) where that differs from ``plan``: another writer switched the unit, or its files moved, since
    the preview."""
    at = at or datetime.now(timezone.utc).isoformat()
    job_ids = [item["job_id"] for item in plan["failed_jobs"]]

    def change(manifest: dict[str, Any]) -> None:
        again = plan_wiff_fallback(manifest, job_ids)
        if [(item["from"], item["to"]) for item in again["swaps"]] != [(item["from"], item["to"]) for item in plan["swaps"]]:
            raise WiffFallbackRefused(UNIT_NOT_SWITCHABLE, "The unit's inputs changed since the switch was planned.")
        by_source = {_key(swap["from"]): swap for swap in again["swaps"]}
        downloads = _downloads(manifest)
        superseded = _superseded(manifest)
        manifest["input_candidates"] = list(again["input_candidates_after"])
        lineage = manifest.get("input_lineage")
        if isinstance(lineage, dict):
            lineage["rows"] = [
                _fallback_row(manifest, row, by_source[_key(str(row.get("path") or ""))], downloads)
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
            "wiff_headers": list(headers or []),
            **({"extractor": dict(extractor)} if extractor else {}),
            "superseded": superseded,
        }
        if crossing:
            record["campaign_authorization"] = {
                key: crossing.get(key) for key in ("approval_id", "campaign_id", "boundary", "unit_id", "entry_point",
                                                    "validated_at")
            }
            manifest["campaign_authorizations"] = [*(manifest.get("campaign_authorizations") or []), dict(crossing)]
        manifest[FALLBACK_FIELD] = record
        mark = {"rule": WIFF_FALLBACK_RULE, "switched_at": at}
        if isinstance(manifest.get("analysis_csv"), dict):
            # Its rows name the .wiff2: superseded, so the execution gate holds no row to a type written for them.
            manifest["analysis_csv"]["status"] = "superseded"
            manifest["analysis_csv"]["superseded_by_encoding_fallback"] = mark
        diagnostics = manifest.get("peak_height_diagnostics")
        if isinstance(diagnostics, list) and diagnostics and isinstance(diagnostics[-1], dict):
            diagnostics[-1]["superseded_by_encoding_fallback"] = mark
        warnings = list(manifest.get("warnings") or [])
        if WIFF_FALLBACK_WARNING not in warnings:
            manifest["warnings"] = [*warnings, WIFF_FALLBACK_WARNING]

    return change


def switch_to_wiff(
    manifest_path: str | Path,
    failed_job_ids: Iterable[str],
    *,
    extractor_path: str | Path,
    crossing: Mapping[str, Any] | None = None,
    extractor: Mapping[str, Any] | None = None,
    reader: Any = None,
) -> dict[str, Any]:
    """Make the switch in the unit manifest at ``manifest_path``: the record (manifest.encoding_fallback) as written.
    The .wiff headers are read with ``extractor_path`` (read_wiff_headers, or ``reader`` with its signature) before
    anything is written, outside the manifest's lock. Raises WiffFallbackRefused, writing nothing, where the conditions
    do not hold."""
    from .repository_reanalysis import read_manifest, update_manifest

    path = Path(manifest_path).resolve()
    manifest = read_manifest(path)
    plan = plan_wiff_fallback(manifest, failed_job_ids)
    reads = (reader or read_wiff_headers)(extractor_path, [swap["to"] for swap in plan["swaps"]],
                                          path.parent / HEADER_READS_DIRECTORY)
    headers = wiff_headers(manifest, plan, reads)
    written = update_manifest(path, fallback_change(plan, crossing=crossing, headers=headers, extractor=extractor))
    return fallback_record(written)


def _moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def current_diagnostics(manifest: Mapping[str, Any], admitted: Iterable[Any], aliases: Mapping[str, str]) -> list[dict[str, Any]]:
    """The peak-count diagnostics of a switched unit that measured the files that run: recorded after the switch,
    never marked superseded_by_encoding_fallback, and whose representative file (its path, or the input its Console
    alias stands for) is one of the unit's current input candidates (``admitted``, resolved). All of the unit's
    diagnostics for a unit that never switched."""
    from .repository_reanalysis import _file_key, _unit_input_of

    diagnostics = [dict(item) for item in manifest.get("peak_height_diagnostics") or [] if isinstance(item, Mapping)]
    record = fallback_record(manifest)
    if not record:
        return diagnostics
    switched = _moment(record.get("switched_at"))
    current = {_file_key(str(item)) for item in admitted}
    kept = []
    for item in diagnostics:
        if item.get("superseded_by_encoding_fallback"):
            continue
        recorded = _moment(item.get("recorded_at"))
        if switched is not None and (recorded is None or recorded < switched):
            continue
        path = str((item.get("representative") or {}).get("file_path") or "").strip()
        if not path or _file_key(_unit_input_of(path, dict(aliases))) not in current:
            continue
        kept.append(item)
    return kept


def threshold_blockers(
    manifest: Mapping[str, Any], state: Mapping[str, Any], admitted: Iterable[Any], aliases: Mapping[str, str]
) -> list[str]:
    """Why a production run of a switched unit may not start at the minimum peak height its workflow asks for: no
    diagnostic measured on the files that run (current_diagnostics) produced that threshold. A diagnostic recorded
    before the switch, one the switch marked superseded, and one whose representative is no current input (the .wiff2
    it measured) do not count: the threshold must be measured on the files that actually run. [] for a unit that never
    switched, or a workflow that asks for no threshold."""
    if not fallback_record(manifest) or state.get("minimum_peak_height") in (None, ""):
        return []
    try:
        asked = float(state["minimum_peak_height"])
    except (TypeError, ValueError):
        return []
    for item in current_diagnostics(manifest, admitted, aliases):
        try:
            if abs(float(item.get("minimum_peak_height")) - asked) < 1e-9:
                return []
        except (TypeError, ValueError):
            continue
    return [
        f"The run asks for a minimum peak height of {state['minimum_peak_height']}, and no peak-count diagnostic measured "
        "on the files that now run produced it: this unit's inputs were switched from its .wiff2 to its .wiff "
        f"({WIFF_FALLBACK_RULE}), and a threshold from a diagnostic before the switch, one marked superseded, or one "
        "whose representative is no current input was measured on other files. Run the diagnostic again."
    ]


def swap_summary(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """The swaps of a record as a report names them: under the data root, never an absolute path."""
    return [
        {"from": str(item.get("from_relative") or Path(str(item.get("from") or "")).name),
         "to": str(item.get("to_relative") or Path(str(item.get("to") or "")).name),
         "sample_id": str(item.get("sample_id") or "")}
        for item in record.get("swaps") or [] if isinstance(item, Mapping)
    ]

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from . import __version__
from .mztab_validation import validate_mztab_outputs
from .diagnostic_paths import is_diagnostic_artifact


HANDOFF_FILENAME = "datamining-handoff.json"


def summarize_jobs(
    jobs: dict[str, dict[str, Any]], limit: int = 5, include_artifacts: bool = False
) -> dict[str, Any]:
    items = [
        summarize_job(job, log_lines=3, include_artifacts=include_artifacts)
        for job in jobs.values()
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    latest = items[0] if items else None
    latest_completed = next((item for item in items if item.get("status") == "completed"), None)
    return {
        "service": "MS-DIAL Interactive",
        "app_version": __version__,
        "agent_api_version": "0.5",
        "capabilities": [
            "guided_analysis_planning",
            "reusable_worksets",
            "single_file_peak_count_tuning",
            "console_path_discovery_and_persistence",
            "console_release_channel_inspection",
            "local_source_console_build_with_provenance",
            "local_source_git_remote_comparison",
            "job_scoped_output_provenance",
            "persistent_job_history",
            "restart_incompatible_local_service",
            "start_msdial_console_run",
            "observe_job_status",
            "generate_lcms_quality_assurance",
            "generate_publication_report",
            "complete_guided_pipeline_without_ui",
            "validate_mztab_m_outputs",
            "preview_mztab_m_outputs",
            "create_datamining_handoff",
            "inspect_repository_sample_metadata",
            "project_repository_metadata_to_msdial_class",
            "save_reviewed_repository_metadata",
            "plan_repository_reanalysis",
            "download_and_recognize_repository_raw_data",
            "cross_check_repository_raw_metadata",
            "prepare_repository_reanalysis_without_ui",
            "inspect_repository_internal_standard_evidence",
            "split_repository_unit_by_acquisition",
            "confirmed_repository_raw_cleanup",
            "resumable_repository_raw_download",
            # A campaign runner checks for these before it relies on them.
            "durable_repository_manifests",
            "repository_manifest_reentry",
            "repository_input_lineage",
            # Leases record their stages, open every archive kind through archives.py, verify a study
            # archive's published MD5 as the object it is, and give archive-derived inputs their basis.
            "repository_archive_extraction",
            "campaign_authorization_v1",
            "isolated_job_registry",
            # Outside a campaign an mzXML or mzData still excludes its unit before download. A campaign's lease
            # converts its unit's mzXML to mzML in its convert stage (mzxml_conversion, every inference off),
            # records input_conversions, and runs what converted; mzData is excluded there too. The one
            # inference: a scan recording no polarity is given the unit's declared ion mode, where its Catalog
            # handoff declares exactly one polarity, and the declaration is recorded with the options. A file
            # some of whose scans record the other polarity and some none is excluded unconverted (reason
            # polarity_contradicts_declaration), and the rest of the unit runs.
            "mzxml_requires_conversion_to_mzml",
            "campaign_mzxml_converted_to_mzml",
            "campaign_mzxml_polarity_from_declared_ion_mode",
            "automatic_alignment_rt_correction",
            "console_time_limits",
            "cancel_console_and_download_jobs",
            "repository_run_attempt_records",
            "one_console_per_repository_unit",
            # A unit splits by acquisition mode, ion-mobility regime and polarity, and an ion-mobility part is
            # written excluded. A split parent's raw tree is released once every part has ended, and the raw
            # cleanup and discard take a campaign approval for boundary 5, each recording what it deleted.
            "split_key_acquisition_ion_mobility_polarity",
            "split_parent_raw_release",
            "campaign_authorized_raw_cleanup_and_discard",
            # A campaign's lease (and any other under store_mode "always") fetches each object once per
            # accession into its download store and gives the unit a tree of links; a unit's release releases its
            # claims, and the store collects what no live claim holds under the approval that covered it.
            "accession_download_store",
            "waiting_for_shared_download_job_state",
            "batch_plan_distinct_objects_and_pre_claims",
            # Replicate rows that share a sample id are each their own input and analysis-CSV row (the sample
            # row, not the id, is mapped), and an undeclared unit's archive member that carries a declared raw
            # file name behind a prefix is that file's, one to one, recorded as paired_by prefixed_member_name.
            "repository_replicate_rows_as_inputs",
            "repository_prefixed_member_names",
            # A declared raw file name and an archive member that share their leading identifier (VV_13) are one
            # file where the key is unique on both sides, recorded as paired_by leading_identifier_token with the
            # warning input_names_paired_by_inference; no inferred pairing crosses a polarity token.
            "repository_leading_identifier_names",
        ],
        "workflow_outline": [
            "Inspect the input path and collect the guided scientific choices.",
            "Review the generated plan and obtain explicit confirmation before execution.",
            "Wait until /api/agent/status reports a completed analysis job.",
            "Call /api/agent/handoff with the completed job_id.",
            "Pass primary_mztab_file or mztab_files to the downstream data-mining MCP server.",
        ],
        "latest_job": latest,
        "latest_completed_job": latest_completed,
        "jobs": items[:limit],
    }


def create_datamining_handoff(
    *,
    preparation: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
    run_directory: str | Path | None = None,
    write_file: bool = True,
) -> dict[str, Any]:
    prep = preparation or (job or {}).get("preparation") or {}
    run_dir = Path(run_directory or prep.get("run_directory", "")).expanduser()
    validation = (job or {}).get("mztab_validation") or validate_mztab_outputs(run_dir)
    mztab_files = [
        {
            "path": item["file"],
            "file_name": item.get("file_name", Path(item["file"]).name),
            "status": item.get("status", "unknown"),
            "warnings": item.get("warnings", []),
            "errors": item.get("errors", []),
            "counts": item.get("counts", {}),
        }
        for item in validation.get("files", [])
    ]
    primary_mztab_file = _select_primary_mztab_file(mztab_files)
    output_files = (
        _collect_output_files_from_artifacts((job or {}).get("artifacts") or {})
        if job and job.get("artifacts")
        else _collect_output_files(run_dir)
    )
    handoff = {
        "schema": "msdial-interactive.datamining-handoff.v1",
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "source_application": "MS-DIAL Interactive",
        "job": summarize_job(job) if job else None,
        "analysis_type": prep.get("analysis_type", ""),
        "run_directory": str(run_dir),
        "input_csv": prep.get("input_csv", ""),
        "method_file": prep.get("method_file", ""),
        "manifest": prep.get("manifest", ""),
        "command": prep.get("command", []),
        "project_file_requested": bool(prep.get("project_file_requested", False)),
        "mztab_validation": validation,
        "primary_mztab_file": primary_mztab_file,
        "primary_mztab_selection": "newest_modified_time",
        "mztab_files": mztab_files,
        "msdial_output_files": output_files,
        "downstream_mcp_hint": {
            "preferred_input": "primary_mztab_file",
            "accepted_inputs": ["mztab_files", "run_directory", "msdial_output_files"],
            "suggested_tasks": [
                "PCA",
                "PLS/OPLS",
                "HCA",
                "analysis summary",
                "chromatogram visualization",
            ],
        },
    }
    if write_file and run_dir.is_dir():
        path = run_dir / HANDOFF_FILENAME
        path.write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8")
        handoff["handoff_file"] = str(path)
        handoff["msdial_output_files"].setdefault("workflow", [])
        if str(path) not in handoff["msdial_output_files"]["workflow"]:
            handoff["msdial_output_files"]["workflow"].append(str(path))
    else:
        handoff["handoff_file"] = ""
    return handoff


def summarize_job(
    job: dict[str, Any] | None, *, log_lines: int = 10, include_artifacts: bool = True
) -> dict[str, Any] | None:
    if not job:
        return None
    preparation = job.get("preparation") or {}
    validation = job.get("mztab_validation") or {}
    handoff = job.get("datamining_handoff") or {}
    artifacts = job.get("artifacts") or {}
    summary = {
        "id": job.get("id", ""),
        "kind": job.get("kind", "run"),
        "status": job.get("status", ""),
        "exit_code": job.get("exit_code"),
        "run_directory": preparation.get("run_directory", ""),
        "analysis_type": preparation.get("analysis_type", ""),
        "mztab_status": validation.get("summary", {}).get("status", ""),
        "mztab_file_count": validation.get("summary", {}).get("file_count", 0),
        "handoff_file": handoff.get("handoff_file", ""),
        "artifact_counts": {
            key: len(artifacts.get(key, [])) for key in ("mztab", "qa", "msdial")
        },
        "created_at": job.get("created_at", ""),
        "updated_at": job.get("updated_at", ""),
        "error": job.get("error", ""),
        # Set when the job was stopped rather than ending on its own: timeout, idle_timeout, cancelled.
        "stop_reason": job.get("stop_reason", ""),
        "warnings": list(job.get("warnings") or []),
        "progress": job.get("progress"),
        "log_tail": (job.get("logs") or [])[-max(0, log_lines):],
    }
    if include_artifacts:
        summary["artifacts"] = {
            key: list(artifacts.get(key, [])) for key in ("mztab", "qa", "msdial")
        }
    return summary


def _collect_output_files(run_directory: Path) -> dict[str, list[str]]:
    patterns = {
        "mztab": ["*.mzTab", "*.mztab", "*.mzTabM", "*.mztabm"],
        "alignment": ["*.mdalign"],
        "peak": ["*.mdpeak", "*.mdscan"],
        "spectra": ["*.mdmsp"],
        "project": ["*.mdproject"],
        "workflow": [
            "analysis_files.csv",
            "method.txt",
            "run-manifest.json",
            "workflow-settings.json",
            "command.txt",
            HANDOFF_FILENAME,
        ],
    }
    if not run_directory.is_dir():
        return {key: [] for key in patterns}
    collected: dict[str, list[str]] = {}
    for key, globs in patterns.items():
        paths: list[Path] = []
        for pattern in globs:
            paths.extend(run_directory.glob(pattern))
        collected[key] = [
            str(path) for path in sorted(set(paths)) if not is_diagnostic_artifact(path)
        ]
    return collected


def _collect_output_files_from_artifacts(
    artifacts: dict[str, Any]
) -> dict[str, list[str]]:
    result = {
        "mztab": list(artifacts.get("mztab", [])),
        "alignment": [],
        "peak": [],
        "spectra": [],
        "project": [],
        "workflow": [],
        "qa": list(artifacts.get("qa", [])),
    }
    for raw in artifacts.get("msdial", []):
        path = Path(raw)
        suffix = path.suffix.casefold()
        if suffix == ".mdalign":
            result["alignment"].append(str(path))
        elif suffix in {".mdpeak", ".mdscan"}:
            result["peak"].append(str(path))
        elif suffix == ".mdmsp":
            result["spectra"].append(str(path))
        elif suffix in {".mdproject", ".arf2", ".dcl"}:
            result["project"].append(str(path))
    return result


def _select_primary_mztab_file(mztab_files: list[dict[str, Any]]) -> str:
    if not mztab_files:
        return ""
    paths = [Path(item["path"]) for item in mztab_files]
    existing = [
        path for path in paths if path.is_file() and not is_diagnostic_artifact(path)
    ]
    if not existing:
        return mztab_files[0]["path"]
    return str(max(existing, key=lambda path: path.stat().st_mtime))

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .mztab_validation import validate_mztab_outputs


HANDOFF_FILENAME = "datamining-handoff.json"


def summarize_jobs(jobs: dict[str, dict[str, Any]], limit: int = 10) -> dict[str, Any]:
    items = [_summarize_job(job) for job in jobs.values()]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    latest = items[0] if items else None
    latest_completed = next((item for item in items if item.get("status") == "completed"), None)
    return {
        "service": "MS-DIAL Interactive",
        "agent_api_version": "0.1",
        "capabilities": [
            "start_msdial_console_run",
            "observe_job_status",
            "validate_mztab_m_outputs",
            "preview_mztab_m_outputs",
            "create_datamining_handoff",
        ],
        "recommended_flow": [
            "Open the local UI and help the user configure a workflow.",
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
    output_files = _collect_output_files(run_dir)
    handoff = {
        "schema": "msdial-interactive.datamining-handoff.v1",
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "source_application": "MS-DIAL Interactive",
        "job": _summarize_job(job) if job else None,
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


def _summarize_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job:
        return None
    preparation = job.get("preparation", {})
    validation = job.get("mztab_validation") or {}
    return {
        "id": job.get("id", ""),
        "kind": job.get("kind", "run"),
        "status": job.get("status", ""),
        "exit_code": job.get("exit_code"),
        "run_directory": preparation.get("run_directory", ""),
        "analysis_type": preparation.get("analysis_type", ""),
        "mztab_status": validation.get("summary", {}).get("status", ""),
        "mztab_file_count": validation.get("summary", {}).get("file_count", 0),
        "handoff_file": job.get("datamining_handoff", {}).get("handoff_file", ""),
        "log_tail": (job.get("logs") or [])[-10:],
    }


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
        collected[key] = [str(path) for path in sorted(set(paths))]
    return collected


def _select_primary_mztab_file(mztab_files: list[dict[str, Any]]) -> str:
    if not mztab_files:
        return ""
    paths = [Path(item["path"]) for item in mztab_files]
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return mztab_files[0]["path"]
    return str(max(existing, key=lambda path: path.stat().st_mtime))

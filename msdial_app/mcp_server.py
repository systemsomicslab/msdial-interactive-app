from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


try:
    from mcp.server import MCPServer

    mcp = MCPServer("MS-DIAL Interactive")
except ImportError as error:
    raise RuntimeError(
        "The MCP Python SDK is required for the MS-DIAL Interactive MCP server. "
        "Install it with: python -m pip install \"mcp[cli]\""
    ) from error


def _base_url(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{int(port)}"


def _request_json(
    method: str,
    path: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    body: dict[str, Any] | None = None,
    timeout: float = 5,
) -> dict[str, Any]:
    url = _base_url(host, port) + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not connect to MS-DIAL Interactive at {url}: {error.reason}") from error


def _status_or_error(host: str, port: int) -> dict[str, Any]:
    try:
        status = _request_json("GET", "/api/agent/status", host=host, port=port, timeout=2)
        return {"running": True, "url": _base_url(host, port), "status": status}
    except RuntimeError as error:
        return {"running": False, "url": _base_url(host, port), "error": str(error)}


@mcp.tool()
def msdial_interactive_status(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Return current MS-DIAL Interactive status and recent analysis jobs."""
    return _status_or_error(host, port)


@mcp.tool()
def msdial_interactive_launch(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Launch the local MS-DIAL Interactive web app if it is not already running."""
    current = _status_or_error(host, port)
    if current["running"]:
        if open_browser:
            webbrowser.open(current["url"])
        return {"launched": False, **current}

    command = [sys.executable, str(ROOT / "app.py"), "--host", host, "--port", str(port)]
    if not open_browser:
        command.append("--no-browser")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    deadline = time.time() + 10
    last = current
    while time.time() < deadline:
        time.sleep(0.5)
        last = _status_or_error(host, port)
        if last["running"]:
            return {"launched": True, **last}
    return {"launched": True, **last}


@mcp.tool()
def msdial_interactive_open(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Open the MS-DIAL Interactive web UI in the user's browser."""
    url = _base_url(host, port)
    webbrowser.open(url)
    return {"opened": True, "url": url, "status": _status_or_error(host, port)}


@mcp.tool()
def msdial_guided_analysis_plan(
    input_path: str,
    answers: dict[str, Any] | None = None,
    workset_id: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Inspect local MS data and return the next guided question and proposed workflow."""
    return _request_json(
        "POST",
        "/api/agent/plan",
        host=host,
        port=port,
        body={
            "input_path": input_path,
            "answers": answers or {},
            "workset_id": workset_id,
        },
        timeout=30,
    )


@mcp.tool()
def msdial_list_worksets(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """List the five built-in MS-DIAL worksets and user-saved worksets."""
    return _request_json("GET", "/api/agent/worksets", host=host, port=port)


@mcp.tool()
def msdial_save_workset(
    name: str,
    answers: dict[str, Any],
    description: str = "",
    workflow_overrides: dict[str, Any] | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Save reusable scientific choices as a named local workset."""
    return _request_json(
        "POST",
        "/api/agent/worksets/save",
        host=host,
        port=port,
        body={
            "name": name,
            "description": description,
            "answers": answers,
            "workflow_overrides": workflow_overrides or {},
        },
    )


@mcp.tool()
def msdial_download_official_library(
    catalog_id: str,
    confirmed: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Download a versioned official library only after explicit user confirmation."""
    config = _request_json("GET", "/api/config", host=host, port=port, timeout=10)
    item = next(
        (entry for entry in config.get("library_catalog", []) if entry.get("id") == catalog_id),
        None,
    )
    if item is None:
        raise RuntimeError(f"Unknown official library catalog id: {catalog_id}")
    if item.get("downloaded"):
        return {"started": False, "already_downloaded": True, "library": item}
    if not confirmed:
        return {
            "started": False,
            "confirmation_required": True,
            "library": item,
            "message": "This downloads a large Zenodo file. Ask the user, then call again with confirmed=true.",
        }
    return _request_json(
        "POST",
        "/api/libraries/download",
        host=host,
        port=port,
        body={"catalog_id": catalog_id},
        timeout=30,
    )


@mcp.tool()
def msdial_prepare_guided_analysis(
    input_path: str,
    answers: dict[str, Any],
    workset_id: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Validate a complete guided plan and write reproducible workflow files without running MS-DIAL."""
    return _request_json(
        "POST",
        "/api/agent/prepare",
        host=host,
        port=port,
        body={"input_path": input_path, "answers": answers, "workset_id": workset_id},
        timeout=120,
    )


@mcp.tool()
def msdial_start_guided_analysis(
    input_path: str,
    answers: dict[str, Any],
    workset_id: str = "",
    confirmed: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Start MS-DIAL only when the complete guided plan has been explicitly confirmed."""
    return _request_json(
        "POST",
        "/api/agent/run",
        host=host,
        port=port,
        body={
            "input_path": input_path,
            "answers": answers,
            "workset_id": workset_id,
            "confirmed": confirmed,
        },
        timeout=120,
    )


@mcp.tool()
def msdial_start_peak_count_diagnostic(
    input_path: str,
    answers: dict[str, Any],
    representative_file: str = "",
    workset_id: str = "",
    confirmed: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Run the zero-threshold single-file diagnostic after explicit confirmation."""
    return _request_json(
        "POST",
        "/api/agent/tuning/run",
        host=host,
        port=port,
        body={
            "input_path": input_path,
            "answers": answers,
            "representative_file": representative_file,
            "workset_id": workset_id,
            "confirmed": confirmed,
        },
        timeout=120,
    )


@mcp.tool()
def msdial_recommend_peak_height(
    job_id: str,
    target_peak_count: int,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Recommend Minimum peak height from a completed diagnostic height distribution."""
    return _request_json(
        "POST",
        "/api/agent/tuning/recommend",
        host=host,
        port=port,
        body={"job_id": job_id, "target_peak_count": target_peak_count},
        timeout=30,
    )


@mcp.tool()
def msdial_interactive_job(
    job_id: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Return one analysis, tuning, or library-download job including its recent logs."""
    return _request_json("GET", f"/api/jobs/{job_id}", host=host, port=port, timeout=10)


@mcp.tool()
def msdial_interactive_wait_for_completion(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout_seconds: int = 3600,
    poll_seconds: int = 10,
) -> dict[str, Any]:
    """Poll until the latest MS-DIAL Console job completes or fails."""
    deadline = time.time() + max(1, timeout_seconds)
    while True:
        status = _request_json("GET", "/api/agent/status", host=host, port=port, timeout=5)
        latest = status.get("latest_job") or {}
        if latest.get("status") in {"completed", "failed"}:
            return {"finished": True, "status": status}
        if time.time() >= deadline:
            return {"finished": False, "status": status}
        time.sleep(max(1, poll_seconds))


@mcp.tool()
def msdial_interactive_create_handoff(
    job_id: str = "",
    run_directory: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Create a data-mining handoff JSON for a completed MS-DIAL job or output folder."""
    payload = {"job_id": job_id, "run_directory": run_directory}
    return _request_json(
        "POST",
        "/api/agent/handoff",
        host=host,
        port=port,
        body=payload,
        timeout=30,
    )


@mcp.tool()
def msdial_interactive_validate_mztab(
    run_directory: str = "",
    file_path: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Validate mzTab-M files in an output folder, or one selected mzTab-M file."""
    return _request_json(
        "POST",
        "/api/mztab/validate",
        host=host,
        port=port,
        body={"run_directory": run_directory, "file_path": file_path},
        timeout=30,
    )


@mcp.tool()
def msdial_interactive_preview_mztab(
    run_directory: str,
    file_path: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Preview mzTab-M metadata, sections, first rows, and numeric columns."""
    return _request_json(
        "POST",
        "/api/mztab/preview",
        host=host,
        port=port,
        body={"run_directory": run_directory, "file_path": file_path},
        timeout=30,
    )


@mcp.tool()
def msdial_generate_lcms_qa(
    run_directory: str,
    internal_standards: list[dict[str, Any]] | None = None,
    file_path: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Generate the LC-MS QA summary and chart data from the newest QA matrix."""
    return _request_json(
        "POST",
        "/api/qa/report",
        host=host,
        port=port,
        body={
            "run_directory": run_directory,
            "file_path": file_path,
            "internal_standards": internal_standards or [],
        },
        timeout=120,
    )


@mcp.tool()
def msdial_generate_publication_report(
    run_directory: str,
    internal_standards: list[dict[str, Any]] | None = None,
    qa_file_path: str = "",
    qa_criteria: dict[str, Any] | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Generate Materials and Methods, QA Results, and supplementary Excel/TSV artifacts."""
    return _request_json(
        "POST",
        "/api/publication/report",
        host=host,
        port=port,
        body={
            "run_directory": run_directory,
            "use_saved_run": True,
            "qa_file_path": qa_file_path,
            "internal_standards": internal_standards or [],
            "qa_criteria": qa_criteria or {},
        },
        timeout=180,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

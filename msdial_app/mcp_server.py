from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
REQUIRED_AGENT_API_VERSION = "0.4"
_EMBEDDED_SERVERS: dict[tuple[str, int], tuple[ThreadingHTTPServer, threading.Thread]] = {}


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
        detected = str(status.get("agent_api_version", "0"))
        return {
            "running": True,
            "compatible": _version_tuple(detected) >= _version_tuple(REQUIRED_AGENT_API_VERSION),
            "required_agent_api_version": REQUIRED_AGENT_API_VERSION,
            "detected_agent_api_version": detected,
            "url": _base_url(host, port),
            "status": status,
        }
    except RuntimeError as error:
        return {
            "running": False,
            "compatible": False,
            "required_agent_api_version": REQUIRED_AGENT_API_VERSION,
            "url": _base_url(host, port),
            "error": str(error),
        }


def _version_tuple(value: str) -> tuple[int, ...]:
    result = []
    for part in str(value).split("."):
        digits = "".join(character for character in part if character.isdigit())
        result.append(int(digits or 0))
    return tuple((result + [0, 0])[:3])


def _repository_download_job(
    job_id: str, host: str, port: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    job = _request_json(
        "GET",
        f"/api/jobs/{job_id}?detail=full",
        host=host,
        port=port,
        timeout=30,
    )
    if job.get("kind") != "repository_download":
        raise RuntimeError(f"Job {job_id} is not a repository download job.")
    if job.get("status") != "completed":
        raise RuntimeError(
            f"Repository download job {job_id} is {job.get('status', 'unknown')}; "
            "wait for completion before preparing the analysis."
        )
    result = job.get("result") or {}
    manifest_path = Path(str(result.get("manifest_path") or "")).expanduser()
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Repository run manifest was not found for job {job_id}: {manifest_path}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    manifest["manifest_path"] = str(manifest_path.resolve())
    return job, manifest


def _repository_project_summary(
    project: dict[str, Any], workspace: dict[str, Any]
) -> dict[str, Any]:
    return {
        "repository": project.get("repository"),
        "accession": project.get("accession"),
        "title": project.get("title"),
        "public_url": project.get("public_url"),
        "publications": project.get("publications", []),
        "sample_count": project.get("sample_count") or len(workspace.get("rows", [])),
        "file_count": len(project.get("files", [])),
        "total_download_bytes": project.get("total_download_bytes", 0),
        "separation": workspace.get("separation"),
        "acquisition_mode": workspace.get("acquisition_mode"),
        "ion_mode": workspace.get("ion_mode"),
        "target_omics": workspace.get("target_omics"),
        "selection_status": project.get("selection_status"),
        "eligible": bool(project.get("eligible")),
        "exclusion_reasons": project.get("exclusion_reasons", []),
        "review_reasons": project.get("review_reasons", []),
        "warnings": project.get("warnings", []),
    }


def _repository_answer_seed(
    workspace: dict[str, Any],
    manifest: dict[str, Any],
    output_root: str,
    raw_retention_policy: str,
) -> dict[str, Any]:
    separation = str(workspace.get("separation") or "").strip().casefold()
    project_type = ""
    if "gas" in separation or separation in {"gc", "gc-ms", "gcms"}:
        project_type = "gcms"
    elif "liquid" in separation or separation in {"lc", "lc-ms", "lcms"}:
        project_type = "lcms"

    raw_ion_mode = str(workspace.get("ion_mode") or "").strip().casefold()
    ion_mode = ""
    if "negative" in raw_ion_mode or raw_ion_mode == "neg":
        ion_mode = "Negative"
    elif "positive" in raw_ion_mode or raw_ion_mode == "pos":
        ion_mode = "Positive"

    raw_acquisition = str(workspace.get("acquisition_mode") or "").strip().casefold()
    acquisition_type = ""
    if "all-ion" in raw_acquisition or "all ion" in raw_acquisition or "aif" in raw_acquisition:
        acquisition_type = "AIF"
    elif "swath" in raw_acquisition or "dia" in raw_acquisition:
        acquisition_type = "SWATH"
    elif "dda" in raw_acquisition or "data-dependent" in raw_acquisition:
        acquisition_type = "DDA"

    answers: dict[str, Any] = {
        "parameter_strategy": "default",
        "output_root": output_root,
        "export_folder_path": output_root,
        "generate_materials_methods": True,
        "workflow_overrides": {
            "repository_run_manifest": manifest["manifest_path"],
            "repository_raw_retention_policy": raw_retention_policy,
        },
    }
    if project_type:
        answers["project_type"] = project_type
        answers["run_qa"] = project_type == "lcms"
    if ion_mode and project_type == "lcms":
        answers["ion_mode"] = ion_mode
    target_omics = str(workspace.get("target_omics") or "").strip()
    if target_omics in {"Metabolomics", "Lipidomics"}:
        answers["target_omics"] = target_omics
    if acquisition_type:
        answers["acquisition_type"] = acquisition_type
    return answers


def _raw_metadata_extractor_candidates(configured: str = "") -> list[str]:
    candidates = [
        configured,
        os.environ.get("MSDIAL_RAW_METADATA_EXTRACTOR", ""),
        str(
            ROOT.parent
            / "msrawdataworkbench"
            / "RawMetadataConsoleApp"
            / "bin"
            / "Release"
            / "net8.0-windows"
            / "RawMetadataConsoleApp.exe"
        ),
        str(
            ROOT.parent
            / "msrawdataworkbench"
            / "RawMetadataConsoleApp"
            / "bin"
            / "Release"
            / "net48"
            / "RawMetadataConsoleApp.exe"
        ),
    ]
    result = []
    for value in candidates:
        path = Path(str(value or "")).expanduser()
        if str(value or "").strip() and path.is_file():
            resolved = str(path.resolve())
            if resolved not in result:
                result.append(resolved)
    return result


def _launch_local_app(host: str, port: int, open_browser: bool) -> dict[str, Any]:
    from .server import Handler

    key = (host, int(port))
    existing = _EMBEDDED_SERVERS.get(key)
    if not existing or not existing[1].is_alive():
        server = ThreadingHTTPServer(key, Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _EMBEDDED_SERVERS[key] = (server, thread)
    if open_browser:
        webbrowser.open(_base_url(host, port))
    deadline = time.time() + 15
    last = _status_or_error(host, port)
    while time.time() < deadline:
        time.sleep(0.5)
        last = _status_or_error(host, port)
        if last["running"]:
            break
    return last


def _local_listener(host: str, port: int) -> dict[str, Any]:
    try:
        import psutil
    except ImportError as error:
        raise RuntimeError(
            "Restart support requires psutil. Reinstall with: python -m pip install -e \".[mcp]\""
        ) from error
    listeners = []
    for connection in psutil.net_connections(kind="inet"):
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        if int(connection.laddr.port) != int(port) or not connection.pid:
            continue
        address = str(connection.laddr.ip)
        if host in {"127.0.0.1", "localhost"} and address not in {"127.0.0.1", "::1"}:
            continue
        listeners.append(connection.pid)
    pids = sorted(set(listeners))
    if len(pids) != 1:
        raise RuntimeError(f"Expected one local listener on port {port}; found {len(pids)}.")
    process = psutil.Process(pids[0])
    command = process.cmdline()
    description = " ".join([process.name(), process.exe(), *command]).casefold()
    markers = ("ms-dial-interactive", "msdial-interactive", "msdial_interactive", "app.py")
    if not any(marker in description for marker in markers):
        raise RuntimeError(
            f"Port {port} is owned by an unrelated process; restart was refused: {process.name()}"
        )
    return {
        "pid": process.pid,
        "name": process.name(),
        "executable": process.exe(),
        "command": command,
        "process": process,
    }


@mcp.tool()
def msdial_interactive_status(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Return current MS-DIAL Interactive status and recent analysis jobs."""
    return _status_or_error(host, port)


@mcp.tool()
def msdial_check_console_path(
    search_roots: list[str] | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Find configured and discoverable MSDIALCUI.exe/MSDIALCUI.dll paths."""
    return _request_json(
        "POST",
        "/api/agent/console/check",
        host=host,
        port=port,
        body={"search_roots": search_roots or []},
        timeout=120,
    )


@mcp.tool()
def msdial_set_console_path(
    console_path: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Validate and persist the MS-DIAL Console path for later local analyses."""
    return _request_json(
        "POST",
        "/api/agent/console/set",
        host=host,
        port=port,
        body={"console_path": console_path},
        timeout=30,
    )


@mcp.tool()
def msdial_interactive_launch(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
) -> dict[str, Any]:
    """Launch the local backend without opening the browser unless explicitly requested."""
    current = _status_or_error(host, port)
    if current["running"]:
        if open_browser:
            webbrowser.open(current["url"])
        return {"launched": False, **current}
    return {"launched": True, **_launch_local_app(host, port, open_browser)}


@mcp.tool()
def msdial_interactive_restart(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    confirmed: bool = False,
    open_browser: bool = False,
) -> dict[str, Any]:
    """Replace a recognized local MS-DIAL Interactive process with the current source version."""
    current = _status_or_error(host, port)
    if not current["running"]:
        return {"restarted": False, "launched": True, **_launch_local_app(host, port, open_browser)}
    listener = _local_listener(host, port)
    process_info = {key: value for key, value in listener.items() if key != "process"}
    if not confirmed:
        return {
            "restarted": False,
            "confirmation_required": True,
            "current": current,
            "process": process_info,
            "message": "Ask the user before stopping this recognized local MS-DIAL Interactive process.",
        }
    process = listener["process"]
    if process.pid == os.getpid():
        embedded = _EMBEDDED_SERVERS.pop((host, int(port)), None)
        if embedded:
            embedded[0].shutdown()
            embedded[0].server_close()
            embedded[1].join(timeout=5)
        else:
            return {"restarted": False, "already_current": True, **current}
    else:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception as error:
            raise RuntimeError(
                f"MS-DIAL Interactive process {process.pid} did not stop cleanly."
            ) from error
    launched = _launch_local_app(host, port, open_browser)
    if not launched.get("compatible"):
        raise RuntimeError(
            "The restarted app is still incompatible with this MCP server: "
            + str(launched)
        )
    return {"restarted": True, "previous_process": process_info, **launched}


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
def msdial_inspect_repository_metadata(
    repository: str,
    accession: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Extract per-sample metadata and publications from a public metabolomics accession."""
    return _request_json(
        "POST",
        "/api/repository/metadata/inspect",
        host=host,
        port=port,
        body={"repository": repository, "accession": accession},
        timeout=180,
    )


@mcp.tool()
def msdial_project_repository_classes(
    workspace: dict[str, Any],
    hierarchy: list[str],
    analysis_files: list[dict[str, Any]] | None = None,
    missing_value: str = "NA",
    separator: str = "_",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Join an ordered metadata hierarchy into MS-DIAL Class and match it to analysis files."""
    return _request_json(
        "POST",
        "/api/repository/metadata/project",
        host=host,
        port=port,
        body={
            "workspace": workspace,
            "hierarchy": hierarchy,
            "files": analysis_files or [],
            "missing_value": missing_value,
            "separator": separator,
        },
        timeout=30,
    )


@mcp.tool()
def msdial_save_repository_metadata(
    workspace: dict[str, Any],
    destination: str,
    analysis_files: list[dict[str, Any]] | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Save reviewed repository metadata as JSON/TSV and an optional MS-DIAL analysis CSV."""
    return _request_json(
        "POST",
        "/api/repository/metadata/save",
        host=host,
        port=port,
        body={
            "workspace": workspace,
            "destination": destination,
            "analysis_files": analysis_files or [],
        },
        timeout=30,
    )


@mcp.tool()
def msdial_repository_reanalysis_plan(
    repository: str,
    accession: str,
    workspace_root: str = "",
    maximum_gb: float = 20,
    raw_retention_policy: str = "keep",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Inspect one public accession and return metadata, QA evidence, and download decisions."""
    response = _request_json(
        "POST",
        "/api/repository/metadata/inspect",
        host=host,
        port=port,
        body={"repository": repository, "accession": accession},
        timeout=180,
    )
    project = response.get("project") or {}
    workspace = response.get("workspace") or {}
    from .repository_qa import repository_internal_standard_evidence

    return {
        "project": _repository_project_summary(project, workspace),
        "metadata": {
            "default_class_hierarchy": workspace.get("hierarchy", []),
            "fields": workspace.get("fields", []),
            "field_count": len(workspace.get("fields", [])),
            "row_count": len(workspace.get("rows", [])),
        },
        "qa_internal_standard_evidence": repository_internal_standard_evidence(workspace),
        "download": {
            "workspace_root": workspace_root,
            "maximum_gb": maximum_gb,
            "raw_retention_policy": raw_retention_policy,
            "confirmation_required": True,
        },
        "next_decisions": [
            "Review the inferred LC-MS/GC-MS, polarity, acquisition mode, and target omics.",
            "Review and confirm the ordered metadata fields used to build MS-DIAL Class.",
            "Choose whether downloaded raw data are kept or deleted only after validated output.",
            "Confirm the bounded raw-data download before starting it.",
        ],
    }


@mcp.tool()
def msdial_download_repository_raw(
    repository: str,
    accession: str,
    workspace_root: str,
    maximum_gb: float = 20,
    raw_retention_policy: str = "keep",
    allow_preflight: bool = False,
    confirmed: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Download and recognize repository raw data only after explicit user confirmation."""
    response = _request_json(
        "POST",
        "/api/repository/metadata/inspect",
        host=host,
        port=port,
        body={"repository": repository, "accession": accession},
        timeout=180,
    )
    project = response.get("project") or {}
    workspace = response.get("workspace") or {}
    preview = {
        "project": _repository_project_summary(project, workspace),
        "workspace_root": workspace_root,
        "maximum_gb": maximum_gb,
        "raw_retention_policy": raw_retention_policy,
        "allow_preflight": allow_preflight,
    }
    if not confirmed:
        return {
            "started": False,
            "confirmation_required": True,
            "preview": preview,
            "message": (
                "This downloads repository raw data to the stated local workspace. "
                "Ask the user to approve the accession, size limit, destination, and retention "
                "policy, then call again with confirmed=true."
            ),
        }
    started = _request_json(
        "POST",
        "/api/repository/download",
        host=host,
        port=port,
        body={
            "project": project,
            "workspace_root": workspace_root,
            "maximum_gb": maximum_gb,
            "raw_retention_policy": raw_retention_policy,
            "allow_preflight": allow_preflight,
        },
        timeout=30,
    )
    return {"started": True, **started, "preview": preview}


@mcp.tool()
def msdial_repository_raw_metadata_preflight(
    download_job_id: str,
    extractor_path: str = "",
    max_inputs: int = 3,
    confirm_untargeted: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Cross-check representative downloaded files with the local raw-metadata parser."""
    _, manifest = _repository_download_job(download_job_id, host, port)
    candidates = _raw_metadata_extractor_candidates(extractor_path)
    if not candidates:
        return {
            "completed": False,
            "extractor_found": False,
            "manifest_path": manifest["manifest_path"],
            "message": (
                "Set extractor_path or MSDIAL_RAW_METADATA_EXTRACTOR to a built "
                "RawMetadataConsoleApp executable. Repository metadata remains available."
            ),
        }
    from .repository_reanalysis import run_raw_metadata_preflight

    result = run_raw_metadata_preflight(
        Path(manifest["manifest_path"]),
        Path(candidates[0]),
        max_inputs=max(1, max_inputs),
        confirm_untargeted=confirm_untargeted,
    )
    raw = result.get("raw_metadata_preflight") or {}
    return {
        "completed": True,
        "extractor_found": True,
        "extractor_path": candidates[0],
        "manifest_path": result.get("manifest_path"),
        "status": result.get("status"),
        "execution_allowed": result.get("execution_allowed"),
        "summary": raw.get("summary"),
        "advisory": raw.get("advisory"),
        "confirm_untargeted_applied": confirm_untargeted,
    }


@mcp.tool()
def msdial_prepare_repository_reanalysis(
    download_job_id: str,
    hierarchy: list[str] | None = None,
    confirmed: bool = False,
    allow_partial_mapping: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Project repository metadata into Class and prepare an analysis CSV after review."""
    job, manifest = _repository_download_job(download_job_id, host, port)
    from .repository_metadata import (
        apply_classes_to_analysis_files,
        metadata_workspace,
        project_class_hierarchy,
        save_metadata_review,
    )

    workspace = metadata_workspace(manifest.get("project") or {})
    selected_hierarchy = list(hierarchy if hierarchy is not None else workspace.get("hierarchy", []))
    projected = project_class_hierarchy(workspace, selected_hierarchy)
    recognized = ((job.get("result") or {}).get("recognized") or {}).get("files", [])
    application = apply_classes_to_analysis_files(projected, recognized)
    output_root = str(manifest.get("output_directory") or "")
    raw_retention_policy = str(job.get("raw_retention_policy") or "keep")
    answer_seed = _repository_answer_seed(
        projected, manifest, output_root, raw_retention_policy
    )
    from .repository_qa import repository_internal_standard_evidence

    preview = {
        "download_job_id": download_job_id,
        "manifest_path": manifest["manifest_path"],
        "analysis_input_path": manifest.get("analysis_input_path"),
        "output_root": output_root,
        "class_hierarchy": selected_hierarchy,
        "matched_count": application["matched_count"],
        "recognized_count": len(recognized),
        "unmatched": application["unmatched"],
        "ambiguous": application["ambiguous"],
        "answer_seed": answer_seed,
        "qa_internal_standard_evidence": repository_internal_standard_evidence(projected),
    }
    if not confirmed:
        return {
            "prepared": False,
            "confirmation_required": True,
            "preview": preview,
            "message": (
                "Review the Class hierarchy and file matching. Call again with confirmed=true "
                "to write reviewed metadata and analysis_files.csv."
            ),
        }
    if (application["unmatched"] or application["ambiguous"]) and not allow_partial_mapping:
        raise RuntimeError(
            "Repository metadata did not map uniquely to every recognized raw file. "
            "Review unmatched/ambiguous paths, or explicitly set allow_partial_mapping=true."
        )
    saved = save_metadata_review(projected, output_root, application["files"])
    input_path = saved.get("analysis_files_csv")
    if not input_path:
        raise RuntimeError("No analysis_files.csv was generated from the repository download.")
    answer_seed["repository_metadata_path"] = saved["metadata_json"]
    return {
        "prepared": True,
        "input_path": input_path,
        "output_root": output_root,
        "files": saved,
        "preview": preview,
        "next_step": (
            "Pass input_path and preview.answer_seed to msdial_guided_analysis_plan, then "
            "collect any remaining scientific decisions before execution."
        ),
    }


@mcp.tool()
def msdial_repository_qa_evidence(
    download_job_id: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Return repository declarations that may identify LC-MS internal-standard QA targets."""
    _, manifest = _repository_download_job(download_job_id, host, port)
    from .repository_metadata import metadata_workspace
    from .repository_qa import repository_internal_standard_evidence

    workspace = metadata_workspace(manifest.get("project") or {})
    evidence = repository_internal_standard_evidence(workspace)
    return {
        "repository": workspace.get("repository"),
        "accession": workspace.get("accession"),
        "ion_mode": workspace.get("ion_mode"),
        "target_omics": workspace.get("target_omics"),
        "evidence": evidence,
        "review_required": bool(evidence),
        "message": (
            "Use the repository evidence to draft name/adduct/mz/tolerances, but present them "
            "as reviewable candidates. Do not invent RT; leave it null unless recorded."
            if evidence
            else "No internal-standard declaration was found in the repository metadata."
        ),
    }


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
def msdial_estimate_peak_height(
    job_id: str,
    target_peak_count: int,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Estimate Minimum peak height from a completed diagnostic height distribution."""
    return _request_json(
        "POST",
        "/api/agent/tuning/estimate",
        host=host,
        port=port,
        body={"job_id": job_id, "target_peak_count": target_peak_count},
        timeout=30,
    )


@mcp.tool()
def msdial_interactive_job(
    job_id: str,
    detail: bool = False,
    log_lines: int = 50,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Return a compact job summary; request detail only for diagnostics."""
    query = urllib.parse.urlencode(
        {"detail": "full" if detail else "summary", "log_lines": max(0, log_lines)}
    )
    return _request_json("GET", f"/api/jobs/{job_id}?{query}", host=host, port=port, timeout=10)


@mcp.tool()
def msdial_interactive_wait_for_completion(
    job_id: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout_seconds: int = 3600,
    poll_seconds: int = 10,
) -> dict[str, Any]:
    """Poll until the specified MS-DIAL job completes or fails."""
    deadline = time.time() + max(1, timeout_seconds)
    while True:
        job = _request_json("GET", f"/api/jobs/{job_id}", host=host, port=port, timeout=5)
        if job.get("status") in {"completed", "failed", "interrupted"}:
            return {"finished": True, "job": job}
        if time.time() >= deadline:
            return {"finished": False, "job": job}
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
    job_id: str = "",
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
        body={"job_id": job_id, "run_directory": run_directory, "file_path": file_path},
        timeout=30,
    )


@mcp.tool()
def msdial_interactive_preview_mztab(
    job_id: str = "",
    run_directory: str = "",
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
        body={"job_id": job_id, "run_directory": run_directory, "file_path": file_path},
        timeout=30,
    )


@mcp.tool()
def msdial_generate_lcms_qa(
    job_id: str,
    internal_standards: list[dict[str, Any]] | None = None,
    file_path: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Generate LC-MS QA only from the matrix created or updated by one job."""
    return _request_json(
        "POST",
        "/api/qa/report",
        host=host,
        port=port,
        body={
            "job_id": job_id,
            "file_path": file_path,
            "internal_standards": internal_standards or [],
        },
        timeout=120,
    )


@mcp.tool()
def msdial_generate_publication_report(
    job_id: str,
    run_qa: bool = True,
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
            "job_id": job_id,
            "use_saved_run": True,
            "run_qa": run_qa,
            "qa_file_path": qa_file_path,
            "internal_standards": internal_standards or [],
            "qa_criteria": qa_criteria or {},
        },
        timeout=180,
    )


@mcp.tool()
def msdial_complete_guided_analysis(
    job_id: str,
    run_qa: bool = True,
    internal_standards: list[dict[str, Any]] | None = None,
    generate_materials_methods: bool = True,
    qa_criteria: dict[str, Any] | None = None,
    timeout_seconds: int = 86400,
    poll_seconds: int = 10,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Wait for one run, then validate/preview mzTab-M and generate requested QA/publication artifacts."""
    deadline = time.time() + max(1, timeout_seconds)
    while True:
        job = _request_json("GET", f"/api/jobs/{job_id}", host=host, port=port, timeout=10)
        if job.get("status") in {"completed", "failed", "interrupted"}:
            break
        if time.time() >= deadline:
            return {"finished": False, "job": job}
        time.sleep(max(1, poll_seconds))
    if job.get("status") != "completed":
        return {"finished": True, "success": False, "job": job}

    job = _request_json(
        "GET", f"/api/jobs/{job_id}?detail=full", host=host, port=port, timeout=30
    )

    preparation = job.get("preparation") or {}
    run_directory = str(preparation.get("run_directory", ""))
    result: dict[str, Any] = {
        "finished": True,
        "success": True,
        "job": {
            "id": job.get("id"),
            "status": job.get("status"),
            "exit_code": job.get("exit_code"),
            "artifacts": job.get("artifacts", {}),
        },
        "run_directory": run_directory,
    }
    result["mztab_validation"] = _request_json(
        "POST",
        "/api/mztab/validate",
        host=host,
        port=port,
        body={"job_id": job_id},
        timeout=120,
    ).get("validation")
    result["mztab_preview"] = _request_json(
        "POST",
        "/api/mztab/preview",
        host=host,
        port=port,
        body={"job_id": job_id},
        timeout=120,
    ).get("preview")

    qa_report = None
    if run_qa and str(preparation.get("analysis_type", "lcms")).casefold() == "lcms":
        try:
            qa_report = _request_json(
                "POST",
                "/api/qa/report",
                host=host,
                port=port,
                body={
                    "job_id": job_id,
                    "internal_standards": internal_standards or [],
                },
                timeout=180,
            ).get("report")
            result["quality_assurance"] = qa_report
        except RuntimeError as error:
            result["quality_assurance_error"] = str(error)

    if generate_materials_methods:
        try:
            result["publication"] = _request_json(
                "POST",
                "/api/publication/report",
                host=host,
                port=port,
                body={
                    "job_id": job_id,
                    "use_saved_run": True,
                    "run_qa": bool(qa_report),
                    "qa_report": qa_report,
                    "internal_standards": internal_standards or [],
                    "qa_criteria": qa_criteria or {},
                },
                timeout=300,
            )
        except RuntimeError as error:
            result["publication_error"] = str(error)

    result["handoff"] = _request_json(
        "POST",
        "/api/agent/handoff",
        host=host,
        port=port,
        body={"job_id": job_id},
        timeout=120,
    ).get("handoff")
    return result


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

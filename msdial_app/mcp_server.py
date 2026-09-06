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
from functools import wraps
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
REQUIRED_AGENT_API_VERSION = "0.4"
REPOSITORY_EXECUTION_SCOPE = {
    "project_type": "LC-MS/MS",
    "acquisition_modes": ["DDA", "DIA", "AIF", "SWATH"],
    "untargeted": True,
    "requires_ms1_survey": True,
    "requires_product_ion_spectra": True,
}
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


class MsdialRequestError(RuntimeError):
    """A backend call that failed, carrying what the backend said about it.

    The backend already answers a rejected call with a specific, actionable sentence -- "Complete the
    guided questions and choose target_peak_count before diagnostic tuning", for one. That sentence
    used to be folded into a formatted string and then dropped at the MCP boundary, so a caller saw
    only "Error executing tool msdial_start_peak_count_diagnostic" with no reason at all. An agent
    cannot correct what it cannot read, and every other guard in this server degrades to halting
    without a reason while this one is missing.
    """

    def __init__(
        self, detail: str, *, status: int | None = None, endpoint: str = "", trace: str = ""
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.endpoint = endpoint
        self.trace = trace

    @property
    def reason(self) -> str:
        if self.status is None:
            return "backend_unavailable"
        if 400 <= self.status < 500:
            return "validation_error"
        return "server_error"


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
        raw = error.read().decode("utf-8", errors="replace")
        detail, trace = raw, ""
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            # The backend answers a rejected call with {"error": ..., "trace": ...}, and the error is
            # the sentence a caller can act on. Folding it into a formatted string is what lost it.
            detail = str(payload.get("error") or payload.get("detail") or raw).strip() or raw
            trace = str(payload.get("trace") or "")
        raise MsdialRequestError(
            detail, status=error.code, endpoint=f"{method} {path}", trace=trace
        ) from error
    except urllib.error.URLError as error:
        raise MsdialRequestError(
            f"Could not connect to MS-DIAL Interactive at {url}: {error.reason}",
            endpoint=f"{method} {path}",
        ) from error


def _status_or_error(
    host: str, port: int, *, limit: int = 5, include_artifacts: bool = False
) -> dict[str, Any]:
    try:
        query = urllib.parse.urlencode(
            {"limit": max(0, int(limit)), "include_artifacts": str(include_artifacts).lower()}
        )
        status = _request_json(
            "GET", f"/api/agent/status?{query}", host=host, port=port, timeout=2
        )
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


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    return None


def _validated_workspace_root(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Set a non-empty repository workspace root.")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("Repository workspace root must be an absolute path.")
    resolved = path.resolve()
    configured = str(os.environ.get("MSDIAL_REPOSITORY_WORKSPACE_ROOT") or "").strip()
    if configured:
        boundary = Path(configured).expanduser().resolve()
        try:
            resolved.relative_to(boundary)
        except ValueError as error:
            raise ValueError(
                f"Repository workspace root must stay under the configured boundary: {boundary}"
            ) from error
    return str(resolved)


def _structured_validation_errors(function):
    """Return a failure a caller can act on instead of raising past the MCP boundary.

    A raised exception reaches the client as "Error executing tool <name>" with the message gone, so
    an unattended caller learns only that something stopped. Every tool that talks to the backend is
    wrapped, not just the two that were, because the reason is equally unreachable from all of them.
    """

    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except MsdialRequestError as error:
            failure = {
                "ok": False,
                "reason": error.reason,
                "detail": error.detail,
                "error_type": type(error).__name__,
                "endpoint": error.endpoint,
            }
            if error.status is not None:
                failure["http_status"] = error.status
            if error.trace:
                failure["trace"] = error.trace
            return failure
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as error:
            return {
                "ok": False,
                "reason": "validation_error",
                "detail": str(error),
                "error_type": type(error).__name__,
            }

    return wrapped


def _analysis_intent(value: str) -> dict[str, Any]:
    purpose = " ".join(str(value or "").split())
    return {
        "purpose": purpose,
        "confirmed": bool(purpose),
        "pending_decisions": [] if purpose else ["analysis_purpose"],
        "prompt": (
            "State the scientific purpose of this reanalysis, including the biological comparison, "
            "whether annotation or comparative profiling is central, and the outputs needed."
            if not purpose
            else "Use this purpose when proposing Class, contrasts, annotation, QA, and outputs."
        ),
    }


def _repository_download_blockers(
    project: dict[str, Any],
    intent: dict[str, Any],
    required_bytes: int,
    maximum_bytes: int,
    allow_preflight: bool,
) -> list[str]:
    blocking = list(project.get("blocking_reasons", []))
    preflight_exception = (
        allow_preflight and project.get("selection_status") == "raw_metadata_required"
    )
    if preflight_exception:
        blocking = [
            item for item in blocking if not str(item).startswith("technical_metadata:")
        ]
    elif not project.get("eligible"):
        blocking.extend(project.get("exclusion_reasons", []))
        blocking.extend(project.get("review_reasons", []))
    if not intent["confirmed"]:
        blocking.append("analysis_purpose:missing")
    if required_bytes > maximum_bytes:
        blocking.append("size_limit:exceeded")
    return list(dict.fromkeys(str(item) for item in blocking if str(item).strip()))


def _load_analysis_unit_handoff(
    handoff: dict[str, Any] | None = None, handoff_path: str = ""
) -> dict[str, Any] | None:
    if handoff is not None and handoff_path:
        raise ValueError("Provide analysis_unit_handoff or analysis_unit_handoff_path, not both.")
    if handoff is not None:
        return handoff
    if not handoff_path:
        return None
    path = Path(handoff_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Analysis-unit handoff was not found: {path}")
    loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ValueError("Analysis-unit handoff file must contain one JSON object.")
    return loaded


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
        "analysis_unit_id": project.get("analysis_unit_id", ""),
        "title": project.get("title"),
        "public_url": project.get("public_url"),
        "publications": project.get("publications", []),
        "publication_status": project.get("publication_status", "none_recorded"),
        "sample_count": project.get("sample_count") or len(workspace.get("rows", [])),
        "file_count": len(project.get("files", [])),
        "total_download_bytes": project.get("total_download_bytes", 0),
        "download_scope": project.get("download_scope", {}),
        "separation": workspace.get("separation"),
        "acquisition_mode": workspace.get("acquisition_mode"),
        "ion_mode": workspace.get("ion_mode"),
        "target_omics": workspace.get("target_omics"),
        "selection_status": project.get("selection_status"),
        "eligible": bool(project.get("eligible")),
        "exclusion_reasons": project.get("exclusion_reasons", []),
        "review_reasons": project.get("review_reasons", []),
        "warnings": project.get("warnings", []),
        "blocking_reasons": project.get("blocking_reasons", []),
        "pending_decisions": project.get("pending_decisions", []),
    }


def _required_download_size(project: dict[str, Any]) -> dict[str, Any]:
    """Resolve the safety-limit byte count for one project.

    ``CLAUDE.md`` designates the actual required bundle bytes as the download
    approval and safety-limit quantity, so it may never be taken on the handoff's
    word alone; see ``resolve_required_download_bytes``.
    """
    from .repository_reanalysis import resolve_required_download_bytes

    return resolve_required_download_bytes(
        (project.get("download_scope") or {}).get("bundle_bytes"),
        project.get("total_download_bytes"),
    )


def _project_from_analysis_unit_handoff(
    handoff: dict[str, Any], repository: str = "", accession: str = ""
) -> tuple[dict[str, Any], dict[str, Any]]:
    if handoff.get("schema") != "msdial-repository-reanalysis-handoff.v1":
        raise ValueError("Unsupported or missing repository reanalysis handoff schema.")
    handoff_repository = str(handoff.get("repository") or "").strip()
    handoff_accession = str(handoff.get("accession") or "").strip()
    unit_id = str(handoff.get("analysis_unit_id") or "").strip()
    if not handoff_repository or not handoff_accession or not unit_id:
        raise ValueError("Handoff repository, accession, and analysis_unit_id are required.")
    if repository and repository != handoff_repository:
        raise ValueError("Handoff repository does not match the requested repository.")
    if accession and accession != handoff_accession:
        raise ValueError("Handoff accession does not match the requested accession.")
    settings = dict(handoff.get("technical_settings") or {})
    file_payload = handoff.get("files") or []
    if handoff.get("files_omitted"):
        file_path = Path(str(handoff.get("file_manifest_path") or "")).expanduser().resolve()
        if not file_path.is_file():
            raise FileNotFoundError(
                f"External file manifest for analysis unit {unit_id} was not found: {file_path}"
            )
        file_payload = json.loads(file_path.read_text(encoding="utf-8-sig"))
        if not isinstance(file_payload, list):
            raise ValueError("External analysis-unit file manifest must contain a JSON array.")
    files = []
    for item in file_payload:
        path = str(item.get("path") or "").strip()
        url = str(item.get("download_url") or "").strip()
        if not path or not url:
            raise ValueError(f"Analysis unit {unit_id} contains a file without path/download_url.")
        files.append(
            {
                "name": path,
                "size_bytes": int(item.get("size_bytes") or 0),
                "url": url,
                "role": str(item.get("role") or "raw"),
                "checksum": str(item.get("checksum") or ""),
            }
        )
    if not files:
        raise ValueError(f"Analysis unit {unit_id} contains no downloadable files.")
    scope = dict(handoff.get("download_scope") or {})
    declared_file_count = scope.get("file_count")
    if declared_file_count is not None and int(declared_file_count) != len(files):
        raise ValueError(
            f"Analysis unit {unit_id} declares {declared_file_count} files but provides {len(files)}."
        )
    manifest_bytes = sum(item["size_bytes"] for item in files)
    declared_bundle_bytes = scope.get("bundle_bytes")
    if (
        declared_bundle_bytes is not None
        and manifest_bytes > 0
        and int(declared_bundle_bytes) < manifest_bytes
    ):
        raise ValueError(
            f"Analysis unit {unit_id} declares a {int(declared_bundle_bytes)}-byte bundle "
            f"but its file manifest totals {manifest_bytes} bytes."
        )
    common_attributes = dict(handoff.get("unit_attributes") or {})
    sample_payload = handoff.get("sample_metadata") or []
    if handoff.get("sample_metadata_omitted"):
        sample_path = Path(str(handoff.get("sample_table_path") or "")).expanduser().resolve()
        if not sample_path.is_file():
            raise FileNotFoundError(
                f"External sample table for analysis unit {unit_id} was not found: {sample_path}"
            )
        sample_payload = json.loads(sample_path.read_text(encoding="utf-8-sig"))
        if not isinstance(sample_payload, list):
            raise ValueError("External analysis-unit sample table must contain a JSON array.")
    samples = [
        {
            "sample_id": str(item.get("sample_id") or ""),
            "raw_file": str(item.get("raw_file") or ""),
            "values": {
                **common_attributes,
                **dict(item.get("attributes") or item.get("values") or {}),
            },
        }
        for item in sample_payload
    ]
    declared_sample_count = handoff.get("sample_count")
    if declared_sample_count is not None and int(declared_sample_count) != len(samples):
        raise ValueError(
            f"Analysis unit {unit_id} declares {declared_sample_count} sample rows but provides {len(samples)}."
        )
    untargeted = _optional_bool(settings.get("untargeted"))
    project = {
        "repository": handoff_repository,
        "accession": handoff_accession,
        "analysis_unit_id": unit_id,
        "source_subrecord_id": str(handoff.get("source_subrecord_id") or ""),
        "title": str(handoff.get("title") or handoff_accession),
        "description": str(handoff.get("description") or ""),
        "public_url": str(handoff.get("repository_url") or ""),
        "separation": str(settings.get("separation") or "Unknown"),
        "acquisition_mode": str(settings.get("acquisition_mode") or "Unknown"),
        "ion_mode": str(settings.get("ion_mode") or "Unknown"),
        "untargeted": untargeted,
        "sample_count": int(
            handoff.get("analytical_sample_count")
            or scope.get("analysis_file_count")
            or len({item["sample_id"] for item in samples if item["sample_id"]})
        ),
        "files": files,
        "publications": list(handoff.get("publications") or []),
        "publication_status": str(handoff.get("publication_status") or "not_retrieved"),
        "sample_metadata": samples,
        "total_download_bytes": sum(item["size_bytes"] for item in files),
        "warnings": list(handoff.get("warnings") or []),
        "selection_status": "unreviewed",
        "review_reasons": [],
        "eligible": False,
        "exclusion_reasons": [],
        "download_scope": scope,
        "class_proposal": handoff.get("class_proposal"),
        "blocking_reasons": [],
        "pending_decisions": [],
        "repository_metadata": {"catalog_handoff": handoff},
    }
    from .repository_reanalysis import EligibilityPolicy, evaluate_eligibility, project_from_dict

    typed = evaluate_eligibility(
        project_from_dict(project),
        EligibilityPolicy(
            max_download_bytes=max(int(scope.get("bundle_bytes") or 0), project["total_download_bytes"], 1),
            max_samples=max(project["sample_count"], 1),
            require_known_size=False,
            require_untargeted=True,
        ),
    )
    declared_blocks = [str(item) for item in handoff.get("blocking_reasons") or []]
    technical_blocks = [item for item in declared_blocks if item.startswith("technical_metadata:")]
    decision_blocks = [item for item in declared_blocks if not item.startswith("technical_metadata:")]
    if technical_blocks and not typed.exclusion_reasons:
        typed.review_reasons = list(dict.fromkeys([*typed.review_reasons, *technical_blocks]))
        typed.eligible = False
        typed.selection_status = "raw_metadata_required"
    typed.blocking_reasons = list(dict.fromkeys(decision_blocks))
    typed.pending_decisions = [
        "class_proposal" for item in decision_blocks if item == "class_proposal:missing"
    ]
    project = typed.as_dict()
    from .repository_metadata import metadata_workspace

    workspace = metadata_workspace(project)
    workspace["target_omics"] = str(settings.get("target_omics") or "Unknown")
    return project, workspace


def _repository_inspection(
    repository: str,
    accession: str,
    analysis_unit_handoff: dict[str, Any] | None,
    analysis_unit_handoff_path: str,
    host: str,
    port: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis_unit_handoff = _load_analysis_unit_handoff(
        analysis_unit_handoff, analysis_unit_handoff_path
    )
    if analysis_unit_handoff:
        return _project_from_analysis_unit_handoff(
            analysis_unit_handoff, repository, accession
        )
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
    from .repository_reanalysis import (
        EligibilityPolicy,
        evaluate_eligibility,
        project_from_dict,
    )

    typed = project_from_dict(project)
    typed = evaluate_eligibility(
        typed,
        EligibilityPolicy(
            max_download_bytes=max(typed.total_download_bytes, 1),
            max_samples=max(typed.sample_count or 1, 1),
            require_known_size=False,
            require_untargeted=True,
        ),
    )
    project = typed.as_dict()
    conflicts = _repository_technical_conflicts(workspace)
    for field, values in conflicts.items():
        project["eligible"] = False
        reason = f"Mixed {field} values in accession scope: {', '.join(values)}."
        project["review_reasons"].append(reason)
        project["warnings"].append(reason + " Select an analysis unit before reanalysis.")
        if field == "ion mode":
            workspace["ion_mode"] = None
        elif field == "separation":
            workspace["separation"] = None
        elif field == "acquisition mode":
            workspace["acquisition_mode"] = None
    if not project["eligible"] and not (
        project["exclusion_reasons"] or project["review_reasons"]
    ):
        project["review_reasons"] = ["Repository metadata requires review before reanalysis."]
    return project, workspace


def _repository_technical_conflicts(workspace: dict[str, Any]) -> dict[str, list[str]]:
    groups = {
        "ion mode": ("polarity", "ion mode"),
        "separation": ("method type", "separation mode"),
        "acquisition mode": ("acquisition mode", "acquisition type", "scan mode"),
    }
    values: dict[str, set[str]] = {key: set() for key in groups}
    for row in workspace.get("rows") or []:
        row_values = dict(row.get("values") or {})
        for name, value in row_values.items():
            normalized_name = str(name).strip().casefold()
            normalized_value = str(value or "").strip()
            if not normalized_value:
                continue
            for group, aliases in groups.items():
                if any(alias in normalized_name for alias in aliases):
                    values[group].add(normalized_value)
    return {
        key: sorted(items, key=str.casefold)
        for key, items in values.items()
        if len(items) > 1
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
        "parameter_strategy": "auto_peak_range",
        "target_peak_count_min": 3000,
        "target_peak_count_max": 6000,
        "smoothing_method": "TimeBasedLinearWeightedMovingAverage",
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
    # Carried into the workflow so it reaches workflow-settings.json and, through it, the publication
    # provenance. A Methods section that names a Class grouping should be able to say which approved
    # proposal produced it, under what purpose and contrast, and from which prompt.
    provenance = workspace.get("class_proposal_provenance")
    if provenance:
        answers["workflow_overrides"]["class_proposal_provenance"] = provenance
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
@_structured_validation_errors
def msdial_interactive_status(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    limit: int = 5,
    include_artifacts: bool = False,
) -> dict[str, Any]:
    """Return current MS-DIAL Interactive status and recent analysis jobs."""
    return _status_or_error(
        host, port, limit=max(0, int(limit)), include_artifacts=include_artifacts
    )


@mcp.tool()
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
def msdial_check_official_console_releases(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Check official stable and prerelease MS-DIAL Console packages on GitHub."""
    return _request_json(
        "POST",
        "/api/agent/console/releases",
        host=host,
        port=port,
        body={},
        timeout=30,
    )


@mcp.tool()
@_structured_validation_errors
def msdial_check_local_console_source(
    source_root: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Fetch origin and compare a local MS-DIAL source checkout with origin/master."""
    return _request_json(
        "POST",
        "/api/agent/console/source-status",
        host=host,
        port=port,
        body={"source_root": source_root},
        timeout=210,
    )


@mcp.tool()
@_structured_validation_errors
def msdial_build_console_from_local_source(
    source_root: str,
    framework: str = "net48",
    confirmed: bool = False,
    select_after_build: bool = True,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Preview or start a local-source Console build; execution requires confirmed=True."""
    return _request_json(
        "POST",
        "/api/agent/console/build",
        host=host,
        port=port,
        body={
            "source_root": source_root,
            "framework": framework,
            "configuration": "Release",
            "confirmed": confirmed,
            "select_after_build": select_after_build,
        },
        timeout=30,
    )


@mcp.tool()
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
def msdial_interactive_open(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Open the MS-DIAL Interactive web UI in the user's browser."""
    url = _base_url(host, port)
    webbrowser.open(url)
    return {"opened": True, "url": url, "status": _status_or_error(host, port)}


@mcp.tool()
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
def msdial_repository_reanalysis_plan(
    repository: str,
    accession: str,
    workspace_root: str = "",
    maximum_gb: float = 20,
    raw_retention_policy: str = "keep",
    analysis_unit_handoff: dict[str, Any] | None = None,
    analysis_unit_handoff_path: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    analysis_purpose: str = "",
) -> dict[str, Any]:
    """Inspect one public accession and return metadata, QA evidence, and download decisions.

    ``maximum_gb`` is decimal GB (1e9 bytes), matching how repositories report size.
    """
    workspace_root = _validated_workspace_root(workspace_root)
    project, workspace = _repository_inspection(
        repository, accession, analysis_unit_handoff, analysis_unit_handoff_path, host, port
    )
    from .repository_qa import repository_internal_standard_evidence

    if float(maximum_gb) <= 0:
        raise ValueError("maximum_gb must be greater than zero.")
    size = _required_download_size(project)
    required_bytes = size["required_download_bytes"]
    maximum_bytes = int(float(maximum_gb) * 1000**3)
    intent = _analysis_intent(analysis_purpose)
    blocking_reasons = _repository_download_blockers(
        project, intent, required_bytes, maximum_bytes, allow_preflight=False
    )

    return {
        "execution_scope": dict(REPOSITORY_EXECUTION_SCOPE),
        "analysis_intent": intent,
        "project": _repository_project_summary(project, workspace),
        "metadata": {
            "default_class_hierarchy": workspace.get("hierarchy", []),
            "class_hierarchy_note": (
                "No biological grouping field was selected automatically; review sample metadata and define Class explicitly."
                if not workspace.get("hierarchy")
                else "Review the proposed biological grouping fields before saving Class assignments."
            ),
            "fields": workspace.get("fields", []),
            "field_count": len(workspace.get("fields", [])),
            "row_count": len(workspace.get("rows", [])),
        },
        "qa_internal_standard_evidence": repository_internal_standard_evidence(workspace),
        "download": {
            "workspace_root": workspace_root,
            "maximum_gb": maximum_gb,
            "unit_file_bytes": size["unit_file_bytes"],
            "required_download_bytes": required_bytes,
            "declared_bundle_bytes": size["declared_bundle_bytes"],
            "bundle_bytes_verified": size["bundle_bytes_verified"],
            "bundle_bytes_contradicted": size["bundle_bytes_contradicted"],
            "within_size_limit": required_bytes <= maximum_bytes,
            "raw_retention_policy": raw_retention_policy,
            "confirmation_required": True,
            "ready": bool(project.get("eligible")) and not blocking_reasons,
            "blocking_reasons": blocking_reasons,
        },
        "next_decisions": [
            intent["prompt"],
            "Confirm that the unit is untargeted LC-MS/MS and distinguish DDA from DIA/AIF/SWATH, with one ion mode.",
            "Review and confirm the ordered metadata fields used to build MS-DIAL Class.",
            "Choose whether downloaded raw data are kept or deleted only after validated output.",
            "Confirm the bounded raw-data download before starting it.",
        ],
    }


@mcp.tool()
@_structured_validation_errors
def msdial_download_repository_raw(
    repository: str,
    accession: str,
    workspace_root: str,
    maximum_gb: float = 20,
    raw_retention_policy: str = "keep",
    allow_preflight: bool = False,
    confirmed: bool = False,
    analysis_unit_handoff: dict[str, Any] | None = None,
    analysis_unit_handoff_path: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    analysis_purpose: str = "",
) -> dict[str, Any]:
    """Download and recognize repository raw data only after explicit user confirmation.

    ``maximum_gb`` is decimal GB (1e9 bytes), matching how repositories report size.
    """
    workspace_root = _validated_workspace_root(workspace_root)
    project, workspace = _repository_inspection(
        repository, accession, analysis_unit_handoff, analysis_unit_handoff_path, host, port
    )
    if float(maximum_gb) <= 0:
        raise ValueError("maximum_gb must be greater than zero.")
    size = _required_download_size(project)
    required_bytes = size["required_download_bytes"]
    maximum_bytes = int(float(maximum_gb) * 1000**3)
    intent = _analysis_intent(analysis_purpose)
    blocking_reasons = _repository_download_blockers(
        project, intent, required_bytes, maximum_bytes, allow_preflight
    )
    preview = {
        "execution_scope": dict(REPOSITORY_EXECUTION_SCOPE),
        "analysis_intent": intent,
        "project": _repository_project_summary(project, workspace),
        "workspace_root": workspace_root,
        "maximum_gb": maximum_gb,
        "raw_retention_policy": raw_retention_policy,
        "allow_preflight": allow_preflight,
        "unit_file_bytes": size["unit_file_bytes"],
        "required_download_bytes": required_bytes,
        "declared_bundle_bytes": size["declared_bundle_bytes"],
        "bundle_bytes_verified": size["bundle_bytes_verified"],
        "bundle_bytes_contradicted": size["bundle_bytes_contradicted"],
        "within_size_limit": required_bytes <= maximum_bytes,
        "blocking_reasons": blocking_reasons,
    }
    if blocking_reasons:
        detail = "; ".join(blocking_reasons)
        return {
            "started": False,
            "blocked": True,
            "confirmation_required": False,
            "preview": preview,
            "message": f"Resolve all download blockers before continuing: {detail}",
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
    if required_bytes > maximum_bytes:
        raise ValueError(
            f"Required repository bundle is {required_bytes} bytes; the approved limit is "
            f"{maximum_bytes} bytes. Increase maximum_gb only after reviewing the bundle size."
        )
    repository_metadata = dict(project.get("repository_metadata") or {})
    repository_metadata["analysis_purpose"] = intent["purpose"]
    project["repository_metadata"] = repository_metadata
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
@_structured_validation_errors
def msdial_repository_batch_plan(
    analysis_unit_handoffs: list[dict[str, Any]] | None = None,
    workspace_root: str = "",
    analysis_unit_handoff_paths: list[str] | None = None,
    maximum_gb_per_unit: float = 20,
    raw_retention_policy: str = "keep",
    analysis_purpose: str = "",
) -> dict[str, Any]:
    """Expand a mixed repository accession into independent analysis-unit run plans.

    ``maximum_gb_per_unit`` is decimal GB (1e9 bytes), matching how repositories
    report size.
    """
    workspace_root = _validated_workspace_root(workspace_root)
    if float(maximum_gb_per_unit) <= 0:
        raise ValueError("maximum_gb_per_unit must be greater than zero.")
    maximum_bytes = int(float(maximum_gb_per_unit) * 1000**3)
    handoffs = list(analysis_unit_handoffs or [])
    for handoff_path in analysis_unit_handoff_paths or []:
        loaded = _load_analysis_unit_handoff(None, handoff_path)
        if loaded is not None:
            handoffs.append(loaded)
    if not handoffs:
        raise ValueError("Provide at least one analysis-unit handoff or handoff path.")
    intent = _analysis_intent(analysis_purpose)
    seen: set[str] = set()
    runs = []
    for handoff in handoffs:
        project, workspace = _project_from_analysis_unit_handoff(handoff)
        unit_id = str(project["analysis_unit_id"])
        if unit_id in seen:
            raise ValueError(f"Duplicate analysis_unit_id in batch: {unit_id}")
        seen.add(unit_id)
        blocking = list(
            dict.fromkeys(
                [
                    *project.get("exclusion_reasons", []),
                    *project.get("review_reasons", []),
                    *project.get("blocking_reasons", []),
                ]
            )
        )
        if not intent["confirmed"]:
            blocking.append("analysis_purpose:missing")
        size = _required_download_size(project)
        required_bytes = size["required_download_bytes"]
        if required_bytes > maximum_bytes:
            blocking.append("size_limit:exceeded")
        pending_decisions = list(project.get("pending_decisions", []))
        pending_decisions.extend(intent["pending_decisions"])
        runs.append(
            {
                "analysis_unit_id": unit_id,
                "repository": project["repository"],
                "accession": project["accession"],
                "technical_settings": handoff.get("technical_settings") or {},
                "project": _repository_project_summary(project, workspace),
                "workspace": str(
                    Path(workspace_root)
                    / project["repository"]
                    / project["accession"]
                    / unit_id
                ),
                "blocking_reasons": list(dict.fromkeys(blocking)),
                "required_download_bytes": required_bytes,
                "unit_file_bytes": size["unit_file_bytes"],
                "bundle_bytes_contradicted": size["bundle_bytes_contradicted"],
                "within_size_limit": required_bytes <= maximum_bytes,
                "pending_decisions": list(dict.fromkeys(pending_decisions)),
                "ready": bool(project.get("eligible")) and not blocking,
                "next_tool": "msdial_download_repository_raw",
            }
        )
    return {
        "schema": "msdial-repository-batch-plan.v1",
        "execution_scope": dict(REPOSITORY_EXECUTION_SCOPE),
        "analysis_intent": intent,
        "workspace_root": workspace_root,
        "raw_retention_policy": raw_retention_policy,
        "maximum_gb_per_unit": maximum_gb_per_unit,
        "run_count": len(runs),
        "ready_count": sum(bool(item["ready"]) for item in runs),
        "blocked_count": sum(not bool(item["ready"]) for item in runs),
        "execution_model": "sequential-independent-analysis-units",
        "runs": runs,
    }


@mcp.tool()
@_structured_validation_errors
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
@_structured_validation_errors
def msdial_cleanup_repository_raw(
    download_job_id: str = "",
    manifest_path: str = "",
    confirmed: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Preview, and only on explicit confirmation perform, deletion of one unit's downloaded raw data.

    With confirmed=false nothing is deleted and the result carries what a deletion would remove and what
    would survive it: the target path, the file count and bytes that would be freed, the retained
    artifact inventory, and any blockers. Only a caller that has shown those to the user and received an
    explicit answer may call again with confirmed=true. A run finishing does not authorise this, whatever
    retention policy was chosen at download time.

    Either identifier works. manifest_path is accepted because the job registry keeps only the most
    recently updated jobs and downgrades running jobs on restart, so a unit whose download job has aged
    out would otherwise have no way back to its own raw data.
    """
    from .repository_reanalysis import cleanup_download_lease

    resolved = Path(str(manifest_path or "")).expanduser()
    if not resolved.is_file():
        if not download_job_id:
            raise ValueError("Provide either download_job_id or a manifest_path that exists.")
        _, manifest = _repository_download_job(download_job_id, host, port)
        resolved = Path(manifest["manifest_path"])
    result = cleanup_download_lease(resolved, confirmed=confirmed)
    if not confirmed:
        result["message"] = (
            "Nothing was deleted. Show the deletion target, the size and the retained artifacts to the "
            "user, and call again with confirmed=true only after an explicit answer."
        )
    return result


@mcp.tool()
@_structured_validation_errors
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

    from .repository_metadata import apply_class_proposal

    project = manifest.get("project") or {}
    workspace = metadata_workspace(project)
    # The Catalog owns the scientific decision and records it as one Class label per sample. Saving
    # that proposal is the confirmation, so a proposal reaching here has already been approved; the
    # Catalog does not move its status field afterwards, and gating on a status it never sets would
    # silently turn this whole path off.
    proposal = project.get("class_proposal") or {}
    if proposal.get("assignments"):
        approved_fields = [str(item) for item in proposal.get("selected_fields") or []]
        if hierarchy is not None and list(hierarchy) != approved_fields:
            raise RuntimeError(
                "This unit has an approved Class proposal, so its per-sample assignments are applied "
                f"and a hierarchy argument may only restate the fields it selected ({approved_fields}). "
                f"Requested {list(hierarchy)}. To group the samples differently, create and approve a "
                "new proposal in the Catalog rather than re-deriving Class here."
            )
        projected = apply_class_proposal(workspace, proposal)
        selected_hierarchy = list(projected.get("hierarchy") or [])
    else:
        selected_hierarchy = list(
            hierarchy if hierarchy is not None else workspace.get("hierarchy", [])
        )
        projected = project_class_hierarchy(workspace, selected_hierarchy)
    recognized = ((job.get("result") or {}).get("recognized") or {}).get("files", [])
    application = apply_classes_to_analysis_files(projected, recognized)
    output_root = str(manifest.get("output_directory") or "")
    raw_retention_policy = str(job.get("raw_retention_policy") or "keep")
    answer_seed = _repository_answer_seed(
        projected, manifest, output_root, raw_retention_policy
    )
    from .repository_qa import repository_internal_standard_evidence

    # Preparing the analysis CSV is allowed for an ineligible unit -- reviewing its metadata is part of
    # how a unit becomes eligible. Running MS-DIAL is not, and that refusal happens at the run endpoint.
    # Stating the verdict here means a caller finds out before it plans a run, not after it starts one.
    execution_allowed = manifest.get("execution_allowed") is True
    execution_blockers: list[str] = []
    if not execution_allowed:
        execution_blockers.append(
            "execution_allowed is not true for this analysis unit "
            f"(status {manifest.get('status', 'unknown')!r}). MS-DIAL will refuse to start until a "
            "raw-header preflight settles the unit's technical conditions."
        )
    preview = {
        "download_job_id": download_job_id,
        "manifest_path": manifest["manifest_path"],
        "analysis_input_path": manifest.get("analysis_input_path"),
        "output_root": output_root,
        "execution_allowed": execution_allowed,
        "execution_blockers": execution_blockers,
        "class_source": projected.get("class_source", "hierarchy"),
        "class_proposal_provenance": projected.get("class_proposal_provenance"),
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
@_structured_validation_errors
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
@_structured_validation_errors
def msdial_list_worksets(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """List the five built-in MS-DIAL worksets and user-saved worksets."""
    return _request_json("GET", "/api/agent/worksets", host=host, port=port)


@mcp.tool()
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
def msdial_estimate_peak_height(
    job_id: str,
    target_peak_count: int = 0,
    target_peak_count_min: int = 3000,
    target_peak_count_max: int = 6000,
    threshold_step: int = 0,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Estimate a stepped Minimum peak height from a completed zero-threshold diagnostic."""
    return _request_json(
        "POST",
        "/api/agent/tuning/estimate",
        host=host,
        port=port,
        body={
            "job_id": job_id,
            "target_peak_count": target_peak_count,
            "target_peak_count_min": target_peak_count_min,
            "target_peak_count_max": target_peak_count_max,
            "threshold_step": threshold_step,
        },
        timeout=30,
    )


@mcp.tool()
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
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
@_structured_validation_errors
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

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from http.server import ThreadingHTTPServer
from pathlib import Path
from functools import wraps
from typing import Any

from . import __version__
from .campaign_authorization import CampaignAuthorizationError

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
REQUIRED_AGENT_API_VERSION = "0.5"
REPOSITORY_EXECUTION_SCOPE = {
    "project_type": "LC-MS/MS",
    "acquisition_modes": ["DDA", "DIA", "AIF", "SWATH"],
    "untargeted": True,
    "requires_ms1_survey": True,
    "requires_product_ion_spectra": True,
}
_EMBEDDED_SERVERS: dict[tuple[str, int], tuple[ThreadingHTTPServer, threading.Thread]] = {}
# When this process started. A restart relaunches the HTTP listener inside this process, with the code
# this process has already imported, so it can tell a caller whether the source on disk has moved on.
_PROCESS_STARTED_AT = time.time()


def _restart_code_note() -> dict[str, Any]:
    """Say what a restart did not do: load source that changed after this process started.

    The restart used to report restarted=true and describe itself as replacing the process "with the
    current source version". The listener is embedded in this MCP server process, and relaunching it
    re-uses the modules this process imported, so after a merge the restarted backend was the old
    code - a missing endpoint answered 404 behind a report of success. Only a new process loads new
    source; reconnecting the MCP server starts one.
    """
    package = Path(__file__).resolve().parent
    changed = sorted(
        path.name for path in package.glob("*.py") if path.stat().st_mtime > _PROCESS_STARTED_AT
    )
    note: dict[str, Any] = {"code_reloaded": False, "source_changed_since_process_start": changed}
    if changed:
        note["warnings"] = [
            f"{len(changed)} source file(s) changed after this MCP server process started "
            f"({', '.join(changed[:5])}). The restarted backend runs inside this process with the "
            "code it already loaded, so those changes are not in effect. Reconnect the "
            "msdial-interactive MCP server to load them."
        ]
    return note


try:
    from mcp.server import MCPServer

    mcp = MCPServer("MS-DIAL Interactive", version=__version__)
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
        self,
        detail: str,
        *,
        status: int | None = None,
        endpoint: str = "",
        trace: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.endpoint = endpoint
        self.trace = trace
        # The backend's whole answer, for the refusals that carry more than a sentence: a busy unit
        # names the job that holds it.
        self.payload = dict(payload or {})

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
            detail,
            status=error.code,
            endpoint=f"{method} {path}",
            trace=trace,
            payload=payload if isinstance(payload, dict) else None,
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
            refused = _refused_authorization_codes(error.detail)
            if refused is not None:
                # Refused by the backend, which checks the approval again before it writes anything.
                failure["reason"] = "campaign_authorization_refused"
                failure["codes"] = refused
            if error.status == 409 and error.payload.get("code") == "unit_busy":
                # The unit already has a live Console job. Nothing was started; the job named is the
                # one to wait for or cancel, and a start that timed out on the caller's side is found here.
                failure["reason"] = "unit_busy"
                failure["live_job_id"] = str(error.payload.get("live_job_id") or "")
                failure["repository_run_manifest"] = str(error.payload.get("repository_run_manifest") or "")
            if error.status is not None:
                failure["http_status"] = error.status
            if error.trace:
                failure["trace"] = error.trace
            return failure
        except CampaignAuthorizationError as error:
            return {
                "ok": False,
                "reason": "campaign_authorization_refused",
                "codes": list(error.codes),
                "detail": str(error),
                "error_type": type(error).__name__,
            }
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as error:
            refused = _refused_extractor_codes(str(error))
            if refused is not None:
                # A campaign asked for a verified, pinned raw-metadata extractor and the one selected is
                # not. A missing precondition of the campaign, not a failure of the unit.
                return {
                    "ok": False,
                    "reason": "raw_metadata_extractor_refused",
                    "codes": refused,
                    "detail": str(error),
                    "error_type": type(error).__name__,
                }
            return {
                "ok": False,
                "reason": "validation_error",
                "detail": str(error),
                "error_type": type(error).__name__,
            }
        except OSError as error:
            # A unit manifest held by another writer, or kept unreadable by renames, for longer than the
            # manifest layer waits: nothing was changed, and the same call can be repeated. These were
            # unmapped, while the JSONDecodeError a torn read gave before them was mapped.
            from .repository_reanalysis import ManifestBusyError

            busy = isinstance(error, ManifestBusyError)
            return {
                "ok": False,
                "reason": "manifest_busy" if busy else "os_error",
                "retryable": busy,
                "detail": str(error),
                "error_type": type(error).__name__,
            }

    return wrapped


def _refused_codes(detail: str, token: str) -> list[str] | None:
    text = str(detail or "")
    if not text.startswith(token) or "]" not in text:
        return None
    return [item.strip() for item in text[len(token):text.index("]")].split(",") if item.strip()]


def _refused_authorization_codes(detail: str) -> list[str] | None:
    return _refused_codes(detail, "campaign_authorization_refused [")


def _refused_extractor_codes(detail: str) -> list[str] | None:
    return _refused_codes(detail, "raw_metadata_extractor_refused [")


def _campaign_authorization(
    path: str,
    manifest_or_project: dict[str, Any],
    boundary: Any,
    entry_point: str,
    raw_retention_policy: str | None = None,
) -> dict[str, Any] | None:
    """Validate a campaign approval for one unit and boundary; None when none was passed.

    Accepts a unit manifest (which may name a split parent) or a project from a handoff.
    """
    from .campaign_authorization import authorize, unit_identity

    if not str(path or "").strip():
        return None
    if isinstance(manifest_or_project.get("project"), dict):
        unit, parent = unit_identity(manifest_or_project)
    else:
        unit, parent = str(manifest_or_project.get("analysis_unit_id") or ""), ""
    return authorize(
        path,
        unit,
        boundary,
        entry_point=entry_point,
        parent_unit_id=parent,
        raw_retention_policy=raw_retention_policy,
    )


def _campaign_coverage(path: str, unit_id: str, raw_retention_policy: str) -> dict[str, Any] | None:
    """Whether a campaign approval would cover one unit's download, for the plan tools.

    A unit it does not cover is reported, not refused; only a record that cannot be read, or is itself
    invalid, raises.
    """
    from .campaign_authorization import load_campaign_authorization

    authorization = load_campaign_authorization(path)
    if authorization is None:
        return None
    verdict = authorization.check(unit_id, 1, raw_retention_policy=raw_retention_policy)
    return {
        "approval_id": authorization.approval_id,
        "manifest_digest": authorization.manifest_digest,
        "valid": verdict["valid"],
        "codes": verdict["codes"],
        "reasons": verdict["reasons"],
    }


def _campaign_converts_mzxml(path: str, unit_id: str, raw_retention_policy: str | None) -> bool:
    """Whether a unit's mzXML is converted: only where a campaign approval covers its download (boundary 1).

    Only a campaign converts (EligibilityPolicy.convert_mzxml). An approval that does not cover the unit
    converts nothing here, and the tool that crosses the boundary refuses it; only a record that cannot be
    read, or is itself invalid, raises, as it would there.
    """
    if not str(path or "").strip() or not unit_id:
        return False
    coverage = _campaign_coverage(path, unit_id, raw_retention_policy)
    return bool(coverage and coverage["valid"])


def _repository_unit(
    download_job_id: str, manifest_path: str, host: str, port: int
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The unit a repository tool acts on: by manifest when one is named, else by its download job.

    The manifest route needs no backend and no registry entry, which is the point: the job registry keeps
    only its hundred newest jobs, and a unit reached weeks after its download would otherwise be lost.
    """
    if str(manifest_path or "").strip():
        from .repository_reanalysis import load_unit_manifest

        return None, load_unit_manifest(manifest_path)
    if not str(download_job_id or "").strip():
        raise ValueError("Provide download_job_id or manifest_path.")
    return _repository_download_job(download_job_id, host, port)


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
    # A part of a split unit has no download of its own, and is addressed through a job record the
    # split registered for it; its manifest names the parent whose raw files it reads.
    if job.get("kind") not in {"repository_download", "repository_split_part"}:
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
    from .repository_reanalysis import read_manifest

    manifest = read_manifest(manifest_path)
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


# The blocking reason a unit carries when the Catalog's analysis-input counts and its own listing disagree.
ANALYSIS_INPUT_COUNT_MISMATCH = "analysis_input:count_mismatch"
# Catalog issue codes that already block the unit, and under which sample rows and inputs may not pair up.
_CATALOG_BLOCKING_INPUT_ISSUES = frozenset(
    {
        "container_shared_by_samples",
        "sample_without_container",
        "container_without_sample",
        "declared_directory_not_msdial_input",
    }
)


def _inputs_unpaired_with_rows(inputs: list[dict[str, Any]], samples: list[dict[str, Any]]) -> list[str]:
    """Where the Catalog's declared inputs and its sample rows do not pair one to one; [] where they do.

    ONE INPUT PER SAMPLE ROW, NOT PER SAMPLE ID. A repository lists each injection as a row of its own, and
    the rows of one sample share its id: MetaboBank MTBKS64 names S01 in two rows, Assay Names S01_M01 and
    S01_M02, one per .RAW. The Catalog lists one input per row, each with the row's sample_id, and counts
    them alike (analysis_input_count and analytical_sample_count are both the inputs), so this check, which
    read a second input of S01 as a second input of one row, blocked a consistent unit before its download.
    Each input names a sample that has rows; a sample is named by as many inputs as it has rows; and where
    it has more than one, each of its inputs is the row whose raw_file is the input's path (or, for an
    archived container, its archive's) - the path the Catalog's projection writes into the row it attributed
    the input to - else the one of them with the input's file name. Two inputs on one row, or an input none
    of its sample's rows names, is still a disagreement.
    """
    from .repository_metadata import rows_naming_input

    problems: list[str] = []
    claimed = [str(item.get("sample_id") or "") for item in inputs]
    rows_of: dict[str, list[dict[str, Any]]] = {}
    for item in samples:
        rows_of.setdefault(str(item.get("sample_id") or ""), []).append(item)
    if len(samples) != len(inputs):
        problems.append(f"{len(samples)} sample rows are listed for {len(inputs)} analysis inputs")
    if any(not value or value not in rows_of for value in claimed):
        problems.append("an analysis input names no sample row of this unit")
    shared_row = False
    unnamed: list[str] = []
    for sample_id, count in Counter(claimed).items():
        rows = rows_of.get(sample_id) or []
        if not sample_id or not rows:
            continue
        if len(rows) == 1:
            shared_row = shared_row or count > 1
            continue
        if count > len(rows):
            problems.append(f"{count} analysis inputs name the {len(rows)} sample rows of sample {sample_id!r}")
            continue
        taken: set[int] = set()
        for item in inputs:
            if str(item.get("sample_id") or "") != sample_id:
                continue
            # As the analysis-CSV builder pairs it after the download (rows_naming_input): by path, else name.
            found = rows_naming_input((item.get("path"), item.get("archive")), rows, range(len(rows)))
            if len(found) != 1:
                unnamed.append(str(item.get("path") or ""))
            elif found[0] in taken:
                shared_row = True
            else:
                taken.add(found[0])
    if shared_row:
        problems.append("two analysis inputs name the same sample row")
    if unnamed:
        problems.append(
            "analysis inputs of a sample that several rows describe are named by none, or by more than one, of "
            "those rows' raw files: " + ", ".join(sorted(unnamed, key=str.casefold)[:5])
        )
    return problems


def _handoff_analysis_inputs(
    handoff: dict[str, Any],
    unit_id: str,
    files: list[dict[str, Any]],
    samples: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The Catalog's declared analysis inputs, and whether its counts agree with its own listing.

    Catalog 0.6.0 lists one analysis input per sample row (analysis_input_model one-input-per-sample.v1):
    a Waters .raw or Agilent/Bruker .d folder is one input and its files are members. The list is inline,
    or in analysis_input_manifest_path when the handoff omits it, as files and samples may be. A handoff
    without analysis_input_model predates it and returns ([], {}).

    The check compares the counts the handoff states with the lists it carries: analysis_input_count,
    analytical_sample_count and download_scope.analysis_file_count against the inputs; each vendor folder
    against the members listed for it; and, where no Catalog issue already blocks the unit, the sample rows
    against the inputs, one input per row (_inputs_unpaired_with_rows: rows may share a sample id). A
    disagreement is returned as {"status": "failed", "problems": [...]}, for the caller to record against
    this unit, never raised.
    """
    if "analysis_input_model" not in handoff:
        return [], {}
    payload = handoff.get("analysis_inputs") or []
    if handoff.get("analysis_inputs_omitted") or (
        not payload and handoff.get("analysis_inputs_declared") and handoff.get("analysis_input_manifest_path")
    ):
        path = Path(str(handoff.get("analysis_input_manifest_path") or "")).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(
                f"External analysis-input manifest for analysis unit {unit_id} was not found: {path}"
            )
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, list):
            raise ValueError("External analysis-unit input manifest must contain a JSON array.")
    inputs = [dict(item) for item in payload if isinstance(item, dict)]
    problems: list[str] = []
    declared = handoff.get("analysis_inputs_declared")
    if declared is not None and bool(declared) != bool(inputs):
        problems.append(
            f"the handoff says analysis_inputs_declared={bool(declared)} but lists {len(inputs)} inputs"
        )
    scope = handoff.get("download_scope") or {}
    for label, value in (
        ("analysis_input_count", handoff.get("analysis_input_count")),
        ("analytical_sample_count", handoff.get("analytical_sample_count")),
        ("download_scope.analysis_file_count", scope.get("analysis_file_count")),
    ):
        if value is not None and int(value or 0) != len(inputs):
            problems.append(f"{label} is {int(value or 0)} but {len(inputs)} analysis inputs are listed")
    paths = [str(item.get("path") or "").replace("\\", "/").strip().rstrip("/").casefold() for item in inputs]
    if "" in paths:
        problems.append("an analysis input names no path")
    doubled = sorted(path for path, count in Counter(paths).items() if path and count > 1)
    if doubled:
        problems.append("analysis inputs listed twice: " + ", ".join(doubled[:5]))
    listed_members: dict[str, int] = {}
    for item in files:
        if item["role"] == "vendor_folder_member":
            key = str(item.get("container") or "").replace("\\", "/").strip().rstrip("/").casefold()
            listed_members[key] = listed_members.get(key, 0) + 1
    folders = {
        path: item for path, item in zip(paths, inputs) if str(item.get("kind") or "") == "vendor_folder"
    }
    for path, item in folders.items():
        count = listed_members.get(path, 0)
        if not count:
            problems.append(f"vendor folder {item.get('path')} has no member in the file listing")
        elif item.get("member_count") is not None and int(item.get("member_count") or 0) != count:
            problems.append(
                f"vendor folder {item.get('path')} declares {int(item.get('member_count') or 0)} members "
                f"but the file listing holds {count}"
            )
    orphans = sorted(key or "(none)" for key in listed_members if key not in folders)
    if orphans:
        problems.append("folder members of a folder no analysis input names: " + ", ".join(orphans[:5]))
    listed_names = {str(item["name"]).replace("\\", "/").casefold() for item in files}
    for item in inputs:
        archive = str(item.get("archive") or "").replace("\\", "/").casefold()
        if str(item.get("kind") or "") == "archived_container" and archive and archive not in listed_names:
            problems.append(f"archived container {item.get('path')} names an archive the file listing lacks")
    blocked = {
        str(item.get("code") or "")
        for item in handoff.get("analysis_input_issues") or []
        if isinstance(item, dict) and item.get("blocking")
    } & _CATALOG_BLOCKING_INPUT_ISSUES
    if inputs and not blocked:
        # One input per sample row, each naming a row that exists. Where the Catalog already blocks the
        # unit for the way rows and folders pair, the pairing is its issue to report, not a count mismatch.
        problems.extend(_inputs_unpaired_with_rows(inputs, samples))
    if not problems:
        return inputs, {"status": "passed", "analysis_inputs": len(inputs), "members": sum(listed_members.values())}
    return inputs, {
        "status": "failed",
        "code": ANALYSIS_INPUT_COUNT_MISMATCH,
        "problems": [
            f"Analysis unit {unit_id}: the Catalog's analysis-input counts disagree with its listing: {problem}."
            for problem in problems
        ],
        "analysis_inputs": len(inputs),
        "sample_rows": len(samples),
        "members": sum(listed_members.values()),
    }


def _project_from_analysis_unit_handoff(
    handoff: dict[str, Any], repository: str = "", accession: str = "", *, convert_mzxml: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A Catalog handoff as the project the download tools judge.

    ``convert_mzxml`` is set only where a campaign approval covers the unit (_campaign_converts_mzxml): its
    mzXML then plans a conversion the lease makes. Otherwise an mzXML or mzData excludes the unit before
    download, as it always did.
    """
    from .repository_reanalysis import requires_msdial_conversion

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
        # A vendor_folder_member keeps its role: it is downloaded and checksummed, and is never an input,
        # so neither eligibility nor the allow-list reads it as one (a Waters _FUNC001.DAT is not a .dat
        # file to convert). Its container says which folder it makes up.
        role = str(item.get("role") or "raw")
        if role in {"raw", "converted"} and (
            bool(item.get("requires_conversion")) or requires_msdial_conversion(path)
        ):
            role = "requires_conversion"
        files.append(
            {
                "name": path,
                "size_bytes": int(item.get("size_bytes") or 0),
                "url": url,
                "role": role,
                "checksum": str(item.get("checksum") or ""),
                "container": str(item.get("container") or ""),
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
    analysis_inputs, input_check = _handoff_analysis_inputs(handoff, unit_id, files, samples)
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
        # The rows, not the distinct ids, where the Catalog counted no input: replicate rows share an id, and
        # each is an injection (MetaboLights MTBLS291 lists 40 mzML in the rows of 8 samples).
        "sample_count": int(
            handoff.get("analytical_sample_count")
            or scope.get("analysis_file_count")
            or sum(1 for item in samples if item["sample_id"])
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
        "analysis_inputs": analysis_inputs,
        "analysis_inputs_declared": bool(analysis_inputs),
        "analysis_input_issues": [
            dict(item) for item in handoff.get("analysis_input_issues") or [] if isinstance(item, dict)
        ],
        "split_hint": handoff.get("split_hint") if isinstance(handoff.get("split_hint"), dict) else None,
    }
    if input_check:
        project["repository_metadata"]["analysis_input_check"] = input_check
    from .repository_reanalysis import EligibilityPolicy, evaluate_eligibility, project_from_dict

    typed = evaluate_eligibility(
        project_from_dict(project),
        EligibilityPolicy(
            max_download_bytes=max(int(scope.get("bundle_bytes") or 0), project["total_download_bytes"], 1),
            max_samples=max(project["sample_count"], 1),
            require_known_size=False,
            require_untargeted=True,
            convert_mzxml=convert_mzxml,
        ),
    )
    declared_blocks = [str(item) for item in handoff.get("blocking_reasons") or []]
    technical_blocks = [item for item in declared_blocks if item.startswith("technical_metadata:")]
    decision_blocks = [item for item in declared_blocks if not item.startswith("technical_metadata:")]
    if technical_blocks and not typed.exclusion_reasons:
        typed.review_reasons = list(dict.fromkeys([*typed.review_reasons, *technical_blocks]))
        typed.eligible = False
        typed.selection_status = "raw_metadata_required"
    if input_check.get("status") == "failed":
        # A FAILURE RECORD FOR THIS UNIT, NOT AN EXCEPTION FOR THE BATCH. The Catalog's counts and its own
        # listing disagree, so which folder is which sample cannot be relied on; the unit is excluded with
        # the reason, and a batch or campaign planning it goes on to the next unit.
        decision_blocks.append(ANALYSIS_INPUT_COUNT_MISMATCH)
        typed.exclusion_reasons = list(
            dict.fromkeys([*typed.exclusion_reasons, *input_check["problems"]])
        )
        typed.review_reasons = []
        typed.eligible = False
        typed.selection_status = "excluded"
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
    campaign_authorization_path: str = "",
    raw_retention_policy: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The project and sample workspace of one unit's handoff, or of an accession inspected by the backend.

    A unit's mzXML plans a conversion only where ``campaign_authorization_path`` covers its download
    (_campaign_converts_mzxml); an accession inspected without a handoff is no campaign unit.
    """
    analysis_unit_handoff = _load_analysis_unit_handoff(
        analysis_unit_handoff, analysis_unit_handoff_path
    )
    if analysis_unit_handoff:
        return _project_from_analysis_unit_handoff(
            analysis_unit_handoff,
            repository,
            accession,
            convert_mzxml=_campaign_converts_mzxml(
                campaign_authorization_path,
                str(analysis_unit_handoff.get("analysis_unit_id") or "").strip(),
                raw_retention_policy,
            ),
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
    """The extractors that exist, in the order a preflight tries them.

    The argument, then the raw_metadata_extractor_path setting, then MSDIAL_RAW_METADATA_EXTRACTOR, then the
    build in the msrawdataworkbench working checkout beside this one. raw_metadata_extractor_candidates
    labels each with its source; a campaign runs only the first, and never the working-checkout default.
    """
    from .raw_metadata_extractor import raw_metadata_extractor_candidates

    return [
        item["path"]
        for item in raw_metadata_extractor_candidates(configured, checkout_parent=ROOT.parent)
        if item["exists"]
    ]


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
    """Stop the local MS-DIAL Interactive listener and start it again inside this MCP server process.

    This clears a hung or incompatible listener. It does not load new source: the listener runs with
    the code this process has already imported. The result says which source files changed after the
    process started; when any did, reconnect the MCP server to load them.
    """
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
            return {"restarted": False, "already_current": True, **_restart_code_note(), **current}
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
    return {"restarted": True, "previous_process": process_info, **_restart_code_note(), **launched}


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
    campaign_authorization_path: str = "",
) -> dict[str, Any]:
    """Inspect one public accession and return metadata, QA evidence, and download decisions.

    ``maximum_gb`` is decimal GB (1e9 bytes), matching how repositories report size.
    ``campaign_authorization_path`` reports whether a campaign approval covers this unit's download; it
    changes no limit and starts nothing.
    """
    workspace_root = _validated_workspace_root(workspace_root)
    project, workspace = _repository_inspection(
        repository,
        accession,
        analysis_unit_handoff,
        analysis_unit_handoff_path,
        host,
        port,
        campaign_authorization_path=campaign_authorization_path,
        raw_retention_policy=raw_retention_policy,
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
    coverage = _campaign_coverage(
        campaign_authorization_path, str(project.get("analysis_unit_id") or ""), raw_retention_policy
    )
    if coverage and not coverage["valid"]:
        blocking_reasons.extend(f"campaign_authorization:{code}" for code in coverage["codes"])

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
            **({"campaign_authorization": coverage} if coverage else {}),
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
    campaign_authorization_path: str = "",
) -> dict[str, Any]:
    """Download and recognize repository raw data only after explicit user confirmation.

    ``maximum_gb`` is decimal GB (1e9 bytes), matching how repositories report size.

    ``campaign_authorization_path`` names a recorded campaign approval. When it covers boundary 1 for
    this unit, with this retention policy, it stands in for confirmed=true and is written into the unit's
    manifest; when it does not, the download is refused even with confirmed=true. It lifts no limit:
    maximum_gb applies as before. Without it nothing changes.
    """
    workspace_root = _validated_workspace_root(workspace_root)
    project, workspace = _repository_inspection(
        repository,
        accession,
        analysis_unit_handoff,
        analysis_unit_handoff_path,
        host,
        port,
        campaign_authorization_path=campaign_authorization_path,
        raw_retention_policy=raw_retention_policy,
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
    crossing = _campaign_authorization(
        campaign_authorization_path,
        project,
        1,
        "msdial_download_repository_raw",
        raw_retention_policy=raw_retention_policy,
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
    if crossing:
        preview["campaign_authorization"] = crossing
    from .user_settings import download_store_mode

    if crossing or download_store_mode() == "always":
        # The lease fetches through the accession download store: what it already holds transfers nothing.
        # required_download_bytes stays the approval quantity; this is what the transfer will actually move.
        from .repository_reanalysis import plan_batch_downloads, unit_download_objects

        store_plan = plan_batch_downloads(
            [{
                "analysis_unit_id": project.get("analysis_unit_id"),
                "repository": project.get("repository"),
                "accession": project.get("accession"),
                "objects": unit_download_objects(project),
            }],
            workspace_root,
        )
        preview["download_store"] = {
            key: store_plan[key]
            for key in (
                "object_count", "objects_in_store", "distinct_bytes", "distinct_bytes_lower_bound",
                "unknown_size_objects", "distinct_bytes_to_transfer",
            )
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
    if not confirmed and crossing is None:
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
            **(
                {"campaign_authorization_path": campaign_authorization_path}
                if crossing
                else {}
            ),
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
    campaign_authorization_path: str = "",
    pre_claim: bool = False,
) -> dict[str, Any]:
    """Expand a mixed repository accession into independent analysis-unit run plans.

    ``maximum_gb_per_unit`` is decimal GB (1e9 bytes), matching how repositories
    report size. ``campaign_authorization_path`` reports, per unit, whether a campaign approval covers
    its download; a unit it does not cover is not ready under it.

    ``download_plan`` is what the accession download store makes of the batch: the distinct objects with
    the units that consume each, per-unit and distinct bytes (per_unit_known_bytes against
    distinct_bytes), the sharing groups, and ``run_order``, which keeps each group together. ``pre_claim``
    records, for each ready unit the approval covers, a pending store claim on each of its objects, so a
    shared object is kept for units that have not run yet; it needs ``campaign_authorization_path``.
    """
    from .repository_reanalysis import (
        is_reserved_workspace_name,
        plan_batch_downloads,
        pre_claim_downloads,
        unit_download_objects,
    )

    if pre_claim and not str(campaign_authorization_path or "").strip():
        raise ValueError(
            "pre_claim records store claims for a campaign's approved units, and needs campaign_authorization_path."
        )
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
    download_units = []
    for handoff in handoffs:
        project, workspace = _project_from_analysis_unit_handoff(
            handoff,
            convert_mzxml=_campaign_converts_mzxml(
                campaign_authorization_path,
                str(handoff.get("analysis_unit_id") or "").strip(),
                raw_retention_policy,
            ),
        )
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
        if any(is_reserved_workspace_name(project[key]) for key in ("repository", "accession", "analysis_unit_id")):
            # _dl is the accession's download store and _campaigns the runner's; neither is ever a unit.
            blocking.append("workspace_name:reserved")
        size = _required_download_size(project)
        required_bytes = size["required_download_bytes"]
        if required_bytes > maximum_bytes:
            blocking.append("size_limit:exceeded")
        coverage = _campaign_coverage(campaign_authorization_path, unit_id, raw_retention_policy)
        if coverage and not coverage["valid"]:
            blocking.extend(f"campaign_authorization:{code}" for code in coverage["codes"])
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
                **({"campaign_authorization": coverage} if coverage else {}),
            }
        )
        download_units.append(
            {
                "analysis_unit_id": unit_id,
                "repository": project["repository"],
                "accession": project["accession"],
                "objects": unit_download_objects(project),
            }
        )
    download_plan = plan_batch_downloads(download_units, workspace_root)
    by_unit = {item["analysis_unit_id"]: item for item in download_plan["units"]}
    for run in runs:
        unit = by_unit[run["analysis_unit_id"]]
        run.update(
            download_object_count=unit["object_count"],
            unit_object_bytes=unit["bytes"],
            unit_object_known_bytes=unit["known_bytes"],
            shared_object_count=unit["shared_object_count"],
            sharing_group=unit["sharing_group"],
            run_position=unit["run_position"],
        )
    plan = {
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
        "run_order": download_plan["run_order"],
        "download_plan": download_plan,
    }
    if pre_claim:
        covered = {
            run["analysis_unit_id"] for run in runs
            if run["ready"] and (run.get("campaign_authorization") or {}).get("valid")
        }
        plan["pre_claimed"] = pre_claim_downloads(
            workspace_root, [unit for unit in download_units if unit["analysis_unit_id"] in covered]
        )
        plan["not_pre_claimed"] = [
            {
                "analysis_unit_id": run["analysis_unit_id"],
                "reason": "blocked" if not run["ready"] else "not_covered_by_the_campaign_approval",
            }
            for run in runs
            if run["analysis_unit_id"] not in covered
        ]
    return plan


@mcp.tool()
@_structured_validation_errors
def msdial_check_raw_metadata_extractor(extractor_path: str = "") -> dict[str, Any]:
    """List the raw-metadata extractors a preflight would consider, with each one's build identity.

    The order is extractor_path, then the saved raw_metadata_extractor_path setting, then the
    MSDIAL_RAW_METADATA_EXTRACTOR environment variable, then the build in the msrawdataworkbench working
    checkout (labelled working_checkout_default). Each existing candidate is inspected: provenance_status
    (verified, absent, stale_mismatch, unreadable, dirty_source), whether its commits are a pinned build, and
    whether a campaign would accept it. Outside a campaign a preflight runs the first that exists; in a
    campaign it runs the first named, only if that one is verified and pinned. Changes nothing, needs no
    backend.
    """
    from .raw_metadata_extractor import check_raw_metadata_extractors

    return check_raw_metadata_extractors(extractor_path, checkout_parent=ROOT.parent)


@mcp.tool()
@_structured_validation_errors
def msdial_set_raw_metadata_extractor_path(extractor_path: str, allow_unverified: bool = False) -> dict[str, Any]:
    """Validate a RawMetadataConsoleApp.exe and persist it as the raw_metadata_extractor_path setting.

    Refused unless its build record verifies against the files on disk; allow_unverified=true saves it
    anyway for work outside a campaign. The reply says whether a campaign would accept it, which also
    needs its commits to be a pinned build.
    """
    from .raw_metadata_extractor import set_raw_metadata_extractor_path

    return set_raw_metadata_extractor_path(extractor_path, allow_unverified=allow_unverified)


@mcp.tool()
@_structured_validation_errors
def msdial_repository_raw_metadata_preflight(
    download_job_id: str = "",
    extractor_path: str = "",
    max_inputs: int = 0,
    confirm_untargeted: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    manifest_path: str = "",
    campaign_authorization_path: str = "",
) -> dict[str, Any]:
    """Read every downloaded input file's header with the local raw-metadata parser.

    max_inputs of 0 inspects every input candidate. A smaller cap is allowed, but MS-DIAL applies an
    acquisition type to each file, so a capped inspection leaves the unit under review. When the
    headers disagree about acquisition mode the unit is reported as Mixed, with its files grouped
    by mode, and cannot be made eligible until it is split.

    The extractor reads at most 20 inputs per process, each process within a time limit set by its
    inputs' formats and sizes, and a failed or timed-out group is read again one input at a time, so an
    unreadable file costs only its own verdict. The reply carries the unit's campaign_disposition (run,
    skip, exclude or split, with reason codes). A unit under a campaign approval - passed here as
    campaign_authorization_path, or recorded for the unit - has the disposition applied, and its extractor
    must inspect as verified and pinned (msdial_check_raw_metadata_extractor). A campaign unit that was
    split, has finished its run or has a run open is not read: completed is false and preflight_held says
    why.

    Give manifest_path instead of download_job_id to reach a unit whose download job the backend no
    longer holds.
    """
    _, manifest = _repository_unit(download_job_id, manifest_path, host, port)
    from .raw_metadata_extractor import select_raw_metadata_extractor
    from .repository_reanalysis import preflight_campaign, run_raw_metadata_preflight

    campaign = preflight_campaign(manifest, campaign_authorization_path)
    selected = select_raw_metadata_extractor(
        extractor_path, campaign=campaign is not None, checkout_parent=ROOT.parent
    )
    if not selected.get("path"):
        return {
            "completed": False,
            "extractor_found": False,
            "manifest_path": manifest["manifest_path"],
            "message": (
                "Set extractor_path, the raw_metadata_extractor_path setting "
                "(msdial_set_raw_metadata_extractor_path) or MSDIAL_RAW_METADATA_EXTRACTOR to a built "
                "RawMetadataConsoleApp executable. Repository metadata remains available."
            ),
        }

    result = run_raw_metadata_preflight(
        Path(manifest["manifest_path"]),
        Path(selected["path"]),
        max_inputs=max(0, max_inputs),
        confirm_untargeted=confirm_untargeted,
        campaign_authorization_path=campaign_authorization_path or None,
        require_pinned_extractor=campaign is not None,
        extractor_source=str(selected.get("source") or ""),
    )
    raw = result.get("raw_metadata_preflight") or {}
    # The per-file verdicts stay in the manifest; a unit of several hundred files would otherwise
    # put every one of them into the reply.
    summary = {key: value for key, value in (raw.get("summary") or {}).items() if key != "per_file"}
    groups = raw.get("acquisition_groups") or {}
    extractor = raw.get("extractor") or {}
    disposition = result.get("campaign_disposition") or {}
    held = result.get("preflight_held") or None
    return {
        # False for a campaign unit that is split, finished or running: nothing was recorded, and everything
        # below is what the unit already carried.
        "completed": held is None,
        "preflight_held": held,
        "extractor_found": True,
        "extractor_path": selected["path"],
        "extractor": {
            key: extractor.get(key)
            for key in (
                "selected_from", "sha256", "inventory_sha256", "provenance_status", "pinned", "pin_state",
                "msrawdataworkbench_commit", "msdialworkbench_commit",
            )
        },
        "manifest_path": result.get("manifest_path"),
        "status": result.get("status"),
        "execution_allowed": result.get("execution_allowed"),
        "exit_code": raw.get("exit_code"),
        "outcomes": raw.get("outcomes") or {},
        "summary": summary,
        "acquisition_groups": {mode: len(files) for mode, files in groups.items()},
        "advisory": raw.get("advisory"),
        "unsupported_formats": raw.get("unsupported_formats") or [],
        "retry_can_help": result.get("status") != "preflight_unsupported_format",
        "confirm_untargeted_applied": confirm_untargeted,
        "campaign_disposition": {
            "disposition": disposition.get("disposition"),
            "applied": disposition.get("applied"),
            "reasons": disposition.get("reasons") or [],
            "warnings": disposition.get("warnings") or [],
            "excluded_inputs": len(disposition.get("excluded_inputs") or []),
            "split_by": (disposition.get("split_key") or {}).get("by") or [],
            "console_acquisition_type": disposition.get("console_acquisition_type"),
            "ion_mode": disposition.get("ion_mode"),
        },
    }


@mcp.tool()
@_structured_validation_errors
def msdial_split_repository_unit(
    download_job_id: str = "",
    confirmed: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    manifest_path: str = "",
    campaign_authorization_path: str = "",
) -> dict[str, Any]:
    """Split a unit whose raw headers disagree about acquisition mode into one part per mode.

    Requires a completed raw-header preflight that read every input file and reported the unit as
    Mixed. With confirmed=false this previews the parts: which files, samples and Class levels each
    would hold. With confirmed=true it writes one workspace and run manifest per part beside the
    parent, marks the parent split, and returns a job id per part. Pass that job id to the preflight,
    prepare and start tools exactly as a download job id. Raw data are shared with the parent, not
    copied, and each part starts with execution_allowed false until its own preflight passes.

    manifest_path reaches the parent after its download job has left the registry. A campaign approval
    covering "split" for the unit stands in for confirmed=true and is recorded on the parent.
    """
    if not str(download_job_id or "").strip() and not str(manifest_path or "").strip():
        raise ValueError("Provide download_job_id or manifest_path.")
    result = _request_json(
        "POST",
        "/api/repository/split",
        host=host,
        port=port,
        body={
            "download_job_id": download_job_id,
            "manifest_path": manifest_path,
            "confirmed": bool(confirmed),
            "campaign_authorization_path": campaign_authorization_path,
        },
        timeout=120,
    )
    parts = []
    for part in result.get("parts") or []:
        files = part.get("input_candidates") or []
        parts.append(
            {
                "analysis_unit_id": part.get("analysis_unit_id"),
                "job_id": part.get("job_id"),
                "acquisition_mode": part.get("acquisition_mode"),
                "workspace": part.get("workspace"),
                "manifest_path": part.get("manifest_path"),
                "file_count": part.get("file_count", len(files)),
                "files": [Path(str(item)).name for item in files[:20]],
                "sample_ids": part.get("sample_ids") or [],
                "class_levels": part.get("class_levels") or {},
                "higher_ms_levels": part.get("higher_ms_levels") or [],
            }
        )
    authorized = result.get("campaign_authorization")
    return {
        "written": bool(result.get("written")),
        "already_split": bool(result.get("already_split")),
        "confirmation_required": not confirmed and not authorized and not result.get("already_split"),
        "manifest_path": result.get("manifest_path"),
        "analysis_unit_id": result.get("analysis_unit_id"),
        "parts": parts,
        "unclaimed_samples": result.get("unclaimed_samples") or [],
        "blockers": result.get("blockers") or [],
        **({"campaign_authorization": authorized} if authorized else {}),
    }


def _unit_manifest_path(download_job_id: str, manifest_path: str, host: str, port: int) -> Path:
    """The unit manifest a raw-data tool acts on: manifest_path when it exists, else its download job's."""
    resolved = Path(str(manifest_path or "")).expanduser()
    if not resolved.is_file():
        if not download_job_id:
            raise ValueError("Provide either download_job_id or a manifest_path that exists.")
        _, manifest = _repository_download_job(download_job_id, host, port)
        resolved = Path(manifest["manifest_path"])
    return resolved


@mcp.tool()
@_structured_validation_errors
def msdial_cleanup_repository_raw(
    download_job_id: str = "",
    manifest_path: str = "",
    confirmed: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    campaign_authorization_path: str = "",
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

    campaign_authorization_path names a recorded campaign approval. It stands in for confirmed=true only
    when it covers boundary 5 for this unit, the approval states delete_after_validated_output, the
    unit's own manifest recorded that same policy at download, and the preview is ready - every guard of
    the preview still applies. The crossing is recorded in the manifest before anything is deleted.

    A split parent's manifest is released here too: its raw tree, which every part reads, goes only once
    every part has ended (validated, failed after its retries, skipped or excluded), and the parent stays
    split_by_acquisition. A part's own preview carries its parent's plan (split_parent_plan).

    Every deletion is recorded in the manifest (raw_deletion, or raw_release for a split parent): the files
    and bytes removed, what was kept, and who authorised it. One a held file stopped is recorded as partial
    and resumes when called again. A multiply-linked file loses only this name; its attributes are untouched.

    A finished run whose MS-DIAL containers could not be moved out of the raw tree holds the deletion
    (finalisation_holds in the manifest). Both calls retry that move first; while it still fails the preview
    lists it as a blocker and the deletion is refused.

    A unit leased through the accession download store releases its store claims with its tree. The
    preview's download_store says how many of the tree's bytes are links to the store's files
    (tree_bytes_kept_by_store), which a person's confirmation does not free: store objects are deleted only
    under a campaign approval covering every unit that released them. Asked again once made, the cleanup
    deletes nothing (already_cleaned) and makes a store release the first one left unmade.
    """
    from .repository_reanalysis import cleanup_download_lease

    resolved = _unit_manifest_path(download_job_id, manifest_path, host, port)
    authorized = bool(str(campaign_authorization_path or "").strip())
    result = cleanup_download_lease(
        resolved,
        confirmed=confirmed,
        campaign_authorization_path=campaign_authorization_path or None,
        entry_point="msdial_cleanup_repository_raw",
    )
    if authorized and not result.get("deleted"):
        result.setdefault("message", "Nothing was deleted: the approval covers this unit, but the deletion is not ready.")
    elif not confirmed and not authorized and not result.get("deleted"):
        result["message"] = (
            "Nothing was deleted. Show the deletion target, the size and the retained artifacts to the "
            "user, and call again with confirmed=true only after an explicit answer."
        )
    return result


@mcp.tool()
@_structured_validation_errors
def msdial_discard_repository_raw(
    download_job_id: str = "",
    manifest_path: str = "",
    confirmed: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    campaign_authorization_path: str = "",
    release_disposition_hold: bool = False,
) -> dict[str, Any]:
    """Preview, and only on explicit confirmation perform, deletion of the raw data of a unit with no validated output.

    For a unit skipped or excluded after its download, one whose download failed, or one whose run failed.
    With confirmed=false and no approval nothing is deleted and the result carries the plan: the target, its
    files and bytes, any mzTab-M in the output, and what refuses the discard. A validated unit is refused here;
    its raw data are deleted by msdial_cleanup_repository_raw.

    campaign_authorization_path names a recorded campaign approval. It stands in for confirmed=true only when
    it covers boundary 5 for this unit (or the unit it was split from) and the approval and the unit both state
    delete_after_validated_output. Under it, and only under it, a failed unit whose output holds an
    unvalidated or invalid mzTab-M is discarded too: the mzTab-M, its validation and the failure record are
    kept under output as failure artifacts and never deleted. A refusal under an approval returns blockers, with
    nothing recorded or deleted.

    A split parent is released as msdial_cleanup_repository_raw releases one. For a split part, an approval
    records that the part has ended, deleting nothing: its raw data are its parent's, released with them.

    release_disposition_hold (default false) is the operator's explicit decision to skip a unit, or split part,
    whose campaign disposition holds it (hold true, e.g. aif_multi_ce_awaiting_console). Without it such a
    unit is never discarded, under an approval or with confirmed=true, and a held part keeps its parent's raw
    data. With it and an approval covering boundary 5 (or confirmed=true) the discard proceeds and records
    disposition_hold_released_by "operator_skip"; on a split parent it lifts its held parts' holds the same way.
    Pass it only on that explicit decision, never because a hold blocks a discard.
    """
    from .repository_reanalysis import discard_download_lease

    resolved = _unit_manifest_path(download_job_id, manifest_path, host, port)
    authorized = bool(str(campaign_authorization_path or "").strip())
    result = discard_download_lease(
        resolved,
        confirmed=confirmed,
        campaign_authorization_path=campaign_authorization_path or None,
        entry_point="msdial_discard_repository_raw",
        release_disposition_hold=bool(release_disposition_hold),
    )
    if not confirmed and not authorized and not result.get("deleted"):
        result["message"] = (
            "Nothing was deleted. Show the deletion target, the size and what refuses it to the user, and call "
            "again with confirmed=true only after an explicit answer."
        )
    return result


@mcp.tool()
@_structured_validation_errors
def msdial_download_store_status(
    workspace_root: str,
    repository: str = "",
    accession: str = "",
    analysis_unit_id: str = "",
) -> dict[str, Any]:
    """Show the accession download stores under a workspace root. Read-only: nothing is fetched or deleted.

    A store, <workspace_root>\\<repository>\\<accession>\\_dl, holds each object one or more units of the
    accession fetch, once, kept by one claim per unit (pending: claimed before its lease; materialized: its
    raw tree links the object; released). For each store: its objects with the units whose live claims keep
    them, its claims by state and by unit (with each unit's manifest status), partial transfers, lock
    holders, objects no live claim holds (which a collection under a campaign approval deletes once it covers
    every unit that released them) and live claims of units whose raw data are already released.
    ``repository`` and ``accession`` narrow it to one store, ``analysis_unit_id`` to one unit's claims.
    ``store_mode`` is the saved setting: "campaign" (the default) uses the store for campaign units only,
    "always" for every lease.
    """
    from .repository_reanalysis import download_store_status

    return download_store_status(
        _validated_workspace_root(workspace_root), repository=repository, accession=accession, unit_id=analysis_unit_id
    )


@mcp.tool()
@_structured_validation_errors
def msdial_prepare_repository_reanalysis(
    download_job_id: str = "",
    hierarchy: list[str] | None = None,
    confirmed: bool = False,
    allow_partial_mapping: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    manifest_path: str = "",
    campaign_authorization_path: str = "",
    new_run: bool = False,
) -> dict[str, Any]:
    """Project repository metadata into Class and prepare an analysis CSV after review.

    manifest_path reaches a unit whose download job the backend no longer holds; the recognised files
    are then rebuilt from the manifest's input_candidates, as the download job built them. A campaign
    approval covering boundary 3 for the unit stands in for confirmed=true and is recorded in the manifest.

    A manifest that carries input_lineage (every lease since 0.5.9) is built from it instead, one row per
    analysis input, a vendor folder being one (repository_analysis_rows). Where its inputs, lineage,
    declared inputs and sample rows disagree, or an acquisition type cannot be written, nothing is
    written but the failure, which is recorded in the manifest and returned with ok false, so an
    unattended caller goes on to its next unit.

    A UNIT PAST ITS RUN. A prepare never writes over the files of a run that finished (mztab_validated,
    completed, cleanup_pending_confirmation, or any unit whose production run was finalised, validation_failed
    included): its output_directory holds the analysis CSV that run read, and retained_artifact_inventory
    their checksums. Such a unit is refused (reason run_finished) and nothing is written, unless new_run=true
    asks for a new production run. Then, in the preview in memory only and on disk when the call writes, the
    finished run's records are copied unchanged into superseded_runs, output_directory becomes a new folder
    (<workspace>\\output-run-<n>), and the analysis CSV is written there; the old output directory and its
    files are left as they are. The reply describes it in preview.new_run. A new run is all or nothing: it is
    decided, its rows built, its aliases made and its metadata and CSV written into a hidden staging folder
    first, and only then, in one write under the manifest lock, the staging folder becomes output-run-<n> and
    the manifest records the new run with its CSV (and any campaign crossing). Where anything before that
    fails (rows that disagree, an alias that cannot be made, files that do not map, an error), or the manifest
    was written by another writer meanwhile (reason new_run_conflict), the manifest and every file of the
    finished run are left byte for byte as they were, the staging folder and the aliases the call made are
    removed, no failure record is written into the manifest, and the error is returned. A unit whose raw data were released
    (raw_cleaned, discarded) is refused with or without new_run (raw_released), and so is a new run while a run
    attempt of the unit may still be running (run_in_progress), or one a legacy disposition decided again would
    not run (would_not_run). Each refusal returns ok false and writes nothing.

    A unit whose applied campaign disposition was decided before 0.5.29 (it records no
    declared_acquisition_source) is decided again here under the header-first rule (B2), from its recorded
    preflight, before its rows are built: in the preview in memory only, and on disk when the call writes
    (confirmed=true, or a campaign approval), before the CSV and whether or not the CSV then fails. A unit past
    its run is decided again only as a new run is prepared for it (new_run=true), never in place, and that
    decision is written only with the new run and its CSV, never on its own. The reply
    names what changed in legacy_disposition_redecision. The execution gate refuses such a disposition's rows
    where the new rule would not run them, so this is the step that clears that refusal.
    """
    job, manifest = _repository_unit(download_job_id, manifest_path, host, port)
    crossing = _campaign_authorization(
        campaign_authorization_path, manifest, 3, "msdial_prepare_repository_reanalysis"
    )
    from .repository_reanalysis import (
        NewProductionRunConflict,
        begin_new_production_run,
        finished_production_run,
        redecide_legacy_disposition,
        start_new_production_run,
    )

    writes = confirmed or crossing is not None
    finished = finished_production_run(manifest)
    if finished is not None and (finished["raw_released"] or not new_run):
        # Refused before anything is decided, built or written: the finished run's records stand as they are.
        return {
            "ok": False,
            "prepared": False,
            "confirmation_required": False,
            "reason": "raw_released" if finished["raw_released"] else "run_finished",
            "finished_run": finished,
            "detail": (
                f"This unit's raw data were released (status {finished['status']!r}); it never runs again, and "
                "nothing was written. Download it into a new lease to analyse it again."
                if finished["raw_released"]
                else f"This unit's production run has finished (status {finished['status']!r}), and its output "
                f"directory {finished['output_directory']} holds that run's files, the analysis CSV it read among "
                "them. A prepare never writes over them, and nothing was written."
            ),
            **(
                {}
                if finished["raw_released"]
                else {
                    "next_step": (
                        "To run the unit again, call again with new_run=true: the finished run's records are kept "
                        "under superseded_runs and the analysis CSV is written into a new output directory."
                    )
                }
            ),
        }
    new_run_record: dict[str, Any] | None = None
    redecision: dict[str, Any] | None = None
    pending: Any = None
    if finished is not None:
        if writes:
            # Decided in memory and committed only with its CSV (PendingNewProductionRun).
            new_run_record, manifest, pending = begin_new_production_run(manifest)
        else:
            new_run_record, manifest = start_new_production_run(manifest, write=False)
        if not new_run_record["started"] and new_run_record["reason"] != "no_finished_run":
            return {
                "ok": False,
                "prepared": False,
                "confirmation_required": False,
                "reason": new_run_record["reason"],
                "detail": str(new_run_record.get("detail") or new_run_record.get("error") or ""),
                "new_run": new_run_record,
            }
        redecision = new_run_record.pop("legacy_disposition_redecision", None)
    if new_run_record is None or not new_run_record["started"]:
        redecision, manifest = redecide_legacy_disposition(manifest, write=writes)
    if new_run and new_run_record is None:
        new_run_record = {"started": False, "reason": "no_finished_run", "written": False}
    try:
        result = _prepare_decided_repository_unit(
            job,
            manifest,
            hierarchy=hierarchy,
            download_job_id=download_job_id,
            confirmed=confirmed,
            allow_partial_mapping=allow_partial_mapping,
            crossing=crossing,
            redecision=redecision,
            new_run_record=new_run_record,
            pending=pending,
        )
    except NewProductionRunConflict as error:
        return {
            "ok": False,
            "prepared": False,
            "confirmation_required": False,
            "reason": "new_run_conflict",
            "detail": str(error),
            "new_run": new_run_record,
        }
    finally:
        if pending is not None:
            # Whatever stopped the new run before its commit, the staging folder and its aliases go.
            pending.abandon()
    if pending is not None and pending.committed:
        new_run_record["written"] = True
        if redecision is not None:
            redecision["written"] = True
    return result


def _prepare_decided_repository_unit(
    job: dict[str, Any] | None,
    manifest: dict[str, Any],
    *,
    hierarchy: list[str] | None,
    download_job_id: str,
    confirmed: bool,
    allow_partial_mapping: bool,
    crossing: dict[str, Any] | None,
    redecision: dict[str, Any] | None,
    new_run_record: dict[str, Any] | None,
    pending: Any,
) -> dict[str, Any]:
    """msdial_prepare_repository_reanalysis once the unit, and any new run of it, has been decided.

    ``pending`` is the confirmed new production run (repository_reanalysis.PendingNewProductionRun), else None.
    With it nothing is written but into its staging folder until pending.commit, which writes the manifest once.
    """
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
    if isinstance(manifest.get("input_lineage"), dict):
        return _prepare_repository_rows_from_lineage(
            job,
            manifest,
            projected,
            selected_hierarchy,
            download_job_id=download_job_id,
            confirmed=confirmed,
            allow_partial_mapping=allow_partial_mapping,
            crossing=crossing,
            redecision=redecision,
            new_run_record=new_run_record,
            pending=pending,
        )
    if job is not None:
        recognized = ((job.get("result") or {}).get("recognized") or {}).get("files", [])
    else:
        from .workflow import expand_paths_report

        recognized = expand_paths_report(list(manifest.get("input_candidates") or [])).get("files", [])
    application = apply_classes_to_analysis_files(projected, recognized)
    from .repository_reanalysis import (
        acquisition_start_order,
        record_analytical_order,
        with_order_source,
    )

    analytical_order = with_order_source(
        acquisition_start_order(
            manifest,
            [str(item.get("file_path", "")) for item in application["files"]],
            [str(item.get("class_id", "")) for item in application["files"]],
        ),
        application["files"],
        application.get("declared_order_files") or [],
        recognized,
    )
    output_root = str(manifest.get("output_directory") or "")
    raw_retention_policy = str(
        (job or {}).get("raw_retention_policy") or manifest.get("raw_retention_policy") or "keep"
    )
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
        # The record keeps every file with its order; the preview says how many, so that a large unit's
        # manifest does not land in the model context.
        "analytical_order": {
            key: value for key, value in analytical_order.items() if key not in ("orders", "files")
        } | {"files_recorded": len(analytical_order.get("files") or [])},
    }
    if crossing:
        preview["campaign_authorization"] = crossing
    if redecision is not None:
        preview["legacy_disposition_redecision"] = redecision
    if new_run_record is not None:
        preview["new_run"] = new_run_record
    if not confirmed and crossing is None:
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
    if pending is not None:
        from .repository_reanalysis import analytical_order_change, campaign_authorization_change

        saved = save_metadata_review(
            projected,
            pending.staging_directory(),
            application["files"],
            analytical_orders=analytical_order.get("orders"),
        )
        if not saved.get("analysis_files_csv"):
            raise RuntimeError("No analysis_files.csv was generated from the repository download.")
        saved = {key: str(pending.staged(value)) for key, value in saved.items()}
        pending.commit(
            ([campaign_authorization_change(crossing)] if crossing else [])
            + [analytical_order_change(analytical_order)]
        )
        answer_seed["repository_metadata_path"] = saved["metadata_json"]
        return {
            "prepared": True,
            "input_path": saved["analysis_files_csv"],
            "output_root": output_root,
            "files": saved,
            "preview": preview,
            "next_step": (
                "Pass input_path and preview.answer_seed to msdial_guided_analysis_plan, then "
                "collect any remaining scientific decisions before execution."
            ),
        }
    if crossing:
        from .repository_reanalysis import record_campaign_authorization

        record_campaign_authorization(manifest["manifest_path"], crossing)
    saved = save_metadata_review(
        projected,
        output_root,
        application["files"],
        analytical_orders=analytical_order.get("orders"),
    )
    input_path = saved.get("analysis_files_csv")
    if not input_path:
        raise RuntimeError("No analysis_files.csv was generated from the repository download.")
    # Only once the CSV it describes exists, so a failed prepare cannot replace a valid record.
    record_analytical_order(manifest["manifest_path"], analytical_order)
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


def _prepare_repository_rows_from_lineage(
    job: dict[str, Any] | None,
    manifest: dict[str, Any],
    projected: dict[str, Any],
    selected_hierarchy: list[str],
    *,
    download_job_id: str,
    confirmed: bool,
    allow_partial_mapping: bool,
    crossing: dict[str, Any] | None,
    redecision: dict[str, Any] | None = None,
    new_run_record: dict[str, Any] | None = None,
    pending: Any = None,
) -> dict[str, Any]:
    """msdial_prepare_repository_reanalysis for a manifest with input_lineage: one row per analysis input.

    WHY NOT THE RECOGNISED FILES. They were the download job's, and the registry keeps a hundred jobs; they
    were matched to sample rows by file name, which a folder named raw/x.raw/ in its sample row never
    matched; and the unit's one acquisition label was written on every row. The rows come from the record
    the lease wrote for this, and each row's acquisition type from that file's own header.
    """
    from .repository_analysis_rows import (
        blocking_failures,
        build_repository_analysis_rows,
        create_console_aliases,
        order_rows,
        record_analysis_csv,
        record_analysis_csv_failure,
        write_analysis_csv,
    )
    from .repository_metadata import save_metadata_review
    from .repository_qa import repository_internal_standard_evidence
    from .repository_reanalysis import record_analytical_order, record_campaign_authorization

    built = build_repository_analysis_rows(manifest, projected)
    analytical_order = order_rows(manifest, built)
    output_root = str(manifest.get("output_directory") or "")
    raw_retention_policy = str(
        (job or {}).get("raw_retention_policy") or manifest.get("raw_retention_policy") or "keep"
    )
    answer_seed = _repository_answer_seed(projected, manifest, output_root, raw_retention_policy)
    # THE ROWS' TYPES, NOT THE UNIT'S. The guided plan writes an answered acquisition_type over every file
    # (agent_workflow._workflow), and the seed reads the unit's one label, 'DIA' as SWATH: a Waters MSe
    # unit whose headers say AIF would have run as SWATH. The seed carries the type every row shares, and
    # none where the rows differ, so each file runs as the CSV says.
    row_types = {row["acquisition_type"] for row in built["rows"] if row["acquisition_type"]}
    if len(row_types) == 1:
        answer_seed["acquisition_type"] = next(iter(row_types))
    else:
        answer_seed.pop("acquisition_type", None)
    execution_allowed = manifest.get("execution_allowed") is True
    execution_blockers: list[str] = []
    if not execution_allowed:
        execution_blockers.append(
            "execution_allowed is not true for this analysis unit "
            f"(status {manifest.get('status', 'unknown')!r}). MS-DIAL will refuse to start until a "
            "raw-header preflight settles the unit's technical conditions."
        )
    failures = built["failures"]
    blocking = blocking_failures(built, allow_partial_mapping)
    unmatched = next((item["inputs"] for item in failures if item["code"] == "input_without_sample"), [])
    # A row two inputs name, an input two rows name, or an input of a sample whose rows do not say which.
    ambiguous = [
        name
        for item in failures
        if item["code"] in ("sample_row_with_two_inputs", "input_with_two_sample_rows", "sample_row_not_identified")
        for name in item["inputs"]
    ]
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
        "built_from": built["built_from"],
        "counts": built["counts"],
        "matched_count": sum(1 for row in built["rows"] if row["sample_id"]),
        "recognized_count": len(built["rows"]),
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "acquisition_types": {
            value: sum(1 for row in built["rows"] if row["acquisition_type"] == value)
            for value in built["acquisition_types"]
        },
        "console_aliases": len(built["aliases"]),
        "class_id_aliases": built["class_id_aliases"],
        "excluded_inputs": built["excluded_inputs"],
        "failures": failures,
        "blocking_failures": [item["code"] for item in blocking],
        # What stops nothing and is recorded with the CSV: sample rows without an input (ST001264 runs 3 of its
        # 31), and inputs whose raw file the lease paired by an inferred rule.
        "warnings": list(built.get("warnings") or []),
        "sample_row_coverage": dict(built.get("sample_row_coverage") or {}),
        "inferred_name_pairings": list(built.get("inferred_name_pairings") or []),
        # Archive members no sample row pairs with, run as unattributed inputs (2026-10-07).
        "unattributed_inputs": list(built.get("unattributed_inputs") or []),
        **({"aif_run_as_swath": dict(built["aif_run_as_swath"])} if built.get("aif_run_as_swath") else {}),
        "answer_seed": answer_seed,
        "qa_internal_standard_evidence": repository_internal_standard_evidence(projected),
        "analytical_order": {
            key: value for key, value in analytical_order.items() if key not in ("orders", "files")
        } | {"files_recorded": len(analytical_order.get("files") or [])},
    }
    if crossing:
        preview["campaign_authorization"] = crossing
    if redecision is not None:
        # A disposition decided before 0.5.29, decided again: the rows above are built from the new decision.
        preview["legacy_disposition_redecision"] = redecision
    if new_run_record is not None:
        # A new production run of a unit past its run: output_root above is the new run's folder.
        preview["new_run"] = new_run_record
    if not confirmed and crossing is None:
        return {
            "prepared": False,
            "confirmation_required": True,
            "preview": preview,
            "message": (
                "Review the Class assignments and the one row per analysis input"
                + (", and the failures that stop the CSV" if blocking else "")
                + ". Call again with confirmed=true to write reviewed metadata and analysis_files.csv."
            ),
        }
    if pending is not None:
        return _commit_new_run_rows(
            pending, built, projected, analytical_order, blocking, crossing, preview, answer_seed, output_root
        )
    if crossing:
        record_campaign_authorization(manifest["manifest_path"], crossing)
    if not blocking:
        blocking = create_console_aliases(built)
    if blocking:
        # A FAILURE RECORD FOR THIS UNIT, NOT AN EXCEPTION. The unit's own records disagree, or a row
        # could not be written as the Console reads it; the manifest says which, and nothing is written
        # that a run could start from.
        record = record_analysis_csv_failure(manifest["manifest_path"], built, blocking)
        return {
            "ok": False,
            "prepared": False,
            "reason": "analysis_csv_failed",
            "codes": sorted({item["code"] for item in blocking}),
            "detail": " ".join(item["message"] for item in blocking),
            "analysis_csv": record,
            "preview": preview,
        }
    saved = save_metadata_review(_with_raw_file_paired_by(projected, built), output_root)
    input_path = write_analysis_csv(built, Path(output_root) / "analysis_files.csv")
    saved["analysis_files_csv"] = str(input_path)
    record_analysis_csv(manifest["manifest_path"], built, input_path)
    # Only once the CSV it describes exists, so a failed prepare cannot replace a valid record.
    record_analytical_order(manifest["manifest_path"], analytical_order)
    answer_seed["repository_metadata_path"] = saved["metadata_json"]
    return {
        "prepared": True,
        "input_path": str(input_path),
        "output_root": output_root,
        "files": saved,
        "preview": preview,
        "next_step": (
            "Pass input_path and preview.answer_seed to msdial_guided_analysis_plan, then "
            "collect any remaining scientific decisions before execution."
        ),
    }


def _with_raw_file_paired_by(projected: dict[str, Any], built: dict[str, Any]) -> dict[str, Any]:
    """The projected sample rows, each saying how its raw file was paired with its input.

    exact, or the inferred rule (prefixed_member_name, leading_identifier_token); a row without an input says
    nothing. Both the prepare of a unit's first run and of a new production run write it into the reviewed
    sample TSV, so an inferred pairing is on record in every run's table.

    An archive member no sample row pairs with, included as an unattributed input (user decision, 2026-10-07),
    gets a row of its own after the unit's sample rows: its stem as the sample id, its member name as the raw
    file, raw_file_paired_by unattributed_member, and the Class its CSV row takes.
    """
    paired_by = {
        row["sample_row_index"]: row["raw_file_paired_by"]
        for row in built["rows"]
        if row["sample_row_index"] is not None
    }
    unattributed = [
        {
            "sample_id": str(item.get("sample_id") or ""),
            "source_name": str(item.get("sample_id") or ""),
            "raw_file": str(item.get("member_name") or item.get("input") or ""),
            "values": {},
            "class_id": str(built.get("unattributed_class") or ""),
            "raw_file_paired_by": "unattributed_member",
        }
        for item in built.get("unattributed_inputs") or []
    ]
    return {
        **projected,
        "rows": [
            *(
                {**row, "raw_file_paired_by": paired_by.get(index, "")}
                for index, row in enumerate(projected.get("rows") or [])
            ),
            *unattributed,
        ],
    }


def _commit_new_run_rows(
    pending: Any,
    built: dict[str, Any],
    projected: dict[str, Any],
    analytical_order: dict[str, Any],
    blocking: list[dict[str, Any]],
    crossing: dict[str, Any] | None,
    preview: dict[str, Any],
    answer_seed: dict[str, Any],
    output_root: str,
) -> dict[str, Any]:
    """The confirmed lineage prepare of a new production run: staged, then committed in one manifest write.

    A failure returns analysis_csv_failed as any prepare does, but nothing of it is written into the manifest:
    the unit keeps its finished run, and the caller (msdial_prepare_repository_reanalysis) removes the staging
    folder and the aliases this call made.
    """
    from .repository_analysis_rows import (
        analysis_csv_change,
        analysis_csv_failure_record,
        create_console_aliases,
        write_analysis_csv,
    )
    from .repository_metadata import save_metadata_review
    from .repository_reanalysis import analytical_order_change, campaign_authorization_change

    if not blocking:
        blocking = create_console_aliases(built, made=pending.aliases_made)
    if blocking:
        record = analysis_csv_failure_record(built, blocking)
        return {
            "ok": False,
            "prepared": False,
            "reason": "analysis_csv_failed",
            "codes": sorted({item["code"] for item in blocking}),
            "detail": " ".join(item["message"] for item in blocking)
            + " No new run was prepared: the unit's finished run and its manifest are as they were.",
            "analysis_csv": {**record, "written_to_manifest": False},
            "preview": preview,
        }
    staging = pending.staging_directory()
    saved = save_metadata_review(_with_raw_file_paired_by(projected, built), staging)
    staged_csv = write_analysis_csv(built, staging / "analysis_files.csv")
    saved["analysis_files_csv"] = str(staged_csv)
    saved = {key: str(pending.staged(value)) for key, value in saved.items()}
    input_path = saved["analysis_files_csv"]
    # Every alias the CSV names, made here or reused: the commit refuses if a concurrent prepare that made one
    # has since abandoned it (review r7-62).
    required = [
        path
        for row in built.get("rows") or []
        if row.get("console_alias")
        for path in (
            Path(row["console_alias"]["path"]),
            *(
                Path(row["console_alias"]["path"]).with_name(str(name))
                for name in row["console_alias"].get("sidecars") or []
            ),
        )
    ]
    pending.commit(
        ([campaign_authorization_change(crossing)] if crossing else [])
        + [analysis_csv_change(built, input_path), analytical_order_change(analytical_order)],
        required_paths=required,
    )
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
    download_job_id: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    manifest_path: str = "",
) -> dict[str, Any]:
    """Return repository declarations that may identify LC-MS internal-standard QA targets."""
    _, manifest = _repository_unit(download_job_id, manifest_path, host, port)
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
    answers: dict[str, Any] | None = None,
    description: str = "",
    workflow_overrides: dict[str, Any] | None = None,
    run_directory: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Save reusable scientific choices as a named local workset.

    Give run_directory to save the answers a finished run actually used, rather than
    retyping them; the run records them beside its bundle. Dataset-scoped answers --
    the Class confirmation, the dilution factor, the paths -- are left out on purpose
    and the result names them under not_reusable.
    """
    return _request_json(
        "POST",
        "/api/agent/worksets/save",
        host=host,
        port=port,
        body={
            "name": name,
            "description": description,
            "answers": answers or {},
            "workflow_overrides": workflow_overrides or {},
            "run_directory": run_directory,
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
    campaign_authorization_path: str = "",
    timeout_seconds: float = 0,
    idle_timeout_seconds: float = 0,
) -> dict[str, Any]:
    """Start MS-DIAL only when the complete guided plan has been explicitly confirmed.

    A campaign approval covering boundary 4 for the repository unit the plan names stands in for
    confirmed=true; the backend checks it and records it in the unit manifest before the run starts.

    timeout_seconds stops the Console when it runs longer than that, and idle_timeout_seconds when
    neither its output nor its log grows for that long; the job then fails with exit code -3. Both are 0,
    no limit, unless given; a limit past ten years is refused. A repository unit runs one Console at a
    time: a second start for the same unit is refused with reason unit_busy and the live job's id.
    """
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
            "campaign_authorization_path": campaign_authorization_path,
            "timeout_seconds": timeout_seconds,
            "idle_timeout_seconds": idle_timeout_seconds,
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
    campaign_authorization_path: str = "",
    timeout_seconds: float = 0,
    idle_timeout_seconds: float = 0,
) -> dict[str, Any]:
    """Run the zero-threshold single-file diagnostic after explicit confirmation.

    The diagnostic starts the Console, so a campaign approval must cover boundary 4 to stand in for
    confirmed=true. The diagnostic records itself in its own directory, which is what lets
    msdial_estimate_peak_height find it by manifest_path later. timeout_seconds and
    idle_timeout_seconds limit the Console as they do for msdial_start_guided_analysis; 0 is no limit.
    An LC-MS diagnostic loads no annotation library (no MSP, LBM or text library): only its peaks and
    their heights are read, annotation changes neither, and every peak-spotting setting is the production
    method's. Its .mdpeak's Adduct, Isotope and MS1 isotopes columns are not the production run's. Its
    provenance records annotation "skipped_for_peak_count".
    """
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
            "campaign_authorization_path": campaign_authorization_path,
            "timeout_seconds": timeout_seconds,
            "idle_timeout_seconds": idle_timeout_seconds,
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
    manifest_path: str = "",
) -> dict[str, Any]:
    """Estimate a stepped Minimum peak height from a completed zero-threshold diagnostic.

    The coarse step is ALWAYS the instrument family's (100 QTOF-type, 1,000 FT), read again from the
    diagnostic's representative file, and of its multiples the threshold is the HIGHEST whose estimated
    count is still at least target_peak_count_min: the lower end of the range. Only when no multiple of
    it lands in range does the search make the same choice in the fine step - 10 for QTOF-type, 100 for
    FT, an absolute floor - and the estimate then says step_fallback true, fallback_reason
    "no_coarse_step_in_range", threshold_step the step used and coarse_threshold_step the family step.
    Leave threshold_step at 0: a step passed, including a fallback's step echoed back, is recorded as
    requested_threshold_step with a warning and is never searched in the family step's place. When
    within_target_range is false, show the user the estimate's warnings.

    With manifest_path, a diagnostic the backend no longer holds is found in the unit's diagnostics
    directory by its job_id and its result file is read again, instead of running the Console again.
    """
    return _request_json(
        "POST",
        "/api/agent/tuning/estimate",
        host=host,
        port=port,
        body={
            "job_id": job_id,
            "manifest_path": manifest_path,
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
def msdial_cancel_job(
    job_id: str,
    reason: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Stop a running or queued MS-DIAL run, peak-count diagnostic, or repository download.

    A run or diagnostic has its Console's whole process tree stopped and fails with exit code -4; a
    download stops at its next progress report, which a slow transfer makes at least once a second and a
    stalled one within the idle read timeout (120 s), and its unit manifest records download_failed with
    reason cancelled, keeping the partial file for a resume. Returns at once with cancel_requested; poll
    the job to see it end. Stopping polling, as msdial_interactive_wait_for_completion does on its
    timeout, never stopped anything; this does.
    """
    return _request_json(
        "POST",
        f"/api/jobs/{urllib.parse.quote(str(job_id).strip(), safe='')}/cancel",
        host=host,
        port=port,
        body={"reason": reason},
        timeout=30,
    )


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
    job_id: str = "",
    internal_standards: list[dict[str, Any]] | None = None,
    file_path: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    manifest_path: str = "",
) -> dict[str, Any]:
    """Generate LC-MS QA only from the matrix created or updated by one job.

    manifest_path reaches that job's matrix through the run the unit manifest recorded when it was
    finalised, after the backend has forgotten the job.
    """
    return _request_json(
        "POST",
        "/api/qa/report",
        host=host,
        port=port,
        body={
            "job_id": job_id,
            "manifest_path": manifest_path,
            "file_path": file_path,
            "internal_standards": internal_standards or [],
        },
        timeout=120,
    )


@mcp.tool()
@_structured_validation_errors
def msdial_generate_publication_report(
    job_id: str = "",
    run_qa: bool = True,
    internal_standards: list[dict[str, Any]] | None = None,
    qa_file_path: str = "",
    qa_criteria: dict[str, Any] | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    manifest_path: str = "",
) -> dict[str, Any]:
    """Generate Materials and Methods, QA Results, and supplementary Excel/TSV artifacts.

    manifest_path reaches the run through the unit manifest's finalised-run record. For a repository
    unit the retained-artifact inventory is refreshed afterwards, so it lists the publication artifacts.
    A unit whose run left an mzTab-M it could not redact, or MS-DIAL's library copy it could not delete, is
    held from sharing: the step is retried first, and while it still fails nothing is written.
    """
    return _request_json(
        "POST",
        "/api/publication/report",
        host=host,
        port=port,
        body={
            "job_id": job_id,
            "manifest_path": manifest_path,
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

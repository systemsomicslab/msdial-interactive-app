from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .agent_bridge import create_datamining_handoff, summarize_job, summarize_jobs
from .agent_workflow import build_guided_plan, estimate_peak_height
from .knowledge import KnowledgeBase, next_parameter_question
from .library_catalog import catalog_status, download_library, library_directory
from .literature import evaluate_literature_evidence
from .mztab_validation import list_mztab_outputs, validate_mztab_files, validate_mztab_outputs
from .mztab_preview import preview_mztab_outputs
from .materials_methods import generate_publication_report
from .quality_assurance import build_lcms_qa_report_from_file, find_qa_files
from .workflow import (
    console_version,
    console_capabilities,
    discover_console_paths,
    expand_paths,
    expand_paths_report,
    find_mdpeak,
    find_mdscan,
    load_parameter_template,
    detect_raw_format,
    parse_method,
    parse_mdpeak,
    parse_mdscan,
    parse_rt_correction_result,
    prepare_run,
    prepare_rt_correction_run,
    prepare_tuning_run,
    read_analysis_csv,
    read_adducts,
    read_lipid_queries,
    read_rt_correction_anchors,
    run_console,
    save_rt_correction_anchors,
    save_rt_correction_selections,
    is_supported,
    validate_workflow,
)
from .user_settings import load_user_settings, save_path_settings, settings_path, user_data_directory
from .worksets import list_worksets, save_workset


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
STATIC = ROOT / "static"
RESOURCES = ROOT / "resources"
KNOWLEDGE = ROOT / "knowledge"
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
JOBS_FILE = user_data_directory() / "agent-jobs.json"
DOWNLOADS: dict[str, Path] = {}
KB = KnowledgeBase(KNOWLEDGE)


def _artifact_roots(preparation: dict[str, Any]) -> list[Path]:
    values = [preparation.get("run_directory"), preparation.get("export_folder_path")]
    roots: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not str(value or "").strip():
            continue
        root = Path(str(value)).expanduser().resolve()
        key = str(root).casefold()
        if key not in seen:
            seen.add(key)
            roots.append(root)
    return roots


def _snapshot_run_artifacts(preparation: dict[str, Any]) -> dict[str, list[int]]:
    snapshot: dict[str, list[int]] = {}
    for root in _artifact_roots(preparation):
        if not root.is_dir():
            continue
        for path in _artifact_files(root):
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path.resolve())] = [stat.st_mtime_ns, stat.st_size]
    return snapshot


def _classify_artifact(path: Path) -> str:
    name = path.name.casefold()
    if name.endswith(".qa.tsv"):
        return "qa"
    if path.suffix.casefold() in {".mztab", ".mztabm"} or "mztab" in name:
        return "mztab"
    if path.suffix.casefold() in {".mdalign", ".mdpeak", ".mdscan", ".mdmsp", ".mdproject", ".arf2", ".dcl"}:
        return "msdial"
    return "other"


def _artifact_files(root: Path) -> list[Path]:
    files = [path for path in root.glob("*") if path.is_file()]
    for child in root.iterdir():
        if not child.is_dir() or child.suffix.casefold() in {".d", ".raw"}:
            continue
        files.extend(path for path in child.glob("*") if path.is_file())
    return files


def _changed_run_artifacts(
    preparation: dict[str, Any], baseline: dict[str, list[int]]
) -> dict[str, Any]:
    grouped: dict[str, list[str]] = {"mztab": [], "qa": [], "msdial": [], "other": []}
    records: list[dict[str, Any]] = []
    for root in _artifact_roots(preparation):
        if not root.is_dir():
            continue
        for path in _artifact_files(root):
            try:
                stat = path.stat()
            except OSError:
                continue
            resolved = str(path.resolve())
            signature = [stat.st_mtime_ns, stat.st_size]
            if baseline.get(resolved) == signature:
                continue
            kind = _classify_artifact(path)
            grouped[kind].append(resolved)
            records.append(
                {
                    "path": resolved,
                    "kind": kind,
                    "size_bytes": stat.st_size,
                    "modified_time_ns": stat.st_mtime_ns,
                    "change": "created" if resolved not in baseline else "updated",
                }
            )
    for values in grouped.values():
        values.sort()
    records.sort(key=lambda item: item["path"])
    return {**grouped, "records": records}


def _job_artifact_paths(job: dict[str, Any], kind: str) -> list[str]:
    return [str(path) for path in (job.get("artifacts") or {}).get(kind, [])]


def _load_persisted_jobs() -> dict[str, dict[str, Any]]:
    if not JOBS_FILE.is_file():
        return {}
    try:
        raw = json.loads(JOBS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    jobs = raw.get("jobs", {}) if isinstance(raw, dict) else {}
    if not isinstance(jobs, dict):
        return {}
    now = dt.datetime.now().astimezone().isoformat()
    for job in jobs.values():
        if job.get("status") in {"queued", "running"}:
            job["status"] = "interrupted"
            job["updated_at"] = now
            job["error"] = (
                "The local backend stopped before this job completed. "
                "A library download can be started again and will resume from its .part file."
            )
            job.setdefault("logs", []).append(job["error"])
    return jobs


def _persist_jobs_locked() -> None:
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        JOBS.items(),
        key=lambda item: str(item[1].get("updated_at") or item[1].get("created_at") or ""),
        reverse=True,
    )[:100]
    temporary = JOBS_FILE.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"jobs": dict(ordered)}, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(JOBS_FILE)


JOBS.update(_load_persisted_jobs())
if JOBS:
    with JOBS_LOCK:
        _persist_jobs_locked()


def _local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        host_name = socket.gethostname()
        for info in socket.getaddrinfo(host_name, None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        if not address.startswith("127."):
            addresses.add(address)
        probe.close()
    except OSError:
        pass
    return sorted(addresses)


def _default_console_path() -> str:
    return str(discover_console_paths().get("selected_path", ""))


def _application_config() -> dict[str, Any]:
    saved = load_user_settings()
    default_queries = str(saved.get("queries_path", "")).strip() or str(
        RESOURCES / "LbmQueries.txt"
    )
    default_template = str(saved.get("template_path", "")).strip() or str(
        RESOURCES / "msdial_console_param4lipidomics.txt"
    )
    default_console = str(saved.get("console_path", "")).strip() or _default_console_path()
    lipid_queries = read_lipid_queries(default_queries)
    try:
        loaded = load_parameter_template(default_template, default_queries)
        if loaded["workflow"].get("target_omics", "").casefold() == "lipidomics":
            for item in lipid_queries:
                item["selected"] = True
    except (OSError, ValueError):
        for item in lipid_queries:
            item["selected"] = True
    console_discovery = discover_console_paths()
    return {
        "default_console": default_console,
        "default_queries": default_queries,
        "default_template": default_template,
        "default_gcms_template": str(RESOURCES / "gcms_console_param_kovats.txt"),
        "settings_file": str(settings_path()),
        "settings_loaded": bool(saved),
        "library_directory": str(library_directory()),
        "library_catalog": catalog_status(),
        "console_discovery": console_discovery,
        "lipid_queries": lipid_queries,
    }


def _diagnose_console_failure(logs: list[str], fallback: str) -> str:
    text = "\n".join(logs).lower()
    if "basedataaccess" in text:
        return (
            "Agilent reader could not load BaseDataAccess.dll. Check the MS-DIAL Console "
            "package and its lib/Agilent deployment before checking the VC++ runtime."
        )
    if "rdam_dll" in text or "loading spectral information" in text and "fileloadexception" in text:
        return (
            "The ABF/Reifycs reader dependency could not be loaded. Use the official "
            "MS-DIAL Console package with its lib/Reifycs folder intact, or select a "
            "Console path whose vendor-reader dependencies match this data type."
        )
    vc_runtime_markers = ("msvcp120", "msvcr120", "vcruntime", "0xc000007b")
    if any(marker in text for marker in vc_runtime_markers):
        return (
            "Agilent vendor reader could not load its native runtime. Install Microsoft "
            "Visual C++ 2013 Redistributable Package x64, then retry."
        )
    if "file not found:" in text:
        return (
            "MS-DIAL could not find an input path. Folder-type .raw/.d data require a "
            "Console build containing the vendor-directory CSV parser fix."
        )
    if "required 'scan' file missing" in text or "required 'scan' file is missing" in text:
        return (
            "The SCIEX reader could not find the WIFF.SCAN adjacent to the processed WIFF. "
            "Use Add original files, Add original folder, or Add path so MS-DIAL reads "
            "the WIFF from its original directory."
        )
    return fallback


def _register_download(path: str | Path) -> str:
    token = uuid.uuid4().hex
    DOWNLOADS[token] = Path(path).resolve()
    return f"/api/downloads/{token}"


def _filesystem_roots() -> list[dict[str, str]]:
    if os.name == "nt":
        roots = []
        for code in range(ord("A"), ord("Z") + 1):
            root = f"{chr(code)}:\\"
            if Path(root).exists():
                roots.append({"label": root, "path": root})
        return roots
    return [
        {"label": "Home", "path": str(Path.home())},
        {"label": "/", "path": "/"},
    ]


def _browse_filesystem(path_text: str = "") -> dict[str, Any]:
    roots = _filesystem_roots()
    if path_text:
        current = Path(path_text).expanduser()
    elif os.name == "nt" and roots:
        current = Path(roots[0]["path"])
    else:
        current = Path.home()
    if current.is_file():
        current = current.parent
    current = current.resolve()
    if not current.exists() or not current.is_dir():
        raise FileNotFoundError(f"Directory not found: {current}")

    entries = []
    try:
        children = list(current.iterdir())
    except PermissionError:
        children = []
    for child in sorted(children, key=lambda item: (not item.is_dir(), item.name.lower())):
        try:
            child_is_dir = child.is_dir()
            child_is_file = child.is_file()
        except OSError:
            continue
        suffix = child.suffix.lower()
        is_vendor_folder = child_is_dir and child.name.lower().endswith((".d", ".raw"))
        selectable = is_supported(child)
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "is_dir": child_is_dir,
                "is_file": child_is_file,
                "is_vendor_folder": is_vendor_folder,
                "is_supported": selectable,
                "suffix": suffix,
                "format": detect_raw_format(child)["format"] if selectable else "",
            }
        )
    parent = current.parent if current.parent != current else None
    return {
        "path": str(current),
        "parent": str(parent) if parent else "",
        "roots": roots,
        "entries": entries,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MSDIALInteractive/0.1"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/config":
            bind_host, bind_port = self.server.server_address[:2]
            shared_server = bind_host in ("0.0.0.0", "::") or not str(bind_host).startswith(
                "127."
            )
            lan_urls = [f"http://{address}:{bind_port}" for address in _local_ipv4_addresses()]
            app_config = _application_config()
            self._json(
                {
                    "platform": os.name,
                    "python": os.sys.version.split()[0],
                    "app_version": __version__,
                    "root": str(ROOT),
                    "server": {
                        "bind_host": bind_host,
                        "port": bind_port,
                        "shared_server": shared_server,
                        "lan_urls": lan_urls,
                    },
                    **app_config,
                    "smoothing_methods": [
                        "SimpleMovingAverage",
                        "LinearWeightedMovingAverage",
                        "SavitzkyGolayFilter",
                        "BinomialFilter",
                        "LowessFilter",
                        "LoessFilter",
                        "TimeBasedLinearWeightedMovingAverage",
                    ],
                    "knowledge_cards": {"ja": KB.count("ja"), "en": KB.count("en")},
                    "adducts": {
                        "Positive": read_adducts(
                            RESOURCES / "AdductIonResource_Positive.txt",
                            "Positive",
                        ),
                        "Negative": read_adducts(
                            RESOURCES / "AdductIonResource_Negative.txt",
                            "Negative",
                        ),
                    },
                    "llm_environment": {
                        "azure_configured": bool(
                            os.environ.get("AZURE_OPENAI_ENDPOINT")
                            and os.environ.get("AZURE_OPENAI_API_KEY")
                            and os.environ.get("AZURE_OPENAI_DEPLOYMENT")
                        ),
                    },
                }
            )
            return
        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            query = urllib.parse.parse_qs(parsed.query)
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job and query.get("detail", ["summary"])[0] == "full":
                    response = dict(job)
                else:
                    response = summarize_job(
                        job,
                        log_lines=int(query.get("log_lines", ["50"])[0]),
                    )
            if response is None:
                self._json({"error": "Job not found."}, HTTPStatus.NOT_FOUND)
            else:
                self._json(response)
            return
        if parsed.path == "/api/agent/status":
            with JOBS_LOCK:
                response = summarize_jobs(dict(JOBS))
            self._json(response)
            return
        if parsed.path == "/api/agent/worksets":
            self._json({"worksets": list_worksets()})
            return
        if parsed.path == "/api/agent/console":
            self._json(discover_console_paths())
            return
        if parsed.path == "/api/agent/handoff":
            query = urllib.parse.parse_qs(parsed.query)
            with JOBS_LOCK:
                job = JOBS.get(query.get("job_id", [""])[0])
            run_directory = query.get("run_directory", [""])[0]
            if job is None and not run_directory:
                self._json(
                    {"error": "Set job_id or run_directory."},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._json(
                {
                    "handoff": create_datamining_handoff(
                        job=job,
                        run_directory=run_directory or None,
                    )
                }
            )
            return
        if parsed.path.startswith("/api/downloads/"):
            token = parsed.path.rsplit("/", 1)[-1]
            target = DOWNLOADS.get(token)
            if target is None or not target.is_file():
                self._json({"error": "Download not found."}, HTTPStatus.NOT_FOUND)
            else:
                self._download(target)
            return
        if parsed.path == "/api/method":
            query = urllib.parse.parse_qs(parsed.query)
            self._json(parse_method(query.get("path", [""])[0]))
            return
        self._static(parsed.path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/files/expand":
                self._json(expand_paths_report(body.get("paths", [])))
            elif parsed.path == "/api/settings/paths":
                saved = save_path_settings(body)
                self._json(
                    {
                        "settings": saved,
                        "settings_file": str(settings_path()),
                    }
                )
            elif parsed.path == "/api/agent/console/check":
                self._json(discover_console_paths(body.get("search_roots", [])))
            elif parsed.path == "/api/agent/console/set":
                path = Path(str(body.get("console_path", ""))).expanduser().resolve()
                if not path.is_file() or path.name.casefold() not in {
                    "msdialcui.exe", "msdialcui.dll"
                }:
                    raise ValueError(
                        "console_path must identify an existing MSDIALCUI.exe or MSDIALCUI.dll."
                    )
                saved = save_path_settings({"console_path": str(path)})
                self._json(
                    {
                        "console_path": saved["console_path"],
                        "version": console_version(saved["console_path"]),
                        **console_capabilities(saved["console_path"]),
                        "settings_file": str(settings_path()),
                    }
                )
            elif parsed.path == "/api/templates/load":
                self._json(
                    load_parameter_template(
                        body.get("path", ""),
                        body.get("queries_path", "") or None,
                    )
                )
            elif parsed.path == "/api/libraries/download":
                catalog_id = str(body.get("catalog_id", "")).strip()
                if not catalog_id:
                    raise ValueError("Select a library to download.")
                job_id = uuid.uuid4().hex
                with JOBS_LOCK:
                    JOBS[job_id] = {
                        "id": job_id,
                        "status": "queued",
                        "kind": "library_download",
                        "catalog_id": catalog_id,
                        "progress": 0,
                        "received": 0,
                        "total": 0,
                        "logs": [],
                        "result": None,
                        "created_at": dt.datetime.now().astimezone().isoformat(),
                    }
                    _persist_jobs_locked()
                threading.Thread(
                    target=_run_library_download_job,
                    args=(job_id, catalog_id),
                    daemon=True,
                ).start()
                self._json({"job_id": job_id})
            elif parsed.path == "/api/files/import-csv":
                self._json(read_analysis_csv(body.get("path", "")))
            elif parsed.path == "/api/files/browse":
                self._json(_browse_filesystem(body.get("path", "")))
            elif parsed.path == "/api/dialog/files":
                self._json(expand_paths_report(_pick_files()))
            elif parsed.path == "/api/dialog/vendor-directory":
                if body.get("dry_run"):
                    self._json({"ok": True, "endpoint": parsed.path})
                    return
                selected = _pick_directory(
                    "Select a vendor folder (.d/.raw) or a parent folder containing vendor folders"
                )
                report = (
                    expand_paths_report([selected])
                    if selected
                    else {"files": [], "warnings": [], "rejected": []}
                )
                self._json({"path": selected, **report})
            elif parsed.path == "/api/dialog/directory":
                selected = _pick_directory()
                report = (
                    expand_paths_report([selected])
                    if selected
                    else {"files": [], "warnings": [], "rejected": []}
                )
                self._json({"path": selected, **report})
            elif parsed.path == "/api/dialog/mztab-file":
                selected = _pick_mztab_file()
                self._json({"path": selected})
            elif parsed.path == "/api/dialog/qa-file":
                selected = _pick_qa_file()
                self._json({"path": selected})
            elif parsed.path == "/api/dialog/reference-file":
                selected = _pick_reference_file(body.get("kind", "reference"))
                self._json({"path": selected})
            elif parsed.path == "/api/rt-correction/anchors/load":
                path = body.get("path", "")
                self._json({"path": str(Path(path).expanduser().resolve()), "rows": read_rt_correction_anchors(path)})
            elif parsed.path == "/api/rt-correction/anchors/save":
                saved = save_rt_correction_anchors(
                    body.get("workflow", {}), body.get("rows", [])
                )
                self._json({"anchor_file": saved})
            elif parsed.path == "/api/knowledge/search":
                self._json(
                    {
                        "cards": KB.search(
                            body.get("query", ""),
                            body.get("language", "ja"),
                            int(body.get("limit", 6)),
                        )
                    }
                )
            elif parsed.path == "/api/assistant":
                self._json(
                    KB.answer(
                        body.get("query", ""),
                        body.get("language", "ja"),
                        body.get("workflow", {}),
                        body.get("llm", {}),
                    )
                )
            elif parsed.path == "/api/literature/evidence":
                self._json(
                    evaluate_literature_evidence(
                        body.get("workflow", {}),
                        body.get("llm", {}),
                        body.get("language", "ja"),
                    )
                )
            elif parsed.path == "/api/next-question":
                self._json(
                    {
                        "question": next_parameter_question(
                            body.get("workflow", {}),
                            body.get("language", "ja"),
                        )
                    }
                )
            elif parsed.path == "/api/agent/plan":
                self._json(
                    build_guided_plan(
                        body.get("input_path", ""),
                        body.get("answers", {}),
                        body.get("workset_id", ""),
                    )
                )
            elif parsed.path == "/api/agent/worksets/save":
                self._json(
                    {
                        "workset": save_workset(
                            body.get("name", ""),
                            body.get("answers", {}),
                            description=body.get("description", ""),
                            workflow_overrides=body.get("workflow_overrides", {}),
                        )
                    }
                )
            elif parsed.path == "/api/agent/prepare":
                plan = build_guided_plan(
                    body.get("input_path", ""),
                    body.get("answers", {}),
                    body.get("workset_id", ""),
                )
                if not plan["ready_to_prepare"]:
                    self._json(
                        {"plan": plan, "error": "The guided analysis plan is not ready."},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                messages: list[str] = []
                preparation = prepare_run(plan["workflow"], messages.append)
                self._json(
                    {
                        "plan": plan,
                        "preparation": preparation,
                        "messages": messages,
                        "download_url": _register_download(preparation["bundle"]),
                    }
                )
            elif parsed.path == "/api/agent/run":
                plan = build_guided_plan(
                    body.get("input_path", ""),
                    body.get("answers", {}),
                    body.get("workset_id", ""),
                )
                if not plan["ready_to_prepare"]:
                    self._json(
                        {"plan": plan, "error": "The guided analysis plan is not ready."},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                if body.get("confirmed") is not True:
                    self._json(
                        {
                            "started": False,
                            "confirmation_required": True,
                            "plan": plan,
                            "message": "Review the plan, then call again with confirmed=true.",
                        }
                    )
                    return
                preparation = prepare_run(plan["workflow"])
                job_id = uuid.uuid4().hex
                artifact_baseline = _snapshot_run_artifacts(preparation)
                with JOBS_LOCK:
                    JOBS[job_id] = {
                        "id": job_id,
                        "status": "queued",
                        "kind": "run",
                        "logs": [],
                        "preparation": preparation,
                        "exit_code": None,
                        "created_at": dt.datetime.now().astimezone().isoformat(),
                        "artifact_baseline": artifact_baseline,
                        "artifacts": {},
                    }
                    _persist_jobs_locked()
                threading.Thread(
                    target=_run_job,
                    args=(job_id, preparation),
                    daemon=True,
                ).start()
                self._json(
                    {
                        "started": True,
                        "job_id": job_id,
                        "plan": plan,
                        "preparation": preparation,
                        "download_url": _register_download(preparation["bundle"]),
                    }
                )
            elif parsed.path == "/api/agent/tuning/run":
                plan = build_guided_plan(
                    body.get("input_path", ""),
                    body.get("answers", {}),
                    body.get("workset_id", ""),
                )
                workflow = plan.get("workflow")
                if not workflow or not plan.get("requires_diagnostic"):
                    raise ValueError(
                        "Complete the guided questions and choose target_peak_count before diagnostic tuning."
                    )
                if body.get("confirmed") is not True:
                    self._json(
                        {
                            "started": False,
                            "confirmation_required": True,
                            "plan": plan,
                            "message": "The diagnostic runs MS-DIAL on one file. Call again with confirmed=true.",
                        }
                    )
                    return
                representative = str(body.get("representative_file", "")).strip()
                if not representative:
                    representative = workflow["files"][0]["file_path"]
                preparation = prepare_tuning_run(
                    workflow,
                    representative,
                    workflow["output_root"],
                )
                job_id = uuid.uuid4().hex
                with JOBS_LOCK:
                    JOBS[job_id] = {
                        "id": job_id,
                        "status": "queued",
                        "kind": "tuning",
                        "logs": [],
                        "preparation": preparation,
                        "exit_code": None,
                        "result": None,
                        "created_at": dt.datetime.now().astimezone().isoformat(),
                    }
                    _persist_jobs_locked()
                threading.Thread(
                    target=_run_tuning_job,
                    args=(job_id, preparation),
                    daemon=True,
                ).start()
                self._json({"started": True, "job_id": job_id, "preparation": preparation})
            elif parsed.path == "/api/agent/tuning/estimate":
                job_id = str(body.get("job_id", "")).strip()
                with JOBS_LOCK:
                    job = dict(JOBS.get(job_id) or {})
                if not job:
                    raise ValueError(f"Diagnostic job not found: {job_id}")
                if job.get("status") != "completed" or not job.get("result"):
                    self._json(
                        {
                            "ready": False,
                            "job_id": job_id,
                            "status": job.get("status", "unknown"),
                        }
                    )
                    return
                estimate = estimate_peak_height(
                    job["result"].get("heights", []),
                    int(body.get("target_peak_count", 0)),
                )
                self._json({"ready": True, "job_id": job_id, "estimate": estimate})
            elif parsed.path == "/api/validate":
                state = body.get("workflow", body)
                self._json(
                    {
                        "issues": validate_workflow(state),
                        "console_version": console_version(state.get("console_path", "")),
                    }
                )
            elif parsed.path == "/api/prepare":
                messages: list[str] = []
                result = prepare_run(body.get("workflow", body), messages.append)
                self._json(
                    {
                        "preparation": result,
                        "messages": messages,
                        "download_url": _register_download(result["bundle"]),
                    }
                )
            elif parsed.path == "/api/mztab/validate":
                job_id = str(body.get("job_id", "")).strip()
                job = None
                if job_id:
                    with JOBS_LOCK:
                        job = JOBS.get(job_id)
                    if job is None:
                        raise ValueError(f"Job not found: {job_id}")
                target = body.get("file_path", "") or body.get("run_directory", "")
                self._json(
                    {
                        "validation": (
                            validate_mztab_files(
                                _job_artifact_paths(job, "mztab"),
                                (job.get("preparation") or {}).get("run_directory", ""),
                            )
                            if job is not None
                            else validate_mztab_outputs(target)
                        ),
                        "job_id": job_id,
                    }
                )
            elif parsed.path == "/api/mztab/list":
                self._json(
                    {
                        "mztab": list_mztab_outputs(
                            body.get("run_directory", "")
                        )
                    }
                )
            elif parsed.path == "/api/mztab/preview":
                job_id = str(body.get("job_id", "")).strip()
                file_path = body.get("file_path", "") or None
                run_directory = body.get("run_directory", "")
                if job_id:
                    with JOBS_LOCK:
                        job = JOBS.get(job_id)
                    if job is None:
                        raise ValueError(f"Job not found: {job_id}")
                    files = _job_artifact_paths(job, "mztab")
                    if not files:
                        raise FileNotFoundError(
                            f"Job {job_id} did not create or update an mzTab-M file."
                        )
                    if file_path:
                        requested = str(Path(file_path).expanduser().resolve()).casefold()
                        owned = {str(Path(path).resolve()).casefold(): path for path in files}
                        if requested not in owned:
                            raise ValueError(
                                f"The requested mzTab-M file was not created or updated by job {job_id}."
                            )
                        file_path = owned[requested]
                    else:
                        file_path = files[0]
                    run_directory = (job.get("preparation") or {}).get("run_directory", "")
                preview = preview_mztab_outputs(run_directory, file_path)
                if job_id:
                    preview["files"] = files
                self._json({"preview": preview, "job_id": job_id})
            elif parsed.path == "/api/qa/list":
                files = find_qa_files(body.get("path", ""))
                self._json(
                    {
                        "files": [str(path) for path in files],
                        "default_file": str(files[0]) if files else "",
                    }
                )
            elif parsed.path == "/api/qa/report":
                job_id = str(body.get("job_id", "")).strip()
                qa_path = str(body.get("file_path", "")).strip()
                if job_id:
                    with JOBS_LOCK:
                        job = JOBS.get(job_id)
                    if job is None:
                        raise ValueError(f"Job not found: {job_id}")
                    qa_files = _job_artifact_paths(job, "qa")
                    if not qa_files:
                        raise FileNotFoundError(
                            f"Job {job_id} did not create or update an LC-MS QA matrix. "
                            "Enable Height matrix export and set Export folder path before running."
                        )
                    qa_path = qa_files[0]
                elif not qa_path:
                    raise ValueError("Set job_id or an explicit QA file_path.")
                self._json(
                    {
                        "report": build_lcms_qa_report_from_file(
                            qa_path,
                            body.get("internal_standards", []),
                        ),
                        "job_id": job_id,
                        "qa_file": qa_path,
                    }
                )
            elif parsed.path == "/api/publication/report":
                state = body.get("workflow", {})
                job_id = str(body.get("job_id", "")).strip()
                job = None
                if job_id:
                    with JOBS_LOCK:
                        job = JOBS.get(job_id)
                    if job is None:
                        raise ValueError(f"Job not found: {job_id}")
                run_directory = str(
                    body.get("run_directory", "")
                    or ((job or {}).get("preparation") or {}).get("run_directory", "")
                ).strip()
                settings_file = Path(run_directory).expanduser() / "workflow-settings.json"
                used_saved_settings = body.get("use_saved_run", True) and settings_file.is_file()
                if used_saved_settings:
                    state = json.loads(settings_file.read_text(encoding="utf-8-sig"))
                additional_provenance = body.get("additional_library_provenance", [])
                if additional_provenance:
                    state["library_provenance"] = [
                        *state.get("library_provenance", []),
                        *additional_provenance,
                    ]
                qa_report = body.get("qa_report")
                qa_path = str(body.get("qa_file_path", "")).strip()
                run_qa = bool(body.get("run_qa", True))
                output_root = run_directory or str(state.get("output_root", "")).strip()
                if not output_root:
                    raise ValueError("Set a run/output directory for the publication report.")
                if run_qa and job is not None:
                    # A job-scoped publication must derive QA from that job's own
                    # matrix, never from a client-supplied report object.
                    qa_report = None
                    qa_files = _job_artifact_paths(job, "qa")
                    if qa_path:
                        requested = str(Path(qa_path).expanduser().resolve()).casefold()
                        owned = {str(Path(path).resolve()).casefold() for path in qa_files}
                        if requested not in owned:
                            raise ValueError(
                                f"The requested QA matrix was not created or updated by job {job_id}."
                            )
                    elif qa_files:
                        qa_path = qa_files[0]
                if run_qa and not qa_report and not qa_path:
                    raise FileNotFoundError(
                        "No QA matrix is associated with this publication request. "
                        "Set run_qa=false to generate Materials and Methods without QA."
                    )
                if run_qa and not qa_report and qa_path:
                    qa_report = build_lcms_qa_report_from_file(
                        qa_path, body.get("internal_standards", [])
                    )
                result = generate_publication_report(
                    state,
                    qa_report,
                    output_root,
                    app_version=str(
                        state.get("msdial_interactive_version")
                        or ("not recorded" if used_saved_settings else __version__)
                    ),
                    console_version=str(
                        state.get("msdial_console_version")
                        or (
                            "not recorded"
                            if used_saved_settings
                            else console_version(state.get("console_path", ""))
                        )
                    ),
                    qa_criteria=body.get("qa_criteria"),
                )
                self._json(
                    {
                        "report": result,
                        "downloads": {
                            "methods": _register_download(result["methods_file"]),
                            "qa_results": _register_download(result["qa_results_file"]),
                            "supplementary_workbook": _register_download(result["supplementary_workbook"]),
                            "supplementary_table": _register_download(result["supplementary_table"]),
                            "audit": _register_download(result["audit_file"]),
                            "bundle": _register_download(result["bundle"]),
                        },
                        "used_saved_settings": used_saved_settings,
                        "settings_file": str(settings_file) if settings_file.is_file() else "",
                        "qa_file": qa_path,
                        "job_id": job_id,
                        "qa_included": bool(qa_report),
                    }
                )
            elif parsed.path == "/api/agent/handoff":
                job_id = body.get("job_id", "")
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                run_directory = body.get("run_directory", "")
                if job is None and not run_directory:
                    self._json(
                        {"error": "Set job_id or run_directory."},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                self._json(
                    {
                        "handoff": create_datamining_handoff(
                            job=job,
                            run_directory=run_directory or None,
                        )
                    }
                )
            elif parsed.path == "/api/export-workflow":
                result = prepare_run(body.get("workflow", body))
                self._json(
                    {
                        "preparation": result,
                        "download_url": _register_download(result["bundle"]),
                    }
                )
            elif parsed.path == "/api/run":
                state = body.get("workflow", body)
                preparation = prepare_run(state)
                job_id = uuid.uuid4().hex
                artifact_baseline = _snapshot_run_artifacts(preparation)
                with JOBS_LOCK:
                    JOBS[job_id] = {
                        "id": job_id,
                        "status": "queued",
                        "kind": "run",
                        "logs": [],
                        "preparation": preparation,
                        "exit_code": None,
                        "created_at": dt.datetime.now().astimezone().isoformat(),
                        "artifact_baseline": artifact_baseline,
                        "artifacts": {},
                    }
                    _persist_jobs_locked()
                threading.Thread(
                    target=_run_job,
                    args=(job_id, preparation),
                    daemon=True,
                ).start()
                self._json(
                    {
                        "job_id": job_id,
                        "preparation": preparation,
                        "download_url": _register_download(preparation["bundle"]),
                    }
                )
            elif parsed.path == "/api/tuning/run":
                state = body.get("workflow", body)
                preparation = prepare_tuning_run(
                    state,
                    body.get("file_path", ""),
                    state.get("output_root", ""),
                )
                job_id = uuid.uuid4().hex
                with JOBS_LOCK:
                    JOBS[job_id] = {
                        "id": job_id,
                        "status": "queued",
                        "kind": "tuning",
                        "logs": [],
                        "preparation": preparation,
                        "exit_code": None,
                        "result": None,
                        "created_at": dt.datetime.now().astimezone().isoformat(),
                    }
                    _persist_jobs_locked()
                threading.Thread(
                    target=_run_tuning_job,
                    args=(job_id, preparation),
                    daemon=True,
                ).start()
                self._json({"job_id": job_id, "preparation": preparation})
            elif parsed.path == "/api/rt-correction/run":
                state = body.get("workflow", body)
                preparation = prepare_rt_correction_run(state)
                job_id = uuid.uuid4().hex
                with JOBS_LOCK:
                    JOBS[job_id] = {
                        "id": job_id,
                        "status": "queued",
                        "kind": "rt_correction",
                        "logs": [],
                        "preparation": preparation,
                        "exit_code": None,
                        "result": None,
                        "created_at": dt.datetime.now().astimezone().isoformat(),
                    }
                    _persist_jobs_locked()
                threading.Thread(
                    target=_run_rt_correction_job,
                    args=(job_id, preparation),
                    daemon=True,
                ).start()
                self._json({"job_id": job_id, "preparation": preparation})
            elif parsed.path == "/api/rt-correction/save":
                state = body.get("workflow", {})
                path = save_rt_correction_selections(state, body.get("rows", []))
                self._json({"selection_file": path})
            else:
                self._json({"error": "Unknown endpoint."}, HTTPStatus.NOT_FOUND)
        except Exception as error:
            self._json(
                {"error": str(error), "trace": traceback.format_exc()},
                HTTPStatus.BAD_REQUEST,
            )

    def log_message(self, format: str, *args: object) -> None:
        print(f"[http] {self.address_string()} {format % args}", file=sys.stderr)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _download(self, target: Path) -> None:
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix.lower() in {".txt", ".tsv", ".csv", ".json"}:
            content_type += "; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{target.name}"',
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, request_path: str) -> None:
        relative = (
            "index.html"
            if request_path in ("", "/", "/rt-correction", "/rt-correction/")
            else request_path.lstrip("/")
        )
        target = (STATIC / relative).resolve()
        if target.parent != STATIC.resolve() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def _run_library_download_job(job_id: str, catalog_id: str) -> None:
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
        JOBS[job_id]["logs"].append("Downloading the selected Zenodo library.")
        _persist_jobs_locked()

    def progress(received: int, total: int) -> None:
        with JOBS_LOCK:
            JOBS[job_id]["received"] = received
            JOBS[job_id]["total"] = total
            JOBS[job_id]["progress"] = round(received / total * 100, 1) if total else 0
            progress_value = float(JOBS[job_id]["progress"] or 0)
            if progress_value - float(JOBS[job_id].get("persisted_progress", -5)) >= 5:
                JOBS[job_id]["persisted_progress"] = progress_value
                JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
                _persist_jobs_locked()

    try:
        result = download_library(catalog_id, progress)
        with JOBS_LOCK:
            JOBS[job_id]["result"] = result
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["status"] = "completed"
            JOBS[job_id]["logs"].append(
                "Library ready: " + str(result["local_path"])
            )
            JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
            _persist_jobs_locked()
    except Exception as error:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = str(error)
            JOBS[job_id]["logs"].append(traceback.format_exc())
            JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
            _persist_jobs_locked()


def _run_job(job_id: str, preparation: dict[str, Any]) -> None:
    def log(line: str) -> None:
        with JOBS_LOCK:
            JOBS[job_id]["logs"].append(line)
            JOBS[job_id]["logs"] = JOBS[job_id]["logs"][-2000:]

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
        _persist_jobs_locked()
    try:
        log("Starting MS-DIAL Console run.")
        log("Command: " + " ".join(preparation["command"]))
        log("Large vendor raw files can take several minutes before the first Console message.")
        exit_code = run_console(preparation, log)
        validation = None
        handoff = None
        with JOBS_LOCK:
            baseline = dict(JOBS[job_id].get("artifact_baseline") or {})
        artifacts = _changed_run_artifacts(preparation, baseline)
        artifact_warnings: list[str] = []
        if exit_code == 0 and preparation.get("qa_matrix_expected") and not artifacts["qa"]:
            warning = (
                "LC-MS QA matrix export was requested, but this job did not create or update "
                "a *.qa.tsv file. Verify that the selected Console advertises the "
                "lcms_alignment_qa_matrix capability and that Export folder path is writable."
            )
            artifact_warnings.append(warning)
            log("WARNING: " + warning)
        if exit_code == 0:
            validation = validate_mztab_files(
                artifacts["mztab"], preparation["run_directory"]
            )
            summary = validation["summary"]
            log(
                "mzTab-M validation: "
                f"{summary['status']} "
                f"({summary['passed']} passed, "
                f"{summary['warnings']} warning, "
                f"{summary['failed']} failed)."
            )
            handoff = create_datamining_handoff(
                preparation=preparation,
                job={
                    "id": job_id,
                    "kind": "run",
                    "status": "completed",
                    "exit_code": exit_code,
                    "preparation": preparation,
                    "mztab_validation": validation,
                    "artifacts": artifacts,
                    "logs": [],
                },
            )
            if handoff.get("handoff_file"):
                log("Data-mining handoff: " + handoff["handoff_file"])
        with JOBS_LOCK:
            JOBS[job_id]["exit_code"] = exit_code
            JOBS[job_id]["mztab_validation"] = validation
            JOBS[job_id]["datamining_handoff"] = handoff
            JOBS[job_id]["artifacts"] = artifacts
            JOBS[job_id]["warnings"] = artifact_warnings
            JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
            if exit_code == 0:
                JOBS[job_id]["status"] = "completed"
            else:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["error"] = _diagnose_console_failure(
                    JOBS[job_id]["logs"],
                    f"MS-DIAL Console exited with code {exit_code}.",
                )
            JOBS[job_id].pop("artifact_baseline", None)
            _persist_jobs_locked()
    except Exception as error:
        with JOBS_LOCK:
            message = _diagnose_console_failure(JOBS[job_id]["logs"], str(error))
        log(traceback.format_exc())
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = message
            JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
            JOBS[job_id].pop("artifact_baseline", None)
            _persist_jobs_locked()
    finally:
        if preparation.get("preserve_temporary_input_folder"):
            folder = preparation.get("temporary_input_folder")
            if folder:
                log(
                    "Keeping folder-type input links for the saved MS-DIAL project: "
                    + str(folder)
                )
        else:
            _cleanup_temporary_input_folder(preparation.get("temporary_input_folder"), log)


def _run_tuning_job(job_id: str, preparation: dict[str, Any]) -> None:
    def log(line: str) -> None:
        with JOBS_LOCK:
            JOBS[job_id]["logs"].append(line)
            JOBS[job_id]["logs"] = JOBS[job_id]["logs"][-2000:]

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
        _persist_jobs_locked()
    try:
        exit_code = run_console(preparation, log)
        result = None
        if exit_code == 0:
            analysis_type = str(preparation.get("analysis_type", "lcms")).lower()
            diagnostic_result = preparation.get("diagnostic_result_file")
            try:
                if analysis_type == "gcms":
                    result = parse_mdscan(diagnostic_result or find_mdscan(preparation["run_directory"]))
                else:
                    result = parse_mdpeak(diagnostic_result or find_mdpeak(preparation["run_directory"]))
            except FileNotFoundError as missing:
                raise RuntimeError(
                    "MS-DIAL finished without generating the expected diagnostic result file. "
                    "Check whether the selected Console build can read this raw-data format and "
                    f"whether vendor dependencies are installed. Missing: {missing}"
                ) from missing
        with JOBS_LOCK:
            JOBS[job_id]["exit_code"] = exit_code
            JOBS[job_id]["result"] = result
            JOBS[job_id]["status"] = "completed" if exit_code == 0 else "failed"
            if exit_code != 0:
                JOBS[job_id]["error"] = _diagnose_console_failure(
                    JOBS[job_id]["logs"],
                    f"MS-DIAL Console exited with code {exit_code}.",
                )
            JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
            _persist_jobs_locked()
    except Exception as error:
        with JOBS_LOCK:
            message = _diagnose_console_failure(JOBS[job_id]["logs"], str(error))
        log(traceback.format_exc())
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = message
            JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
            _persist_jobs_locked()
    finally:
        _cleanup_temporary_input_folder(
            preparation.get("diagnostic_input_folder") or preparation.get("temporary_input_folder"),
            log,
        )


def _run_rt_correction_job(job_id: str, preparation: dict[str, Any]) -> None:
    def log(line: str) -> None:
        with JOBS_LOCK:
            JOBS[job_id]["logs"].append(line)
            JOBS[job_id]["logs"] = JOBS[job_id]["logs"][-2000:]

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
        _persist_jobs_locked()
    try:
        log("Starting RT correction EIC audit.")
        log("Command: " + " ".join(preparation["command"]))
        preparation["run_started_ns"] = time.time_ns()
        exit_code = run_console(preparation, log)
        result = parse_rt_correction_result(preparation) if exit_code == 0 else None
        with JOBS_LOCK:
            JOBS[job_id]["exit_code"] = exit_code
            JOBS[job_id]["result"] = result
            JOBS[job_id]["status"] = "completed" if exit_code == 0 else "failed"
            if exit_code != 0:
                JOBS[job_id]["error"] = _diagnose_console_failure(
                    JOBS[job_id]["logs"],
                    f"MS-DIAL Console exited with code {exit_code}.",
                )
            JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
            _persist_jobs_locked()
    except Exception as error:
        with JOBS_LOCK:
            message = _diagnose_console_failure(JOBS[job_id]["logs"], str(error))
        log(traceback.format_exc())
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = message
            JOBS[job_id]["updated_at"] = dt.datetime.now().astimezone().isoformat()
            _persist_jobs_locked()


def _cleanup_temporary_input_folder(path: str | None, log: Any) -> None:
    if not path:
        return
    staging = Path(path).resolve()
    base = (ROOT / "work" / "console_inputs").resolve()
    if staging.parent != base or not staging.name.startswith(".msdial_interactive_input_"):
        log(f"Skipped cleanup for unexpected temporary input folder: {staging}")
        return
    try:
        for child in staging.iterdir():
            child.rmdir()
        staging.rmdir()
    except OSError as error:
        log(f"Could not clean up temporary input folder {staging}: {error}")


def _pick_files() -> list[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        paths = filedialog.askopenfilenames(
            title="Select MS-DIAL analysis files",
            filetypes=[
                ("MS-DIAL raw data", "*.wiff *.wiff2 *.raw *.mzML *.mzXML *.cdf *.abf *.ibf"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
        return list(paths)
    except Exception:
        return []


def _pick_directory(title: str = "Select directory") -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        path = filedialog.askdirectory(title=title)
        root.destroy()
        return path
    except Exception:
        return ""


def _pick_mztab_file() -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        path = filedialog.askopenfilename(
            title="Select mzTab-M output",
            filetypes=[
                ("mzTab-M files", "*.mzTab *.mztab *.mzTabM *.mztabm *.txt"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
        return path
    except Exception:
        return ""


def _pick_qa_file() -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        path = filedialog.askopenfilename(
            title="Select MS-DIAL LC-MS quality-assurance matrix",
            filetypes=[
                ("MS-DIAL QA matrix", "*.qa.tsv"),
                ("Tab-separated files", "*.tsv"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
        return path
    except Exception:
        return ""


def _pick_reference_file(kind: str) -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        if kind == "rt-selection":
            title = "Select RT correction peak selections"
            filetypes = [("RT correction selection", "*.tsv *.txt"), ("All files", "*.*")]
        elif kind == "analysis-csv":
            title = "Select MS-DIAL analysis metadata CSV"
            filetypes = [("Analysis metadata CSV", "*.csv"), ("All files", "*.*")]
        else:
            title = "Select RT correction anchor library"
            filetypes = [("MS-DIAL text library", "*.txt *.tsv"), ("All files", "*.*")]
        path = filedialog.askopenfilename(title=title, filetypes=filetypes)
        root.destroy()
        return path
    except Exception:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="MS-DIAL Interactive local web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--rt-correction",
        action="store_true",
        help="Open the focused RT correction review workspace.",
    )
    parser.add_argument(
        "--lab",
        action="store_true",
        help="Serve on all network interfaces for lab-internal use.",
    )
    args = parser.parse_args()
    if args.lab:
        args.host = "0.0.0.0"
        args.no_browser = True
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    start_path = "/rt-correction" if args.rt_correction else "/"
    url = f"http://{args.host}:{args.port}{start_path}"
    print(f"MS-DIAL Interactive: {url}")
    if args.host in ("0.0.0.0", "::"):
        print("Lab server mode: use only on a trusted lab network.")
        print("Raw-data paths must be visible from this server, not only from a client PC.")
        lan_urls = _local_ipv4_addresses()
        if lan_urls:
            print("Candidate lab URLs:")
            for address in lan_urls:
                print(f"  http://{address}:{args.port}")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

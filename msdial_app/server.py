from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import threading
import traceback
import urllib.parse
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .agent_bridge import create_datamining_handoff, summarize_jobs
from .knowledge import KnowledgeBase, next_parameter_question
from .literature import recommend_from_literature
from .mztab_validation import list_mztab_outputs, validate_mztab_outputs
from .mztab_preview import preview_mztab_outputs
from .workflow import (
    console_version,
    expand_paths,
    expand_paths_report,
    find_mdpeak,
    find_mdscan,
    detect_raw_format,
    parse_method,
    parse_mdpeak,
    parse_mdscan,
    prepare_run,
    prepare_tuning_run,
    read_adducts,
    read_lipid_queries,
    run_console,
    is_supported,
    validate_workflow,
)


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
RESOURCES = ROOT / "resources"
KNOWLEDGE = ROOT / "knowledge"
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
DOWNLOADS: dict[str, Path] = {}
KB = KnowledgeBase(KNOWLEDGE)


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
    configured = os.environ.get("MSDIAL_CONSOLE_PATH", "")
    candidates = [
        Path(configured) if configured else None,
        ROOT.parent
        / "MsdialWorkbench"
        / "tests"
        / "MSDIAL5"
        / "MsdialCoreTestApp"
        / "bin"
        / "Release"
        / "net48"
        / "MSDIALCUI.exe",
        ROOT.parent
        / "MsdialWorkbench"
        / "tests"
        / "MSDIAL5"
        / "MsdialCoreTestApp"
        / "bin"
        / "Release"
        / "net8"
        / "MSDIALCUI.dll",
        ROOT.parent
        / "MSDIAL.console.v5.5.260323-windows-net48"
        / "MSDIALCUI.exe",
        ROOT / "MSDIALCUI.exe",
        ROOT / "MSDIALCUI.dll",
    ]
    return next((str(path.resolve()) for path in candidates if path and path.is_file()), "")


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
            self._json(
                {
                    "platform": os.name,
                    "python": os.sys.version.split()[0],
                    "root": str(ROOT),
                    "server": {
                        "bind_host": bind_host,
                        "port": bind_port,
                        "shared_server": shared_server,
                        "lan_urls": lan_urls,
                    },
                    "default_console": _default_console_path(),
                    "default_queries": str(RESOURCES / "LbmQueries.txt"),
                    "default_template": str(RESOURCES / "msdial_console_param4lipidomics.txt"),
                    "default_gcms_template": str(RESOURCES / "gcms_console_param_kovats.txt"),
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
                    "lipid_queries": read_lipid_queries(RESOURCES / "LbmQueries.txt"),
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
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                response = dict(job) if job else None
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
            elif parsed.path == "/api/literature/recommend":
                self._json(
                    recommend_from_literature(
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
                self._json(
                    {
                        "validation": validate_mztab_outputs(
                            body.get("file_path", "") or body.get("run_directory", "")
                        )
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
                self._json(
                    {
                        "preview": preview_mztab_outputs(
                            body.get("run_directory", ""),
                            body.get("file_path", "") or None,
                        )
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
                with JOBS_LOCK:
                    JOBS[job_id] = {
                        "id": job_id,
                        "status": "queued",
                        "logs": [],
                        "preparation": preparation,
                        "exit_code": None,
                    }
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
                    }
                threading.Thread(
                    target=_run_tuning_job,
                    args=(job_id, preparation),
                    daemon=True,
                ).start()
                self._json({"job_id": job_id, "preparation": preparation})
            else:
                self._json({"error": "Unknown endpoint."}, HTTPStatus.NOT_FOUND)
        except Exception as error:
            self._json(
                {"error": str(error), "trace": traceback.format_exc()},
                HTTPStatus.BAD_REQUEST,
            )

    def log_message(self, format: str, *args: object) -> None:
        print(f"[http] {self.address_string()} {format % args}")

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
        self.send_header("Content-Type", "application/zip")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{target.name}"',
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
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


def _run_job(job_id: str, preparation: dict[str, Any]) -> None:
    def log(line: str) -> None:
        with JOBS_LOCK:
            JOBS[job_id]["logs"].append(line)
            JOBS[job_id]["logs"] = JOBS[job_id]["logs"][-2000:]

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
    try:
        log("Starting MS-DIAL Console run.")
        log("Command: " + " ".join(preparation["command"]))
        log("Large vendor raw files can take several minutes before the first Console message.")
        exit_code = run_console(preparation, log)
        validation = None
        handoff = None
        if exit_code == 0:
            validation = validate_mztab_outputs(preparation["run_directory"])
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
                    "logs": [],
                },
            )
            if handoff.get("handoff_file"):
                log("Data-mining handoff: " + handoff["handoff_file"])
        with JOBS_LOCK:
            JOBS[job_id]["exit_code"] = exit_code
            JOBS[job_id]["mztab_validation"] = validation
            JOBS[job_id]["datamining_handoff"] = handoff
            if exit_code == 0:
                JOBS[job_id]["status"] = "completed"
            else:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["error"] = _diagnose_console_failure(
                    JOBS[job_id]["logs"],
                    f"MS-DIAL Console exited with code {exit_code}.",
                )
    except Exception as error:
        with JOBS_LOCK:
            message = _diagnose_console_failure(JOBS[job_id]["logs"], str(error))
        log(traceback.format_exc())
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = message
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
    except Exception as error:
        with JOBS_LOCK:
            message = _diagnose_console_failure(JOBS[job_id]["logs"], str(error))
        log(traceback.format_exc())
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = message
    finally:
        _cleanup_temporary_input_folder(
            preparation.get("diagnostic_input_folder") or preparation.get("temporary_input_folder"),
            log,
        )


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


def main() -> None:
    parser = argparse.ArgumentParser(description="MS-DIAL Interactive local web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-browser", action="store_true")
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
    url = f"http://{args.host}:{args.port}"
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

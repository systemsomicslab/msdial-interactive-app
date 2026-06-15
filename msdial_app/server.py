from __future__ import annotations

import argparse
import json
import mimetypes
import os
import threading
import traceback
import urllib.parse
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .knowledge import KnowledgeBase, next_parameter_question
from .workflow import (
    console_version,
    expand_paths,
    expand_paths_report,
    find_mdpeak,
    parse_method,
    parse_mdpeak,
    prepare_run,
    prepare_tuning_run,
    read_lipid_queries,
    run_console,
    validate_workflow,
)


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
RESOURCES = ROOT / "resources"
KNOWLEDGE = ROOT / "knowledge"
UPLOADS = ROOT / "work" / "uploads"
TUNING_RUNS = ROOT / "work" / "tuning-runs"
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
KB = KnowledgeBase(KNOWLEDGE)


def _diagnose_console_failure(logs: list[str], fallback: str) -> str:
    text = "\n".join(logs).lower()
    if "basedataaccess" in text:
        return (
            "Agilent reader could not load BaseDataAccess.dll. Check the MS-DIAL Console "
            "package and its lib/Agilent deployment before checking the VC++ runtime."
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
    return fallback


class Handler(BaseHTTPRequestHandler):
    server_version = "MSDIALInteractive/0.1"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/config":
            self._json(
                {
                    "platform": os.name,
                    "python": os.sys.version.split()[0],
                    "root": str(ROOT),
                    "default_queries": str(RESOURCES / "LbmQueries.txt"),
                    "default_template": str(RESOURCES / "msdial_console_param4lipidomics.txt"),
                    "knowledge_cards": {"ja": KB.count("ja"), "en": KB.count("en")},
                    "lipid_queries": read_lipid_queries(RESOURCES / "LbmQueries.txt"),
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
            elif parsed.path == "/api/dialog/files":
                self._json(expand_paths_report(_pick_files()))
            elif parsed.path == "/api/dialog/directory":
                selected = _pick_directory()
                report = (
                    expand_paths_report([selected])
                    if selected
                    else {"files": [], "warnings": [], "rejected": []}
                )
                self._json({"path": selected, **report})
            elif parsed.path == "/api/upload-session":
                session = uuid.uuid4().hex
                directory = UPLOADS / session
                directory.mkdir(parents=True, exist_ok=False)
                self._json({"session": session, "root": str(directory.resolve())})
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
                self._json({"preparation": result, "messages": messages})
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
                self._json({"job_id": job_id, "preparation": preparation})
            elif parsed.path == "/api/tuning/run":
                state = body.get("workflow", body)
                preparation = prepare_tuning_run(
                    state,
                    body.get("file_path", ""),
                    TUNING_RUNS,
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

    def do_PUT(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/uploads/"):
            self._json({"error": "Unknown endpoint."}, HTTPStatus.NOT_FOUND)
            return
        parts = parsed.path.split("/")
        if len(parts) not in (4, 5):
            self._json({"error": "Invalid upload path."}, HTTPStatus.BAD_REQUEST)
            return
        session = Path(parts[3]).name
        directory = (UPLOADS / session).resolve()
        if directory.parent != UPLOADS.resolve() or not directory.exists():
            self._json({"error": "Upload session not found."}, HTTPStatus.NOT_FOUND)
            return
        if len(parts) == 5:
            relative = Path(urllib.parse.unquote(parts[4])).name
        else:
            query = urllib.parse.parse_qs(parsed.query)
            relative = query.get("path", [""])[0].replace("\\", "/").lstrip("/")
        if not relative:
            self._json({"error": "Missing upload path."}, HTTPStatus.BAD_REQUEST)
            return
        target = (directory / Path(relative)).resolve()
        try:
            target.relative_to(directory)
        except ValueError:
            self._json({"error": "Invalid upload path."}, HTTPStatus.BAD_REQUEST)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        remaining = int(self.headers.get("Content-Length", "0"))
        with target.open("wb") as handle:
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                handle.write(chunk)
                remaining -= len(chunk)
        self._json({"path": str(target), "size": target.stat().st_size})

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
        exit_code = run_console(preparation, log)
        generated_mdpeaks = list(Path(preparation["run_directory"]).glob("*.mdpeak"))
        with JOBS_LOCK:
            JOBS[job_id]["exit_code"] = exit_code
            if exit_code == 0 and generated_mdpeaks:
                JOBS[job_id]["status"] = "completed"
            else:
                JOBS[job_id]["status"] = "failed"
                fallback = (
                    "MS-DIAL returned exit code 0 but generated no mdpeak file. "
                    "Check the Console log for an import or parser error."
                    if exit_code == 0
                    else f"MS-DIAL Console exited with code {exit_code}."
                )
                JOBS[job_id]["error"] = _diagnose_console_failure(
                    JOBS[job_id]["logs"],
                    fallback,
                )
    except Exception as error:
        with JOBS_LOCK:
            message = _diagnose_console_failure(JOBS[job_id]["logs"], str(error))
        log(traceback.format_exc())
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = message


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
            result = parse_mdpeak(find_mdpeak(preparation["run_directory"]))
        with JOBS_LOCK:
            JOBS[job_id]["exit_code"] = exit_code
            JOBS[job_id]["result"] = result
            JOBS[job_id]["status"] = "completed" if exit_code == 0 else "failed"
    except Exception as error:
        with JOBS_LOCK:
            message = _diagnose_console_failure(JOBS[job_id]["logs"], str(error))
        log(traceback.format_exc())
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = message


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


def _pick_directory() -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        path = filedialog.askdirectory(title="Select directory")
        root.destroy()
        return path
    except Exception:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="MS-DIAL Interactive local web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"MS-DIAL Interactive: {url}")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

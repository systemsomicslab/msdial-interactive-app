"""POST /api/jobs/<id>/cancel stops a run, a diagnostic or a repository download.

Nothing could stop a job before this. msdial_interactive_wait_for_completion and
msdial_complete_guided_analysis stop polling at their timeout and leave the Console running, and a
download ran to its last byte or its first error. A cancel sets the job's flag: a Console job's watch
stops the Console's process tree and the job fails with exit code -4; a download stops at its next
progress report, and its lease - already in the unit's manifest - records download_failed with reason
cancelled. A job still queued stops before it starts anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import mcp_server, repository_reanalysis, server
    from msdial_app.process_liveness import process_is_alive
    from msdial_app.repository_reanalysis import (
        RepositoryFile,
        RepositoryProject,
        _write_json,
        read_manifest,
    )
    from msdial_app.workflow import CONSOLE_EXIT_CANCELLED


def _wait_for(condition, within: float = 20.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


class _Backend:
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.jobs: dict = {}
        patches = [
            patch.object(server, "JOBS", self.jobs),
            patch.object(server, "PROCESSES", {}),
            patch.object(server, "_persist_jobs_locked", lambda: None),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.backend.shutdown()
        self.backend.server_close()
        self.thread.join(timeout=5)
        self.directory.cleanup()

    def cancel(self, job_id: str, **body) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.backend.server_port}/api/jobs/{job_id}/cancel",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read().decode("utf-8"))
            error.close()
            return error.code, payload


class CancellingAConsoleJob(_Backend, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.output = self.root / "unit" / "output"
        self.output.mkdir(parents=True)
        self.manifest = self.root / "unit" / "provenance" / "run-manifest.json"
        _write_json(self.manifest, {"status": "preflight_passed", "project": {"analysis_unit_id": "unit-a"}})

    def preparation(self, command: list[str]) -> dict:
        return {
            "command": command,
            "run_directory": str(self.output),
            "export_folder_path": str(self.output),
            "repository_run_manifest": str(self.manifest),
            "repository_raw_retention_policy": "keep",
            "expected_analysis_exports": [],
            "qa_matrix_expected": False,
        }

    def register(self, job_id: str, kind: str, preparation: dict) -> None:
        with server.JOBS_LOCK:
            self.jobs[job_id] = {
                "id": job_id, "status": "queued", "kind": kind, "logs": [], "preparation": preparation,
                "artifact_baseline": {},
            }
            server._register_process_locked(job_id, kind)

    def test_a_running_console_is_stopped_and_the_job_fails_with_minus_4(self) -> None:
        preparation = self.preparation([sys.executable, "-c", "import time; time.sleep(120)"])
        self.register("run1", "run", preparation)
        worker = threading.Thread(target=server._run_job, args=("run1", preparation), daemon=True)
        worker.start()
        self.assertTrue(_wait_for(lambda: (self.jobs["run1"].get("console_process") or {}).get("pid")))
        pid = self.jobs["run1"]["console_process"]["pid"]

        status, response = self.cancel("run1", reason="operator stop")
        worker.join(timeout=60)
        job = self.jobs["run1"]
        manifest = read_manifest(self.manifest)

        self.assertEqual(200, status, response)
        self.assertTrue(response["cancel_requested"])
        self.assertEqual(pid, response["console_pid"])
        self.assertFalse(worker.is_alive())
        self.assertEqual(CONSOLE_EXIT_CANCELLED, job["exit_code"])
        self.assertEqual("failed", job["status"])
        self.assertEqual("cancelled", job["stop_reason"])
        self.assertTrue(job["cancel_requested_at"])
        self.assertEqual("operator stop", job["cancel_reason"])
        self.assertTrue(job["error"].startswith("The job was cancelled"), job["error"])
        self.assertTrue(_wait_for(lambda: process_is_alive(pid) is False), "the Console is stopped")
        self.assertEqual((-4, "cancelled"), (manifest["run_attempts"][-1]["exit_code"],
                                             manifest["run_attempts"][-1]["reason"]))
        self.assertEqual("run_failed", manifest["status"])

    def test_a_queued_job_stops_before_its_console_starts(self) -> None:
        preparation = self.preparation([sys.executable, "-c", "pass"])
        self.register("tune1", "tuning", preparation)

        status, response = self.cancel("tune1")
        with patch("msdial_app.workflow.subprocess.Popen", side_effect=AssertionError("no Console")):
            server._run_tuning_job("tune1", preparation)

        self.assertEqual(200, status)
        self.assertTrue(response["cancel_requested"])
        self.assertEqual(CONSOLE_EXIT_CANCELLED, self.jobs["tune1"]["exit_code"])
        self.assertEqual("failed", self.jobs["tune1"]["status"])

    def test_a_second_cancel_is_harmless(self) -> None:
        self.register("run2", "run", self.preparation(["MSDIALCUI.exe"]))

        first = self.cancel("run2")
        second = self.cancel("run2")

        self.assertEqual((200, False), (first[0], first[1]["already_requested"]))
        self.assertEqual((200, True), (second[0], second[1]["already_requested"]))
        self.assertEqual(1, sum(1 for line in self.jobs["run2"]["logs"] if line.startswith("Cancellation")))

    def test_a_finished_job_has_nothing_to_stop(self) -> None:
        self.jobs["done"] = {"id": "done", "kind": "run", "status": "completed", "logs": []}

        status, response = self.cancel("done")

        self.assertEqual(200, status)
        self.assertFalse(response["cancel_requested"])
        self.assertEqual("finished", response["reason"])

    def test_a_run_past_its_console_is_not_interrupted(self) -> None:
        """Once the Console has exited, the job is validating and recording its outputs."""
        self.jobs["late"] = {"id": "late", "kind": "run", "status": "running", "logs": []}

        status, response = self.cancel("late")

        self.assertEqual(200, status)
        self.assertEqual("console_finished", response["reason"])

    def test_unknown_and_uncancellable_jobs_are_refused(self) -> None:
        self.jobs["lib"] = {"id": "lib", "kind": "library_download", "status": "running", "logs": []}

        self.assertEqual(404, self.cancel("nope")[0])
        status, response = self.cancel("lib")
        self.assertEqual(400, status)
        self.assertEqual("not_cancellable", response["code"])

    def test_the_mcp_tool_reaches_the_route(self) -> None:
        self.register("run3", "run", self.preparation(["MSDIALCUI.exe"]))

        response = mcp_server.msdial_cancel_job("run3", reason="stop", port=self.backend.server_port)
        missing = mcp_server.msdial_cancel_job("nope", port=self.backend.server_port)

        self.assertTrue(response["cancel_requested"])
        self.assertTrue(server.PROCESSES["run3"]["cancel"].is_set())
        self.assertFalse(missing["ok"])
        self.assertEqual(404, missing["http_status"])


class _SlowClient:
    """Stands in for the network: reports progress in steps, and the cancel arrives between two."""

    def __init__(self, data: bytes, between) -> None:
        self.data = data
        self.between = between

    def download(self, url, destination, _maximum_bytes, progress_callback=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        with partial.open("wb") as handle:
            for index in range(4):
                handle.write(self.data[index::4])
                if progress_callback:
                    progress_callback((index + 1) * len(self.data) // 4, len(self.data))
                if index == 1:
                    self.between()
        partial.replace(destination)
        return {
            "path": str(destination), "size_bytes": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(), "md5": hashlib.md5(self.data).hexdigest(),
            "resumed_from_bytes": 0,
        }


class CancellingARepositoryDownload(_Backend, unittest.TestCase):
    def project(self) -> RepositoryProject:
        return RepositoryProject(
            repository="metabolights",
            accession="MTBLS-CANCEL",
            analysis_unit_id="unit-c",
            eligible=True,
            selection_status="eligible",
            files=[RepositoryFile("FILES/a.mzML", 8, "https://example.org/a.mzML")],
            total_download_bytes=8,
            sample_metadata=[{"sample_id": "a", "raw_file": "a.mzML"}],
        )

    def start(self, job_id: str) -> None:
        with server.JOBS_LOCK:
            self.jobs[job_id] = {
                "id": job_id, "status": "queued", "kind": "repository_download", "logs": [], "result": None,
            }
            server._register_process_locked(job_id, "repository_download")

    def test_the_lease_records_download_failed_with_reason_cancelled(self) -> None:
        self.start("dl1")
        client = _SlowClient(b"abcdefgh", lambda: self.cancel("dl1"))
        with patch.object(repository_reanalysis, "RepositoryHttpClient", lambda: client):
            server._run_repository_download_job("dl1", self.project(), self.root, 1000, False, "keep")
        manifest = read_manifest(
            self.root / "metabolights" / "MTBLS-CANCEL" / "unit-c" / "provenance" / "run-manifest.json"
        )
        job = self.jobs["dl1"]

        self.assertEqual("download_failed", manifest["status"])
        self.assertEqual("cancelled", manifest["download_failure"]["reason"])
        self.assertEqual("RepositoryDownloadCancelled", manifest["download_failure"]["error_type"])
        self.assertEqual("failed", job["status"])
        self.assertEqual("cancelled", job["stop_reason"])
        self.assertIn("cancelled on request", job["error"])
        self.assertLess(job["received"], 8, "it stopped part-way")
        self.assertTrue(
            any(self.root.rglob("a.mzML.part")), "the partial file is kept for a resume"
        )
        self.assertNotIn("dl1", server.PROCESSES)

    def test_a_download_cancelled_while_queued_writes_no_lease(self) -> None:
        self.start("dl2")
        self.cancel("dl2")
        with patch.object(repository_reanalysis, "RepositoryHttpClient", side_effect=AssertionError("no fetch")):
            server._run_repository_download_job("dl2", self.project(), self.root, 1000, False, "keep")

        self.assertEqual("cancelled", self.jobs["dl2"]["stop_reason"])
        self.assertFalse(any(self.root.rglob("run-manifest.json")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

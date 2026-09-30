"""A run attempt is written into the unit's manifest before its Console starts, and closed after.

Every other record of a run was written after the Console returned: the failure record, the finalised run.
A backend that stopped while the Console ran wrote neither, and the unit looked exactly like a unit nobody
had started while a Console might still be writing into it. run_attempts[] fixes that: one entry per
attempt, opened before the start (job, kind, backend process, Console identity, output directory), given
the Console's process id once it exists, and closed with the exit code and the reason. Like the failure
record, writing it never raises, and a local analysis, which has no manifest, records nothing.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import server
    from msdial_app.process_liveness import process_is_alive
    from msdial_app.repository_reanalysis import (
        _write_json,
        live_run_attempt,
        read_manifest,
        record_run_end,
        record_run_process,
        record_run_start,
    )

CONSOLE = {
    "status": "verified",
    "version": "5.5.250930",
    "binary_sha256": "ab" * 32,
    "assembly_sha256": "cd" * 32,
    # A location, which the attempt must not carry: the Console is named by version and checksum.
    "assembly_path": "Q:\\synthetic\\console\\MSDIALCUI.dll",
    "warning": "",
}


def _end_leftover(pid: int) -> None:
    # Only a failed test leaves a stand-in Console running; it is not left behind for the next one.
    if process_is_alive(pid):
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGKILL)


class _Unit:
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.output = self.root / "unit" / "output"
        self.output.mkdir(parents=True)
        self.manifest = self.root / "unit" / "provenance" / "run-manifest.json"
        _write_json(
            self.manifest,
            {"status": "preflight_passed", "project": {"analysis_unit_id": "unit-a"}, "raw_retention_policy": "keep"},
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def attempts(self) -> list[dict]:
        return read_manifest(self.manifest).get("run_attempts") or []


class TheRecords(_Unit, unittest.TestCase):
    def test_a_start_opens_an_attempt_naming_its_job_backend_and_console(self) -> None:
        opened = record_run_start(
            self.manifest,
            "job1",
            "run",
            output_directory=self.output,
            console=CONSOLE,
            command=["MSDIALCUI.exe", "lcms", "-i", "a.csv"],
            timeout_seconds=3600.0,
        )
        [attempt] = self.attempts()

        self.assertTrue(opened["recorded"])
        self.assertEqual(opened["attempt_id"], attempt["attempt_id"])
        self.assertEqual(1, attempt["attempt"])
        self.assertEqual("job1", attempt["job_id"])
        self.assertEqual("run", attempt["kind"])
        self.assertIsNone(attempt["ended_at"])
        self.assertIsNone(attempt["exit_code"])
        self.assertTrue(attempt["started_at"].endswith("+00:00"), "recorded in UTC")
        self.assertEqual(str(self.output), attempt["output_directory"])
        self.assertEqual(3600.0, attempt["timeout_seconds"])
        self.assertIsNone(attempt["idle_timeout_seconds"])
        self.assertEqual(os.getpid(), attempt["backend"]["pid"])
        self.assertIsNotNone(attempt["backend"]["process_created_at"])
        self.assertEqual(
            {"version": "5.5.250930", "binary_sha256": "ab" * 32, "assembly_sha256": "cd" * 32,
             "provenance_status": "verified"},
            attempt["console"],
        )
        self.assertEqual(64, len(attempt["command_sha256"]))
        self.assertNotIn("synthetic", self.manifest.read_text(encoding="utf-8"), "no Console location")
        self.assertEqual("preflight_passed", read_manifest(self.manifest)["status"], "the status is untouched")

    def test_the_process_and_the_end_close_the_same_attempt(self) -> None:
        opened = record_run_start(self.manifest, "job1", "run")
        record_run_process(self.manifest, opened["attempt_id"], 4242, 1_790_000_000.5)
        closed = record_run_end(self.manifest, opened, -3, "timeout", {"elapsed_seconds": 3600.2})
        [attempt] = self.attempts()

        self.assertTrue(closed["recorded"])
        self.assertEqual(4242, attempt["console_pid"])
        self.assertEqual(1_790_000_000.5, attempt["console_process_created_at"])
        self.assertEqual(-3, attempt["exit_code"])
        self.assertEqual("timeout", attempt["reason"])
        self.assertEqual({"elapsed_seconds": 3600.2}, attempt["detail"])
        self.assertTrue(attempt["ended_at"])

    def test_attempts_are_appended_and_numbered_per_kind(self) -> None:
        for job, kind in (("t1", "tuning"), ("r1", "run"), ("r2", "run")):
            record_run_end(self.manifest, record_run_start(self.manifest, job, kind), 1, "exited")

        self.assertEqual(
            [("t1", "tuning", 1), ("r1", "run", 1), ("r2", "run", 2)],
            [(item["job_id"], item["kind"], item["attempt"]) for item in self.attempts()],
        )

    def test_an_end_whose_start_was_never_written_is_kept(self) -> None:
        opened = {"attempt_id": "lost", "job_id": "job9", "kind": "run", "recorded": False, "error": "busy"}

        record_run_end(self.manifest, opened, 0, "exited")
        [attempt] = self.attempts()

        self.assertTrue(attempt["start_unrecorded"])
        self.assertEqual("job9", attempt["job_id"])
        self.assertEqual(0, attempt["exit_code"])
        self.assertNotIn("error", attempt)

    def test_a_record_that_cannot_be_written_is_returned_not_raised(self) -> None:
        missing = self.root / "elsewhere" / "run-manifest.json"
        broken = self.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")

        for target in (missing, broken):
            opened = record_run_start(target, "job1")
            self.assertFalse(opened["recorded"])
            self.assertTrue(opened["error"])
            self.assertFalse(record_run_process(target, opened["attempt_id"], 1)["recorded"])
            self.assertFalse(record_run_end(target, opened, 0, "exited")["recorded"])
        self.assertFalse(missing.exists())
        self.assertEqual("{not json", broken.read_text(encoding="utf-8"))


class TheJobsRecordTheirAttempts(_Unit, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.jobs: dict = {}
        patches = [
            patch.object(server, "JOBS", self.jobs),
            patch.object(server, "PROCESSES", {}),
            patch.object(server, "_persist_jobs_locked", lambda: None),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def preparation(self, command: list[str], manifest: bool = True) -> dict:
        return {
            "command": command,
            "run_directory": str(self.output),
            "export_folder_path": str(self.output),
            "repository_run_manifest": str(self.manifest) if manifest else "",
            "repository_raw_retention_policy": "keep",
            "expected_analysis_exports": [],
            "qa_matrix_expected": False,
            "software_provenance": CONSOLE,
        }

    def job(self, job_id: str, kind: str, preparation: dict) -> dict:
        self.jobs[job_id] = {
            "id": job_id, "status": "queued", "kind": kind, "logs": [], "preparation": preparation,
            "artifact_baseline": {},
        }
        return self.jobs[job_id]

    def test_the_attempt_exists_before_the_console_starts(self) -> None:
        """The stand-in Console reads the manifest itself and exits 0 only if its attempt is open."""
        script = (
            "import json, os, sys\n"
            f"attempt = json.load(open({str(self.manifest)!r}, encoding='utf-8'))['run_attempts'][-1]\n"
            "print(os.getpid())\n"
            "sys.exit(0 if attempt['job_id'] == 'job1' and attempt['ended_at'] is None else 7)\n"
        )
        preparation = self.preparation([sys.executable, "-u", "-c", script])
        self.job("job1", "run", preparation)
        lines: list[str] = []

        code = server._run_console_for_job("job1", "run", preparation, lines.append)
        [attempt] = self.attempts()

        self.assertEqual(0, code, lines)
        self.assertEqual("exited", attempt["reason"])
        self.assertEqual(0, attempt["exit_code"])
        self.assertEqual(int(lines[0]), attempt["console_pid"])
        self.assertIsNotNone(attempt["console_process_created_at"])
        self.assertEqual(int(lines[0]), self.jobs["job1"]["console_process"]["pid"])
        self.assertEqual({}, server.PROCESSES, "a finished Console leaves nothing to cancel")

    def test_a_production_run_closes_its_attempt_with_the_console_exit_code(self) -> None:
        seen: list[list[dict]] = []

        def console(_preparation, _log, **watch):
            seen.append(self.attempts())
            watch["on_start"](os.getpid())
            return 3

        preparation = self.preparation(["MSDIALCUI.exe"])
        self.job("run1", "run", preparation)
        with patch.object(server, "run_console", console):
            server._run_job("run1", preparation)
        manifest = read_manifest(self.manifest)
        [attempt] = manifest["run_attempts"]

        self.assertIsNone(seen[0][0]["ended_at"], "open while the Console runs")
        self.assertEqual("failed", self.jobs["run1"]["status"])
        self.assertEqual((3, "exited"), (attempt["exit_code"], attempt["reason"]))
        self.assertEqual(os.getpid(), attempt["console_pid"])
        self.assertEqual(1, len(manifest["run_failures"]), "the failure record is written as before")

    def test_a_diagnostic_records_its_attempt_as_tuning(self) -> None:
        directory = self.root / "unit" / "diagnostics" / "tune1"
        directory.mkdir(parents=True)
        preparation = {
            **self.preparation(["MSDIALCUI.exe"]),
            "run_directory": str(directory),
            "diagnostic_run_directory": str(directory),
            "diagnostic_result_file": str(directory / "a.mdpeak"),
        }
        self.job("tune1", "tuning", preparation)
        with patch.object(server, "run_console", lambda _preparation, _log, **_watch: 5):
            server._run_tuning_job("tune1", preparation)
        [attempt] = self.attempts()

        self.assertEqual(("tune1", "tuning", 1), (attempt["job_id"], attempt["kind"], attempt["attempt"]))
        self.assertEqual(5, attempt["exit_code"])

    def test_a_console_that_cannot_start_is_recorded_as_start_failed(self) -> None:
        preparation = self.preparation([str(self.root / "no-such-console" / "MSDIALCUI.exe"), "lcms"])
        self.job("run2", "run", preparation)

        server._run_job("run2", preparation)
        [attempt] = self.attempts()

        self.assertEqual("failed", self.jobs["run2"]["status"])
        self.assertEqual("start_failed", attempt["reason"])
        self.assertIsNone(attempt["exit_code"])
        self.assertIn("FileNotFoundError", attempt["detail"]["error"])

    def test_a_console_the_watch_stops_is_recorded_with_its_reason(self) -> None:
        preparation = self.preparation([sys.executable, "-c", "import time; time.sleep(120)"])
        self.job("run3", "run", preparation)
        with server.JOBS_LOCK:
            server._register_process_locked("run3", "run", timeout_seconds=1)
        started = time.monotonic()

        server._run_job("run3", preparation)
        [attempt] = self.attempts()
        job = self.jobs["run3"]

        self.assertLess(time.monotonic() - started, 60)
        self.assertEqual(-3, job["exit_code"])
        self.assertEqual("timeout", job["stop_reason"])
        self.assertTrue(job["error"].startswith("The MS-DIAL Console ran longer than its 1 s time limit"), job["error"])
        self.assertEqual((-3, "timeout"), (attempt["exit_code"], attempt["reason"]))
        self.assertEqual(1.0, attempt["timeout_seconds"])
        self.assertIn("method", attempt["detail"]["stop"])
        self.assertIn("time limit", read_manifest(self.manifest)["run_failures"][-1]["reason"])

    def test_a_time_limit_past_ten_years_is_refused_with_the_request(self) -> None:
        # What POST /api/agent/run and /api/agent/tuning/run read, before any job is registered.
        with self.assertRaisesRegex(ValueError, "at most ten years"):
            server._console_watch_request({"timeout_seconds": 1e12})

    def test_a_console_that_fails_after_its_start_is_stopped_and_frees_its_unit(self) -> None:
        """The job failed while its Console ran on untracked: no pid recorded, nothing to cancel, and the
        unit open to a second Console. Here the deadline overflows, as 1e12 s once did."""
        preparation = self.preparation([sys.executable, "-c", "import time; time.sleep(120)"])
        self.job("run4", "run", preparation)
        with server.JOBS_LOCK:
            server._register_process_locked("run4", "run", timeout_seconds=1e12)

        # The ten-year ceiling lifted, so the deadline overflows after the Console has started.
        with patch("msdial_app.workflow._WATCH_MAX_SECONDS", float("inf")):
            server._run_job("run4", preparation)
        [attempt] = self.attempts()
        job = self.jobs["run4"]
        pid = job["console_outcome"]["pid"]
        self.addCleanup(_end_leftover, pid)

        self.assertEqual("failed", job["status"])
        self.assertEqual("error", attempt["reason"])
        self.assertIn("OverflowError", attempt["detail"]["error"])
        self.assertIn("stop", attempt["detail"], "how the Console was stopped is recorded")
        deadline = time.monotonic() + 10
        while process_is_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIs(False, process_is_alive(pid), "the Console is stopped, not left running")
        self.assertIsNone(live_run_attempt(self.manifest))

    def test_a_local_analysis_records_nothing(self) -> None:
        before = self.manifest.read_bytes()
        preparation = self.preparation([sys.executable, "-c", "print('local')"], manifest=False)
        self.job("local1", "run", preparation)
        lines: list[str] = []

        code = server._run_console_for_job("local1", "run", preparation, lines.append)

        self.assertEqual(0, code)
        self.assertEqual(["local"], lines)
        self.assertEqual(before, self.manifest.read_bytes())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""One live Console per repository unit, whoever asks and however often.

Two Consoles on one unit write the same intermediates beside the same raw files and the same unit
manifest, and nothing stopped a second from starting. The MCP start tools give up waiting for the backend
after 120 s while the backend carries on, so a caller that retried a start it could not see succeed - the
campaign runner after a timeout, an agent after an error - started another Console on the same unit.

A start for a unit that already has a live run or diagnostic is answered 409 with the live job's id, before
anything is prepared. The unit's run attempts are read too, which is what finds a Console no registry
knows: one orphaned by a backend that stopped, or one another backend is starting. Local analyses name no
unit and are never held. The Console time limits travel with the same request.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import mcp_server, server
    from msdial_app.process_liveness import process_created_at
    from msdial_app.repository_reanalysis import _write_json, live_run_attempt, read_manifest

TEMPLATE = Path(__file__).resolve().parents[1] / "resources" / "msdial_console_param4lipidomics.txt"


def _files(root: Path) -> dict[str, int]:
    return {str(path): path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file()}


class _Unit:
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.jobs: dict = {}
        patches = [
            patch.object(server, "JOBS", self.jobs),
            patch.object(server, "PROCESSES", {}),
            patch.object(server, "CONSOLE_SLOTS", {}),
            patch.object(server, "_persist_jobs_locked", lambda: None),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.children: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for child in self.children:
            child.kill()
            child.wait(timeout=30)
        self.directory.cleanup()

    def unit(self, name: str) -> Path:
        unit = self.root / "analysis" / name
        (unit / "output").mkdir(parents=True, exist_ok=True)
        manifest = unit / "provenance" / "run-manifest.json"
        _write_json(manifest, {"status": "preflight_passed", "project": {"analysis_unit_id": name}})
        return manifest

    def sleeper(self) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.children.append(child)
        return child

    def attempt(self, manifest: Path, **fields) -> None:
        entry = {
            "attempt_id": "a1", "attempt": 1, "job_id": "old-job", "kind": "run", "started_at": "2026-09-30T00:00:00+00:00",
            "ended_at": None, "exit_code": None, "reason": None, "console_pid": None,
            "console_process_created_at": None, "backend": {"pid": 0, "process_created_at": None},
        }
        entry.update(fields)
        _write_json(manifest, {**read_manifest(manifest), "run_attempts": [entry]})


class TheHold(_Unit, unittest.TestCase):
    def live(self, job_id: str, kind: str, manifest: Path, status: str = "running") -> None:
        self.jobs[job_id] = {
            "id": job_id, "kind": kind, "status": status, "logs": [],
            "preparation": {"repository_run_manifest": str(manifest)},
        }

    def test_a_live_run_holds_its_unit_against_a_run_and_a_diagnostic(self) -> None:
        manifest = self.unit("unit-a")
        self.live("run1", "run", manifest)

        for job_id in ("run2", "tune2"):
            with self.assertRaises(server.UnitBusyError) as caught:
                with server._single_console_per_unit(str(manifest), job_id):
                    self.fail("the body must not run")
            self.assertEqual("run1", caught.exception.live_job_id)
            self.assertEqual("unit_busy", caught.exception.payload()["code"])

    def test_the_unit_is_free_once_its_job_is_no_longer_live(self) -> None:
        manifest = self.unit("unit-a")
        for status in ("completed", "failed", "interrupted"):
            self.live("run1", "tuning", manifest, status=status)
            with server._single_console_per_unit(str(manifest), "run2"):
                pass
        self.assertEqual({}, server.CONSOLE_SLOTS, "the hold is released")

    def test_the_same_unit_named_another_way_is_the_same_unit(self) -> None:
        manifest = self.unit("unit-a")
        self.live("run1", "run", manifest)
        other_spelling = str(manifest.parent / ".." / "provenance" / manifest.name)

        with self.assertRaises(server.UnitBusyError):
            with server._single_console_per_unit(other_spelling, "run2"):
                pass

    def test_two_requests_setting_up_one_unit_cannot_both_pass(self) -> None:
        manifest = self.unit("unit-a")

        with server._single_console_per_unit(str(manifest), "first"):
            with self.assertRaises(server.UnitBusyError) as caught:
                with server._single_console_per_unit(str(manifest), "second"):
                    pass
        self.assertEqual("first", caught.exception.live_job_id)

    def test_other_units_and_local_analyses_are_not_held(self) -> None:
        self.live("run1", "run", self.unit("unit-a"))
        self.jobs["dl"] = {"id": "dl", "kind": "repository_download", "status": "running", "preparation": {}}

        with server._single_console_per_unit(str(self.unit("unit-b")), "run2"):
            with server._single_console_per_unit("", "local1"):
                with server._single_console_per_unit(None, "local2"):
                    pass

    def test_an_orphaned_console_in_the_manifest_holds_the_unit(self) -> None:
        """A backend that stopped left its Console running; no registry knows it, the manifest does."""
        manifest = self.unit("unit-a")
        child = self.sleeper()
        self.attempt(manifest, console_pid=child.pid, console_process_created_at=process_created_at(child.pid))

        with self.assertRaises(server.UnitBusyError) as caught:
            with server._single_console_per_unit(str(manifest), "run2"):
                pass
        self.assertEqual("old-job", caught.exception.live_job_id)
        self.assertIn(f"process {child.pid}", str(caught.exception))

        child.kill()
        child.wait(timeout=30)
        with server._single_console_per_unit(str(manifest), "run3"):
            pass

    def test_a_closed_attempt_whose_console_outlived_its_stop_holds_the_unit(self) -> None:
        manifest = self.unit("unit-a")
        child = self.sleeper()
        self.attempt(
            manifest, ended_at="2026-09-30T01:00:00+00:00", reason="timeout", console_pid=child.pid,
            console_process_created_at=process_created_at(child.pid),
        )
        self.assertIsNotNone(live_run_attempt(manifest))

        # Without its creation time a closed attempt cannot be told from a later process with its id.
        self.attempt(manifest, ended_at="2026-09-30T01:00:00+00:00", console_pid=child.pid)
        self.assertIsNone(live_run_attempt(manifest))

    def test_an_attempt_another_live_backend_is_starting_holds_the_unit(self) -> None:
        manifest = self.unit("unit-a")
        other = self.sleeper()
        self.attempt(manifest, backend={"pid": other.pid, "process_created_at": process_created_at(other.pid)})
        with self.assertRaises(server.UnitBusyError) as caught:
            with server._single_console_per_unit(str(manifest), "run2"):
                pass
        self.assertIn("another backend process", str(caught.exception))

        # Its own open attempts are this backend's registry's to answer for.
        self.attempt(manifest, backend={"pid": os.getpid(), "process_created_at": process_created_at()})
        self.assertIsNone(live_run_attempt(manifest, ignore_backend_pid=os.getpid()))

        other.kill()
        other.wait(timeout=30)
        self.attempt(manifest, backend={"pid": other.pid, "process_created_at": process_created_at()})
        self.assertIsNone(live_run_attempt(manifest))

    def test_legacy_manifests_hold_nothing(self) -> None:
        self.assertIsNone(live_run_attempt(self.unit("unit-a")))
        self.assertIsNone(live_run_attempt(self.root / "missing.json"))


class TheRunRoutes(_Unit, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.manifest = self.unit("unit-neg")
        unit = self.manifest.parents[1]
        self.output = unit / "output"
        data = unit / "raw" / "data"
        data.mkdir(parents=True)
        self.sample = data / "sample.mzML"
        self.sample.write_text("", encoding="ascii")
        _write_json(self.manifest, {
            **read_manifest(self.manifest),
            "workspace": str(unit),
            "output_directory": str(self.output),
            "input_candidates": [str(self.sample)],
            "execution_allowed": True,
            "raw_retention_policy": "keep",
        })
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "MSDIALCUI.exe").write_bytes(b"not really a console binary")
        (tools / "lab.lbm2").write_bytes(b"laboratory library")
        self.answers = {
            "project_type": "lcms",
            "ion_mode": "Negative",
            "target_omics": "Lipidomics",
            "parameter_strategy": "default",
            "execute_rt_correction": False,
            "library_strategy": "existing",
            "libraries": {"lbm_path": str(tools / "lab.lbm2")},
            "run_qa": False,
            "generate_materials_methods": False,
            "console_path": str(tools / "MSDIALCUI.exe"),
            "template_path": str(TEMPLATE),
            "output_root": str(self.output),
            "class_assignment_confirmed": True,
            "workflow_overrides": {"repository_run_manifest": str(self.manifest)},
        }
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.thread.start()
        # The job's worker is not run: its job stays queued, which is live.
        worker = patch.object(server, "_run_job", lambda *args: None)
        worker.start()
        self.addCleanup(worker.stop)

    def tearDown(self) -> None:
        self.backend.shutdown()
        self.backend.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def run_request(self, **body) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.backend.server_port}/api/agent/run",
            data=json.dumps(
                {"input_path": str(self.sample.parent), "answers": self.answers, "confirmed": True, **body}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read().decode("utf-8"))
            error.close()
            return error.code, payload

    def test_a_second_start_for_a_live_unit_is_409_and_prepares_nothing(self) -> None:
        status, first = self.run_request()
        self.assertEqual(200, status, first.get("error"))
        before = _files(self.root)

        status, second = self.run_request()

        self.assertEqual(409, status)
        self.assertEqual("unit_busy", second["code"])
        self.assertEqual(first["job_id"], second["live_job_id"])
        self.assertFalse(second["started"])
        self.assertEqual(before, _files(self.root), "nothing was prepared or recorded for the refused start")
        self.assertEqual([first["job_id"]], list(self.jobs))

    def test_the_unit_takes_a_new_run_once_the_last_one_ended(self) -> None:
        _, first = self.run_request()
        self.jobs[first["job_id"]]["status"] = "failed"

        status, again = self.run_request()

        self.assertEqual(200, status, again.get("error"))
        self.assertNotEqual(first["job_id"], again["job_id"])

    def test_the_mcp_tool_names_the_live_job(self) -> None:
        _, first = self.run_request()

        refused = mcp_server.msdial_start_guided_analysis(
            str(self.sample.parent), self.answers, confirmed=True, port=self.backend.server_port
        )

        self.assertFalse(refused["ok"])
        self.assertEqual("unit_busy", refused["reason"])
        self.assertEqual(first["job_id"], refused["live_job_id"])
        self.assertEqual(409, refused["http_status"])

    def test_time_limits_reach_the_job_and_none_are_set_unless_given(self) -> None:
        _, plain = self.run_request()
        self.jobs[plain["job_id"]]["status"] = "completed"
        status, limited = self.run_request(timeout_seconds=7200, idle_timeout_seconds="900")

        self.assertEqual(200, status, limited.get("error"))
        self.assertNotIn("console_watch", self.jobs[plain["job_id"]])
        self.assertIsNone(server.PROCESSES[plain["job_id"]]["timeout_seconds"])
        self.assertEqual({"timeout_seconds": 7200.0, "idle_timeout_seconds": 900.0}, limited["console_watch"])
        self.assertEqual(limited["console_watch"], self.jobs[limited["job_id"]]["console_watch"])
        entry = server.PROCESSES[limited["job_id"]]
        self.assertEqual((7200.0, 900.0), (entry["timeout_seconds"], entry["idle_timeout_seconds"]))

    def test_an_unreadable_time_limit_is_refused_before_anything_starts(self) -> None:
        status, response = self.run_request(timeout_seconds=-5)

        self.assertEqual(400, status)
        self.assertIn("timeout_seconds", response["error"])
        self.assertEqual({}, self.jobs)


class TheMcpToolsPassTheTimeLimits(unittest.TestCase):
    def test_both_start_tools_send_them_and_default_to_none(self) -> None:
        sent: list[tuple[str, dict]] = []

        def request(_method, path, **kwargs):
            sent.append((path, kwargs["body"]))
            return {"started": True}

        with patch.object(mcp_server, "_request_json", request):
            mcp_server.msdial_start_guided_analysis("x", {}, confirmed=True)
            mcp_server.msdial_start_guided_analysis("x", {}, timeout_seconds=3600, idle_timeout_seconds=600)
            mcp_server.msdial_start_peak_count_diagnostic("x", {}, timeout_seconds=900)

        self.assertEqual("/api/agent/run", sent[0][0])
        self.assertEqual((0, 0), (sent[0][1]["timeout_seconds"], sent[0][1]["idle_timeout_seconds"]))
        self.assertEqual((3600, 600), (sent[1][1]["timeout_seconds"], sent[1][1]["idle_timeout_seconds"]))
        self.assertEqual("/api/agent/tuning/run", sent[2][0])
        self.assertEqual(900, sent[2][1]["timeout_seconds"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

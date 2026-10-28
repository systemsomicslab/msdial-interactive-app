"""What verification run V2 (the GLACIER in-silico tier pilot, 2026-10-28) found, fixed in 0.5.49.

1. A blocking MCP call outlived the client's ~60 s wait; the client restarted the server, whose embedded backend
   took the running MS-DIAL Console with it. Every tool now answers within a reply budget, the waiting tools wait a
   bounded time and say "call again", a repeat collects the call still running, and a Console a stopped backend
   left running is recorded on its job and can be stopped.
2. The first plan of a negative-mode run timed out while it read a 2 GB in-silico MSP for the +1.008 peak. The
   census is faster, read in a thread the plan waits for a bounded time ("still being checked" otherwise, never
   accepted unchecked), and kept across restarts by file identity.
3. The annotators route accepted suggestions true on an in-silico row, and searched an unknown threshold preset
   at "standard".
4. A guided run's Materials and Methods said the lipid RT check's summary was not recorded although the Console
   had printed it.
5. MS-DIAL's annotation code 450 marks an accepted in-silico match (the author's decision of 2026-10-28).

Every library and output here is synthetic: a few bytes under a temporary folder.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import __version__, agent_workflow, lab_lcms_metabolomics, mcp_server, server, workflow
    from msdial_app.agent_bridge import create_datamining_handoff
    from msdial_app.agent_workflow import build_guided_plan
    from msdial_app.annotation_codes import (
        ANNOTATION_CODES,
        describe_annotation_code,
        in_silico_codes,
        is_in_silico_match,
        parse_annotation_code,
    )
    from msdial_app.lab_lcms_metabolomics import (
        THRESHOLDS,
        apply_annotators,
        precursor_plus_1008_census,
        precursor_plus_1008_census_within,
    )
    from msdial_app.materials_methods import (
        _in_silico_code_sentence,
        generate_publication_report,
        in_silico_code_evidence,
    )
    from msdial_app.mztab_validation import validate_mztab_file
    from msdial_app.process_liveness import process_created_at, process_is_alive
    from msdial_app.workflow import _annotator_capability_issues, parse_mdpeak, prepare_run

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "resources" / "msdial_console_param4lipidomics.txt"
RT_SUMMARY_LINE = (
    "Lipid RT check (alignment): 12 annotated, 9 evaluable: 4 Confirmed, 5 NotEvaluable, 0 Inconsistent; "
    "Confirmed tags set 4 (already tagged 0, Misannotation 0, suppressed 0, earlier ones withdrawn 0); "
    "rules 1.1, parameter 1a2b3c4d."
)


def _release(event: threading.Event) -> None:
    event.set()


def _own_user_data(case: unittest.TestCase) -> None:
    """Give a test a user data folder of its own: the census and file-identity caches are written there."""
    folder = tempfile.TemporaryDirectory()
    case.addCleanup(folder.cleanup)
    environment = patch.dict(os.environ, {"LOCALAPPDATA": folder.name, "APPDATA": folder.name,
                                          "XDG_DATA_HOME": folder.name, "XDG_CONFIG_HOME": folder.name})
    environment.start()
    case.addCleanup(environment.stop)


# ---------------------------------------------------------------------------------------------------------------
# 1. Every MCP tool answers within its reply budget
# ---------------------------------------------------------------------------------------------------------------


class ReplyBudget(unittest.TestCase):
    def setUp(self) -> None:
        for item in (
            patch.object(mcp_server, "TOOL_REPLY_BUDGET_SECONDS", 2.0),
            patch.object(mcp_server, "JOB_WAIT_LIMIT_SECONDS", 0.3),
        ):
            item.start()
            self.addCleanup(item.stop)
        mcp_server._PENDING_CALLS.clear()
        self.addCleanup(mcp_server._PENDING_CALLS.clear)
        _own_user_data(self)

    def test_every_tool_is_wrapped(self) -> None:
        source = (ROOT / "msdial_app" / "mcp_server.py").read_text(encoding="utf-8")
        tools = re.findall(r"@mcp\.tool\([^)]*\)[ \t]*\r?\n((?:@\w+[ \t]*\r?\n)*)def (\w+)", source)
        self.assertEqual(source.count("@mcp.tool("), len(tools))
        self.assertGreater(len(tools), 40)
        self.assertEqual(
            [], [name for decorators, name in tools if "@_structured_validation_errors" not in decorators.split()]
        )

    def test_a_call_past_its_budget_answers_still_running_and_a_repeat_collects_it(self) -> None:
        release = threading.Event()
        self.addCleanup(_release, release)
        calls: list[str] = []

        def slow(method, path, **options):
            calls.append(path)
            release.wait(30)
            return {"id": "job-1", "status": "completed"}

        with patch.object(mcp_server, "_request_json", side_effect=slow):
            started = time.monotonic()
            first = mcp_server.msdial_interactive_job("job-1")
            self.assertLess(time.monotonic() - started, 10)
            self.assertIs(False, first["finished"])
            self.assertEqual(("still_running", True, True), (first["reason"], first["retryable"], first["call_again"]))
            self.assertEqual("msdial_interactive_job", first["tool"])
            # A repeat while it still runs is answered the same way, and starts nothing.
            self.assertEqual("still_running", mcp_server.msdial_interactive_job("job-1")["reason"])
            release.set()
            collected = mcp_server.msdial_interactive_job("job-1")
        self.assertEqual("completed", collected["status"])
        self.assertIn("collected_from_earlier_call", collected)
        self.assertEqual(1, len(calls))
        # Collected once: the next call is a new one.
        with patch.object(mcp_server, "_request_json", return_value={"id": "job-1", "status": "completed"}):
            again = mcp_server.msdial_interactive_job("job-1")
        self.assertNotIn("collected_from_earlier_call", again)

    def test_a_finished_call_is_kept_for_its_repeat_only_for_a_while(self) -> None:
        release = threading.Event()
        self.addCleanup(_release, release)
        calls: list[str] = []

        def slow(method, path, **options):
            calls.append(path)
            release.wait(30)
            return {"id": "job-6", "status": "completed"}

        with patch.object(mcp_server, "_request_json", side_effect=slow), \
                patch.object(mcp_server, "PENDING_CALL_RETENTION_SECONDS", 0.0):
            self.assertEqual("still_running", mcp_server.msdial_interactive_job("job-6")["reason"])
            release.set()
            deadline = time.monotonic() + 10
            while not all(call.done.is_set() for call in mcp_server._PENDING_CALLS.values()):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            time.sleep(0.05)
            # Past its retention the earlier result is dropped, and the repeat asks again.
            again = mcp_server.msdial_interactive_job("job-6")
        self.assertNotIn("collected_from_earlier_call", again)
        self.assertEqual(2, len(calls))

    def test_a_tool_called_inside_another_tools_call_runs_there(self) -> None:
        # As a reviewer's probe calls the same tool from inside its own call: before, a nested call waited on the
        # very call that waited on it.
        depth: list[int] = []

        def request(method, path, **options):
            depth.append(len(depth))
            if len(depth) == 1:
                return {"inner": mcp_server.msdial_interactive_job("job-7")}
            return {"id": "job-7", "status": "completed"}

        with patch.object(mcp_server, "_request_json", side_effect=request):
            started = time.monotonic()
            outer = mcp_server.msdial_interactive_job("job-7")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual("completed", outer["inner"]["status"])

    def test_an_error_the_call_raised_reaches_the_caller(self) -> None:
        with patch.object(mcp_server, "_request_json", side_effect=TypeError("broken")):
            with self.assertRaisesRegex(TypeError, "broken"):
                mcp_server.msdial_interactive_job("job-2")

    def test_waiting_for_a_job_is_bounded_whatever_timeout_is_asked(self) -> None:
        with patch.object(mcp_server, "_request_json", return_value={"id": "job-3", "status": "running"}):
            started = time.monotonic()
            waited = mcp_server.msdial_interactive_wait_for_completion("job-3", timeout_seconds=3600, poll_seconds=1)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3)
        self.assertEqual((False, True, True), (waited["finished"], waited["still_running"], waited["call_again"]))
        self.assertEqual("running", waited["status"])
        with patch.object(mcp_server, "_request_json", return_value={"id": "job-3", "status": "failed"}):
            self.assertTrue(mcp_server.msdial_interactive_wait_for_completion("job-3")["finished"])

    def test_completion_of_a_running_job_says_call_again_and_does_nothing_else(self) -> None:
        paths: list[str] = []

        def request(method, path, **options):
            paths.append(path)
            return {"id": "job-4", "status": "running"}

        with patch.object(mcp_server, "_request_json", side_effect=request):
            result = mcp_server.msdial_complete_guided_analysis("job-4", timeout_seconds=86400)
        self.assertEqual((False, True, True), (result["finished"], result["still_running"], result["call_again"]))
        self.assertTrue(all(path.startswith("/api/jobs/") for path in paths), paths)

    def test_one_completion_per_job_whatever_else_a_repeat_passes(self) -> None:
        release = threading.Event()
        self.addCleanup(_release, release)
        validations: list[dict] = []

        def request(method, path, **options):
            if path.startswith("/api/jobs/"):
                return {"id": "job-5", "status": "completed", "preparation": {"run_directory": "", "analysis_type": "gcms"}}
            if path == "/api/mztab/validate":
                validations.append(options.get("body") or {})
                release.wait(30)
                return {"validation": {"status": "passed"}}
            return {}

        with patch.object(mcp_server, "_request_json", side_effect=request):
            first = mcp_server.msdial_complete_guided_analysis("job-5", generate_materials_methods=False)
            self.assertEqual("still_running", first["reason"])
            second = mcp_server.msdial_complete_guided_analysis("job-5", poll_seconds=3, generate_materials_methods=False)
            self.assertEqual("still_running", second["reason"])
            self.assertTrue(second["arguments_differ"])
            release.set()
            done = mcp_server.msdial_complete_guided_analysis("job-5", poll_seconds=3, generate_materials_methods=False)
        self.assertTrue(done["success"])
        self.assertEqual({"status": "passed"}, done["mztab_validation"])
        self.assertTrue(done["collected_from_earlier_call"]["arguments_differ"])
        self.assertEqual(1, len(validations))

    def test_a_backend_that_does_not_answer_in_time_is_a_timeout_not_an_os_error(self) -> None:
        class Slow(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                time.sleep(1.5)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                except OSError:
                    pass  # the caller stopped waiting

            do_POST = do_GET  # noqa: N815

            def log_message(self, *args) -> None:
                pass

        backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(backend.server_close)
        self.addCleanup(backend.shutdown)
        port = backend.server_address[1]

        def call() -> dict:
            return mcp_server._request_json("GET", "/api/jobs/x", port=port, timeout=0.3)

        failure = mcp_server._mapped_failures(call)()
        self.assertEqual(("backend_timeout", True), (failure["reason"], failure["retryable"]))
        self.assertIn("may still be working on it", failure["detail"])

        def post() -> dict:
            return mcp_server._request_json("POST", "/api/agent/run", port=port, body={}, timeout=0.3)

        # A start that timed out may have started: it is not offered for a blind retry.
        self.assertFalse(mcp_server._mapped_failures(post)()["retryable"])


# ---------------------------------------------------------------------------------------------------------------
# 1b. The Console is not handed the MCP stdin, and one a stopped backend left running is recorded and stoppable
# ---------------------------------------------------------------------------------------------------------------


class ConsoleOutlivingItsBackend(unittest.TestCase):
    def setUp(self) -> None:
        _own_user_data(self)

    def test_the_console_does_not_inherit_the_backends_stdin(self) -> None:
        seen: dict = {}
        real = subprocess.Popen

        def recording(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        command = [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"]
        lines: list[str] = []
        with patch.object(workflow.subprocess, "Popen", side_effect=recording):
            code = workflow.run_console({"command": command}, lines.append)
        self.assertEqual(0, code)
        self.assertIs(subprocess.DEVNULL, seen.get("stdin"))
        self.assertEqual(["''"], lines)

    def _jobs_file(self, root: Path, jobs: dict) -> Path:
        path = root / "agent-jobs.json"
        path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        return path

    def test_a_running_console_is_recorded_on_its_interrupted_job_and_cancel_stops_it(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            # As run_console starts a watched Console elsewhere, in a process group of its own.
            **({"start_new_session": True} if os.name != "nt" else {}),
        )
        self.addCleanup(child.wait, 60)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        created = process_created_at(child.pid)
        self.assertIsNotNone(created)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = {
                "run-1": {"id": "run-1", "kind": "run", "status": "running", "logs": [],
                          "console_process": {"pid": child.pid, "process_created_at": created}},
                "run-2": {"id": "run-2", "kind": "run", "status": "running", "logs": [],
                          "console_process": {"pid": child.pid, "process_created_at": created - 3600}},
                "download-1": {"id": "download-1", "kind": "repository_download", "status": "running", "logs": []},
            }
            with patch.object(server, "JOBS_FILE", self._jobs_file(root, jobs)):
                loaded = server._load_persisted_jobs()
                self.assertEqual({"interrupted"}, {job["status"] for job in loaded.values()})
                orphan = loaded["run-1"]["orphaned_console"]
                self.assertEqual((child.pid, True), (orphan["pid"], orphan["alive"]))
                self.assertIn(f"process {child.pid}) ran, and that Console was still running", loaded["run-1"]["error"])
                self.assertIn("msdial_cancel_job stops it", loaded["run-1"]["error"])
                # Another process under the same id is not the recorded Console.
                self.assertIs(False, loaded["run-2"]["orphaned_console"]["alive"])
                self.assertNotIn("library download", loaded["run-2"]["error"])
                self.assertNotIn("orphaned_console", loaded["download-1"])
                with patch.object(server, "JOBS", loaded):
                    status, answer = server._cancel_job("run-1", "left running by V2's restart")
                    self.assertEqual(200, int(status))
                    self.assertEqual(("orphaned_console", "stopped"), (answer["reason"], answer["orphaned_console_stop"]["result"]))
                    self.assertIs(False, loaded["run-1"]["orphaned_console"]["alive"])
                    self.assertIs(False, process_is_alive(child.pid, created))
                    # Asked again, the job is a finished one, and nothing is stopped or recorded again.
                    logged = len(loaded["run-1"]["logs"])
                    _, again = server._cancel_job("run-1")
                    self.assertEqual(("finished", False), (again["reason"], again["cancel_requested"]))
                    self.assertEqual(logged, len(loaded["run-1"]["logs"]))
                    # The process that only shares an id is never stopped.
                    _, other = server._cancel_job("run-2")
                    self.assertEqual("finished", other["reason"])
                    self.assertIn("is no longer running", loaded["run-2"]["error"])

    def test_a_console_without_a_recorded_creation_time_is_not_trusted(self) -> None:
        record = workflow.stop_orphaned_console(os.getpid(), None)
        self.assertEqual("not_stopped", record["result"])
        self.assertIs(True, process_is_alive(os.getpid()))
        # Nor one whose creation time cannot be read again (an access-denied process reads as alive, time unknown).
        created = process_created_at()
        with patch("msdial_app.process_liveness.process_created_at", return_value=None):
            self.assertIsNone(workflow.recorded_process_alive(os.getpid(), created))
            self.assertEqual("not_stopped", workflow.stop_orphaned_console(os.getpid(), created)["result"])
        self.assertIs(True, workflow.recorded_process_alive(os.getpid(), created))


# ---------------------------------------------------------------------------------------------------------------
# 2. The +1.008 census: exact, faster, bounded, and kept across restarts
# ---------------------------------------------------------------------------------------------------------------


def _record(name: str, precursor: str, peaks: list[str], *, header: str = "", count: str = "Num Peaks") -> str:
    return f"NAME: {name}\n{header}PRECURSORMZ: {precursor}\n{count}: {len(peaks)}\n" + "\n".join(peaks) + "\n\n"


class PrecursorPlus1008Census(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.addCleanup(lab_lcms_metabolomics._PRECURSOR_PLUS_1008_CENSUS.clear)
        _own_user_data(self)

    def write(self, name: str, text: str | bytes) -> Path:
        path = self.root / name
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return path

    def test_every_way_a_record_may_be_written_is_read_as_before(self) -> None:
        text = (
            _record("plain carrying", "300.1", ["150.05\t10", "301.108\t5"])
            + _record("plain clean", "300.1", ["150.05\t10", "302.5\t5"])
            # Its peak's integer part appears only as an intensity: searched, then read, and clean.
            + _record("intensity looks like it", "300.1", ["150.05\t301", "200\t5"])
            + _record("exponent", "300.1", ["3.01108e2 5"])
            + _record("leading zeros and pairs", "300.1", ["150.05 10; 0301.109 5;"])
            + _record("leading separator", "300.1", [";301.108 5", ",301.108 5"])
            + "  NAME: indented\nPRECURSORMZ: 100\nNum Peaks: 1\n101.008 1\n\n"
            + _record("lower-case fields", "200", ["201.0085 4"], count="num_peaks").replace("PRECURSORMZ", "precursor_mz")
            + _record("no precursor", "abc", ["101.008 1"])
            + "#NAME: a comment\n"
            + _record("unicode name é", "400", ["401.008 9"], header="COMMENT: 　x\n")
            # Where a decimal's rounding to a float crosses an integer boundary (a reviewer's counterexample).
            + _record("huge", "17179881527.9945", ["17179881528.999999 10"])
        )
        # As the census read it before 0.5.49 (its line-by-line reading gives these counts for this text).
        expected = {"records": 11, "with_precursor_plus_1008_peak": 8}
        for variant, data in (
            ("lf", text.encode("utf-8")),
            ("crlf with bom", b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8")),
            ("cr", text.replace("\n", "\r").encode("utf-8")),
        ):
            for block in (7, 64, 1 << 20):
                with self.subTest(variant=variant, block=block), \
                        patch.object(lab_lcms_metabolomics, "_CENSUS_READ_BYTES", block):
                    lab_lcms_metabolomics._PRECURSOR_PLUS_1008_CENSUS.clear()
                    path = self.write(f"{variant.replace(' ', '_')}_{block}.msp", data)
                    self.assertEqual(expected, precursor_plus_1008_census(path))

    def test_a_large_file_is_kept_across_a_restart_and_found_again_when_copied(self) -> None:
        text = _record("A", "300.1", ["150.05\t10", "301.108\t5"]) + _record("B", "200", ["100 1"])
        original = self.write("Synthetic-InSilico_neg.msp", text)
        copy = self.write("copy-of-it.msp", text)
        expected = {"records": 2, "with_precursor_plus_1008_peak": 1}
        with patch.object(workflow, "IDENTITY_CACHE_MIN_BYTES", 1):
            self.assertEqual(expected, precursor_plus_1008_census(original))
            kept = json.loads((workflow.user_data_directory() / lab_lcms_metabolomics.PRECURSOR_PLUS_1008_CENSUS_CACHE)
                              .read_text(encoding="utf-8"))
            entry = next(value for key, value in kept["entries"].items() if key.startswith(str(original.resolve()).casefold()))
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), entry["sha256"])
            # A restarted backend: nothing in memory, and the file is not read again.
            lab_lcms_metabolomics._PRECURSOR_PLUS_1008_CENSUS.clear()
            with patch.object(lab_lcms_metabolomics, "_read_precursor_plus_1008", side_effect=AssertionError("read")):
                self.assertEqual(expected, precursor_plus_1008_census(original))
                # A copy is found by the digest file_identity already holds for it, never by one taken to look.
                workflow.file_identity(copy)
                self.assertEqual(expected, precursor_plus_1008_census(copy))
            # A census of another version of the reading is read again.
            lab_lcms_metabolomics._PRECURSOR_PLUS_1008_CENSUS.clear()
            reads: list[Path] = []
            real_read = lab_lcms_metabolomics._read_precursor_plus_1008

            def counted(target, progress):
                reads.append(target)
                return real_read(target, progress)

            with patch.object(lab_lcms_metabolomics, "PRECURSOR_PLUS_1008_CENSUS_VERSION", 99), \
                    patch.object(lab_lcms_metabolomics, "_read_precursor_plus_1008", side_effect=counted):
                self.assertEqual(expected, precursor_plus_1008_census(original))
            self.assertEqual([original.resolve()], reads)

    def test_a_small_file_is_kept_for_the_process_only(self) -> None:
        path = self.write("small.msp", _record("A", "300.1", ["301.108 5"]))
        self.assertEqual(1, precursor_plus_1008_census(path)["with_precursor_plus_1008_peak"])
        cache = workflow.user_data_directory() / lab_lcms_metabolomics.PRECURSOR_PLUS_1008_CENSUS_CACHE
        self.assertFalse(cache.exists())
        lab_lcms_metabolomics._PRECURSOR_PLUS_1008_CENSUS.clear()
        with patch.object(lab_lcms_metabolomics, "_read_precursor_plus_1008", side_effect=AssertionError("read")):
            with self.assertRaisesRegex(AssertionError, "read"):
                precursor_plus_1008_census(path)

    def test_a_read_of_an_earlier_version_of_the_file_is_abandoned(self) -> None:
        path = self.write("changing.msp", _record("A", "300.1", ["150.05 10"]))
        release = threading.Event()
        self.addCleanup(_release, release)
        started: list[dict] = []

        def held(target, progress=None):
            started.append(progress)
            if len(started) == 1:
                release.wait(30)
            return {"records": 1, "with_precursor_plus_1008_peak": 0}

        with patch.object(lab_lcms_metabolomics, "precursor_plus_1008_census", side_effect=held):
            self.assertEqual("reading", precursor_plus_1008_census_within(path, 0.1)["state"])
            path.write_text(_record("A", "300.1", ["150.05 10", "160 1"]), encoding="utf-8")
            os.utime(path, (time.time() + 10, time.time() + 10))
            found = precursor_plus_1008_census_within(path, 5)
            self.assertEqual("counted", found["state"])
            self.assertTrue(started[0].get("abandoned"))
            release.set()
        with self.assertRaises(InterruptedError):
            lab_lcms_metabolomics._read_precursor_plus_1008(path, {"abandoned": 1})

    def test_a_plan_waits_for_the_census_a_bounded_time_and_never_accepts_an_unread_file(self) -> None:
        path = self.write("Synthetic-GLACIER_neg_allCE.msp", _record("A", "300.1", ["150.05 10"]))
        release = threading.Event()
        self.addCleanup(_release, release)
        real = lab_lcms_metabolomics.precursor_plus_1008_census

        def slow(target, progress=None):
            if progress is not None:
                progress["bytes_read"] = 10
            release.wait(30)
            return real(target)

        step = ["precursor_plus_1008_peak_removed"]
        with patch.object(lab_lcms_metabolomics, "precursor_plus_1008_census", side_effect=slow), \
                patch.object(agent_workflow, "CENSUS_PLAN_WAIT_SECONDS", 0.2):
            first = agent_workflow._in_silico_preprocessing_problems(path, step, "Negative")
            self.assertEqual(1, len(first))
            self.assertIn(f"{path.name} is still being checked for the peak at the precursor m/z + 1.008", first[0])
            self.assertIn("Plan again in a minute", first[0])
            rows = {"ion_mode": "Negative", "libraries": {"annotators": [
                {"kind": "msp", "label": "probe", "path": str(path), "library_kind": "predicted", "thresholds": "in_silico"}
            ]}}
            self.assertIn("is still being checked", agent_workflow._predicted_annotator_problems(rows)[0])
            release.set()
            deadline = time.monotonic() + 10
            while agent_workflow._in_silico_preprocessing_problems(path, step, "Negative") and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual([], agent_workflow._in_silico_preprocessing_problems(path, step, "Negative"))
            self.assertEqual([], agent_workflow._predicted_annotator_problems(rows))

    def test_a_census_that_fails_raises_to_its_caller(self) -> None:
        path = self.write("unreadable.msp", "NAME: A\n")
        with patch.object(lab_lcms_metabolomics, "precursor_plus_1008_census",
                          side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(PermissionError):
                precursor_plus_1008_census_within(path, 5)
        # Whatever it failed with, it is an OSError to the plan, so the plan refuses the file rather than crash.
        with patch.object(lab_lcms_metabolomics, "precursor_plus_1008_census", side_effect=MemoryError("too big")):
            with self.assertRaisesRegex(OSError, "MemoryError: too big"):
                precursor_plus_1008_census_within(path, 5)


# ---------------------------------------------------------------------------------------------------------------
# 3. The annotators route
# ---------------------------------------------------------------------------------------------------------------


class AnnotatorsRoute(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        _own_user_data(self)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "sample.mzML").write_text("", encoding="ascii")
        self.acquired = self.root / "synthetic-acquired.msp"
        self.acquired.write_text("NAME: A\n", encoding="ascii")
        self.predicted = self.root / "Synthetic-InSilico_pos.msp"
        self.predicted.write_text("NAME: B\nPRECURSORMZ: 100\n", encoding="ascii")

    def row(self, **changes) -> dict:
        return {"kind": "msp", "annotator_id": "probe", "label": "probe", "path": str(self.predicted), "priority": 1,
                "thresholds": "in_silico", "retention_time": "none", "library_kind": "predicted", **changes}

    def plan(self, rows: list[dict]) -> dict:
        answers = {
            "project_type": "lcms", "ion_mode": "Positive", "target_omics": "Metabolomics",
            "parameter_strategy": "default", "execute_rt_correction": False, "library_strategy": "annotators",
            "libraries": {"annotators": rows}, "run_qa": False, "generate_materials_methods": False,
            "class_assignment_confirmed": True, "template_path": str(TEMPLATE), "output_root": str(self.root / "out"),
        }
        with patch("msdial_app.agent_workflow.catalog_status", side_effect=AssertionError("catalog consulted")), \
                patch("msdial_app.workflow.subprocess.run", side_effect=OSError("not a Console")):
            return build_guided_plan(str(self.data), answers)

    def test_an_in_silico_row_that_suggests_is_refused(self) -> None:
        plan = self.plan([self.row(suggestions=True)])
        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(
            any("libraries.annotators (probe): an in-silico (library_kind predicted) row does not suggest" in item
                for item in plan["blockers"]), plan["blockers"])
        for allowed in (False, "false", None):
            with self.subTest(suggestions=allowed):
                row = self.row() if allowed is None else self.row(suggestions=allowed)
                blockers = self.plan([row])["blockers"]
                self.assertFalse(any("does not suggest" in item for item in blockers), blockers)
        # An acquired row may suggest, as it always could.
        blockers = self.plan([self.row(library_kind="acquired", path=str(self.acquired), thresholds="standard",
                                       suggestions=True)])["blockers"]
        self.assertFalse(any("does not suggest" in item for item in blockers), blockers)

    def test_every_spelling_the_console_reads_as_predicted_is_an_in_silico_row(self) -> None:
        for spelling in ("in_silico", "InSilico", "Predicted"):
            with self.subTest(library_kind=spelling):
                blockers = self.plan([self.row(library_kind=spelling, suggestions=True)])["blockers"]
                self.assertTrue(any("does not suggest" in item for item in blockers), blockers)
                state: dict = {}
                apply_annotators(state, [self.row(library_kind=spelling)])
                self.assertIs(False, state["msp_annotators"][0]["suggestions"])

    def test_an_lbm_row_in_any_case_is_the_lbm2(self) -> None:
        lbm = self.root / "synthetic.lbm2"
        lbm.write_bytes(b"synthetic")
        plan = self.plan([{"kind": "LBM", "path": str(lbm), "priority": 2}, self.row(suggestions=False)])
        self.assertFalse(any("thresholds" in item for item in plan["blockers"]), plan["blockers"])
        state: dict = {}
        apply_annotators(state, [{"kind": "LBM", "path": str(lbm), "priority": 2}])
        self.assertEqual((str(lbm), []), (state["lbm_path"], state["msp_annotators"]))

    def test_an_in_silico_row_that_leaves_suggestions_unset_is_written_with_them_off(self) -> None:
        state: dict = {}
        apply_annotators(state, [self.row(), self.row(annotator_id="acq", library_kind="acquired", thresholds="strict")])
        self.assertEqual([False, None], [row.get("suggestions") for row in state["msp_annotators"]])
        state = {}
        apply_annotators(state, [self.row(suggestions="true")])
        self.assertIs(True, state["msp_annotators"][0]["suggestions"])

    def test_a_state_with_a_suggesting_in_silico_row_is_refused_whatever_built_it(self) -> None:
        rows = [{"annotator_id": "ins", "msp_file_path": str(self.predicted), "library_kind": "predicted",
                 "target_omics": "Metabolomics", "suggestions": True}]
        with patch("msdial_app.workflow.subprocess.run", side_effect=OSError("not a Console")):
            issues = [item["message"] for item in _annotator_capability_issues(
                {"project_type": "lcms", "target_omics": "Metabolomics", "msp_annotators": rows}, "lcms")]
        self.assertIn(
            "MSP annotator(s) ins search predicted (in-silico) spectra with annotation suggestions on; an in-silico "
            "row does not suggest. Set their suggestions to false.", issues)

    def test_an_unknown_or_missing_threshold_preset_is_refused_not_searched_as_standard(self) -> None:
        for thresholds, said in (("permissive", "thresholds 'permissive' is"), ("", "thresholds is not given, and"),
                                 (None, "thresholds is not given, and")):
            with self.subTest(thresholds=thresholds):
                plan = self.plan([self.row(thresholds=thresholds, library_kind="acquired", path=str(self.acquired))])
                self.assertFalse(plan["ready_to_prepare"])
                self.assertIsNone(plan["workflow"])
                self.assertIn(
                    f"libraries.annotators (probe): {said} none of {', '.join(THRESHOLDS)}. Name one; a row is not "
                    "searched at settings nobody chose.", plan["blockers"])
        plan = self.plan([self.row(kind="text", path=str(self.acquired))])
        self.assertIn("libraries.annotators (probe): kind 'text' is neither msp nor lbm.", plan["blockers"])
        with self.assertRaisesRegex(ValueError, "thresholds 'permissive' is none of"):
            apply_annotators({}, [self.row(thresholds="permissive")])
        # Every known preset is searched at its own values.
        for preset, values in THRESHOLDS.items():
            state: dict = {}
            apply_annotators(state, [self.row(thresholds=preset, suggestions=False)])
            self.assertEqual(values, {key: state["msp_annotators"][0][key] for key in values})


# ---------------------------------------------------------------------------------------------------------------
# 4. The lipid RT check's summary in a guided run's Methods
# ---------------------------------------------------------------------------------------------------------------


class GuidedRunLipidRtSummary(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        _own_user_data(self)
        data = self.root / "data"
        data.mkdir()
        (data / "sample.mzML").write_text("", encoding="ascii")
        # A file carrying the Console assembly's lipid RT check markers as .NET stores them; it runs nothing.
        console = self.root / "MSDIALCUI.exe"
        console.write_bytes(b"".join(marker.encode("utf-16-le") for marker in workflow.LIPID_RT_CHECK_CONSOLE_MARKERS))
        answers = {
            "console_path": str(console),
            "project_type": "lcms", "ion_mode": "Positive", "target_omics": "Lipidomics",
            "parameter_strategy": "default", "execute_rt_correction": False, "library_strategy": "none",
            "lipid_rt_check": {"mode": "Auto", "operating_point": "HighPrecision", "chromatography": "Auto"},
            "run_qa": False, "generate_materials_methods": True, "class_assignment_confirmed": True,
            "template_path": str(TEMPLATE), "output_root": str(self.root / "out"),
        }
        with patch("msdial_app.agent_workflow.catalog_status", side_effect=AssertionError("catalog consulted")), \
                patch("msdial_app.workflow.subprocess.run", side_effect=OSError("not a Console")):
            plan = build_guided_plan(str(data), answers)
            self.assertIsNotNone(plan["workflow"], plan["blockers"])
            self.prepared = prepare_run(plan["workflow"])
        review = Path(self.prepared["run_directory"]) / "AlignResult-2026_lipidrt_review.tsv"
        review.write_text("ID\tName\n", encoding="utf-8")
        method_time = Path(self.prepared["method_file"]).stat().st_mtime
        os.utime(review, (method_time + 5, method_time + 5))

    def post(self, job: dict) -> str:
        jobs = {"job-rt": job}
        for item in (patch.object(server, "JOBS", jobs), patch.object(server, "_persist_jobs_locked", lambda: None)):
            item.start()
            self.addCleanup(item.stop)
        backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{backend.server_address[1]}/api/publication/report",
                data=json.dumps({"job_id": "job-rt", "run_qa": False, "use_saved_run": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))["report"]["methods_text"]
        finally:
            backend.shutdown()
            backend.server_close()
            thread.join(timeout=5)

    def job(self, **extra) -> dict:
        return {"id": "job-rt", "kind": "run", "status": "completed", "exit_code": 0, "logs": [],
                "preparation": {"run_directory": self.prepared["run_directory"], "analysis_type": "lcms"},
                "artifacts": {"qa": [], "mztab": [], "msdial": []}, **extra}

    def test_the_methods_read_the_summary_the_job_recorded(self) -> None:
        finalisation = {"lipid_rt_check": {"ran": True, "review_files": [], "summary": RT_SUMMARY_LINE.split(": ", 1)[1],
                                           "rule_version": "1.1", "parameter_hash": "1a2b3c4d"}}
        text = self.post(self.job(console_run_finalisation=finalisation))
        self.assertIn("an empty list means the check found nothing to review", text)
        self.assertIn("(rules 1.1)", text)
        self.assertNotIn("summary of the check was not recorded", text)

    def test_a_job_finalised_without_it_is_read_for_the_consoles_line(self) -> None:
        text = self.post(self.job(logs=["Loading libraries", RT_SUMMARY_LINE, "Exporting"]))
        self.assertIn("an empty list means the check found nothing to review", text)
        self.assertNotIn("summary of the check was not recorded", text)

    def test_without_either_it_still_says_it_was_not_recorded(self) -> None:
        text = self.post(self.job())
        self.assertIn("summary of the check was not recorded", text)

    def test_a_report_for_another_directory_or_job_takes_nothing_from_the_job(self) -> None:
        job = self.job(logs=[RT_SUMMARY_LINE])
        self.assertEqual("1.1", server._job_lipid_rt_check(job, self.prepared["run_directory"])["rule_version"])
        self.assertIsNone(server._job_lipid_rt_check(job, str(self.root / "elsewhere")))
        self.assertIsNone(server._job_lipid_rt_check({**job, "kind": "tuning"}, self.prepared["run_directory"]))


# ---------------------------------------------------------------------------------------------------------------
# 5. Annotation code 450
# ---------------------------------------------------------------------------------------------------------------

MZTAB = (
    "MTD\tmzTab-version\t2.0.0-M\n"
    "MTD\tsmall_molecule-identification_reliability\t[MS, MS:1003032, compound identification confidence code in MS-DIAL, ]\n"
    "SMH\tSML_ID\tchemical_name\treliability\topt_global_evidence_source\n"
    "SML\t1\tA\t{insilico}\tInSilico\n"
    "SML\t2\tB\t430\tReferenceSpectrum\n"
    "SML\t3\tC\t530\tPrecursorOnly\n"
    "SML\t4\tnull\t999\tnull\n"
)


class AnnotationCode450(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        _own_user_data(self)

    def test_the_code_table_names_450_and_reads_codes_as_written(self) -> None:
        self.assertEqual("MS/MS matched to an in-silico (predicted) reference spectrum", ANNOTATION_CODES[450])
        self.assertEqual("EI-MS matched", ANNOTATION_CODES[440])
        self.assertEqual(ANNOTATION_CODES[450], describe_annotation_code("450"))
        self.assertEqual("", describe_annotation_code("777"))
        self.assertEqual([450, 430, None, None, None], [parse_annotation_code(value) for value in ("450", "430.0", "null", "", "4.5")])
        # The evidence source decides where it is given: an old Console's 430 in-silico match is one.
        self.assertTrue(is_in_silico_match(430, "InSilico"))
        self.assertFalse(is_in_silico_match(450, "ReferenceSpectrum"))
        self.assertTrue(is_in_silico_match(450, None))
        self.assertTrue(is_in_silico_match(450, "null"))
        self.assertEqual({"codes": {"430": 1, "450": 2}, "with_code_450_not_in_silico": 1, "unknown_codes": {"777": 1}},
                         in_silico_codes([(430, "InSilico"), (450, "InSilico"), (450, ""), (450, "RuleBased"),
                                          (777, "null"), (430, "ReferenceSpectrum")]))

    def test_mztab_validation_records_the_codes_and_never_fails_on_them(self) -> None:
        for code, notes in (("450", 0), ("430", 1)):
            with self.subTest(code=code):
                path = self.root / f"AlignResult-{code}.mzTab"
                path.write_text(MZTAB.format(insilico=code), encoding="utf-8")
                result = validate_mztab_file(path)
                self.assertEqual("passed", result["status"], result)
                codes = result["annotation_codes"]
                self.assertEqual({code: 1}, codes["in_silico_match_codes"])
                self.assertEqual(notes, len(codes["notes"]))
                if notes:
                    self.assertIn("predates the in-silico code", codes["notes"][0])
                    self.assertIn("not identifications", codes["notes"][0])

    def test_an_mdpeak_counts_codes_and_never_an_in_silico_match_as_an_identification(self) -> None:
        header = ["Peak ID", "Name", "Height", "Comment", "Annotation tag (VS1.0)", "Simple dot product",
                  "Weighted dot product", "Reverse dot product", "Evidence source"]
        rows = [
            ["0", "acquired", "10", "Annotation method: msp_high_quality", "430", "0.9", "0.9", "0.9", "ReferenceSpectrum"],
            ["1", "lipid", "10", "", "410", "0.9", "0.9", "0.9", "RuleBased"],
            ["2", "predicted new", "10", "Annotation method: msp_in_silico", "450", "0.6", "0.6", "0.6", "InSilico"],
            ["3", "predicted old", "10", "Annotation method: msp_in_silico", "430", "0.6", "0.6", "0.6", "InSilico"],
            ["4", "coded in silico", "10", "", "450", "0.6", "0.6", "0.6", "ReferenceSpectrum"],
            ["5", "Unknown", "10", "", "999", "null", "null", "null", "null"],
        ]
        path = self.root / "sample.mdpeak"
        path.write_text("\n".join("\t".join(row) for row in [header, *rows]) + "\n", encoding="utf-8")
        parsed = parse_mdpeak(path)
        self.assertEqual(2, parsed["identification_count"])
        self.assertEqual(2, parsed["in_silico_count"])
        self.assertEqual({"410": 1, "430": 2, "450": 2}, parsed["annotation_code_counts"])
        self.assertEqual({"430": 1, "450": 1}, parsed["in_silico_annotation_codes"])

    def test_the_methods_state_the_code_the_run_gave_its_in_silico_matches(self) -> None:
        workflow_state = {"msp_annotators": [{"msp_file_path": "x.msp", "library_kind": "predicted"}]}
        self.assertEqual({}, in_silico_code_evidence(self.root, {"msp_annotators": [{"msp_file_path": "x.msp"}]}))
        none = _in_silico_code_sentence(in_silico_code_evidence(self.root, workflow_state))
        self.assertIn("An MS-DIAL Console that has the in-silico annotation code gives an accepted in-silico match "
                      "the code 450", none)
        self.assertIn("one that predates it gives 430", none)
        (self.root / "AlignResult-1.mzTab").write_text(MZTAB.format(insilico="450"), encoding="utf-8")
        new = _in_silico_code_sentence(in_silico_code_evidence(self.root, workflow_state))
        self.assertIn("Each in-silico match carries the MS-DIAL annotation code 450 (MS/MS matched to an in-silico "
                      "(predicted) reference spectrum)", new)
        self.assertIn("is not counted as an identification", new)
        (self.root / "AlignResult-1.mzTab").write_text(MZTAB.format(insilico="430"), encoding="utf-8")
        old = _in_silico_code_sentence(in_silico_code_evidence(self.root, workflow_state))
        self.assertIn("predates the in-silico annotation code 450", old)
        self.assertIn("gave these matches the code 430", old)

    def test_the_handoff_says_which_code_marks_an_in_silico_match(self) -> None:
        directory = self.root / "run"
        directory.mkdir()
        (directory / "workflow-settings.json").write_text(json.dumps({
            "project_type": "lcms",
            "msp_annotators": [{"annotator_id": "msp_in_silico", "msp_file_path": "Synthetic-InSilico_neg.msp",
                                "library_kind": "predicted", "evidence_tier": "In-silico MSP"}],
        }), encoding="utf-8")
        annotation = create_datamining_handoff(run_directory=str(directory), write_file=False)["annotation"]
        self.assertIn("MS-DIAL annotation code 450", annotation["note"])
        self.assertIn("select by the evidence source, not the code", annotation["note"])
        self.assertEqual({"code": 450, "meaning": ANNOTATION_CODES[450]}, annotation["in_silico_annotation_code"])


if __name__ == "__main__":
    unittest.main()

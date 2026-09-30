"""The Console runs under a watch: a time limit, an idle limit and a cancel flag, none of them by default.

run_console read the Console's output until the pipe closed and then waited without a timeout. One hung
Console held its job, and every unit queued behind it, for ever, and nothing could stop it: a caller that
stopped polling only stopped looking. These pin the watch with a stand-in Console - a Python process that
talks, goes quiet, writes files or starts a child of its own - so what is measured is the real process
handling: the exit codes (-3 for a limit, -4 for a cancel), the whole process tree stopped, and the
unwatched path left exactly as it was.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app.process_liveness import process_is_alive
from msdial_app.workflow import (
    CONSOLE_EXIT_CANCELLED,
    CONSOLE_EXIT_TIMEOUT,
    CONSOLE_WATCHDOG_PREFIX,
    console_watch_seconds,
    run_console,
)


def _gone(pid: int, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if process_is_alive(pid) is False:
            return True
        time.sleep(0.1)
    return False


class _Console:
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.lines: list[str] = []
        self.leftover: list[int] = []

    def tearDown(self) -> None:
        # Only a failed test leaves a stand-in running; it is not left behind for the next one.
        for pid in self.leftover:
            if process_is_alive(pid):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)
                else:
                    os.kill(pid, signal.SIGKILL)
        self.directory.cleanup()

    def preparation(self, script: str) -> dict:
        return {"command": [sys.executable, "-u", "-c", script], "run_directory": str(self.output)}

    def console(self, script: str, **watch) -> tuple[int, dict, float]:
        outcome: dict = {}
        started = time.monotonic()
        code = run_console(self.preparation(script), self.lines.append, outcome=outcome, **watch)
        if outcome.get("pid"):
            self.leftover.append(outcome["pid"])
        return code, outcome, time.monotonic() - started


class WithoutAWatch(_Console, unittest.TestCase):
    def test_the_console_runs_to_its_own_exit_code_as_before(self) -> None:
        code, outcome, _ = self.console("print('one'); print('two'); raise SystemExit(3)")

        self.assertEqual(3, code)
        self.assertEqual(["one", "two"], self.lines)
        self.assertEqual("exited", outcome["reason"])
        self.assertIsNone(outcome["timeout_seconds"])

    def test_a_time_limit_of_zero_or_nothing_means_none(self) -> None:
        for value in (None, "", 0, "0", 0.0):
            self.assertIsNone(console_watch_seconds(value))
        self.assertEqual(12.5, console_watch_seconds("12.5"))
        for value in (-1, "soon", True, float("nan")):
            with self.assertRaises(ValueError):
                console_watch_seconds(value, "timeout_seconds")

    def test_a_time_limit_past_ten_years_is_refused_before_anything_starts(self) -> None:
        """From about 2.6e11 s the deadline is past the last date Python can represent. It was accepted,
        and computing the deadline failed with the Console already running and nobody tracking it."""
        self.assertEqual(3e8, console_watch_seconds(3e8))
        for value in (1e12, "4e8"):
            with self.assertRaisesRegex(ValueError, "at most ten years"):
                console_watch_seconds(value, "timeout_seconds")

        with patch("msdial_app.workflow.subprocess.Popen", side_effect=AssertionError("no Console")):
            for watch in ({"timeout_seconds": 1e12}, {"idle_timeout_seconds": 1e12}):
                with self.assertRaisesRegex(ValueError, "at most ten years"):
                    run_console(self.preparation("pass"), self.lines.append, **watch)


class TheTimeLimit(_Console, unittest.TestCase):
    def test_a_console_past_its_time_limit_is_stopped_with_minus_3(self) -> None:
        code, outcome, elapsed = self.console("import time; print('started'); time.sleep(120)", timeout_seconds=1)

        self.assertEqual(CONSOLE_EXIT_TIMEOUT, code)
        self.assertEqual("timeout", outcome["reason"])
        self.assertLess(elapsed, 30, "the watch stops it, it does not wait for it")
        self.assertTrue(_gone(outcome["pid"]), "the Console process is stopped, not abandoned")
        self.assertTrue(self.lines[-1].startswith(CONSOLE_WATCHDOG_PREFIX), self.lines)
        self.assertIn("1 s time limit", self.lines[-1])
        self.assertIn("deadline_at", outcome)

    def test_the_whole_process_tree_is_stopped(self) -> None:
        """A dotnet launcher, or anything the Console started, goes with it. It also holds the pipe."""
        marker = self.root / "grandchild.pid"
        script = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
            "print('spawned', flush=True)\n"
            "time.sleep(120)\n"
        )
        # Stopped once the grandchild exists, however long the two interpreters take to start.
        cancel = threading.Event()

        def on_line(line: str) -> None:
            self.lines.append(line)
            if line == "spawned":
                cancel.set()

        outcome: dict = {}
        started = time.monotonic()
        code = run_console(
            self.preparation(script), on_line, timeout_seconds=120, cancel_event=cancel, outcome=outcome
        )
        elapsed = time.monotonic() - started
        self.leftover.append(outcome["pid"])
        grandchild = int(marker.read_text())
        self.leftover.append(grandchild)

        self.assertEqual(CONSOLE_EXIT_CANCELLED, code, self.lines)
        self.assertLess(elapsed, 45, "the grandchild held the pipe, and the watch did not wait on it")
        self.assertTrue(_gone(outcome["pid"]))
        self.assertTrue(_gone(grandchild), "the Console's own child is stopped with it")


class TheIdleLimit(_Console, unittest.TestCase):
    # The idle clock starts with the process, so each limit leaves the interpreter time to start.
    def test_a_console_that_goes_quiet_is_stopped_but_not_while_it_talks(self) -> None:
        script = (
            "import time\n"
            "for index in range(15):\n"
            "    print('working', index, flush=True); time.sleep(0.2)\n"
            "time.sleep(120)\n"
        )
        code, outcome, elapsed = self.console(script, idle_timeout_seconds=2)

        self.assertEqual(CONSOLE_EXIT_TIMEOUT, code)
        self.assertEqual("idle_timeout", outcome["reason"])
        self.assertGreaterEqual(elapsed, 3.0, "fifteen lines 0.2 s apart kept it alive")
        self.assertEqual(15, sum(1 for line in self.lines if line.startswith("working")))
        self.assertIn("grew for 2 s", self.lines[-1])
        self.assertTrue(_gone(outcome["pid"]))

    def test_growing_output_files_count_as_activity(self) -> None:
        """MS-DIAL can be silent for long stretches while it writes its per-file results."""
        target = self.output / "sample.mdpeak"
        script = (
            "import time\n"
            "for index in range(20):\n"
            f"    open({str(target)!r}, 'a').write('row\\n'); time.sleep(0.2)\n"
        )
        code, outcome, elapsed = self.console(script, idle_timeout_seconds=2)

        self.assertEqual(0, code, self.lines)
        self.assertEqual("exited", outcome["reason"])
        self.assertGreaterEqual(elapsed, 4.0, "four seconds of writing, twice the idle limit")
        self.assertEqual([], self.lines)


class TheCancelFlag(_Console, unittest.TestCase):
    def test_a_cancel_stops_the_console_with_minus_4(self) -> None:
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()

        code, outcome, elapsed = self.console("import time; time.sleep(120)", cancel_event=cancel)

        self.assertEqual(CONSOLE_EXIT_CANCELLED, code)
        self.assertEqual("cancelled", outcome["reason"])
        self.assertLess(elapsed, 30)
        self.assertTrue(_gone(outcome["pid"]))
        self.assertTrue(self.lines[-1].startswith(CONSOLE_WATCHDOG_PREFIX + "the job was cancelled"))

    def test_a_cancel_before_the_start_starts_nothing(self) -> None:
        cancel = threading.Event()
        cancel.set()
        started: list[int] = []

        with patch("msdial_app.workflow.subprocess.Popen", side_effect=AssertionError("no Console")):
            code = run_console(
                self.preparation("pass"), self.lines.append, cancel_event=cancel, on_start=started.append
            )

        self.assertEqual(CONSOLE_EXIT_CANCELLED, code)
        self.assertEqual([], started)
        self.assertIn("before the MS-DIAL Console started", self.lines[-1])

    def test_a_cancel_only_watch_leaves_a_normal_run_alone(self) -> None:
        code, outcome, _ = self.console("print('done')", cancel_event=threading.Event())

        self.assertEqual(0, code)
        self.assertEqual(["done"], self.lines)
        self.assertEqual("exited", outcome["reason"])


class TheStartCallback(_Console, unittest.TestCase):
    def test_on_start_receives_the_console_process_id(self) -> None:
        started: list[int] = []

        code = run_console(
            self.preparation("import os; print(os.getpid())"), self.lines.append, on_start=started.append
        )

        self.assertEqual(0, code)
        self.assertEqual([int(self.lines[0])], started)

    def test_a_console_its_caller_could_not_register_does_not_run_on(self) -> None:
        started: list[int] = []

        def refuse(pid: int) -> None:
            started.append(pid)
            raise RuntimeError("could not register")

        with self.assertRaisesRegex(RuntimeError, "could not register"):
            run_console(
                self.preparation("import time; time.sleep(120)"),
                self.lines.append,
                timeout_seconds=600,
                on_start=refuse,
            )
        self.leftover.extend(started)

        self.assertTrue(_gone(started[0]))


class AFailureOnceItRuns(_Console, unittest.TestCase):
    """Whatever fails once the Console exists stops it. The caller sees only the failure: it could
    neither cancel that Console nor keep a second one off its unit."""

    def assert_stopped(self, outcome: dict) -> None:
        self.leftover.append(outcome["pid"])
        self.assertTrue(_gone(outcome["pid"]), "the Console is stopped, not left running")
        self.assertIn("stop", outcome)

    def test_a_deadline_that_cannot_be_computed(self) -> None:
        outcome: dict = {}

        # The ten-year ceiling lifted, so the deadline overflows as it did before there was one.
        with patch("msdial_app.workflow._WATCH_MAX_SECONDS", float("inf")):
            with self.assertRaises(OverflowError):
                run_console(
                    self.preparation("import time; time.sleep(120)"),
                    self.lines.append,
                    timeout_seconds=1e12,
                    outcome=outcome,
                )

        self.assert_stopped(outcome)

    def test_a_line_handler_that_raises_watched_or_not(self) -> None:
        def evicted(_line: str) -> None:
            raise KeyError("job evicted")

        for watch in ({"timeout_seconds": 600}, {}):
            with self.subTest(watch=watch):
                outcome: dict = {}
                with self.assertRaises(KeyError):
                    run_console(
                        self.preparation("import time; print('hi'); time.sleep(120)"),
                        evicted,
                        outcome=outcome,
                        **watch,
                    )
                self.assert_stopped(outcome)


class TheDrain(_Console, unittest.TestCase):
    def test_a_slow_line_handler_loses_none_of_the_console_output(self) -> None:
        """The pipe is read ahead of the handler. The watch gave up on that backlog a drain time after
        the Console exited and called the pipe left open, losing the last lines - the Console's
        closing errors, which the failure diagnosis reads."""
        count = 2000
        script = f"import sys\nfor index in range({count}): sys.stdout.write('line %d\\n' % index)\n"

        def slow(line: str) -> None:
            self.lines.append(line)
            time.sleep(0.001)

        outcome: dict = {}
        with patch("msdial_app.workflow._WATCH_DRAIN_SECONDS", 0.25):
            code = run_console(self.preparation(script), slow, cancel_event=threading.Event(), outcome=outcome)
        self.leftover.append(outcome["pid"])

        self.assertEqual(0, code)
        self.assertEqual(count, len(self.lines))
        self.assertEqual(f"line {count - 1}", self.lines[-1])
        self.assertNotIn("output_left_open", outcome)

    def test_a_survivor_still_writing_into_the_pipe_does_not_hold_the_job(self) -> None:
        """A process the Console started keeps the pipe open and keeps writing. What it wrote within the
        drain time is passed on; the job is not held for the rest."""
        marker = self.root / "survivor.pid"
        survivor = (
            "import sys, time\n"
            "for index in range(1200):\n"
            "    sys.stdout.write('tick\\n'); sys.stdout.flush(); time.sleep(0.1)\n"
        )
        script = (
            "import subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, '-c', {survivor!r}])\n"
            f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(1)\n"
            "print('bye', flush=True)\n"
        )
        outcome: dict = {}
        started = time.monotonic()
        with patch("msdial_app.workflow._WATCH_DRAIN_SECONDS", 1.0):
            code = run_console(
                self.preparation(script), self.lines.append, cancel_event=threading.Event(), outcome=outcome
            )
        elapsed = time.monotonic() - started
        self.leftover.extend([outcome["pid"], int(marker.read_text())])

        self.assertEqual(0, code)
        self.assertIn("bye", self.lines)
        self.assertIn("tick", self.lines, "the survivor wrote into the Console's pipe")
        self.assertTrue(outcome.get("output_left_open"))
        self.assertLess(elapsed, 30, "the survivor's output was not waited on to its end")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

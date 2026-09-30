"""A recorded process is judged alive or gone by reading it, never by signalling it.

On Windows os.kill(pid, 0) is not a probe: signal 0 is CTRL_C_EVENT. And Windows reuses process ids, so
an id alone does not say the recorded process is still the one running under it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from msdial_app.process_liveness import process_created_at, process_is_alive


class ProcessLiveness(unittest.TestCase):
    def test_this_process_is_alive_and_has_a_creation_time(self) -> None:
        created = process_created_at()

        self.assertIsInstance(created, float)
        self.assertLess(created, time.time())
        self.assertIs(True, process_is_alive(os.getpid(), created))

    def test_an_exited_process_is_gone(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=60)

        # The Popen object still holds a handle, which keeps the process object; exited is still gone.
        self.assertIs(False, process_is_alive(child.pid))
        self.assertIsNone(process_created_at(child.pid))

    def test_a_live_process_with_another_creation_time_is_a_different_process(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            created = process_created_at(child.pid)
            self.assertIsInstance(created, float)
            self.assertIs(True, process_is_alive(child.pid, created))
            self.assertIs(False, process_is_alive(child.pid, created - 3600))
        finally:
            child.kill()
            child.wait(timeout=60)

    def test_an_unusable_process_id_cannot_be_read(self) -> None:
        for pid in (None, "", "abc", 0, -5):
            with self.subTest(pid=pid):
                self.assertIsNone(process_is_alive(pid))

    def test_no_process_is_ever_signalled(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            with patch("os.kill", side_effect=AssertionError("os.kill is not a liveness probe")):
                self.assertIs(True, process_is_alive(child.pid))
                self.assertIs(False, process_is_alive(999_999_999))
        finally:
            child.kill()
            child.wait(timeout=60)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""The double-click launchers for macOS and Linux.

A person opens `Start MS-DIAL Interactive.command` (macOS) or `start-msdial-interactive.sh` (Linux)
at the top of the app. Each starts the scripts under scripts/, which start app.py, which opens the
browser. A launcher that ran the next script by its path needed that script's execute bit, which
git did not record for scripts/ (and which a copy made on Windows, or synced through a cloud drive,
loses anyway): double-clicking stopped at "Permission denied". The launchers now run the next
script through bash, so only the file a person opens needs its execute bit.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = {
    "Start MS-DIAL Interactive.command": ["scripts/start-local-macos.command", "scripts/start-local-linux.sh"],
    "start-msdial-interactive.sh": ["scripts/start-local-linux.sh"],
}
SHELL_SCRIPTS = [
    "Start MS-DIAL Interactive.command",
    "start-msdial-interactive.sh",
    "scripts/start-local-macos.command",
    "scripts/start-local-linux.sh",
    "scripts/start-rt-correction-macos.command",
    "scripts/start-rt-correction-linux.sh",
    "scripts/start-lab-linux.sh",
]


def git_bash() -> str | None:
    """A bash that runs scripts: on Windows, Git's, never the WSL launcher in System32."""
    if os.name == "nt":
        for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(candidate).is_file():
                return candidate
        found = shutil.which("bash")
        return found if found and "system32" not in found.lower() else None
    return shutil.which("bash")


class LauncherTests(unittest.TestCase):
    def test_every_shell_launcher_uses_lf_line_endings(self) -> None:
        for name in SHELL_SCRIPTS:
            self.assertNotIn(b"\r", (ROOT / name).read_bytes(), f"{name} has a CRLF line ending")

    def test_the_launchers_run_the_next_script_through_bash(self) -> None:
        for launcher, chain in LAUNCHERS.items():
            for path in [launcher] + chain[:-1]:
                text = (ROOT / path).read_text(encoding="utf-8")
                calls = [line.strip() for line in text.splitlines() if "$SCRIPT_DIR/" in line and "SCRIPT_DIR=" not in line]
                self.assertTrue(calls, path)
                for call in calls:
                    self.assertTrue(call.startswith("exec bash "), f"{path} runs {call!r} without bash")

    @unittest.skipUnless(git_bash(), "no bash to run the launchers with")
    def test_a_launcher_starts_the_app_when_only_it_is_executable(self) -> None:
        bash = git_bash()
        for launcher, chain in LAUNCHERS.items():
            with self.subTest(launcher=launcher), tempfile.TemporaryDirectory() as folder:
                app = Path(folder) / "app"
                for name in [launcher] + chain:
                    target = app / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / name, target)
                    target.chmod(stat.S_IRUSR | stat.S_IWUSR)  # no execute bit anywhere
                (app / launcher).chmod(stat.S_IRWXU)  # the file a person opens
                fake = Path(folder) / "fake-python"
                fake.write_text('#!/usr/bin/env bash\necho "python $*"\n', encoding="utf-8", newline="\n")
                fake.chmod(stat.S_IRWXU)
                environment = dict(os.environ, PYTHON_BIN=fake.as_posix(), MSDIAL_INTERACTIVE_PORT="8799")
                completed = subprocess.run(
                    [bash, (app / launcher).as_posix()], capture_output=True, text=True, env=environment,
                    timeout=60, check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertIn("python -B app.py --host 127.0.0.1 --port 8799", completed.stdout)

    @unittest.skipUnless((ROOT / ".git").exists() and shutil.which("git"), "not a git checkout")
    def test_git_records_every_shell_launcher_as_executable(self) -> None:
        listed = subprocess.run(
            ["git", "ls-files", "-s", "--", *SHELL_SCRIPTS], cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        modes = {line.split("\t", 1)[1]: line.split()[0] for line in listed}
        self.assertEqual({name: "100755" for name in SHELL_SCRIPTS}, modes)


if __name__ == "__main__":
    unittest.main()

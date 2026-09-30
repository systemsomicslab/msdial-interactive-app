"""Two backends can keep separate job registries.

The registry lived in one file per user, %LOCALAPPDATA%\\MSDIALInteractive\\agent-jobs.json. A campaign's
backend and an interactive session's backend therefore wrote the same file: each persisted its own jobs
over the other's, and each, when it restarted, rewrote the other's running jobs as interrupted.
MSDIAL_INTERACTIVE_JOBS_FILE gives a backend a registry of its own.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# One backend's lifetime in miniature: load whatever its registry holds, add a job, persist.
_BACKEND = """
import json, sys
sys.path.insert(0, {root!r})
from msdial_app import server
with server.JOBS_LOCK:
    server.JOBS[{job!r}] = {{"id": {job!r}, "kind": "run", "status": "running", "updated_at": "2026-09-30"}}
    server._persist_jobs_locked()
print(json.dumps({{"jobs_file": str(server.JOBS_FILE), "known": sorted(server.JOBS)}}))
"""


class EachBackendKeepsItsOwnRegistry(unittest.TestCase):
    def _backend(self, job: str, local: Path, jobs_file: Path | None) -> dict:
        environment = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "LOCALAPPDATA": str(local),
            "APPDATA": str(local),
            "XDG_DATA_HOME": str(local),
        }
        environment.pop("MSDIAL_INTERACTIVE_JOBS_FILE", None)
        if jobs_file is not None:
            environment["MSDIAL_INTERACTIVE_JOBS_FILE"] = str(jobs_file)
        completed = subprocess.run(
            [sys.executable, "-c", _BACKEND.format(root=str(ROOT), job=job)],
            capture_output=True,
            text=True,
            timeout=120,
            env=environment,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_two_backends_with_their_own_files_do_not_touch_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_file = root / "campaign" / "backend" / "agent-jobs.json"
            interactive = self._backend("interactive-job", root / "profile", None)
            campaign = self._backend("campaign-job", root / "profile", campaign_file)
            interactive_again = self._backend("second-interactive-job", root / "profile", None)

            campaign_jobs = json.loads(campaign_file.read_text(encoding="utf-8"))["jobs"]
            shared_jobs = json.loads(Path(interactive["jobs_file"]).read_text(encoding="utf-8"))["jobs"]

        self.assertEqual(str(campaign_file.resolve()), campaign["jobs_file"])
        self.assertNotEqual(interactive["jobs_file"], campaign["jobs_file"])
        self.assertEqual(["campaign-job"], sorted(campaign_jobs))
        self.assertEqual("running", campaign_jobs["campaign-job"]["status"],
                         "the other backend's restart must not mark this job interrupted")
        self.assertEqual(["interactive-job", "second-interactive-job"], sorted(shared_jobs))
        self.assertEqual("interrupted", shared_jobs["interactive-job"]["status"],
                         "a backend still marks its own registry's running jobs interrupted on restart")
        self.assertEqual(["campaign-job"], campaign["known"])
        self.assertNotIn("campaign-job", interactive_again["known"])

    def test_without_the_setting_the_per_user_file_is_used_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._backend("job", Path(temporary) / "profile", None)

        self.assertEqual("agent-jobs.json", Path(result["jobs_file"]).name)
        if os.name == "nt":
            self.assertEqual("MSDIALInteractive", Path(result["jobs_file"]).parent.name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

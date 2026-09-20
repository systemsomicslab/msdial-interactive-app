"""A failed run must leave a record where the unit lives, not only in a registry that gets evicted.

A failed MS-DIAL run set status and a diagnosed error in the in-memory JOBS registry and nowhere
else. That registry is persisted truncated to the hundred most recently updated jobs, and at the
scale this programme is for -- each accession consuming a download job, a tuning job and one or more
run jobs -- later work evicts the failure. The unit's own workspace then looks exactly like a unit
nobody ever tried, which is the difference between "this one cannot be analysed" and "this one is
still to do".

The project contract requires "a failure record when unsuccessful" for every attempted unit. It
existed only as prose telling an agent to write one.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_app.repository_reanalysis import CLEANUP_READY_STATUSES, record_run_failure


class RecordRunFailure(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.manifest = self.root / "run-manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "workspace": str(self.root),
                    "status": "prepared",
                    "cleanup_allowed": True,
                    "output_directory": str(self.root / "output"),
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _manifest(self) -> dict:
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def test_the_failure_reaches_the_units_own_manifest(self) -> None:
        record_run_failure(self.manifest, "MS-DIAL Console exited with code 1.", 1, ["a", "b"])

        manifest = self._manifest()
        self.assertEqual("run_failed", manifest["status"])
        self.assertEqual(1, len(manifest["run_failures"]))
        self.assertEqual(1, manifest["run_failures"][0]["exit_code"])
        self.assertIn("exited with code 1", manifest["run_failures"][0]["reason"])
        self.assertEqual(["a", "b"], manifest["run_failures"][0]["log_tail"])

    def test_a_failed_run_never_clears_its_raw_data_for_deletion(self) -> None:
        """The raw data is what a retry needs, and a failure is when the disk looks reclaimable.

        The campaign's retention policy is now "delete after a successful run". This is the clause
        that keeps the word "successful" in it: cleanup_allowed is set false explicitly rather than
        left at whatever the download left it, and run_failed is not a status cleanup accepts.
        """
        record_run_failure(self.manifest, "crashed", 134, [])

        self.assertIs(False, self._manifest()["cleanup_allowed"])
        self.assertNotIn("run_failed", CLEANUP_READY_STATUSES)

    def test_repeated_failures_accumulate_rather_than_overwrite(self) -> None:
        """Three attempts that all failed is a different fact from one attempt that failed.

        It is the difference that says whether to keep retrying a unit or to set it aside, so the
        earlier attempts are kept rather than replaced by the latest.
        """
        record_run_failure(self.manifest, "first", 1, [])
        record_run_failure(self.manifest, "second", 2, [])

        failures = self._manifest()["run_failures"]
        self.assertEqual(["first", "second"], [item["reason"] for item in failures])

    def test_a_long_log_is_trimmed_so_the_manifest_stays_readable(self) -> None:
        record_run_failure(self.manifest, "verbose", 1, [f"line {index}" for index in range(500)])

        tail = self._manifest()["run_failures"][0]["log_tail"]
        self.assertEqual(40, len(tail))
        self.assertEqual("line 499", tail[-1], "the end of the log is the part that diagnoses")

    def test_a_manifest_that_cannot_be_written_returns_rather_than_raises(self) -> None:
        """Failing while recording a failure would lose both; the caller is already on its error path."""
        missing = self.root / "does-not-exist" / "run-manifest.json"

        result = record_run_failure(missing, "boom", 1, [])

        self.assertEqual("run_failed", result["status"])
        self.assertIn("manifest_error", result)
        self.assertEqual("boom", result["run_failure"]["reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

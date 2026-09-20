"""The measurement behind a threshold must outlive the job that measured it.

The project contract requires a zero-threshold diagnostic before every production repository run,
and requires "the method, representative sample, diagnostic count, threshold step, and accepted
threshold" to be retained in provenance.

All five were computed. None of them reached the workspace. The estimate went into the HTTP
response and into the in-memory JOBS registry, which is persisted truncated to the hundred most
recently updated jobs -- so at campaign scale, where each accession consumes a download job, a
tuning job and one or more run jobs, the measurement was evicted while the run it justified was
still on disk. What survived was the number alone, carried by hand into the production answers.

An audit reading the retained artifacts could see that a run used a threshold of 500 and could not
see whether 500 had ever been measured on this unit, on a different unit, or at all.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_app.repository_reanalysis import record_peak_height_diagnostic


ESTIMATE = {
    "minimum_peak_height": 500,
    "target_peak_count_min": 3000,
    "target_peak_count_max": 6000,
    "estimated_peak_count": 4180,
    "diagnostic_peak_count": 41230,
    "threshold_step": 100,
    "within_target_range": True,
    "method": "quantized height-range search",
}

REPRESENTATIVE = {
    "file_path": r"D:\analysis\unit\raw\QC_05.mzML",
    "file_name": "QC_05.mzML",
    "reason": "QC nearest the analytical-order midpoint",
    "analytical_order": 5,
}


class TheDiagnosticReachesTheUnitsOwnManifest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.manifest = Path(self.directory.name) / "run-manifest.json"
        self.manifest.write_text(
            json.dumps({"schema": "msdial-public-reanalysis-run.v1", "status": "prepared"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _manifest(self) -> dict:
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def test_all_five_facts_the_contract_names_are_written(self) -> None:
        """THE REGRESSION. Every one of these was computed and thrown away."""
        record_peak_height_diagnostic(self.manifest, ESTIMATE, REPRESENTATIVE, job_id="abc123")

        recorded = self._manifest()["peak_height_diagnostics"][0]

        self.assertEqual("quantized height-range search", recorded["method"])
        self.assertEqual("QC_05.mzML", recorded["representative"]["file_name"])
        self.assertEqual(41230, recorded["diagnostic_peak_count"])
        self.assertEqual(100, recorded["threshold_step"])
        self.assertEqual(500, recorded["minimum_peak_height"])

    def test_the_whole_estimate_is_kept_unedited_beside_the_named_fields(self) -> None:
        """Selecting fields is how a later change to the estimator stops being recorded."""
        record_peak_height_diagnostic(self.manifest, ESTIMATE, REPRESENTATIVE)

        self.assertEqual(ESTIMATE, self._manifest()["peak_height_diagnostics"][0]["estimate"])

    def test_which_file_it_was_measured_on_is_part_of_the_record(self) -> None:
        """A threshold measured on a blank is not the same fact as one measured on a mid-run QC."""
        record_peak_height_diagnostic(self.manifest, ESTIMATE, REPRESENTATIVE)

        representative = self._manifest()["peak_height_diagnostics"][0]["representative"]

        self.assertEqual("QC nearest the analytical-order midpoint", representative["reason"])

    def test_a_second_diagnostic_is_appended_rather_than_replacing_the_first(self) -> None:
        """Which thresholds were considered is part of why the accepted one was accepted."""
        record_peak_height_diagnostic(self.manifest, ESTIMATE, REPRESENTATIVE, job_id="first")
        record_peak_height_diagnostic(
            self.manifest, {**ESTIMATE, "minimum_peak_height": 700, "threshold_step": 1000},
            REPRESENTATIVE, job_id="second",
        )

        diagnostics = self._manifest()["peak_height_diagnostics"]

        self.assertEqual(["first", "second"], [item["job_id"] for item in diagnostics])
        self.assertEqual([500, 700], [item["minimum_peak_height"] for item in diagnostics])

    def test_a_zero_threshold_is_recorded_as_zero_and_not_as_nothing(self) -> None:
        """The contract keeps 0 when the diagnostic count is at most 6,000.

        0 and "no threshold was recorded" must not look the same to a reader, which is what a
        falsy test would have made of it.
        """
        record_peak_height_diagnostic(
            self.manifest,
            {**ESTIMATE, "minimum_peak_height": 0, "diagnostic_peak_count": 4200,
             "estimated_peak_count": 4200},
            REPRESENTATIVE,
        )

        recorded = self._manifest()["peak_height_diagnostics"][0]

        self.assertEqual(0, recorded["minimum_peak_height"])
        self.assertIsNotNone(recorded["minimum_peak_height"])

    def test_nothing_else_in_the_manifest_is_disturbed(self) -> None:
        record_peak_height_diagnostic(self.manifest, ESTIMATE, REPRESENTATIVE)

        manifest = self._manifest()

        self.assertEqual("prepared", manifest["status"])
        self.assertEqual("msdial-public-reanalysis-run.v1", manifest["schema"])

    def test_an_unwritable_manifest_returns_rather_than_raising(self) -> None:
        """The estimate is already in the caller's hands; losing the record must not lose it too."""
        missing = Path(self.directory.name) / "does-not-exist" / "run-manifest.json"

        result = record_peak_height_diagnostic(missing, ESTIMATE, REPRESENTATIVE)

        self.assertFalse(result["recorded"])
        self.assertIn("manifest_error", result)
        self.assertEqual(500, result["diagnostic"]["minimum_peak_height"])


class ALaboratoryAnalysisHasNoUnitToRecordInto(unittest.TestCase):
    def test_it_says_so_rather_than_looking_like_a_write_that_worked(self) -> None:
        from msdial_app.server import _record_peak_height_diagnostic

        result = _record_peak_height_diagnostic({}, ESTIMATE, REPRESENTATIVE, "job")

        self.assertFalse(result["recorded"])
        self.assertEqual("no_repository_manifest", result["reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

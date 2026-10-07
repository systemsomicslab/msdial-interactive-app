"""A production run records the peaks it actually kept, beside the diagnostic's estimate.

The user's decision of 2026-10-06: the threshold aims at the lower end of 3,000-6,000 estimated peaks, and
the production run's actual counts are recorded, because the estimate is read off one file's zero-threshold
diagnostic and a production run keeps fewer. finalise_console_run appends one production_peak_counts record
per run to the unit manifest: every file's count from its .mdpeak export, and the representative file's count
against the estimate of the diagnostic whose threshold the run applied.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_app.repository_reanalysis import read_manifest
from msdial_app.run_finalisation import (
    PRODUCTION_PEAK_COUNTS,
    count_mdpeak_peaks,
    finalise_console_run,
    production_peak_counts,
)

HEADER = "Peak ID\tName\tHeight\tSimple dot product\tWeighted dot product\tReverse dot product\n"


def _mdpeak(path: Path, count: int, nulls: int = 0) -> Path:
    rows = "".join(f"{index}\tUnknown\t{1000 + index}\tnull\tnull\tnull\n" for index in range(count))
    rows += "".join(f"{count + index}\tUnknown\tnull\tnull\tnull\tnull\n" for index in range(nulls))
    path.write_text(HEADER + rows, encoding="utf-8")
    return path


def _diagnostic(job_id: str, threshold: int, estimated: int, representative: str = "QC_05") -> dict:
    return {
        "job_id": job_id,
        "representative": {"file_name": representative, "instrument_family": "QTOF"},
        "estimate": {"minimum_peak_height": threshold, "estimated_peak_count": estimated,
                     "target_peak_count_min": 3000, "target_peak_count_max": 6000},
        "minimum_peak_height": threshold,
        "diagnostic_peak_count": 20000,
        "estimated_peak_count": estimated,
        "threshold_step": 100,
        "coarse_threshold_step": 100,
        "step_fallback": False,
        "within_target_range": True,
    }


class TheRecord(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name) / "output"
        self.output.mkdir()
        self.method = self.output / "method.txt"
        self.method.write_text("#Data type\nMinimum peak height: 200\nMass slice width: 0.1\n", encoding="utf-8")
        counts = {"S_01": 2800, "QC_05": 3100, "S_09": 2500}
        self.exports = [str(_mdpeak(self.output / f"{name}.mdpeak", count, nulls=3)) for name, count in counts.items()]
        self.preparation = {"method_file": str(self.method), "expected_analysis_exports": self.exports}

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_row_without_a_height_is_not_a_peak(self) -> None:
        self.assertEqual(3100, count_mdpeak_peaks(self.output / "QC_05.mdpeak"))

    def test_every_file_and_the_representative_against_the_diagnostic_whose_threshold_ran(self) -> None:
        manifest = {"peak_height_diagnostics": [
            _diagnostic("d1", 100, 5200), _diagnostic("d2", 200, 3600), _diagnostic("d3", 300, 3000),
        ]}

        record = production_peak_counts("run1", self.preparation, manifest, True)

        self.assertEqual("msdial-production-peak-counts.v1", record["schema"])
        self.assertEqual(200.0, record["minimum_peak_height"])
        self.assertEqual(("d2", True), (record["diagnostic_job_id"], record["diagnostic_matches_applied_threshold"]))
        self.assertEqual(3600, record["estimated_peak_count"])
        self.assertEqual(("QC_05", 3100), (record["representative_file_name"], record["representative_peak_count"]))
        self.assertEqual(round(3100 / 3600, 4), record["representative_to_estimate_ratio"])
        self.assertIs(True, record["representative_within_target_range"])
        self.assertEqual(
            [("S_01", 2800), ("QC_05", 3100), ("S_09", 2500)],
            [(item["file_name"], item["peak_count"]) for item in record["files"]],
        )
        self.assertEqual((3, 3, 2500, 2800, 3100, 8400), (
            record["file_count"], record["files_counted"], record["peak_count_min"], record["peak_count_median"],
            record["peak_count_max"], record["peak_count_total"],
        ))

    def test_with_no_diagnostic_at_the_applied_threshold_the_latest_is_named_as_such(self) -> None:
        manifest = {"peak_height_diagnostics": [_diagnostic("d1", 100, 5200), _diagnostic("d3", 300, 3000)]}

        record = production_peak_counts("run1", self.preparation, manifest, True)

        self.assertEqual(("d3", False), (record["diagnostic_job_id"], record["diagnostic_matches_applied_threshold"]))

    def test_a_missing_export_is_listed_with_why_and_not_counted(self) -> None:
        Path(self.exports[2]).unlink()

        record = production_peak_counts("run1", self.preparation, {}, False)

        self.assertIsNone(record["files"][2]["peak_count"])
        self.assertIn("error", record["files"][2])
        self.assertEqual((3, 2, False), (record["file_count"], record["files_counted"], record["run_complete"]))
        self.assertIsNone(record["diagnostic_job_id"])
        self.assertIsNone(record["representative_peak_count"])

    def test_a_run_with_no_mdpeak_exports_records_nothing(self) -> None:
        self.assertIsNone(production_peak_counts("run1", {"expected_analysis_exports": ["a.mdscan"]}, {}, True))


class TheRunsFinalisationAppendsIt(unittest.TestCase):
    def test_two_runs_leave_two_records_in_the_unit_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            (output / "method.txt").write_text("Minimum peak height: 200\n", encoding="utf-8")
            exports = [str(_mdpeak(output / f"{name}.mdpeak", count)) for name, count in (("QC_05", 3100), ("S_01", 2900))]
            manifest = root / "run-manifest.json"
            manifest.write_text(json.dumps({
                "schema": "msdial-public-reanalysis-run.v1", "output_directory": str(output),
                "peak_height_diagnostics": [_diagnostic("d2", 200, 3600)],
            }), encoding="utf-8")
            preparation = {"repository_run_manifest": str(manifest), "run_directory": str(output),
                           "method_file": str(output / "method.txt"), "expected_analysis_exports": exports}
            lines: list[str] = []

            first = finalise_console_run("run1", preparation, {}, 0, {}, lines.append)
            finalise_console_run("run2", preparation, {}, 0, {}, lines.append)

            written = read_manifest(manifest)[PRODUCTION_PEAK_COUNTS]
            self.assertEqual(["run1", "run2"], [item["job_id"] for item in written])
            self.assertEqual((3100, 3600, "d2"), (
                written[0]["representative_peak_count"], written[0]["estimated_peak_count"], written[0]["diagnostic_job_id"],
            ))
            self.assertEqual([3100, 2900], [item["peak_count"] for item in written[0]["files"]])
            # The finalisation record carries the summary; the per-file list is in the manifest's record.
            self.assertEqual(3100, first[PRODUCTION_PEAK_COUNTS]["representative_peak_count"])
            self.assertNotIn("files", first[PRODUCTION_PEAK_COUNTS])
            self.assertTrue(any(line.startswith("Production peak counts:") for line in lines))

    def test_a_laboratory_run_writes_no_manifest_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            exports = [str(_mdpeak(output / "A.mdpeak", 10))]

            record = finalise_console_run(
                "run1", {"run_directory": str(output), "expected_analysis_exports": exports}, {}, 0, {}, lambda _l: None
            )

            self.assertEqual("laboratory", record["scope"])
            self.assertNotIn(PRODUCTION_PEAK_COUNTS, record)


if __name__ == "__main__":
    unittest.main()

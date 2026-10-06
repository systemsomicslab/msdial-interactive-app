"""The stepped threshold falls back to a tenth of the instrument-family step, and no finer.

The user's decision of 2026-10-06. The range search for a Minimum peak height keeping 3,000-6,000
peaks uses the instrument-family step: 100 for QTOF-type data, 1,000 for Fourier-transform data. Only
when the zero-threshold count is above 6,000 and no multiple of that step gives a count in range does
it fall back to the fine step, 10 for QTOF-type and 100 for FT. It never goes finer: on the Waters MSe
demo a count in range is reached only at a threshold of 2, where the median S/N is 2.6. When even the
fine step misses, the threshold is the candidate nearest the range, marked out of range with a warning.

Before this, the family step was the only step. Four of the fourteen diagnosed sets came out of range
with nothing but within_target_range=false to say so: the Bruker compact demo at 100 kept an estimated
1,932 peaks, the Waters Premier DDA demo 754, MetaboBank MTBKS281 338, and the Waters MSe demo 26.

The vectors are the height histograms of those fourteen diagnostics (tests/vectors/
peak_height_diagnostics.v1.json), so the cases below are the measured ones, not constructed ones.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app.agent_bridge import summarize_jobs
from msdial_app.agent_workflow import estimate_peak_height_range, fine_threshold_step
from msdial_app.repository_reanalysis import record_peak_height_diagnostic

from test_manifest_reentry import _Backend, _Unit, read_manifest, server


VECTORS = Path(__file__).resolve().parent / "vectors" / "peak_height_diagnostics.v1.json"
SETS = {item["label"]: item for item in json.loads(VECTORS.read_text(encoding="utf-8"))["sets"]}
NEW_FIELDS = ("threshold_step", "coarse_threshold_step", "step_fallback", "fallback_reason", "within_target_range")


def heights(label: str) -> list[float]:
    return [float(height) for height, count in SETS[label]["buckets"] for _ in range(count)]


def estimate(label: str) -> dict:
    return estimate_peak_height_range(heights(label), 3000, 6000, SETS[label]["family_step"])


class TheFourSetsTheFamilyStepLeftOutOfRange(unittest.TestCase):
    def test_bruker_compact_dda_goes_from_100_to_30_and_into_range(self) -> None:
        result = estimate("demo bruker-compact_dda")

        self.assertEqual(30, result["minimum_peak_height"])
        self.assertEqual(4722, result["estimated_peak_count"])
        self.assertEqual(10, result["threshold_step"])
        self.assertEqual(100, result["coarse_threshold_step"])
        self.assertTrue(result["step_fallback"])
        self.assertEqual("no_coarse_step_in_range", result["fallback_reason"])
        self.assertTrue(result["within_target_range"])
        self.assertEqual([], result["warnings"])
        # What the family step alone chose is kept, so the fallback can be read against it.
        self.assertEqual(100, result["coarse_minimum_peak_height"])
        self.assertEqual(1932, result["coarse_estimated_peak_count"])

    def test_waters_premier_dda_goes_from_100_to_20_and_into_range(self) -> None:
        result = estimate("demo waters-premier_dda_pos")

        self.assertEqual((20, 3627), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertEqual((10, 100, True), (result["threshold_step"], result["coarse_threshold_step"], result["step_fallback"]))
        self.assertTrue(result["within_target_range"])
        self.assertEqual((100, 754), (result["coarse_minimum_peak_height"], result["coarse_estimated_peak_count"]))

    def test_mtbks281_goes_from_100_to_10_and_is_still_out_of_range_with_a_warning(self) -> None:
        result = estimate("MTBKS281")

        self.assertEqual((10, 2484), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertEqual(10, result["threshold_step"])
        self.assertTrue(result["step_fallback"])
        self.assertEqual("no_coarse_step_in_range", result["fallback_reason"])
        self.assertFalse(result["within_target_range"])
        self.assertEqual(1, len(result["warnings"]))
        self.assertIn("2484", result["warnings"][0])
        self.assertIn("out of the target range", result["warnings"][0])

    def test_waters_mse_goes_from_100_to_10_and_is_still_out_of_range_with_a_warning(self) -> None:
        """A step of 2 would reach the range, at the noise floor. The fine step is the floor."""
        result = estimate("demo waters-premier_mse_pos")

        self.assertEqual((10, 522), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertEqual(10, result["threshold_step"])
        self.assertTrue(result["step_fallback"])
        self.assertFalse(result["within_target_range"])
        self.assertTrue(result["warnings"])


class EverySetTheFamilyStepAlreadyPlacedIsUnchanged(unittest.TestCase):
    def test_the_threshold_and_count_of_every_in_range_set_are_what_they_were(self) -> None:
        unchanged = [label for label, item in SETS.items() if item["after"] == {
            **item["before"], "threshold_step": item["family_step"], "within_target_range": True,
        }]
        self.assertEqual(10, len(unchanged), unchanged)
        for label in unchanged:
            with self.subTest(label=label):
                result = estimate(label)
                before = SETS[label]["before"]
                self.assertEqual(before["minimum_peak_height"], result["minimum_peak_height"])
                self.assertEqual(before["estimated_peak_count"], result["estimated_peak_count"])
                self.assertEqual(SETS[label]["family_step"], result["threshold_step"])
                self.assertEqual(SETS[label]["family_step"], result["coarse_threshold_step"])
                self.assertFalse(result["step_fallback"])
                self.assertIsNone(result["fallback_reason"])
                self.assertTrue(result["within_target_range"])
                self.assertEqual([], result["warnings"])

    def test_every_vector_gives_its_recorded_result(self) -> None:
        for label, item in SETS.items():
            with self.subTest(label=label):
                result = estimate(label)
                self.assertEqual(item["after"], {key: result[key] for key in item["after"]})

    def test_a_zero_threshold_count_at_or_below_6000_keeps_zero_without_a_fallback(self) -> None:
        """MTBLS2207's DIA unit: 4,893 peaks at zero."""
        result = estimate("MTBLS2207-dia")

        self.assertEqual(0, result["minimum_peak_height"])
        self.assertEqual(1000, result["threshold_step"])
        self.assertFalse(result["step_fallback"])
        self.assertIsNone(result["fallback_reason"])
        for field in NEW_FIELDS:
            self.assertIn(field, result)

    def test_below_the_lower_bound_at_zero_is_still_zero_and_not_a_fallback(self) -> None:
        result = estimate_peak_height_range([float(value) for value in range(2500)], 3000, 6000, 100)

        self.assertEqual(0, result["minimum_peak_height"])
        self.assertFalse(result["within_target_range"])
        self.assertFalse(result["step_fallback"])


class TheFineStepIsUsedOnlyWhenNeededAndIsAFloor(unittest.TestCase):
    def test_a_coarse_step_in_range_is_kept_even_where_the_fine_step_is_nearer_the_midpoint(self) -> None:
        """demo waters-xevo_dda: 100 keeps 5,121; a step of 10 would choose 120 (4,329). The rule is 100."""
        result = estimate("demo waters-xevo_dda")

        self.assertEqual((100, 5121), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertFalse(result["step_fallback"])

    def test_fourier_transform_data_fall_back_from_1000_to_100(self) -> None:
        """No FT set has needed it yet, so this one is constructed. 7,000 peaks at 1,000 and 2,000 at
        2,000 put no multiple of 1,000 in range; in steps of 100, 1,500 keeps 4,500."""
        values = [5000.0] * 2000 + [float(height) for height in range(1000, 2000, 100) for _ in range(500)] + [10.0] * 3000
        result = estimate_peak_height_range(values, 3000, 6000, 1000)

        self.assertEqual((1500, 4500), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertEqual((100, 1000), (result["threshold_step"], result["coarse_threshold_step"]))
        self.assertTrue(result["step_fallback"])
        self.assertEqual("no_coarse_step_in_range", result["fallback_reason"])
        self.assertTrue(result["within_target_range"])
        self.assertEqual((1000, 7000), (result["coarse_minimum_peak_height"], result["coarse_estimated_peak_count"]))

    def test_the_search_never_goes_finer_than_the_fine_step(self) -> None:
        """Only a threshold of 2 or 3 would land in range. The fine step of 10 is the floor, and of its
        multiples 0 (8,100 peaks) is nearer the range than 10 (100 peaks)."""
        values = [1.0] * 4000 + [3.0] * 4000 + [500.0] * 100
        result = estimate_peak_height_range(values, 3000, 6000, 100)

        self.assertEqual(10, result["threshold_step"])
        self.assertTrue(result["step_fallback"])
        self.assertEqual((0, 8100), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertFalse(result["within_target_range"])
        self.assertTrue(result["warnings"])

    def test_the_fine_step_is_a_tenth_of_the_family_step(self) -> None:
        self.assertEqual(10, fine_threshold_step(100))
        self.assertEqual(100, fine_threshold_step(1000))
        # A step with no tenth that divides it has no finer step: it is its own floor.
        self.assertEqual(15, fine_threshold_step(15))
        self.assertEqual(1, fine_threshold_step(1))

    def test_a_step_with_no_finer_step_warns_when_out_of_range_and_records_no_fallback(self) -> None:
        result = estimate_peak_height_range([1.0] * 8000, 3000, 6000, 1)

        self.assertFalse(result["step_fallback"])
        self.assertIsNone(result["fallback_reason"])
        self.assertFalse(result["within_target_range"])
        self.assertTrue(result["warnings"])


class TheFallbackIsOnRecord(unittest.TestCase):
    """The user said the fallback MUST be recorded, and so must a fine step that still misses."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.manifest = Path(self.directory.name) / "run-manifest.json"
        self.manifest.write_text(json.dumps({"schema": "msdial-public-reanalysis-run.v1"}), encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _recorded(self) -> dict:
        return json.loads(self.manifest.read_text(encoding="utf-8"))["peak_height_diagnostics"][-1]

    def test_the_five_fields_are_in_the_provenance_record_beside_the_threshold(self) -> None:
        record_peak_height_diagnostic(self.manifest, estimate("MTBKS281"), {"file_name": "Lm1"})

        recorded = self._recorded()

        self.assertEqual(10, recorded["minimum_peak_height"])
        self.assertEqual(10, recorded["threshold_step"])
        self.assertEqual(100, recorded["coarse_threshold_step"])
        self.assertIs(True, recorded["step_fallback"])
        self.assertEqual("no_coarse_step_in_range", recorded["fallback_reason"])
        self.assertIs(False, recorded["within_target_range"])
        self.assertTrue(recorded["estimate"]["warnings"])

    def test_a_record_with_no_fallback_says_so_rather_than_leaving_it_out(self) -> None:
        record_peak_height_diagnostic(self.manifest, estimate("MTBKS217"), {"file_name": "190827_040nn"})

        recorded = self._recorded()

        self.assertEqual((100, 100), (recorded["threshold_step"], recorded["coarse_threshold_step"]))
        self.assertIs(False, recorded["step_fallback"])
        self.assertIn("fallback_reason", recorded)
        self.assertIsNone(recorded["fallback_reason"])
        self.assertIs(True, recorded["within_target_range"])

    def test_an_estimate_from_before_the_fallback_reads_as_its_own_step_and_no_fallback(self) -> None:
        record_peak_height_diagnostic(
            self.manifest,
            {"minimum_peak_height": 500, "threshold_step": 100, "within_target_range": True,
             "method": "quantized height-range search"},
        )

        recorded = self._recorded()

        self.assertEqual(100, recorded["coarse_threshold_step"])
        self.assertIs(False, recorded["step_fallback"])
        self.assertIsNone(recorded["fallback_reason"])

    def test_the_capability_is_advertised(self) -> None:
        self.assertIn("peak_height_fine_step_fallback", summarize_jobs({})["capabilities"])


class TheEstimateEndpointRecordsTheFallback(_Unit, _Backend, unittest.TestCase):
    """End to end through POST /api/agent/tuning/estimate, on the Bruker compact demo's heights."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "unit"
        self.manifest = self.make(self.root)
        self.job_id = "diagfallback"
        self.diagnostic = self.root / "diagnostics" / self.job_id
        self.diagnostic.mkdir(parents=True)
        result_file = self.diagnostic / "a_DDA_1.mdpeak"
        rows = "".join(f"{height:g}\t0\t0\t0\n" for height in heights("demo bruker-compact_dda"))
        result_file.write_text(
            "Height\tSimple dot product\tWeighted dot product\tReverse dot product\n" + rows,
            encoding="utf-8",
        )
        self.preparation = {
            "diagnostic_run_directory": str(self.diagnostic),
            "repository_run_manifest": str(self.manifest),
            "analysis_type": "lcms",
            "diagnostic_result_file": str(result_file),
            "peak_tuning_profile": {"file_name": "a_DDA_1", "threshold_step": 100, "reason": "QC nearest"},
        }
        self.start()

    def tearDown(self) -> None:
        self.stop()
        self.directory.cleanup()

    def test_the_response_and_the_manifest_both_carry_the_fallback(self) -> None:
        server._write_diagnostic_record(self.job_id, self.preparation, "running")
        server._write_diagnostic_record(self.job_id, self.preparation, "completed", exit_code=0)

        with patch.object(server, "JOBS", {}):
            status, response = self.post(
                "/api/agent/tuning/estimate",
                {"job_id": self.job_id, "manifest_path": str(self.manifest),
                 "target_peak_count_min": 3000, "target_peak_count_max": 6000},
            )

        self.assertEqual(200, status, response)
        found = response["estimate"]
        self.assertEqual((30, 10, 100, True, "no_coarse_step_in_range", True), (
            found["minimum_peak_height"], found["threshold_step"], found["coarse_threshold_step"],
            found["step_fallback"], found["fallback_reason"], found["within_target_range"],
        ))
        recorded = read_manifest(self.manifest)["peak_height_diagnostics"][-1]
        self.assertEqual({field: found[field] for field in NEW_FIELDS}, {field: recorded[field] for field in NEW_FIELDS})
        self.assertEqual(30, recorded["minimum_peak_height"])


if __name__ == "__main__":
    unittest.main()

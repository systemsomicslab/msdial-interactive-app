"""The stepped threshold: the lower end of the range, the family step first, an absolute fine-step floor.

The user's decisions of 2026-10-06. The range search for a Minimum peak height keeping 3,000-6,000 peaks:
- keeps 0 when the zero-threshold count is 6,000 or fewer;
- otherwise takes, of the multiples of the instrument-family step (100 for QTOF-type data, 1,000 for
  Fourier-transform data), the HIGHEST whose estimated count is still at least 3,000: the lower end of the
  range, because the user wants MS/MS of high quality and gap filling recovers sub-threshold peaks;
- only when no multiple of the family step lands in range, makes the same choice in the fine step, 10 for
  QTOF-type and 100 for FT. The fine step is an absolute floor: a caller passing a smaller step gets the
  floor, never a step of 1, which on the Waters MSe demo lands at a threshold of 2 (median S/N 2.6);
- when even the fine step misses, takes the candidate nearest the range, marked out of range with a warning.

Before 0.5.28 the family step was the only step and the threshold nearest the range's midpoint was taken.
The first draft of this PR (164e770) added the fallback but kept the midpoint and derived the fine step as a
tenth of the caller's step, so a request with step 10 searched in steps of 1.

The vectors are the height histograms of the fourteen diagnostics on disk (tests/vectors/
peak_height_diagnostics.v1.json), so the cases below are the measured ones, not constructed ones.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from bisect import bisect_left
from pathlib import Path
from unittest.mock import patch

from msdial_app.agent_bridge import summarize_jobs
from msdial_app.agent_workflow import (
    SELECTION_RULE,
    estimate_peak_height_range,
    fine_threshold_step,
)
from msdial_app.repository_reanalysis import record_peak_height_diagnostic

from test_manifest_reentry import _Backend, _Unit, read_manifest, server


VECTORS = Path(__file__).resolve().parent / "vectors" / "peak_height_diagnostics.v1.json"
SETS = {item["label"]: item for item in json.loads(VECTORS.read_text(encoding="utf-8"))["sets"]}
NEW_FIELDS = ("threshold_step", "coarse_threshold_step", "step_fallback", "fallback_reason", "within_target_range")


def heights(label: str) -> list[float]:
    return [float(height) for height, count in SETS[label]["buckets"] for _ in range(count)]


def estimate(label: str, step: int | None = None) -> dict:
    item = SETS[label]
    return estimate_peak_height_range(
        heights(label), 3000, 6000, item["family_step"] if step is None else step, item["instrument_family"]
    )


def count_at(values: list[float], threshold: float) -> int:
    ordered = sorted(values)
    return len(ordered) - bisect_left(ordered, threshold)


class TheLowerEndOfTheRange(unittest.TestCase):
    def test_st001337_moves_from_the_midpoint_to_the_highest_threshold_keeping_3000(self) -> None:
        """Thermo Fusion, FT: 181,000 kept 4,495; 323,000 keeps 3,004 and 324,000 would keep fewer than 3,000."""
        result = estimate("ST001337")

        self.assertEqual((323000, 3004), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertEqual((1000, 1000, False), (result["threshold_step"], result["coarse_threshold_step"], result["step_fallback"]))
        self.assertTrue(result["within_target_range"])
        self.assertEqual(SELECTION_RULE, result["selection_rule"])

    def test_every_in_range_threshold_is_the_highest_multiple_still_keeping_3000(self) -> None:
        for label in SETS:
            result = estimate(label)
            if not result["within_target_range"] or result["minimum_peak_height"] == 0:
                continue
            with self.subTest(label=label):
                values = heights(label)
                threshold, step = result["minimum_peak_height"], result["threshold_step"]
                self.assertEqual(0, threshold % step)
                self.assertGreaterEqual(count_at(values, threshold), 3000)
                self.assertLess(count_at(values, threshold + step), 3000)

    def test_the_sets_whose_midpoint_choice_was_already_the_lower_end_are_unchanged(self) -> None:
        unchanged = [
            label for label, item in SETS.items()
            if (item["after"]["minimum_peak_height"], item["after"]["estimated_peak_count"])
            == (item["before"]["minimum_peak_height"], item["before"]["estimated_peak_count"])
        ]
        self.assertEqual(
            sorted(["MTBKS217", "MTBKS236", "MTBLS2207-dia", "demo waters-xevo_dda", "demo bruker-swath"]),
            sorted(unchanged),
        )

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


class TheFamilyStepFirstAndTheFineStepOnlyWhenNoCoarseStepIsInRange(unittest.TestCase):
    def test_bruker_compact_dda_falls_back_to_50(self) -> None:
        result = estimate("demo bruker-compact_dda")

        self.assertEqual((50, 3257), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertEqual((10, 100), (result["threshold_step"], result["coarse_threshold_step"]))
        self.assertTrue(result["step_fallback"])
        self.assertEqual("no_coarse_step_in_range", result["fallback_reason"])
        self.assertTrue(result["within_target_range"])
        self.assertEqual([], result["warnings"])
        # What the family step alone chose is kept, so the fallback can be read against it.
        self.assertEqual((100, 1932), (result["coarse_minimum_peak_height"], result["coarse_estimated_peak_count"]))

    def test_waters_premier_dda_falls_back_to_20(self) -> None:
        result = estimate("demo waters-premier_dda_pos")

        self.assertEqual((20, 3627), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertEqual((10, 100, True), (result["threshold_step"], result["coarse_threshold_step"], result["step_fallback"]))
        self.assertTrue(result["within_target_range"])
        self.assertEqual((100, 754), (result["coarse_minimum_peak_height"], result["coarse_estimated_peak_count"]))

    def test_mtbks281_falls_back_to_10_and_is_still_out_of_range_with_a_warning(self) -> None:
        result = estimate("MTBKS281")

        self.assertEqual((10, 2484), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertEqual(10, result["threshold_step"])
        self.assertTrue(result["step_fallback"])
        self.assertFalse(result["within_target_range"])
        self.assertEqual(1, len(result["warnings"]))
        self.assertIn("2484", result["warnings"][0])
        self.assertIn("out of the target range", result["warnings"][0])

    def test_waters_mse_falls_back_to_10_and_is_still_out_of_range_with_a_warning(self) -> None:
        result = estimate("demo waters-premier_mse_pos")

        self.assertEqual((10, 522), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertTrue(result["step_fallback"])
        self.assertFalse(result["within_target_range"])
        self.assertTrue(result["warnings"])

    def test_a_coarse_step_in_range_is_kept_where_the_fine_step_would_differ(self) -> None:
        """demo waters-xevo_dda: 100 keeps 5,121 and 200 fewer than 3,000; steps of 10 would choose higher."""
        result = estimate("demo waters-xevo_dda")

        self.assertEqual((100, 5121), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertFalse(result["step_fallback"])
        self.assertLess(count_at(heights("demo waters-xevo_dda"), 200), 3000)

    def test_fourier_transform_data_fall_back_from_1000_to_100(self) -> None:
        """No FT set on disk has needed it, so this one is constructed. 1,000 keeps 7,000 and 2,000 keeps
        2,000, so no multiple of 1,000 is in range; in steps of 100, 1,800 is the highest keeping 3,000."""
        values = [5000.0] * 2000 + [float(height) for height in range(1000, 2000, 100) for _ in range(500)] + [10.0] * 3000
        result = estimate_peak_height_range(values, 3000, 6000, 1000, "Fourier-transform MS")

        self.assertEqual((1800, 3000), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertEqual((100, 1000), (result["threshold_step"], result["coarse_threshold_step"]))
        self.assertTrue(result["step_fallback"])
        self.assertTrue(result["within_target_range"])
        # 1,000 (7,000, 1,000 above) and 2,000 (2,000, 1,000 below) are equally near: the one keeping more.
        self.assertEqual((1000, 7000), (result["coarse_minimum_peak_height"], result["coarse_estimated_peak_count"]))

    def test_the_search_never_goes_finer_than_the_fine_step(self) -> None:
        """Only a threshold of 2 or 3 would land in range. Of the multiples of 10, 0 (8,100 peaks) is nearer
        the range than 10 (100 peaks)."""
        values = [1.0] * 4000 + [3.0] * 4000 + [500.0] * 100
        result = estimate_peak_height_range(values, 3000, 6000, 100)

        self.assertEqual(10, result["threshold_step"])
        self.assertTrue(result["step_fallback"])
        self.assertEqual((0, 8100), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertFalse(result["within_target_range"])
        self.assertTrue(result["warnings"])


class TheFineStepIsAnAbsoluteFloor(unittest.TestCase):
    """The review of 164e770: a caller echoing a fallback's step of 10 got a search in steps of 1."""

    def test_echoing_the_step_10_back_on_the_mse_demo_does_not_reach_a_threshold_of_2(self) -> None:
        result = estimate("demo waters-premier_mse_pos", step=10)

        self.assertEqual((10, 522), (result["minimum_peak_height"], result["estimated_peak_count"]))
        self.assertEqual((10, 10, 10), (result["threshold_step"], result["coarse_threshold_step"], result["fine_threshold_step"]))
        self.assertFalse(result["step_fallback"], "a coarse step at the floor has nothing finer to fall back to")
        self.assertFalse(result["within_target_range"])
        self.assertTrue(result["warnings"])

    def test_echoing_the_step_10_back_on_mtbks281_does_not_reach_a_threshold_of_4(self) -> None:
        result = estimate("MTBKS281", step=10)

        self.assertEqual((10, 2484, 10), (result["minimum_peak_height"], result["estimated_peak_count"], result["threshold_step"]))
        self.assertFalse(result["within_target_range"])

    def test_a_requested_step_below_the_floor_is_raised_to_it_and_recorded(self) -> None:
        result = estimate("demo waters-premier_mse_pos", step=1)

        self.assertEqual(1, result["requested_threshold_step"])
        self.assertEqual(10, result["coarse_threshold_step"])
        self.assertEqual(10, result["minimum_peak_height"])

    def test_the_fourier_transform_floor_is_100(self) -> None:
        values = heights("MTBLS2207-dda")
        for requested in (10, 100):
            with self.subTest(requested=requested):
                result = estimate_peak_height_range(values, 3000, 6000, requested, "Fourier-transform MS")
                self.assertEqual(100, result["coarse_threshold_step"])
                self.assertEqual(100, result["fine_threshold_step"])
                self.assertFalse(result["step_fallback"])
                self.assertEqual(0, result["minimum_peak_height"] % 100)

    def test_fine_threshold_step(self) -> None:
        self.assertEqual(10, fine_threshold_step(100))
        self.assertEqual(10, fine_threshold_step(100, "QTOF"))
        self.assertEqual(100, fine_threshold_step(1000))
        self.assertEqual(100, fine_threshold_step(1000, "Fourier-transform MS"))
        self.assertEqual(100, fine_threshold_step(1000, "FT-ICR"))
        # Absolute, not a tenth of the step passed.
        self.assertEqual(10, fine_threshold_step(10))
        self.assertEqual(10, fine_threshold_step(50))
        self.assertEqual(100, fine_threshold_step(100, "Fourier-transform MS"))
        self.assertEqual(100, fine_threshold_step(5000, "Fourier-transform MS"))
        # A step already below the floor is its own fine step (the estimator raises it to the floor first).
        self.assertEqual(1, fine_threshold_step(1))

    def test_without_a_family_a_step_of_1000_is_the_ft_family_step(self) -> None:
        values = [5000.0] * 2000 + [float(height) for height in range(1000, 2000, 100) for _ in range(500)] + [10.0] * 3000
        result = estimate_peak_height_range(values, 3000, 6000, 1000)

        self.assertEqual(100, result["fine_threshold_step"])
        self.assertEqual(1800, result["minimum_peak_height"])


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

    def test_the_fields_are_in_the_provenance_record_beside_the_threshold(self) -> None:
        record_peak_height_diagnostic(
            self.manifest, estimate("MTBKS281"),
            {"file_name": "Lm1", "instrument_family": "QTOF", "instrument_family_source": "vendor_format"},
        )

        recorded = self._recorded()

        self.assertEqual(10, recorded["minimum_peak_height"])
        self.assertEqual(10, recorded["threshold_step"])
        self.assertEqual(100, recorded["coarse_threshold_step"])
        self.assertEqual(10, recorded["fine_threshold_step"])
        self.assertIs(True, recorded["step_fallback"])
        self.assertEqual("no_coarse_step_in_range", recorded["fallback_reason"])
        self.assertIs(False, recorded["within_target_range"])
        self.assertEqual(SELECTION_RULE, recorded["selection_rule"])
        self.assertEqual(("QTOF", "vendor_format"), (recorded["instrument_family"], recorded["instrument_family_source"]))
        self.assertTrue(recorded["estimate"]["warnings"])

    def test_a_record_with_no_fallback_says_so_rather_than_leaving_it_out(self) -> None:
        record_peak_height_diagnostic(self.manifest, estimate("MTBKS217"), {"file_name": "190827_040nn"})

        recorded = self._recorded()

        self.assertEqual((100, 100), (recorded["threshold_step"], recorded["coarse_threshold_step"]))
        self.assertIs(False, recorded["step_fallback"])
        self.assertIn("fallback_reason", recorded)
        self.assertIsNone(recorded["fallback_reason"])
        self.assertIs(True, recorded["within_target_range"])

    def test_an_estimate_from_before_the_rules_reads_as_its_own_step_with_no_rule_named(self) -> None:
        record_peak_height_diagnostic(
            self.manifest,
            {"minimum_peak_height": 500, "threshold_step": 100, "within_target_range": True,
             "method": "quantized height-range search"},
        )

        recorded = self._recorded()

        self.assertEqual(100, recorded["coarse_threshold_step"])
        self.assertIs(False, recorded["step_fallback"])
        self.assertIsNone(recorded["fallback_reason"])
        self.assertIsNone(recorded["selection_rule"], "it chose the midpoint, and says nothing else")

    def test_the_capability_is_advertised(self) -> None:
        capabilities = summarize_jobs({})["capabilities"]
        for name in ("peak_height_fine_step_fallback", "peak_height_lower_end_selection",
                     "mzml_header_instrument_family", "production_peak_counts"):
            self.assertIn(name, capabilities)


class TheEstimateEndpointRecordsTheFallback(_Unit, _Backend, unittest.TestCase):
    """End to end through POST /api/agent/tuning/estimate, on the Bruker compact demo's heights."""

    FAMILY = "QTOF"

    @staticmethod
    def diagnostic_heights() -> list[float]:
        return heights("demo bruker-compact_dda")

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "unit"
        self.manifest = self.make(self.root)
        self.job_id = "diagfallback"
        self.diagnostic = self.root / "diagnostics" / self.job_id
        self.diagnostic.mkdir(parents=True)
        result_file = self.diagnostic / "a_DDA_1.mdpeak"
        rows = "".join(f"{height:g}\t0\t0\t0\n" for height in self.diagnostic_heights())
        result_file.write_text(
            "Height\tSimple dot product\tWeighted dot product\tReverse dot product\n" + rows,
            encoding="utf-8",
        )
        self.preparation = {
            "diagnostic_run_directory": str(self.diagnostic),
            "repository_run_manifest": str(self.manifest),
            "analysis_type": "lcms",
            "diagnostic_result_file": str(result_file),
            "peak_tuning_profile": {
                "file_name": "a_DDA_1", "threshold_step": 100, "reason": "QC nearest",
                "instrument_family": self.FAMILY, "instrument_family_source": "format_default",
            },
        }
        self.start()

    def tearDown(self) -> None:
        self.stop()
        self.directory.cleanup()

    def _estimate(self, **body) -> dict:
        server._write_diagnostic_record(self.job_id, self.preparation, "running")
        server._write_diagnostic_record(self.job_id, self.preparation, "completed", exit_code=0)
        with patch.object(server, "JOBS", {}):
            status, response = self.post(
                "/api/agent/tuning/estimate",
                {"job_id": self.job_id, "manifest_path": str(self.manifest),
                 "target_peak_count_min": 3000, "target_peak_count_max": 6000, **body},
            )
        self.assertEqual(200, status, response)
        return response

    def test_the_response_and_the_manifest_both_carry_the_fallback(self) -> None:
        found = self._estimate()["estimate"]

        self.assertEqual((50, 10, 100, True, "no_coarse_step_in_range", True), (
            found["minimum_peak_height"], found["threshold_step"], found["coarse_threshold_step"],
            found["step_fallback"], found["fallback_reason"], found["within_target_range"],
        ))
        recorded = read_manifest(self.manifest)["peak_height_diagnostics"][-1]
        self.assertEqual({field: found[field] for field in NEW_FIELDS}, {field: recorded[field] for field in NEW_FIELDS})
        self.assertEqual(50, recorded["minimum_peak_height"])
        self.assertEqual(SELECTION_RULE, recorded["selection_rule"])
        self.assertEqual(("QTOF", "format_default"), (recorded["instrument_family"], recorded["instrument_family_source"]))

    def test_a_request_echoing_the_fallback_step_is_held_at_the_floor(self) -> None:
        found = self._estimate(threshold_step=10)["estimate"]

        self.assertEqual((10, 10, 10), (found["requested_threshold_step"], found["coarse_threshold_step"], found["threshold_step"]))
        self.assertEqual(50, found["minimum_peak_height"])

        found = self._estimate(threshold_step=1)["estimate"]

        self.assertEqual((1, 10), (found["requested_threshold_step"], found["threshold_step"]))
        self.assertEqual(50, found["minimum_peak_height"])


class TheEstimateEndpointHoldsAnFtDiagnosticAtItsFloor(TheEstimateEndpointRecordsTheFallback):
    """The endpoint passes the diagnostic's family to the estimator, so an FT diagnostic asked for steps of
    100 (an FT fallback's step, echoed back) cannot fall back to 10. Constructed: 3,500 peaks at 105 and 3,500
    at 155 put no multiple of 100 in range; a step of 10 would choose 150."""

    FAMILY = "Fourier-transform MS"

    @staticmethod
    def diagnostic_heights() -> list[float]:
        return [105.0] * 3500 + [155.0] * 3500 + [5.0] * 100

    def test_the_response_and_the_manifest_both_carry_the_fallback(self) -> None:
        found = self._estimate(threshold_step=100)["estimate"]

        self.assertEqual("Fourier-transform MS", found["instrument_family"])
        self.assertEqual((100, 100, 100), (found["coarse_threshold_step"], found["fine_threshold_step"], found["threshold_step"]))
        self.assertFalse(found["step_fallback"])
        self.assertEqual((100, 7000), (found["minimum_peak_height"], found["estimated_peak_count"]))
        self.assertFalse(found["within_target_range"])
        self.assertTrue(found["warnings"])

    def test_a_request_echoing_the_fallback_step_is_held_at_the_floor(self) -> None:
        found = self._estimate(threshold_step=10)["estimate"]

        self.assertEqual((10, 100), (found["requested_threshold_step"], found["threshold_step"]))
        self.assertEqual(0, found["minimum_peak_height"] % 100)


if __name__ == "__main__":
    unittest.main()

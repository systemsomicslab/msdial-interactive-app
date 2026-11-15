"""The attribution coverage gate (the author's decision of 2026-11-15).

ST002512's negative unit 9d1e8e566ba00ff0be25 admitted 5 blank injections of 110 declared rows through the
leading-identifier fallback, ran on them, and was marked done. A unit whose attribution gives an input to fewer than
ATTRIBUTION_COVERAGE_MINIMUM of its analytical sample rows of its own polarity, or to no biological sample's row, now
fails at its lease's attribute stage with the record; every other lease records the gate's reading.

Every name here is synthetic, shaped like the triage's examples. No raw data.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app.repository_analysis_rows import (
        ATTRIBUTION_ADMITTED_NO_SAMPLE,
        ATTRIBUTION_COVERAGE_BELOW_MINIMUM,
        ATTRIBUTION_COVERAGE_MINIMUM,
        ATTRIBUTION_COVERAGE_NOT_COMPUTED,
        ATTRIBUTION_COVERAGE_REFUSALS,
        ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY,
        ATTRIBUTION_ROWS_BELOW_MINIMUM_WARNING,
        ATTRIBUTION_SAMPLE_COVERAGE_BELOW_MINIMUM,
        _coverage_file_type,
        attribution_coverage,
    )
    from msdial_app.repository_reanalysis import AttributionCoverageError, read_manifest

from test_replicate_injections import UNIT_SCOPED, _st001264, _Workspace


# ---- The coverage gate -----------------------------------------------------------------------------------------


class TheCoverageGate(_Workspace):
    def test_the_minimum_is_half(self) -> None:
        self.assertEqual(0.5, ATTRIBUTION_COVERAGE_MINIMUM)

    def test_an_st002512_shaped_unit_that_admits_a_few_rows_fails_at_the_attribute_stage_with_the_record(self) -> None:
        """Six negative rows; the archive holds the file of one. The lease used to run on it."""
        rows = [(f"S{index}", f"Neg_S{index}.raw; Pos_S{index}.raw") for index in range(1, 7)]
        rows.append(("Bk_1", "-; Pos_Bk_1.raw"))
        project, payloads = _st001264(["Neg_S1.raw", "Pos_S1.raw", "Pos_S2.raw"], rows)
        project.ion_mode = "Negative"

        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        self.assertIn(ATTRIBUTION_COVERAGE_BELOW_MINIMUM, str(raised.exception))
        coverage = raised.exception.coverage
        self.assertEqual(("refused", 6, 1, 1, 0.1667), (coverage["status"], coverage["declared_rows"],
                                                        coverage["admitted_rows"], coverage["rows_of_another_unit"],
                                                        coverage["ratio"]))
        self.assertEqual(5, coverage["rows_without_input_count"])
        self.assertEqual([{"input": "Neg_S1.raw", "sample_id": "S1", "sample_row_index": 0, "file_type": "Sample",
                           "admitted_by": "exact"}], coverage["inputs"])

    def test_the_failed_lease_says_why_in_its_manifest(self) -> None:
        rows = [(f"S{index}", f"S{index}.raw") for index in range(1, 6)]
        project, payloads = _st001264(["S1.raw"], rows)
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        manifests = list(self.root.rglob("run-manifest.json"))
        self.assertEqual(1, len(manifests))
        manifest = read_manifest(manifests[0])
        self.assertEqual(("download_failed", "refused"),
                         (manifest["status"], manifest["attribution_coverage"]["status"]))
        self.assertEqual(manifest["download_failure"], raised.exception.download_failure)
        failure = raised.exception.download_failure
        self.assertEqual(("attribute", ATTRIBUTION_COVERAGE_BELOW_MINIMUM, "AttributionCoverageError", False),
                         (failure["stage"], failure["reason_code"], failure["error_type"], failure["retryable"]))
        # A failure, not an exclusion: no disposition; and never retried as it stands (the same attribution repeats).
        self.assertNotIn("disposition", failure)
        self.assertIn(failure["reason_code"], ATTRIBUTION_COVERAGE_REFUSALS)
        self.assertEqual((5, 1, 0.2), (failure["attribution_coverage"]["declared_rows"],
                                       failure["attribution_coverage"]["admitted_rows"],
                                       failure["attribution_coverage"]["ratio"]))

    def test_a_unit_that_admits_only_qcs_is_refused_whatever_its_ratio(self) -> None:
        rows = [("QC_01", "QC_01.raw"), ("QC_02", "QC_02.raw"), ("A1", "A1.raw")]
        project, payloads = _st001264(["QC_01.raw", "QC_02.raw"], rows)
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        coverage = raised.exception.coverage
        self.assertEqual((ATTRIBUTION_ADMITTED_NO_SAMPLE, 0.6667, 1, 0),
                         (coverage["reason"], coverage["ratio"], coverage["sample_rows_declared"],
                          coverage["sample_rows_admitted"]))

    def test_blank_rows_are_no_analytical_rows(self) -> None:
        rows = [("S1", "S1.raw"), ("S2", "S2.raw"), ("Blank1", "Blank1.raw"), ("Blank2", "Blank2.raw"),
                ("Blank3", "Blank3.raw")]
        project, payloads = _st001264(["S1.raw"], rows)
        manifest = self.lease(project, payloads)
        coverage = manifest["attribution_coverage"]
        self.assertEqual(("passed", 3, 2, 1, 0.5), (coverage["status"], coverage["blank_rows"],
                                                    coverage["declared_rows"], coverage["admitted_rows"],
                                                    coverage["ratio"]))
        attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
        self.assertEqual({"status": "passed", "reason": "", "declared_rows": 2, "admitted_rows": 1, "ratio": 0.5,
                          "row_ratio": 0.5, "sample_ratio": 0.5},
                         attribute["attribution_coverage"])

    def test_a_unit_whose_rows_are_all_another_polaritys_is_refused_and_one_with_only_blanks_is_not_judged(self) -> None:
        project, payloads = _st001264(["S1.raw"], [("S1", "S1.raw")])
        manifest = self.lease(project, payloads)
        manifest["project"]["sample_metadata"] = [{"sample_id": "S1", "raw_file": "S1_neg.raw"}]
        coverage = attribution_coverage(manifest)
        # Rows all of another polarity while inputs were admitted: a Catalog mis-assignment, refused (review of 0.5.67).
        self.assertEqual(("refused", ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY, 1),
                         (coverage["status"], coverage["reason"], coverage["rows_of_another_unit"]))
        self.assertEqual([{"sample_row_index": 0, "sample_id": "S1", "reason": "raw_file_states_another_polarity"}],
                         coverage["rows_of_another_unit_listed"])
        # A unit whose own rows are blanks only is recorded and not judged.
        manifest["project"]["sample_metadata"] = [{"sample_id": "Blank1", "raw_file": "Blank1.raw"}]
        self.assertEqual(("not_evaluated", "no_analytical_rows_of_the_unit_polarity"),
                         (attribution_coverage(manifest)["status"], attribution_coverage(manifest)["reason"]))
        manifest["project"]["sample_metadata"] = []
        self.assertEqual(("not_evaluated", "no_sample_rows"),
                         (attribution_coverage(manifest)["status"], attribution_coverage(manifest)["reason"]))

    def test_unattributed_inputs_of_a_unit_scoped_archive_count(self) -> None:
        """The user's decision of 2026-10-07: they are the unit's admitted analysis files, with no row."""
        project, payloads = _st001264(["x_A.raw", "x_B.raw"], [("Sample1", "Sample1")])
        project.download_scope = dict(UNIT_SCOPED)
        manifest = self.lease(project, payloads)
        coverage = manifest["attribution_coverage"]
        self.assertEqual(("passed", 0, 2, 1.0), (coverage["status"], coverage["admitted_rows"],
                                                 coverage["unattributed_inputs_counted"], coverage["ratio"]))
        # Its rows alone are below the minimum: passed, but never silently (the author's ruling is open).
        self.assertEqual((0.0, [ATTRIBUTION_ROWS_BELOW_MINIMUM_WARNING]), (coverage["row_ratio"], coverage["warnings"]))
        self.assertIn(ATTRIBUTION_ROWS_BELOW_MINIMUM_WARNING, manifest["warnings"])

    def test_a_joined_cells_rows_are_counted_by_the_unit_and_the_other_units_rows_are_not(self) -> None:
        """R1's ST003720 shape: three rows, each naming both polarities; the positive unit admits all three."""
        members = ["NEG_D1.raw", "POS_D1.raw", "NEG_D2.raw", "POS_D2.raw", "NEG_D3.raw", "POS_D3.raw"]
        rows = [(f"DMSO-{index}", f"NEG_D{index}.raw POS_D{index}.raw") for index in (1, 2, 3)]
        rows.append(("Bk_1", "-; NEG_Bk_1.raw"))
        project, payloads = _st001264(members, rows)
        coverage = self.lease(project, payloads)["attribution_coverage"]

        self.assertEqual(("passed", 3, 3, 1.0, 1), (coverage["status"], coverage["declared_rows"],
                                                    coverage["admitted_rows"], coverage["ratio"],
                                                    coverage["rows_of_another_unit"]))
        self.assertEqual({"exact": 3}, coverage["admitted_by"])
        self.assertEqual({"rows": 4, "rows_of_another_unit": 1, "refused": [], "refused_count": 0},
                         coverage["joined_raw_files"])

    def test_a_row_whose_sample_id_states_another_polarity_is_another_units(self) -> None:
        """R6's ST004757 shape: rows name only .RAW, and b1_neg1 is the negative unit's row."""
        project, payloads = _st001264(["b1_pos1.raw", "b1_pos2.raw", "b1_neg1.raw"],
                                      [("b1_pos1", ".RAW"), ("b1_pos2", ".RAW"), ("b1_neg1", ".RAW")])
        coverage = self.lease(project, payloads)["attribution_coverage"]
        self.assertEqual(("passed", 1, 2, 2), (coverage["status"], coverage["rows_of_another_unit"],
                                               coverage["declared_rows"], coverage["admitted_rows"]))
        self.assertEqual({"sample_id_member_stem": 2}, coverage["admitted_by"])


# ---- The review of 0.5.67: what the gate must never pass ----------------------------------------------------------


class TheGateReadsBlanksQcsAndSamplesApart(_Workspace):
    def test_blank_and_qc_names_in_common_shapes_are_no_samples(self) -> None:
        """ST002512's admitted blanks were Neg_bk_1.raw and Bk_1(1).raw, which file_type_for typed Sample."""
        for name in ("Bk_1", "bk_12", "BK1", "Neg_bk_1.raw", "Bk_1(1).raw", "Blk_01", "Blank1"):
            self.assertEqual("Blank", _coverage_file_type(name), name)
        for name in ("QC1", "QC01", "QC_01", "IQC_Neg_Start", "Pool_3", "SRM1950", "NIST_2"):
            self.assertEqual("QC", _coverage_file_type(name), name)
        for name in ("S1", "Bkg1", "Ctrl1- Fecal", "Poolside7", "Dobk_1"):
            self.assertEqual("Sample", _coverage_file_type(name), name)

    def test_a_unit_that_admits_only_its_bk_blanks_is_refused(self) -> None:
        """Four samples and four Bk_n blanks, only the blanks admitted: 4 of 8 passed before the review."""
        rows = [(f"A{index}", f"A{index}.raw") for index in range(1, 5)]
        rows += [(f"Bk_{index}", f"Neg_bk_{index}.raw") for index in range(1, 5)]
        project, payloads = _st001264([f"Neg_bk_{index}.raw" for index in range(1, 5)], rows)
        project.ion_mode = "Negative"
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        coverage = raised.exception.coverage
        self.assertEqual((ATTRIBUTION_COVERAGE_BELOW_MINIMUM, 4, 4, 0), (coverage["reason"], coverage["blank_rows"],
                                                                      coverage["declared_rows"], coverage["admitted_rows"]))

    def test_qc_rows_never_make_up_for_missing_samples(self) -> None:
        """Four QCs and one of four samples: 5 of 8 rows, but a quarter of the samples."""
        rows = [(f"QC0{index}", f"QC0{index}.raw") for index in range(1, 5)]
        rows += [(f"A{index}", f"A{index}.raw") for index in range(1, 5)]
        project, payloads = _st001264([*(f"QC0{index}.raw" for index in range(1, 5)), "A1.raw"], rows)
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        coverage = raised.exception.coverage
        self.assertEqual((ATTRIBUTION_SAMPLE_COVERAGE_BELOW_MINIMUM, 0.625, 0.25),
                         (coverage["reason"], coverage["ratio"], coverage["sample_ratio"]))
        self.assertIn(coverage["reason"], ATTRIBUTION_COVERAGE_REFUSALS)


class TheGateNeverPassesForWantOfAVerdict(_Workspace):
    def test_rows_that_cannot_be_built_refuse_the_unit(self) -> None:
        project, payloads = _st001264(["S1.raw"], [("S1", "S1.raw")])
        manifest = self.lease(project, payloads)
        with patch("msdial_app.repository_analysis_rows.build_repository_analysis_rows",
                   side_effect=ValueError("broken row")):
            coverage = attribution_coverage(manifest)
        self.assertEqual(("refused", ATTRIBUTION_COVERAGE_NOT_COMPUTED), (coverage["status"], coverage["reason"]))
        manifest["project"]["sample_metadata"] = []
        with patch("msdial_app.repository_analysis_rows.build_repository_analysis_rows",
                   side_effect=ValueError("broken row")):
            self.assertEqual("not_evaluated", attribution_coverage(manifest)["status"])


class TheGatesDenominator(_Workspace):
    def test_a_control_sample_id_or_name_is_the_units_own_row(self) -> None:
        """PR #58's exemption: Neg_Ctrl_1 in a positive unit names a negative control, not a polarity."""
        project, payloads = _st001264(["NC1.raw", "Neg_Ctrl_2.raw", "S1.raw"],
                                      [("Neg_Ctrl_1", "NC1.raw"), ("NC2", "Neg_Ctrl_2.raw"), ("S1", "S1.raw")])
        coverage = self.lease(project, payloads)["attribution_coverage"]
        self.assertEqual(("passed", 0, 3, 3), (coverage["status"], coverage["rows_of_another_unit"],
                                               coverage["declared_rows"], coverage["admitted_rows"]))

    def test_a_row_of_another_chromatography_is_another_units_where_the_rows_state_two(self) -> None:
        """ST002330's reversed-phase unit lists HILIC-RN1-neg.mzXML beside C18-RN1-neg.mzXML."""
        project, payloads = _st001264(["C18-R1.raw"], [("R1", "C18-R1.raw"), ("H1", "HILIC-H1.raw")])
        manifest = self.lease(project, payloads)
        manifest["project"]["separation"] = "Reversed phase"
        coverage = attribution_coverage(manifest)
        self.assertEqual((1, 1, 1.0), (coverage["declared_rows"], coverage["rows_of_another_unit"], coverage["ratio"]))
        self.assertEqual([{"sample_row_index": 1, "sample_id": "H1", "reason": "raw_file_states_another_chromatography"}],
                         coverage["rows_of_another_unit_listed"])

    def test_a_glued_sample_id_is_read_only_for_a_row_that_names_no_file(self) -> None:
        """b1_neg1 with raw_file .RAW is the negative unit's; Neg1 with a file of its own is this unit's row."""
        project, payloads = _st001264(["Neg1.raw", "S1.raw"], [("Neg1", "Neg1.raw"), ("S1", "S1.raw")])
        coverage = self.lease(project, payloads)["attribution_coverage"]
        self.assertEqual((0, 2), (coverage["rows_of_another_unit"], coverage["declared_rows"]))


if __name__ == "__main__":
    unittest.main()

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
        ATTRIBUTION_ADMITTED_ANOTHER_UNITS_INPUTS,
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
    from msdial_app.repository_reanalysis import AttributionCoverageError, name_chromatographies, read_manifest

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


class NoBlankOrQcRowExcusesAnotherUnitsRows(_Workspace):
    """The independent check of 0.5.67: one blank or QC row beside rows of another polarity let a positive unit run on
    their files. With no biological sample row of its own polarity, rows of another unit and inputs admitted, the unit
    is refused, whatever blank, QC or standard rows it also lists."""

    NEGATIVE_ROWS = [("S1", "S1_neg.raw"), ("S2", "S2_neg.raw"), ("S3", "S3_neg.raw")]
    NEGATIVE_FILES = ["S1_neg.raw", "S2_neg.raw", "S3_neg.raw"]

    def refused(self, members: list[str], rows: list[tuple[str, str]], scope: dict | None = None) -> dict:
        project, payloads = _st001264(members, rows)
        if scope:
            project.download_scope = dict(scope)
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        self.assertIn(ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY, str(raised.exception))
        return raised.exception.coverage

    def test_a_blank_row_beside_another_polaritys_rows_is_refused(self) -> None:
        coverage = self.refused([*self.NEGATIVE_FILES, "Blank1.raw"], [*self.NEGATIVE_ROWS, ("Blank1", "Blank1.raw")])
        self.assertEqual(("refused", ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY, 3, 1, 0, 0),
                         (coverage["status"], coverage["reason"], coverage["rows_of_another_unit"],
                          coverage["blank_rows"], coverage["declared_rows"], coverage["sample_rows_declared"]))

    def test_a_qc_row_beside_another_polaritys_rows_is_refused(self) -> None:
        coverage = self.refused([*self.NEGATIVE_FILES, "QC1.raw"], [*self.NEGATIVE_ROWS, ("QC1", "QC1.raw")])
        self.assertEqual(("refused", ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY, 3, 0, 1, 0),
                         (coverage["status"], coverage["reason"], coverage["rows_of_another_unit"],
                          coverage["blank_rows"], coverage["declared_rows"], coverage["sample_rows_declared"]))

    def test_a_unit_scoped_archive_with_a_blank_row_is_refused(self) -> None:
        coverage = self.refused(["S1_neg.raw", "S2_neg.raw", "Blank1.raw", "Blank2.raw"],
                                [("S1", "S1_neg.raw"), ("S2", "S2_neg.raw"), ("Blank1", "Blank1.raw")],
                                scope=UNIT_SCOPED)
        self.assertEqual(("refused", 2, 1, 0), (coverage["status"], coverage["rows_of_another_unit"],
                                                coverage["blank_rows"], coverage["sample_rows_declared"]))

    def test_own_polarity_sample_rows_beside_a_blank_and_another_polaritys_row_pass(self) -> None:
        """The negative control: the unit's own samples are there and the other polarity's file is not admitted, so its
        row is merely not counted."""
        project, payloads = _st001264(["S1_pos.raw", "S2_pos.raw", "Blank1.raw"],
                                      [("S1", "S1_pos.raw"), ("S2", "S2_pos.raw"), ("S3", "S3_neg.raw"),
                                       ("Blank1", "Blank1.raw")])
        coverage = self.lease(project, payloads)["attribution_coverage"]
        self.assertEqual(("passed", 1, 1, 2, 2, 2, 0), (coverage["status"], coverage["rows_of_another_unit"],
                                                        coverage["blank_rows"], coverage["declared_rows"],
                                                        coverage["admitted_rows"], coverage["sample_rows_declared"],
                                                        coverage["inputs_of_another_units_rows"]))
        self.assertEqual(["Blank1.raw", "S1_pos.raw", "S2_pos.raw"], sorted(item["input"] for item in coverage["inputs"]))

    def test_with_no_input_admitted_the_unit_keeps_its_own_outcome(self) -> None:
        project, payloads = _st001264(["S1.raw"], [("S1", "S1.raw")])
        manifest = self.lease(project, payloads)
        manifest["project"]["sample_metadata"] = [{"sample_id": "S1", "raw_file": "S1_neg.raw"},
                                                  {"sample_id": "Blank1", "raw_file": "Blank1.raw"}]
        self.assertEqual(ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY, attribution_coverage(manifest)["reason"])
        with patch("msdial_app.repository_analysis_rows.build_repository_analysis_rows",
                   return_value={"rows": [], "failures": []}):
            coverage = attribution_coverage(manifest)
        self.assertEqual(("not_evaluated", "no_analytical_rows_of_the_unit_polarity"),
                         (coverage["status"], coverage["reason"]))

    def test_qc_and_blank_inputs_of_a_unit_scoped_archive_are_never_counted(self) -> None:
        """Four sample rows, one admitted; a QC and a blank member taken unattributed must not lift it to the minimum."""
        project, payloads = _st001264(["A1.raw", "QC_x1.raw", "QC_x2.raw", "Blank_x1.raw"],
                                      [(f"A{index}", f"A{index}.raw") for index in range(1, 5)])
        project.download_scope = dict(UNIT_SCOPED)
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        coverage = raised.exception.coverage
        self.assertEqual((ATTRIBUTION_COVERAGE_BELOW_MINIMUM, 3, 0, 0.25),
                         (coverage["reason"], coverage["unattributed_inputs"], coverage["unattributed_inputs_counted"],
                          coverage["ratio"]))


class NoUnitRunsOnAnotherUnitsFiles(_Workspace):
    """Verification of 0.5.67's fix: the gate counted only the unit's own rows, so one admitted sample row of its own
    polarity hid any number of another polarity's inputs, each admitted and run as the unit's. A file paired with a row
    of another unit now refuses the unit, whatever its own samples; a row of another unit whose file was not admitted
    refuses nothing."""

    def lease_refused(self, members: list[str], rows: list[tuple[str, str]], scope: dict | None = None) -> dict:
        project, payloads = _st001264(members, rows)
        if scope:
            project.download_scope = dict(scope)
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        self.assertIn(ATTRIBUTION_ADMITTED_ANOTHER_UNITS_INPUTS, str(raised.exception))
        return raised.exception.coverage

    def test_one_positive_sample_row_never_carries_ten_negative_files(self) -> None:
        rows = [("P1", "P1_pos.raw"), ("P2", "P2_pos.raw"), *((f"S{index}", f"S{index}_neg.raw") for index in range(10))]
        members = ["P1_pos.raw", *(f"S{index}_neg.raw" for index in range(10))]
        coverage = self.lease_refused(members, rows)
        self.assertEqual(("refused", ATTRIBUTION_ADMITTED_ANOTHER_UNITS_INPUTS, 10, 10, 0.5),
                         (coverage["status"], coverage["reason"], coverage["rows_of_another_unit"],
                          coverage["inputs_of_another_units_rows"], coverage["ratio"]))
        self.assertIn(ATTRIBUTION_ADMITTED_ANOTHER_UNITS_INPUTS, ATTRIBUTION_COVERAGE_REFUSALS)

    def test_a_full_ratio_never_excuses_another_units_files(self) -> None:
        rows = [("P1", "P1_pos.raw"), *((f"S{index}", f"S{index}_neg.raw") for index in range(5))]
        members = ["P1_pos.raw", *(f"S{index}_neg.raw" for index in range(5))]
        coverage = self.lease_refused(members, rows)
        self.assertEqual((1.0, 5), (coverage["ratio"], coverage["inputs_of_another_units_rows"]))

    def test_a_unit_scoped_archive_never_carries_another_units_files(self) -> None:
        rows = [("P1", "P1_pos.raw"), *((f"S{index}", f"S{index}_neg.raw") for index in range(5))]
        members = ["P1_pos.raw", *(f"S{index}_neg.raw" for index in range(5))]
        coverage = self.lease_refused(members, rows, scope=UNIT_SCOPED)
        self.assertEqual((1.0, 5), (coverage["ratio"], coverage["inputs_of_another_units_rows"]))

    def test_another_polaritys_file_beside_own_samples_and_a_blank_is_refused(self) -> None:
        """The former negative control's fixture: S3_neg.raw in the archive was admitted and run by the positive unit."""
        coverage = self.lease_refused(["S1_pos.raw", "S2_pos.raw", "S3_neg.raw", "Blank1.raw"],
                                      [("S1", "S1_pos.raw"), ("S2", "S2_pos.raw"), ("S3", "S3_neg.raw"),
                                       ("Blank1", "Blank1.raw")])
        self.assertEqual((1, 2, 2), (coverage["inputs_of_another_units_rows"], coverage["sample_rows_declared"],
                                     coverage["sample_rows_admitted"]))
        self.assertEqual([("S3_neg.raw", 2)], [(item["input"], item["sample_row_index"]) for item in coverage["inputs"]
                                               if item["sample_row_index"] == 2])

    def test_a_negative_unit_never_carries_positive_files(self) -> None:
        project, payloads = _st001264(["N1_neg.raw", "N2_neg.raw", "P1_pos.raw"],
                                      [("N1", "N1_neg.raw"), ("N2", "N2_neg.raw"), ("P1", "P1_pos.raw")])
        project.ion_mode = "Negative"
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        self.assertEqual(ATTRIBUTION_ADMITTED_ANOTHER_UNITS_INPUTS, raised.exception.coverage["reason"])

    def stray_row_passes(self, qc: str) -> None:
        rows = [(f"{qc}1", f"{qc}1_pos.raw"), (f"{qc}2", f"{qc}2_pos.raw"), ("X", "Blank_neg.raw")]
        project, payloads = _st001264([f"{qc}1_pos.raw", f"{qc}2_pos.raw"], rows)
        coverage = self.lease(project, payloads)["attribution_coverage"]
        self.assertEqual(("passed", 1, 0, 2, 2, 1.0),
                         (coverage["status"], coverage["rows_of_another_unit"],
                          coverage["inputs_of_another_units_rows"], coverage["declared_rows"],
                          coverage["admitted_rows"], coverage["ratio"]))

    def test_a_stray_row_of_another_unit_whose_file_was_not_admitted_refuses_no_qc_only_unit(self) -> None:
        """QC-only own rows beside one other-polarity row absent from the archive run on their own files, as before."""
        self.stray_row_passes("QC")

    def test_a_stray_row_of_another_unit_whose_file_was_not_admitted_refuses_no_pooled_unit(self) -> None:
        """Pool1 and Pool2 are typed QC: the same holds for pooled samples."""
        self.stray_row_passes("Pool")

    def test_a_stray_row_of_another_unit_whose_file_was_admitted_refuses_a_qc_only_unit(self) -> None:
        project, payloads = _st001264(["Pool1_pos.raw", "Pool2_pos.raw", "X_neg.raw"],
                                      [("Pool1", "Pool1_pos.raw"), ("Pool2", "Pool2_pos.raw"), ("X", "X_neg.raw")])
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        coverage = raised.exception.coverage
        self.assertEqual((ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY, 1, 1),
                         (coverage["reason"], coverage["inputs_of_another_units_rows"], coverage["rows_of_another_unit"]))


# ---- Verification of f0084e2 -------------------------------------------------------------------------------------


class NoUnitRunsOnItsBlanksOrQcsAlone(_Workspace):
    """f0084e2 refused a unit with no sample row of its own only where it admitted another unit's file, so a positive unit
    whose every biological row was the other polarity's, with those files absent, ran on its blanks or QCs alone (the
    ST002512 defect). A row of another unit that is a biological sample's now refuses such a unit whether or not its file
    was admitted; a stray blank or QC row of another unit still refuses nothing."""

    NEGATIVE_ROWS = [(f"S{index}", f"S{index}_neg.raw") for index in range(1, 6)]

    def refused(self, members: list[str], rows: list[tuple[str, str]], reason: str, scope: dict | None = None) -> dict:
        project, payloads = _st001264(members, rows)
        if scope:
            project.download_scope = dict(scope)
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        self.assertEqual(reason, raised.exception.coverage["reason"])
        return raised.exception.coverage

    def test_blank_rows_beside_another_polaritys_sample_rows_whose_files_are_absent_are_refused(self) -> None:
        coverage = self.refused(["Blank1.raw", "Blank2.raw"],
                                [*self.NEGATIVE_ROWS, ("Blank1", "Blank1.raw"), ("Blank2", "Blank2.raw")],
                                ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY)
        self.assertEqual((5, 0, 2), (coverage["rows_of_another_unit"], coverage["inputs_of_another_units_rows"],
                                     len(coverage["inputs"])))

    def test_the_same_in_a_unit_scoped_archive(self) -> None:
        self.refused(["Blank1.raw", "Blank2.raw"], [*self.NEGATIVE_ROWS, ("Blank1", "Blank1.raw"),
                                                    ("Blank2", "Blank2.raw")],
                     ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY, scope=UNIT_SCOPED)

    def test_qc_rows_beside_another_polaritys_sample_rows_whose_files_are_absent_are_refused(self) -> None:
        self.refused(["QC1.raw", "QC2.raw"], [*self.NEGATIVE_ROWS, ("QC1", "QC1.raw"), ("QC2", "QC2.raw")],
                     ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY)
        self.refused(["QC1.raw", "Blank1.raw"], [*self.NEGATIVE_ROWS, ("QC1", "QC1.raw"), ("Blank1", "Blank1.raw")],
                     ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY)

    def test_a_unit_that_admits_only_blanks_is_refused(self) -> None:
        coverage = self.refused(["Blank1.raw", "Blank2.raw"], [("Blank1", "Blank1.raw"), ("Blank2", "Blank2.raw")],
                                ATTRIBUTION_ADMITTED_NO_SAMPLE)
        self.assertEqual((2, 0, 0), (coverage["blank_rows"], coverage["declared_rows"], coverage["rows_of_another_unit"]))

    def test_own_blank_and_qc_rows_beside_a_stray_blank_row_of_another_unit_pass(self) -> None:
        """The stray row's file is absent: the unit's own Blank1 and QC1 are its own rows' files, not undeclared ones."""
        project, payloads = _st001264(["QC1_pos.raw", "Blank1.raw"],
                                      [("QC1", "QC1_pos.raw"), ("Blank1", "Blank1.raw"), ("X", "Blank_neg.raw")])
        coverage = self.lease(project, payloads)["attribution_coverage"]
        self.assertEqual(("passed", 1, 0, 1, 1), (coverage["status"], coverage["rows_of_another_unit"],
                                                  coverage["inputs_of_another_units_rows"], coverage["blank_rows"],
                                                  coverage["declared_rows"]))

    def test_an_unattributed_sample_member_beside_a_stray_row_refuses_a_qc_only_unit(self) -> None:
        """f0084e2's 'or an unattributed member' half: Z9.raw is admitted with no row of the unit's own."""
        coverage = self.refused(["QC1_pos.raw", "QC2_pos.raw", "Z9.raw"],
                                [("QC1", "QC1_pos.raw"), ("QC2", "QC2_pos.raw"), ("X", "Blank_neg.raw")],
                                ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY, scope=UNIT_SCOPED)
        self.assertEqual((1, 0), (coverage["unattributed_inputs"], coverage["inputs_of_another_units_rows"]))

    def test_an_unattributed_qc_member_beside_a_stray_row_does_not(self) -> None:
        """A QC member counts nowhere, as a declared QC row's file does not: the unit runs on its QCs as before."""
        project, payloads = _st001264(["QC1_pos.raw", "QC2_pos.raw", "QC_x9.raw"],
                                      [("QC1", "QC1_pos.raw"), ("QC2", "QC2_pos.raw"), ("X", "Blank_neg.raw")])
        project.download_scope = dict(UNIT_SCOPED)
        coverage = self.lease(project, payloads)["attribution_coverage"]
        self.assertEqual(("passed", 1, 0), (coverage["status"], coverage["unattributed_inputs"],
                                            coverage["unattributed_inputs_counted"]))


class AnInputIsAnotherUnitsByItsOwnEvidence(_Workspace):
    """f0084e2 refused every input paired with a row read as another unit's, by the row's tokens: H3.raw, whose own name
    states nothing, was refused because its row's sample id was HIV_neg_1. An input is now another unit's only where its
    own name (or, its name silent, its folders) states another polarity or chromatography, or its row's own attribute
    does."""

    def test_a_sample_ids_pos_or_neg_never_makes_a_silent_file_another_units(self) -> None:
        rows = [("HIV_pos_1", "H1.raw"), ("HIV_pos_2", "H2.raw"), ("HIV_neg_1", "H3.raw"), ("HIV_neg_2", "H4.raw")]
        project, payloads = _st001264(["H1.raw", "H2.raw", "H3.raw", "H4.raw"], rows)
        coverage = self.lease(project, payloads)["attribution_coverage"]
        self.assertEqual(("passed", 2, 0, 4), (coverage["status"], coverage["rows_of_another_unit"],
                                               coverage["inputs_of_another_units_rows"], len(coverage["inputs"])))

    def test_its_folders_speak_where_its_name_is_silent(self) -> None:
        rows = [("HIV_pos_1", "H1.raw"), ("HIV_pos_2", "H2.raw"), ("HIV_neg_1", "H3.raw")]
        project, payloads = _st001264(["POS/H1.raw", "POS/H2.raw", "NEG/H3.raw"], rows)
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        self.assertEqual((ATTRIBUTION_ADMITTED_ANOTHER_UNITS_INPUTS, 1),
                         (raised.exception.coverage["reason"], raised.exception.coverage["inputs_of_another_units_rows"]))

    def test_a_files_own_pos_or_neg_token_still_refuses_the_unit(self) -> None:
        """Pinned, conservative: ER_neg_1.raw may name an ER-negative tumour run in positive mode, but a name's own
        polarity token is read as its polarity, as PR #58's census found it to be in every Catalog case (19 units).
        Raw-header preflight runs after this gate, so the gate cannot ask the header; the author's to rule."""
        rows = [("ER_pos_1", "ER_pos_1.raw"), ("ER_pos_2", "ER_pos_2.raw"), ("ER_neg_1", "ER_neg_1.raw"),
                ("ER_neg_2", "ER_neg_2.raw")]
        project, payloads = _st001264([raw for _sample, raw in rows], rows)
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        self.assertEqual((ATTRIBUTION_ADMITTED_ANOTHER_UNITS_INPUTS, 2),
                         (raised.exception.coverage["reason"], raised.exception.coverage["inputs_of_another_units_rows"]))
        # And a negative control in a listing that names its files by polarity is read as the negative run.
        project, payloads = _st001264(["S1_pos.raw", "S2_pos.raw", "Neg_Ctrl_1.raw"],
                                      [("S1", "S1_pos.raw"), ("S2", "S2_pos.raw"), ("NC1", "Neg_Ctrl_1.raw")])
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        self.assertEqual(ATTRIBUTION_ADMITTED_ANOTHER_UNITS_INPUTS, raised.exception.coverage["reason"])


class TheGateReadsEveryStatementOfAChromatography(_Workspace):
    """Stage3b's done units that ran another Catalog unit's files although f0084e2 passed them: ST002796's C18neg names
    and ST003964's glued ...xHILICneg_ tokens, which no reader read, and ST004606's rows, whose names state nothing and
    whose attribute "Chromatography ion mode" says HILIC POS or RP NEG. Synthetic names of those shapes."""

    def test_a_column_glued_to_a_polarity_states_the_chromatography(self) -> None:
        self.assertEqual({"RP"}, name_chromatographies("A_01_C18neg.mzML"))
        self.assertEqual({"RP"}, name_chromatographies("C18pos/A_01.mzML"))
        self.assertEqual({"HILIC"}, name_chromatographies("A_01_HILICneg.mzML"))
        self.assertEqual({"HILIC"}, name_chromatographies("A_01xHILICneg_B.raw"))
        self.assertEqual({"RP"}, name_chromatographies("A_01xRPneg_B.raw"))
        self.assertEqual({"RP"}, name_chromatographies("A_7rppos.raw"))
        for name in ("Sharpneg_1.raw", "Harppos_2.raw", "Grc8neg.raw", "S1_neg.raw"):
            self.assertEqual(set(), name_chromatographies(name), name)

    def lease_then_judge(self, members: list[str], rows: list[tuple[str, str]], separation: str,
                         values: list[dict] | None = None, ion_mode: str = "Positive") -> dict:
        project, payloads = _st001264(members, rows)
        project.ion_mode = ion_mode
        if values:
            for sample, value in zip(project.sample_metadata, values):
                sample["values"] = dict(value)
        manifest = self.lease(project, payloads)
        manifest["project"]["separation"] = separation
        return attribution_coverage(manifest)

    def test_a_c18_unit_carrying_hilic_files_is_refused(self) -> None:
        rows = [("A_01", "A_01_C18neg.mzML"), ("A_02", "A_02_C18neg.mzML"), ("B_01", "B_01_HILICneg.mzML"),
                ("B_02", "B_02_HILICneg.mzML")]
        coverage = self.lease_then_judge([raw for _sample, raw in rows], rows, "Reversed phase", ion_mode="Negative")
        self.assertEqual(("refused", ATTRIBUTION_ADMITTED_ANOTHER_UNITS_INPUTS, 2, 2),
                         (coverage["status"], coverage["reason"], coverage["rows_of_another_unit"],
                          coverage["inputs_of_another_units_rows"]))

    def test_a_glued_hilic_unit_carrying_rp_files_is_refused(self) -> None:
        rows = [("H1", "R_1xHILICneg_K.raw"), ("H2", "R_2xHILICneg_K.raw"), ("P1", "R_3xRPneg_K.raw")]
        coverage = self.lease_then_judge([raw for _sample, raw in rows], rows, "HILIC", ion_mode="Negative")
        self.assertEqual((ATTRIBUTION_ADMITTED_ANOTHER_UNITS_INPUTS, 1),
                         (coverage["reason"], coverage["inputs_of_another_units_rows"]))

    def test_a_reversed_phase_part_whose_every_row_states_hilic_is_refused(self) -> None:
        """ST002796's RP unit's aif-pos part: three QC rows, every one named ..._HILICpos, run as the RP unit's."""
        rows = [(f"QCX_0{index}_HILICpos", f"QCX_0{index}_HILICpos.mzML") for index in range(1, 4)]
        coverage = self.lease_then_judge([raw for _sample, raw in rows], rows, "Reversed phase")
        self.assertEqual(("refused", ATTRIBUTION_NO_ROWS_OF_THE_UNIT_POLARITY, 3, 3),
                         (coverage["status"], coverage["reason"], coverage["rows_of_another_unit"],
                          coverage["inputs_of_another_units_rows"]))

    def test_rows_that_state_only_rp_by_name_are_a_hilic_units_own(self) -> None:
        """"RP" alone may be a sample's code: a HILIC unit whose rows all say RP is not judged by it."""
        rows = [("RP1", "S_RP_1.raw"), ("RP2", "S_RP_2.raw")]
        coverage = self.lease_then_judge([raw for _sample, raw in rows], rows, "HILIC")
        self.assertEqual(("passed", 0), (coverage["status"], coverage["rows_of_another_unit"]))

    def test_a_rows_chromatography_attribute_is_its_own_statement(self) -> None:
        rows = [(f"F{index}", f"F{index}.raw") for index in range(1, 5)]
        values = [{"Chromatography ion mode": mode} for mode in ("HILIC POS", "HILIC POS", "RP POS", "RP POS")]
        coverage = self.lease_then_judge([raw for _sample, raw in rows], rows, "HILIC", values)
        self.assertEqual(("refused", ATTRIBUTION_ADMITTED_ANOTHER_UNITS_INPUTS, 2, 2),
                         (coverage["status"], coverage["reason"], coverage["rows_of_another_unit"],
                          coverage["inputs_of_another_units_rows"]))
        self.assertEqual({"row_attribute_states_another_chromatography"},
                         {item["reason"] for item in coverage["rows_of_another_unit_listed"]})

    def test_a_rows_polarity_attribute_is_its_own_statement(self) -> None:
        rows = [(f"F{index}", f"F{index}.raw") for index in range(1, 5)]
        values = [{"Parameter Value[Scan polarity]": mode} for mode in ("positive", "positive", "negative", "negative")]
        project, payloads = _st001264([raw for _sample, raw in rows], rows)
        for sample, value in zip(project.sample_metadata, values):
            sample["values"] = dict(value)
        with self.assertRaises(AttributionCoverageError) as raised:
            self.lease(project, payloads)
        coverage = raised.exception.coverage
        self.assertEqual((ATTRIBUTION_ADMITTED_ANOTHER_UNITS_INPUTS, 2), (coverage["reason"],
                                                                         coverage["inputs_of_another_units_rows"]))
        self.assertEqual({"row_attribute_states_another_polarity"},
                         {item["reason"] for item in coverage["rows_of_another_unit_listed"]})

    def test_attributes_that_agree_with_the_unit_or_state_both_polarities_pass(self) -> None:
        rows = [(f"F{index}", f"F{index}.raw") for index in range(1, 4)]
        values = [{"Mode": "HILIC Positive"}, {"Mode": "HILIC Positive"}, {"Mode": "HILIC both polarities negative"}]
        coverage = self.lease_then_judge([raw for _sample, raw in rows], rows, "HILIC", values)
        self.assertEqual(("passed", 0), (coverage["status"], coverage["rows_of_another_unit"]))


if __name__ == "__main__":
    unittest.main()

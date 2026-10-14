"""The laboratory's positive/negative reconciliation of BMP and PE P- (2026-10-14).

BMP is annotatable only in positive mode and is annotated PG in negative mode; negative mode
annotates PE P- and PE O- both as PE O-. A negative row positive mode identifies is renamed before
sheet 3 selects, and nothing is kept or dropped by the reconciliation itself.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lab_export_fixture import spot, write_export
from msdial_app.lipidome_merge import (
    MergeRefused,
    bmp_to_pg_name,
    describe_export,
    merge_pos_neg,
    pg_to_bmp_name,
    plasmanyl_to_plasmenyl_name,
    plasmenyl_to_plasmanyl_name,
    reconcile_polarities,
    reconciliation_selection_warnings,
)

SAMPLES = {
    "Positive": [("A_Pos", "SPF", "Sample"), ("B_Pos", "GF", "Sample"), ("QC_Pos", "QC", "QC"), ("Blank_Pos", "Blank", "Blank")],
    "Negative": [("A_Neg", "SPF", "Sample"), ("B_Neg", "GF", "Sample"), ("QC_Neg", "QC", "QC"), ("Blank_Neg", "Blank", "Blank")],
}
RULES = (
    "Class\tAdduct\tIon mode\tIsSelected\n"
    "BMP\t[M+NH4]+\tPositive\tFALSE\nBMP\t[M-H]-\tNegative\tTRUE\nPG\t[M-H]-\tNegative\tTRUE\n"
    "EtherPE\t[M+H]+\tPositive\tFALSE\nEtherPE\t[M-H]-\tNegative\tTRUE\nPC\t[M+H]+\tPositive\tTRUE\n"
)


def lipid(alignment_id: int, name: str, adduct: str, ontology: str, rt: float, cells=(100, 200, 300, 0), comment=""):
    return spot(alignment_id, 700.0, rt, name, adduct=adduct, ontology=ontology, cells=list(cells), comment=comment)


class NameConversionTests(unittest.TestCase):
    def test_bmp_reads_as_pg_in_every_part_and_back(self) -> None:
        self.assertEqual("PG 36:2|PG 18:1_18:1", bmp_to_pg_name("BMP 36:2|BMP 18:1_18:1"))
        self.assertEqual("PG 18:1/18:1", bmp_to_pg_name("BMP 18:1/18:1"))
        self.assertEqual("BMP 36:2|BMP 18:1_18:1", pg_to_bmp_name("PG 36:2|PG 18:1_18:1"))
        # Only the class token changes; a name with none has nothing to convert.
        self.assertIsNone(bmp_to_pg_name("PG 36:2"))
        self.assertIsNone(bmp_to_pg_name("BMP"))
        self.assertEqual("PG 36:2|HBMP 54:3", bmp_to_pg_name("BMP 36:2|HBMP 54:3"))

    def test_a_plasmenyl_chain_carries_one_double_bond_more_than_its_plasmanyl_spelling(self) -> None:
        cases = {
            "PE P-18:0_16:0": "PE O-18:1_16:0",
            "PE P-34:1": "PE O-34:2",
            "PE P-34:2|PE P-16:0_18:2": "PE O-34:3|PE O-16:1_18:2",
            "PE P-18:0/20:4": "PE O-18:1/20:4",
            "PE P-38:6|PE P-16:0/22:6": "PE O-38:7|PE O-16:1/22:6",
            "PE P-16:0_18:2;O": "PE O-16:1_18:2;O",
        }
        for plasmenyl, plasmanyl in cases.items():
            with self.subTest(name=plasmenyl):
                self.assertEqual(plasmanyl, plasmenyl_to_plasmanyl_name(plasmenyl))
                self.assertEqual(plasmenyl, plasmanyl_to_plasmenyl_name(plasmanyl))

    def test_only_a_name_that_is_plasmenyl_in_every_part_converts(self) -> None:
        for name in ("PE O-36:2", "PE 36:2", "PE P-34:2|PE O-16:1_18:2", "LPE P-18:0", "PE P-x:1"):
            with self.subTest(name=name):
                self.assertIsNone(plasmenyl_to_plasmanyl_name(name))
        # A PE O- with no double bond cannot be a PE P-.
        self.assertIsNone(plasmanyl_to_plasmenyl_name("PE O-34:0"))


class _Exports(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.rules = self.root / "rules.txt"
        self.rules.write_text(RULES, encoding="utf-8")

    def tearDown(self) -> None:
        self._directory.cleanup()

    def reconcile(self, positive_rows, negative_rows, **options):
        positive = describe_export(write_export(self.root / "pos.txt", positive_rows, SAMPLES["Positive"]))
        negative = describe_export(write_export(self.root / "neg.txt", negative_rows, SAMPLES["Negative"]))
        value_columns = [positive["first_sample_column"] + offset for offset in range(3)]
        rows, report = reconcile_polarities(positive["rows"], negative["rows"], positive["columns"], value_columns, **options)
        return rows, report, positive["columns"]

    def merge(self, positive_rows, negative_rows, **options):
        write_export(self.root / "pos.txt", positive_rows, SAMPLES["Positive"])
        write_export(self.root / "neg.txt", negative_rows, SAMPLES["Negative"])
        output = self.root / "out" / "lipid_metabolome.csv"
        report = merge_pos_neg(self.root / "pos.txt", self.root / "neg.txt", self.rules, output, **options)
        return report, output


class BmpTests(_Exports):
    def test_a_negative_pg_named_as_a_positive_bmp_becomes_bmp(self) -> None:
        rows, report, columns = self.reconcile(
            [lipid(10, "BMP 36:2|BMP 18:1_18:1", "[M+NH4]+", "BMP", 8.30)],
            [lipid(20, "PG 36:2|PG 18:1_18:1", "[M-H]-", "PG", 8.35, comment="Normalized unit: pmol"),
             lipid(21, "PG 34:1|PG 16:0_18:1", "[M-H]-", "PG", 8.33)],
        )
        renamed = rows[0]
        self.assertEqual("BMP 36:2|BMP 18:1_18:1", renamed[columns["metabolite_name"]])
        self.assertEqual("BMP", renamed[columns["ontology"]])
        self.assertEqual("[M-H]-", renamed[columns["adduct"]])
        comment = renamed[columns["comment"]]
        self.assertTrue(comment.startswith("Normalized unit: pmol; Reassigned from PG to BMP"), comment)
        self.assertIn("BMP 36:2|BMP 18:1_18:1 (positive alignment ID 10; delta RT 0.050 min)", comment)
        self.assertNotIn(",", comment)
        self.assertEqual("PG 34:1|PG 16:0_18:1", rows[1][columns["metabolite_name"]])
        self.assertEqual({"positive_rows": 1, "matched": 1, "unmatched": 0, "ambiguous": 0, "lost_claims": 0,
                          "positive_rows_not_converted": 0}, report["counts"]["BMP"])
        pair = report["matched"][0]
        self.assertEqual(("10", "20", 0.05), (pair["positive"]["alignment_id"], pair["negative"]["alignment_id"], pair["delta_rt"]))
        self.assertEqual({"name": "BMP 36:2|BMP 18:1_18:1", "ontology": "BMP"}, pair["renamed_to"])

    def test_a_near_miss_name_is_not_matched_and_is_reported_with_its_neighbours(self) -> None:
        # The user chose exact equality after the class substitution: no sum-composition fallback.
        rows, report, columns = self.reconcile(
            [lipid(10, "BMP 36:2", "[M+NH4]+", "BMP", 8.30),
             lipid(11, "BMP 18:1_18:1", "[M+NH4]+", "BMP", 8.60)],
            [lipid(20, "PG 36:2|PG 18:1_18:1", "[M-H]-", "PG", 8.32),
             lipid(21, "PG 18:1/18:1", "[M-H]-", "PG", 8.61)],
        )
        self.assertEqual(["PG", "PG"], [row[columns["ontology"]] for row in rows])
        self.assertEqual(2, report["counts"]["BMP"]["unmatched"])
        first = report["unmatched"][0]
        self.assertEqual("PG 36:2", first["negative_name"])
        self.assertIn("no kept negative PG row is named PG 36:2", first["reason"])
        self.assertEqual(["20"], [item["alignment_id"] for item in first["other_names_within_tolerance"]])

    def test_the_rt_tolerance_includes_its_boundary(self) -> None:
        for negative_rt, matched in ((8.400, True), (8.401, False), (8.200, True), (8.199, False)):
            with self.subTest(rt=negative_rt):
                rows, report, columns = self.reconcile(
                    [lipid(10, "BMP 36:2", "[M+NH4]+", "BMP", 8.300)],
                    [lipid(20, "PG 36:2", "[M-H]-", "PG", negative_rt)],
                )
                self.assertEqual("BMP" if matched else "PG", rows[0][columns["ontology"]])
                if not matched:
                    self.assertIn("lies 0.101 min away", report["unmatched"][0]["reason"])

    def test_the_tolerance_is_a_parameter(self) -> None:
        rows, report, columns = self.reconcile(
            [lipid(10, "BMP 36:2", "[M+NH4]+", "BMP", 8.30)], [lipid(20, "PG 36:2", "[M-H]-", "PG", 8.45)],
            rt_tolerance=0.2,
        )
        self.assertEqual("BMP", rows[0][columns["ontology"]])
        self.assertEqual(0.2, report["rt_tolerance_min"])
        with self.assertRaises(MergeRefused):
            self.reconcile([lipid(10, "BMP 36:2", "[M+NH4]+", "BMP", 8.30)], [lipid(20, "PG 36:2", "[M-H]-", "PG", 8.45)],
                           rt_tolerance=float("nan"))

    def test_of_several_negative_candidates_the_closest_then_the_most_intense_is_taken(self) -> None:
        rows, report, columns = self.reconcile(
            [lipid(10, "BMP 36:2", "[M+NH4]+", "BMP", 8.30)],
            [lipid(20, "PG 36:2", "[M-H]-", "PG", 8.36), lipid(21, "PG 36:2", "[M-H]-", "PG", 8.32)],
        )
        self.assertEqual(["PG", "BMP"], [row[columns["ontology"]] for row in rows])
        self.assertEqual(1, report["counts"]["BMP"]["ambiguous"])
        self.assertEqual("21", report["ambiguous"][0]["chosen_alignment_id"])
        # Equally close: the higher mean over the sample and QC columns (the Blank is not counted).
        rows, report, _ = self.reconcile(
            [lipid(10, "BMP 36:2", "[M+NH4]+", "BMP", 8.30)],
            [lipid(20, "PG 36:2", "[M-H]-", "PG", 8.35, cells=(10, 10, 10, 99999)),
             lipid(21, "PG 36:2", "[M-H]-", "PG", 8.25, cells=(20, 20, 20, 0))],
        )
        self.assertEqual(["PG", "BMP"], [row[columns["ontology"]] for row in rows])
        self.assertEqual([0.05, 0.05], [item["delta_rt"] for item in report["ambiguous"][0]["candidates"]])
        self.assertEqual([20.0, 10.0], [item["mean_intensity"] for item in report["ambiguous"][0]["candidates"]])

    def test_two_positive_rows_claiming_one_negative_row_the_closer_wins_and_the_other_is_reported(self) -> None:
        # The same BMP seen as two adducts names the same negative row twice.
        rows, report, columns = self.reconcile(
            [lipid(10, "BMP 36:2", "[M+NH4]+", "BMP", 8.36), lipid(11, "BMP 36:2", "[M+H]+", "BMP", 8.31)],
            [lipid(20, "PG 36:2", "[M-H]-", "PG", 8.30)],
        )
        self.assertEqual("BMP", rows[0][columns["ontology"]])
        self.assertIn("positive alignment ID 11;", rows[0][columns["comment"]])
        self.assertEqual({"matched": 1, "lost_claims": 1}, {key: report["counts"]["BMP"][key] for key in ("matched", "lost_claims")})
        lost = report["lost_claims"][0]
        self.assertEqual(("10", "11", "20"), (lost["positive"]["alignment_id"], lost["won_by"]["alignment_id"],
                                             lost["negative"]["alignment_id"]))
        self.assertEqual((0.06, 0.01), (lost["delta_rt"], lost["winner_delta_rt"]))

    def test_an_unmatched_positive_bmp_is_reported_and_left_to_the_selection(self) -> None:
        report, output = self.merge(
            [lipid(10, "BMP 36:2", "[M+NH4]+", "BMP", 8.30), lipid(12, "PC 34:1", "[M+H]+", "PC", 9.0)],
            [lipid(20, "PG 34:1", "[M-H]-", "PG", 8.30)],
        )
        reconciliation = report["polarity_reconciliation"]
        self.assertEqual(1, reconciliation["counts"]["BMP"]["unmatched"])
        self.assertEqual("10", reconciliation["unmatched"][0]["positive"]["alignment_id"])
        # Sheet 3 sets BMP positive FALSE, so the positive BMP is dropped by the selection, not by the reconciliation.
        self.assertEqual(1, report["dropped_by_polarity"]["positive"]["BMP is not quantified as [M+NH4]+"])
        self.assertEqual((1, 1), (report["kept_from_positive"], report["kept_from_negative"]))


class EtherPeTests(_Exports):
    def test_a_negative_pe_o_named_as_a_positive_pe_p_takes_the_plasmenyl_name(self) -> None:
        rows, report, columns = self.reconcile(
            [lipid(10, "PE P-38:6|PE P-16:0_22:6", "[M+H]+", "EtherPE", 9.68),
             lipid(11, "PE P-34:1", "[M+H]+", "EtherPE", 10.00),
             lipid(12, "PE O-36:2", "[M+H]+", "EtherPE", 11.17)],
            [lipid(20, "PE O-38:7|PE O-16:1_22:6", "[M-H]-", "EtherPE", 9.71),
             lipid(21, "PE O-34:2", "[M-H]-", "EtherPE", 10.02),
             lipid(22, "PE O-36:2", "[M-H]-", "EtherPE", 11.20)],
        )
        self.assertEqual(["PE P-38:6|PE P-16:0_22:6", "PE P-34:1", "PE O-36:2"],
                         [row[columns["metabolite_name"]] for row in rows])
        self.assertEqual(["EtherPE"] * 3, [row[columns["ontology"]] for row in rows])
        self.assertIn("Renamed from PE O-34:2 to PE P-34:1 by the positive-mode plasmenyl annotation PE P-34:1 "
                      "(positive alignment ID 11; delta RT 0.020 min)", rows[1][columns["comment"]])
        counts = report["counts"]["EtherPE"]
        # The positive PE O- (a plasmanyl positive mode itself names) is no evidence for a rename.
        self.assertEqual((2, 2, 1), (counts["positive_rows"], counts["matched"], counts["positive_rows_not_converted"]))

    def test_a_chain_composition_that_differs_is_not_matched_even_at_the_same_sum(self) -> None:
        rows, report, columns = self.reconcile(
            [lipid(10, "PE P-38:5|PE P-18:1_20:4", "[M+H]+", "EtherPE", 9.805)],
            [lipid(20, "PE O-38:6|PE O-16:0_22:6", "[M-H]-", "EtherPE", 9.845)],
        )
        self.assertEqual("PE O-38:6|PE O-16:0_22:6", rows[0][columns["metabolite_name"]])
        item = report["unmatched"][0]
        self.assertEqual("PE O-38:6|PE O-18:2_20:4", item["negative_name"])
        self.assertEqual("20", item["other_names_within_tolerance"][0]["alignment_id"])

    def test_the_renamed_rows_are_selected_and_written_by_their_new_names(self) -> None:
        report, output = self.merge(
            [lipid(10, "PE P-34:1", "[M+H]+", "EtherPE", 10.00), lipid(11, "BMP 36:2", "[M+NH4]+", "BMP", 8.30)],
            [lipid(20, "PE O-34:2", "[M-H]-", "EtherPE", 10.02), lipid(21, "PE O-36:3", "[M-H]-", "EtherPE", 11.00),
             lipid(22, "PG 36:2", "[M-H]-", "PG", 8.31), lipid(23, "PG 34:1", "[M-H]-", "PG", 8.00)],
        )
        self.assertEqual((0, 4), (report["kept_from_positive"], report["kept_from_negative"]))
        self.assertEqual({"kept": 2, "not_quantified": 0, "not_selected": 0}, report["polarity_reconciliation"]["renamed_rows"])
        rows = [line.split(",") for line in output.read_bytes().decode("ascii").split("\r\n")[5:] if line]
        self.assertEqual([("BMP 36:2", "BMP"), ("PE O-36:3", "EtherPE"), ("PE P-34:1", "EtherPE"), ("PG 34:1", "PG")],
                         [(row[3], row[11]) for row in rows])
        self.assertEqual(["BMP", "EtherPE", "PG"], report["ontologies"])
        self.assertEqual([], report["warnings"])

    def test_with_the_reconciliation_off_nothing_is_renamed(self) -> None:
        report, output = self.merge(
            [lipid(10, "PE P-34:1", "[M+H]+", "EtherPE", 10.00)],
            [lipid(20, "PE O-34:2", "[M-H]-", "EtherPE", 10.02), lipid(22, "PG 36:2", "[M-H]-", "PG", 8.31)],
            reconcile=False,
        )
        self.assertFalse(report["polarity_reconciliation"]["enabled"])
        self.assertNotIn("matched", report["polarity_reconciliation"])
        self.assertIn(",PE O-34:2,", output.read_text(encoding="ascii"))
        self.assertNotIn("Renamed", output.read_text(encoding="ascii"))

    def test_rows_set_aside_before_the_reconciliation_are_no_evidence(self) -> None:
        rows = [lipid(10, "w/o MS2: PE P-34:1", "[M+H]+", "EtherPE", 10.00),
                lipid(11, "PE P-36:1", "[M+H]+", "EtherPE", 10.50, comment="x rejected")]
        report, _ = self.merge(rows, [lipid(20, "PE O-34:2", "[M-H]-", "EtherPE", 10.02),
                                      lipid(21, "PE O-36:2", "[M-H]-", "EtherPE", 10.50)])
        self.assertEqual(0, report["polarity_reconciliation"]["counts"]["EtherPE"]["positive_rows"])


class SelectionWarningTests(_Exports):
    def test_a_selection_that_contradicts_the_reconciliation_is_said(self) -> None:
        warnings = reconciliation_selection_warnings({
            ("BMP", "[M+NH4]+"), ("BMP", "[M-H]-"), ("EtherPE", "[M+H]+"), ("EtherPE", "[M-H]-"), ("PG", "[M-H]-"),
        })
        self.assertEqual(2, len(warnings))
        self.assertIn("selects BMP [M+NH4]+ in positive mode", warnings[0])
        self.assertIn("selects EtherPE [M+H]+ in positive mode", warnings[1])
        self.assertTrue(any("does not select EtherPE [M-H]-" in item for item in
                            reconciliation_selection_warnings({("EtherPE", "[M+H]+")})))
        self.assertEqual([], reconciliation_selection_warnings({("BMP", "[M-H]-"), ("EtherPE", "[M-H]-")}))

    def test_renamed_rows_the_rule_table_does_not_select_are_said(self) -> None:
        self.rules.write_text(RULES.replace("BMP\t[M-H]-\tNegative\tTRUE", "BMP\t[M-H]-\tNegative\tFALSE"), encoding="utf-8")
        report, _ = self.merge([lipid(11, "BMP 36:2", "[M+NH4]+", "BMP", 8.30)], [lipid(22, "PG 36:2", "[M-H]-", "PG", 8.31)])
        self.assertEqual(1, report["polarity_reconciliation"]["renamed_rows"]["not_selected"])
        self.assertTrue(any("in neither class" in item for item in report["warnings"]))
        self.assertTrue(any("does not select BMP [M-H]-" in item for item in report["warnings"]))


if __name__ == "__main__":
    unittest.main()

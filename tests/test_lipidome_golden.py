"""The 2024 hands-on lipidome, reproduced from its own inputs (opt-in).

Set MSDIAL_LIPIDOMICS_DEMO to the hands-on data2 folder (NormalizedHeight_Pos.txt,
NormalizedHeight_Neg.txt, lipid_metabolome.csv, quant_utility_table.xlsx and the two EquiSPLASH
libraries; its pos and neg raw folders and metadata.xlsx for the input check). The rule table that produced the 2024 file, metaboservice's targetadduct.txt, is
looked for at ../inhouse_programs/metaboservice/ beside it, or wherever
MSDIAL_LIPIDOMICS_DEMO_RULES says. MSDIAL_LIPIDOMICS_AUDIT may name the 2026-09-06 audit folder
whose hand-made standards.tsv and quant_rules.tsv the workbook derivation is compared with.

Every file is only read; outputs go to a temporary folder.
"""

from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path

from msdial_app.lab_lipidomics import (
    cross_check_standards,
    expected_resolution_matrix,
    read_internal_standard_text,
    read_quant_workbook,
    salt_evidence,
    standards_rows,
)
from msdial_app.lipidome_merge import describe_export, merge_pos_neg

DEMO = Path(os.environ.get("MSDIAL_LIPIDOMICS_DEMO", "")) if os.environ.get("MSDIAL_LIPIDOMICS_DEMO") else None
RULES = (
    Path(os.environ["MSDIAL_LIPIDOMICS_DEMO_RULES"]) if os.environ.get("MSDIAL_LIPIDOMICS_DEMO_RULES")
    else DEMO.parent / "inhouse_programs" / "metaboservice" / "targetadduct.txt" if DEMO else None
)
AUDIT = Path(os.environ["MSDIAL_LIPIDOMICS_AUDIT"]) if os.environ.get("MSDIAL_LIPIDOMICS_AUDIT") else None
needs_demo = unittest.skipUnless(
    DEMO is not None and (DEMO / "NormalizedHeight_Pos.txt").is_file(),
    "set MSDIAL_LIPIDOMICS_DEMO to the hands-on data2 folder",
)


@needs_demo
class GoldenMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def merge(self, rules: Path, name: str) -> tuple[dict, Path]:
        output = self.root / name
        return merge_pos_neg(DEMO / "NormalizedHeight_Pos.txt", DEMO / "NormalizedHeight_Neg.txt", rules, output), output

    @unittest.skipUnless(RULES is not None and RULES.is_file(), "metaboservice's targetadduct.txt was not found")
    def test_the_2024_lipidome_is_reproduced_byte_for_byte_from_its_rule_table(self) -> None:
        result, output = self.merge(RULES, "lipid_metabolome.csv")
        self.assertEqual((372, 212, 160), (result["kept_rows"], result["kept_from_positive"], result["kept_from_negative"]))
        self.assertEqual((DEMO / "lipid_metabolome.csv").read_bytes(), output.read_bytes())

    def test_sheet_three_keeps_the_2024_rows_and_the_positive_ether_pe(self) -> None:
        result, output = self.merge(DEMO / "quant_utility_table.xlsx", "lipid_metabolome_sheet3.csv")
        self.assertEqual((394, 234, 160), (result["kept_rows"], result["kept_from_positive"], result["kept_from_negative"]))
        reference = {tuple(row) for row in csv.reader((DEMO / "lipid_metabolome.csv").read_text(encoding="ascii").splitlines())}
        added = [row for row in csv.reader(output.read_text(encoding="ascii").splitlines()) if tuple(row) not in reference]
        self.assertEqual(22, len(added))
        self.assertEqual({("EtherPE", "[M+H]+")}, {(row[11], row[4]) for row in added})
        self.assertEqual(15, len(result["rules"]["multi_adduct_classes"]))

    def test_the_two_exports_pair_their_samples(self) -> None:
        positive = describe_export(DEMO / "NormalizedHeight_Pos.txt")
        negative = describe_export(DEMO / "NormalizedHeight_Neg.txt")
        self.assertEqual(7, len(positive["sample_names"]))
        self.assertEqual(["SPF", "GF", "GF", "GF", "SPF", "SPF", "Blank"], positive["sample_classes"])
        self.assertEqual(positive["sample_classes"], negative["sample_classes"])


@needs_demo
class GoldenWorkbookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workbook = read_quant_workbook(DEMO / "quant_utility_table.xlsx", "H")
        self.positive = read_internal_standard_text(DEMO / "20200121_MsdialTxtDB_Pos_EquiSPLASH.txt", "Positive")
        self.negative = read_internal_standard_text(DEMO / "20200121_MsdialTxtDB_Neg_EquiSPLASH.txt", "Negative")

    def test_the_workbook_reads_as_saved(self) -> None:
        self.assertEqual([], self.workbook["blockers"])
        preparation = self.workbook["preparation"]
        self.assertEqual(15, len(preparation["standards"]))
        self.assertEqual("pmol_per_microL_plasma", self.workbook["unit"])
        # The workbook as saved: 10 uL in H4, while a note below says plasma 20 uL (2024 used 20).
        self.assertEqual(10.0, preparation["sample_parameter"]["value"])
        self.assertIn("plasma 20 uL", [note["text"] for note in preparation["notes"]])
        lookup = self.workbook["lookup"]
        self.assertEqual(160, len(lookup["rows"]))
        self.assertEqual(21, sum("-" in row["target_class"] for row in lookup["rows"]))
        self.assertEqual(["FA 16:0(d3)"], lookup["unused_standards"])
        quant = self.workbook["quant_info"]
        self.assertEqual((360, 160), (quant["rule_count"], len(quant["selected"])))
        self.assertEqual({"Negative": 96, "Positive": 64}, quant["selected_by_ion_mode"])

    def test_the_libraries_and_what_will_be_quantified(self) -> None:
        self.assertEqual(([], []), (self.positive["blockers"], self.negative["blockers"]))
        self.assertEqual((13, 11), (len(self.positive["records"]), len(self.negative["records"])))
        lookup, quant = self.workbook["lookup"], self.workbook["quant_info"]
        checks = cross_check_standards(self.workbook["preparation"], lookup, self.positive["records"], self.negative["records"])
        self.assertEqual([], checks["blockers"])
        matrix = expected_resolution_matrix(lookup, quant, self.positive["records"], self.negative["records"])["by_polarity"]
        self.assertEqual(["FA 18:0(d3)"], matrix["Positive"]["unresolved_expected"])
        self.assertEqual(["CE 18:1(d7)", "DG 15:0_18:1(d7)", "MG 18:1(d7)", "TG 15:0_18:1(d7)_15:0"],
                         matrix["Negative"]["unresolved_expected"])
        self.assertEqual(["AHexBRS", "AHexCAS", "AHexCS", "AHexSIS", "AHexSTS"], matrix["Negative"]["not_quantified"])
        self.assertEqual(["WE"], matrix["Positive"]["any_others_classes"])
        self.assertEqual(["ASHexCer", "DLCL"], matrix["Negative"]["any_others_classes"])
        evidence = salt_evidence("CH3COONH4", self.negative["records"], quant)
        self.assertEqual(["CH3COONH4"], evidence["salts_consistent_with_evidence"])
        self.assertEqual(31, evidence["selected_negative_adducts"]["[M+CH3COO]-"])

    @unittest.skipUnless(AUDIT is not None and (AUDIT / "standards.tsv").is_file(),
                         "set MSDIAL_LIPIDOMICS_AUDIT to the 2026-09-06 audit folder")
    def test_the_derivation_equals_the_hand_made_audit_tables(self) -> None:
        # Compared as numbers: the hand-made table carries 70.320999999999998 where repr writes 70.321.
        derived = [(row["StandardName"], row["TargetClass"], float(row["Concentration"]))
                   for row in standards_rows(self.workbook["lookup"], {"standards": []})]
        with open(AUDIT / "standards.tsv", encoding="utf-8-sig", newline="") as handle:
            audit = [(row["StandardName"], row["TargetClass"], float(row["Concentration"]))
                     for row in csv.DictReader(handle, delimiter="\t")]
        self.assertEqual(audit, derived)
        with open(AUDIT / "quant_rules.tsv", encoding="utf-8-sig", newline="") as handle:
            rules = {(row["Class"], row["Adduct"], row["Ion mode"]): row["IsSelected"].strip() in {"1", "TRUE", "true"}
                     for row in csv.DictReader(handle, delimiter="\t")}
        sheet = {(row["class"], row["adduct"], row["ion_mode"]): row["selected"]
                 for row in self.workbook["quant_info"]["rows"]}
        self.assertEqual(rules, sheet)


@needs_demo
@unittest.skipUnless(DEMO is not None and (DEMO / "pos").is_dir() and (DEMO / "neg").is_dir(),
                     "the hands-on data2 folder has no pos and neg raw folders")
class GoldenInputCheckTests(unittest.TestCase):
    """msdial_lipidomics_check_inputs on the hands-on batch, reading only: no LBM2, root or Console given."""

    def test_the_batch_checks_clean_and_its_sample_table_is_the_metadata(self) -> None:
        from msdial_app.lab_lipidomics_agent import check_inputs, public_check

        result = public_check(check_inputs({
            "pos_path": str(DEMO / "pos"), "neg_path": str(DEMO / "neg"),
            "is_text_pos": str(DEMO / "20200121_MsdialTxtDB_Pos_EquiSPLASH.txt"),
            "is_text_neg": str(DEMO / "20200121_MsdialTxtDB_Neg_EquiSPLASH.txt"),
            "quant_table_path": str(DEMO / "quant_utility_table.xlsx"), "amount_column": "H",
            "solvent_type": "酢酸アンモニウム", "retention_time_tolerance": 1.5,
            "analysis_purpose": "SPF versus GF mouse plasma lipidome",
            "sample_metadata_path": str(DEMO / "metadata.xlsx"),
        }))
        self.assertEqual([], result["blockers"])
        self.assertEqual(["lbm_path", "analysis_root", "console_path"], [item["id"] for item in result["questions"]])
        self.assertEqual((7, 7), (result["folders"]["Positive"]["file_count"], result["folders"]["Negative"]["file_count"]))
        table = result["paired_sample_table"]
        self.assertTrue(table["matched"], table["problems"])
        self.assertEqual(
            ["SPF", "GF", "GF", "GF", "SPF", "SPF", "Blank"],
            [row["class_id"] for row in sorted(table["rows"], key=lambda row: row["analytical_order"])],
        )
        joined = result["sample_metadata"]["joined"]["Negative"]
        self.assertEqual((7, [], []), (joined["matched"], joined["unmatched"], joined["disagreements"]))
        self.assertEqual(["AHexBRS", "AHexCAS", "AHexCS", "AHexSIS", "AHexSTS"],
                         result["expected_resolution"]["by_polarity"]["Negative"]["not_quantified"])
        self.assertEqual(10.0, result["quant_table"]["sample_parameter"]["value"])
        self.assertEqual("CH3COONH4", result["salt"]["salt"])


if __name__ == "__main__":
    unittest.main()

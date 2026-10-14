"""The 2024 hands-on lipidome, reproduced from its own inputs (opt-in).

Set MSDIAL_LIPIDOMICS_DEMO to the hands-on data2 folder (NormalizedHeight_Pos.txt,
NormalizedHeight_Neg.txt, lipid_metabolome.csv, quant_utility_table.xlsx and the two EquiSPLASH
libraries; its pos and neg raw folders and metadata.xlsx for the input check). The rule table that produced the 2024 file, metaboservice's targetadduct.txt, is
looked for at ../inhouse_programs/metaboservice/ beside it, or wherever
MSDIAL_LIPIDOMICS_DEMO_RULES says. MSDIAL_LIPIDOMICS_AUDIT may name the 2026-09-06 audit folder
whose hand-made standards.tsv and quant_rules.tsv the workbook derivation is compared with.

What metaboservice.exe (2024) wrote from these inputs -- the four tables and the 357 dotplot files
-- is held as sha256 in tests/vectors/metaboservice_2024_data2.v1.json, so only the inputs are needed.
The tests that read quant_utility_table.xlsx expect it as saved on 2025-03-19 (DEMO_WORKBOOK_SHA256),
and fail with "the demo workbook changed" otherwise: their counts are that sheet 3's.

Every file is only read; outputs go to a temporary folder.
"""

from __future__ import annotations

import csv
import hashlib
import json
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
from msdial_app.lipidome_merge import METABOSERVICE_2024_SAMPLE_FILE_TYPES, describe_export, merge_pos_neg

DEMO = Path(os.environ.get("MSDIAL_LIPIDOMICS_DEMO", "")) if os.environ.get("MSDIAL_LIPIDOMICS_DEMO") else None
RULES = (
    Path(os.environ["MSDIAL_LIPIDOMICS_DEMO_RULES"]) if os.environ.get("MSDIAL_LIPIDOMICS_DEMO_RULES")
    else DEMO.parent / "inhouse_programs" / "metaboservice" / "targetadduct.txt" if DEMO else None
)
VECTOR = Path(__file__).resolve().parent / "vectors" / "metaboservice_2024_data2.v1.json"
AUDIT = Path(os.environ["MSDIAL_LIPIDOMICS_AUDIT"]) if os.environ.get("MSDIAL_LIPIDOMICS_AUDIT") else None
needs_demo = unittest.skipUnless(
    DEMO is not None and (DEMO / "NormalizedHeight_Pos.txt").is_file(),
    "set MSDIAL_LIPIDOMICS_DEMO to the hands-on data2 folder",
)
# data2's quant_utility_table.xlsx as saved on 2025-03-19. Its sheet 3 selects BMP [M+NH4]+ and EtherPE [M+H]+,
# which the reconciliation warns about, and Hex3Cer [M+CH3COO]-; the laboratory means to change those, and the
# counts below are for this workbook only.
DEMO_WORKBOOK_SHA256 = "da49269258de17e41aef7695a77a714f9a94ba35c506c1c99cd091dff1038110"


def demo_workbook(case: unittest.TestCase) -> Path:
    path = DEMO / "quant_utility_table.xlsx"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != DEMO_WORKBOOK_SHA256:
        case.fail(
            f"the demo workbook changed: {path} is sha256 {digest}, and these expectations are for "
            f"{DEMO_WORKBOOK_SHA256} (as saved 2025-03-19). Update them, and DEMO_WORKBOOK_SHA256, to the edited sheets."
        )
    return path


@needs_demo
class GoldenMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def merge(self, rules: Path, name: str, folder: str = "out", **options) -> tuple[dict, Path]:
        output = self.root / folder / name
        return merge_pos_neg(DEMO / "NormalizedHeight_Pos.txt", DEMO / "NormalizedHeight_Neg.txt", rules, output,
                             **options), output

    @unittest.skipUnless(RULES is not None and RULES.is_file(), "metaboservice's targetadduct.txt was not found")
    def test_the_2024_outputs_are_reproduced_byte_for_byte_but_one_digit_of_one_value(self) -> None:
        """The four files and the 357 dotplot files metaboservice.exe (2024) wrote from these inputs.

        With the 2024 program's rules: no reconciliation, and Sample columns only (the 2024 binary
        took no QC; data2 has none anyway). The four files are compared with data2's own copies, and
        every file with the sha256 of the 2024 binary's output (tests/vectors). One computed value is
        one unit off in the 15th significant digit, in the two files that print it: the 32-bit .NET
        Framework rounds it up, which exact half-up rounding does not (net_double_tostring).
        """
        vector = json.loads(VECTOR.read_text(encoding="ascii"))
        for name, digest in vector["inputs_sha256"].items():
            self.assertEqual(digest, hashlib.sha256((DEMO / name).read_bytes()).hexdigest(), name)
        self.assertEqual(vector["rules_sha256"], hashlib.sha256(RULES.read_bytes()).hexdigest())
        result, output = self.merge(RULES, "lipid_metabolome.csv", reconcile=False,
                                    sample_file_types=METABOSERVICE_2024_SAMPLE_FILE_TYPES)
        self.assertEqual((372, 212, 160), (result["kept_rows"], result["kept_from_positive"], result["kept_from_negative"]))
        self.assertEqual([], result["warnings"])
        folder = output.parent
        written = {str(path.relative_to(folder)).replace(os.sep, "/"): path.read_bytes()
                   for path in folder.rglob("*") if path.is_file()}
        self.assertEqual(set(vector["files"]), set(written))
        self.assertEqual(357, result["outputs"]["dotplots"]["files"])

        known = vector["known_difference"]
        ours, theirs = known["value"]["interactive"].encode("ascii"), known["value"]["metaboservice_x86"].encode("ascii")
        for relative, data in sorted(written.items()):
            with self.subTest(file=relative):
                if relative in known["lines"]:
                    lines = data.split(b"\r\n")
                    index = known["lines"][relative] - 1
                    # Exactly this value on exactly this line, and nothing else differs.
                    self.assertTrue(lines[index].endswith(ours) or ours + b"," in lines[index], lines[index])
                    self.assertNotEqual(vector["files"][relative]["sha256"], hashlib.sha256(data).hexdigest())
                    lines[index] = lines[index].replace(ours, theirs)
                    data = b"\r\n".join(lines)
                self.assertEqual(vector["files"][relative]["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(vector["files"][relative]["bytes"], len(data))
        for name in ("lipid_metabolome.csv", "lipid_metabolome4stat.csv", "lipid_metabolome4classsummary.csv"):
            self.assertEqual((DEMO / name).read_bytes(), written[name], name)
        reference = (DEMO / "lipid_metabolome4summaryplot.csv").read_bytes()
        self.assertEqual(reference, written["lipid_metabolome4summaryplot.csv"].replace(ours, theirs))
        self.assertEqual(1, sum(a != b for a, b in zip(reference.split(b"\r\n"),
                                                       written["lipid_metabolome4summaryplot.csv"].split(b"\r\n"))))

    @unittest.skipUnless(RULES is not None and RULES.is_file(), "metaboservice's targetadduct.txt was not found")
    def test_the_reconciliation_names_seven_negative_ether_pe_rows_from_positive_mode(self) -> None:
        # On by default. data2's positive BMPs are all "w/o MS2", so no PG is renamed.
        result, output = self.merge(RULES, "lipid_metabolome.csv")
        reconciliation = result["polarity_reconciliation"]
        self.assertEqual((372, 212, 160), (result["kept_rows"], result["kept_from_positive"], result["kept_from_negative"]))
        self.assertEqual({"positive_rows": 0, "matched": 0, "unmatched": 0, "lost_claims": 0, "rematched": 0,
                          "ambiguous": 0, "conflicting_positive_evidence": 0, "positive_rows_not_converted": 0},
                         reconciliation["counts"]["BMP"])
        self.assertEqual((14, 7, 7, 0, 0, 0, 8), tuple(reconciliation["counts"]["EtherPE"][key] for key in (
            "positive_rows", "matched", "unmatched", "ambiguous", "lost_claims", "rematched", "positive_rows_not_converted")))
        # The eight positive PE O- name no negative row of their very name (data2's negative ones are chain-level).
        self.assertTrue(all(item["names_negative"] == [] for item in reconciliation["not_converted"]))
        self.assertEqual({"kept": 7, "not_quantified": 0, "not_selected": 0}, reconciliation["renamed_rows"])
        pairs = {item["negative"]["alignment_id"]: (item["positive"]["alignment_id"], item["renamed_to"]["name"])
                 for item in reconciliation["matched"]}
        self.assertEqual(("1595", "PE P-38:6|PE P-16:0_22:6"), pairs["1045"])
        self.assertEqual({"965", "1002", "1009", "1045", "1053", "1094", "1142"}, set(pairs))
        # Same sum, other chains: exact names only, as decided.
        unmatched = {item["positive"]["alignment_id"]: item for item in reconciliation["unmatched"]}
        self.assertEqual("1050", unmatched["1605"]["other_names_within_tolerance"][0]["alignment_id"])
        self.assertIn("lies 0.145 min away", unmatched["1774"]["reason"])
        self.assertTrue(any("selects BMP [M+NH4]+ in positive mode" in item for item in result["warnings"]))
        self.assertIn(b"PE P-16:0_22:6_1045,EtherPE,94,", (output.parent / "lipid_metabolome4summaryplot.csv").read_bytes())

    def test_sheet_three_under_the_2024_rules_adds_the_positive_ether_pe_to_the_2024_rows(self) -> None:
        """Not the laboratory's configuration: sheet 3 read as the 2024 program read its rules (no reconciliation).

        The 22 rows beyond the 2024 lipidome are all EtherPE [M+H]+, the selection the reconciliation warns
        about as counting EtherPE from both polarities. The laboratory's configuration is the test below.
        """
        result, output = self.merge(demo_workbook(self), "lipid_metabolome_sheet3.csv", reconcile=False)
        self.assertEqual((394, 234, 160), (result["kept_rows"], result["kept_from_positive"], result["kept_from_negative"]))
        reference = {tuple(row) for row in csv.reader((DEMO / "lipid_metabolome.csv").read_text(encoding="ascii").splitlines())}
        added = [row for row in csv.reader(output.read_text(encoding="ascii").splitlines()) if tuple(row) not in reference]
        self.assertEqual(22, len(added))
        self.assertEqual({("EtherPE", "[M+H]+")}, {(row[11], row[4]) for row in added})
        self.assertEqual(15, len(result["rules"]["multi_adduct_classes"]))
        self.assertFalse(any("reconciliation" in item for item in result["warnings"]))

    def test_the_laboratory_configuration_sheet_three_with_the_reconciliation_on(self) -> None:
        """Sheet 3 as saved, the reconciliation on, Sample and QC columns (data2 has no QC).

        Seven negative EtherPE rows take their PE P- names. Sheet 3 still selects BMP [M+NH4]+ and EtherPE
        [M+H]+, so the merge says EtherPE (and BMP) would be counted from both polarities, and the double
        counts it reports include each positive PE P- beside the negative row renamed from it.
        """
        result, output = self.merge(demo_workbook(self), "lipid_metabolome.csv", folder="lab")
        reconciliation = result["polarity_reconciliation"]
        self.assertEqual((394, 234, 160), (result["kept_rows"], result["kept_from_positive"], result["kept_from_negative"]))
        self.assertEqual({"kept": 7, "not_quantified": 0, "not_selected": 0}, reconciliation["renamed_rows"])
        pairs = {(item["positive"]["alignment_id"], item["negative"]["alignment_id"]) for item in reconciliation["matched"]}
        self.assertEqual({("1433", "965"), ("1508", "1002"), ("1519", "1009"), ("1595", "1045"), ("1613", "1053"),
                          ("1747", "1094"), ("1900", "1142")}, pairs)
        self.assertEqual(([], []), (reconciliation["ambiguous"], reconciliation["lost_claims"]))
        selection = [item for item in result["warnings"] if "in positive mode" in item]
        self.assertEqual(2, len(selection), result["warnings"])
        self.assertIn("selects BMP [M+NH4]+ in positive mode", selection[0])
        self.assertIn("selects EtherPE [M+H]+ in positive mode", selection[1])
        doubled = {
            (positive["alignment_id"], negative["alignment_id"])
            for group in result["possible_double_counts"] if group["ontology"] == "EtherPE"
            for positive in group["rows"] if positive["polarity"] == "positive"
            for negative in group["rows"] if negative["polarity"] == "negative" and negative["name"] == positive["name"]
        }
        self.assertEqual(pairs, doubled)
        self.assertEqual(16, sum(1 for group in result["possible_double_counts"] if group["ontology"] == "EtherPE"))
        text = output.read_text(encoding="ascii")
        self.assertIn("PE P-38:6|PE P-16:0_22:6,[M-H]-", text)
        self.assertEqual([], result["outputs"]["category_unassigned_ontologies"])

    def test_the_two_exports_pair_their_samples(self) -> None:
        positive = describe_export(DEMO / "NormalizedHeight_Pos.txt")
        negative = describe_export(DEMO / "NormalizedHeight_Neg.txt")
        self.assertEqual(7, len(positive["sample_names"]))
        self.assertEqual(["SPF", "GF", "GF", "GF", "SPF", "SPF", "Blank"], positive["sample_classes"])
        self.assertEqual(positive["sample_classes"], negative["sample_classes"])


@needs_demo
class GoldenWorkbookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workbook = read_quant_workbook(demo_workbook(self), "H")
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

        demo_workbook(self)
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
        # Sheet 3 as saved contradicts the reconciliation (BMP [M+NH4]+, EtherPE [M+H]+): asked about.
        self.assertEqual(["lbm_path", "merge_selection_accepted", "analysis_root", "raw_location", "console_path"],
                         [item["id"] for item in result["questions"]])
        self.assertEqual(2, len(result["merge_selection_warnings"]))
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

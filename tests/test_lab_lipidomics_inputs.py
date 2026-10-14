"""The laboratory lipidomics inputs: the workbook, the internal-standard libraries, the salt."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from msdial_app.lab_lipidomics import (
    LabInputError,
    adduct_mz,
    amount_unit_for_header,
    analysis_root_problems,
    canonical_text_db_lines,
    cross_check_standards,
    excel_lock_files,
    expected_resolution_matrix,
    folder_layout_problems,
    formula_mass,
    join_sample_metadata,
    lab_method_answers,
    load_lipid_class_vocabulary,
    normalize_salt,
    pair_sample_tables,
    parse_adduct,
    parse_is_preparation,
    parse_lookup_table,
    parse_quant_info,
    read_internal_standard_text,
    read_quant_workbook,
    read_sample_metadata,
    read_xlsx,
    salt_drops,
    salt_evidence,
    same_adduct,
    split_by_polarity,
    write_canonical_text_db,
)
from xlsx_fixture import (
    LOOKUP,
    NEGATIVE_LIBRARY,
    POSITIVE_LIBRARY,
    QUANT_INFO,
    STANDARDS,
    Error,
    Formula,
    Inline,
    Rich,
    Sheet,
    build_xlsx,
    lookup_sheet,
    preparation_sheet,
    quant_sheet,
    quant_workbook,
)


class Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def library(self, text: str, name: str = "library.txt", encoding: str = "ascii", bom: bool = False) -> Path:
        path = self.root / name
        data = text.encode(encoding)
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
        return path


class XlsxReaderTests(Workspace):
    def test_rich_text_is_joined_from_its_runs_without_the_phonetic_guide(self) -> None:
        path = build_xlsx(self.root / "a.xlsx", [Sheet("S", {"A1": Rich(["PC ", "15:0"], phonetic="ぴーしー")})])
        self.assertEqual("PC 15:0", read_xlsx(path).sheets[0].text("A1"))

    def test_inline_strings_booleans_and_saved_formula_values_are_read(self) -> None:
        path = build_xlsx(self.root / "a.xlsx", [Sheet("S", {
            "A1": Inline("inline"), "B1": True, "C1": False, "D1": Formula("E6/$H$4", 66.3917),
            "E1": Formula('"x"&"y"', "xy"),
        })])
        sheet = read_xlsx(path).sheets[0]
        self.assertEqual("inline", sheet.text("A1"))
        self.assertIs(True, sheet.value("B1"))
        self.assertEqual("FALSE", sheet.text("C1"))
        self.assertEqual(66.3917, sheet.number("D1"))
        self.assertEqual("=E6/$H$4", sheet.cell("D1").formula)
        self.assertEqual("xy", sheet.text("E1"))

    def test_a_formula_excel_never_calculated_is_refused_rather_than_read_as_empty(self) -> None:
        path = build_xlsx(self.root / "a.xlsx", [Sheet("S", {"A1": Formula("B1*2")})])
        sheet = read_xlsx(path).sheets[0]
        with self.assertRaisesRegex(LabInputError, "no saved value"):
            sheet.value("A1")

    def test_an_error_cell_is_not_a_value(self) -> None:
        path = build_xlsx(self.root / "a.xlsx", [Sheet("S", {"A1": Error("#N/A")})])
        sheet = read_xlsx(path).sheets[0]
        self.assertIsNone(sheet.number("A1"))
        self.assertEqual("", sheet.text("A1"))
        self.assertEqual("error", sheet.cell("A1").kind)

    def test_sheets_come_in_workbook_order_whatever_their_parts_are_called(self) -> None:
        # The analyst numbers sheets as Excel shows them; the part names follow creation order.
        path = build_xlsx(
            self.root / "a.xlsx",
            [Sheet("first", {"A1": "one"}), Sheet("second", {"A1": "two"})],
            part_names=["sheet9.xml", "sheet2.xml"],
        )
        workbook = read_xlsx(path)
        self.assertEqual(["first", "second"], workbook.sheet_names)
        self.assertEqual("one", workbook.sheets[0].text("A1"))

    def test_a_merged_cell_reads_as_the_value_of_its_range(self) -> None:
        path = build_xlsx(self.root / "a.xlsx", [Sheet("S", {"A2": "PC 15:0_18:1(d7)"}, merged=["A2:A4"])])
        sheet = read_xlsx(path).sheets[0]
        self.assertEqual("PC 15:0_18:1(d7)", sheet.text("A4"))
        self.assertEqual(4, sheet.max_row)

    def test_a_workbook_asking_to_be_recalculated_is_warned_about(self) -> None:
        path = build_xlsx(self.root / "a.xlsx", [Sheet("S", {"A1": 1})], full_calc=True)
        workbook = read_xlsx(path)
        self.assertTrue(workbook.full_calc_on_load)
        self.assertIn("stale", workbook.warnings[0])

    def test_an_excel_lock_file_is_found(self) -> None:
        path = build_xlsx(self.root / "quant_utility_table.xlsx", [Sheet("S", {"A1": 1})])
        (self.root / "~$quant_utility_table.xlsx").write_bytes(b"lock")
        self.assertEqual(1, len(excel_lock_files(path)))

    def test_something_that_is_not_a_workbook_is_refused(self) -> None:
        path = self.root / "a.xlsx"
        path.write_text("not a zip", encoding="ascii")
        with self.assertRaisesRegex(LabInputError, "cannot be read"):
            read_xlsx(path)


class PreparationSheetTests(Workspace):
    def sheet(self, **options):
        path = build_xlsx(self.root / "q.xlsx", [preparation_sheet(**options)])
        return read_xlsx(path).sheets[0]

    def test_the_standards_end_at_the_first_empty_name_and_the_worked_example_is_not_read(self) -> None:
        result = parse_is_preparation(self.sheet(), "H")
        self.assertEqual([], result["blockers"])
        self.assertEqual(5, result["header_row"])
        self.assertEqual([name for name, _, _ in STANDARDS], [item["name"] for item in result["standards"]])
        self.assertAlmostEqual(66.3917, result["standards"][0]["amount"])

    def test_each_amount_column_names_its_unit(self) -> None:
        sheet = self.sheet()
        self.assertEqual("pmol_per_mg_tissue", parse_is_preparation(sheet, "F")["unit"])
        self.assertEqual("pmol_per_10E6_cells", parse_is_preparation(sheet, "G")["unit"])
        self.assertEqual("pmol_per_microL_plasma", parse_is_preparation(sheet, "H")["unit"])
        self.assertEqual("nmol_per_mg_tissue", amount_unit_for_header("int (nmol/mg tissue)"))
        self.assertEqual("pmol_per_microL_plasma", amount_unit_for_header("pmol/µL plasma"))

    def test_a_column_that_names_no_amount_is_refused(self) -> None:
        result = parse_is_preparation(self.sheet(), "E")
        self.assertIsNone(result["unit"])
        self.assertIn("names no amount", result["blockers"][0])

    def test_the_sample_amount_behind_the_column_is_read_back_with_the_notes(self) -> None:
        result = parse_is_preparation(self.sheet(), "H")
        self.assertEqual(10.0, result["sample_parameter"]["value"])
        self.assertEqual("Put uL in solvent", result["sample_parameter"]["label"])
        self.assertIn("plasma 20 uL", [note["text"] for note in result["notes"]])

    def test_the_cells_column_multiplying_by_the_sample_amount_is_warned_about(self) -> None:
        self.assertTrue(any("multiplies" in item for item in parse_is_preparation(self.sheet(), "G")["warnings"]))
        self.assertFalse(any("multiplies" in item for item in parse_is_preparation(self.sheet(), "H")["warnings"]))

    def test_a_standard_listed_twice_is_refused(self) -> None:
        standards = STANDARDS + [STANDARDS[0]]
        result = parse_is_preparation(self.sheet(standards=standards), "H")
        self.assertTrue(any("twice" in item for item in result["blockers"]))

    def test_an_amount_excel_never_calculated_is_a_blocker(self) -> None:
        result = parse_is_preparation(self.sheet(extra={"H6": Formula("E6/$H$4")}), "H")
        self.assertTrue(any("no saved value" in item for item in result["blockers"]))

    def test_no_header_row_is_refused(self) -> None:
        path = build_xlsx(self.root / "q.xlsx", [Sheet("S", {"B2": "Compound", "D2": "Class"})])
        result = parse_is_preparation(read_xlsx(path).sheets[0], "H")
        self.assertIn("no header row", result["blockers"][0])


class LookupSheetTests(Workspace):
    def parse(self, lookup=LOOKUP, column: str = "H", **options):
        path = build_xlsx(self.root / "q.xlsx", [preparation_sheet(), lookup_sheet(lookup, **options)])
        workbook = read_xlsx(path)
        preparation = parse_is_preparation(workbook.sheets[0], column)
        return parse_lookup_table(workbook.sheets[1], preparation)

    def test_every_row_is_read_and_its_amount_comes_from_the_chosen_column(self) -> None:
        result = self.parse()
        self.assertEqual([], result["blockers"])
        self.assertEqual(len(LOOKUP), len(result["rows"]))
        self.assertAlmostEqual(66.3917, result["rows"][0]["amount"])
        # Column B is a VLOOKUP into a copy of the plasma column; with F it is recomputed.
        tissue = self.parse(column="F")
        self.assertAlmostEqual(663.917 / 5, tissue["rows"][0]["amount"])
        cells = self.parse(column="G")
        self.assertAlmostEqual(165.97925, cells["rows"][0]["amount"])
        self.assertTrue(any("caches" in item for item in cells["warnings"]))

    def test_a_header_row_is_skipped(self) -> None:
        result = self.parse(header=True)
        self.assertEqual([], result["blockers"])
        self.assertEqual(len(LOOKUP), len(result["rows"]))

    def test_a_standard_sheet_one_does_not_prepare_is_refused(self) -> None:
        result = self.parse(LOOKUP + [("SM 18:1;O2/18:1(d9)", "SM")])
        self.assertTrue(any("sheet 1 does not list" in item for item in result["blockers"]))

    def test_any_others_must_appear_exactly_once_and_exactly_so(self) -> None:
        without = [row for row in LOOKUP if row[1] != "Any others"]
        self.assertTrue(any("exactly once" in item for item in self.parse(without)["blockers"]))
        lowered = without + [("LPC 18:1(d7)", "any others")]
        blockers = self.parse(lowered)["blockers"]
        self.assertTrue(any("recognises only 'Any others'" in item for item in blockers))

    def test_a_class_normalize_never_assigns_is_refused_and_the_spelling_it_uses_named(self) -> None:
        blockers = self.parse(LOOKUP + [("Cer 18:1;O2/15:0(d7)", "Cer_AS")])["blockers"]
        self.assertTrue(any("'Cer_AS'" in item and "'Cer-AS'" in item for item in blockers))
        blockers = self.parse(LOOKUP + [("LPC 18:1(d7)", "NotAClass")])["blockers"]
        self.assertTrue(any("NotAClass" in item for item in blockers))

    def test_a_class_named_twice_is_refused(self) -> None:
        blockers = self.parse(LOOKUP + [("LPC 18:1(d7)", "PC")])["blockers"]
        self.assertTrue(any("would use the first" in item for item in blockers))

    def test_a_class_beside_an_empty_standard_is_refused(self) -> None:
        sheet = lookup_sheet()
        sheet.cells[f"C{len(LOOKUP) + 1}"] = "SM"
        path = build_xlsx(self.root / "q.xlsx", [preparation_sheet(), sheet])
        workbook = read_xlsx(path)
        result = parse_lookup_table(workbook.sheets[1], parse_is_preparation(workbook.sheets[0], "H"))
        self.assertTrue(any("names no standard" in item for item in result["blockers"]))

    def test_a_standard_merged_down_its_rows_covers_each_of_them(self) -> None:
        cells = {"A1": "LPC 18:1(d7)", "C1": "LPC", "C2": "Any others", "C3": "CAR"}
        path = build_xlsx(self.root / "q.xlsx", [preparation_sheet(), Sheet("2", cells, merged=["A1:A3"])])
        workbook = read_xlsx(path)
        result = parse_lookup_table(workbook.sheets[1], parse_is_preparation(workbook.sheets[0], "H"))
        self.assertEqual(["LPC 18:1(d7)"] * 3, [row["standard"] for row in result["rows"]])

    def test_a_prepared_standard_no_class_uses_is_reported(self) -> None:
        self.assertEqual(["FA 16:0(d3)"], self.parse()["unused_standards"])


class QuantInfoSheetTests(Workspace):
    def parse(self, rows=QUANT_INFO):
        path = build_xlsx(self.root / "q.xlsx", [quant_sheet(rows)])
        return parse_quant_info(read_xlsx(path).sheets[0])

    def test_selected_rows_become_the_merge_selection(self) -> None:
        result = self.parse()
        self.assertEqual([], result["blockers"])
        self.assertIn(("PC", "[M+CH3COO]-"), result["selected"])
        self.assertNotIn(("PC", "[M+HCOO]-"), result["selected"])
        self.assertEqual(len(QUANT_INFO), result["rule_count"])
        self.assertEqual({"Negative": 5, "Positive": 5}, result["selected_by_ion_mode"])

    def test_text_flags_are_read_as_booleans_are(self) -> None:
        path = build_xlsx(self.root / "q.xlsx", [Sheet("3", {
            "A1": "Class", "B1": "Adduct", "C1": "Ion mode", "D1": "IsSelected",
            "A2": "PC", "B2": "[M+H]+", "C2": "Positive", "D2": "TRUE",
            "A3": "PE", "B3": "[M+H]+", "C3": "Positive", "D3": 0,
        })])
        result = parse_quant_info(read_xlsx(path).sheets[0])
        self.assertEqual({("PC", "[M+H]+")}, result["selected"])

    def test_a_class_the_enum_does_not_know_is_a_blocker_when_selected(self) -> None:
        result = self.parse(QUANT_INFO + [("Cer-NS", "[M+H]+", "Positive", True)])
        self.assertTrue(any("not an LbmClass name" in item for item in result["blockers"]))
        unselected = self.parse(QUANT_INFO + [("Cer-NS", "[M+H]+", "Positive", False)])
        self.assertEqual([], unselected["blockers"])
        self.assertTrue(any("not an LbmClass name" in item for item in unselected["warnings"]))

    def test_an_enum_class_without_a_definition_is_flagged_as_falling_to_any_others(self) -> None:
        # LipidA is an LbmClass member that MS-DIAL gives no class definition.
        result = self.parse(QUANT_INFO + [("LipidA", "[M-H]-", "Negative", True)])
        self.assertEqual([], result["blockers"])
        self.assertEqual(["LipidA"], [item["class"] for item in result["undefined_classes"]])
        self.assertTrue(any("Any others" in item for item in result["warnings"]))

    def test_an_adduct_of_the_wrong_polarity_is_refused(self) -> None:
        result = self.parse(QUANT_INFO + [("SM", "[M+H]+", "Negative", True)])
        self.assertTrue(any("positive ion in a Negative row" in item for item in result["blockers"]))

    def test_classes_selected_with_more_than_one_adduct_are_listed(self) -> None:
        self.assertEqual(["EtherPE"], [item["class"] for item in self.parse()["multi_adduct_classes"]])

    def test_a_sheet_without_the_four_headers_is_refused(self) -> None:
        path = build_xlsx(self.root / "q.xlsx", [Sheet("3", {"A1": "Class", "B1": "Adduct"})])
        self.assertIn("Ion mode", parse_quant_info(read_xlsx(path).sheets[0])["blockers"][0])


class QuantWorkbookTests(Workspace):
    def test_the_three_sheets_are_read_by_position(self) -> None:
        result = read_quant_workbook(quant_workbook(self.root / "q.xlsx"), "H")
        self.assertEqual([], result["blockers"])
        self.assertEqual("pmol_per_microL_plasma", result["unit"])
        self.assertEqual(["1. IS preparation", "2. Lookup table", "3. Quant info"], result["sheet_names"])
        self.assertEqual(64, len(result["sha256"]))

    def test_a_workbook_with_fewer_sheets_is_refused(self) -> None:
        path = build_xlsx(self.root / "q.xlsx", [preparation_sheet()])
        self.assertIn("sheets 1, 2 and 3", read_quant_workbook(path, "H")["blockers"][0])


class AdductTests(unittest.TestCase):
    def test_adduct_masses_agree_with_ms_dial_resource_values(self) -> None:
        # resources/AdductIonResource_*.txt: the shift MS-DIAL adds to M for each ion.
        for name, shift in (
            ("[M+H]+", 1.007276), ("[M+NH4]+", 18.033823), ("[M+Na]+", 22.989218),
            ("[M-H]-", -1.007276), ("[M+FA-H]-", 44.998201), ("[M+Hac-H]-", 59.013851),
        ):
            self.assertAlmostEqual(shift, adduct_mz(500.0, name) - 500.0, places=5, msg=name)
        # The resource's -17.002191 for [M+H-H2O]+ is 1.1 mDa off H+ minus water; this is not.
        self.assertAlmostEqual(1.007276 - 18.010565, adduct_mz(500.0, "[M+H-H2O]+") - 500.0, places=5)

    def test_aliases_of_one_ion_are_the_same_adduct(self) -> None:
        self.assertTrue(same_adduct("[M+CH3COO]-", "[M+Hac-H]-"))
        self.assertTrue(same_adduct("[M+HCOO]-", "[M+FA-H]-"))
        self.assertFalse(same_adduct("[M+CH3COO]-", "[M+HCOO]-"))
        self.assertFalse(same_adduct("[M+H]+", "[M+NH4]+"))

    def test_bicarbonate_and_multiply_charged_ions(self) -> None:
        self.assertAlmostEqual(500.0 + 60.993118, adduct_mz(500.0, "[M+HCO3]-"), places=5)
        self.assertAlmostEqual((500.0 - 2 * 1.007276) / 2, adduct_mz(500.0, "[M-2H]2-"), places=5)
        self.assertAlmostEqual((500.0 + 2 * 18.033823) / 2, adduct_mz(500.0, "[M+2NH4]2+"), places=5)
        self.assertEqual(2, parse_adduct("[M+2NH4]2+").charge)

    def test_a_deuterated_standard_mz_follows_from_formula_and_adduct(self) -> None:
        mass = formula_mass("C41H73D7NO8P")
        self.assertAlmostEqual(753.6134, adduct_mz(mass, "[M+H]+"), places=3)
        self.assertAlmostEqual(811.6199, adduct_mz(mass, "[M+CH3COO]-"), places=3)

    def test_unreadable_adducts_and_formulas_are_refused(self) -> None:
        for text in ("M+H", "[M+H]", "[M+Xx]+"):
            with self.assertRaises(ValueError, msg=text):
                parse_adduct(text)
        with self.assertRaises(ValueError):
            formula_mass("C2H5Zz")


class SaltTests(unittest.TestCase):
    def test_every_spelling_of_a_salt_maps_to_the_console_value(self) -> None:
        for answer, salt in (
            ("酢酸アンモニウム", "CH3COONH4"), ("ammonium acetate", "CH3COONH4"), ("NH4OAc", "CH3COONH4"),
            ("10 mM ammonium formate", "HCOONH4"), ("ギ酸アンモニウム", "HCOONH4"),
            ("重炭酸アンモニウム", "NH4HCO3"), ("炭酸水素アンモニウム", "NH4HCO3"), ("NH4HCO3", "NH4HCO3"),
        ):
            self.assertEqual(salt, normalize_salt(answer)["salt"], answer)

    def test_two_anions_or_none_are_not_guessed(self) -> None:
        self.assertIsNone(normalize_salt("ammonium acetate + 0.1% formic acid")["salt"])
        self.assertIn("more than one", normalize_salt("ammonium acetate + 0.1% formic acid")["problem"])
        self.assertIsNone(normalize_salt("water")["salt"])

    def test_what_each_salt_removes(self) -> None:
        self.assertTrue(salt_drops("CH3COONH4", "[M+HCOO]-"))
        self.assertTrue(salt_drops("CH3COONH4", "[M+HCO3]-"))
        self.assertFalse(salt_drops("CH3COONH4", "[M+CH3COO]-"))
        self.assertTrue(salt_drops("HCOONH4", "[M+Hac-H]-"))
        self.assertFalse(salt_drops("HCOONH4", "[M+FA-H]-"))
        self.assertTrue(salt_drops("NH4HCO3", "[M+CH3COO]-"))
        self.assertTrue(salt_drops("NH4HCO3", "[M+HCOO]-"))
        self.assertFalse(salt_drops("NH4HCO3", "[M-H]-"))
        self.assertFalse(salt_drops("HCOONH4", "[M+NH4]+"), "positive mode is never filtered")


class SaltEvidenceTests(Workspace):
    def setUp(self) -> None:
        super().setUp()
        self.negative = read_internal_standard_text(self.library(NEGATIVE_LIBRARY), "Negative")["records"]
        path = build_xlsx(self.root / "q.xlsx", [quant_sheet()])
        self.quant = parse_quant_info(read_xlsx(path).sheets[0])

    def test_the_evidence_points_at_the_salt_that_drops_nothing(self) -> None:
        evidence = salt_evidence("CH3COONH4", self.negative, self.quant)
        self.assertEqual([], evidence["blockers"])
        self.assertEqual(["CH3COONH4"], evidence["salts_consistent_with_evidence"])
        self.assertEqual(3, len(evidence["negative_standard_adducts"]["[M+CH3COO]-"]))

    def test_a_salt_that_drops_selected_rows_blocks_and_one_that_drops_standards_warns(self) -> None:
        for salt in ("HCOONH4", "NH4HCO3"):
            evidence = salt_evidence(salt, self.negative, self.quant)
            self.assertEqual(1, len(evidence["blockers"]), salt)
            self.assertIn("could never be annotated", evidence["blockers"][0])
            # The text library still annotates a standard the LBM filter drops.
            self.assertIn("PC 15:0_18:1(d7)", evidence["standard_conflicts"])
            self.assertTrue(evidence["warnings"])


class InternalStandardTextTests(Workspace):
    def test_columns_are_found_by_header_in_any_case_and_order(self) -> None:
        text = "rt\tAdduct\tname\tONTOLOGY\tm/z\r\n9.5\t[M+H]+\tPC 15:0_18:1(d7)\tPC\t753.6134\r\n"
        result = read_internal_standard_text(self.library(text, bom=True), "Positive")
        self.assertEqual([], result["blockers"])
        record = result["records"][0]
        self.assertEqual(("PC 15:0_18:1(d7)", 753.6134, 9.5, "PC"), (record.name, record.mz, record.rt, record.ontology))

    def test_the_laboratory_files_read_cleanly(self) -> None:
        positive = read_internal_standard_text(self.library(POSITIVE_LIBRARY, "p.txt"), "Positive")
        negative = read_internal_standard_text(self.library(NEGATIVE_LIBRARY, "n.txt"), "Negative")
        self.assertEqual(([], []), (positive["blockers"], negative["blockers"]))
        self.assertEqual(([], []), (positive["warnings"], negative["warnings"]))
        self.assertEqual(4, len(positive["records"]))
        self.assertEqual("ascii", positive["encoding"])

    def test_a_missing_required_column_is_refused(self) -> None:
        result = read_internal_standard_text(self.library("Name\tMZ\tRT\tAdduct\r\nX\t1\t1\t[M+H]+\r\n"))
        self.assertIn("no ontology column", result["blockers"][0])

    def test_an_ion_of_the_other_polarity_is_refused(self) -> None:
        result = read_internal_standard_text(self.library(NEGATIVE_LIBRARY), "Positive")
        self.assertEqual(5, len(result["blockers"]))
        self.assertIn("negative ion in the positive library", result["blockers"][0])

    def test_a_repeated_name_is_refused(self) -> None:
        lines = POSITIVE_LIBRARY.splitlines()
        result = read_internal_standard_text(self.library("\r\n".join(lines + [lines[1]]) + "\r\n"), "Positive")
        self.assertTrue(any("repeats the name" in item for item in result["blockers"]))

    def test_one_file_holding_both_polarities_lists_a_standard_once_in_each(self) -> None:
        # PC, LPC and Cer d7 are in both polarities, under one name and two adducts.
        both = POSITIVE_LIBRARY + "".join(NEGATIVE_LIBRARY.splitlines(keepends=True)[1:])
        result = read_internal_standard_text(self.library(both, "both.txt"))
        self.assertEqual([], result["blockers"])
        self.assertEqual((4, 5), tuple(len(records) for records in split_by_polarity(result["records"]).values()))
        repeated = both + NEGATIVE_LIBRARY.splitlines(keepends=True)[1]
        result = read_internal_standard_text(self.library(repeated, "repeated.txt"))
        self.assertEqual(1, len(result["blockers"]))
        self.assertIn("repeats the name on line 6", result["blockers"][0])

    def test_an_mz_that_disagrees_with_formula_and_adduct_is_warned_about(self) -> None:
        text = "Name\tMZ\tRT\tAdduct\tFormula\tOntology\r\nPC 15:0_18:1(d7)\t753.6234\t9.5\t[M+H]+\tC41H73D7NO8P\tPC\r\n"
        result = read_internal_standard_text(self.library(text), "Positive")
        self.assertEqual([], result["blockers"])
        self.assertIn("mDa", result["warnings"][0])

    def test_a_decimal_comma_or_an_empty_rt_is_refused(self) -> None:
        text = "Name\tMZ\tRT\tAdduct\tOntology\r\nA\t753,6134\t9.5\t[M+H]+\tPC\r\nB\t500.1\t\t[M+H]+\tPC\r\n"
        result = read_internal_standard_text(self.library(text), "Positive")
        self.assertEqual(2, len(result["blockers"]))

    def test_utf8_is_accepted_with_a_warning_and_non_ascii_values_are_refused(self) -> None:
        text = "Name\tMZ\tRT\tAdduct\tOntology\r\nA\t500.1\t1\t[M+H]+\tPC\r\nB µ\t600.1\t1\t[M+H]+\tPC\r\n"
        result = read_internal_standard_text(self.library(text, encoding="utf-8"), "Positive")
        self.assertEqual("utf-8", result["encoding"])
        self.assertEqual(1, len(result["blockers"]))
        self.assertIn("non-ASCII", result["blockers"][0])
        self.assertTrue(any("UTF-8" in item for item in result["warnings"]))


def positional_text_library(text: str) -> list[dict[str, object]]:
    """A port of MS-DIAL's TextLibraryParser: header skipped, columns by position, strict parses."""
    lines = text.split("\r\n")
    records = []
    for line in lines[1:]:
        if not line:
            continue
        cells = line.split("\t")
        record: dict[str, object] = {"name": cells[0].strip('"'), "mz": float(cells[1])}
        if len(cells) > 2:
            record["rt"] = float(cells[2])  # raises, as the Console drops the library
        if len(cells) > 3:
            record["adduct"] = cells[3]
        if len(cells) > 7:
            record["ontology"] = cells[7]
        if len(cells) > 8:
            record["ccs"] = float(cells[8])
        records.append(record)
    return records


class CanonicalTextLibraryTests(Workspace):
    def test_a_reordered_library_round_trips_through_the_positional_reader(self) -> None:
        text = "Ontology\tAdduct\tName\tRT\tMZ\r\nPC\t[M+H]+\tPC 15:0_18:1(d7)\t9.5\t753.6134\r\nLPC\t[M+H]+\tLPC 18:1(d7)\t6.33\t529.39934\r\n"
        records = read_internal_standard_text(self.library(text), "Positive")["records"]
        target = self.root / "out" / "is_pos.txt"
        written = write_canonical_text_db(records, target)
        parsed = positional_text_library(target.read_bytes().decode("ascii"))
        self.assertEqual(2, written["records"])
        self.assertEqual(
            [{"name": "PC 15:0_18:1(d7)", "mz": 753.6134, "rt": 9.5, "adduct": "[M+H]+", "ontology": "PC"},
             {"name": "LPC 18:1(d7)", "mz": 529.39934, "rt": 6.33, "adduct": "[M+H]+", "ontology": "LPC"}],
            parsed,
        )

    def test_no_row_ends_in_an_empty_ccs_field(self) -> None:
        # An empty ninth field makes the Console drop the row, and with it the library.
        text = "Name\tMZ\tRT\tAdduct\tOntology\tCCS\r\nA\t500.1\t1\t[M+H]+\tPC\t\r\nB\t600.1\t2\t[M+H]+\tPC\t250.5\r\n"
        records = read_internal_standard_text(self.library(text), "Positive")["records"]
        lines = canonical_text_db_lines(records)
        self.assertEqual(8, len(lines[1].split("\t")))
        self.assertEqual(9, len(lines[2].split("\t")))
        self.assertEqual(250.5, positional_text_library("\r\n".join(lines) + "\r\n")[1]["ccs"])

    def test_the_laboratory_library_keeps_its_numbers_as_written(self) -> None:
        records = read_internal_standard_text(self.library(POSITIVE_LIBRARY), "Positive")["records"]
        lines = canonical_text_db_lines(records)
        self.assertEqual(POSITIVE_LIBRARY.splitlines()[1:], [line for line in lines[1:]])


class CrossCheckTests(Workspace):
    def setUp(self) -> None:
        super().setUp()
        workbook = read_quant_workbook(quant_workbook(self.root / "q.xlsx"), "H")
        self.preparation = workbook["preparation"]
        self.lookup = workbook["lookup"]
        self.quant = workbook["quant_info"]
        self.positive = read_internal_standard_text(self.library(POSITIVE_LIBRARY, "p.txt"), "Positive")["records"]
        self.negative = read_internal_standard_text(self.library(NEGATIVE_LIBRARY, "n.txt"), "Negative")["records"]

    def test_the_expected_resolution_is_known_before_anything_runs(self) -> None:
        matrix = expected_resolution_matrix(self.lookup, self.quant, self.positive, self.negative)
        positive = matrix["by_polarity"]["Positive"]
        negative = matrix["by_polarity"]["Negative"]
        self.assertEqual(["FA 18:0(d3)"], positive["unresolved_expected"])
        self.assertEqual(["CE 18:1(d7)"], negative["unresolved_expected"])
        # AHexCS is selected in negative mode, but its standard is in the positive library only.
        self.assertEqual(["AHexCS"], negative["not_quantified"])
        self.assertEqual([], positive["not_quantified"])
        self.assertEqual(["EtherPE", "WE"], positive["any_others_classes"])
        self.assertEqual(["EtherPE"], negative["any_others_classes"])
        cer = next(row for row in matrix["rows"] if row["class"] == "Cer_NS")
        self.assertEqual(("Cer-NS", "Cer 18:1;O2/15:0(d7)", "own"), (cer["vs2_class"], cer["standard"], cer["via"]))

    def test_a_prepared_standard_neither_library_lists_is_a_blocker(self) -> None:
        result = cross_check_standards(self.preparation, self.lookup, self.positive, self.negative[:-2])
        self.assertTrue(any("FA 16:0(d3)" in item for item in result["blockers"]))
        self.assertTrue(any("FA 18:0(d3)" in item for item in result["blockers"]))

    def test_a_prepared_standard_no_class_uses_is_noted(self) -> None:
        result = cross_check_standards(self.preparation, self.lookup, self.positive, self.negative)
        self.assertEqual([], result["blockers"])
        self.assertTrue(any("FA 16:0(d3)" in item for item in result["notes"]))


class SampleTableTests(Workspace):
    HEADER = ["File path", "File name", "Type", "Class ID", "Acquisiotion type", "Batch",
              "Analytical order", "Dilution factor", "Included"]

    def metadata(self, rows: list[list[object]]) -> Path:
        cells = {f"{chr(65 + index)}1": title for index, title in enumerate(self.HEADER)}
        for number, row in enumerate(rows, start=2):
            for index, value in enumerate(row):
                cells[f"{chr(65 + index)}{number}"] = value
        return build_xlsx(self.root / "metadata.xlsx", [Sheet("Sheet1", cells)])

    def test_the_gui_sample_table_is_read_and_joined_by_stem(self) -> None:
        path = self.metadata([
            [r"E:\old\neg\S1_SPF_Neg.wiff", "S1_SPF_Neg", "Sample", "SPF", "DDA", 1, 1, 1, True],
            [r"E:\old\neg\S2_GF_Neg.wiff", "S2_GF_Neg", "Sample", "GF", "DDA", 1, 2, 2, True],
            [r"E:\old\neg\Blank_Neg.wiff", "Blank_Neg", "Blank", "Blank", "DDA", 1, 3, 1, False],
        ])
        metadata = read_sample_metadata(path)
        self.assertEqual([], metadata["blockers"])
        self.assertTrue(any("Acquisiotion" in item for item in metadata["warnings"]))
        self.assertTrue(any("multiplies" in item for item in metadata["warnings"]))
        self.assertEqual([1.0, 2.0, 1.0], [row["factor"] for row in metadata["rows"]])
        self.assertFalse(metadata["rows"][2]["included"])
        proposed = [
            {"file_name": "S1_SPF_Pos", "file_type": "Sample", "class_id": "SPF", "analytical_order": 1},
            {"file_name": "S2_GF_Pos", "file_type": "Sample", "class_id": "SPF", "analytical_order": 2},
            {"file_name": "Other_Pos", "file_type": "Sample", "class_id": "GF", "analytical_order": 3},
        ]
        joined = join_sample_metadata(metadata, proposed)
        self.assertEqual("stem", joined["files"][0]["matched_by"])
        self.assertEqual(["Other_Pos"], joined["unmatched"])
        self.assertEqual([{"file_name": "S2_GF_Pos", "field": "class_id", "proposed": "SPF", "metadata": "GF"}],
                         joined["disagreements"])

    def test_a_csv_sample_table_is_read_the_same_way(self) -> None:
        path = self.root / "metadata.csv"
        path.write_text("File name,Type,Class ID,Analytical order,Included\nS1_Pos,Sample,A,1,TRUE\nS2_Pos,QC,QC,x,maybe\n",
                        encoding="utf-8")
        metadata = read_sample_metadata(path)
        self.assertEqual(2, len(metadata["blockers"]))
        self.assertEqual("S1", metadata["rows"][0]["stem"])

    def test_both_polarities_pair_into_one_table(self) -> None:
        positive = [{"file_name": "A_Pos", "file_type": "Sample", "class_id": "SPF", "analytical_order": 1},
                    {"file_name": "B_Pos", "file_type": "Blank", "class_id": "Blank", "analytical_order": 2}]
        negative = [{"file_name": "A_Neg", "file_type": "Sample", "class_id": "SPF", "analytical_order": 1},
                    {"file_name": "B_Neg", "file_type": "Blank", "class_id": "Blank", "analytical_order": 2}]
        paired = pair_sample_tables(positive, negative)
        self.assertTrue(paired["matched"], paired["problems"])
        self.assertEqual(["A", "B"], [row["stem"] for row in paired["rows"]])
        negative[0]["class_id"] = "GF"
        self.assertFalse(pair_sample_tables(positive, negative[:1] + [{"file_name": "C_Neg"}])["matched"])


class LayoutTests(Workspace):
    def test_the_two_polarities_need_two_separate_folders(self) -> None:
        (self.root / "pos").mkdir()
        (self.root / "pos" / "inner").mkdir()
        self.assertTrue(folder_layout_problems(self.root / "pos", self.root / "pos"))
        self.assertTrue(folder_layout_problems(self.root / "pos", self.root / "pos" / "inner"))
        self.assertEqual([], folder_layout_problems(self.root / "pos", self.root / "neg"))

    def test_an_analysis_root_in_a_checkout_or_a_temporary_folder_is_refused(self) -> None:
        checkout = self.root / "repo"
        (checkout / ".git").mkdir(parents=True)
        result = analysis_root_problems(checkout / "analysis", home=self.root / "home", temporary=[])
        self.assertTrue(any("code checkout" in item for item in result["blockers"]))
        result = analysis_root_problems(self.root / "work", home=self.root / "home", temporary=[self.root])
        self.assertTrue(any("temporary" in item for item in result["blockers"]))
        result = analysis_root_problems(self.root / "home" / "x", home=self.root / "home", temporary=[])
        self.assertEqual([], result["blockers"])
        self.assertTrue(any("user profile" in item for item in result["warnings"]))

    def test_the_stated_method_writes_every_anchor_setting_out(self) -> None:
        answers = lab_method_answers()["answers"]
        self.assertEqual(12, answers["automatic_rt_correction_maximum_anchors"])
        self.assertEqual(1.5, answers["automatic_rt_correction_local_support_rt_window"])
        self.assertTrue(answers["automatic_rt_correction_interpolate_blanks_by_analytical_order"])
        self.assertIn("confirmed sample table", lab_method_answers()["basis"][
            "automatic_rt_correction_interpolate_blanks_by_analytical_order"])


class VocabularyUseTests(unittest.TestCase):
    def test_both_spellings_reach_the_same_class_definition(self) -> None:
        vocabulary = load_lipid_class_vocabulary()
        self.assertEqual("Cer-NS", vocabulary.canonical_vs2("Cer_NS"))
        self.assertEqual("Cer-NS", vocabulary.canonical_vs2("Cer-NS"))
        self.assertEqual("Undefined", vocabulary.canonical_vs2("NotAClass"))
        self.assertTrue(vocabulary.is_lbm_class("LipidA"))
        self.assertEqual("Undefined", vocabulary.vs2_of_enum("LipidA"))


if __name__ == "__main__":
    unittest.main()

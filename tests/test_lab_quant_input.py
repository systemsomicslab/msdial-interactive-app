"""Format 1 of the laboratory lipidomics quantification input (docs/lab-lipidomics-quant-input.md).

It must say the same as the earlier form it replaces (quant_utility_table.xlsx, an amount column and
two text libraries), and say every problem with its table, row and column.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lab_batch_fixture import Batch, capabilities_patches, normalize_outputs, write_run  # noqa: E402

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import lab_lipidomics_agent, lab_quant_input as q  # noqa: E402
    from msdial_app.lab_lipidomics import (  # noqa: E402
        NEGATIVE,
        POSITIVE,
        adduct_mz,
        formula_mass,
        parse_adduct,
        read_internal_standard_text,
        read_quant_workbook,
    )

EXAMPLE = ROOT / "examples" / "lab-lipidomics"
HEADER = {key: list(q.HEADERS[key]) for key in q.TABLES}
SETTINGS = [["Format", q.FORMAT_ID], ["Sample amount", "10"], ["Sample basis", "uL plasma"]]
PC = ["PC 15:0_18:1(d7)", "PC", "663.917", "C41H73D7NO8P", "", "", "[M+H]+", "753.6134", "9.5", "",
      "[M+CH3COO]-", "811.6199", "9.5", ""]
FA = ["FA 18:0(d3)", "FA", "3200", "C18H33D3O2", "", "", "", "", "", "", "[M-H]-", "286.2830842", "6.96", ""]
CLASS_STANDARDS = [["PC", "PC 15:0_18:1(d7)"], ["FA", "FA 18:0(d3)"], ["Any others", "PC 15:0_18:1(d7)"]]
SELECTION = [["PC", "[M+H]+", "Positive", "TRUE"], ["FA", "[M-H]-", "Negative", "TRUE"]]


def _write_table(path: Path, header: list[str], rows: list[list[str]], delimiter: str = "\t") -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8")


class _Folder(unittest.TestCase):
    """A small format-1 input as a folder of text files, each table replaceable."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)

    def folder(self, **tables: list[list[str]]) -> Path:
        folder = self.base / "input"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
        rows = {"settings": SETTINGS, "standards": [PC, FA], "class_standards": CLASS_STANDARDS, "selection": SELECTION}
        headers = dict(HEADER)
        for key, value in tables.items():
            if key.endswith("_header"):
                headers[key[: -len("_header")]] = value
            else:
                rows[key] = value
        for key in q.TABLES:
            _write_table(folder / f"{q.FILE_STEMS[key]}.tsv", headers[key], rows[key])
        return folder

    def read(self, **tables: list[list[str]]) -> dict:
        return q.read_quant_input(self.folder(**tables))

    def assertBlocks(self, read: dict, *fragments: str) -> None:
        text = "\n".join(read["blockers"])
        for fragment in fragments:
            self.assertIn(fragment, text)


class TheExampleSaysWhatTheEarlierFormSays(unittest.TestCase):
    def setUp(self) -> None:
        self.legacy = read_quant_workbook(EXAMPLE / "quant_utility_table.xlsx", "H")
        self.texts = {
            POSITIVE: read_internal_standard_text(EXAMPLE / "EquiSPLASH_Pos.txt", POSITIVE)["records"],
            NEGATIVE: read_internal_standard_text(EXAMPLE / "EquiSPLASH_Neg.txt", NEGATIVE)["records"],
        }

    def check(self, path: Path) -> None:
        read = q.read_quant_input(path)
        self.assertEqual([], read["blockers"])
        self.assertEqual([], read["warnings"])
        self.assertEqual(self.legacy["unit"], read["unit"])
        self.assertEqual(
            [(item["name"], item["class_name"], item["amount"]) for item in self.legacy["preparation"]["standards"]],
            [(item["name"], item["class_name"], item["amount"]) for item in read["preparation"]["standards"]],
        )
        self.assertEqual(
            [(row["standard"], row["target_class"], row["amount"]) for row in self.legacy["lookup"]["rows"]],
            [(row["standard"], row["target_class"], row["amount"]) for row in read["lookup"]["rows"]],
        )
        self.assertEqual(self.legacy["quant_info"]["selected"], read["quant_info"]["selected"])
        self.assertEqual(self.legacy["quant_info"]["rule_count"], read["quant_info"]["rule_count"])

        def key(record):
            return (record.name, record.mz, record.rt, record.adduct, record.ontology, record.inchikey,
                    record.formula, record.smiles, record.ccs)

        for polarity in (POSITIVE, NEGATIVE):
            self.assertEqual(sorted(map(key, self.texts[polarity])), sorted(map(key, read["libraries"][polarity])))
        self.assertEqual(10, read["settings"]["sample_amount"])

    def test_the_workbook(self) -> None:
        self.check(EXAMPLE / "quant_input.xlsx")

    def test_the_folder(self) -> None:
        self.check(EXAMPLE / "quant_input_tsv")

    def test_the_formats_are_told_apart(self) -> None:
        self.assertEqual(q.FORMAT_ID, q.detect_format(EXAMPLE / "quant_input.xlsx"))
        self.assertEqual(q.FORMAT_ID, q.detect_format(EXAMPLE / "quant_input_tsv"))
        self.assertEqual(q.LEGACY_FORMAT, q.detect_format(EXAMPLE / "quant_utility_table.xlsx"))
        self.assertEqual("", q.detect_format(EXAMPLE / "EquiSPLASH_Pos.txt"))

    def test_the_defaults_and_the_examples_are_the_example_converted(self) -> None:
        spec = importlib.util.spec_from_file_location("generator", ROOT / "scripts" / "generate-lab-quant-input-defaults.py")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        tables = generator.converted()
        self.assertEqual(generator.defaults(tables), json.loads(q.DEFAULTS_PATH.read_text(encoding="utf-8")))
        folder = EXAMPLE / "quant_input_tsv"
        for key in q.TABLES:
            written = list(csv.reader(io.StringIO((folder / f"{q.FILE_STEMS[key]}.tsv").read_text(encoding="utf-8")),
                                      delimiter="\t"))
            self.assertEqual(HEADER[key], written[0])
            self.assertEqual([[q._text_value(value) for value in row] for row in tables[key]], written[1:], key)


class TheSettings(_Folder):
    def test_the_amount_per_sample_unit_is_the_spiked_amount_over_the_sample_amount(self) -> None:
        read = self.read()
        self.assertEqual([], read["blockers"])
        self.assertEqual("pmol_per_microL_plasma", read["unit"])
        amounts = {item["name"]: item["amount"] for item in read["preparation"]["standards"]}
        self.assertEqual(663.917 / 10, amounts["PC 15:0_18:1(d7)"])
        self.assertEqual(320.0, amounts["FA 18:0(d3)"])

    def test_the_basis_and_the_unit_name_normalizes_unit(self) -> None:
        read = self.read(settings=[["Format", q.FORMAT_ID], ["Sample amount", "4"], ["Sample basis", "mg tissue"],
                                   ["Spiked amount unit", "nmol"]])
        self.assertEqual("nmol_per_mg_tissue", read["unit"])
        read = self.read(settings=[["Format", q.FORMAT_ID], ["Sample amount", "0.25"], ["Sample basis", "10^6 cells"]])
        self.assertEqual("pmol_per_10E6_cells", read["unit"])
        read = self.read(settings=[["Format", q.FORMAT_ID], ["Sample amount", "20"], ["Sample basis", "µL plasma"]])
        self.assertEqual("pmol_per_microL_plasma", read["unit"])

    def test_the_format_must_be_this_one(self) -> None:
        self.assertBlocks(self.read(settings=SETTINGS[1:]), "has no Format row")
        self.assertBlocks(self.read(settings=[["Format", "msdial-lipidomics-quant-input/2"]] + SETTINGS[1:]),
                          "this reader knows only msdial-lipidomics-quant-input/1")

    def test_each_setting_is_said_where_it_is_wrong(self) -> None:
        read = self.read(settings=[["Format", q.FORMAT_ID], ["Sample amount", "1,5"], ["Sample basis", "uL serum"],
                                   ["Spiked amount unit", "ug"], ["Operator", "me"]])
        self.assertBlocks(read, "settings.tsv row 3, Value: '1,5' is not a number written with a decimal point",
                          "Sample basis 'uL serum' is not one of uL plasma, mg tissue, 10^6 cells",
                          "Spiked amount unit 'ug' is not pmol, nmol or fmol")
        self.assertIn("settings.tsv row 6: the setting 'Operator' is not one this format reads; ignored.", read["notes"])
        self.assertBlocks(self.read(settings=SETTINGS + [["Sample amount", "20"]]), "Sample amount is given twice")
        self.assertBlocks(self.read(settings=SETTINGS[:1]), "Sample amount is required", "Sample basis is required")


class TheStandards(_Folder):
    def test_the_mz_is_computed_from_the_formula_when_it_is_blank(self) -> None:
        blank = list(PC)
        blank[7] = ""
        read = self.read(standards=[blank, FA])
        self.assertEqual([], read["blockers"])
        record = next(item for item in read["libraries"][POSITIVE] if item.name == PC[0])
        expected = adduct_mz(formula_mass("C41H73D7NO8P"), parse_adduct("[M+H]+"))
        self.assertTrue(math.isclose(expected, record.mz, abs_tol=1e-6))
        self.assertLess(abs(record.mz - 753.6134), 0.001)
        self.assertTrue(any("standards.tsv row 2, Pos m/z: computed from C41H73D7NO8P [M+H]+" in note for note in read["notes"]))

    def test_an_mz_far_from_the_formula_is_warned_about(self) -> None:
        wrong = list(PC)
        wrong[7] = "753.6334"
        read = self.read(standards=[wrong, FA])
        self.assertEqual([], read["blockers"])
        self.assertTrue(any("standards.tsv row 2, Pos m/z: 753.6334 differs from" in item for item in read["warnings"]))

    def test_a_polarity_left_blank_is_not_used_and_a_half_filled_one_is_a_problem(self) -> None:
        read = self.read()
        self.assertEqual(["PC 15:0_18:1(d7)"], [item.name for item in read["libraries"][POSITIVE]])
        self.assertEqual(["PC 15:0_18:1(d7)", "FA 18:0(d3)"], [item.name for item in read["libraries"][NEGATIVE]])
        half = list(FA)
        half[6] = "[M+H]+"
        self.assertBlocks(self.read(standards=[PC, half]),
                          "standards.tsv row 3, Pos RT: the retention time of FA 18:0(d3) on your chromatography is required")
        no_formula = list(half)
        no_formula[3] = ""
        self.assertBlocks(self.read(standards=[PC, no_formula]),
                          "standards.tsv row 3, Pos m/z: the m/z is required, or a Formula")

    def test_every_cell_is_said_where_it_is_wrong(self) -> None:
        wrong = ["PC 15:0_18:1(d7)µ", "PC", "-1", "C41H73D7NO8Pq", "", "", "[M-H]-", "753,6134", "9.5", "",
                 "[M+CH3COO]-", "811.6199", "", ""]
        self.assertBlocks(
            self.read(standards=[wrong, FA]),
            "standards.tsv row 2, Name: 'PC 15:0_18:1(d7)µ' holds non-ASCII characters",
            "standards.tsv row 2, Spiked amount: the amount must be positive",
            "standards.tsv row 2, Formula:",
            "standards.tsv row 2, Pos adduct: [M-H]- is a negative ion",
            "standards.tsv row 2, Pos m/z: '753,6134' is not a number written with a decimal point",
            "standards.tsv row 2, Neg RT: the retention time of",
        )
        self.assertBlocks(self.read(standards=[PC, PC]), "standards.tsv row 3, Name: PC 15:0_18:1(d7) is also on row 2")
        none = ["Lonely", "PC", "10"] + [""] * 11
        self.assertBlocks(self.read(standards=[PC, FA, none]), "standards.tsv row 4: Lonely has a peak in neither polarity")
        no_amount = list(PC)
        no_amount[2] = ""
        self.assertBlocks(self.read(standards=[no_amount, FA]),
                          "standards.tsv row 2, Spiked amount: the amount of PC 15:0_18:1(d7) added to each sample is required")

    def test_each_polarity_needs_a_standard(self) -> None:
        self.assertBlocks(self.read(standards=[FA], class_standards=[["Any others", "FA 18:0(d3)"]]),
                          "no standard has a positive-mode peak")

    def test_a_class_in_the_lbm_spelling_is_read_in_the_class_definition_spelling(self) -> None:
        cer = ["Cer 18:1;O2/15:0(d7)", "Cer_NS", "941.7435", "C33H58D7NO3", "", "", "[M+H]+", "", "9.34", "",
               "", "", "", ""]
        read = self.read(standards=[PC, FA, cer])
        self.assertEqual([], read["blockers"])
        self.assertIn("standards.tsv row 4, Class: Cer_NS read as Cer-NS.", read["notes"])
        self.assertEqual("Cer-NS", next(item for item in read["libraries"][POSITIVE] if item.name.startswith("Cer")).ontology)

    def test_unknown_and_duplicate_columns(self) -> None:
        read = self.read(standards_header=HEADER["standards"] + ["My notes"], standards=[PC + ["mine"], FA + [""]])
        self.assertEqual([], read["blockers"])
        self.assertIn("standards.tsv: columns not read: My notes.", read["notes"])
        self.assertBlocks(self.read(standards_header=HEADER["standards"][:-1] + ["Pos RT"]), "has the column Pos RT twice")
        self.assertBlocks(self.read(standards_header=["Name", "Class"]), "standards.tsv row 1 has no Spiked amount")

    def test_headers_are_matched_without_case_spaces_or_units(self) -> None:
        header = ["name", "CLASS", "Spiked amount (pmol)", "formula", "InChIKey", "SMILES", "pos_adduct", "Pos m/z",
                  "Pos RT (min)", "Pos CCS", "Neg Adduct", "neg mz", "Neg RT", "Neg CCS"]
        self.assertEqual([], self.read(standards_header=header)["blockers"])


class TheClassStandards(_Folder):
    def test_either_spelling_is_read_and_unknown_classes_are_problems(self) -> None:
        read = self.read(class_standards=CLASS_STANDARDS + [["Cer_NS", "PC 15:0_18:1(d7)"]])
        self.assertEqual([], read["blockers"])
        self.assertIn("class_standards.tsv row 5, Class: Cer_NS read as Cer-NS.", read["notes"])
        self.assertIn(("PC 15:0_18:1(d7)", "Cer-NS"), [(row["standard"], row["target_class"]) for row in read["lookup"]["rows"]])
        self.assertBlocks(self.read(class_standards=CLASS_STANDARDS + [["Phospholipids", "PC 15:0_18:1(d7)"]]),
                          "class_standards.tsv row 5, Class: 'Phospholipids' is a class MS-DIAL knows in neither spelling")

    def test_any_others_once_each_class_once_and_only_listed_standards(self) -> None:
        self.assertBlocks(self.read(class_standards=CLASS_STANDARDS[:2]), "must name 'Any others' exactly once")
        self.assertBlocks(self.read(class_standards=CLASS_STANDARDS + [["any others", "FA 18:0(d3)"]]),
                          "must name 'Any others' exactly once")
        self.assertBlocks(self.read(class_standards=CLASS_STANDARDS + [["PC", "FA 18:0(d3)"]]),
                          "class_standards.tsv row 5, Class: PC is also on row 2")
        self.assertBlocks(self.read(class_standards=CLASS_STANDARDS + [["LPC", "LPC 18:1(d7)"]]),
                          "class_standards.tsv row 5, Standard: LPC 18:1(d7) is not a Name in the Standards table")

    def test_a_standard_quantifying_nothing_is_listed(self) -> None:
        read = self.read(class_standards=[["PC", "PC 15:0_18:1(d7)"], ["Any others", "PC 15:0_18:1(d7)"]])
        self.assertEqual(["FA 18:0(d3)"], read["lookup"]["unused_standards"])


class TheSelection(_Folder):
    def test_use_is_true_when_blank_or_absent(self) -> None:
        read = self.read(selection=[["PC", "[M+H]+", "Positive", ""], ["FA", "[M-H]-", "Neg", "FALSE"]])
        self.assertEqual({("PC", "[M+H]+")}, read["quant_info"]["selected"])
        read = self.read(selection_header=["Class", "Adduct", "Polarity"],
                         selection=[["PC", "[M+H]+", "Positive"], ["FA", "[M-H]-", "Negative"]])
        self.assertEqual({("PC", "[M+H]+"), ("FA", "[M-H]-")}, read["quant_info"]["selected"])

    def test_problems_in_a_used_row_block_and_in_an_unused_row_are_said(self) -> None:
        read = self.read(selection=SELECTION + [["PC", "[M+H]+", "Negative", "TRUE"], ["PE", "[M-H]-", "Positive", "FALSE"],
                                                ["PC", "[M+Na]+", "Positive", "maybe"]])
        self.assertBlocks(read, "selection.tsv row 4 (PC [M+H]+): [M+H]+ is a positive ion in a Negative row",
                          "selection.tsv row 6, Use: 'maybe' is not TRUE or FALSE")
        self.assertTrue(any("selection.tsv row 5 (PE [M-H]-)" in item for item in read["warnings"]))

    def test_a_class_in_the_class_definition_spelling_is_read_in_the_lbm_spelling(self) -> None:
        read = self.read(selection=SELECTION + [["Cer-NS", "[M+CH3COO]-", "Negative", "TRUE"]])
        self.assertEqual([], read["blockers"])
        self.assertIn("selection.tsv row 4, Class: Cer-NS read as Cer_NS.", read["notes"])
        self.assertIn(("Cer_NS", "[M+CH3COO]-"), read["quant_info"]["selected"])

    def test_nothing_selected_blocks(self) -> None:
        self.assertBlocks(self.read(selection=[["PC", "[M+H]+", "Positive", "FALSE"]]), "selects no class and adduct")


class TheContainers(_Folder):
    def test_a_folder_needs_each_table_once_and_reads_csv(self) -> None:
        folder = self.folder()
        (folder / "selection.tsv").unlink()
        self.assertBlocks(q.read_quant_input(folder), "has no selection.tsv (or .csv): the Selection table is missing")
        _write_table(folder / "selection.csv", HEADER["selection"], [["PC", "[M+H]+", "Positive", "TRUE"],
                                                                     ["FA", "[M-H]-", "Negative", "TRUE"]], ",")
        self.assertEqual([], q.read_quant_input(folder)["blockers"])
        _write_table(folder / "selection.tsv", HEADER["selection"], SELECTION)
        self.assertBlocks(q.read_quant_input(folder), "has both selection.tsv and selection.csv; keep one")

    def test_comment_lines_and_blank_rows_are_skipped_with_rows_numbered_as_in_the_file(self) -> None:
        folder = self.folder()
        text = (folder / "standards.tsv").read_text(encoding="utf-8").splitlines()
        wrong = list(FA)
        wrong[12] = "6,96"
        (folder / "standards.tsv").write_text(
            "\n".join(["# the laboratory's standards", text[0], text[1], "", "\t".join(wrong)]) + "\n", encoding="utf-8")
        self.assertBlocks(q.read_quant_input(folder), "standards.tsv row 5, Neg RT: '6,96' is not a number")

    def test_a_workbook_needs_its_four_sheets(self) -> None:
        tables, _, _ = q.converted_tables(EXAMPLE / "quant_utility_table.xlsx", "H", EXAMPLE / "EquiSPLASH_Pos.txt",
                                          EXAMPLE / "EquiSPLASH_Neg.txt")
        from msdial_app.supplementary_excel import _write_xlsx

        path = self.base / "three.xlsx"
        _write_xlsx(path, [{"name": q.SHEET_NAMES[key], "widths": [10],
                            "rows": [{"values": q.HEADERS[key]}] + [{"values": row} for row in tables[key]]}
                           for key in ("settings", "standards", "selection")])
        self.assertEqual(q.FORMAT_ID, q.detect_format(path))
        self.assertBlocks(q.read_quant_input(path), "three.xlsx has no 'Class standards' sheet")
        old = self.base / "old.xls"
        old.write_bytes(b"\xd0\xcf\x11\xe0")
        self.assertBlocks(q.read_quant_input(old), "old.xls is an old .xls workbook; save it as .xlsx")


class TheTemplate(_Folder):
    def test_a_preview_writes_nothing_and_nothing_is_overwritten(self) -> None:
        target = self.base / "new.xlsx"
        preview = q.quant_input_template({"output_path": str(target)})
        self.assertFalse(preview["written"])
        self.assertFalse(target.exists())
        self.assertEqual({"Settings": 5, "Standards": 15, "Class standards": 160, "Selection": 360}, preview["rows"])
        written = q.quant_input_template({"output_path": str(target), "confirmed": True})
        self.assertTrue(written["written"])
        with self.assertRaises(ValueError):
            q.quant_input_template({"output_path": str(target), "confirmed": True})
        with self.assertRaises(ValueError):
            q.quant_input_template({"output_path": str(self.base / "new.txt")})

    def test_the_template_says_what_to_fill_in_and_reads_once_filled(self) -> None:
        target = self.base / "fill"
        written = q.quant_input_template({"output_path": str(target), "layout": "tsv", "confirmed": True})
        blockers = "\n".join(written["check"]["blockers"])
        for fragment in ("Settings: Sample amount is required", "Settings: Sample basis is required",
                         "standards.tsv row 2, Spiked amount", "standards.tsv row 2, Pos RT", "standards.tsv row 2, Neg RT"):
            self.assertIn(fragment, blockers.replace("settings.tsv", "Settings"))
        self.assertNotIn("Pos m/z", blockers)  # computed from the formula
        # Fill in what it asked for: the amounts and RTs of the example, and the sample amount.
        example = {row[0]: row for row in q.converted_tables(
            EXAMPLE / "quant_utility_table.xlsx", "H", EXAMPLE / "EquiSPLASH_Pos.txt", EXAMPLE / "EquiSPLASH_Neg.txt")[0]["standards"]}
        rows = list(csv.reader(io.StringIO((target / "standards.tsv").read_text(encoding="utf-8")), delimiter="\t"))
        for row in rows[1:]:
            source = example[row[0]]
            row[2] = q._text_value(source[2])
            row[8] = q._text_value(source[8]) if row[6] else ""
            row[12] = q._text_value(source[12]) if row[10] else ""
        _write_table(target / "standards.tsv", rows[0], rows[1:])
        _write_table(target / "settings.tsv", HEADER["settings"], SETTINGS)
        read = q.read_quant_input(target)
        self.assertEqual([], read["blockers"])
        legacy = read_quant_workbook(EXAMPLE / "quant_utility_table.xlsx", "H")
        self.assertEqual(legacy["quant_info"]["selected"], read["quant_info"]["selected"])
        self.assertEqual([item["amount"] for item in legacy["preparation"]["standards"]],
                         [item["amount"] for item in read["preparation"]["standards"]])

    def test_ammonium_formate_takes_formate_adducts_in_negative_mode(self) -> None:
        target = self.base / "formate"
        q.quant_input_template({"output_path": str(target), "layout": "tsv", "salt": "ammonium formate", "confirmed": True})
        selection = list(csv.reader(io.StringIO((target / "selection.tsv").read_text(encoding="utf-8")), delimiter="\t"))[1:]
        used = [(row[0], row[1]) for row in selection if row[2] == "Negative" and row[3] == "TRUE"]
        self.assertFalse([item for item in used if item[1] == "[M+CH3COO]-"])
        acetate = {row[0] for row in q.lab_defaults()["selection"] if row[1] == "[M+CH3COO]-" and row[3] == "TRUE"}
        self.assertEqual(acetate, {klass for klass, adduct in used if adduct == "[M+HCOO]-"})
        standards = (target / "standards.tsv").read_text(encoding="utf-8")
        self.assertNotIn("[M+CH3COO]-", standards)
        self.assertIn("[M+HCOO]-", standards)

    def test_prefill_none_writes_the_headers_only(self) -> None:
        target = self.base / "bare"
        q.quant_input_template({"output_path": str(target), "layout": "tsv", "prefill": "none", "confirmed": True})
        self.assertEqual(HEADER["standards"], (target / "standards.tsv").read_text(encoding="utf-8").splitlines()[0].split("\t"))
        self.assertEqual(1, len((target / "selection.tsv").read_text(encoding="utf-8").splitlines()))

    def test_the_earlier_form_converted_reads_as_it(self) -> None:
        target = self.base / "converted.xlsx"
        result = q.quant_input_template({
            "output_path": str(target), "from_quant_table_path": str(EXAMPLE / "quant_utility_table.xlsx"),
            "amount_column": "H", "is_text_pos": str(EXAMPLE / "EquiSPLASH_Pos.txt"),
            "is_text_neg": str(EXAMPLE / "EquiSPLASH_Neg.txt"), "confirmed": True,
        })
        self.assertEqual(([], []), (result["check"]["blockers"], result["check"]["warnings"]))
        self.assertEqual("converted", result["kind"])


class TheWorkflowReadsEitherForm(unittest.TestCase):
    """The same batch through check, contract, resolution, normalize and merge, from both forms."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patches = capabilities_patches()
        self.patches.__enter__()
        self.addCleanup(self.patches.__exit__, None, None, None)

    def batch(self, name: str, layout: str | None) -> tuple[Batch, dict]:
        batch = Batch(Path(self.directory.name) / name)
        if layout is None:
            return batch, {}
        tables, problems, _ = q.converted_tables(batch.workbook, "H", batch.is_pos, batch.is_neg)
        self.assertEqual([], problems)
        target = batch.workbook.parent / ("quant_input.xlsx" if layout == "xlsx" else "quant_input")
        q.write_input(target, layout, tables, q.load_lipid_class_vocabulary())
        return batch, {"quant_table_path": str(target), "is_text_pos": "", "is_text_neg": "", "amount_column": ""}

    def lipidome(self, batch: Batch, extra: dict) -> tuple[dict, bytes]:
        contract = batch.contract(**extra)
        runs = {polarity: write_run(batch, contract, polarity) for polarity in (POSITIVE, NEGATIVE)}
        resolved = lab_lipidomics_agent.resolve_standards_for_contract(contract, dict(runs))
        for polarity, decisions in ((POSITIVE, {"accept_searched": ["Cer 18:1;O2/15:0(d7)"]}), (NEGATIVE, {})):
            digest = resolved["polarities"][polarity]["resolution_digest"]
            plan = lab_lipidomics_agent.plan_normalize(contract, runs[polarity], digest, **decisions)
            lab_lipidomics_agent.write_normalize_inputs(plan)
            _, _, report = normalize_outputs(lab_lipidomics_agent.public_normalize_plan(plan), polarity)
            lab_lipidomics_agent.finish_normalize(plan, 0, report.splitlines())
        written = lab_lipidomics_agent.merge_lipidome(contract, write=True)
        return lab_lipidomics_agent.load_contract(contract), (batch.analysis / "quant" / "lipidome" / "lipid_metabolome.csv").read_bytes()

    def test_both_forms_give_the_same_lipidome(self) -> None:
        legacy_contract, legacy = self.lipidome(*self.batch("legacy", None))
        for layout in ("xlsx", "tsv"):
            batch, extra = self.batch(layout, layout)
            contract, lipidome = self.lipidome(batch, extra)
            self.assertEqual(legacy, lipidome, layout)
            self.assertEqual(q.FORMAT_ID, contract["quant_format"])
            self.assertIsNone(contract["amount_column"])
            self.assertEqual(legacy_contract["unit"], contract["unit"])
            self.assertEqual(legacy_contract["lookup"], contract["lookup"])
            self.assertEqual(sorted(legacy_contract["standards"], key=str), sorted(contract["standards"], key=str))
            self.assertEqual(contract["inputs"]["quant_table"]["path"], contract["inputs"]["internal_standard_texts"][POSITIVE]["path"])
            self.assertEqual([], lab_lipidomics_agent.contract_drift(contract))

    def test_the_check_takes_a_format_one_input_alone(self) -> None:
        batch, extra = self.batch("check", "tsv")
        legacy = lab_lipidomics_agent.check_inputs(batch.request())
        check = lab_lipidomics_agent.check_inputs(batch.request(**extra))
        self.assertTrue(check["ready_for_contract"], (check["questions"], check["blockers"]))
        self.assertEqual(q.FORMAT_ID, check["quant_format"])
        self.assertEqual(legacy["expected_resolution"], check["expected_resolution"])
        self.assertEqual(legacy["derived_preview"], check["derived_preview"])
        self.assertEqual(
            {record.name for polarity in (POSITIVE, NEGATIVE) for record in read_internal_standard_text(
                batch.is_pos if polarity == POSITIVE else batch.is_neg, polarity)["records"]},
            {item["name"] for item in check["quant_input"]["standards"]},
        )
        both = lab_lipidomics_agent.check_inputs(batch.request(quant_table_path=extra["quant_table_path"]))
        self.assertTrue(any("is_text_pos is not given with a format-1 input" in item for item in both["blockers"]))
        self.assertTrue(any("amount_column is not given with a format-1 input" in item for item in both["blockers"]))

    def test_a_changed_folder_input_makes_the_contract_unusable(self) -> None:
        batch, extra = self.batch("drift", "tsv")
        contract = lab_lipidomics_agent.load_contract(batch.contract(**extra))
        settings = Path(extra["quant_table_path"]) / "settings.tsv"
        settings.write_text(settings.read_text(encoding="utf-8").replace("\t10\n", "\t20\n"), encoding="utf-8")
        self.assertTrue(any("quant_input has changed" in item for item in lab_lipidomics_agent.contract_drift(contract)))

    def test_a_workset_of_a_format_one_contract_carries_the_one_input(self) -> None:
        batch, extra = self.batch("workset", "xlsx")
        settings = lab_lipidomics_agent.lab_workset_settings(batch.contract(**extra))
        self.assertEqual(extra["quant_table_path"], settings["answers"]["quant_table_path"])
        for key in ("is_text_pos", "is_text_neg", "amount_column"):
            self.assertNotIn(key, settings["answers"])

    def test_the_input_checks_on_its_own_without_raw_data(self) -> None:
        batch, extra = self.batch("alone", "xlsx")
        result = lab_lipidomics_agent.check_quant_input({"quant_table_path": extra["quant_table_path"], "solvent_type": "HCOONH4"})
        self.assertEqual(q.FORMAT_ID, result["format"])
        self.assertFalse(result["ready"])  # the fixture's acetate selection under formate
        self.assertTrue(result["salt_evidence"])
        legacy = lab_lipidomics_agent.check_quant_input({"quant_table_path": str(batch.workbook)})
        self.assertTrue(any("give amount_column" in item for item in legacy["blockers"]))
        legacy = lab_lipidomics_agent.check_quant_input({
            "quant_table_path": str(batch.workbook), "amount_column": "H", "is_text_pos": str(batch.is_pos),
            "is_text_neg": str(batch.is_neg),
        })
        self.assertEqual([], legacy["blockers"])
        alone = lab_lipidomics_agent.check_quant_input({"quant_table_path": extra["quant_table_path"]})
        self.assertEqual(legacy["expected_resolution"], alone["expected_resolution"])
        self.assertTrue(alone["ready"])


if __name__ == "__main__":
    unittest.main()

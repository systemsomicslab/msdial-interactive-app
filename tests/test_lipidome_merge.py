from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from msdial_app.lipidome_merge import (
    MergeRefused,
    compare_sample_order,
    describe_export,
    merge_pos_neg,
    read_adduct_rules,
    should_keep,
)

HEADER_WIDTH = 35


def _export(samples: list[tuple[str, str]], rows: list[list[str]]) -> str:
    """An MS-DIAL alignment export: four label rows, a column-name row, then data."""

    def label_row(label: str, values: list[str]) -> list[str]:
        return [""] * (HEADER_WIDTH - 1) + [label] + values + ["NA", "NA"]

    names = [name for name, _ in samples]
    classes = [group for _, group in samples]
    columns = [""] * HEADER_WIDTH
    columns[0] = "Alignment ID"
    columns[3] = "Metabolite name"
    columns[4] = "Adduct type"
    columns[11] = "Ontology"
    columns[18] = "Comment"
    lines = [
        label_row("Class", classes),
        label_row("File type", ["Sample"] * len(samples)),
        label_row("Injection order", [str(index + 1) for index in range(len(samples))]),
        label_row("Batch ID", ["1"] * len(samples)),
        columns + names + ["Average", "Stdev"],
    ]
    lines.extend(rows)
    return "\n".join("\t".join(cell for cell in line) for line in lines) + "\n"


def _row(name: str, adduct: str, ontology: str, comment: str = "", values: list[str] | None = None) -> list[str]:
    cells = [""] * HEADER_WIDTH
    cells[0] = "1"
    cells[3] = name
    cells[4] = adduct
    cells[11] = ontology
    cells[18] = comment
    return cells + (values or ["100", "200"]) + ["150", "50"]


RULES = "Class\tAdduct\tIon mode\tIsSelected\nPC\t[M+H]+\tPositive\tTRUE\nPC\t[M+HCOO]-\tNegative\tFALSE\nPE\t[M-H]-\tNegative\tTRUE\n"


class AdductRuleTests(unittest.TestCase):
    def test_only_the_selected_combination_of_a_class_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rules.txt"
            path.write_text(RULES, encoding="utf-8")
            rules = read_adduct_rules(path)

        self.assertIn(("PC", "[M+H]+"), rules["selected"])
        self.assertNotIn(("PC", "[M+HCOO]-"), rules["selected"])
        self.assertEqual(3, rules["rule_count"])

    def test_a_table_selecting_nothing_is_refused_rather_than_dropping_everything(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rules.txt"
            path.write_text("Class\tAdduct\tIon mode\tIsSelected\nPC\t[M+H]+\tPositive\tFALSE\n", encoding="utf-8")
            with self.assertRaisesRegex(MergeRefused, "every one is unselected"):
                read_adduct_rules(path)

    def test_a_numeric_selection_flag_is_read_the_same_way(self) -> None:
        # The rule table is kept both as text and as a spreadsheet sheet, which writes 1/0.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rules.txt"
            path.write_text("Class\tAdduct\tIon mode\tIsSelected\nPC\t[M+H]+\tPositive\t1\n", encoding="utf-8")
            self.assertIn(("PC", "[M+H]+"), read_adduct_rules(path)["selected"])


class RowSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "rules.txt"
        path.write_text(RULES, encoding="utf-8")
        self.rules = read_adduct_rules(path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_selected_class_and_adduct_is_kept(self) -> None:
        keep, reason = should_keep(_row("PC 34:1", "[M+H]+", "PC"), self.rules)
        self.assertTrue(keep, reason)

    def test_the_same_lipid_in_an_unselected_adduct_is_dropped(self) -> None:
        keep, reason = should_keep(_row("PC 34:1", "[M+HCOO]-", "PC"), self.rules)
        self.assertFalse(keep)
        self.assertIn("[M+HCOO]-", reason)

    def test_a_deuterated_standard_never_reaches_the_lipidome(self) -> None:
        # The standards quantify the sample; they are not part of it.
        keep, _ = should_keep(_row("PC 15:0_18:1(d7)", "[M+H]+", "PC"), self.rules)
        self.assertFalse(keep)

    def test_an_unannotated_row_is_dropped(self) -> None:
        keep, reason = should_keep(_row("Unknown", "[M+H]+", "PC"), self.rules)
        self.assertFalse(keep)
        self.assertEqual("unannotated", reason)

    def test_a_row_marked_for_exclusion_in_the_comment_is_dropped(self) -> None:
        keep, reason = should_keep(_row("PC 34:1", "[M+H]+", "PC", comment="x removed on review"), self.rules)
        self.assertFalse(keep)
        self.assertIn("manually excluded", reason)

    def test_an_annotation_below_the_reporting_bar_is_dropped(self) -> None:
        for name in ("low score: PC 34:1", "no MS2: PC 34:1"):
            keep, reason = should_keep(_row(name, "[M+H]+", "PC"), self.rules)
            self.assertFalse(keep, name)
            self.assertIn("reporting bar", reason)


class SamplePairingTests(unittest.TestCase):
    def _write(self, directory: Path, samples: list[tuple[str, str]], name: str) -> Path:
        path = directory / name
        path.write_text(_export(samples, [_row("PC 34:1", "[M+H]+", "PC")]), encoding="utf-8")
        return path

    def test_the_same_samples_in_the_same_order_pair_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            positive = self._write(directory, [("A_Pos", "SPF"), ("B_Pos", "GF")], "pos.txt")
            negative = self._write(directory, [("A_Neg", "SPF"), ("B_Neg", "GF")], "neg.txt")
            order = compare_sample_order(describe_export(positive), describe_export(negative))

        self.assertTrue(order["matched"], order["problems"])
        self.assertEqual(2, order["positive_sample_count"])

    def test_a_different_number_of_samples_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            positive = self._write(directory, [("A_Pos", "SPF"), ("B_Pos", "GF")], "pos.txt")
            negative = self._write(directory, [("A_Neg", "SPF")], "neg.txt")
            rules = directory / "rules.txt"
            rules.write_text(RULES, encoding="utf-8")
            with self.assertRaisesRegex(MergeRefused, "negative lists 1"):
                merge_pos_neg(positive, negative, rules, directory / "out.txt")

    def test_the_same_samples_in_a_different_order_are_refused(self) -> None:
        # The merged table keeps one set of sample columns and pairs them by position,
        # so a reordered second polarity would attach every value to the wrong sample.
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            positive = self._write(directory, [("A_Pos", "SPF"), ("B_Pos", "GF")], "pos.txt")
            negative = self._write(directory, [("B_Neg", "GF"), ("A_Neg", "SPF")], "neg.txt")
            rules = directory / "rules.txt"
            rules.write_text(RULES, encoding="utf-8")
            with self.assertRaisesRegex(MergeRefused, "position 1"):
                merge_pos_neg(positive, negative, rules, directory / "out.txt")

    def test_a_class_that_changed_between_polarities_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            positive = self._write(directory, [("A_Pos", "SPF")], "pos.txt")
            negative = self._write(directory, [("A_Neg", "GF")], "neg.txt")
            rules = directory / "rules.txt"
            rules.write_text(RULES, encoding="utf-8")
            with self.assertRaisesRegex(MergeRefused, "class 'SPF'"):
                merge_pos_neg(positive, negative, rules, directory / "out.txt")


class MergeTests(unittest.TestCase):
    def test_each_polarity_contributes_the_classes_it_quantifies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            samples_pos = [("A_Pos", "SPF"), ("B_Pos", "GF")]
            samples_neg = [("A_Neg", "SPF"), ("B_Neg", "GF")]
            positive = directory / "pos.txt"
            positive.write_text(
                _export(samples_pos, [
                    _row("PC 34:1", "[M+H]+", "PC"),
                    _row("PE 34:1", "[M+H]+", "PE"),
                ]),
                encoding="utf-8",
            )
            negative = directory / "neg.txt"
            negative.write_text(
                _export(samples_neg, [
                    _row("PE 34:1", "[M-H]-", "PE"),
                    _row("PC 34:1", "[M+HCOO]-", "PC"),
                ]),
                encoding="utf-8",
            )
            rules = directory / "rules.txt"
            rules.write_text(RULES, encoding="utf-8")
            result = merge_pos_neg(positive, negative, rules, directory / "merged.txt")
            written = (directory / "merged.txt").read_text(encoding="utf-8").splitlines()

        self.assertEqual(2, result["kept_rows"])
        self.assertEqual(1, result["kept_from_positive"])
        self.assertEqual(1, result["kept_from_negative"])
        self.assertEqual(["PC", "PE"], result["ontologies"])
        self.assertEqual(5 + 2, len(written), "the merged table keeps one set of header rows")
        self.assertTrue(written[0].startswith("\t"), "the class row is preserved")

    def test_every_dropped_row_is_accounted_for_by_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            positive = directory / "pos.txt"
            positive.write_text(
                _export([("A_Pos", "SPF")], [
                    _row("PC 34:1", "[M+H]+", "PC"),
                    _row("Unknown", "[M+H]+", ""),
                    _row("PC 15:0_18:1(d7)", "[M+H]+", "PC"),
                ]),
                encoding="utf-8",
            )
            negative = directory / "neg.txt"
            negative.write_text(
                _export([("A_Neg", "SPF")], [_row("PC 34:1", "[M+HCOO]-", "PC")]),
                encoding="utf-8",
            )
            rules = directory / "rules.txt"
            rules.write_text(RULES, encoding="utf-8")
            result = merge_pos_neg(positive, negative, rules, directory / "merged.txt")

        self.assertEqual(1, result["kept_rows"])
        self.assertEqual(3, result["dropped_rows"])
        self.assertEqual(3, sum(result["dropped_reasons"].values()))


class ExportGeometryTests(unittest.TestCase):
    """Rows from both exports are concatenated and then read by position."""

    def _write(self, path: Path, samples: list[tuple[str, str]], width: int) -> Path:
        global HEADER_WIDTH
        original = HEADER_WIDTH
        HEADER_WIDTH = width
        try:
            path.write_text(
                _export(samples, [_row("PC 34:1", "[M+H]+", "PC")]), encoding="utf-8"
            )
        finally:
            HEADER_WIDTH = original
        return path

    def test_exports_whose_metadata_blocks_differ_in_width_are_refused(self) -> None:
        # Otherwise every negative value lands under the wrong sample, in every row.
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            positive = self._write(directory / "pos.txt", [("A_Pos", "SPF")], 35)
            negative = self._write(directory / "neg.txt", [("A_Neg", "SPF")], 40)
            rules = directory / "rules.txt"
            rules.write_text(RULES, encoding="utf-8")
            with self.assertRaisesRegex(MergeRefused, "not the same shape"):
                merge_pos_neg(positive, negative, rules, directory / "out.txt")

    def test_metadata_columns_are_located_by_header_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write(Path(temporary) / "pos.txt", [("A_Pos", "SPF")], 35)
            columns = describe_export(path)["columns"]

        self.assertEqual(3, columns["metabolite_name"])
        self.assertEqual(11, columns["ontology"])
        self.assertEqual(18, columns["comment"])


class RuleTableIdentityTests(unittest.TestCase):
    """A search list and a selection list have the same four columns."""

    def test_the_merge_reports_what_the_rule_table_selects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rules = directory / "rules.txt"
            rules.write_text(RULES, encoding="utf-8")
            read = read_adduct_rules(rules)

        self.assertEqual({"Negative": 1, "Positive": 1}, read["selected_by_ion_mode"])
        self.assertEqual(2, read["selected_classes"])
        self.assertEqual(3, read["rule_count"])

    def test_a_table_selecting_many_adducts_per_class_is_visible_as_such(self) -> None:
        # A selection list keeps about one adduct per class per polarity; a search list
        # keeps many. The counts say which was passed before its numbers are used.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "search.txt"
            lines = ["Class\tAdduct\tIon mode\tIsSelected"]
            for adduct in ("[M+H]+", "[M+Na]+", "[M+NH4]+"):
                lines.append(f"PC\t{adduct}\tPositive\tTRUE")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            read = read_adduct_rules(path)

        self.assertEqual(1, read["selected_classes"])
        self.assertEqual(3, len(read["selected"]))
        self.assertEqual({"Positive": 3}, read["selected_by_ion_mode"])


from msdial_app.lipidome_merge import collation_key, reference_matched_ids  # noqa: E402
from lab_export_fixture import candidate, write_candidates  # noqa: E402
from xlsx_fixture import quant_sheet, quant_workbook  # noqa: E402

UNIT = "Normalized unit: pmol_per_microL_plasma"
LAB_RULES = (
    "Class\tAdduct\tIon mode\tIsSelected\n"
    "PC\t[M+H]+\tPositive\tTRUE\nPE\t[M-H]-\tNegative\tTRUE\n"
    "BRSE\t[M+NH4]+\tPositive\tTRUE\nBileAcid\t[M-H]-\tNegative\tTRUE\n"
    "EtherPE\t[M+H]+\tPositive\tTRUE\nEtherPE\t[M-H]-\tNegative\tTRUE\nCAR\t[M+H]+\tPositive\tTRUE\n"
)


def _identified(alignment_id: int, name: str, adduct: str, ontology: str, comment: str = UNIT,
                formula: str = "", values: list[str] | None = None) -> list[str]:
    cells = _row(name, adduct, ontology, comment, values)
    cells[0] = str(alignment_id)
    cells[10] = formula
    return cells


class LaboratoryMergeTests(unittest.TestCase):
    """The laboratory's merge: its file format, its order, and what never reaches the lipidome."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.rules = self.root / "rules.txt"
        self.rules.write_text(LAB_RULES, encoding="utf-8")

    def tearDown(self) -> None:
        self._directory.cleanup()

    def merge(self, positive_rows, negative_rows, output: str = "lipid_metabolome.csv", **options):
        positive = self.root / "pos.txt"
        negative = self.root / "neg.txt"
        # An export always has rows; an unannotated one stands in where a test needs none.
        filler = [_identified(99, "Unknown", "[M-H]-", "")]
        positive.write_text(_export([("A_Pos", "SPF"), ("B_Pos", "GF")], positive_rows or filler), encoding="utf-8")
        negative.write_text(_export([("A_Neg", "SPF"), ("B_Neg", "GF")], negative_rows or filler), encoding="utf-8")
        result = merge_pos_neg(positive, negative, self.rules, self.root / output, **options)
        return result, (self.root / output).read_bytes()

    def test_a_csv_is_comma_separated_with_crlf_as_the_laboratory_program_wrote_it(self) -> None:
        _, data = self.merge([_identified(1, "PC 34:1", "[M+H]+", "PC")], [_identified(2, "PE 34:1", "[M-H]-", "PE")])
        self.assertNotIn(b"\t", data)
        self.assertEqual(5 + 2, data.count(b"\r\n"))
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertTrue(data.startswith(b","))

    def test_a_cell_holding_a_comma_is_written_unquoted_as_the_program_did_and_said(self) -> None:
        # metaboservice joined cells with ',' and never quoted; such a row has an extra column.
        result, data = self.merge([_identified(1, "PC 34:1", "[M+H]+", "PC", comment="checked, kept")], [])
        self.assertIn(b",checked, kept,", data)
        self.assertNotIn(b'"', data)
        self.assertEqual("none", result["format"]["quoting"])
        self.assertTrue(any("inside a cell (1)" in item and "unquoted" in item for item in result["warnings"]))

    def test_rows_are_ordered_the_way_dotnet_compares_names(self) -> None:
        names = ["BRSE", "BileAcid", "CAR", "CerP", "Cer_NS"]
        self.assertEqual(["BileAcid", "BRSE", "CAR", "Cer_NS", "CerP"], sorted(names, key=collation_key))
        result, data = self.merge(
            [_identified(1, "BRSE 18:1", "[M+NH4]+", "BRSE"), _identified(2, "CAR 16:0", "[M+H]+", "CAR")],
            [_identified(3, "BileAcid 24:1", "[M-H]-", "BileAcid")],
        )
        order = [line.split(b",")[11] for line in data.split(b"\r\n")[5:] if line]
        self.assertEqual([b"BileAcid", b"BRSE", b"CAR"], order)
        self.assertEqual(3, result["kept_rows"])

    def test_a_standard_is_excluded_by_its_alignment_id_even_without_a_label(self) -> None:
        result, _ = self.merge(
            [_identified(1, "PC 33:1", "[M+H]+", "PC"), _identified(2, "PC 34:1", "[M+H]+", "PC")], [],
            internal_standard_ids={"positive": {"1"}},
        )
        self.assertEqual(1, result["kept_rows"])
        self.assertEqual({"internal standard": 1}, result["dropped_by_polarity"]["positive"])

    def test_with_the_candidate_table_only_reference_matched_rows_are_kept(self) -> None:
        table = write_candidates(self.root / "pos.mdcandidate.tsv", [
            candidate(1, "PC 34:1"), candidate(2, "PC 36:2", matched=False, suggested=True),
        ])
        matched = reference_matched_ids(table)
        self.assertEqual({"1"}, matched)
        result, _ = self.merge(
            [_identified(1, "PC 34:1", "[M+H]+", "PC"), _identified(2, "PC 36:2", "[M+H]+", "PC")],
            [_identified(3, "PE 34:1", "[M-H]-", "PE")],
            reference_matched_ids={"positive": matched, "negative": None},
        )
        self.assertEqual((1, 1), (result["kept_from_positive"], result["kept_from_negative"]))
        self.assertEqual({"not reference-matched": 1}, result["dropped_by_polarity"]["positive"])

    def test_selected_rows_without_a_concentration_go_to_their_own_file(self) -> None:
        result, _ = self.merge(
            [
                _identified(1, "PC 34:1", "[M+H]+", "PC"),
                _identified(2, "PC 36:2", "[M+H]+", "PC", comment="NOT QUANTIFIED: the PC standard X did not resolve"),
                _identified(3, "PC 38:4", "[M+H]+", "PC", comment="Normalized unit: Intensity"),
                _identified(4, "PC 40:6", "[M+H]+", "PC", comment="Normalized unit: pmol_per_mg_tissue"),
                _identified(5, "PC 32:0", "[M+H]+", "PC"),
            ],
            [],
            expected_unit="pmol_per_microL_plasma",
            unquantified_ids={"positive": {"5"}},
            not_quantified_path=self.root / "lipid_metabolome_not_quantified.csv",
        )
        self.assertEqual(1, result["kept_rows"])
        self.assertEqual(4, result["not_quantified"]["rows"])
        self.assertEqual({"PC": 4}, result["not_quantified"]["by_class"])
        side = (self.root / "lipid_metabolome_not_quantified.csv").read_bytes()
        self.assertEqual(5 + 4, side.count(b"\r\n"))
        reasons = [item["reason"] for item in result["not_quantified"]["items"]]
        self.assertTrue(all(reason.startswith("not quantified") for reason in reasons))
        self.assertEqual({}, result["dropped_by_polarity"]["positive"])

    def test_without_a_side_file_they_are_counted_as_dropped(self) -> None:
        result, _ = self.merge(
            [_identified(2, "PC 36:2", "[M+H]+", "PC", comment="NOT QUANTIFIED: the PC standard X did not resolve")], [],
        )
        self.assertEqual(1, result["dropped_reasons"]["not quantified: its standard did not resolve"])
        self.assertIsNone(result["not_quantified"]["output"])

    def test_non_ascii_text_is_written_as_utf8_and_said(self) -> None:
        result, data = self.merge([_identified(1, "PC 34:1", "[M+H]+", "PC", comment="確認")], [])
        self.assertEqual("utf-8", result["format"]["encoding"])
        self.assertTrue(result["warnings"])
        self.assertIn("確認".encode("utf-8"), data)

    def test_the_report_names_double_counts_and_selections_without_rows(self) -> None:
        result, _ = self.merge(
            [_identified(1, "PE P-36:2", "[M+H]+", "EtherPE", formula="C41H78NO7P"),
             _identified(2, "PC 34:1", "[M+H]+", "PC", formula="C42H82NO8P")],
            [_identified(3, "PE O-36:3", "[M-H]-", "EtherPE", formula="C41H78NO7P"),
             _identified(4, "PE 37:1", "[M-H]-", "PE", formula="C42H82NO8P")],
            report_path=self.root / "merge-report.json",
        )
        # PC 34:1 and PE 37:1 share a formula and are different lipids; only EtherPE is a double count.
        self.assertEqual(["EtherPE"], [group["ontology"] for group in result["possible_double_counts"]])
        self.assertIn({"class": "BRSE", "adduct": "[M+NH4]+"}, result["selected_pairs_without_rows"])
        self.assertEqual(["EtherPE"], result["ontologies_from_both_polarities"])
        self.assertTrue((self.root / "merge-report.json").is_file())
        self.assertEqual(64, len(result["inputs"]["positive_sha256"]))

    def test_the_selection_is_read_from_the_workbooks_third_sheet(self) -> None:
        rules = read_adduct_rules(quant_workbook(self.root / "quant_utility_table.xlsx"))
        self.assertIn(("PC", "[M+CH3COO]-"), rules["selected"])
        self.assertEqual(["EtherPE"], [item["class"] for item in rules["multi_adduct_classes"]])

    def test_a_workbook_selection_with_a_problem_is_refused(self) -> None:
        bad = quant_sheet([("PC", "[M+H]+", "Negative", True)])
        path = quant_workbook(self.root / "quant_utility_table.xlsx", quant=bad)
        with self.assertRaisesRegex(MergeRefused, "cannot be used"):
            read_adduct_rules(path)


if __name__ == "__main__":
    unittest.main()

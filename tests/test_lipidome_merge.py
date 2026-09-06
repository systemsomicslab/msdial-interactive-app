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


if __name__ == "__main__":
    unittest.main()


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

"""What the laboratory's merge program (metaboservice) wrote, and the .NET behaviour it rests on.

The tables metabolomics_service.R reads (lipid_metabolome4stat.csv, ...4classsummary.csv,
...4summaryplot.csv) and its dotplot folders; numbers as .NET Framework's Double.ToString(); rows in
the order Windows' culture-sensitive comparison gives. The 2024 byte-for-byte reproduction is in
test_lipidome_golden (opt-in).
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import random
import tempfile
import unittest
from pathlib import Path

from lab_export_fixture import export_text, spot, write_export
from msdial_app.lipidome_merge import (
    CATEGORY_TABLE_PATH,
    MergeRefused,
    lipid_category,
    load_lipid_category_table,
    merge_pos_neg,
    net_double_parse,
    net_double_tostring,
    nls_sort_key,
    number_text,
)

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "scripts" / "generate-lipid-category-table.py"
SAMPLES = {
    "Positive": [("A_Pos", "SPF", "Sample"), ("B_Pos", "GF", "Sample"), ("QC_Pos", "QC", "QC"), ("Blank_Pos", "Blank", "Blank")],
    "Negative": [("A_Neg", "SPF", "Sample"), ("B_Neg", "GF", "Sample"), ("QC_Neg", "QC", "QC"), ("Blank_Neg", "Blank", "Blank")],
}
RULES = (
    "Class\tAdduct\tIon mode\tIsSelected\n"
    "PC\t[M+H]+\tPositive\tTRUE\nLPC\t[M+H]+\tPositive\tTRUE\nCAR\t[M+H]+\tPositive\tTRUE\n"
    "PE\t[M-H]-\tNegative\tTRUE\nBMP\t[M-H]-\tNegative\tTRUE\nEtherPE\t[M-H]-\tNegative\tTRUE\n"
)


def lipid(alignment_id: int, name: str, adduct: str, ontology: str, cells, rt: float = 5.0):
    return spot(alignment_id, 700.0, rt, name, adduct=adduct, ontology=ontology, cells=list(cells), comment="")


def lines(path: Path) -> list[str]:
    data = path.read_bytes()
    assert data.endswith(b"\r\n") and b"\n" not in data.replace(b"\r\n", b""), path
    return data.decode("ascii").split("\r\n")[:-1]


class NumberTextTests(unittest.TestCase):
    def test_fifteen_significant_digits_as_dotnet_framework_printed_them(self) -> None:
        cases = {
            0.0: "0", -0.0: "0", 1.5: "1.5", 100.0: "100", 0.1 + 0.2: "0.3",
            123456789012345.0: "123456789012345", 1e15: "1E+15", 1234567890123456.0: "1.23456789012346E+15",
            0.0001: "0.0001", 0.00001: "1E-05", 1.5e-05: "1.5E-05", 1e-100: "1E-100", 1e100: "1E+100",
            -2.5e-07: "-2.5E-07", 2.0 / 3.0: "0.666666666666667", math.log10(2.0): "0.301029995663981",
        }
        for value, text in cases.items():
            with self.subTest(value=repr(value)):
                self.assertEqual(text, net_double_tostring(value))

    def test_an_exact_tie_rounds_half_up_not_to_even(self) -> None:
        # 1000000000000005 is exact in binary; Python's '%.15G' gives 1E+15 (to even).
        self.assertEqual("1E+15", "%.15G" % 1000000000000005.0)
        self.assertEqual("1.00000000000001E+15", net_double_tostring(1000000000000005.0))
        self.assertEqual("-1.00000000000001E+15", net_double_tostring(-1000000000000005.0))

    def test_what_has_no_number_is_written_as_the_ascii_writer_wrote_it(self) -> None:
        self.assertEqual("NaN", net_double_tostring(math.nan))
        self.assertEqual("-\u221e", net_double_tostring(-math.inf))
        self.assertEqual("-?", number_text(-math.inf))
        self.assertEqual("?", number_text(math.inf))

    def test_parsing_follows_double_parse(self) -> None:
        self.assertEqual(1234.5, net_double_parse("1,234.5"))
        self.assertEqual(3.0, net_double_parse(" 3 "))
        self.assertEqual(1.5e-05, net_double_parse("1.5E-05"))
        self.assertEqual(0.5, net_double_parse(".5"))
        self.assertEqual(5.0, net_double_parse("5."))
        self.assertTrue(math.isnan(net_double_parse("NaN")))
        self.assertEqual(-math.inf, net_double_parse("-\u221e"))
        for text in ("", "abc", "1.2.3", "e5", "-"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                net_double_parse(text)

    def test_a_value_reprinted_from_its_own_text_is_unchanged(self) -> None:
        # Values in 4stat are MS-DIAL's 15-digit text parsed and printed again.
        for text in ("12.3456789012345", "0.000123", "1.5E-05", "98765.4321"):
            with self.subTest(text=text):
                self.assertEqual(text, net_double_tostring(net_double_parse(text)))


class SortKeyTests(unittest.TestCase):
    def test_dotnet_order_is_not_ordinal_order(self) -> None:
        self.assertEqual(["BileAcid", "BRSE", "CAR", "Cer_NS", "CerP"],
                         sorted(["BRSE", "BileAcid", "CAR", "CerP", "Cer_NS"], key=nls_sort_key))
        self.assertEqual(["TG 56:1|TG 18:1_18:0_20:0", "TG 56:10|TG 18:1_18:3_20:6"],
                         sorted(["TG 56:10|TG 18:1_18:3_20:6", "TG 56:1|TG 18:1_18:0_20:0"], key=nls_sort_key))
        # Symbols before digits before letters; lower case first only when all else is equal.
        self.assertEqual([" ", "_", "0", "a", "A", "b"], sorted(["b", "A", "a", "0", "_", " "], key=nls_sort_key))

    def test_a_hyphen_and_an_apostrophe_count_only_as_a_last_tie_break(self) -> None:
        self.assertEqual(["PEA", "PECer", "PE-Cer"], sorted(["PE-Cer", "PECer", "PEA"], key=nls_sort_key))
        self.assertLess(nls_sort_key("coop"), nls_sort_key("co-op"))
        self.assertLess(nls_sort_key("PE O-36:2"), nls_sort_key("PE P-34:1"))

    def test_text_outside_ascii_sorts_as_the_question_marks_the_program_read(self) -> None:
        self.assertEqual(nls_sort_key("??"), nls_sort_key("\u00c4"))
        self.assertEqual(nls_sort_key("A???"), nls_sort_key("A\u78ba"))

    @unittest.skipUnless(os.name == "nt", "LCMapStringEx is a Windows function")
    def test_the_keys_are_the_ones_windows_computes(self) -> None:
        names = [
            "BileAcid", "BRSE", "Cer_NS", "Cer-NS", "PE O-16:1_22:6", "PE P-16:0_22:6", "TG 56:1|TG 18:1_18:0_20:0",
            "co-op", "coop", "it's", "its", "ABC", "abc", "a b", "a_b", "a.b", "[M+H]+", "~x", "{y}", "x\ty",
            "SPF_1", "QC_3", "Hex3Cer 42:2;O3", "PC 16:0/18:1", "1,2-dioleoyl", "@ #$%&*()",
        ]
        generator = random.Random(20261014)
        alphabet = [chr(code) for code in range(32, 127)] + ["\t"]
        names += ["".join(generator.choice(alphabet) for _ in range(generator.randint(0, 12))) for _ in range(500)]
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(_windows_sort_key(name), nls_sort_key(name))


def _windows_sort_key(text: str) -> bytes:
    import ctypes

    function = ctypes.WinDLL("kernel32").LCMapStringEx
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_int,
                         ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    function.restype = ctypes.c_int
    flags = 0x400 | 0x08000000  # LCMAP_SORTKEY | NORM_LINGUISTIC_CASING
    if not text:
        return b"\x01\x01\x01\x01\x00"
    size = function("en-US", flags, text, len(text), None, 0, None, None, None)
    buffer = ctypes.create_string_buffer(size)
    function("en-US", flags, text, len(text), buffer, size, None, None, None)
    return buffer.raw[:size]


class _Merge(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.rules = self.root / "rules.txt"
        self.rules.write_text(RULES, encoding="utf-8")
        self.out = self.root / "lipidome"

    def tearDown(self) -> None:
        self._directory.cleanup()

    def merge(self, positive_rows, negative_rows, **options):
        write_export(self.root / "pos.txt", positive_rows, SAMPLES["Positive"])
        write_export(self.root / "neg.txt", negative_rows, SAMPLES["Negative"])
        return merge_pos_neg(self.root / "pos.txt", self.root / "neg.txt", self.rules,
                             self.out / "lipid_metabolome.csv", **options)


class StatisticsTableTests(_Merge):
    def setUp(self) -> None:
        super().setUp()
        self.report = self.merge(
            [lipid(1, "PC 34:1", "[M+H]+", "PC", (10, 20, 30, 999)),
             lipid(2, "LPC 18:1", "[M+H]+", "LPC", (1, 2, 3, 0)),
             lipid(3, "CAR 16:0", "[M+H]+", "CAR", (0, 0, 0, 5))],
            [lipid(1, "PE 34:1|PE 16:0_18:1", "[M-H]-", "PE", (4, 5, 6, 0))],
        )

    def test_the_samples_are_the_sample_and_qc_columns_and_never_a_blank(self) -> None:
        self.assertEqual(["SPF_1", "GF_2", "QC_3"], [item["name"] for item in self.report["samples"]["taken"]])
        self.assertEqual([{"column": 43, "file": "Blank_Pos", "file_type": "Blank"}], self.report["samples"]["not_taken"])
        self.assertEqual(["QC", "Sample"], self.report["samples"]["file_types"])

    def test_the_stat_table_is_samples_by_lipids_in_the_merged_order(self) -> None:
        self.assertEqual([
            "Metabolite,Class,CAR 16:0_3,LPC 18:1_2,PC 34:1_1,PE 16:0_18:1_1",
            "SPF_1,SPF,0,1,10,4",
            "GF_2,GF,0,2,20,5",
            "QC_3,QC,0,3,30,6",
        ], lines(self.out / "lipid_metabolome4stat.csv"))

    def test_the_class_summary_sums_each_ontology(self) -> None:
        self.assertEqual([
            "Metabolite,Class,CAR,LPC,PC,PE", "SPF_1,SPF,0,1,10,4", "GF_2,GF,0,2,20,5", "QC_3,QC,0,3,30,6",
        ], lines(self.out / "lipid_metabolome4classsummary.csv"))

    def test_the_summary_plot_ranks_the_log10_means_and_writes_minus_infinity_as_the_program_did(self) -> None:
        self.assertEqual([
            "Metabolite,Subclass,Order,Intensity",
            "PC 34:1_1,PC,1,1.30102999566398",
            "PE 16:0_18:1_1,PE,2,0.698970004336019",
            "LPC 18:1_2,LPC,3,0.301029995663981",
            "CAR 16:0_3,CAR,4,-?",
        ], lines(self.out / "lipid_metabolome4summaryplot.csv"))
        self.assertTrue(any("no finite log10 mean" in item for item in self.report["warnings"]))
        self.assertTrue(any("constant column" in item for item in self.report["warnings"]))

    def test_the_dotplot_folders_are_the_ones_the_r_script_reads(self) -> None:
        written = sorted(str(path.relative_to(self.out)).replace(os.sep, "/") for path in self.out.rglob("*.csv")
                         if path.parent != self.out)
        self.assertEqual([
            "Summary_Dotplots/FattyAcyls.csv", "Summary_Dotplots/Glycerophospholipids.csv",
            "category_FattyAcyls/CAR.csv", "category_Glycerophospholipids/LPC.csv",
            "category_Glycerophospholipids/PC.csv", "category_Glycerophospholipids/PE.csv",
            "subclass_CAR/CAR 16_0_3.csv", "subclass_LPC/LPC 18_1_2.csv", "subclass_PC/PC 34_1_1.csv",
            "subclass_PE/PE 16_0_18_1_1.csv",
        ], written)
        self.assertEqual(["Value,Class,Metabolite", "20,GF,PC 34:1_1", "30,QC,PC 34:1_1", "10,SPF,PC 34:1_1"],
                         lines(self.out / "subclass_PC" / "PC 34_1_1.csv"))
        # Summary_Dotplots: the log10 means in the merged order, the category as the last column.
        self.assertEqual(["Value,Class,Metabolite", "0.301029995663981,LPC,Glycerophospholipids",
                          "1.30102999566398,PC,Glycerophospholipids", "0.698970004336019,PE,Glycerophospholipids"],
                         lines(self.out / "Summary_Dotplots" / "Glycerophospholipids.csv"))
        self.assertEqual(["Value,Class,Metabolite", "5,GF,PE", "6,QC,PE", "4,SPF,PE"],
                         lines(self.out / "category_Glycerophospholipids" / "PE.csv"))
        self.assertEqual(10, self.report["outputs"]["dotplots"]["files"])
        manifest = self.report["outputs"]["dotplots"]["manifest"]
        data = (self.out / "subclass_PC" / "PC 34_1_1.csv").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), manifest["subclass_PC/PC 34_1_1.csv"])

    def test_every_file_is_ascii_crlf_without_a_byte_order_mark(self) -> None:
        for path in self.out.rglob("*.csv"):
            with self.subTest(path=path.name):
                data = path.read_bytes()
                self.assertTrue(data.isascii())
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
                self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))


class DotplotLimitTests(_Merge):
    def test_only_the_41_largest_lipids_of_a_class_get_a_file(self) -> None:
        rows = [lipid(index, f"PC {index}:0", "[M+H]+", "PC", (index, index, index, 0)) for index in range(1, 43)]
        report = self.merge(rows, [lipid(100, "PE 34:1", "[M-H]-", "PE", (1, 2, 3, 0))])
        files = sorted(path.name for path in (self.out / "subclass_PC").iterdir())
        self.assertEqual(41, len(files))
        self.assertNotIn("PC 1_0_1.csv", files)
        self.assertEqual(43, len(lines(self.out / "lipid_metabolome4stat.csv")[0].split(",")) - 2)
        # subclass_PC 41, subclass_PE 1, category_Glycerophospholipids PC and PE, Summary_Dotplots 1.
        self.assertEqual(41 + 1 + 2 + 1, report["outputs"]["dotplots"]["files"])


class EarlierDotplotTests(_Merge):
    def test_an_earlier_merges_files_are_replaced_and_anything_else_refuses_the_write(self) -> None:
        first = self.merge([lipid(1, "PC 34:1", "[M+H]+", "PC", (1, 2, 3, 0)), lipid(2, "CAR 16:0", "[M+H]+", "CAR", (1, 2, 3, 0))],
                           [lipid(9, "PE 34:1", "[M-H]-", "PE", (1, 2, 3, 0))])
        manifest = first["outputs"]["dotplots"]["manifest"]
        self.assertTrue((self.out / "subclass_CAR" / "CAR 16_0_2.csv").is_file())
        # Without the earlier merge's record, its files are anyone's: the write is refused, the preview says so.
        positive = [lipid(1, "PC 34:1", "[M+H]+", "PC", (1, 2, 3, 0))]
        negative = [lipid(9, "PE 34:1", "[M-H]-", "PE", (1, 2, 3, 0))]
        with self.assertRaisesRegex(MergeRefused, "no earlier merge recorded writing"):
            self.merge(positive, negative)
        preview = self.merge(positive, negative, write=False)
        self.assertIn("Summary_Dotplots/FattyAcyls.csv", preview["outputs"]["dotplots"]["foreign_files"])
        # With it, they are replaced, and a folder left empty is removed (R fails on an empty one).
        second = self.merge(positive, negative, replaceable_dotplot_files=manifest)
        self.assertEqual(3, second["outputs"]["dotplots"]["replaced_files_of_earlier_merge"])
        self.assertFalse((self.out / "subclass_CAR").exists())
        self.assertFalse((self.out / "category_FattyAcyls").exists())
        self.assertFalse((self.out / "Summary_Dotplots" / "FattyAcyls.csv").exists())
        # A file the person put there, or one changed since, is never removed.
        (self.out / "subclass_PC" / "notes.csv").write_text("mine", encoding="ascii")
        with self.assertRaisesRegex(MergeRefused, "notes.csv"):
            self.merge(positive, negative, replaceable_dotplot_files=second["outputs"]["dotplots"]["manifest"])
        self.assertTrue((self.out / "subclass_PC" / "notes.csv").is_file())

    def test_another_folder_beside_the_tables_is_said(self) -> None:
        (self.out / "pos").mkdir(parents=True)
        report = self.merge([lipid(1, "PC 34:1", "[M+H]+", "PC", (1, 2, 3, 0))], [lipid(9, "PE 34:1", "[M-H]-", "PE", (1, 2, 3, 0))])
        self.assertTrue(any("would also read as dotplot folders: pos" in item for item in report["warnings"]))


class SampleColumnTests(_Merge):
    def test_a_file_type_that_differs_between_polarities_is_refused(self) -> None:
        # The positive header decides which columns are samples, for the negative rows as well.
        write_export(self.root / "pos.txt", [lipid(1, "PC 34:1", "[M+H]+", "PC", (1, 2, 3, 0))], SAMPLES["Positive"])
        negative = [(name, group, "Sample" if kind == "QC" else kind) for name, group, kind in SAMPLES["Negative"]]
        write_export(self.root / "neg.txt", [lipid(9, "PE 34:1", "[M-H]-", "PE", (1, 2, 3, 0))], negative)
        with self.assertRaisesRegex(MergeRefused, "file type 'QC' in positive and 'Sample' in negative"):
            merge_pos_neg(self.root / "pos.txt", self.root / "neg.txt", self.rules, self.out / "lipid_metabolome.csv")

    def test_a_selected_row_without_a_number_in_a_sample_cell_is_set_aside(self) -> None:
        # metaboservice stopped on such a cell; a Blank cell is never read.
        report = self.merge([lipid(1, "PC 34:1", "[M+H]+", "PC", (1, "", 3, 0)), lipid(2, "PC 36:1", "[M+H]+", "PC", (1, 2, 3, ""))],
                            [lipid(9, "PE 34:1", "[M-H]-", "PE", (1, 2, 3, 0))],
                            not_quantified_path=self.root / "not_quantified.csv")
        self.assertEqual((1, 1), (report["kept_from_positive"], report["not_quantified"]["rows"]))
        self.assertEqual("not quantified: 1 sample or QC cell(s) hold no number", report["not_quantified"]["items"][0]["reason"])

    def test_the_2024_rule_takes_sample_columns_only(self) -> None:
        report = self.merge([lipid(1, "PC 34:1", "[M+H]+", "PC", (1, 2, 3, 0))], [lipid(9, "PE 34:1", "[M-H]-", "PE", (1, 2, 3, 0))],
                            sample_file_types=("Sample",))
        self.assertEqual(["SPF_1", "GF_2"], [item["name"] for item in report["samples"]["taken"]])
        self.assertEqual(3, len(lines(self.out / "lipid_metabolome4stat.csv")))

    def test_a_sample_without_a_class_label_is_taken_as_metaboservice_took_it_and_said(self) -> None:
        # set_metadata stops only at a Class of exactly "NA": an empty or an "Na" class is a sample like any other.
        samples = {
            label: [(f"A_{tag}", "SPF", "Sample"), (f"B_{tag}", "", "Sample"), (f"C_{tag}", "Na", "QC"),
                    (f"Blank_{tag}", "Blank", "Blank")]
            for label, tag in (("Positive", "Pos"), ("Negative", "Neg"))
        }
        write_export(self.root / "pos.txt", [lipid(1, "PC 34:1", "[M+H]+", "PC", (1, 2, 3, 0))], samples["Positive"])
        write_export(self.root / "neg.txt", [lipid(9, "PE 34:1", "[M-H]-", "PE", (4, 5, 6, 0))], samples["Negative"])
        report = merge_pos_neg(self.root / "pos.txt", self.root / "neg.txt", self.rules, self.out / "lipid_metabolome.csv")
        self.assertEqual(["SPF_1", "_2", "Na_3"], [item["name"] for item in report["samples"]["taken"]])
        self.assertEqual(["Metabolite,Class,PC 34:1_1,PE 34:1_9", "SPF_1,SPF,1,4", "_2,,2,5", "Na_3,Na,3,6"],
                         lines(self.out / "lipid_metabolome4stat.csv"))
        self.assertEqual(["Value,Class,Metabolite", "2,,PC 34:1_1", "3,Na,PC 34:1_1", "1,SPF,PC 34:1_1"],
                         lines(self.out / "subclass_PC" / "PC 34_1_1.csv"))
        self.assertTrue(any("1 QC or Sample column(s) have no class label (B_Pos)" in item for item in report["warnings"]))


class TableNameTests(_Merge):
    def write_exports(self) -> None:
        write_export(self.root / "pos.txt", [lipid(1, "PC 34:1", "[M+H]+", "PC", (1, 2, 3, 0))], SAMPLES["Positive"])
        write_export(self.root / "neg.txt", [lipid(9, "PE 34:1", "[M-H]-", "PE", (1, 2, 3, 0))], SAMPLES["Negative"])

    def test_the_three_tables_keep_metaboservices_names_whatever_the_lipidome_is_called(self) -> None:
        # IntegratePosNegResults wrote them under fixed names, and metabolomics_service.R reads those names.
        self.write_exports()
        report = merge_pos_neg(self.root / "pos.txt", self.root / "neg.txt", self.rules, self.out / "lipid_metabolome_v2.csv")
        self.assertEqual(
            ["lipid_metabolome4classsummary.csv", "lipid_metabolome4stat.csv", "lipid_metabolome4summaryplot.csv",
             "lipid_metabolome_v2.csv"],
            sorted(path.name for path in self.out.iterdir() if path.is_file()),
        )
        self.assertEqual(str((self.out / "lipid_metabolome4stat.csv").resolve()), report["outputs"]["stat"]["path"])
        self.assertEqual(str((self.out / "lipid_metabolome_v2.csv").resolve()), report["output"])

    def test_the_lipidome_cannot_take_a_tables_name(self) -> None:
        self.write_exports()
        for name in ("lipid_metabolome4stat.csv", "LIPID_METABOLOME4SUMMARYPLOT.CSV"):
            with self.subTest(name=name), self.assertRaisesRegex(MergeRefused, "one of the tables metaboservice writes"):
                merge_pos_neg(self.root / "pos.txt", self.root / "neg.txt", self.rules, self.out / name)
        self.assertFalse(self.out.exists())


class TextOutsideAsciiTests(_Merge):
    def test_text_outside_ascii_is_written_as_utf8_and_said_and_ordered_as_metaboservice_read_it(self) -> None:
        # A recorded decision: metaboservice wrote one '?' per byte, so 対照 and 処理 would both have been
        # '??????', one class. The text is kept; the rows keep the order those '?' gave (file order here,
        # where Unicode order would put 処理 first).
        samples = {
            label: [(f"A_{tag}", "対照", "Sample"), (f"B_{tag}", "処理", "Sample")]
            for label, tag in (("Positive", "Pos"), ("Negative", "Neg"))
        }
        for name, rows, label in (("pos.txt", [lipid(1, "PC 34:1", "[M+H]+", "PC", (1, 2))], "Positive"),
                                  ("neg.txt", [lipid(9, "PE 34:1", "[M-H]-", "PE", (4, 5))], "Negative")):
            (self.root / name).write_text(export_text(rows, samples[label]), encoding="utf-8", newline="")
        report = merge_pos_neg(self.root / "pos.txt", self.root / "neg.txt", self.rules, self.out / "lipid_metabolome.csv")
        self.assertEqual("Metabolite,Class,PC 34:1_1,PE 34:1_9\r\n対照_1,対照,1,4\r\n処理_2,処理,2,5\r\n",
                         (self.out / "lipid_metabolome4stat.csv").read_bytes().decode("utf-8"))
        self.assertEqual("Value,Class,Metabolite\r\n1,対照,PC 34:1_1\r\n2,処理,PC 34:1_1\r\n",
                         (self.out / "subclass_PC" / "PC 34_1_1.csv").read_bytes().decode("utf-8"))
        self.assertEqual("utf-8", report["format"]["encoding"])
        self.assertEqual("utf-8", report["outputs"]["stat"]["encoding"])
        self.assertTrue(any("written as UTF-8" in item and "one '?' per byte" in item for item in report["warnings"]))


class CategoryTableTests(unittest.TestCase):
    def test_the_table_is_metaboservices_superclass_switch(self) -> None:
        cases, default, source = load_lipid_category_table()
        self.assertEqual("Unassigned lipid", default)
        self.assertIn("MsdialWorkbench b01effaaa", source)
        self.assertEqual(248, len(cases))
        self.assertEqual("Glycerophospholipids", lipid_category("EtherPE"))
        self.assertEqual("Glycerophospholipids", lipid_category("BMP"))
        self.assertEqual("SterolLipids", lipid_category("BileAcid"))
        self.assertEqual("Glycerolipids", lipid_category("SMGDG"))
        self.assertEqual("Unassigned lipid", lipid_category("LipidA"))
        self.assertTrue(CATEGORY_TABLE_PATH.read_text(encoding="ascii").startswith("# Generated by"))


SYNTHETIC_CONVERTER = """
public static class LipidomicsConverter {
    //public static string ConvertMsdialClassDefinitionToSuperClassVS2(string lipidclass) {
    //    switch (lipidclass) { case "PC": return "WRONG"; default: return "Old"; }
    //}
    public static string ConvertMsdialClassDefinitionToSuperClassVS2(string lipidclass)
    {
        /* FattyAcyls [FA] */
        switch (lipidclass)
        {
            case "FA": return "FattyAcyls";
            case "PC":
            case "PE": return "Glycerophospholipids";
            // case "TG": return "Glycerolipids";
            default: return "Unassigned lipid";
        }
    }
}
"""


def load_generator():
    spec = importlib.util.spec_from_file_location("generate_lipid_category_table", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CategoryGeneratorTests(unittest.TestCase):
    def test_only_the_live_switch_is_read(self) -> None:
        cases, default = load_generator().parse_superclass_switch(SYNTHETIC_CONVERTER)
        self.assertEqual([("FA", "FattyAcyls"), ("PC", "Glycerophospholipids"), ("PE", "Glycerophospholipids")], cases)
        self.assertEqual("Unassigned lipid", default)

    def test_a_switch_without_a_default_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            load_generator().parse_superclass_switch(SYNTHETIC_CONVERTER.replace('default: return "Unassigned lipid";', ""))


@unittest.skipUnless(
    os.environ.get("MSDIAL_WORKBENCH_SOURCE"),
    "set MSDIAL_WORKBENCH_SOURCE to an MsdialWorkbench checkout to compare the resource with it",
)
class CategoryResourceAgainstSourceTests(unittest.TestCase):
    def test_the_resource_is_what_the_source_generates(self) -> None:
        generator = load_generator()
        source = (Path(os.environ["MSDIAL_WORKBENCH_SOURCE"]) / generator.CONVERTER_SOURCE).read_text(encoding="utf-8-sig")
        lines = generator.table_lines(source, "checkout")
        current = CATEGORY_TABLE_PATH.read_text(encoding="ascii").splitlines()
        self.assertEqual([line for line in current if not line.startswith("#")],
                         [line for line in lines if not line.startswith("#")])


if __name__ == "__main__":
    unittest.main()

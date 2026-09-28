from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path

from msdial_app.quality_assurance import build_lcms_qa_report, find_qa_files


class QualityAssuranceTests(unittest.TestCase):
    def test_find_qa_files_can_find_latest_nested_run_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            older = root / "qa_older" / "alignment.qa.tsv"
            newer = root / "qa_newer" / "alignment.qa.tsv"
            older.parent.mkdir()
            newer.parent.mkdir()
            older.write_text("ID\n", encoding="utf-8")
            newer.write_text("ID\n", encoding="utf-8")
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))

            self.assertEqual([], find_qa_files(root))
            self.assertEqual(
                [newer.resolve(), older.resolve()],
                find_qa_files(root, recursive=True),
            )

    def test_lcms_qa_report_summarizes_metadata_pca_and_internal_standard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qa = root / "AlignResult.qa.tsv"
            headers = [
                "ID", "File", "Class", "File type", "Injection order", "Batch ID",
                "Height", "RT", "MZ", "SN", "MSMS", "Reference matched",
            ]
            samples = [
                ("blank", "Blank", "Blank", 1),
                ("qc1", "QC", "QC", 2),
                ("sample", "Case", "Sample", 3),
                ("qc2", "QC", "QC", 4),
            ]
            with qa.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, delimiter="\t")
                writer.writerow(headers)
                for spot in range(6):
                    for index, (name, class_name, file_type, order) in enumerate(samples):
                        is_standard = spot == 2
                        height = (spot + 1) * (index + 1) * 100
                        if name == "blank":
                            height /= 10
                        writer.writerow([
                            spot, name, class_name, file_type, order, 1, height,
                            5.0 + spot + index * 0.001,
                            100.0 + spot + index * 0.0001,
                            10,
                            "TRUE" if spot % 2 == 0 else "FALSE",
                            "TRUE" if spot % 2 == 0 else "FALSE",
                        ])

            report = build_lcms_qa_report(
                root,
                [{"name": "IS", "adduct": "[M-H]-", "mz": 102.0, "rt": 7.0, "mz_tolerance": 0.01, "rt_tolerance": 0.1}],
            )

            self.assertEqual(str(qa.resolve()), report["file"])
            self.assertEqual(4, report["summary"]["sample_count"])
            self.assertEqual(6, report["summary"]["alignment_spot_count"])
            self.assertEqual(2, report["summary"]["category_counts"]["QC"])
            self.assertEqual(0.5, report["summary"]["median_msms_acquisition_rate"])
            self.assertEqual(10.0, report["summary"]["median_sample_sn"])
            self.assertTrue(all(item["msms_acquisition_rate"] == 0.5 for item in report["samples"]))
            self.assertEqual("ok", report["pca"]["status"])
            self.assertEqual(4, len(report["pca"]["points"]))
            self.assertEqual("matched", report["internal_standards"][0]["status"])
            self.assertEqual("2", report["internal_standards"][0]["alignment_id"])
            self.assertEqual("[M-H]-", report["internal_standards"][0]["adduct"])
            self.assertTrue(find_qa_files(root))

    def test_qc_criteria_need_three_qc_injections_and_say_why_not(self) -> None:
        def build(qc_count: int) -> dict:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                qa = root / "AlignResult.qa.tsv"
                headers = ["ID", "File", "Class", "File type", "Injection order", "Batch ID",
                           "Height", "RT", "MZ", "SN", "MSMS", "Reference matched"]
                samples = [("blank", "Blank", "Blank")] + [(f"qc{i}", "QC", "QC") for i in range(qc_count)] \
                    + [(f"s{i}", "Case", "Sample") for i in range(3)]
                with qa.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.writer(handle, delimiter="\t")
                    writer.writerow(headers)
                    for spot in range(6):
                        for index, (name, class_name, file_type) in enumerate(samples):
                            height = (spot + 1) * 100 + index * 7
                            writer.writerow([spot, name, class_name, file_type, index + 1, 1, height / (10 if name == "blank" else 1),
                                             5.0 + spot, 100.0 + spot, 10, "TRUE", "FALSE"])
                return build_lcms_qa_report(root)["summary"]

        two, three = build(2), build(3)
        for metric in ("median_qc_rsd_percent", "qc_features_rsd_le_30_percent", "median_qc_detection_rate",
                       "qc_pca_relative_dispersion"):
            self.assertIsNone(two[metric], metric)
            self.assertEqual("the run had 2 QC injection(s), and at least three are needed",
                             two["not_assessed_reasons"][metric])
            self.assertIsNotNone(three[metric], metric)
            self.assertNotIn(metric, three["not_assessed_reasons"])
        # The blank is the first injection, so nothing precedes it to carry over.
        self.assertEqual("no Blank file followed an injection with detected features in its batch",
                         two["not_assessed_reasons"]["median_blank_carryover_ratio"])
        self.assertNotIn("sample_blank_ratio_ge_3", two["not_assessed_reasons"])

    def test_carryover_after_an_injection_that_detected_nothing_is_not_said_to_have_no_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qa = root / "AlignResult.qa.tsv"
            headers = ["ID", "File", "Class", "File type", "Injection order", "Batch ID",
                       "Height", "RT", "MZ", "SN", "MSMS", "Reference matched"]
            samples = [("s1", "Case", "Sample"), ("empty", "Case", "Sample"), ("blank", "Blank", "Blank"),
                       ("s2", "Case", "Sample")]
            with qa.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, delimiter="\t")
                writer.writerow(headers)
                for spot in range(4):
                    for index, (name, class_name, file_type) in enumerate(samples):
                        height = 0 if name == "empty" else 100 + spot
                        writer.writerow([spot, name, class_name, file_type, index + 1, 1, height, 5.0, 100.0, 10,
                                         "TRUE", "FALSE"])
            summary = build_lcms_qa_report(root)["summary"]

        self.assertIsNone(summary["median_blank_carryover_ratio"])
        self.assertEqual("no Blank file followed an injection with detected features in its batch",
                         summary["not_assessed_reasons"]["median_blank_carryover_ratio"])

    def test_every_reason_is_one_of_the_published_phrases(self) -> None:
        import itertools, re
        from msdial_app.quality_assurance import NOT_ASSESSED_REASON_PHRASES, not_assessed_reasons

        patterns = [re.compile(re.escape(phrase).replace(re.escape("{n}"), r"\d+") + "$")
                    for phrase in NOT_ASSESSED_REASON_PHRASES]
        seen = set()
        for injections, qc, blank, features, flags in itertools.product(
                (0, 2, 5), (0, 1, 3), (0, 2), (0, 9), itertools.product((None, 1.0), repeat=3)):
            summary = {"sample_count": injections, "alignment_spot_count": features,
                       "category_counts": {"QC": qc, "Blank": blank},
                       "median_qc_rsd_percent": flags[0], "qc_pca_relative_dispersion": flags[1],
                       "run_order_intensity_correlation": flags[2]}
            for reason in not_assessed_reasons(summary).values():
                self.assertTrue(any(pattern.match(reason) for pattern in patterns), reason)
                seen.add(reason)
        self.assertGreater(len(seen), 6)

    def test_a_summary_from_before_the_minimum_is_read_with_it(self) -> None:
        from msdial_app.quality_assurance import with_qc_minimum

        old = {"sample_count": 8, "alignment_spot_count": 10, "category_counts": {"Sample": 6, "QC": 2},
               "median_qc_rsd_percent": 12.0, "median_qc_detection_rate": 0.9, "run_order_intensity_correlation": 0.1}
        read = with_qc_minimum(old)

        self.assertIsNone(read["median_qc_rsd_percent"])
        self.assertEqual("the run had 2 QC injection(s), and at least three are needed",
                         read["not_assessed_reasons"]["median_qc_rsd_percent"])
        self.assertEqual(12.0, old["median_qc_rsd_percent"])   # the summary given is not changed

    def test_every_criterion_without_a_value_has_a_reason(self) -> None:
        from msdial_app.quality_assurance import CRITERION_METRICS, not_assessed_reasons

        cases = {
            "no features": ({"sample_count": 4, "alignment_spot_count": 0, "category_counts": {"QC": 5, "Blank": 1}},
                            "the alignment has no features"),
            "too few injections": ({"sample_count": 2, "alignment_spot_count": 10, "category_counts": {"Sample": 2}},
                                   "the run had 2 injection(s), and at least three are needed"),
        }
        for name, (summary, reason) in cases.items():
            with self.subTest(name):
                reasons = not_assessed_reasons(summary)
                self.assertEqual(set(CRITERION_METRICS), set(reasons))
                self.assertIn(reason, reasons.values())

    def test_lcms_qa_report_rejects_old_alignment_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            qa = Path(temporary) / "broken.qa.tsv"
            qa.write_text("ID\tFile\tHeight\n1\tsample\t100\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing columns"):
                build_lcms_qa_report(qa)

    def test_lcms_qa_report_accepts_matrix_without_msms_column(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            qa = Path(temporary) / "legacy.qa.tsv"
            qa.write_text(
                "ID\tFile\tClass\tFile type\tInjection order\tBatch ID\tHeight\tRT\tMZ\tSN\tReference matched\n"
                "1\tsample\tCase\tSample\t1\t1\t100\t5\t100\t10\tTRUE\n",
                encoding="utf-8",
            )
            report = build_lcms_qa_report(qa)
            self.assertIsNone(report["summary"]["median_msms_acquisition_rate"])
            self.assertIsNone(report["samples"][0]["msms_acquisition_rate"])

    def test_internal_standard_can_match_by_mz_without_rt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            qa = Path(temporary) / "mz-only.qa.tsv"
            qa.write_text(
                "ID\tFile\tClass\tFile type\tInjection order\tBatch ID\tHeight\tRT\tMZ\tSN\tMSMS\tReference matched\n"
                "1\tsample\tCase\tSample\t1\t1\t100\t15\t101.000\t10\tTRUE\tTRUE\n"
                "2\tsample\tCase\tSample\t1\t1\t100\t5\t102.000\t10\tTRUE\tTRUE\n",
                encoding="utf-8",
            )
            report = build_lcms_qa_report(
                qa,
                [{"name": "m/z-only IS", "mz": 101.0, "rt": None, "mz_tolerance": 0.01}],
            )
            standard = report["internal_standards"][0]
            self.assertEqual("matched", standard["status"])
            self.assertEqual("1", standard["alignment_id"])
            self.assertIsNone(standard["rt"])
            self.assertIsNone(standard["values"][0]["rt_delta"])


if __name__ == "__main__":
    unittest.main()

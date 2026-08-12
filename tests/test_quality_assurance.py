from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from msdial_app.quality_assurance import build_lcms_qa_report, find_qa_files


class QualityAssuranceTests(unittest.TestCase):
    def test_lcms_qa_report_summarizes_metadata_pca_and_internal_standard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qa = root / "AlignResult.qa.tsv"
            headers = [
                "ID", "File", "Class", "File type", "Injection order", "Batch ID",
                "Height", "RT", "MZ", "SN", "Reference matched",
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
                        ])

            report = build_lcms_qa_report(
                root,
                [{"name": "IS", "mz": 102.0, "rt": 7.0, "mz_tolerance": 0.01, "rt_tolerance": 0.1}],
            )

            self.assertEqual(str(qa.resolve()), report["file"])
            self.assertEqual(4, report["summary"]["sample_count"])
            self.assertEqual(6, report["summary"]["alignment_spot_count"])
            self.assertEqual(2, report["summary"]["category_counts"]["QC"])
            self.assertEqual("ok", report["pca"]["status"])
            self.assertEqual(4, len(report["pca"]["points"]))
            self.assertEqual("matched", report["internal_standards"][0]["status"])
            self.assertEqual("2", report["internal_standards"][0]["alignment_id"])
            self.assertTrue(find_qa_files(root))

    def test_lcms_qa_report_rejects_old_alignment_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            qa = Path(temporary) / "broken.qa.tsv"
            qa.write_text("ID\tFile\tHeight\n1\tsample\t100\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing columns"):
                build_lcms_qa_report(qa)


if __name__ == "__main__":
    unittest.main()

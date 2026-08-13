from __future__ import annotations

import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from msdial_app.materials_methods import assess_qa, generate_publication_report
from msdial_app.workflow import console_version


class MaterialsMethodsTests(unittest.TestCase):
    def test_generates_methods_results_supplement_and_audit_bundle(self) -> None:
        workflow = {
            "project_type": "lcms",
            "ion_mode": "Negative",
            "target_omics": "Lipidomics",
            "smoothing_method": "LinearWeightedMovingAverage",
            "minimum_peak_height": 300,
            "mass_slice_width": 0.1,
            "minimum_peak_width": 5,
            "ms1_tolerance": 0.01,
            "ms2_tolerance": 0.025,
            "alignment_rt_tolerance": 0.1,
            "alignment_ms1_tolerance": 0.015,
            "files": [
                {
                    "file_path": "D:/data/sample.raw",
                    "file_name": "sample",
                    "file_type": "Sample",
                    "class_id": "Case",
                    "acquisition_type": "SWATH",
                    "batch_order": 1,
                    "analytical_order": 3,
                    "factor": 1,
                }
            ],
            "msp_annotators": [
                {
                    "annotator_id": "msp_annotator_1",
                    "msp_file_path": "D:/libraries/public.msp",
                    "weighted_dot_product_cutoff": 0.6,
                }
            ],
            "selected_adducts": ["[M-H]-"],
            "selected_lipids": [
                {"lipid_class": "FA", "adduct": "[M-H]-", "ion_mode": "Negative"}
            ],
            "library_provenance": [
                {
                    "label": "Public negative library",
                    "local_path": "D:/libraries/public.msp",
                    "doi": "10.5281/zenodo.21904103",
                    "record_url": "https://zenodo.org/records/21904103",
                    "version": "VS20",
                    "md5": "0123456789abcdef0123456789abcdef",
                    "license": "CC BY 4.0",
                }
            ],
        }
        qa = {
            "summary": {
                "sample_count": 8,
                "alignment_spot_count": 1000,
                "category_counts": {"Sample": 4, "QC": 3, "Blank": 1},
                "median_qc_rsd_percent": 20,
                "qc_features_rsd_le_30_percent": 0.8,
                "median_qc_detection_rate": 0.9,
                "sample_blank_ratio_ge_3": 0.75,
                "qc_pca_relative_dispersion": 0.3,
                "median_blank_carryover_ratio": 0.05,
                "run_order_intensity_correlation": 0.1,
            },
            "internal_standards": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_publication_report(
                workflow,
                qa,
                temporary,
                app_version="0.2.0",
                console_version="5.5.241113",
            )

            self.assertIn("MS-DIAL Console version 5.5.241113", result["methods_text"])
            self.assertIn("7 of 7 prespecified", result["methods_text"])
            self.assertIn("7 of 7 evaluable", result["qa_results_text"])
            self.assertEqual([], result["warnings"])
            with Path(result["supplementary_table"]).open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertTrue({"Software", "Data", "Guided setup", "Annotation", "Library provenance", "Quality assurance"}.issubset({row["Section"] for row in rows}))
            self.assertTrue(any(row["Value"] == "10.5281/zenodo.21904103" for row in rows))
            audit = json.loads(Path(result["audit_file"]).read_text(encoding="utf-8"))
            self.assertEqual("pass", audit["qa_assessment"]["status"])
            with zipfile.ZipFile(result["bundle"]) as archive:
                self.assertEqual(
                    {
                        "MS_DIAL_Materials_and_Methods.txt",
                        "MS_DIAL_QA_Results.txt",
                        "Supplementary_Table_MS_DIAL.tsv",
                        "MS_DIAL_publication_report.json",
                    },
                    set(archive.namelist()),
                )

    def test_failed_and_missing_qa_checks_are_not_described_as_passed(self) -> None:
        assessment = assess_qa(
            {
                "summary": {
                    "median_qc_rsd_percent": 45,
                    "run_order_intensity_correlation": -0.2,
                }
            }
        )

        self.assertEqual("review", assessment["status"])
        self.assertEqual(1, assessment["passed"])
        self.assertEqual(2, assessment["evaluated"])
        self.assertEqual(5, sum(item["status"] == "not_assessed" for item in assessment["checks"]))

    def test_user_library_without_identifier_is_flagged(self) -> None:
        workflow = {
            "project_type": "lcms",
            "files": [],
            "msp_annotators": [{"msp_file_path": "D:/private/inhouse.msp"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_publication_report(
                workflow,
                None,
                temporary,
                app_version="0.2.0",
                console_version="",
            )
        self.assertIn("No persistent identifier", result["warnings"][0])
        self.assertIn("[VERSION NOT RECORDED]", result["methods_text"])

    def test_gcms_methods_use_retention_index_settings_without_lcms_tolerances(self) -> None:
        workflow = {
            "project_type": "gcms",
            "ion_mode": "Positive",
            "target_omics": "Metabolomics",
            "files": [],
            "smoothing_method": "LinearWeightedMovingAverage",
            "minimum_peak_height": 1000,
            "minimum_peak_width": 5,
            "gcms_accuracy_type": "IsNominal",
            "gcms_retention_type": "RI",
            "gcms_alignment_index_type": "RI",
            "gcms_ri_compound_type": "Alkanes",
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_publication_report(
                workflow, None, temporary, app_version="0.2.0", console_version="5.5.241113"
            )
        self.assertIn("Electron-ionization", result["methods_text"])
        self.assertIn("configured RI compound type was Alkanes", result["methods_text"])
        self.assertNotIn("MS1 and MS2 centroid tolerances", result["methods_text"])

    def test_console_version_prefers_version_option(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "fake-console.exe"
            executable.write_bytes(b"")
            with patch(
                "msdial_app.workflow.subprocess.run",
                return_value=CompletedProcess([], 0, "5.5.260323\n", ""),
            ) as run:
                self.assertEqual("5.5.260323", console_version(str(executable)))
            self.assertEqual("--version", run.call_args.args[0][-1])


if __name__ == "__main__":
    unittest.main()

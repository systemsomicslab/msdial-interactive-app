from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch
from xml.etree import ElementTree as ET

from msdial_app.materials_methods import (
    _automatic_rt_correction_evidence,
    assess_qa,
    generate_publication_report,
)
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
            workbook_path = Path(result["supplementary_workbook"])
            self.assertTrue(workbook_path.is_file())
            with zipfile.ZipFile(workbook_path) as workbook:
                workbook_xml = ET.fromstring(workbook.read("xl/workbook.xml"))
                namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                self.assertEqual(
                    ["Data", "Guided setup", "Annotation", "Quality assurance"],
                    [item.attrib["name"] for item in workbook_xml.findall("x:sheets/x:sheet", namespace)],
                )
                data_text = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
                guided_text = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
                annotation_text = workbook.read("xl/worksheets/sheet3.xml").decode("utf-8")
            self.assertIn("file_path", data_text)
            self.assertIn("analytical_order", data_text)
            self.assertIn("D:/data/sample.raw", data_text)
            self.assertIn("Core workflow", guided_text)
            self.assertIn("Project type", guided_text)
            self.assertIn("MSP annotator: msp_annotator_1", annotation_text)
            self.assertIn("Selected lipid queries", annotation_text)
            self.assertIn("10.5281/zenodo.21904103", annotation_text)
            audit = json.loads(Path(result["audit_file"]).read_text(encoding="utf-8"))
            self.assertEqual("pass", audit["qa_assessment"]["status"])
            with zipfile.ZipFile(result["bundle"]) as archive:
                self.assertEqual(
                    {
                        "MS_DIAL_Materials_and_Methods.txt",
                        "MS_DIAL_QA_Results.txt",
                        "Supplementary_Table_MS_DIAL.xlsx",
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

    def test_official_library_doi_matches_an_identical_filename_at_another_path(self) -> None:
        workflow = {
            "project_type": "lcms",
            "files": [],
            "lbm_path": "D:/demo/Msp2025_dev.lbm2",
            "library_provenance": [
                {
                    "label": "Official lipid library",
                    "filename": "Msp2025_dev.lbm2",
                    "local_path": "C:/cache/21904324/Msp2025_dev.lbm2",
                    "doi": "10.5281/zenodo.21904324",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_publication_report(
                workflow, None, temporary, app_version="0.3.1", console_version="5.5"
            )

        self.assertEqual([], result["warnings"])
        self.assertIn("10.5281/zenodo.21904324", result["methods_text"])

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

    def test_automatic_rt_methods_require_retained_console_evidence(self) -> None:
        workflow = {
            "project_type": "lcms",
            "ion_mode": "Negative",
            "target_omics": "Metabolomics",
            "files": [],
            "execute_automatic_rt_correction": True,
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_publication_report(
                workflow,
                None,
                temporary,
                app_version="0.5.0",
                console_version="5.5",
            )

        self.assertNotIn("learned distributed anchor features", result["methods_text"])
        self.assertTrue(
            any("does not prove that it was performed" in item for item in result["warnings"])
        )

    @staticmethod
    def _write_automatic_rt_audit(
        root: Path,
        summary: str = (
            "0\tQC-reference\tReference\n"
            "1\tSample-1\tDetectedAnchors\n"
        ),
        anchors: str = (
            "0\t1\tTrue\n"
            "0\t2\tTrue\n"
            "1\t1\tTrue\n"
            "1\t2\tTrue\n"
        ),
        method_digest: str | None = None,
    ) -> None:
        """Write the three Console records the way a run leaves them: method file, then its key
        record carrying the method file's hash, then the two audit TSVs from alignment."""
        method = root / "method.txt"
        method.write_text("Execute automatic RT correction for alignment: True\n", encoding="utf-8")
        digest = method_digest or hashlib.sha256(method.read_bytes()).hexdigest()
        keys = root / "method.keys.json"
        keys.write_text(
            json.dumps(
                {
                    "schema": "msdial-method-file-keys.v1",
                    "method_file": "method.txt",
                    "method_file_sha256": digest,
                    # As the Console records it: the key as the method file spells it.
                    "applied": ["Execute automatic RT correction for alignment"],
                    "unrecognised": [],
                    "unusable": [],
                }
            ),
            encoding="utf-8",
        )
        summary_path = root / "automatic_alignment_rt_correction_summary.tsv"
        summary_path.write_text("File ID\tFile name\tModel source\n" + summary, encoding="utf-8")
        anchors_path = root / "automatic_alignment_rt_correction_anchors.tsv"
        anchors_path.write_text("File ID\tAnchor ID\tUsed\n" + anchors, encoding="utf-8")
        record_time = keys.stat().st_mtime
        for path in (summary_path, anchors_path):
            os.utime(path, (record_time + 1, record_time + 1))

    def _automatic_rt_report(self, root: Path) -> tuple[dict, dict]:
        workflow = {
            "project_type": "lcms",
            "ion_mode": "Negative",
            "target_omics": "Metabolomics",
            "files": [],
            "execute_automatic_rt_correction": True,
        }
        evidence = _automatic_rt_correction_evidence(root, workflow)
        result = generate_publication_report(
            workflow, None, root, app_version="0.5.0", console_version="5.5"
        )
        return result, evidence

    def test_automatic_rt_evidence_must_come_from_this_method_file(self) -> None:
        # A key record left by an earlier preparation of the same directory names a method file
        # that is no longer the one there.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_automatic_rt_audit(root, method_digest="0" * 64)

            result, evidence = self._automatic_rt_report(root)

        self.assertFalse(evidence["performed"])
        self.assertEqual("method_key_record_not_from_this_method_file", evidence["reason"])
        self.assertNotIn("learned distributed anchor features", result["methods_text"])

    def test_automatic_rt_audit_older_than_the_key_record_is_not_this_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_automatic_rt_audit(root)
            record_time = (root / "method.keys.json").stat().st_mtime
            stale = root / "automatic_alignment_rt_correction_anchors.tsv"
            os.utime(stale, (record_time - 60, record_time - 60))

            result, evidence = self._automatic_rt_report(root)

        self.assertFalse(evidence["performed"])
        self.assertEqual("audit_older_than_method_key_record", evidence["reason"])

    def test_automatic_rt_reference_anchors_alone_are_not_a_correction(self) -> None:
        # The reference file's anchors are always marked used. A run in which every other file
        # kept its original RT, or where a Blank copied the reference's identity map, corrected
        # nothing.
        cases = {
            "every other file uncorrected": (
                "0\tQC-reference\tReference\n1\tSample-1\tUncorrected\n",
                "0\t1\tTrue\n0\t2\tTrue\n1\t1\tFalse\n",
            ),
            "blank copies the reference": (
                "0\tQC-reference\tReference\n1\tBlank-1\tNearestBlank\n",
                "0\t1\tTrue\n0\t2\tTrue\n",
            ),
        }
        for name, (summary, anchors) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self._write_automatic_rt_audit(root, summary=summary, anchors=anchors)

                result, evidence = self._automatic_rt_report(root)

                self.assertFalse(evidence["performed"])
                self.assertEqual("audit_does_not_show_correction", evidence["reason"])
                self.assertTrue(
                    any("does not prove that it was performed" in item for item in result["warnings"])
                )

    def test_automatic_rt_counts_what_the_correction_reached(self) -> None:
        # A Blank interpolated between two corrected samples is part of a real correction, and
        # the Methods text says how much of the run was corrected rather than only "applied".
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_automatic_rt_audit(
                root,
                summary=(
                    "0\tQC-reference\tReference\n"
                    "1\tSample-1\tDetectedAnchors\n"
                    "2\tBlank-1\tInterpolatedBlank\n"
                    "3\tSample-2\tDetectedAnchors\n"
                    "4\tSample-3\tUncorrected\n"
                ),
                anchors=(
                    "0\t1\tTrue\n0\t2\tTrue\n0\t3\tTrue\n"
                    "1\t1\tTrue\n1\t2\tTrue\n"
                    "3\t2\tTrue\n3\t3\tTrue\n"
                    "4\t1\tFalse\n"
                ),
            )

            result, evidence = self._automatic_rt_report(root)

        self.assertTrue(evidence["performed"])
        self.assertEqual(2, evidence["corrected_file_count"])
        self.assertEqual(3, evidence["selected_anchor_count"])
        # The reference is counted on its own, so the three counts partition the other files.
        self.assertIn(
            "Of the other 4 audited file(s), 2 were corrected from their own anchors "
            "(3 distinct anchor(s) used), 1 Blank file(s) took an interpolated or "
            "nearest-sample model, and 1 kept their original retention times",
            result["methods_text"],
        )
        # One file left at its measured RTs means aligned RTs are not all on the reference axis.
        self.assertNotIn("are therefore on the retention-time axis", result["methods_text"])
        self.assertIn("only where none of those files contributes", result["methods_text"])
        self.assertTrue(any("left 1 file(s) uncorrected" in item for item in result["warnings"]))

    def test_automatic_rt_a_value_the_console_discarded_is_not_proof(self) -> None:
        # The Console records "<key>: <value>" under unusable and runs with its default.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_automatic_rt_audit(root)
            keys = root / "method.keys.json"
            record = json.loads(keys.read_text(encoding="utf-8"))
            record["unusable"] = ["Automatic RT correction maximum anchors: 3000000000"]
            keys.write_text(json.dumps(record), encoding="utf-8")
            for path in root.glob("automatic_alignment_rt_correction_*.tsv"):
                later = keys.stat().st_mtime + 1
                os.utime(path, (later, later))

            result, evidence = self._automatic_rt_report(root)

        self.assertFalse(evidence["performed"])
        self.assertEqual("method_key_value_discarded_by_console", evidence["reason"])
        self.assertEqual(["automatic rt correction maximum anchors"], evidence["discarded_keys"])

    def test_automatic_rt_settings_stay_out_of_table_s1_when_off(self) -> None:
        workflow = {
            "project_type": "lcms",
            "ion_mode": "Negative",
            "target_omics": "Metabolomics",
            "files": [],
            "execute_automatic_rt_correction": False,
            "automatic_rt_correction_minimum_anchors": 3,
            "automatic_rt_correction_outlier_mad_threshold": 3.5,
            "sample_table_proposal": {"analytical_order": {"derived_from": "listing"}},
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_publication_report(
                workflow, None, temporary, app_version="0.5.0", console_version="5.5"
            )
            with zipfile.ZipFile(result["supplementary_workbook"]) as workbook:
                guided = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
            with Path(result["supplementary_table"]).open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                parameters = {row["Parameter"] for row in csv.DictReader(handle, delimiter="\t")}

        self.assertNotIn("minimum anchors", guided.casefold())
        self.assertNotIn("outlier mad", guided.casefold())
        self.assertNotIn("sample table proposal", guided.casefold())
        # The full record keeps both.
        self.assertIn("sample_table_proposal", parameters)
        self.assertIn("automatic_rt_correction_minimum_anchors", parameters)

    def test_automatic_rt_methods_and_table_use_console_audit_files(self) -> None:
        workflow = {
            "project_type": "lcms",
            "ion_mode": "Negative",
            "target_omics": "Metabolomics",
            "files": [],
            "execute_automatic_rt_correction": True,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_automatic_rt_audit(root)

            result = generate_publication_report(
                workflow,
                None,
                root,
                app_version="0.5.0",
                console_version="5.5",
            )

            with Path(result["supplementary_table"]).open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            with zipfile.ZipFile(result["supplementary_workbook"]) as workbook:
                guided = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")

        self.assertIn("reference file QC-reference", result["methods_text"])
        self.assertIn(
            "reference file QC-reference, which defines the axis and keeps its measured "
            "retention times. Of the other 1 audited file(s), 1 were corrected from their own "
            "anchors (2 distinct anchor(s) used)",
            result["methods_text"],
        )
        # Every other file corrected: the unqualified axis statement holds, with no warning.
        self.assertIn(
            "are therefore on the retention-time axis of reference file QC-reference",
            result["methods_text"],
        )
        self.assertFalse(any("uncorrected" in item for item in result["warnings"]))
        self.assertFalse(
            any("does not prove that it was performed" in item for item in result["warnings"])
        )
        self.assertTrue(
            any(
                row["Section"] == "Automatic alignment RT correction evidence"
                and row["Parameter"] == "reference_file_name"
                and row["Value"] == "QC-reference"
                for row in rows
            )
        )
        self.assertIn("Automatic alignment RT correction evidence", guided)
        self.assertIn("QC-reference", guided)

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

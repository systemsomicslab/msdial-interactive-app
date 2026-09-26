import tempfile
import unittest
import zipfile
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

from msdial_app.agent_bridge import create_datamining_handoff, summarize_jobs
from msdial_app import __version__
from msdial_app.mztab_preview import preview_mztab_file, preview_mztab_outputs
from msdial_app.mztab_validation import validate_mztab_file, validate_mztab_files, validate_mztab_outputs
from msdial_app.workflow import (
    _write_method,
    build_console_command,
    console_capabilities,
    detect_raw_format,
    expand_paths,
    expand_paths_report,
    load_parameter_template,
    parse_mdpeak,
    parse_mdscan,
    parse_rt_correction_result,
    prepare_run,
    prepare_rt_correction_run,
    prepare_tuning_run,
    read_analysis_csv,
    read_adducts,
    read_lipid_queries,
    read_rt_correction_anchors,
    format_based_peak_parameters,
    save_rt_correction_anchors,
    save_rt_correction_selections,
    validate_workflow,
)


class WorkflowTests(unittest.TestCase):
    def test_project_version_matches_package_version(self) -> None:
        project = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        version = re.search(r'^version = "([^"]+)"$', project, flags=re.MULTILINE)
        self.assertIsNotNone(version)
        self.assertEqual(version.group(1), __version__)

    def test_mzxml_is_rejected_with_mzml_conversion_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.mzXML"
            path.write_text("", encoding="ascii")

            report = expand_paths_report([str(path)])

            self.assertEqual([], report["files"])
            self.assertEqual(1, len(report["rejected"]))
            self.assertIn("no mzXML/mzData reader", report["rejected"][0])
            self.assertIn("convert to mzML", report["rejected"][0])

    def test_shimadzu_lcd_and_qgd_are_supported_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lcd = root / "sample.lcd"
            qgd = root / "sample.qgd"
            lcd.write_text("", encoding="ascii")
            qgd.write_text("", encoding="ascii")
            report = expand_paths_report([str(root)])
            self.assertEqual(2, len(report["files"]))
            self.assertEqual("Shimadzu", detect_raw_format(lcd)["vendor"])
            self.assertEqual("Shimadzu LCD", detect_raw_format(lcd)["format"])
            self.assertEqual("Shimadzu QGD", detect_raw_format(qgd)["format"])

    @patch("msdial_app.workflow.subprocess.run")
    def test_console_capability_probes_command_help_and_qa_exporter(self, run: Mock) -> None:
        run.return_value = Mock(
            returncode=0,
            stdout="Usage: MSDIALCUI rtcorrection --library <library> --selection <selection>",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temporary:
            console = Path(temporary) / "MSDIALCUI.exe"
            console.write_bytes("LC-MS quality-assurance matrix:".encode("utf-16-le"))

            result = console_capabilities(str(console))

        self.assertEqual("rtcorrection help + QA exporter marker", result["capability_probe"])
        self.assertEqual(
            ["lcms_alignment_qa_matrix", "rt_correction_review"],
            result["capabilities"],
        )
        self.assertEqual([str(console), "rtcorrection", "--help"], run.call_args.args[0])

    @patch("msdial_app.workflow.subprocess.run")
    def test_console_capability_falls_back_to_qa_assembly_marker(self, run: Mock) -> None:
        run.return_value = Mock(returncode=1, stdout="", stderr="Unknown command")
        with tempfile.TemporaryDirectory() as temporary:
            console = Path(temporary) / "MSDIALCUI.exe"
            console.write_bytes("LC-MS quality-assurance matrix:".encode("utf-16-le"))

            result = console_capabilities(str(console))

            self.assertEqual("QA exporter marker", result["capability_probe"])
            self.assertIn("lcms_alignment_qa_matrix", result["capabilities"])

    @patch("msdial_app.workflow.subprocess.run")
    def test_console_capability_detects_automatic_alignment_rt_correction(self, run: Mock) -> None:
        # The two strings MSDIALCUI.exe of the feature build carries, as UTF-16 .NET literals:
        # the lowercase key ConfigParser reads and the audit line LcmsProcess writes.
        run.return_value = Mock(returncode=1, stdout="", stderr="Unknown command")
        for marker in (
            "execute automatic rt correction for alignment",
            "Automatic alignment RT correction audit:",
        ):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as temporary:
                console = Path(temporary) / "MSDIALCUI.exe"
                console.write_bytes(marker.encode("utf-16-le"))

                result = console_capabilities(str(console))

                self.assertIn("automatic_alignment_rt_correction", result["capabilities"])
                self.assertEqual("automatic RT correction marker", result["capability_probe"])

    @patch("msdial_app.workflow.subprocess.run")
    def test_console_capability_ignores_the_core_library_field_label(self, run: Mock) -> None:
        # The title-case label is in MsdialCore.dll, not MSDIALCUI.exe. The probe once looked
        # for it, so the feature build was reported as lacking the feature, and any file that
        # happened to hold the label would have been reported as having it.
        run.return_value = Mock(returncode=1, stdout="", stderr="Unknown command")
        with tempfile.TemporaryDirectory() as temporary:
            console = Path(temporary) / "MSDIALCUI.exe"
            label = "Execute automatic RT correction for alignment"
            console.write_bytes(label.encode("utf-16-le") + label.encode("utf-8"))

            result = console_capabilities(str(console))

        self.assertNotIn("automatic_alignment_rt_correction", result["capabilities"])

    def test_parameter_template_loads_guided_annotation_and_lipid_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queries = root / "LbmQueries.txt"
            queries.write_text(
                "Class\tAdduct\tIon mode\tDefault\n"
                "PC\t[M+H]+\tPositive\tFALSE\n"
                "PE\t[M-H]-\tNegative\tFALSE\n",
                encoding="ascii",
            )
            msp = root / "library.msp"
            lbm = root / "library.lbm2"
            template = root / "method.txt"
            template.write_text(
                "Ion mode: Positive\n"
                "Target omics: Lipidomics\n"
                "Machine category: LCMS\n"
                "Msp file path: library.msp\n"
                "Lbm file path: library.lbm2\n"
                "Minimum peak height: 1234\n"
                "Smoothing method: SavitzkyGolayFilter\n"
                "Execute automatic RT correction for alignment: True\n"
                "Automatic RT correction minimum anchors: 4\n"
                "Automatic RT correction maximum anchors: 8\n"
                "Automatic RT correction interpolate blanks by analytical order: False\n"
                "Weighted dot product cutoff for MSP-based annotation: 0.72\n"
                "adduct list: [M+H]+,[M+Na]+\n"
                "Searched lipid class: PC [M+H]+\n",
                encoding="ascii",
            )

            result = load_parameter_template(template, queries)

            self.assertEqual("lcms", result["workflow"]["project_type"])
            self.assertEqual(1234, result["workflow"]["minimum_peak_height"])
            self.assertEqual("SavitzkyGolayFilter", result["workflow"]["smoothing_method"])
            self.assertTrue(result["workflow"]["execute_automatic_rt_correction"])
            self.assertEqual(4, result["workflow"]["automatic_rt_correction_minimum_anchors"])
            self.assertEqual(8, result["workflow"]["automatic_rt_correction_maximum_anchors"])
            self.assertFalse(
                result["workflow"][
                    "automatic_rt_correction_interpolate_blanks_by_analytical_order"
                ]
            )
            self.assertEqual(str(msp.resolve()), result["msp_annotators"][0]["msp_file_path"])
            self.assertEqual(str(lbm.resolve()), result["lbm_annotator"]["lbm_file_path"])
            self.assertEqual(0.72, result["msp_annotators"][0]["weighted_dot_product_cutoff"])
            self.assertEqual(["[M+H]+", "[M+Na]+"], result["selected_adducts"])
            selected = [
                f"{item['lipid_class']} {item['adduct']}"
                for item in result["lipid_queries"]
                if item["selected"]
            ]
            self.assertEqual(["PC [M+H]+"], selected)

    def test_lipidomics_template_without_query_list_selects_all_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queries = root / "LbmQueries.txt"
            queries.write_text(
                "Class\tAdduct\tIon mode\tDefault\n"
                "PC\t[M+H]+\tPositive\tFALSE\n"
                "PE\t[M-H]-\tNegative\tFALSE\n",
                encoding="ascii",
            )
            template = root / "method.txt"
            template.write_text("Target omics: Lipidomics\n", encoding="ascii")

            result = load_parameter_template(template, queries)

            self.assertTrue(all(item["selected"] for item in result["lipid_queries"]))

    def test_parameter_template_loads_multiple_annotator_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.msp"
            second = root / "second.msp"
            text_db = root / "standards.txt"
            for path in (first, second, text_db):
                path.write_text("", encoding="ascii")
            msp_settings = root / "msp_annotator_settings.tsv"
            msp_settings.write_text(
                "annotator_id\tmsp_file_path\tpriority\trt_tolerance\tminimum_spectrum_match\tuse_retention_information_for_scoring\tuse_retention_information_for_filtering\n"
                f"confirmed\t{first.name}\t2\t0.05\t5\tTrue\tTrue\n"
                f"predicted\t{second.name}\t1\t1.0\t3\tTrue\tFalse\n",
                encoding="ascii",
            )
            text_settings = root / "text_annotator_settings.tsv"
            text_settings.write_text(
                "annotator_id\ttext_db_file_path\tpriority\trt_tolerance\tms1_tolerance\ttotal_score_cutoff\tuse_retention_information_for_scoring\tuse_retention_information_for_filtering\n"
                f"istd\t{text_db.name}\t3\t0.1\t0.005\t0.7\tTrue\tTrue\n",
                encoding="ascii",
            )
            template = root / "method.txt"
            template.write_text(
                f"MSP annotator settings file path: {msp_settings.name}\n"
                f"Text annotator settings file path: {text_settings.name}\n",
                encoding="ascii",
            )

            result = load_parameter_template(template)

            self.assertEqual(["confirmed", "predicted"], [row["annotator_id"] for row in result["msp_annotators"]])
            self.assertEqual(str(first.resolve()), result["msp_annotators"][0]["msp_file_path"])
            self.assertEqual(5, result["msp_annotators"][0]["minimum_spectrum_match"])
            self.assertTrue(result["msp_annotators"][0]["use_rt_filtering"])
            self.assertEqual("istd", result["text_annotators"][0]["annotator_id"])
            self.assertEqual(str(text_db.resolve()), result["text_annotators"][0]["text_db_file_path"])

    def test_analysis_metadata_csv_import_preserves_per_file_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "sample1.mzML"
            second = root / "sample2.mzML"
            first.write_bytes(b"")
            second.write_bytes(b"")
            source = root / "analysis.csv"
            source.write_text(
                "file_path,file_name,file_type,class_id,acquisition_type,batch_order,analytical_order,factor,Included\n"
                "sample1.mzML,First,Sample,Control,DDA,2,7,1.5,TRUE\n"
                f"{second},Second,QC,QC,SWATH,3,8,2,FALSE\n",
                encoding="utf-8",
            )

            result = read_analysis_csv(source)

            self.assertEqual(2, len(result["files"]))
            self.assertEqual(str(first.resolve()), result["files"][0]["file_path"])
            self.assertEqual("Control", result["files"][0]["class_id"])
            self.assertEqual(2, result["files"][0]["batch_order"])
            self.assertEqual(7, result["files"][0]["analytical_order"])
            self.assertEqual(1.5, result["files"][0]["factor"])
            self.assertEqual("SWATH", result["files"][1]["acquisition_type"])
            self.assertEqual("QC", result["files"][1]["file_type"])

    def test_rt_correction_anchor_library_edit_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "anchors.txt"
            source.write_text(
                "Name\tRT(min)\tRT tol.(min)\tm/z\tm/z tol.\tMinimum height\tT/F\n"
                "STD1\t7.5\t3\t231.1\t0.01\t10000\tTRUE\n",
                encoding="ascii",
            )
            rows = read_rt_correction_anchors(source)
            rows[0]["rt"] = 7.6
            rows[0]["minimum_height"] = 15000
            saved = Path(
                save_rt_correction_anchors(
                    {
                        "output_root": str(root),
                        "rt_correction_anchor_source_path": str(source),
                    },
                    rows,
                )
            )
            reloaded = read_rt_correction_anchors(saved)

            self.assertRegex(saved.name, r"^anchors_\d{8}-\d{6}\.txt$")
            self.assertTrue(source.is_file())
            self.assertEqual(7.6, reloaded[0]["rt"])
            self.assertEqual(3, reloaded[0]["rt_tolerance"])
            self.assertEqual(15000, reloaded[0]["minimum_height"])
            self.assertTrue(reloaded[0]["include"])

    def test_rt_correction_preview_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.mzML"
            raw.write_text("raw", encoding="ascii")
            anchor = root / "anchors.txt"
            anchor.write_text("Name\tRT\nSTD1\t7.5\n", encoding="ascii")
            template = root / "method.txt"
            template.write_text("Ion mode: Negative\n", encoding="ascii")
            console = root / "MSDIALCUI.dll"
            console.write_text("", encoding="ascii")
            state = {
                "project_type": "lcms",
                "files": [
                    {
                        "file_path": str(raw),
                        "file_name": "sample",
                        "acquisition_type": "SWATH",
                    }
                ],
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "output"),
                "ion_mode": "Negative",
                "rt_correction_anchor_path": str(anchor),
            }
            prepared = prepare_rt_correction_run(state)
            self.assertEqual("dotnet", prepared["command"][0])
            self.assertEqual(["eic", "rtcorrection"], prepared["command"][2:4])
            self.assertIn("--library", prepared["command"])
            self.assertIn("--ionmode", prepared["command"])
            self.assertIn("--acquisitiontype", prepared["command"])
            self.assertNotIn("-library", prepared["command"])
            self.assertNotIn("-ionmode", prepared["command"])
            self.assertNotIn("-acquisitiontype", prepared["command"])
            self.assertIn("SWATH", prepared["command"])

            with patch(
                "msdial_app.workflow.console_capabilities",
                return_value={
                    "capability_probe": "rtcorrection help",
                    "capabilities": ["rt_correction_review"],
                },
            ):
                current = prepare_rt_correction_run(state)
            self.assertEqual("rtcorrection", current["command"][2])
            self.assertNotEqual("eic", current["command"][2])

            selection = Path(prepared["selection_file"])
            selection.write_text(
                "File path\tFile name\tStandard ID\tStandard name\tReference RT (min)\tDetected RT (min)\tSelected RT (min)\tUse\tPeak height\n"
                f"{raw}\tsample\t0\tSTD1\t7.5\t7.45\t7.45\tTrue\t12345\n",
                encoding="utf-8",
            )
            Path(prepared["eic_file"]).write_text(
                "FileName,FilePath,StandardId,StandardName,ReferenceRT,RTTolerance,TargetMz,MzTolerance,MinimumHeight,ScanId,RT,CorrectedRT,Intensity,SmoothedIntensity\n"
                f"sample,{raw},0,STD1,7.5,0.5,231.1,0.01,10000,1,7.45,7.50,12345,12000\n",
                encoding="utf-8",
            )
            result = parse_rt_correction_result(prepared)
            self.assertEqual(1, len(result["rows"]))
            self.assertEqual(7.5, result["series"][0]["corrected_rt"][0])
            self.assertEqual(231.1, result["series"][0]["target_mz"])
            self.assertEqual(0.5, result["series"][0]["rt_tolerance"])
            result["rows"][0]["use"] = False
            saved = Path(save_rt_correction_selections(state, result["rows"]))
            self.assertIn("\t0\tFalse\t", saved.read_text(encoding="utf-8"))

    def test_rt_correction_preview_rejects_legacy_eic_format(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = root / "selections.tsv"
            selection.write_text(
                "File path\tFile name\tStandard ID\tStandard name\tReference RT (min)\tDetected RT (min)\tSelected RT (min)\tUse\tPeak height\n",
                encoding="utf-8",
            )
            eic = root / "eics.csv"
            eic.write_text(
                "FileName,FilePath,StandardId,StandardName,ReferenceRT,TargetMz,RT,Intensity\n",
                encoding="utf-8",
            )
            preparation = {
                "selection_file": str(selection),
                "eic_file": str(eic),
                "command": ["old-MSDIALCUI.exe"],
            }
            with self.assertRaisesRegex(RuntimeError, "legacy RT correction EIC format"):
                parse_rt_correction_result(preparation)

    def test_prepare_run_writes_rt_correction_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.mzML"
            raw.write_text("raw", encoding="ascii")
            console = root / "MSDIALCUI"
            console.write_text("", encoding="ascii")
            anchor = root / "anchors.txt"
            anchor.write_text("anchors", encoding="ascii")
            selection = root / "selection.tsv"
            selection.write_text("selection", encoding="ascii")
            template = root / "method.txt"
            template.write_text("Ion mode: Positive\nExecute RT correction: False\n", encoding="ascii")
            files = expand_paths([str(raw)])
            state = {
                "project_type": "lcms",
                "files": files,
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "output"),
                "ion_mode": "Negative",
                "target_omics": "Metabolomics",
                "execute_rt_correction": True,
                "rt_correction_anchor_path": str(anchor),
                "rt_correction_selection_path": str(selection),
                "rt_correction_diff_method": "SampleMinusReference",
                "rt_correction_smooth_rt_diff": True,
                "rt_correction_intercept": 0.25,
                "rt_correction_extrapolation_begin": "FirstPoint",
                "rt_correction_extrapolation_end": "LinearExtrapolation",
                "rt_correction_peak_selection_mode": "Weighted",
                "rt_correction_peak_selection_rt_weight": 0.7,
            }
            prepared = prepare_run(state)
            method = Path(prepared["method_file"]).read_text(encoding="utf-8")
            self.assertIn("Execute RT correction: True", method)
            self.assertIn(f"Compounds library file path for RT correction: {anchor}", method)
            self.assertIn(f"RT correction peak selection file path: {selection}", method)
            self.assertIn("RT diff calc method: SampleMinusReference", method)
            self.assertIn("RT correction with smoothing for RT diff: True", method)
            self.assertIn("User setting intercept: 0.25", method)
            self.assertIn("Extrapolation method (begin): FirstPoint", method)
            self.assertIn("Extrapolation method (end): LinearExtrapolation", method)
            self.assertIn("RT correction peak selection mode: Weighted", method)
            self.assertIn("RT correction peak selection RT weight: 0.7", method)
            with zipfile.ZipFile(prepared["bundle"]) as archive:
                self.assertIn(anchor.name, archive.namelist())
                self.assertIn(selection.name, archive.namelist())

    def test_prepare_run_writes_automatic_alignment_rt_correction_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.mzML"
            raw.write_text("raw", encoding="ascii")
            console = root / "MSDIALCUI"
            console.write_bytes(
                "execute automatic rt correction for alignment".encode("utf-16-le")
            )
            template = root / "method.txt"
            template.write_text(
                "Ion mode: Positive\nExecute automatic RT correction for alignment: False\n",
                encoding="ascii",
            )
            state = {
                "project_type": "lcms",
                "files": expand_paths([str(raw)]),
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "output"),
                "ion_mode": "Negative",
                "target_omics": "Metabolomics",
                "together_with_alignment": True,
                "execute_rt_correction": False,
                "execute_automatic_rt_correction": True,
                "automatic_rt_correction_reference_file_id": -1,
                "automatic_rt_correction_rt_bin_width": 0.4,
                "automatic_rt_correction_match_rt_tolerance": 1.2,
                "automatic_rt_correction_minimum_anchors": 4,
                "automatic_rt_correction_maximum_anchors": 9,
                "automatic_rt_correction_minimum_sample_coverage": 0.7,
                "automatic_rt_correction_intensity_quantile": 0.8,
                "automatic_rt_correction_maximum_peak_width_quantile": 0.6,
                "automatic_rt_correction_minimum_signal_to_noise": 5,
                "automatic_rt_correction_minimum_gaussian_similarity": 0.3,
                "automatic_rt_correction_minimum_ideal_slope": 0.4,
                "automatic_rt_correction_outlier_mad_threshold": 4.5,
                "automatic_rt_correction_reference_centrality_weight": 0.25,
                "automatic_rt_correction_interpolate_blanks_by_analytical_order": False,
            }

            prepared = prepare_run(state)
            method = Path(prepared["method_file"]).read_text(encoding="utf-8")

            self.assertIn("Execute automatic RT correction for alignment: True", method)
            self.assertIn("Automatic RT correction reference file ID: -1", method)
            self.assertIn("Automatic RT correction RT bin width: 0.4", method)
            self.assertIn("Automatic RT correction match RT tolerance: 1.2", method)
            self.assertIn("Automatic RT correction minimum anchors: 4", method)
            self.assertIn("Automatic RT correction maximum anchors: 9", method)
            self.assertIn("Automatic RT correction minimum sample coverage: 0.7", method)
            self.assertIn("Automatic RT correction intensity quantile: 0.8", method)
            self.assertIn("Automatic RT correction maximum peak width quantile: 0.6", method)
            self.assertIn("Automatic RT correction minimum signal to noise: 5", method)
            self.assertIn("Automatic RT correction minimum Gaussian similarity: 0.3", method)
            self.assertIn("Automatic RT correction minimum ideal slope: 0.4", method)
            self.assertIn("Automatic RT correction outlier MAD threshold: 4.5", method)
            self.assertIn("Automatic RT correction reference centrality weight: 0.25", method)
            self.assertIn(
                "Automatic RT correction interpolate blanks by analytical order: False",
                method,
            )
            self.assertEqual(
                {
                    str(Path(prepared["run_directory"]) / "automatic_alignment_rt_correction_summary.tsv"),
                    str(Path(prepared["run_directory"]) / "automatic_alignment_rt_correction_anchors.tsv"),
                },
                set(prepared["expected_automatic_rt_correction_exports"]),
            )

    def test_automatic_rt_keys_are_omitted_when_disabled_or_not_lcms(self) -> None:
        for project_type in ("lcms", "gcms"):
            with self.subTest(project_type=project_type), tempfile.TemporaryDirectory() as temporary:
                template = Path(temporary) / "template.txt"
                method = Path(temporary) / "method.txt"
                template.write_text(
                    "Ion mode: Positive\n"
                    "Execute automatic RT correction for alignment: False\n"
                    "Automatic RT correction minimum anchors: 3\n",
                    encoding="ascii",
                )

                _write_method(
                    method,
                    {
                        "project_type": project_type,
                        "template_path": str(template),
                        "execute_automatic_rt_correction": False,
                        "target_omics": "Metabolomics",
                    },
                )

                written = method.read_text(encoding="utf-8")
                self.assertNotIn("automatic rt correction", written.lower())

    def test_automatic_rt_correction_refuses_a_console_without_the_feature(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.mzML"
            raw.write_text("raw", encoding="ascii")
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"not the feature marker")
            template = root / "method.txt"
            template.write_text("Ion mode: Negative\n", encoding="ascii")
            state = {
                "project_type": "lcms",
                "files": expand_paths([str(raw)]),
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "output"),
                "target_omics": "Metabolomics",
                "together_with_alignment": True,
                "execute_automatic_rt_correction": True,
                "automatic_rt_correction_minimum_anchors": 3,
                "automatic_rt_correction_maximum_anchors": 6,
                "automatic_rt_correction_rt_bin_width": 0.5,
                "automatic_rt_correction_match_rt_tolerance": 0.5,
                "automatic_rt_correction_outlier_mad_threshold": 3.5,
                "automatic_rt_correction_minimum_sample_coverage": 0.5,
                "automatic_rt_correction_intensity_quantile": 0.75,
                "automatic_rt_correction_maximum_peak_width_quantile": 0.5,
                "automatic_rt_correction_reference_centrality_weight": 0.35,
            }

            issues = validate_workflow(state)

        self.assertTrue(
            any("requires an MS-DIAL Console build" in item["message"] for item in issues)
        )

    @patch("msdial_app.workflow.console_capabilities")
    def test_repository_listing_order_warns_before_blank_interpolation(self, capabilities: Mock) -> None:
        capabilities.return_value = {
            "capability_probe": "test",
            "capabilities": ["automatic_alignment_rt_correction"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.mzML"
            raw.write_text("raw", encoding="ascii")
            blank = root / "blank_01.mzML"
            blank.write_text("raw", encoding="ascii")
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"stub")
            template = root / "method.txt"
            template.write_text("Ion mode: Negative\n", encoding="ascii")
            files = expand_paths([str(raw), str(blank)])
            for item in files:
                if item["file_name"].startswith("blank"):
                    item["file_type"] = "Blank"
            state = {
                "project_type": "lcms",
                "files": files,
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "output"),
                "target_omics": "Metabolomics",
                "together_with_alignment": True,
                "execute_automatic_rt_correction": True,
                "automatic_rt_correction_minimum_anchors": 3,
                "automatic_rt_correction_maximum_anchors": 6,
                "automatic_rt_correction_rt_bin_width": 0.5,
                "automatic_rt_correction_match_rt_tolerance": 0.5,
                "automatic_rt_correction_outlier_mad_threshold": 3.5,
                "automatic_rt_correction_minimum_sample_coverage": 0.5,
                "automatic_rt_correction_intensity_quantile": 0.75,
                "automatic_rt_correction_maximum_peak_width_quantile": 0.5,
                "automatic_rt_correction_reference_centrality_weight": 0.35,
                "automatic_rt_correction_interpolate_blanks_by_analytical_order": True,
                "repository_run_manifest": str(root / "run-manifest.json"),
            }

            # "embedded" is a sequence number read out of the file names, with every Blank and
            # QC placed after the samples: as much an inference as the listing.
            for source in ("listing", "embedded", ""):
                with self.subTest(source=source):
                    state["sample_table_proposal"] = {
                        "analytical_order": {"derived_from": source}
                    }

                    issues = validate_workflow(state)

                    self.assertTrue(
                        any(
                            item["level"] == "warning"
                            and "not read from the instrument" in item["message"]
                            for item in issues
                        ),
                        issues,
                    )

            # With no Blank there is nothing to interpolate, so nothing to warn about.
            state["files"] = [item for item in files if item["file_type"] != "Blank"]
            state["sample_table_proposal"] = {"analytical_order": {"derived_from": "listing"}}
            issues = validate_workflow(state)
            self.assertFalse(
                any("not read from the instrument" in item["message"] for item in issues),
                issues,
            )

    def test_automatic_and_user_defined_rt_correction_are_mutually_exclusive(self) -> None:
        issues = validate_workflow(
            {
                "project_type": "lcms",
                "execute_rt_correction": True,
                "execute_automatic_rt_correction": True,
                "rt_correction_anchor_path": "missing.txt",
                "automatic_rt_correction_minimum_anchors": 3,
                "automatic_rt_correction_maximum_anchors": 6,
                "automatic_rt_correction_rt_bin_width": 0.5,
                "automatic_rt_correction_match_rt_tolerance": 0.5,
                "automatic_rt_correction_outlier_mad_threshold": 3.5,
                "automatic_rt_correction_minimum_sample_coverage": 0.5,
                "automatic_rt_correction_intensity_quantile": 0.75,
                "automatic_rt_correction_maximum_peak_width_quantile": 0.5,
                "automatic_rt_correction_reference_centrality_weight": 0.35,
                "files": [],
                "console_path": "",
                "template_path": "",
                "output_root": "",
                "target_omics": "Metabolomics",
            }
        )

        self.assertTrue(
            any("cannot be enabled together" in issue["message"] for issue in issues)
        )

    def test_expand_paths_and_prepare_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.mzML"
            raw.write_text("test")
            lbm = root / "lipid.lbm2"
            lbm.write_text("NAME: lipid\n", encoding="ascii")
            template = root / "method.txt"
            template.write_text(
                "\n".join(
                    [
                        "Ion mode: Positive",
                        "Target omics: Lipidomics",
                        "Msp file path:",
                        "Lbm file path:",
                        "Text DB file path:",
                        "Export as mztabM format: False",
                        "Mass slice width: 0.1",
                        "Weighted dot product cutoff for MSP-based annotation: 0.6",
                        "Simple dot product cutoff for MSP-based annotation: 0.6",
                        "Reverse dot product cutoff for MSP-based annotation: 0.8",
                        "Matched peaks percentage cutoff for MSP-based annotation: 0.1",
                        "Minimum spectrum match for MSP-based annotation: 3",
                        "RT tolerance for LBM-based annotation: 100",
                        "MS1 tolerance for LBM-based annotation: 0.01",
                        "Use retention information for LBM-based annotation filtering: False",
                        "# Annotation parameter",
                        "Solvent type: HCOONH4",
                        "Searched lipid class: PC [M+H]+",
                    ]
                ),
                encoding="utf-8",
            )
            console = root / "MSDIALCUI"
            console.write_bytes("LC-MS quality-assurance matrix:".encode("utf-16-le"))
            files = expand_paths([str(raw)])
            files[0]["acquisition_type"] = "SWATH"
            state = {
                "files": files,
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "runs"),
                "ion_mode": "Negative",
                "target_omics": "Lipidomics",
                "ms1_data_type": "Profile",
                "ms2_data_type": "Centroid",
                "smoothing_method": "SavitzkyGolayFilter",
                "minimum_peak_height": 4321,
                "mass_slice_width": 0.05,
                "alignment_light_mode": True,
                "run_qa": True,
                "height_matrix_export": True,
                "export_folder_path": "",
                "msp_weighted_dot_product": 0.55,
                "lbm_path": str(lbm),
                "lbm_rt_tolerance": 0.25,
                "lbm_ms1_tolerance": 0.02,
                "lbm_use_rt_filtering": True,
                "solvent": "CH3COONH4",
                "selected_lipids": [
                    {
                        "lipid_class": "PC",
                        "adduct": "[M+CH3COO]-",
                        "ion_mode": "Negative",
                    }
                ],
                "library_provenance": [
                    {
                        "record_id": "21904324",
                        "record_url": "https://zenodo.org/records/21904324",
                        "filename": "MSDIAL-LipidDB-VS72-FiehnOad.lbm2",
                        "md5": "c54c577ec40d2e8fd6d4365daf4a8157",
                        "license": "CC BY 4.0",
                        "local_path": str(lbm),
                    }
                ],
                "stage_inputs": True,
            }
            result = prepare_run(state)
            self.assertEqual((root / "runs").resolve(), Path(result["run_directory"]))
            csv_text = Path(result["input_csv"]).read_text(encoding="ascii")
            method_text = Path(result["method_file"]).read_text(encoding="utf-8")
            self.assertIn(",SWATH,", csv_text)
            self.assertIn("Ion mode: Negative", method_text)
            self.assertIn("MS1 data type: Profile", method_text)
            self.assertIn("Smoothing method: SavitzkyGolayFilter", method_text)
            self.assertIn("Minimum peak height: 4321", method_text)
            self.assertIn("Mass slice width: 0.05", method_text)
            self.assertIn("Alignment light mode: True", method_text)
            self.assertIn(f"Export folder path: {(root / 'runs').resolve()}", method_text)
            self.assertIn("Height matrix export: True", method_text)
            self.assertIn(
                "Weighted dot product cutoff for MSP-based annotation: 0.55",
                method_text,
            )
            self.assertIn(f"Lbm file path: {lbm}", method_text)
            self.assertIn("RT tolerance for LBM-based annotation: 0.25", method_text)
            self.assertIn("MS1 tolerance for LBM-based annotation: 0.02", method_text)
            self.assertIn("Use retention information for LBM-based annotation filtering: True", method_text)
            self.assertIn("Searched lipid class: PC [M+CH3COO]-", method_text)
            self.assertLess(
                method_text.index("Searched lipid class:"),
                method_text.index("Solvent type:"),
            )
            bundle = Path(result["bundle"])
            self.assertTrue(bundle.is_file())
            with zipfile.ZipFile(bundle) as archive:
                self.assertEqual(
                    {
                        "REPRODUCE.txt",
                        "analysis_files.csv",
                        "command.txt",
                        "method.txt",
                        "run-manifest.json",
                        "run-msdial.ps1",
                        "run-msdial.sh",
                        "workflow-settings.json",
                    },
                    set(archive.namelist()),
                )
            self.assertIn(
                "vim method.txt",
                Path(result["reproduce_readme"]).read_text(encoding="utf-8"),
            )
            settings = json.loads(
                Path(result["settings_file"]).read_text(encoding="utf-8")
            )
            manifest = json.loads(
                Path(result["manifest"]).read_text(encoding="utf-8")
            )
            self.assertEqual(__version__, settings["msdial_interactive_version"])
            self.assertEqual("not recorded", settings["msdial_console_version"])
            self.assertEqual(__version__, manifest["msdial_interactive_version"])
            self.assertEqual("21904324", settings["library_provenance"][0]["record_id"])
            self.assertEqual("CC BY 4.0", settings["library_provenance"][0]["license"])

    def test_prepare_run_writes_multi_msp_annotator_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.mzML"
            raw.write_text("test")
            real_rt = root / "realRT.msp"
            pred_rt = root / "predRT.msp"
            text_library = root / "istd.txt"
            real_rt.write_text("NAME: A\n", encoding="ascii")
            pred_rt.write_text("NAME: B\n", encoding="ascii")
            text_library.write_text("NAME\tMZ\tRT\nISTD\t100.0\t1.0\n", encoding="ascii")
            template = root / "method.txt"
            template.write_text(
                "\n".join(
                    [
                        "Ion mode: Negative",
                        "Target omics: Metabolomics",
                        "Msp file path:",
                        "Text DB file path:",
                        "Weighted dot product cutoff for MSP-based annotation: 0.6",
                    ]
                ),
                encoding="utf-8",
            )
            console = root / "MSDIALCUI"
            console.write_text("")
            state = {
                "files": expand_paths([str(raw)]),
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "runs"),
                "project_type": "lcms",
                "ion_mode": "Negative",
                "target_omics": "Metabolomics",
                "selected_adducts": ["[M-H]-"],
                "msp_annotators": [
                    {
                        "annotator_id": "realRT",
                        "msp_file_path": str(real_rt),
                        "priority": 3,
                        "rt_tolerance": 0.05,
                        "use_rt_scoring": True,
                        "use_rt_filtering": True,
                        "weighted_dot_product_cutoff": 0.8,
                        "simple_dot_product_cutoff": 0.8,
                        "reverse_dot_product_cutoff": 0.9,
                        "matched_peaks_percentage_cutoff": 0.2,
                        "minimum_spectrum_match": 5,
                    },
                    {
                        "annotator_id": "predRT",
                        "msp_file_path": str(pred_rt),
                        "priority": 2,
                        "rt_tolerance": 1.0,
                        "use_rt_scoring": True,
                        "use_rt_filtering": False,
                        "weighted_dot_product_cutoff": 0.6,
                        "simple_dot_product_cutoff": 0.6,
                        "reverse_dot_product_cutoff": 0.8,
                        "matched_peaks_percentage_cutoff": 0.1,
                        "minimum_spectrum_match": 3,
                    },
                    {
                        "annotator_id": "",
                        "msp_file_path": str(real_rt),
                        "priority": 5,
                    },
                ],
                "text_annotators": [
                    {
                        "annotator_id": "",
                        "text_db_file_path": str(text_library),
                        "priority": 4,
                        "rt_tolerance": 0.1,
                        "ms1_tolerance": 0.005,
                        "total_score_cutoff": 0.7,
                        "use_rt_scoring": True,
                        "use_rt_filtering": True,
                    }
                ],
            }
            result = prepare_run(state)
            method_text = Path(result["method_file"]).read_text(encoding="utf-8")
            settings_path = Path(result["run_directory"]) / "msp_annotator_settings.tsv"
            text_settings_path = Path(result["run_directory"]) / "text_annotator_settings.tsv"
            self.assertIn("Msp file path: ", method_text)
            self.assertIn("Text DB file path: ", method_text)
            self.assertIn(
                f"MSP annotator settings file path: {settings_path}",
                method_text,
            )
            self.assertIn(
                f"Text annotator settings file path: {text_settings_path}",
                method_text,
            )
            settings_text = settings_path.read_text(encoding="ascii")
            self.assertIn("annotator_id\tmsp_file_path\tpriority", settings_text)
            self.assertIn(f"realRT\t{real_rt.resolve()}\t3\t0.05", settings_text)
            self.assertIn("\tTrue\tTrue", settings_text)
            self.assertIn(f"predRT\t{pred_rt.resolve()}\t2\t1.0", settings_text)
            self.assertIn(f"msp_annotator_3\t{real_rt.resolve()}\t5", settings_text)
            text_settings_text = text_settings_path.read_text(encoding="ascii")
            self.assertIn("annotator_id\ttext_db_file_path\tpriority", text_settings_text)
            self.assertIn(f"text_annotator_1\t{text_library.resolve()}\t4\t0.1\t0.005\t0.7\tTrue\tTrue", text_settings_text)
            with zipfile.ZipFile(result["bundle"]) as archive:
                self.assertIn("msp_annotator_settings.tsv", set(archive.namelist()))
                self.assertIn("text_annotator_settings.tsv", set(archive.namelist()))

    def test_mztab_validation_builtin_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mztab = root / "alignment.mzTab"
            mztab.write_text(
                "\n".join(
                    [
                        "MTD\tmzTab-version\t2.0.0-M",
                        "MTD\tmzTab-mode\tComplete",
                        "SMH\tSML_ID\tchemical_formula",
                        "SML\tSML1\tC6H12O6",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = validate_mztab_file(mztab)
            self.assertEqual("passed", result["status"])
            self.assertEqual(2, result["counts"]["MTD"])
            summary = validate_mztab_outputs(root)["summary"]
            self.assertEqual("passed", summary["status"])
            self.assertEqual(1, summary["passed"])

    def test_mztab_validation_reports_missing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mztab = root / "bad.mzTab"
            mztab.write_text("SMH\tSML_ID\nSML\tSML1\n", encoding="utf-8")
            result = validate_mztab_file(mztab)
            self.assertEqual("failed", result["status"])
            self.assertTrue(any("mzTab-version" in message for message in result["errors"]))

    def test_mztab_validation_can_be_limited_to_job_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "old.mzTab"
            current = root / "current.mzTab"
            content = (
                "MTD\tmzTab-version\t2.0.0-M\n"
                "SMH\tidentifier\n"
                "SML\tfeature\n"
            )
            old.write_text(content, encoding="ascii")
            current.write_text(content, encoding="ascii")

            result = validate_mztab_files([current], root)

            self.assertEqual(1, result["summary"]["file_count"])
            self.assertEqual(str(current.resolve()), result["files"][0]["file"])

    def test_mztab_preview_reads_metadata_sections_and_numeric_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mztab = root / "alignment.mzTab"
            mztab.write_text(
                "\n".join(
                    [
                        "MTD\tmzTab-version\t2.0.0-M",
                        "MTD\tmzTab-ID\tpreview-demo",
                        "SMH\tSML_ID\tchemical_name\tabundance_assay[1]\topt_global_score",
                        "SML\tSML1\tLipid A\t123.4\t0.95",
                        "SML\tSML2\tLipid B\t\t0.10",
                        "SFH\tSMF_ID\texp_mass_to_charge\tretention_time_in_seconds",
                        "SMF\tSMF1\t760.585\t120.0",
                        "SEH\tSME_ID\tdatabase_identifier\tidentification_method",
                        "SME\tSME1\tHMDB:1\tMSP",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            preview = preview_mztab_file(mztab)
            self.assertEqual("preview-demo", preview["metadata"]["mzTab-ID"])
            self.assertEqual(2, preview["sections"]["SML"]["row_count"])
            self.assertEqual(1, preview["sections"]["SMF"]["row_count"])
            self.assertIn(
                "abundance_assay[1]",
                preview["sections"]["SML"]["suggested_columns"]["abundance"],
            )
            numeric_names = [
                column["name"]
                for column in preview["sections"]["SML"]["numeric_columns"]
            ]
            self.assertIn("abundance_assay[1]", numeric_names)
            output_preview = preview_mztab_outputs(root)
            self.assertEqual(str(mztab.resolve()), output_preview["file"])

    def test_agent_handoff_collects_mztab_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mztab = root / "alignment.mzTab"
            mztab.write_text(
                "MTD\tmzTab-version\t2.0.0-M\n"
                "SMH\tSML_ID\tchemical_formula\n"
                "SML\tSML1\tC6H12O6\n",
                encoding="utf-8",
            )
            preparation = {
                "run_directory": str(root),
                "analysis_type": "lcms",
                "input_csv": str(root / "analysis_files.csv"),
                "method_file": str(root / "method.txt"),
                "manifest": str(root / "run-manifest.json"),
                "command": ["MSDIALCUI.exe", "lcms"],
                "project_file_requested": True,
            }
            job = {
                "id": "job1",
                "kind": "run",
                "status": "completed",
                "exit_code": 0,
                "preparation": preparation,
                "logs": ["done"],
            }
            handoff = create_datamining_handoff(job=job)
            self.assertEqual("alignment.mzTab", Path(handoff["primary_mztab_file"]).name)
            self.assertEqual("passed", handoff["mztab_validation"]["summary"]["status"])
            self.assertTrue(Path(handoff["handoff_file"]).is_file())
            status = summarize_jobs({"job1": {**job, "datamining_handoff": handoff}})
            self.assertEqual("job1", status["latest_completed_job"]["id"])
            self.assertEqual("0.5", status["agent_api_version"])
            self.assertEqual(__version__, status["app_version"])
            self.assertIn("split_repository_unit_by_acquisition", status["capabilities"])
            self.assertIn("create_datamining_handoff", status["capabilities"])
            self.assertIn("inspect_repository_sample_metadata", status["capabilities"])
            self.assertIn("prepare_repository_reanalysis_without_ui", status["capabilities"])

    def test_agent_status_accepts_jobs_with_null_optional_objects(self) -> None:
        status = summarize_jobs(
            {
                "download": {
                    "id": "download",
                    "kind": "repository_download",
                    "status": "completed",
                    "preparation": None,
                    "datamining_handoff": None,
                    "mztab_validation": None,
                    "artifacts": None,
                }
            }
        )
        self.assertEqual("download", status["latest_completed_job"]["id"])
        self.assertEqual("", status["latest_completed_job"]["handoff_file"])

    def test_agent_status_is_bounded_and_omits_artifact_paths_by_default(self) -> None:
        jobs = {
            f"job-{index}": {
                "id": f"job-{index}",
                "status": "completed",
                "created_at": f"2026-09-02T00:{index:02d}:00+00:00",
                "artifacts": {"mztab": [f"D:/large/{index}/{item}.mzTab" for item in range(100)]},
            }
            for index in range(20)
        }
        status = summarize_jobs(jobs)
        self.assertEqual(5, len(status["jobs"]))
        self.assertNotIn("artifacts", status["latest_job"])
        self.assertEqual(100, status["latest_job"]["artifact_counts"]["mztab"])
        self.assertLess(len(json.dumps(status)), 8000)

    def test_console_dll_uses_dotnet(self) -> None:
        command = build_console_command("MSDIALCUI.dll", "a.csv", "out", "method.txt")
        self.assertEqual("dotnet", command[0])
        self.assertEqual("-p", command[-1])
        self.assertNotIn(
            "-p",
            build_console_command(
                "MSDIALCUI.dll",
                "a.csv",
                "out",
                "method.txt",
                project_store=False,
            ),
        )

    def test_gcms_prepare_generates_ri_dictionary_and_gcms_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.abf"
            raw.write_bytes(b"abf")
            ri = root / "alkaneinfo.txt"
            ri.write_text("Num\tRT(min)\n10\t4.024\n11\t5.164\n", encoding="ascii")
            template = root / "gcmsparam.txt"
            template.write_text(
                "\n".join(
                    [
                        "Target omics: Metabolomics",
                        "Ionization: EI",
                        "Machine category: GCMS",
                        "Msp file path:",
                        "Smoothing method: LinearWeightedMovingAverage",
                        "Minimum peak height: 1000",
                        "Mass slice width: 0.1",
                        "Accuracy type: IsNominal",
                        "Weighted dot product cutoff: 0.5",
                        "Simple dot product cutoff: 0.5",
                        "Reverse dot product cutoff: 0.5",
                        "Matched peaks percentage cutoff: 0.5",
                        "Minimum spectrum match: 3",
                        "RI index file pathes:",
                        "RI compound type: Alkanes",
                        "Retention type: RT",
                        "Alignment index type: RT",
                        "Retention index alignment tolerance: 10",
                    ]
                ),
                encoding="ascii",
            )
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"")
            state = {
                "files": expand_paths([str(raw)]),
                "project_type": "gcms",
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "output"),
                "ion_mode": "Positive",
                "target_omics": "Metabolomics",
                "ms1_data_type": "Centroid",
                "ms2_data_type": "Centroid",
                "smoothing_method": "LinearWeightedMovingAverage",
                "minimum_peak_height": 1234,
                "mass_slice_width": 0.5,
                "minimum_peak_width": 5,
                "retention_time_begin": 0,
                "retention_time_end": 30,
                "ms1_tolerance": 0.5,
                "ms2_tolerance": 0.5,
                "alignment_rt_tolerance": 0.05,
                "alignment_ms1_tolerance": 0.5,
                "msp_weighted_dot_product": 0.55,
                "msp_simple_dot_product": 0.56,
                "msp_reverse_dot_product": 0.57,
                "msp_matched_peaks_percentage": 0.58,
                "msp_minimum_spectrum_match": 4,
                "gcms_accuracy_type": "IsNominal",
                "gcms_ri_compound_type": "Alkanes",
                "gcms_retention_type": "RI",
                "gcms_alignment_index_type": "RI",
                "gcms_ri_alignment_tolerance": 12,
                "gcms_ri_source": "single",
                "gcms_ri_standard_path": str(ri),
                "selected_lipids": [],
                "selected_adducts": [],
            }

            prepared = prepare_run(state)
            self.assertIn("gcms", prepared["command"])
            self.assertEqual("gcms", prepared["analysis_type"])
            self.assertEqual(
                str(Path(prepared["run_directory"]) / "sample.mdscan"),
                prepared["diagnostic_result_file"],
            )
            method = Path(prepared["method_file"]).read_text(encoding="utf-8")
            self.assertIn("Smoothing method: LinearWeightedMovingAverage", method)
            self.assertIn("Accuracy type: IsNominal", method)
            self.assertIn("Retention type: RI", method)
            self.assertIn("Alignment index type: RI", method)
            self.assertIn("Retention index tolerance for alignment: 12", method)
            self.assertIn(
                "Square root of weighted dot product cutoff for MSP-based annotation: 0.55",
                method,
            )
            self.assertIn(
                "Square root of simple dot product cutoff for MSP-based annotation: 0.56",
                method,
            )
            self.assertIn(
                "Square root of reverse dot product cutoff for MSP-based annotation: 0.57",
                method,
            )
            self.assertIn(
                "Matched peaks percentage cutoff for MSP-based annotation: 0.58",
                method,
            )
            self.assertIn("Minimum spectrum match for MSP-based annotation: 4", method)
            ri_dictionary = Path(prepared["run_directory"]) / "ri_dictionary_paths.txt"
            self.assertTrue(ri_dictionary.is_file())
            self.assertIn(str(raw.resolve()), ri_dictionary.read_text(encoding="ascii"))
            self.assertIn(str(ri.resolve()), ri_dictionary.read_text(encoding="ascii"))
            with zipfile.ZipFile(prepared["bundle"]) as archive:
                self.assertIn("ri_dictionary_paths.txt", set(archive.namelist()))
                self.assertIn("gcms", archive.read("run-msdial.sh").decode("utf-8"))

    def test_gcms_per_file_ri_dictionary_builder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raws = []
            for name in ("a.abf", "b.abf"):
                raw = root / name
                raw.write_bytes(b"abf")
                raws.append(raw)
            ri_a = root / "alkane_a.txt"
            ri_b = root / "alkane_b.txt"
            ri_a.write_text("Num\tRT(min)\n10\t4.0\n", encoding="ascii")
            ri_b.write_text("Num\tRT(min)\n10\t4.2\n", encoding="ascii")
            template = root / "gcmsparam.txt"
            template.write_text(
                "Target omics: Metabolomics\nRI index file pathes:\nRetention type: RT\nAlignment index type: RT\n",
                encoding="ascii",
            )
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"")
            files = expand_paths([str(root)])
            mapping = {
                str(raws[0].resolve()): str(ri_a),
                str(raws[1].resolve()): str(ri_b),
            }
            prepared = prepare_run(
                {
                    "files": files,
                    "project_type": "gcms",
                    "console_path": str(console),
                    "template_path": str(template),
                    "output_root": str(root / "output"),
                    "target_omics": "Metabolomics",
                    "smoothing_method": "LinearWeightedMovingAverage",
                    "gcms_retention_type": "RI",
                    "gcms_alignment_index_type": "RI",
                    "gcms_ri_source": "perFile",
                    "gcms_ri_file_map": [
                        {"file_path": key, "ri_path": value}
                        for key, value in mapping.items()
                    ],
                }
            )
            dictionary = Path(prepared["run_directory"]) / "ri_dictionary_paths.txt"
            text = dictionary.read_text(encoding="ascii")
            self.assertIn(str(ri_a.resolve()), text)
            self.assertIn(str(ri_b.resolve()), text)

    def test_lipid_query_parser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "LbmQueries.txt"
            path.write_text(
                "Class\tAdduct\tIon mode\tIsSelected\nPC\t[M+H]+\tPositive\tTRUE\n",
                encoding="utf-8",
            )
            rows = read_lipid_queries(path)
            self.assertEqual("PC", rows[0]["lipid_class"])
            self.assertTrue(rows[0]["selected"])

    def test_folder_type_vendor_detection_and_starting_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            waters = root / "sample.raw"
            waters.mkdir()
            agilent = root / "agilent.d"
            (agilent / "AcqData").mkdir(parents=True)
            bruker = root / "bruker.d"
            bruker.mkdir()
            (bruker / "analysis.baf").write_bytes(b"")
            thermo = root / "thermo.raw"
            thermo.write_bytes(b"")

            expanded = expand_paths([str(root)])
            self.assertEqual(4, len(expanded))
            vendors = {item["file_name"]: item["vendor"] for item in expanded}
            self.assertEqual("Waters", vendors["sample"])
            self.assertEqual("Agilent", vendors["agilent"])
            self.assertEqual("Bruker", vendors["bruker"])
            self.assertEqual("Thermo", vendors["thermo"])
            self.assertEqual(
                {"minimum_peak_height": 10000, "mass_slice_width": 0.05},
                format_based_peak_parameters(expanded),
            )
            self.assertEqual("Agilent", detect_raw_format(agilent)["vendor"])

    MDPEAK_HEADER = [
        "Peak ID",
        "Name",
        "Height",
        "Simple dot product",
        "Weighted dot product",
        "Reverse dot product",
        "Matched peaks count",
        "Matched peaks percentage",
    ]

    def _write_mdpeak(self, directory: Path, *rows: str) -> Path:
        path = directory / "sample.mdpeak"
        path.write_text(
            "\t".join(self.MDPEAK_HEADER) + "\n" + "".join(row + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def test_parse_ascii_mdpeak(self) -> None:
        """A Console older than the shared AnnotationScoreFormat: 0.000 dot products with a -1
        matched-peak sentinel for the precursor-only row."""
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_mdpeak(
                Path(temporary),
                "0\tKnown compound\t50\t0.6\t0.7\t0.8\t4\t0.5",
                "1\tno MS2: FA 5:0\t75\t0\t0\t0\t-1\t-1",
                "2\tUnknown\t100\tnull\tnull\tnull\tnull\tnull",
            )
            result = parse_mdpeak(path)
            self.assertEqual([50.0, 75.0, 100.0], result["heights"])
            self.assertEqual(2, result["msp_candidate_count"])
            self.assertEqual(1, result["msp_scored_count"])
            self.assertEqual(0.7, result["msp_scores"][0]["weighted"])

    def test_parse_ascii_mdpeak_reports_same_counts_for_null_uncomputed_scores(self) -> None:
        """A Console that includes the shared AnnotationScoreFormat writes null into every score
        column of a precursor-only row. The reported counts must not change."""
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_mdpeak(
                Path(temporary),
                "0\tKnown compound\t50\t0.6\t0.7\t0.8\t4\t0.5",
                "1\tno MS2: FA 5:0\t75\tnull\tnull\tnull\tnull\tnull",
                "2\tUnknown\t100\tnull\tnull\tnull\tnull\tnull",
            )
            result = parse_mdpeak(path)
            self.assertEqual(2, result["msp_candidate_count"])
            self.assertEqual(1, result["msp_scored_count"])
            self.assertEqual(1, len(result["msp_scores"]))

    def test_parse_ascii_mdpeak_keeps_a_comparison_that_scored_zero(self) -> None:
        """A product-ion spectrum was compared and nothing overlapped. Those zeros are
        measurements, so the row is a scored candidate."""
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_mdpeak(
                Path(temporary),
                "0\tlow score: LPA 13:1\t50\t0\t0\t0\t0\t0",
                "1\tno MS2: FA 5:0\t75\tnull\tnull\tnull\tnull\tnull",
            )
            result = parse_mdpeak(path)
            self.assertEqual(2, result["msp_candidate_count"])
            self.assertEqual(1, result["msp_scored_count"])
            self.assertEqual(0.0, result["msp_scores"][0]["matched_count"])

    def test_folder_type_tuning_uses_linked_input_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agilent = root / "sample.d"
            (agilent / "AcqData").mkdir(parents=True)
            template = root / "method.txt"
            template.write_text(
                "Ion mode: Negative\nTarget omics: Metabolomics\n",
                encoding="ascii",
            )
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"")
            files = expand_paths([str(agilent)])
            state = {
                "files": files,
                "project_type": "lcms",
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "output"),
                "ion_mode": "Negative",
                "target_omics": "Metabolomics",
                "selected_adducts": ["[M-H]-"],
                "selected_lipids": [],
                "msp_annotators": [],
            }

            old = os.environ.get("MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED")
            os.environ["MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED"] = "1"
            try:
                prepared = prepare_tuning_run(state, files[0]["file_path"], root / "output")
            finally:
                if old is None:
                    os.environ.pop("MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED", None)
                else:
                    os.environ["MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED"] = old

            input_index = prepared["command"].index("-i") + 1
            diagnostic_input = Path(prepared["command"][input_index])
            self.assertEqual(Path(prepared["input_csv"]), diagnostic_input)
            self.assertFalse(prepared.get("diagnostic_input_folder"))

    def test_folder_type_run_uses_csv_with_patched_console(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raws = []
            for name in ("sample1.d", "sample2.d"):
                raw = root / name
                (raw / "AcqData").mkdir(parents=True)
                raws.append(raw)
            template = root / "method.txt"
            template.write_text(
                "Ion mode: Negative\nTarget omics: Metabolomics\n",
                encoding="ascii",
            )
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"")
            state = {
                "files": expand_paths([str(path) for path in raws]),
                "project_type": "lcms",
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "output"),
                "ion_mode": "Negative",
                "target_omics": "Metabolomics",
                "selected_adducts": ["[M-H]-"],
                "selected_lipids": [],
                "msp_annotators": [],
                "project_store": True,
            }

            old = os.environ.get("MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED")
            os.environ["MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED"] = "1"
            try:
                prepared = prepare_run(state)
            finally:
                if old is None:
                    os.environ.pop("MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED", None)
                else:
                    os.environ["MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED"] = old

            input_index = prepared["command"].index("-i") + 1
            console_input = Path(prepared["command"][input_index])
            self.assertEqual(Path(prepared["input_csv"]), console_input)
            self.assertEqual("", prepared["temporary_input_folder"])
            self.assertFalse(prepared["preserve_temporary_input_folder"])

    def test_folder_type_run_rejects_unpatched_console(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agilent = root / "sample.d"
            (agilent / "AcqData").mkdir(parents=True)
            template = root / "method.txt"
            template.write_text(
                "Ion mode: Negative\nTarget omics: Metabolomics\n",
                encoding="ascii",
            )
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"")
            state = {
                "files": expand_paths([str(agilent)]),
                "project_type": "lcms",
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "output"),
                "ion_mode": "Negative",
                "target_omics": "Metabolomics",
                "selected_adducts": ["[M-H]-"],
                "selected_lipids": [],
                "msp_annotators": [],
            }

            issues = validate_workflow(state)

            self.assertTrue(
                any(
                    "does not support folder-type raw-data paths" in issue["message"]
                    for issue in issues
                )
            )
            with self.assertRaisesRegex(ValueError, "folder-type raw-data paths"):
                prepare_run(state)

    def test_parse_ascii_mdscan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.mdscan"
            path.write_text(
                "\t".join(
                    [
                        "Name",
                        "Integrated height",
                        "Simple dot product",
                        "Weighted dot product",
                        "Reverse dot product",
                        "Matched peaks count",
                        "Fragment presence %",
                        "Spectrum",
                    ]
                )
                + "\n"
                + "Known\t50\t0.6\t0.7\t0.8\t4\t0.5\t55.0:10 57.0:20\n"
                + "Unknown\t75\t-1\t-1\t-1\t-1\t-1\t55.0:10\n",
                encoding="utf-8",
            )
            result = parse_mdscan(path)
            self.assertEqual([50.0, 75.0], result["heights"])
            # GcmsAnalysisMetadataAccessor writes -1 into every score column of an Unknown row,
            # so only the named row is a reference candidate.
            self.assertEqual(1, result["msp_candidate_count"])
            self.assertEqual(1, result["msp_scored_count"])
            self.assertEqual(4.0, result["msp_scores"][0]["matched_count"])

    def test_sciex_primary_files_and_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wiff = root / "sample.wiff"
            wiff.write_bytes(b"")
            (root / "sample.wiff.scan").write_bytes(b"")
            wiff2 = root / "sample.wiff2"
            wiff2.write_bytes(b"")

            report = expand_paths_report([str(root)])

            self.assertEqual(2, len(report["files"]))
            formats = {item["format"] for item in report["files"]}
            self.assertEqual({"SCIEX WIFF", "SCIEX WIFF2"}, formats)
            self.assertEqual("SCIEX", detect_raw_format(wiff)["vendor"])
            self.assertTrue(detect_raw_format(wiff)["sidecar_available"])
            self.assertEqual(1, len(report["warnings"]))
            self.assertIn("Both .wiff and .wiff2", report["warnings"][0])

    def test_sciex_sidecar_is_not_an_analysis_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sidecar = Path(temporary) / "sample.wiff.scan"
            sidecar.write_bytes(b"")

            report = expand_paths_report([str(sidecar)])

            self.assertEqual([], report["files"])
            self.assertEqual([str(sidecar.resolve())], report["rejected"])

    def test_sciex_wiff_without_sidecar_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wiff = Path(temporary) / "sample.wiff"
            wiff.write_bytes(b"")

            report = expand_paths_report([str(wiff)])

            self.assertEqual(1, len(report["files"]))
            self.assertEqual([], report["warnings"])
            self.assertFalse(report["files"][0]["sidecar_available"])
            issues = validate_workflow(
                {
                    "files": report["files"],
                    "project_type": "lcms",
                    "console_path": str(wiff),
                    "template_path": str(wiff),
                    "output_root": str(Path(temporary) / "output"),
                    "target_omics": "Metabolomics",
                    "selected_adducts": ["[M-H]-"],
                }
            )
            self.assertTrue(
                any("WIFF.SCAN is not accessible" in issue["message"] for issue in issues)
            )

    def test_prepare_run_uses_original_wiff_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wiff = root / "sample.wiff"
            wiff.write_bytes(b"wiff")
            sidecar = root / "sample.wiff.scan"
            sidecar.write_bytes(b"scan")
            template = root / "method.txt"
            template.write_text(
                "Ion mode: Negative\nTarget omics: Metabolomics\n",
                encoding="utf-8",
            )
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"")
            prepared = prepare_run(
                {
                    "files": expand_paths([str(wiff)]),
                    "project_type": "lcms",
                    "console_path": str(console),
                    "template_path": str(template),
                    "output_root": str(root / "output"),
                    "ion_mode": "Negative",
                    "target_omics": "Metabolomics",
                    "selected_adducts": ["[M-H]-"],
                    "stage_inputs": False,
                    "repository_metadata": {
                        "schema": "msdial-repository-metadata.v1",
                        "repository": "test",
                        "accession": "X1",
                        "fields": [{"name": "Group", "non_missing": 1, "missing": 0, "unique_count": 1, "examples": ["Control"]}],
                        "rows": [{"sample_id": "sample", "raw_file": "sample.wiff", "values": {"Group": "Control"}, "class_id": "Control"}],
                        "hierarchy": ["Group"],
                    },
                }
            )

            run_directory = Path(prepared["run_directory"])
            self.assertFalse((run_directory / "input").exists())
            csv_text = Path(prepared["input_csv"]).read_text(encoding="ascii")
            self.assertIn(str(wiff.resolve()), csv_text)
            manifest = Path(prepared["manifest"]).read_text(encoding="utf-8")
            self.assertIn('"stage_inputs": false', manifest)
            self.assertIn("X1_repository_metadata_reviewed.json", manifest)
            with zipfile.ZipFile(prepared["bundle"]) as archive:
                self.assertIn("X1_repository_metadata_reviewed.json", archive.namelist())
                self.assertIn("X1_sample_metadata_reviewed.tsv", archive.namelist())

    def test_prepare_run_stages_a_wiff_with_its_sidecar_when_asked(self) -> None:
        # A .wiff is unreadable without its .wiff.scan, so staging one without the other
        # would produce an input that fails deep inside the vendor reader.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wiff = root / "sample.wiff"
            wiff.write_bytes(b"wiff")
            (root / "sample.wiff.scan").write_bytes(b"scan")
            template = root / "method.txt"
            template.write_text(
                "Ion mode: Negative" + chr(10) + "Target omics: Metabolomics" + chr(10), encoding="utf-8"
            )
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"")
            prepared = prepare_run(
                {
                    "files": expand_paths([str(wiff)]),
                    "project_type": "lcms",
                    "console_path": str(console),
                    "template_path": str(template),
                    "output_root": str(root / "output"),
                    "ion_mode": "Negative",
                    "target_omics": "Metabolomics",
                    "selected_adducts": ["[M-H]-"],
                    "stage_inputs": True,
                }
            )

            staged = Path(prepared["run_directory"]) / "input"
            self.assertTrue((staged / "sample.wiff").is_file())
            self.assertTrue((staged / "sample.wiff.scan").is_file())
            csv_text = Path(prepared["input_csv"]).read_text(encoding="ascii")
            self.assertIn(str(staged / "sample.wiff"), csv_text)
            self.assertNotIn(str(wiff.resolve()), csv_text)
            self.assertEqual(str(staged), prepared["temporary_input_folder"])
            # The copy was asked for, so it outlives the run rather than being cleaned up.
            self.assertTrue(prepared["preserve_temporary_input_folder"])

            tuning = prepare_tuning_run(
                {
                    "files": expand_paths([str(wiff)]),
                    "project_type": "lcms",
                    "console_path": str(console),
                    "template_path": str(template),
                    "output_root": str(root / "ignored"),
                    "ion_mode": "Negative",
                    "target_omics": "Metabolomics",
                    "selected_adducts": ["[M-H]-"],
                },
                str(wiff.resolve()),
                root / "diagnostic-output",
            )
            self.assertEqual(
                (root / "diagnostic-output").resolve(),
                Path(tuning["run_directory"]),
            )
            self.assertNotIn("-p", tuning["command"])

    def test_tuning_runs_with_alignment_features_switched_off(self) -> None:
        # The diagnostic runs one file without alignment. With automatic RT correction or light
        # mode still on from the production state it was refused as "requires Together with
        # alignment", so a unit that had enabled either could not be tuned.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.mzML"
            raw.write_text("raw", encoding="ascii")
            console = root / "MSDIALCUI.exe"
            console.write_bytes("execute automatic rt correction for alignment".encode("utf-16-le"))
            template = root / "method.txt"
            template.write_text(
                "Ion mode: Negative\n"
                "Execute automatic RT correction for alignment: True\n"
                "Automatic RT correction minimum anchors: 3\n"
                "Alignment light mode: True\n",
                encoding="ascii",
            )
            state = {
                "files": expand_paths([str(raw)]),
                "project_type": "lcms",
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "output"),
                "ion_mode": "Negative",
                "target_omics": "Metabolomics",
                "selected_adducts": ["[M-H]-"],
                "together_with_alignment": True,
                "alignment_light_mode": True,
                "execute_automatic_rt_correction": True,
            }

            tuning = prepare_tuning_run(state, str(raw.resolve()), root / "diagnostic")
            method = Path(tuning["method_file"]).read_text(encoding="utf-8").lower()

        self.assertNotIn("automatic rt correction", method)
        self.assertNotIn("alignment light mode: true", method)
        self.assertTrue(state["execute_automatic_rt_correction"])  # the production state is untouched

    def test_automatic_rt_validation_uses_the_defaults_the_writer_writes(self) -> None:
        # An absent tolerance defaulted to 0 in the validator, which refused it, while the writer
        # would have written 0.5 for the same state.
        issues = validate_workflow(
            {
                "project_type": "lcms",
                "together_with_alignment": True,
                "execute_automatic_rt_correction": True,
            }
        )

        messages = [item["message"] for item in issues if item["level"] == "error"]
        self.assertFalse(
            [message for message in messages if message.startswith("Automatic RT correction")],
            messages,
        )

    def test_automatic_rt_whole_number_settings_are_refused_not_truncated(self) -> None:
        # int() turned 2.9 into 2 for the check while the method file carried 2.9, which the
        # Console refuses and replaces with its default of 3.
        base = {
            "project_type": "lcms",
            "together_with_alignment": True,
            "execute_automatic_rt_correction": True,
        }
        cases = {
            "automatic_rt_correction_minimum_anchors": (2.9, "minimum anchors must be a whole number"),
            "automatic_rt_correction_maximum_anchors": (6.5, "maximum anchors must be a whole number"),
            "automatic_rt_correction_reference_file_id": (-2, "reference file ID must be -1"),
        }
        for key, (value, message) in cases.items():
            with self.subTest(key=key):
                issues = validate_workflow({**base, key: value})
                self.assertTrue(
                    any(item["level"] == "error" and message in item["message"] for item in issues),
                    issues,
                )

    def test_automatic_rt_outlier_mad_threshold_zero_means_no_rejection(self) -> None:
        # The Console skips MAD rejection when the threshold is 0; only a negative value is wrong.
        base = {
            "project_type": "lcms",
            "together_with_alignment": True,
            "execute_automatic_rt_correction": True,
        }
        zero = validate_workflow({**base, "automatic_rt_correction_outlier_mad_threshold": 0})
        negative = validate_workflow({**base, "automatic_rt_correction_outlier_mad_threshold": -1})

        self.assertFalse(any("outlier MAD" in item["message"] for item in zero), zero)
        self.assertTrue(any("outlier MAD" in item["message"] for item in negative), negative)

    @patch("msdial_app.workflow.subprocess.run")
    def test_console_capability_reads_the_net8_assembly_beside_its_launcher(self, run: Mock) -> None:
        # A net8 MSDIALCUI.exe, or MSDIALCUI on Linux and macOS, is an apphost; the strings are
        # in MSDIALCUI.dll next to it, and a launcher has a runtimeconfig.json.
        run.return_value = Mock(returncode=1, stdout="", stderr="Unknown command")
        for name in ("MSDIALCUI.exe", "MSDIALCUI"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                console = Path(temporary) / name
                console.write_bytes(b"apphost")
                (Path(temporary) / "MSDIALCUI.runtimeconfig.json").write_text("{}", encoding="ascii")
                (Path(temporary) / "MSDIALCUI.dll").write_bytes(
                    "Automatic alignment RT correction audit:".encode("utf-16-le")
                )

                result = console_capabilities(str(console))

                self.assertIn("automatic_alignment_rt_correction", result["capabilities"])

    @staticmethod
    def _managed_pe() -> bytes:
        """A minimal PE32 image whose CLI header directory is present, as a net48 exe has."""
        import struct

        image = bytearray(0x200)
        image[0:2] = b"MZ"
        struct.pack_into("<I", image, 0x3C, 0x80)
        image[0x80:0x84] = b"PE\0\0"
        optional = 0x80 + 24
        struct.pack_into("<H", image, optional, 0x10B)
        struct.pack_into("<II", image, optional + 96 + 14 * 8, 0x2008, 0x48)
        return bytes(image)

    @patch("msdial_app.workflow.subprocess.run")
    def test_a_net48_exe_over_a_net8_folder_is_read_as_itself(self, run: Mock) -> None:
        # Unpacking net48 over net8 leaves the net8 dll and its runtimeconfig.json behind; the
        # exe's own CLI header says it is the assembly, so the dll is not read.
        run.return_value = Mock(returncode=1, stdout="", stderr="Unknown command")
        with tempfile.TemporaryDirectory() as temporary:
            console = Path(temporary) / "MSDIALCUI.exe"
            console.write_bytes(self._managed_pe())
            (Path(temporary) / "MSDIALCUI.runtimeconfig.json").write_text("{}", encoding="ascii")
            (Path(temporary) / "MSDIALCUI.dll").write_bytes(
                "Automatic alignment RT correction audit:".encode("utf-16-le")
            )

            result = console_capabilities(str(console))

        self.assertNotIn("automatic_alignment_rt_correction", result["capabilities"])

    @patch("msdial_app.workflow.subprocess.run")
    def test_a_stale_dll_does_not_lend_a_net48_console_features(self, run: Mock) -> None:
        # Unpacking a net48 archive over a net8 one replaces MSDIALCUI.exe and leaves the net8
        # dll behind. The net48 exe is the assembly itself and has no runtimeconfig.json.
        run.return_value = Mock(returncode=1, stdout="", stderr="Unknown command")
        with tempfile.TemporaryDirectory() as temporary:
            console = Path(temporary) / "MSDIALCUI.exe"
            console.write_bytes(b"net48 assembly without the markers")
            (Path(temporary) / "MSDIALCUI.dll").write_bytes(
                "Automatic alignment RT correction audit:".encode("utf-16-le")
                + "LC-MS quality-assurance matrix:".encode("utf-16-le")
            )

            result = console_capabilities(str(console))

        self.assertNotIn("automatic_alignment_rt_correction", result["capabilities"])
        self.assertNotIn("lcms_alignment_qa_matrix", result["capabilities"])

    def test_reproduction_reads_its_own_method_file(self) -> None:
        # The Console writes <method>.keys.json beside the method file, so a reproduction that
        # read the original method file overwrote the run's key record. It reads a byte copy
        # under another name in the same directory, so relative paths still resolve, and a CSV
        # copy in reproduced-results, so the Console's project folder leaves the run too.
        from msdial_app.workflow import _powershell_script, _shell_script

        for store_project in (True, False):
            with self.subTest(store_project=store_project):
                powershell = _powershell_script(r"C:\msdial\MSDIALCUI.exe", "lcms", store_project)
                shell = _shell_script("/opt/msdial/MSDIALCUI.dll", "lcms", store_project)

                self.assertIn("'method.reproduce.txt'", powershell)
                self.assertIn("'-i', $Inputs, '-o', $Output, '-m', $Method", powershell)
                self.assertIn("-ErrorAction Stop", powershell)
                self.assertIn("if ($null -eq $LASTEXITCODE) { exit 1 }", powershell)
                self.assertIn('METHOD="$HERE/method.reproduce.txt"', shell)
                self.assertIn('-i "$INPUTS" -o "$OUTPUT" -m "$METHOD"', shell)
                self.assertNotIn(",,}", shell)  # bash 4 only
                # Pop-Location in a finally, so a failed start does not strand the caller.
                self.assertIn("} finally {\n  Pop-Location\n}", powershell)
                self.assertEqual(store_project, "'-p'" in powershell)
                self.assertEqual(store_project, '"$METHOD" -p' in shell)

    @staticmethod
    def _reproduction_bundle(root: Path) -> bytes:
        method = "\ufeffIon mode: Negative\r\nMsp file path: D:\\\u30e9\u30a4\u30d6\u30e9\u30ea\\a.msp\r\n".encode("utf-8")
        (root / "method.txt").write_bytes(method)
        (root / "analysis_files.csv").write_text("file_path\nD:\\raw\\a.mzML\n", encoding="utf-8")
        return method

    @unittest.skipUnless(shutil.which("bash"), "bash is required")
    def test_shell_reproduction_runs_from_copies_and_leaves_the_run_alone(self) -> None:
        from msdial_app.workflow import _shell_script

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            method = self._reproduction_bundle(root)
            script = root / "run-msdial.sh"
            script.write_text(_shell_script("echo", "lcms"), encoding="utf-8", newline="\n")

            # The resolved path, not "bash": Windows looks in System32 before PATH, where
            # bash.exe is the WSL launcher.
            completed = subprocess.run(
                [shutil.which("bash"), script.name],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(method, (root / "method.reproduce.txt").read_bytes())
            self.assertEqual(method, (root / "method.txt").read_bytes())
            self.assertTrue((root / "reproduced-results" / "analysis_files.csv").is_file())
            self.assertIn("method.reproduce.txt", completed.stdout)

            # A Console named relative to the caller's directory, even by a bare name, is found
            # there, not in the bundle the script changes into.
            caller = root / "caller"
            caller.mkdir()
            fake = caller / "fake-console"
            fake.write_text('#!/bin/sh\necho FAKE-CONSOLE "$@"\n', encoding="ascii", newline="\n")
            fake.chmod(0o755)
            bare = subprocess.run(
                [shutil.which("bash"), str(script), "fake-console"],
                cwd=caller,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(0, bare.returncode, bare.stderr)
            self.assertIn("FAKE-CONSOLE", bare.stdout)

            # A directory named like the Console is not a Console; the name is looked up on PATH.
            tools = root / "tools"
            tools.mkdir()
            on_path = tools / "path-console"
            on_path.write_text('#!/bin/sh\necho PATH-CONSOLE "$@"\n', encoding="ascii", newline="\n")
            on_path.chmod(0o755)
            (caller / "path-console").mkdir()
            tools_posix = subprocess.run(
                [shutil.which("bash"), "-c", 'cd "$1" && pwd', "_", str(tools)],
                capture_output=True, text=True, timeout=60,
            ).stdout.strip()
            via_path = subprocess.run(
                [shutil.which("bash"), "-c",
                 f'PATH="{tools_posix}:$PATH" "$0" "$1" path-console', "bash", str(script)],
                cwd=caller, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(0, via_path.returncode, via_path.stderr)
            self.assertIn("PATH-CONSOLE", via_path.stdout)

            # A relative path missing from the caller's directory fails; it never runs a file
            # of that name in the bundle the script changes into.
            (root / "sub").mkdir()
            decoy = root / "sub" / "decoy-console"
            decoy.write_text('#!/bin/sh\necho BUNDLE-DECOY\n', encoding="ascii", newline="\n")
            decoy.chmod(0o755)
            missing = subprocess.run(
                [shutil.which("bash"), str(script), "sub/decoy-console"],
                cwd=caller, capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(0, missing.returncode)
            self.assertNotIn("BUNDLE-DECOY", missing.stdout)

            # A protected run record gives read-only copies; a second run must still start.
            (root / "method.reproduce.txt").unlink()
            (root / "reproduced-results" / "analysis_files.csv").unlink()
            for name in ("method.txt", "analysis_files.csv"):
                (root / name).chmod(0o444)
            try:
                for attempt in (1, 2):
                    again = subprocess.run(
                        [shutil.which("bash"), script.name],
                        cwd=root,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    self.assertEqual(0, again.returncode, (attempt, again.stderr))
            finally:
                for path in (root / "method.txt", root / "analysis_files.csv",
                             root / "method.reproduce.txt",
                             root / "reproduced-results" / "analysis_files.csv"):
                    if path.exists():
                        path.chmod(0o644)

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell is required")
    def test_powershell_reproduction_copies_bytes_and_fails_when_nothing_runs(self) -> None:
        from msdial_app.workflow import _powershell_script

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            method = self._reproduction_bundle(root)
            fake = root / "fake.cmd"
            fake.write_text("@echo CWD=%CD% %*\r\n", encoding="ascii")
            script = root / "run-msdial.ps1"
            script.write_text(_powershell_script(str(fake), "lcms"), encoding="utf-8-sig")

            def run(*extra: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(script), *extra],
                    capture_output=True, text=True, timeout=120,
                )

            completed = run()
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(method, (root / "method.reproduce.txt").read_bytes())
            self.assertEqual(method, (root / "method.txt").read_bytes())
            self.assertIn("method.reproduce.txt", completed.stdout)
            # The Console runs from the bundle, so a relative path in method.txt means one thing.
            self.assertIn(f"CWD={root.resolve()}".casefold(), completed.stdout.casefold())

            # A Console path given relative to the caller's directory is resolved before the move.
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            relative = subprocess.run(
                [shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script), os.path.join("..", "fake.cmd")],
                cwd=elsewhere, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(0, relative.returncode, relative.stderr)
            self.assertIn("method.reproduce.txt", relative.stdout)

            # A relative path missing from the caller's directory is not found in the bundle.
            (root / "sub").mkdir()
            (root / "sub" / "decoy.cmd").write_text("@echo BUNDLE-DECOY\r\n", encoding="ascii")
            decoy = subprocess.run(
                [shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script), os.path.join("sub", "decoy.cmd")],
                cwd=elsewhere, capture_output=True, text=True, timeout=120,
            )
            self.assertNotEqual(0, decoy.returncode)
            self.assertNotIn("BUNDLE-DECOY", decoy.stdout)

            # A Console that cannot be started must not look like a reproduction that ran.
            missing = run(str(root / "no-such-console.exe"))
            self.assertNotEqual(0, missing.returncode)

            # Nor may a failed copy leave an older method file to run on.
            (root / "method.txt").unlink()
            no_method = run()
            self.assertNotEqual(0, no_method.returncode)
            self.assertNotIn("method.reproduce.txt", no_method.stdout)

    def test_bundle_scripts_carry_the_runs_project_flag_and_a_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.mzML"
            raw.write_text("raw", encoding="ascii")
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"stub")
            template = root / "method.txt"
            template.write_text("Ion mode: Negative\n", encoding="ascii")
            for store in (True, False):
                with self.subTest(project_store=store):
                    prepared = prepare_run(
                        {
                            "project_type": "lcms",
                            "files": expand_paths([str(raw)]),
                            "console_path": str(console),
                            "template_path": str(template),
                            "output_root": str(root / f"output-{store}"),
                            "ion_mode": "Negative",
                            "target_omics": "Metabolomics",
                            "project_store": store,
                        }
                    )
                    run_directory = Path(prepared["run_directory"])
                    powershell = (run_directory / "run-msdial.ps1").read_bytes()
                    shell = (run_directory / "run-msdial.sh").read_text(encoding="utf-8")

                    self.assertTrue(powershell.startswith(b"\xef\xbb\xbf"))
                    self.assertEqual("-p" in prepared["command"], b"'-p'" in powershell)
                    self.assertEqual("-p" in prepared["command"], '"$METHOD" -p' in shell)

    def test_automatic_rt_values_the_console_would_discard_are_refused(self) -> None:
        # Each of these was accepted and written to method.txt, where the Console refused it
        # and used its default, or accepted it and then failed after all peak picking.
        base = {
            "project_type": "lcms",
            "together_with_alignment": True,
            "execute_automatic_rt_correction": True,
        }
        cases = {
            "automatic_rt_correction_reference_file_id": [3_000_000_000],
            "automatic_rt_correction_maximum_anchors": [2**31, 1e20],
            "automatic_rt_correction_match_rt_tolerance": ["nan", float("inf"), "abc", None, ""],
            "automatic_rt_correction_rt_bin_width": [float("nan")],
            "automatic_rt_correction_outlier_mad_threshold": ["nan"],
            "automatic_rt_correction_minimum_signal_to_noise": [-1, "inf"],
            "automatic_rt_correction_minimum_gaussian_similarity": [5, False],
            "automatic_rt_correction_minimum_ideal_slope": [1.5],
            # The method file would say True, 1_000 or a full-width digit; the Console reads none.
            "automatic_rt_correction_minimum_sample_coverage": [True],
            "automatic_rt_correction_reference_file_id": [True, "\uff11"],
            "automatic_rt_correction_maximum_anchors": ["1_000"],
            # Above 0 as a double, 0 as the float the Console stores.
            "automatic_rt_correction_match_rt_tolerance": [1e-46],
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    issues = validate_workflow({**base, key: value})
                    self.assertTrue(
                        any(
                            item["level"] == "error" and item["message"].startswith("Automatic RT correction")
                            for item in issues
                        ),
                        issues,
                    )

    def test_automatic_rt_method_lines_carry_the_validated_value(self) -> None:
        # Written as given, "\n5" split the method line so the Console read a blank key, and
        # bool("false") wrote True. The writer writes the parsed value.
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "template.txt"
            template.write_text("Ion mode: Negative\n", encoding="ascii")
            method = Path(temporary) / "method.txt"
            _write_method(
                method,
                {
                    "project_type": "lcms",
                    "template_path": str(template),
                    "target_omics": "Metabolomics",
                    "execute_automatic_rt_correction": True,
                    "automatic_rt_correction_minimum_anchors": "\n5",
                    "automatic_rt_correction_maximum_anchors": "7.",
                    "automatic_rt_correction_rt_bin_width": " 0.2\r",
                    "automatic_rt_correction_interpolate_blanks_by_analytical_order": "false",
                },
            )
            lines = method.read_text(encoding="utf-8").splitlines()

        self.assertIn("Automatic RT correction minimum anchors: 5", lines)
        self.assertIn("Automatic RT correction maximum anchors: 7", lines)
        self.assertIn("Automatic RT correction RT bin width: 0.2", lines)
        self.assertIn("Automatic RT correction interpolate blanks by analytical order: False", lines)
        self.assertFalse([line for line in lines if line.strip() in {"5", "0.2"}], lines)

    def test_automatic_rt_needs_a_positive_alignment_ms1_tolerance(self) -> None:
        # The correction matches anchors within this tolerance and refuses 0 after peak picking.
        base = {
            "project_type": "lcms",
            "together_with_alignment": True,
            "execute_automatic_rt_correction": True,
        }
        for value, refused in ((0, True), (-0.01, True), ("", True), (True, True), (0.015, False)):
            with self.subTest(value=value):
                issues = validate_workflow({**base, "alignment_ms1_tolerance": value})
                found = any("alignment MS1 tolerance" in item["message"] for item in issues)
                self.assertEqual(refused, found, issues)
        # Without the feature the tolerance is not this check's business.
        issues = validate_workflow({"project_type": "lcms", "alignment_ms1_tolerance": 0})
        self.assertFalse(any("alignment MS1 tolerance" in item["message"] for item in issues))

    def test_automatic_rt_reference_file_must_be_a_non_blank_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = ["blank_01.mzML", "sample_01.mzML", "sample_02.mzML"]
            for name in names:
                (root / name).write_text("raw", encoding="ascii")
            files = expand_paths([str(root / name) for name in names])
            files[0]["file_type"] = "Blank"
            base = {
                "project_type": "lcms",
                "together_with_alignment": True,
                "execute_automatic_rt_correction": True,
                "files": files,
            }
            expected = {0: "is a Blank", 3: "is not a file", 1: None, -1: None}
            for reference, message in expected.items():
                with self.subTest(reference=reference):
                    issues = validate_workflow(
                        {**base, "automatic_rt_correction_reference_file_id": reference}
                    )
                    found = [item["message"] for item in issues if "reference file ID" in item["message"]]
                    if message is None:
                        self.assertEqual([], found)
                    else:
                        self.assertTrue(any(message in text for text in found), found)

    def test_template_whole_number_settings_are_not_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "method.txt"
            template.write_text(
                "Ion mode: Positive\n"
                "Automatic RT correction minimum anchors: 2.9\n"
                "Automatic RT correction maximum anchors: nan\n",
                encoding="ascii",
            )

            loaded = load_parameter_template(template)["workflow"]

        self.assertEqual(2.9, loaded["automatic_rt_correction_minimum_anchors"])
        issues = validate_workflow(
            {
                **loaded,
                "project_type": "lcms",
                "together_with_alignment": True,
                "execute_automatic_rt_correction": True,
            }
        )
        messages = [item["message"] for item in issues if item["level"] == "error"]
        self.assertTrue(any("minimum anchors must be a whole number" in text for text in messages), messages)
        self.assertTrue(any("maximum anchors must be a finite number" in text for text in messages), messages)

    def test_a_numeric_blank_file_type_counts_as_a_blank(self) -> None:
        # The Console parses the file type as an enum and accepts its number; Blank is 3.
        from msdial_app.workflow import is_blank_file_type

        for value in ("Blank", " blank ", "3", "+03", 3):
            with self.subTest(value=value):
                self.assertTrue(is_blank_file_type(value))
        for value in ("Sample", "QC", "Solvent Blank", "0", "", None, "0_3", "\uff13"):
            with self.subTest(value=value):
                self.assertFalse(is_blank_file_type(value))

    def test_automatic_rt_defaults_are_one_table(self) -> None:
        from msdial_app.workflow import (
            AUTOMATIC_RT_CORRECTION_DEFAULTS,
            AUTOMATIC_RT_CORRECTION_METHOD_KEYS,
        )

        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "method.txt"
            template.write_text("Ion mode: Positive\n", encoding="ascii")
            loaded = load_parameter_template(template)["workflow"]
            method = Path(temporary) / "written.txt"
            _write_method(
                method,
                {
                    "project_type": "lcms",
                    "template_path": str(template),
                    "target_omics": "Metabolomics",
                    "execute_automatic_rt_correction": True,
                },
            )
            written = {
                line.split(":", 1)[0].strip().casefold(): line.split(":", 1)[1].strip()
                for line in method.read_text(encoding="utf-8").splitlines()
                if ":" in line
            }

        for key, default in AUTOMATIC_RT_CORRECTION_DEFAULTS.items():
            with self.subTest(key=key):
                self.assertEqual(default, loaded[key])
                label = key.replace("automatic_rt_correction_", "automatic rt correction ").replace("_", " ")
                self.assertIn(label, AUTOMATIC_RT_CORRECTION_METHOD_KEYS)
                self.assertEqual(str(default).casefold(), written[label].casefold())

    def test_reads_adduct_resources(self) -> None:
        resource = (
            Path(__file__).parents[1]
            / "resources"
            / "AdductIonResource_Negative.txt"
        )
        adducts = read_adducts(resource, "Negative")

        self.assertEqual("[M-H]-", adducts[0]["adduct"])
        self.assertTrue(adducts[0]["selected"])

    def test_method_writes_selected_adducts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.mzML"
            raw.write_bytes(b"")
            template = root / "method.txt"
            template.write_text(
                "Adduct list: [M+H]+\nIon mode: Positive\nTarget omics: Metabolomics\n",
                encoding="utf-8",
            )
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"")
            state = {
                "files": expand_paths([str(raw)]),
                "project_type": "lcms",
                "console_path": str(console),
                "template_path": str(template),
                "output_root": str(root / "output"),
                "ion_mode": "Positive",
                "target_omics": "Metabolomics",
                "selected_adducts": ["[M+H]+", "[M+Na]+"],
            }

            prepared = prepare_run(state)
            method = Path(prepared["method_file"]).read_text(encoding="utf-8")

            self.assertIn("Searched adduct ions: [M+H]+,[M+Na]+", method)

    def test_non_lcms_gcms_project_is_not_executable_yet(self) -> None:
        issues = validate_workflow(
            {
                "project_type": "dims",
                "files": [],
                "console_path": "",
                "template_path": "",
                "output_root": "",
                "target_omics": "Metabolomics",
            }
        )
        self.assertTrue(any("does not execute this project type yet" in issue["message"] for issue in issues))

    def test_agilent_validation_explains_reader_prerequisites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agilent = root / "sample.d"
            (agilent / "AcqData").mkdir(parents=True)
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"")
            template = root / "method.txt"
            template.write_text("# template\n", encoding="utf-8")
            files = expand_paths([str(agilent)])

            issues = validate_workflow(
                {
                    "files": files,
                    "console_path": str(console),
                    "template_path": str(template),
                    "output_root": str(root / "output"),
                    "target_omics": "Metabolomics",
                }
            )
            messages = "\n".join(issue["message"] for issue in issues)
            self.assertIn("Visual C++ 2013 Redistributable Package x64", messages)
            self.assertIn("BaseDataAccess.dll", messages)

    def test_distribution_file_list_excludes_work_outputs(self) -> None:
        script = Path(__file__).parents[1] / "scripts" / "build-distribution.py"
        spec = importlib.util.spec_from_file_location("build_distribution", script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        names = {str(path.relative_to(module.ROOT)).replace("\\", "/") for path in module.iter_files()}

        self.assertIn("scripts/start-local-windows.ps1", names)
        self.assertIn("scripts/start-local-windows.cmd", names)
        self.assertIn("scripts/start-local-linux.sh", names)
        self.assertIn("scripts/start-local-macos.command", names)
        self.assertIn("docs/local_user_tutorial_ja.md", names)
        self.assertFalse(any(name.startswith("runs/") for name in names))
        self.assertFalse(any(name.startswith("work/") for name in names))


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
import zipfile
import importlib.util
import os
from pathlib import Path

from msdial_app.agent_bridge import create_datamining_handoff, summarize_jobs
from msdial_app.mztab_preview import preview_mztab_file, preview_mztab_outputs
from msdial_app.mztab_validation import validate_mztab_file, validate_mztab_outputs
from msdial_app.workflow import (
    build_console_command,
    detect_raw_format,
    expand_paths,
    expand_paths_report,
    parse_mdpeak,
    parse_mdscan,
    prepare_run,
    prepare_tuning_run,
    read_adducts,
    read_lipid_queries,
    recommended_peak_parameters,
    validate_workflow,
)


class WorkflowTests(unittest.TestCase):
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
            console.write_text("")
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
            self.assertIn("create_datamining_handoff", status["capabilities"])

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
            self.assertIn("Retention index alignment tolerance: 12", method)
            self.assertIn("Weighted dot product cutoff: 0.55", method)
            self.assertIn("Minimum spectrum match: 4", method)
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

    def test_folder_type_vendor_detection_and_recommendations(self) -> None:
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
                recommended_peak_parameters(expanded),
            )
            self.assertEqual("Agilent", detect_raw_format(agilent)["vendor"])

    def test_parse_ascii_mdpeak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.mdpeak"
            path.write_text(
                "\t".join(
                    [
                        "Peak ID",
                        "Height",
                        "Simple dot product",
                        "Weighted dot product",
                        "Reverse dot product",
                        "Matched peaks count",
                        "Matched peaks percentage",
                    ]
                )
                + "\n"
                + "0\t50\t0.6\t0.7\t0.8\t4\t0.5\n"
                + "1\t75\t0\t0\t0\t-1\t-1\n"
                + "2\t100\tnull\tnull\tnull\tnull\tnull\n",
                encoding="utf-8",
            )
            result = parse_mdpeak(path)
            self.assertEqual([50.0, 75.0, 100.0], result["heights"])
            self.assertEqual(2, result["msp_candidate_count"])
            self.assertEqual(1, result["msp_scored_count"])
            self.assertEqual(0.7, result["msp_scores"][0]["weighted"])

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
            self.assertEqual(2, result["msp_candidate_count"])
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
                    "stage_inputs": True,
                }
            )

            run_directory = Path(prepared["run_directory"])
            self.assertFalse((run_directory / "input").exists())
            csv_text = Path(prepared["input_csv"]).read_text(encoding="ascii")
            self.assertIn(str(wiff.resolve()), csv_text)
            manifest = Path(prepared["manifest"]).read_text(encoding="utf-8")
            self.assertIn('"stage_inputs": false', manifest)

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

import tempfile
import unittest
from pathlib import Path

from msdial_app.workflow import (
    build_console_command,
    detect_raw_format,
    expand_paths,
    expand_paths_report,
    parse_mdpeak,
    prepare_run,
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
                "minimum_peak_height": 4321,
                "mass_slice_width": 0.05,
                "msp_weighted_dot_product": 0.55,
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
            csv_text = Path(result["input_csv"]).read_text(encoding="ascii")
            method_text = Path(result["method_file"]).read_text(encoding="utf-8")
            self.assertIn(",SWATH,", csv_text)
            self.assertIn("Ion mode: Negative", method_text)
            self.assertIn("MS1 data type: Profile", method_text)
            self.assertIn("Minimum peak height: 4321", method_text)
            self.assertIn("Mass slice width: 0.05", method_text)
            self.assertIn(
                "Weighted dot product cutoff for MSP-based annotation: 0.55",
                method_text,
            )
            self.assertIn("Searched lipid class: PC [M+CH3COO]-", method_text)
            self.assertLess(
                method_text.index("Searched lipid class:"),
                method_text.index("Solvent type:"),
            )

    def test_console_dll_uses_dotnet(self) -> None:
        command = build_console_command("MSDIALCUI.dll", "a.csv", "out", "method.txt")
        self.assertEqual("dotnet", command[0])
        self.assertEqual("-p", command[-1])

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
                + "1\t100\tnull\tnull\tnull\tnull\tnull\n",
                encoding="utf-8",
            )
            result = parse_mdpeak(path)
            self.assertEqual([50.0, 100.0], result["heights"])
            self.assertEqual(1, result["msp_candidate_count"])
            self.assertEqual(0.7, result["msp_scores"][0]["weighted"])

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

    def test_staging_wiff_copies_implicit_sidecar(self) -> None:
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

            staged = Path(prepared["run_directory"]) / "input"
            self.assertTrue((staged / wiff.name).exists())
            self.assertTrue((staged / sidecar.name).exists())

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

    def test_non_lcms_project_is_not_executable_yet(self) -> None:
        issues = validate_workflow(
            {
                "project_type": "gcms",
                "files": [],
                "console_path": "",
                "template_path": "",
                "output_root": "",
                "target_omics": "Metabolomics",
            }
        )
        self.assertTrue(any("executes LC-MS workflows only" in issue["message"] for issue in issues))

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


if __name__ == "__main__":
    unittest.main()

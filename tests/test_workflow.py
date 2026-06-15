import tempfile
import unittest
from pathlib import Path

from msdial_app.workflow import (
    build_console_command,
    detect_raw_format,
    expand_paths,
    parse_mdpeak,
    prepare_run,
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

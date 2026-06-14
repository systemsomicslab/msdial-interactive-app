import tempfile
import unittest
from pathlib import Path

from msdial_app.workflow import (
    build_console_command,
    expand_paths,
    prepare_run,
    read_lipid_queries,
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


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app.agent_workflow import (
    build_guided_plan,
    inspect_analysis_input,
    estimate_peak_height,
)
from msdial_app.worksets import get_workset, list_worksets, save_workset


ROOT = Path(__file__).resolve().parent.parent


class AgentWorkflowTests(unittest.TestCase):
    def test_guided_plan_inspects_input_and_builds_ready_lcms_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.mzML"
            raw.write_text("", encoding="ascii")
            console = root / "MSDIALCUI.exe"
            console.write_text("", encoding="ascii")
            inspection = inspect_analysis_input(str(root))

            plan = build_guided_plan(
                str(root),
                {
                    "project_type": "lcms",
                    "ion_mode": "Negative",
                    "target_omics": "Metabolomics",
                    "parameter_strategy": "default",
                    "execute_rt_correction": False,
                    "library_strategy": "none",
                    "run_qa": False,
                    "generate_materials_methods": True,
                    "console_path": str(console),
                    "template_path": str(
                        ROOT / "resources" / "msdial_console_param4lipidomics.txt"
                    ),
                },
            )

            self.assertEqual(1, inspection["file_count"])
            self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
            self.assertEqual("lcms", plan["workflow"]["project_type"])
            self.assertEqual("Negative", plan["workflow"]["ion_mode"])
            self.assertEqual([], plan["workflow"]["msp_annotators"])
            self.assertTrue(plan["workflow"]["selected_adducts"])

    def test_guided_plan_returns_first_scientific_question(self) -> None:
        plan = build_guided_plan("", {})

        self.assertEqual("project_type", plan["next_question"]["id"])
        self.assertEqual("neutral", plan["next_question"]["presentation"])
        self.assertFalse(plan["ready_to_prepare"])

    def test_target_peak_count_requires_diagnostic_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sample.mzML").write_text("", encoding="ascii")
            plan = build_guided_plan(
                str(root),
                {
                    "project_type": "lcms",
                    "ion_mode": "Positive",
                    "target_omics": "Metabolomics",
                    "parameter_strategy": "target_peak_count",
                    "target_peak_count": 1000,
                    "execute_rt_correction": False,
                    "library_strategy": "none",
                    "run_qa": False,
                    "generate_materials_methods": False,
                },
            )

            self.assertTrue(plan["requires_diagnostic"])
            self.assertTrue(any("diagnostic" in item for item in plan["blockers"]))

    def test_string_false_answers_remain_false(self) -> None:
        plan = build_guided_plan(
            "",
            {
                "project_type": "lcms",
                "ion_mode": "Negative",
                "target_omics": "Metabolomics",
                "parameter_strategy": "default",
                "execute_rt_correction": "false",
                "library_strategy": "none",
                "run_qa": "false",
                "generate_materials_methods": "false",
            },
        )

        self.assertFalse(plan["post_run_actions"]["quality_assurance"])
        self.assertFalse(plan["post_run_actions"]["materials_and_methods"])

    def test_peak_height_estimate_uses_target_order_statistic(self) -> None:
        result = estimate_peak_height([1, 2, 3, 4, 5], 2)

        self.assertEqual(4, result["minimum_peak_height"])
        self.assertEqual(2, result["estimated_peak_count"])

    def test_builtin_and_user_worksets_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}
        ):
            saved = save_workset(
                "My negative lipidomics",
                {
                    "project_type": "lcms",
                    "ion_mode": "Negative",
                    "target_omics": "Lipidomics",
                    "minimum_peak_height": 500,
                    "input_path": "D:/raw",
                },
            )

            self.assertGreaterEqual(len(list_worksets()), 6)
            self.assertIsNotNone(get_workset("lcms-negative-lipidomics"))
            self.assertEqual("Negative", get_workset(saved["id"])["answers"]["ion_mode"])
            self.assertEqual(500, get_workset(saved["id"])["answers"]["minimum_peak_height"])
            self.assertNotIn("input_path", get_workset(saved["id"])["answers"])


if __name__ == "__main__":
    unittest.main()

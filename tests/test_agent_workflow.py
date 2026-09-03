import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app.agent_workflow import (
    build_guided_plan,
    inspect_analysis_input,
    estimate_peak_height,
    estimate_peak_height_range,
    select_peak_tuning_representative,
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
                    "smoothing_method": "TimeBasedLinearWeightedMovingAverage",
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
            self.assertEqual(
                "TimeBasedLinearWeightedMovingAverage",
                plan["workflow"]["smoothing_method"],
            )
            self.assertEqual([], plan["workflow"]["msp_annotators"])
            self.assertTrue(plan["workflow"]["selected_adducts"])

    def test_guided_plan_returns_first_scientific_question(self) -> None:
        plan = build_guided_plan("", {})

        self.assertEqual("project_type", plan["next_question"]["id"])
        self.assertEqual("neutral", plan["next_question"]["presentation"])
        self.assertFalse(plan["ready_to_prepare"])

    def test_guided_plan_loads_repository_metadata_from_local_review_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "sample.mzML"
            raw.write_text("", encoding="ascii")
            console = root / "MSDIALCUI.exe"
            console.write_text("", encoding="ascii")
            metadata = root / "MPST000007_repository_metadata_reviewed.json"
            metadata.write_text(
                """{
  "schema": "msdial-repository-metadata.v1",
  "repository": "mb_post",
  "accession": "MPST000007",
  "fields": [],
  "rows": [],
  "hierarchy": []
}""",
                encoding="utf-8",
            )

            plan = build_guided_plan(
                str(root),
                {
                    "project_type": "lcms",
                    "ion_mode": "Negative",
                    "target_omics": "Lipidomics",
                    "parameter_strategy": "default",
                    "execute_rt_correction": False,
                    "library_strategy": "none",
                    "run_qa": False,
                    "generate_materials_methods": True,
                    "console_path": str(console),
                    "repository_metadata_path": str(metadata),
                },
            )

            self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
            self.assertEqual("MPST000007", plan["workflow"]["repository_metadata"]["accession"])
            self.assertEqual(str(metadata.resolve()), plan["workflow"]["repository_metadata_source_path"])

    def test_missing_persisted_resource_paths_fall_back_to_bundled_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sample.cdf").write_text("", encoding="ascii")
            console = root / "MSDIALCUI.exe"
            console.write_text("", encoding="ascii")
            with patch(
                "msdial_app.agent_workflow.load_user_settings",
                return_value={
                    "template_path": str(root / "missing-template.txt"),
                    "queries_path": str(root / "missing-queries.txt"),
                },
            ):
                plan = build_guided_plan(
                    str(root),
                    {
                        "project_type": "gcms",
                        "parameter_strategy": "default",
                        "gcms_retention_type": "RT",
                        "library_strategy": "none",
                        "generate_materials_methods": False,
                        "console_path": str(console),
                    },
                )
            self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
            self.assertEqual(
                (ROOT / "resources" / "gcms_console_param_kovats.txt").resolve(),
                Path(plan["workflow"]["template_path"]),
            )
            self.assertEqual(
                {"None"},
                {item["acquisition_type"] for item in plan["workflow"]["files"]},
            )

    def test_unknown_answer_key_is_reported(self) -> None:
        plan = build_guided_plan("", {"export_folder_path_typo": "D:/ignored"})

        self.assertEqual(["export_folder_path_typo"], plan["unknown_answer_keys"])
        self.assertTrue(any("export_folder_path_typo" in item for item in plan["warnings"]))

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

    def test_lcms_qa_sets_height_matrix_export_to_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sample.mzML").write_text("", encoding="ascii")
            console = root / "MSDIALCUI.exe"
            console.write_text("", encoding="ascii")
            plan = build_guided_plan(
                str(root),
                {
                    "project_type": "lcms",
                    "ion_mode": "Negative",
                    "target_omics": "Metabolomics",
                    "parameter_strategy": "default",
                    "execute_rt_correction": False,
                    "library_strategy": "none",
                    "run_qa": True,
                    "generate_materials_methods": True,
                    "console_path": str(console),
                    "template_path": str(ROOT / "resources" / "msdial_console_param4lipidomics.txt"),
                },
            )

            self.assertTrue(plan["workflow"]["height_matrix_export"])
            self.assertEqual(root.resolve(), Path(plan["workflow"]["export_folder_path"]).resolve())

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

    def test_peak_height_range_estimate_respects_instrument_step(self) -> None:
        heights = list(range(1, 10001))

        qtof = estimate_peak_height_range(heights, 3000, 6000, 100)
        ft = estimate_peak_height_range(heights, 3000, 6000, 1000)

        self.assertEqual(0, qtof["minimum_peak_height"] % 100)
        self.assertEqual(0, ft["minimum_peak_height"] % 1000)
        self.assertTrue(qtof["within_target_range"])
        self.assertTrue(ft["within_target_range"])

    def test_peak_height_range_keeps_zero_when_diagnostic_is_below_upper_bound(self) -> None:
        result = estimate_peak_height_range(list(range(2500)), 3000, 6000, 100)

        self.assertEqual(0, result["minimum_peak_height"])
        self.assertEqual(2500, result["estimated_peak_count"])
        self.assertFalse(result["within_target_range"])

    def test_peak_tuning_representative_prefers_midrun_qc_and_ft_step(self) -> None:
        files = [
            {
                "file_path": "D:/sample-1.raw",
                "file_name": "sample-1",
                "file_type": "Sample",
                "analytical_order": 1,
                "instrument_family": "Fourier-transform MS",
            },
            {
                "file_path": "D:/qc-1.raw",
                "file_name": "qc-1",
                "file_type": "QC",
                "analytical_order": 5,
                "instrument_family": "Fourier-transform MS",
            },
            {
                "file_path": "D:/qc-2.raw",
                "file_name": "qc-2",
                "file_type": "QC",
                "analytical_order": 9,
                "instrument_family": "Fourier-transform MS",
            },
        ]

        result = select_peak_tuning_representative(files)

        self.assertEqual("D:/qc-1.raw", result["file_path"])
        self.assertEqual(1000, result["threshold_step"])
        self.assertEqual("QC-nearest-run-midpoint", result["selection_reason"])

    def test_zero_peak_height_is_an_accepted_auto_tuning_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sample.mzML").write_text("", encoding="ascii")
            plan = build_guided_plan(
                str(root),
                {
                    "project_type": "lcms",
                    "ion_mode": "Negative",
                    "target_omics": "Metabolomics",
                    "parameter_strategy": "auto_peak_range",
                    "minimum_peak_height": 0,
                    "execute_rt_correction": False,
                    "library_strategy": "none",
                    "run_qa": False,
                    "generate_materials_methods": False,
                },
            )

        self.assertFalse(plan["requires_diagnostic"])
        self.assertFalse(any("Peak-count tuning" in item for item in plan["blockers"]))

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

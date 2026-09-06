import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app.agent_workflow import build_guided_plan
from msdial_app.annotation_pipeline import (
    TIERED_LCMS_PROFILE_ID,
    apply_tiered_lcms_annotation,
    tiered_lcms_annotation,
)
from msdial_app.workflow import _write_msp_annotator_settings


ROOT = Path(__file__).resolve().parent.parent


class AnnotationPipelineTests(unittest.TestCase):
    def test_tiered_profile_has_auditable_priority_and_thresholds(self) -> None:
        profile = tiered_lcms_annotation("lipids.lbm2", "private.msp")

        self.assertEqual(TIERED_LCMS_PROFILE_ID, profile["profile_id"])
        self.assertEqual(3, profile["lbm_annotator"]["priority"])
        high, low = profile["msp_annotators"]
        self.assertEqual(("msp_high_quality", 2, 0.05, 0.8, 3), (
            high["annotator_id"], high["priority"], high["ms2_tolerance"],
            high["reverse_dot_product_cutoff"], high["minimum_spectrum_match"],
        ))
        self.assertEqual(("msp_low_quality", 1, 0.25, 0.5, 1), (
            low["annotator_id"], low["priority"], low["ms2_tolerance"],
            low["reverse_dot_product_cutoff"], low["minimum_spectrum_match"],
        ))
        self.assertTrue(all(row["target_omics"] == "Metabolomics" for row in (high, low)))
        self.assertTrue(all(row["matched_peaks_percentage_cutoff"] == 0 for row in (high, low)))

    def test_settings_tsv_preserves_mode_tier_and_reuses_one_msp_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            msp = root / "private.msp"
            msp.write_text("", encoding="ascii")
            state = {"project_type": "lcms", "lbm_annotator": {}}
            apply_tiered_lcms_annotation(state, str(root / "lipids.lbm2"), str(msp))

            path = _write_msp_annotator_settings(root, state)
            with path.open(encoding="ascii", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))

            self.assertEqual(2, len(rows))
            self.assertEqual(rows[0]["msp_file_path"], rows[1]["msp_file_path"])
            self.assertEqual("Metabolomics", rows[0]["target_omics"])
            self.assertEqual("MSP low quality candidate", rows[1]["evidence_tier"])

    def test_agent_can_build_tiered_pipeline_for_metabolomics_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sample.mzML").write_text("", encoding="ascii")
            console = root / "MSDIALCUI.exe"
            console.write_text("", encoding="ascii")
            msp = root / "private-neg-vs20.msp"
            msp.write_text("", encoding="ascii")
            lbm = root / "lipids.lbm2"
            lbm.write_text("", encoding="ascii")
            catalog = [{
                "id": "lipidomics",
                "kind": "lbm",
                "downloaded": True,
                "local_path": str(lbm),
                "record_id": 21904324,
                "record_url": "https://zenodo.org/records/21904324",
                "doi": "10.5281/zenodo.21904324",
                "license": "CC BY 4.0",
            }]

            with patch("msdial_app.agent_workflow.catalog_status", return_value=catalog):
                plan = build_guided_plan(
                    str(root),
                    {
                        "project_type": "lcms",
                        "ion_mode": "Negative",
                        "target_omics": "Metabolomics",
                        "parameter_strategy": "default",
                        "execute_rt_correction": False,
                        "library_strategy": "tiered_lipid_msp",
                        "libraries": {"msp_paths": [str(msp)], "msp_version": "VS20"},
                        "run_qa": False,
                        "generate_materials_methods": False,
                        "console_path": str(console),
                        "template_path": str(ROOT / "resources" / "msdial_console_param4lipidomics.txt"),
                    },
                )

            self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
            workflow = plan["workflow"]
            self.assertEqual(TIERED_LCMS_PROFILE_ID, workflow["annotation_pipeline_profile"])
            self.assertEqual(2, len(workflow["msp_annotators"]))
            self.assertTrue(workflow["selected_lipids"])
            self.assertEqual(3, workflow["lbm_annotator"]["priority"])


if __name__ == "__main__":
    unittest.main()

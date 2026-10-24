import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app import __version__
from msdial_app.agent_workflow import build_guided_plan
from msdial_app.annotation_pipeline import (
    TIERED_LCMS_PROFILE_ID,
    apply_tiered_lcms_annotation,
    tiered_lcms_annotation,
)
from msdial_app.materials_methods import generate_publication_report
from msdial_app.sharing import library_records
from msdial_app.workflow import _write_msp_annotator_settings, prepare_run


ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "resources" / "msdial_console_param4lipidomics.txt"


def method_lines(path: str | Path) -> dict[str, str]:
    lines: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            lines[key.strip()] = value.strip()
    return lines


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
                        "class_assignment_confirmed": True,
                    },
                )

            self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
            workflow = plan["workflow"]
            self.assertEqual(TIERED_LCMS_PROFILE_ID, workflow["annotation_pipeline_profile"])
            self.assertEqual(2, len(workflow["msp_annotators"]))
            self.assertTrue(workflow["selected_lipids"])
            self.assertEqual(3, workflow["lbm_annotator"]["priority"])
            # Without libraries.lbm_path, the official catalog LBM2 is the lipid-rule tier and its record.
            self.assertEqual("lipidomics", plan["official_library"]["id"])
            self.assertEqual(str(lbm), workflow["lbm_path"])
            self.assertEqual(str(lbm), workflow["lbm_annotator"]["lbm_file_path"])
            self.assertEqual(
                {
                    "path": str(lbm),
                    "version": "21904324",
                    "source": "https://zenodo.org/records/21904324",
                    "doi": "10.5281/zenodo.21904324",
                    "license": "CC BY 4.0",
                },
                workflow["library_provenance"][0],
            )
            self.assertEqual(str(msp), workflow["library_provenance"][1]["path"])


class GivenLbmPathTests(unittest.TestCase):
    """The tiered profile loads the LBM2 its answers name, and every record names the one it loads.

    Until 0.5.47 the profile loaded the official catalog LBM2 whatever libraries.lbm_path said, while
    the answers, and a campaign's records built from them, named the given one.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "sample.mzML").write_text("", encoding="ascii")
        self.libraries = self.root / "libraries"
        self.libraries.mkdir()
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_text("", encoding="ascii")
        self.msp = self.libraries / "synthetic-neg.msp"
        self.msp.write_text("NAME: A\n", encoding="ascii")
        self.lbm = self.libraries / "synthetic-lab-lipids.lbm2"
        self.lbm.write_bytes(b"synthetic laboratory LBM2 library")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def answers(self, **libraries) -> dict:
        return {
            "project_type": "lcms", "ion_mode": "Negative", "target_omics": "Lipidomics",
            "parameter_strategy": "default", "execute_rt_correction": False,
            "library_strategy": "tiered_lipid_msp",
            "libraries": {"lbm_path": str(self.lbm), "msp_paths": [str(self.msp)], "msp_version": "VS-test",
                          **libraries},
            "run_qa": False, "generate_materials_methods": False, "class_assignment_confirmed": True,
            "console_path": str(self.console), "template_path": str(TEMPLATE),
            "output_root": str(self.root / "out"),
        }

    def plan(self, answers: dict) -> dict:
        # The catalog is the official LBM2's; a plan with an LBM2 of its own never asks it.
        with patch("msdial_app.agent_workflow.catalog_status", side_effect=AssertionError("catalog consulted")):
            return build_guided_plan(str(self.data), answers)

    def test_the_given_lbm2_reaches_the_method_file_and_every_record_by_its_sha256(self) -> None:
        digest = hashlib.sha256(self.lbm.read_bytes()).hexdigest()
        plan = self.plan(self.answers(lbm_version="synthetic-1"))

        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertIsNone(plan["official_library"])
        workflow = plan["workflow"]
        self.assertEqual(str(self.lbm), workflow["lbm_path"])
        self.assertEqual(str(self.lbm), workflow["lbm_annotator"]["lbm_file_path"])
        self.assertEqual(3, workflow["lbm_annotator"]["priority"])
        self.assertEqual(
            {
                "path": str(self.lbm), "name": self.lbm.name, "size_bytes": self.lbm.stat().st_size,
                "sha256": digest, "version": "synthetic-1", "source": "", "doi": "",
                "license": "institutional/private",
            },
            workflow["library_provenance"][0],
        )
        self.assertEqual([str(self.msp)] * 2, [row["msp_file_path"] for row in workflow["msp_annotators"]])

        prepared = prepare_run(workflow)
        self.assertEqual(str(self.lbm), method_lines(prepared["method_file"])["Lbm file path"])
        settings = json.loads(Path(prepared["settings_file"]).read_text(encoding="utf-8"))
        self.assertEqual(str(self.lbm), settings["lbm_path"])
        self.assertEqual(digest, settings["library_provenance"][0]["sha256"])
        manifest = json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(
            [(str(self.lbm), digest)],
            [(item["path"], item["sha256"]) for item in manifest["libraries"] if item["path"].endswith(".lbm2")],
        )
        # The identities a repository unit's run manifest, its finalisation and its reports name libraries by.
        identities = [library.identity for library in library_records(workflow)]
        self.assertEqual(
            [(self.lbm.name, digest, "private")],
            [(item["name"], item["sha256"], item["distribution"]) for item in identities if item["role"] == "lbm"],
        )
        report = generate_publication_report(
            workflow, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )
        self.assertIn("one LBM annotator", report["methods_text"])
        self.assertIn(f"{self.lbm.name} (SHA-256 {digest})", report["methods_text"])
        recorded = json.dumps([workflow, settings, manifest, identities]) + report["methods_text"]
        self.assertNotIn("zenodo", recorded.casefold())

    def test_an_lbm_path_the_console_could_not_load_is_a_blocker(self) -> None:
        missing = self.libraries / "missing.lbm2"
        empty = self.libraries / "empty.lbm2"
        empty.write_bytes(b"")
        other = self.libraries / "lipids.msp"
        other.write_text("NAME: A\n", encoding="ascii")
        folder = self.libraries / "folder.lbm2"
        folder.mkdir()
        for path, message in (
            (missing, f"LBM library not found: {missing}"),
            (empty, "libraries.lbm_path: empty.lbm2 is empty"),
            (other, "libraries.lbm_path: The tiered_lipid_msp profile reads an .lbm2 library; lipids.msp is not one."),
            (folder, "libraries.lbm_path: folder.lbm2 is a folder"),
        ):
            with self.subTest(path=path.name):
                plan = self.plan(self.answers(lbm_path=str(path)))
                self.assertFalse(plan["ready_to_prepare"])
                self.assertIsNone(plan["official_library"])
                self.assertTrue(any(message in item for item in plan["blockers"]), plan["blockers"])

    def test_a_second_msp_path_is_refused_rather_than_left_out(self) -> None:
        second = self.libraries / "second.msp"
        second.write_text("NAME: B\n", encoding="ascii")
        plan = self.plan(self.answers(msp_paths=[str(self.msp), str(second)]))

        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(
            any("libraries.msp_paths names 2" in item for item in plan["blockers"]), plan["blockers"]
        )

    def test_without_an_lbm_path_the_official_lbm2_is_still_asked_for(self) -> None:
        answers = self.answers()
        del answers["libraries"]["lbm_path"]
        missing = [{
            "id": "lipidomics", "kind": "lbm", "downloaded": False, "local_path": "", "record_id": 21904324,
            "record_url": "https://zenodo.org/records/21904324", "doi": "10.5281/zenodo.21904324",
            "license": "CC BY 4.0",
        }]
        with patch("msdial_app.agent_workflow.catalog_status", return_value=missing):
            plan = build_guided_plan(str(self.data), answers)
            unanswered = build_guided_plan(str(self.data), {**answers, "libraries": {}})

        self.assertFalse(plan["ready_to_prepare"])
        self.assertEqual("lipidomics", plan["official_library"]["id"])
        self.assertIn(
            "The official lipidomics LBM library is not downloaded. Ask for confirmation, then download it.",
            plan["blockers"],
        )
        question = next(item for item in unanswered["remaining_questions"] if item["id"] == "libraries")
        self.assertIn("libraries.lbm_path", question["prompt"])
        self.assertNotIn("The official LBM library is used as tier 1", question["prompt"])


if __name__ == "__main__":
    unittest.main()

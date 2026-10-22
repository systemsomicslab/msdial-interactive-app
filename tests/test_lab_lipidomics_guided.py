"""The guided path's laboratory lipidomics profile: what it asks, and what reaches the method file.

LC-MS lipidomics with the laboratory's own libraries, or a plan built from a laboratory lipidomics
contract, must be told the salt, the LBM2, the annotation RT tolerance, where to write and what the
analysis is for; each of them otherwise changes the result without a word. The internal standards
are searched in the same run as a Text-DB annotator with retention time on, and the annotation
candidate table is exported, which the resolver and the merge read.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab_batch_fixture import EVERY_CAPABILITY, Batch, capabilities_patches

from msdial_app.agent_workflow import apply_sample_table, build_guided_plan
from msdial_app.workflow import (
    NORMALIZE_PEAK_IDS_ONLY_CAPABILITY,
    prepare_run,
    prepare_tuning_run,
    validate_workflow,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "resources" / "msdial_console_param4lipidomics.txt"


def method_lines(path: str | Path) -> dict[str, str]:
    lines: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            lines[key.strip()] = value.strip()
    return lines


class LaboratoryProfileQuestions(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        for name in ("S1_SPF_Pos.mzML", "S2_GF_Pos.mzML", "Blank_Pos.mzML"):
            (self.root / name).write_text("", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_bytes(b"not a console")
        self.lbm = self.root / "lab.lbm2"
        self.lbm.write_bytes(b"laboratory library")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def answers(self, **extra) -> dict:
        answers = {
            "project_type": "lcms", "ion_mode": "Negative", "target_omics": "Lipidomics",
            "parameter_strategy": "default", "execute_rt_correction": False,
            "library_strategy": "existing", "libraries": {"lbm_path": str(self.lbm)},
            "use_retention_time_for_annotation": True, "retention_time_tolerance": 1.5,
            "solvent_type": "ammonium acetate", "analysis_purpose": "SPF versus GF",
            "output_root": str(self.root / "out"), "run_qa": False, "generate_materials_methods": False,
            "console_path": str(self.console), "template_path": str(TEMPLATE),
            "class_assignment_confirmed": True,
        }
        answers.update(extra)
        return {key: value for key, value in answers.items() if value is not None}

    def required(self, plan: dict) -> list[str]:
        return [item["id"] for item in plan["remaining_questions"] if item["required"]]

    def test_each_laboratory_decision_is_a_required_question(self) -> None:
        for key, question in (
            ("analysis_purpose", "analysis_purpose"), ("solvent_type", "solvent_type"),
            ("output_root", "output_root"), ("use_retention_time_for_annotation", "use_retention_time_for_annotation"),
            ("retention_time_tolerance", "retention_time_tolerance"),
        ):
            with self.subTest(key=key):
                plan = build_guided_plan(str(self.root), self.answers(**{key: None}))
                self.assertIn(question, self.required(plan))
                self.assertFalse(plan["ready_to_prepare"])
                self.assertEqual("lab_lipidomics", plan["profile"])
        plan = build_guided_plan(str(self.root), self.answers(libraries={"msp_paths": [str(self.lbm)]}))
        self.assertIn("libraries", self.required(plan), "an MSP alone does not answer the LBM2 question")

    def test_a_complete_laboratory_plan_is_ready_and_states_the_salt(self) -> None:
        plan = build_guided_plan(str(self.root), self.answers())
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertEqual("CH3COONH4", plan["workflow"]["solvent"])
        self.assertEqual("SPF versus GF", plan["workflow"]["analysis_purpose"])

    def test_metabolomics_and_repository_runs_are_not_the_laboratory_profile(self) -> None:
        metabolomics = build_guided_plan(
            str(self.root),
            self.answers(target_omics="Metabolomics", solvent_type=None, analysis_purpose=None, output_root=None),
        )
        self.assertEqual("", metabolomics["profile"])
        self.assertNotIn("solvent_type", self.required(metabolomics))
        repository = build_guided_plan(
            str(self.root),
            self.answers(solvent_type=None, analysis_purpose=None,
                         workflow_overrides={"repository_run_manifest": str(self.root / "run-manifest.json")}),
        )
        self.assertNotIn("solvent_type", self.required(repository))
        self.assertNotIn("analysis_purpose", self.required(repository))

    def test_a_salt_spelled_any_way_reaches_the_console_once_and_after_the_lipid_classes(self) -> None:
        for answer, salt in (("酢酸アンモニウム", "CH3COONH4"), ("ammonium formate", "HCOONH4"),
                             ("NH4HCO3", "NH4HCO3")):
            with self.subTest(answer=answer):
                plan = build_guided_plan(str(self.root), self.answers(solvent_type=answer))
                prepared = prepare_run(plan["workflow"])
                text = Path(prepared["method_file"]).read_text(encoding="utf-8").splitlines()
                solvent = [line for line in text if line.startswith("Solvent type")]
                self.assertEqual([f"Solvent type: {salt}"], solvent)
                searched = next(index for index, line in enumerate(text) if line.startswith("Searched lipid class"))
                self.assertGreater(text.index(solvent[0]), searched)

    def test_a_salt_the_console_cannot_read_is_refused(self) -> None:
        plan = build_guided_plan(str(self.root), self.answers(solvent_type="ammonium acetate and formic acid"))
        self.assertTrue(any("solvent_type" in item for item in plan["blockers"]), plan["blockers"])
        issues = validate_workflow({"project_type": "lcms", "solvent": "ch3coonh4", "files": []})
        self.assertTrue(any("Solvent type 'ch3coonh4'" in item["message"] for item in issues))

    def test_the_laboratory_lbm2_must_be_an_lbm2_with_content(self) -> None:
        empty = self.root / "empty.lbm2"
        empty.write_bytes(b"")
        msp = self.root / "lab.msp"
        msp.write_text("NAME: x\n", encoding="ascii")
        for path, message in ((empty, "is empty"), (msp, "not one")):
            with self.subTest(path=path.name):
                plan = build_guided_plan(str(self.root), self.answers(libraries={"lbm_path": str(path)}))
                self.assertTrue(any(message in item for item in plan["blockers"]), plan["blockers"])

    def test_the_template_lbm_cutoffs_are_written_and_the_default_is_0_1(self) -> None:
        plan = build_guided_plan(str(self.root), self.answers())
        lines = method_lines(prepare_run(plan["workflow"])["method_file"])
        for kind in ("Weighted", "Simple", "Reverse"):
            self.assertEqual("0.1", lines[f"{kind} dot product cutoff for LBM-based annotation"])
        custom = self.root / "template.txt"
        custom.write_text(
            TEMPLATE.read_text(encoding="utf-8").replace(
                "Reverse dot product cutoff for LBM-based annotation: 0.1",
                "Reverse dot product cutoff for LBM-based annotation: 0.25",
            ),
            encoding="utf-8",
        )
        plan = build_guided_plan(str(self.root), self.answers(template_path=str(custom)))
        lines = method_lines(prepare_run(plan["workflow"])["method_file"])
        self.assertEqual("0.25", lines["Reverse dot product cutoff for LBM-based annotation"])
        bare = self.root / "bare.txt"
        bare.write_text(
            "\n".join(line for line in TEMPLATE.read_text(encoding="utf-8").splitlines()
                      if "dot product cutoff for LBM" not in line) + "\n",
            encoding="utf-8",
        )
        plan = build_guided_plan(str(self.root), self.answers(template_path=str(bare)))
        lines = method_lines(prepare_run(plan["workflow"])["method_file"])
        self.assertEqual("0.1", lines["Weighted dot product cutoff for LBM-based annotation"])
        self.assertEqual("0.1", lines["Reverse dot product cutoff for LBM-based annotation"])

    def test_the_analysts_text_libraries_follow_the_retention_time_decision(self) -> None:
        library = self.root / "extra.txt"
        library.write_text("Name\tMZ\tRT\tAdduct\tInChIKey\tFormula\tSMILES\tOntology\nX\t300.1\t2.0\t[M-H]-\t\t\t\tFA\n",
                           encoding="ascii")
        plan = build_guided_plan(
            str(self.root), self.answers(libraries={"lbm_path": str(self.lbm), "text_paths": [str(library)]})
        )
        prepared = prepare_run(plan["workflow"])
        settings = Path(prepared["run_directory"]) / "text_annotator_settings.tsv"
        row = next(csv.DictReader(settings.read_text(encoding="ascii").splitlines(), delimiter="\t"))
        self.assertEqual(("1.5", "True", "True"), (
            row["rt_tolerance"], row["use_retention_information_for_scoring"],
            row["use_retention_information_for_filtering"],
        ))


class SampleTableAnswer(unittest.TestCase):
    FILES = [
        {"file_name": "S1_Pos", "file_path": "S1_Pos.mzML", "file_type": "Sample", "class_id": "S1",
         "analytical_order": 1, "batch_order": 1, "factor": 1.0},
        {"file_name": "S2_Pos", "file_path": "S2_Pos.mzML", "file_type": "Sample", "class_id": "S2",
         "analytical_order": 2, "batch_order": 1, "factor": 1.0},
        {"file_name": "Blank_Pos", "file_path": "Blank_Pos.mzML", "file_type": "Sample", "class_id": "Blank",
         "analytical_order": 3, "batch_order": 1, "factor": 1.0},
    ]

    def test_the_confirmed_table_replaces_what_the_names_suggested(self) -> None:
        applied = apply_sample_table(self.FILES, [
            {"file_name": "S1_Pos", "class_id": "SPF", "factor": 2},
            {"file_name": "S2_Pos", "class_id": "GF", "included": False},
            {"file_name": "blank_pos", "file_type": "blank", "analytical_order": 7},
        ])
        self.assertEqual([], applied["problems"])
        self.assertEqual(["S2_Pos"], applied["excluded"])
        self.assertEqual([("S1_Pos", "SPF", "Sample", 2.0), ("Blank_Pos", "Blank", "Blank", 1.0)], [
            (item["file_name"], item["class_id"], item["file_type"], item["factor"]) for item in applied["files"]
        ])
        self.assertEqual(7, applied["files"][1]["analytical_order"])

    def test_a_table_for_other_files_or_with_unreadable_values_is_refused(self) -> None:
        problems = apply_sample_table(self.FILES, [
            {"file_name": "S1_Pos", "file_type": "Blnak"},
            {"file_name": "S9_Pos"},
            {"file_name": "S2_Pos", "factor": 0, "analytical_order": 1.5, "class_id": "a,b"},
        ])["problems"]
        text = " ".join(problems)
        for fragment in ("'Blnak'", "S9_Pos", "no row for Blank_Pos", "factor '0'", "analytical_order '1.5'",
                         "holds a comma"):
            self.assertIn(fragment, text)

    def test_false_is_not_an_answer_to_the_class_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("S1_Pos.mzML", "S2_Pos.mzML"):
                (root / name).write_text("", encoding="ascii")
            (root / "MSDIALCUI.exe").write_bytes(b"x")
            answers = {
                "project_type": "lcms", "ion_mode": "Positive", "target_omics": "Metabolomics",
                "parameter_strategy": "default", "execute_rt_correction": False, "library_strategy": "none",
                "run_qa": False, "generate_materials_methods": False,
                "console_path": str(root / "MSDIALCUI.exe"), "template_path": str(TEMPLATE),
                "class_assignment_confirmed": False,
            }
            plan = build_guided_plan(str(root), answers)
            self.assertIn("class_assignment_confirmed", [item["id"] for item in plan["remaining_questions"]])
            self.assertIsNone(plan["workflow"])
            table = [{"file_name": "S1_Pos", "class_id": "A"}, {"file_name": "S2_Pos", "class_id": "B"}]
            plan = build_guided_plan(str(root), {**answers, "sample_table": table})
            self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
            self.assertEqual(["A", "B"], [item["class_id"] for item in plan["workflow"]["files"]])
            plan = build_guided_plan(str(root), {**answers, "sample_table": [{"file_name": "S1_Pos"}]})
            self.assertTrue(any("no row for S2_Pos" in item for item in plan["blockers"]))


class PlanFromAContract(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.batch = Batch(Path(self.directory.name))
        self.patches = capabilities_patches()
        self.patches.__enter__()
        self.contract = self.batch.contract()

    def tearDown(self) -> None:
        self.patches.__exit__(None, None, None)
        self.directory.cleanup()

    def plan(self, polarity: str = "Negative", folder: Path | None = None, **extra) -> dict:
        # The answers the skill gives: the contract, the polarity and the accepted threshold, nothing else.
        answers = {"lab_lipidomics_contract_path": self.contract, "ion_mode": polarity,
                   "minimum_peak_height": 500, **extra}
        target = folder or (self.batch.neg if polarity == "Negative" else self.batch.pos)
        return build_guided_plan(str(target), {key: value for key, value in answers.items() if value is not None})

    def test_the_contract_answers_every_laboratory_question(self) -> None:
        plan = self.plan()
        self.assertTrue(plan["ready_to_prepare"], (plan["blockers"], plan["remaining_questions"]))
        workflow = plan["workflow"]
        self.assertEqual("CH3COONH4", workflow["solvent"])
        self.assertEqual((self.batch.analysis / "neg").resolve(), Path(workflow["output_root"]).resolve())
        self.assertEqual(str(self.batch.lbm.resolve()), str(Path(workflow["lbm_path"]).resolve()))
        self.assertTrue(workflow["execute_automatic_rt_correction"])
        self.assertEqual(12, workflow["automatic_rt_correction_maximum_anchors"])
        self.assertEqual(1.5, workflow["automatic_rt_correction_local_support_rt_window"])
        self.assertTrue(workflow["automatic_rt_correction_interpolate_blanks_by_analytical_order"])
        self.assertEqual("TimeBasedLinearWeightedMovingAverage", workflow["smoothing_method"])
        self.assertTrue(workflow["export_annotation_candidates"])
        classes ={item["file_name"]: item["class_id"] for item in workflow["files"]}
        self.assertEqual({"S1_Neg": "SPF", "S2_Neg": "GF", "Blank_Neg": "Blank"}, classes)
        self.assertEqual("Negative", plan["lab_lipidomics"]["polarity"])
        self.assertEqual([], plan["lab_lipidomics"]["deviations"])
        # QA checks the standards of the run's own internal-standard library.
        self.assertEqual(
            {"PC 15:0_18:1(d7)", "LPC 18:1(d7)", "Cer 18:1;O2/15:0(d7)", "FA 18:0(d3)", "FA 16:0(d3)"},
            {item["name"] for item in plan["post_run_actions"]["internal_standards"]},
        )

    def test_the_internal_standards_are_searched_in_the_run_with_retention_time(self) -> None:
        prepared = prepare_run(self.plan()["workflow"])
        settings = Path(prepared["run_directory"]) / "text_annotator_settings.tsv"
        rows = list(csv.DictReader(settings.read_text(encoding="ascii").splitlines(), delimiter="\t"))
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("internal_standards_neg", row["annotator_id"])
        self.assertEqual(("1", "0.5", "0.8", "True", "True"), (
            row["priority"], row["rt_tolerance"], row["total_score_cutoff"],
            row["use_retention_information_for_scoring"], row["use_retention_information_for_filtering"],
        ))
        self.assertEqual(
            (self.batch.analysis / "inputs" / "is_neg.txt").resolve(), Path(row["text_db_file_path"]).resolve()
        )
        lines = method_lines(prepared["method_file"])
        self.assertEqual("True", lines["Export annotation candidates"])
        self.assertEqual("1.5", lines["Automatic RT correction local support RT window"])
        self.assertEqual("12", lines["Automatic RT correction maximum anchors"])
        self.assertEqual("1.5", lines["RT tolerance for LBM-based annotation"])

    def test_the_diagnostic_writes_no_candidate_table(self) -> None:
        workflow = self.plan()["workflow"]
        diagnostic = prepare_tuning_run(workflow, workflow["files"][0]["file_path"], self.batch.root / "diagnostic")
        self.assertNotIn("Export annotation candidates", method_lines(diagnostic["method_file"]))

    def test_what_the_contract_fixes_cannot_be_answered_otherwise(self) -> None:
        plan = self.plan(solvent_type="HCOONH4")
        self.assertTrue(any("solvent_type is fixed by the contract" in item for item in plan["blockers"]))
        self.assertFalse(plan["ready_to_prepare"])
        plan = self.plan(output_root=str(self.batch.root / "elsewhere"))
        self.assertTrue(any("output_root is fixed" in item for item in plan["blockers"]))
        plan = self.plan(folder=self.batch.pos)
        self.assertTrue(any("input_path must be the contract's negative" in item for item in plan["blockers"]))
        plan = self.plan(polarity="")
        self.assertTrue(any("Name the ion mode" in item for item in plan["blockers"]))

    def test_workflow_overrides_cannot_replace_what_the_contract_fixes(self) -> None:
        other = self.batch.root / "lib" / "other.lbm2"
        other.write_bytes(b"another laboratory library")
        plan = self.plan(workflow_overrides={"lbm_path": str(other), "text_annotators": [], "lbm_rt_tolerance": 100})
        self.assertFalse(plan["ready_to_prepare"])
        for key in ("lbm_path", "text_annotators", "lbm_rt_tolerance"):
            self.assertTrue(any(f"workflow_overrides.{key} would replace" in item for item in plan["blockers"]), key)
        self.assertEqual(3, len(plan["diagnostic_blockers"]))
        plan = self.plan(workflow_overrides={"ms1_tolerance": 0.02})
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertEqual([{"key": "workflow_overrides.ms1_tolerance", "contract": None, "given": 0.02}],
                         plan["lab_lipidomics"]["deviations"])

    def test_the_diagnostic_may_start_only_when_nothing_but_the_threshold_is_missing(self) -> None:
        plan = self.plan(minimum_peak_height=None)
        self.assertTrue(plan["requires_diagnostic"])
        self.assertEqual([], plan["diagnostic_blockers"])
        self.assertEqual(["Peak-count tuning requires a diagnostic run and an accepted minimum_peak_height."],
                         plan["blockers"])
        other = self.batch.root / "original" / "neg"
        other.mkdir(parents=True)
        for item in self.batch.neg.iterdir():
            (other / item.name).write_bytes(item.read_bytes())
        plan = self.plan(minimum_peak_height=None, folder=other)
        self.assertIsNotNone(plan["workflow"])
        self.assertTrue(any("input_path must be" in item for item in plan["diagnostic_blockers"]))

    def test_a_contract_whose_output_folders_are_the_raw_folders_is_refused(self) -> None:
        # Written by hand, or by a version that did not check it.
        contract = json.loads(Path(self.contract).read_text(encoding="utf-8"))
        root = self.batch.neg.parent
        contract["analysis_root"] = str(root)
        contract["output_roots"] = {"Positive": str(root / "pos"), "Negative": str(root / "neg")}
        Path(self.contract).write_text(json.dumps(contract), encoding="utf-8")
        plan = self.plan()
        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(any("the negative raw-data folder itself" in item for item in plan["blockers"]), plan["blockers"])

    def test_a_changed_method_default_is_a_reported_deviation(self) -> None:
        plan = self.plan(execute_automatic_rt_correction=False)
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertEqual(
            [{"key": "execute_automatic_rt_correction", "contract": True, "given": False}],
            plan["lab_lipidomics"]["deviations"],
        )
        self.assertFalse(plan["workflow"]["execute_automatic_rt_correction"])

    def test_the_template_and_query_list_are_the_contracts_whatever_else_names_them(self) -> None:
        custom = self.batch.root / "gui_template.txt"
        custom.write_text(
            TEMPLATE.read_text(encoding="utf-8").replace(
                "Weighted dot product cutoff for LBM-based annotation: 0.1",
                "Weighted dot product cutoff for LBM-based annotation: 0.6",
            ),
            encoding="utf-8",
        )
        queries = self.batch.root / "LbmQueries.txt"
        queries.write_bytes((ROOT / "resources" / "LbmQueries.txt").read_bytes())
        for key, value in (("template_path", custom), ("queries_path", queries)):
            with self.subTest(answer=key):
                plan = self.plan(**{key: str(value)})
                self.assertFalse(plan["ready_to_prepare"])
                self.assertTrue(any(f"{key} is fixed by the contract" in item for item in plan["blockers"]), plan["blockers"])
        # A regular workset answers alike, and is judged alike.
        workset = {"id": "w", "kind": "user", "answers": {"template_path": str(custom)}}
        with patch("msdial_app.agent_workflow.get_workset", return_value=workset):
            plan = build_guided_plan(str(self.batch.neg), {"lab_lipidomics_contract_path": self.contract,
                                                           "ion_mode": "Negative", "minimum_peak_height": 500}, "w")
        self.assertTrue(any("template_path is fixed by the contract" in item for item in plan["blockers"]), plan["blockers"])
        # The shared settings are not asked at all.
        with patch("msdial_app.agent_workflow.load_user_settings",
                   return_value={"template_path": str(custom), "queries_path": str(queries)}):
            plan = self.plan()
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertEqual(str(TEMPLATE.resolve()), plan["workflow"]["template_path"])
        self.assertEqual(0.1, float(plan["workflow"]["lbm_weighted_dot_product"]))
        self.assertEqual([], plan["lab_lipidomics"]["deviations"])

    def test_a_method_file_changed_since_the_contract_stops_the_plan(self) -> None:
        contract = json.loads(Path(self.contract).read_text(encoding="utf-8"))
        contract["inputs"]["parameter_template"]["sha256"] = "0" * 64
        Path(self.contract).write_text(json.dumps(contract), encoding="utf-8")
        plan = self.plan()
        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(any("parameter template msdial_console_param4lipidomics.txt has changed" in item
                            for item in plan["blockers"]), plan["blockers"])

    def test_an_answer_the_contract_does_not_state_is_a_deviation(self) -> None:
        plan = self.plan(automatic_rt_correction_match_rt_tolerance=2.0, alignment_light_mode=True)
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertEqual(
            [{"key": "alignment_light_mode", "contract": None, "given": True},
             {"key": "automatic_rt_correction_match_rt_tolerance", "contract": None, "given": 2.0}],
            plan["lab_lipidomics"]["deviations"],
        )

    def test_an_alignment_rt_tolerance_the_contract_does_not_fix_is_a_recorded_deviation(self) -> None:
        # The lipidomics contract fixes no alignment tolerance: a plan given one applies it and records it.
        plan = self.plan(alignment_rt_tolerance=0.3)
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertEqual([{"key": "alignment_rt_tolerance", "contract": None, "given": 0.3}],
                         plan["lab_lipidomics"]["deviations"])
        self.assertEqual("0.3", method_lines(prepare_run(plan["workflow"])["method_file"])[
            "Retention time tolerance for alignment"])
        self.assertEqual("0.1", method_lines(prepare_run(self.plan()["workflow"])["method_file"])[
            "Retention time tolerance for alignment"])

    def test_the_exports_cannot_be_sent_into_a_raw_folder(self) -> None:
        plan = self.plan(export_folder_path=str(self.batch.neg))
        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(any("export_folder_path is fixed by the contract" in item for item in plan["blockers"]),
                        plan["blockers"])
        self.assertEqual((self.batch.analysis / "neg").resolve(), Path(self.plan()["workflow"]["export_folder_path"]).resolve())

    def test_a_changed_input_stops_every_plan_from_the_contract(self) -> None:
        self.batch.is_neg.write_bytes(self.batch.is_neg.read_bytes() + b"\r\n")
        plan = self.plan()
        self.assertTrue(any("changed after it was written" in item for item in plan["blockers"]), plan["blockers"])

    def test_a_console_without_peak_ids_only_is_refused_before_the_run(self) -> None:
        found = {"capability_probe": "test", "capabilities": [
            item for item in EVERY_CAPABILITY["capabilities"] if item != NORMALIZE_PEAK_IDS_ONLY_CAPABILITY
        ]}
        with patch("msdial_app.workflow.console_capabilities", return_value=found):
            plan = self.plan()
        self.assertTrue(any("--peak-ids-only" in item for item in plan["blockers"]), plan["blockers"])

    def test_a_console_without_the_candidate_table_is_refused(self) -> None:
        found = {"capability_probe": "test", "capabilities": [
            item for item in EVERY_CAPABILITY["capabilities"] if item != "annotation_candidates_export"
        ]}
        with patch("msdial_app.workflow.console_capabilities", return_value=found):
            plan = self.plan()
        self.assertTrue(any(".mdcandidate.tsv" in item for item in plan["blockers"]), plan["blockers"])

    def test_an_internal_standard_library_not_in_the_console_layout_is_refused(self) -> None:
        library = self.batch.root / "reordered.txt"
        library.write_text(
            "Ontology\tName\tAdduct\tRT\tMZ\nPC\tPC 15:0_18:1(d7)\t[M+CH3COO]-\t9.5\t811.6199\n", encoding="ascii"
        )
        answers = {
            "project_type": "lcms", "ion_mode": "Negative", "target_omics": "Lipidomics",
            "parameter_strategy": "default", "execute_rt_correction": False, "library_strategy": "existing",
            "libraries": {"lbm_path": str(self.batch.lbm)}, "use_retention_time_for_annotation": False,
            "solvent_type": "CH3COONH4", "analysis_purpose": "x", "output_root": str(self.batch.root / "o"),
            "run_qa": False, "generate_materials_methods": False, "console_path": str(self.batch.console),
            "template_path": str(TEMPLATE), "class_assignment_confirmed": True,
            "internal_standard_text_path": str(library),
        }
        plan = build_guided_plan(str(self.batch.neg), answers)
        self.assertTrue(any("not in the column order" in item for item in plan["blockers"]), plan["blockers"])


if __name__ == "__main__":
    unittest.main()

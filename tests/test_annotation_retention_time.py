from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_app.agent_workflow import build_guided_plan
from msdial_app.workflow import prepare_run

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "resources" / "msdial_console_param4lipidomics.txt"


def _base_answers(console: Path, lbm: Path) -> dict:
    return {
        "project_type": "lcms",
        "ion_mode": "Negative",
        "target_omics": "Lipidomics",
        "parameter_strategy": "default",
        "execute_rt_correction": False,
        "library_strategy": "existing",
        "libraries": {"lbm_path": str(lbm)},
        "run_qa": False,
        "generate_materials_methods": False,
        "console_path": str(console),
        "template_path": str(TEMPLATE),
        # The Class assignment is a required confirmation on the laboratory path.
        "class_assignment_confirmed": True,
        # LC-MS lipidomics with the laboratory's own LBM2 is the laboratory lipidomics profile, which
        # also requires these (agent_workflow._is_lab_lipidomics_profile).
        "analysis_purpose": "test the guided path",
        "solvent_type": "CH3COONH4",
        "use_retention_time_for_annotation": False,
        "output_root": str(lbm.parent / "out"),
    }


class RetentionTimeQuestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "sample.mzML").write_text("", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_text("", encoding="ascii")
        self.lbm = self.root / "lab.lbm2"
        self.lbm.write_bytes(b"laboratory library")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _plan(self, **extra) -> dict:
        answers = _base_answers(self.console, self.lbm)
        answers.pop("use_retention_time_for_annotation")
        answers.update(extra)
        return build_guided_plan(str(self.root), answers)

    def _question_ids(self, plan: dict) -> list[str]:
        return [item["id"] for item in plan["remaining_questions"]]

    def test_choosing_a_library_raises_the_retention_time_question(self) -> None:
        plan = self._plan()
        self.assertIn("use_retention_time_for_annotation", self._question_ids(plan))

    def test_the_question_is_not_asked_before_a_library_is_chosen(self) -> None:
        # Whether retention time helps is a property of the library, so there is nothing
        # to ask until one has been named.
        plan = self._plan(library_strategy="ask")
        self.assertNotIn("use_retention_time_for_annotation", self._question_ids(plan))

    def test_a_tolerance_is_only_asked_for_once_retention_time_is_in_use(self) -> None:
        without = self._plan(use_retention_time_for_annotation=False)
        self.assertNotIn("retention_time_tolerance", self._question_ids(without))

        with_rt = self._plan(use_retention_time_for_annotation=True)
        self.assertIn("retention_time_tolerance", self._question_ids(with_rt))

    def test_the_thread_count_is_asked_once(self) -> None:
        self.assertIn("number_of_threads", self._question_ids(self._plan()))
        self.assertNotIn(
            "number_of_threads", self._question_ids(self._plan(number_of_threads=8))
        )

    def test_an_unanswered_advisory_question_does_not_hold_the_plan_up(self) -> None:
        # Leaving them unanswered keeps the conservative default -- annotation without
        # retention time -- which is a defensible position, not a guess. Outside the
        # laboratory lipidomics profile, that is.
        plan = self._plan(target_omics="Metabolomics")
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertIsNotNone(plan["workflow"])
        advisory = [item["id"] for item in plan["advisory_questions"]]
        self.assertIn("use_retention_time_for_annotation", advisory)
        self.assertIn("number_of_threads", advisory)

    def test_the_laboratory_lipidomics_profile_requires_the_annotation_rt_decision(self) -> None:
        plan = self._plan()
        required = [item["id"] for item in plan["remaining_questions"] if item["required"]]
        self.assertIn("use_retention_time_for_annotation", required)
        self.assertFalse(plan["ready_to_prepare"])
        tolerance = self._plan(use_retention_time_for_annotation=True)
        self.assertIn(
            "retention_time_tolerance",
            [item["id"] for item in tolerance["remaining_questions"] if item["required"]],
        )


class RetentionTimeReachesTheParameterFileTests(unittest.TestCase):
    """The answers are only worth collecting if MS-DIAL is told about them."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "sample.mzML").write_text("", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_text("", encoding="ascii")
        self.lbm = self.root / "lab.lbm2"
        self.lbm.write_bytes(b"laboratory library")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _method_lines(self, **extra) -> dict[str, str]:
        answers = _base_answers(self.console, self.lbm)
        answers["output_root"] = str(self.root / "out")
        answers.update(extra)
        plan = build_guided_plan(str(self.root), answers)
        self.assertIsNotNone(plan["workflow"], plan["blockers"])
        prepared = prepare_run(plan["workflow"])
        text = Path(prepared["method_file"]).read_text(encoding="utf-8")
        lines = {}
        for line in text.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                lines[key.strip()] = value.strip()
        return lines

    def test_using_retention_time_turns_on_scoring_and_filtering(self) -> None:
        lines = self._method_lines(
            use_retention_time_for_annotation=True, retention_time_tolerance=2
        )
        self.assertEqual("True", lines["Use retention information for LBM-based annotation scoring"])
        self.assertEqual("True", lines["Use retention information for LBM-based annotation filtering"])
        # A whole-numbered tolerance is now written "2" rather than "2.0". The C# arm for this
        # key is float.TryParse and reads either, but several sibling keys are int.TryParse and
        # read only the first, so the writer no longer emits a decimal point it does not need.
        self.assertEqual("2", lines["RT tolerance for LBM-based annotation"])
        self.assertEqual(
            "0.5",
            self._method_lines(
                use_retention_time_for_annotation=True, retention_time_tolerance=0.5
            )["RT tolerance for LBM-based annotation"],
            "and a fractional tolerance keeps its fraction",
        )

    def test_declining_retention_time_leaves_it_off(self) -> None:
        lines = self._method_lines(use_retention_time_for_annotation=False)
        self.assertEqual("False", lines["Use retention information for LBM-based annotation scoring"])
        self.assertEqual("False", lines["Use retention information for LBM-based annotation filtering"])

    def test_a_tolerance_given_without_using_retention_time_is_not_applied(self) -> None:
        # Accepting it would write a tolerance that nothing consults, which reads in the
        # parameter file as though retention time were in play.
        lines = self._method_lines(
            use_retention_time_for_annotation=False, retention_time_tolerance=2
        )
        self.assertEqual("False", lines["Use retention information for LBM-based annotation scoring"])
        self.assertNotEqual("2.0", lines["RT tolerance for LBM-based annotation"])

    def _msp_settings(self, **extra) -> list[dict[str, str]]:
        """The MSP annotator table a metabolomics run with one MSP library writes."""
        msp = self.root / "library.msp"
        msp.write_text("NAME: Alanine\nPRECURSORMZ: 90.055\nRETENTIONTIME: 0.9\nNum Peaks: 1\n44.05 100\n\n", encoding="ascii")
        answers = _base_answers(self.console, self.lbm)
        answers.update({
            "target_omics": "Metabolomics", "ion_mode": "Positive", "libraries": {"msp_paths": [str(msp)]},
            "output_root": str(self.root / "out"),
        })
        answers.update(extra)
        plan = build_guided_plan(str(self.root), answers)
        self.assertIsNotNone(plan["workflow"], plan["blockers"])
        prepared = prepare_run(plan["workflow"])
        settings = Path(prepared["method_file"]).parent / "msp_annotator_settings.tsv"
        lines = settings.read_text(encoding="ascii").splitlines()
        header = lines[0].split("\t")
        return [dict(zip(header, line.split("\t"))) for line in lines[1:]]

    def test_the_decision_reaches_an_msp_library_too(self) -> None:
        # Each MSP annotator row carries its own RT settings, and the row is what the Console reads, so
        # before this an MSP with retention times was searched without them whatever was answered.
        (row,) = self._msp_settings(use_retention_time_for_annotation=True, retention_time_tolerance=0.5)
        self.assertEqual(("True", "True", "0.5"), (
            row["use_retention_information_for_scoring"], row["use_retention_information_for_filtering"], row["rt_tolerance"]))
        (row,) = self._msp_settings(use_retention_time_for_annotation=False)
        self.assertEqual(("False", "False"), (
            row["use_retention_information_for_scoring"], row["use_retention_information_for_filtering"]))

    def test_a_row_whose_rt_use_was_decided_for_that_library_keeps_it(self) -> None:
        from msdial_app.agent_workflow import _apply_retention_time_use

        state = {
            "msp_annotators": [
                {"annotator_id": "in_house", "rt_fixed": True, "use_rt_scoring": True, "use_rt_filtering": True, "rt_tolerance": 0.5},
                {"annotator_id": "public", "use_rt_scoring": False, "use_rt_filtering": False, "rt_tolerance": 0.5},
            ],
            "text_annotators": [{"annotator_id": "is", "internal_standards": True, "use_rt_scoring": True, "rt_tolerance": 0.2}],
        }
        _apply_retention_time_use(state, {"use_retention_time_for_annotation": False})
        fixed, public = state["msp_annotators"]
        self.assertEqual((True, True, 0.5), (fixed["use_rt_scoring"], fixed["use_rt_filtering"], fixed["rt_tolerance"]))
        self.assertEqual((False, False), (public["use_rt_scoring"], public["use_rt_filtering"]))
        self.assertEqual((True, 0.2), (state["text_annotators"][0]["use_rt_scoring"], state["text_annotators"][0]["rt_tolerance"]))
        _apply_retention_time_use(state, {"use_retention_time_for_annotation": True, "retention_time_tolerance": 2})
        self.assertEqual((True, 2.0), (public["use_rt_scoring"], public["rt_tolerance"]))
        self.assertEqual(0.5, fixed["rt_tolerance"])

    def test_the_thread_count_reaches_the_method_file(self) -> None:
        self.assertEqual("8", self._method_lines(number_of_threads=8)["Number of threads"])

    def test_the_answers_survive_a_workset(self) -> None:
        from msdial_app.worksets import get_workset, save_workset

        name = "lab-lipidomics-retention-time-test"
        save_workset(
            name,
            {
                "use_retention_time_for_annotation": True,
                "retention_time_tolerance": 2,
                "number_of_threads": 8,
            },
            description="retention-time settings saved for reuse",
        )
        try:
            stored = get_workset(name)
            self.assertTrue(stored["answers"]["use_retention_time_for_annotation"])
            self.assertEqual(2, stored["answers"]["retention_time_tolerance"])
            self.assertEqual(8, stored["answers"]["number_of_threads"])
        finally:
            path = Path(stored["file"]) if stored and stored.get("file") else None
            if path and path.exists():
                path.unlink()


if __name__ == "__main__":
    unittest.main()


class WorksetOverrideTests(unittest.TestCase):
    """A workset stores answers and overrides side by side; both must be read back."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "sample.mzML").write_text("", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_text("", encoding="ascii")
        self.lbm = self.root / "lab.lbm2"
        self.lbm.write_bytes(b"laboratory library")
        self.saved: list[Path] = []

    def tearDown(self) -> None:
        for path in self.saved:
            if path.exists():
                path.unlink()
        self.directory.cleanup()

    def _save(self, name: str, answers: dict, overrides: dict) -> None:
        from msdial_app.worksets import get_workset, save_workset

        save_workset(name, answers, description="test", workflow_overrides=overrides)
        stored = get_workset(name)
        if stored and stored.get("file"):
            self.saved.append(Path(stored["file"]))

    def _workflow(self, workset_id: str, **extra) -> dict:
        answers = _base_answers(self.console, self.lbm)
        answers["output_root"] = str(self.root / "out")
        answers.update(extra)
        plan = build_guided_plan(str(self.root), answers, workset_id)
        self.assertIsNotNone(plan["workflow"], plan["blockers"])
        return plan["workflow"]

    def test_an_override_saved_in_a_workset_reaches_the_workflow(self) -> None:
        # It used to be written to disk and then ignored by every plan built from it.
        self._save("override-test-a", {}, {"minimum_peak_width": 9})
        self.assertEqual(9, self._workflow("override-test-a")["minimum_peak_width"])

    def test_an_override_supplied_now_beats_the_one_the_workset_remembers(self) -> None:
        self._save("override-test-b", {}, {"minimum_peak_width": 9})
        workflow = self._workflow(
            "override-test-b", workflow_overrides={"minimum_peak_width": 3}
        )
        self.assertEqual(3, workflow["minimum_peak_width"])

    def test_answers_and_overrides_from_one_workset_both_apply(self) -> None:
        self._save(
            "override-test-c",
            {"number_of_threads": 8},
            {"minimum_peak_width": 9},
        )
        workflow = self._workflow("override-test-c")
        self.assertEqual(8, workflow["number_of_threads"])
        self.assertEqual(9, workflow["minimum_peak_width"])


class SampleTableConfirmationTests(unittest.TestCase):
    """The grouping is the axis every comparison is drawn along."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        for name in ("run_1_ctrl.mzML", "run_2_ctrl.mzML", "run_3_dosed.mzML", "run_4_dosed.mzML"):
            (self.root / name).write_text("", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_text("", encoding="ascii")
        self.lbm = self.root / "lab.lbm2"
        self.lbm.write_bytes(b"laboratory library")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _plan(self, **extra) -> dict:
        answers = _base_answers(self.console, self.lbm)
        answers.pop("class_assignment_confirmed", None)
        answers.update(extra)
        return build_guided_plan(str(self.root), answers)

    def test_the_class_assignment_must_be_confirmed_before_the_plan_is_ready(self) -> None:
        plan = self._plan()
        ids = [item["id"] for item in plan["remaining_questions"]]
        self.assertIn("class_assignment_confirmed", ids)
        self.assertFalse(plan["ready_to_prepare"])
        self.assertIsNone(plan["workflow"], "nothing is built until the comparison is agreed")

    def test_confirming_it_lets_the_plan_proceed(self) -> None:
        plan = self._plan(class_assignment_confirmed=True)
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertIsNotNone(plan["workflow"])

    def test_a_repository_run_is_not_asked_because_it_has_no_analyst(self) -> None:
        # Repository reanalysis takes its classes from repository metadata and has its
        # own gate; asking here would stop a systematic batch that nobody is watching.
        metadata = self.root / "meta.json"
        metadata.write_text(
            json.dumps(
                {
                    "project": {
                        "repository": "MetaboLights",
                        "accession": "MTBLS0000",
                        "sample_metadata": [
                            {"values": {"raw_file": name, "group": name.split("_")[-1]}}
                            for name in ("run_1_ctrl", "run_2_ctrl", "run_3_dosed", "run_4_dosed")
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        plan = self._plan(repository_metadata_path=str(metadata))
        ids = [item["id"] for item in plan["remaining_questions"]]
        self.assertNotIn("class_assignment_confirmed", ids)

    def test_the_dilution_factor_is_raised_and_reaches_the_files(self) -> None:
        plan = self._plan(class_assignment_confirmed=True)
        self.assertIn("dilution_factor", [item["id"] for item in plan["remaining_questions"]])

        answered = self._plan(class_assignment_confirmed=True, dilution_factor=2.5)
        self.assertEqual(
            [2.5] * 4, [item["factor"] for item in answered["workflow"]["files"]]
        )

    def test_the_plan_says_how_the_injection_order_was_derived(self) -> None:
        proposal = self._plan(class_assignment_confirmed=True)["input"]["sample_table_proposal"]
        self.assertIn("acquisition sequence", proposal["analytical_order"]["reason"])
        self.assertTrue(proposal["dilution_factor"]["assumed"])
        self.assertIn("default rather than a value", proposal["dilution_factor"]["note"])

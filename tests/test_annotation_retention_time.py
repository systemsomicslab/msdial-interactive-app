from __future__ import annotations

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
    }


class RetentionTimeQuestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "sample.mzML").write_text("", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_text("", encoding="ascii")
        self.lbm = self.root / "lab.lbm2"
        self.lbm.write_text("", encoding="ascii")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _plan(self, **extra) -> dict:
        answers = _base_answers(self.console, self.lbm)
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
        # retention time -- which is a defensible position, not a guess.
        plan = self._plan()
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertIsNotNone(plan["workflow"])
        advisory = [item["id"] for item in plan["advisory_questions"]]
        self.assertIn("use_retention_time_for_annotation", advisory)
        self.assertIn("number_of_threads", advisory)


class RetentionTimeReachesTheParameterFileTests(unittest.TestCase):
    """The answers are only worth collecting if MS-DIAL is told about them."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "sample.mzML").write_text("", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_text("", encoding="ascii")
        self.lbm = self.root / "lab.lbm2"
        self.lbm.write_text("", encoding="ascii")

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
        self.assertEqual("2.0", lines["RT tolerance for LBM-based annotation"])

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
        self.lbm.write_text("", encoding="ascii")
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

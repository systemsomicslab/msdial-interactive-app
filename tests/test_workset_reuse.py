"""A second dataset should only have to confirm what changed.

That premise has two halves, and only one of them is safe on its own. Settings must
carry from one analysis to the next, and confirmations must not: a Class assignment
agreed for one set of file names is not an agreement about another set, and a workset
that carried it would answer a question nobody was asked.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_app.agent_workflow import build_guided_plan
from msdial_app.worksets import describe_workset_candidate, get_workset, save_workset

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "resources" / "msdial_console_param4lipidomics.txt"


class WorksetCandidateTests(unittest.TestCase):
    def test_the_method_settings_carry(self) -> None:
        candidate = describe_workset_candidate(
            {
                "project_type": "lcms",
                "target_omics": "Lipidomics",
                "use_retention_time_for_annotation": True,
                "retention_time_tolerance": 2,
                "number_of_threads": 8,
            }
        )
        self.assertEqual(2, candidate["reusable_answers"]["retention_time_tolerance"])
        self.assertEqual(8, candidate["reusable_answers"]["number_of_threads"])
        self.assertTrue(candidate["worth_saving"])

    def test_a_confirmation_is_not_inherited(self) -> None:
        candidate = describe_workset_candidate(
            {"number_of_threads": 8, "class_assignment_confirmed": True}
        )
        self.assertNotIn("class_assignment_confirmed", candidate["reusable_answers"])
        self.assertIn("class_assignment_confirmed", candidate["not_reusable"])
        self.assertIn(
            "the next dataset has its own",
            candidate["not_reusable"]["class_assignment_confirmed"],
        )

    def test_the_dilution_factor_stays_with_the_batch_that_was_prepared(self) -> None:
        candidate = describe_workset_candidate({"dilution_factor": 10})
        self.assertNotIn("dilution_factor", candidate["reusable_answers"])
        self.assertIn("scales every concentration", candidate["not_reusable"]["dilution_factor"])

    def test_paths_to_this_dataset_do_not_travel(self) -> None:
        candidate = describe_workset_candidate(
            {
                "input_path": "D:/data/run1",
                "output_root": "D:/out/run1",
                "repository_metadata_path": "D:/meta/MTBLS1.json",
                "number_of_threads": 8,
            }
        )
        self.assertEqual({"number_of_threads": 8}, candidate["reusable_answers"])

    def test_a_measured_threshold_says_it_was_measured(self) -> None:
        # It is a legitimate laboratory setting and it carries, but saving a number that
        # a diagnostic produced from these files, as though it had been chosen, is how a
        # threshold nobody measured ends up in somebody else's run.
        candidate = describe_workset_candidate(
            {"parameter_strategy": "target_peak_count", "minimum_peak_height": 500}
        )
        self.assertEqual(500, candidate["reusable_answers"]["minimum_peak_height"])
        self.assertTrue(candidate["caveats"])
        self.assertIn("measured by a", candidate["caveats"][0])

    def test_a_chosen_threshold_needs_no_caveat(self) -> None:
        candidate = describe_workset_candidate(
            {"parameter_strategy": "default", "minimum_peak_height": 1000}
        )
        self.assertEqual([], candidate["caveats"])

    def test_a_workset_already_in_use_and_unchanged_is_not_offered_again(self) -> None:
        source = {"id": "lab", "answers": {"number_of_threads": 8}}
        unchanged = describe_workset_candidate({"number_of_threads": 8}, source=source)
        self.assertFalse(unchanged["worth_saving"])
        self.assertEqual({}, unchanged["changed_from_source"])

        changed = describe_workset_candidate({"number_of_threads": 16}, source=source)
        self.assertTrue(changed["worth_saving"])
        self.assertEqual({"was": 8, "now": 16}, changed["changed_from_source"]["number_of_threads"])


class WorksetRoundTripTests(unittest.TestCase):
    """What is saved must be what comes back, and no more than that."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        for name in ("run_1_ctrl.mzML", "run_2_ctrl.mzML", "run_3_dosed.mzML", "run_4_dosed.mzML"):
            (self.root / name).write_text("", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_text("", encoding="ascii")
        self.lbm = self.root / "lab_library.lbm2"
        self.lbm.write_text("", encoding="ascii")
        self.saved: list[Path] = []

    def tearDown(self) -> None:
        for path in self.saved:
            if path.exists():
                path.unlink()
        self.directory.cleanup()

    def _answers(self, **extra) -> dict:
        answers = {
            "project_type": "lcms",
            "ion_mode": "Negative",
            "target_omics": "Lipidomics",
            "parameter_strategy": "default",
            "execute_rt_correction": False,
            "library_strategy": "existing",
            "libraries": {"lbm_path": str(self.lbm)},
            "use_retention_time_for_annotation": True,
            "retention_time_tolerance": 2,
            "number_of_threads": 8,
            "run_qa": False,
            "generate_materials_methods": False,
            "console_path": str(self.console),
            "template_path": str(TEMPLATE),
            "class_assignment_confirmed": True,
        }
        answers.update(extra)
        return answers

    def _save(self, name: str, answers: dict) -> dict:
        item = save_workset(name, answers, description="round trip test")
        self.saved.append(Path(item["file"]))
        return item

    def test_the_plan_offers_a_workset_and_names_it_after_the_method(self) -> None:
        plan = build_guided_plan(str(self.root), self._answers())
        suggestion = plan["workset_suggestion"]
        self.assertTrue(suggestion["worth_saving"])
        self.assertIn("LC-MS", suggestion["suggested_name"])
        self.assertIn("Lipidomics", suggestion["suggested_name"])
        self.assertIn("lab_library", suggestion["suggested_name"])

    def test_the_second_dataset_inherits_the_settings(self) -> None:
        item = self._save("lab lipidomics reuse test", self._answers())
        second = build_guided_plan(
            str(self.root),
            {"class_assignment_confirmed": True},
            item["id"],
        )
        self.assertTrue(second["ready_to_prepare"], second["blockers"])
        workflow = second["workflow"]
        self.assertEqual(8, workflow["number_of_threads"])
        self.assertEqual(str(self.lbm), workflow["lbm_path"])

    def test_the_second_dataset_is_still_asked_to_confirm_its_own_classes(self) -> None:
        # This is the whole point of separating the two halves: inheriting the library
        # and the tolerance must not also inherit somebody's agreement about a different
        # set of file names.
        item = self._save("lab lipidomics confirmation test", self._answers())
        stored = get_workset(item["id"])
        self.assertNotIn("class_assignment_confirmed", stored["answers"])

        second = build_guided_plan(str(self.root), {}, item["id"])
        ids = [question["id"] for question in second["remaining_questions"]]
        self.assertIn("class_assignment_confirmed", ids)
        self.assertFalse(second["ready_to_prepare"])

    def test_a_run_records_the_answers_it_used(self) -> None:
        # workflow-settings.json holds the resolved parameters and cannot say which of
        # them anyone decided, so a workset cannot be rebuilt from it.
        from msdial_app.server import _read_guided_answers, _write_guided_answers

        run_directory = self.root / "run"
        run_directory.mkdir()
        plan = build_guided_plan(str(self.root), self._answers())
        written = _write_guided_answers({"run_directory": str(run_directory)}, plan)
        self.assertTrue(Path(written).is_file())

        payload = json.loads(Path(written).read_text(encoding="utf-8"))
        self.assertEqual(self.root.resolve(), Path(payload["input_path"]).resolve())
        self.assertEqual(8, payload["answers"]["number_of_threads"])

        recovered = _read_guided_answers(str(run_directory))
        self.assertEqual(2, recovered["retention_time_tolerance"])

    def test_recovering_answers_from_a_run_that_recorded_none_says_so(self) -> None:
        from msdial_app.server import _read_guided_answers

        with self.assertRaises(FileNotFoundError) as raised:
            _read_guided_answers(str(self.root))
        self.assertIn("cannot be recovered", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

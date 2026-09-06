from __future__ import annotations

import unittest

from msdial_app.sample_grouping import file_type_for, propose_grouping

# The reference lipidomics study: germ-free against specific-pathogen-free mice, three
# replicates each, one blank. The names also carry a date, an instrument, a project, a
# subject number, an age, a sex, a matrix, a replicate index and a polarity.
LECTURE_NAMES = [
    "190722_TT1_Hclass_Aging_004_2_9w_SPF_M_Plasma_2_Neg",
    "190722_TT1_Hclass_Aging_010_13_9w_GF_M_Plasma_1_Neg",
    "190722_TT1_Hclass_Aging_022_14_9w_GF_M_Plasma_2_Neg",
    "190722_TT1_Hclass_Aging_023_15_9w_GF_M_Plasma_3_Neg",
    "190722_TT1_Hclass_Aging_025_3_9w_SPF_M_Plasma_3_Neg",
    "190722_TT1_Hclass_Aging_035_1_9w_SPF_M_Plasma_1_Neg",
    "190722_TT1_Hclass_Aging_Blank_Neg",
]


class LectureStudyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = propose_grouping(LECTURE_NAMES)

    def test_the_treatment_is_proposed_rather_than_the_matrix(self) -> None:
        # "Plasma" is in every name, so grouping by it makes one group of everything.
        assignments = self.result["assignments"]
        self.assertEqual(
            ["SPF", "GF", "GF", "GF", "SPF", "SPF", "Blank"],
            [assignments[name] for name in LECTURE_NAMES],
        )

    def test_the_blank_is_set_aside_from_the_comparison(self) -> None:
        self.assertEqual("Blank", self.result["assignments"][LECTURE_NAMES[-1]])
        self.assertEqual("Blank", file_type_for(LECTURE_NAMES[-1]))
        for candidate in self.result["candidates"]:
            self.assertNotIn("Blank", candidate["values"])

    def test_the_replicate_index_is_offered_but_not_chosen(self) -> None:
        # It does partition the files, so it is a real alternative for a person to pick;
        # it just is not the likelier comparison.
        alternatives = {" / ".join(candidate["values"]) for candidate in self.result["candidates"]}
        self.assertIn("GF / SPF", alternatives)
        self.assertIn("1 / 2 / 3", alternatives)
        self.assertEqual(["GF", "SPF"], self.result["candidates"][0]["values"])

    def test_the_proposal_says_how_it_was_reached(self) -> None:
        self.assertIn("GF, SPF", self.result["reason"])
        self.assertIn("each shared by at least 3 files", self.result["reason"])


class GroupingShapeTests(unittest.TestCase):
    def test_a_token_that_never_varies_groups_nothing(self) -> None:
        result = propose_grouping(["study_plasma_a1", "study_plasma_a2", "study_plasma_a3"])
        for candidate in result["candidates"]:
            self.assertNotIn("plasma", candidate["values"])

    def test_a_token_unique_to_every_file_is_an_identifier_not_a_factor(self) -> None:
        result = propose_grouping(["run_001_ctrl", "run_002_ctrl", "run_003_ctrl"])
        self.assertEqual([], result["candidates"], "nothing here forms a replicated group")
        self.assertEqual({"run_001_ctrl": "Sample", "run_002_ctrl": "Sample", "run_003_ctrl": "Sample"},
                         result["assignments"])

    def test_a_group_of_one_is_a_sample_not_a_condition(self) -> None:
        result = propose_grouping(["s_ctrl_1", "s_ctrl_2", "s_treated_1"])
        self.assertEqual([], result["candidates"])

    def test_a_word_factor_outranks_a_numeric_one(self) -> None:
        names = [
            "x_ctrl_1", "x_ctrl_2", "x_ctrl_3",
            "x_dosed_1", "x_dosed_2", "x_dosed_3",
        ]
        result = propose_grouping(names)
        self.assertEqual(["ctrl", "dosed"], result["candidates"][0]["values"])

    def test_polarity_is_never_the_experimental_factor(self) -> None:
        # Both polarities of the same samples are two runs, not two conditions.
        names = ["a_ctrl_pos", "b_ctrl_pos", "a_ctrl_neg", "b_ctrl_neg"]
        result = propose_grouping(names)
        for candidate in result["candidates"]:
            self.assertNotIn("pos", [value.lower() for value in candidate["values"]])

    def test_a_quality_control_file_is_set_aside(self) -> None:
        names = ["s_ctrl_1", "s_ctrl_2", "s_dosed_1", "s_dosed_2", "s_QC_1"]
        result = propose_grouping(names)
        self.assertEqual("QC", result["assignments"]["s_QC_1"])
        self.assertEqual("QC", file_type_for("s_QC_1"))

    def test_one_file_cannot_establish_a_grouping(self) -> None:
        result = propose_grouping(["only_one_sample"])
        self.assertIsNone(result["chosen"])
        self.assertIn("fewer than two", result["reason"])

    def test_no_files_at_all_is_answered_without_failing(self) -> None:
        result = propose_grouping([])
        self.assertIsNone(result["chosen"])
        self.assertEqual({}, result["assignments"])


if __name__ == "__main__":
    unittest.main()

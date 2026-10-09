"""The user's one encoding rule of 2026-10-09, as one function (msdial_app.encoding_rule.choose_encoding).

"A: この一つのルールで統一": when one sample's data arrive in several encodings, copies in other folders included,

1. of the readable ones exactly one is used, the highest in the order vendor -> mzML -> mzXML (a re-encoding MS-DIAL
   opens, .cdf/.abf/.ibf, outside the rule's words, keeps its old place between vendor and mzML);
2. a tie goes to the first by path name, lexicographic, without case, '/'-separated;
3. a chosen one that cannot be read or decoded, or whose conversion fails, gives way to the next in order;
4. the file used is that sample's own input, whatever encoding the sample row names (the lease's part:
   test_replicate_injections.TheOneEncodingRule);
5. every file not used is recorded with its reason, naming the file used.

These tests hold the function to clauses 1, 2, 3 and 5 with readability given directly; the lease tests hold the
lease to all five on real leases.
"""

from __future__ import annotations

import unittest

from msdial_app import encoding_rule
from msdial_app.encoding_rule import (
    CONVERSION_FAILED,
    INCOMPLETE_CONTAINER,
    LOWER_IN_ENCODING_ORDER,
    RAW_HEADER_UNREADABLE,
    RAW_HEADER_UNSUPPORTED_FORMAT,
    REQUIRES_CONVERSION,
    RULE,
    TIE_LEXICOGRAPHIC,
    UNDECODABLE,
    choices_record,
    choose_encoding,
    encoding_rank,
)


def _choose(paths: list[str], unreadable: dict[str, str] | None = None, asked: list[str] | None = None):
    """choose_encoding over these relative paths (each its own key), every one readable but ``unreadable``'s."""
    unreadable = unreadable or {}

    def readability(item: str) -> str:
        if asked is not None:
            asked.append(item)
        return unreadable.get(item, "")

    return choose_encoding({path: path for path in paths}, readability)


class Clause1TheOrderIsVendorThenMzmlThenMzxml(unittest.TestCase):
    def test_the_rank_of_each_encoding(self) -> None:
        self.assertEqual(
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 2, 3, None, None],
            [encoding_rank(path) for path in (
                "S1.raw", "S1.d", "S1.wiff", "S1.wiff2", "a/S1.RAW", "S1.lcd", "S1.qgd", "S1.cdf", "S1.ABF", "S1.ibf",
                "S1.mzML", "S1.mzXML", "S1.mzData", "S1.txt",
            )],
        )

    def test_a_re_encoding_ranks_after_every_vendor_format_and_before_mzml(self) -> None:
        # Review of PR #69, follow-up 1: .cdf and .abf ranked as vendor, and 'abf' and 'cdf' sort before 'd' and 'raw',
        # so a netCDF or ABF re-encoding won every tie against the instrument's own file. Outside the rule's words, they
        # keep the place they held before it: below a vendor file, above an mzML.
        for re_encoding in ("S1.cdf", "S1.abf", "S1.ibf"):
            with self.subTest(re_encoding=re_encoding):
                for vendor in ("S1.raw", "S1.d", "S1.wiff"):
                    choice = _choose([vendor, re_encoding])
                    self.assertEqual(vendor, choice.used)
                    self.assertEqual([(re_encoding, LOWER_IN_ENCODING_ORDER)], list(choice.unused))
                choice = _choose(["S1.mzML", re_encoding])
                self.assertEqual((re_encoding, [("S1.mzML", LOWER_IN_ENCODING_ORDER)]), (choice.used, list(choice.unused)))

    def test_two_re_encodings_tie(self) -> None:
        choice = _choose(["S1.cdf", "S1.abf"])

        self.assertEqual(("S1.abf", [("S1.cdf", TIE_LEXICOGRAPHIC)]), (choice.used, list(choice.unused)))

    def test_an_unreadable_vendor_file_gives_way_to_a_re_encoding(self) -> None:
        choice = _choose(["S1.raw", "S1.cdf", "S1.mzML"], {"S1.raw": RAW_HEADER_UNREADABLE})

        self.assertEqual("S1.cdf", choice.used)
        self.assertEqual([("S1.raw", RAW_HEADER_UNREADABLE), ("S1.mzML", LOWER_IN_ENCODING_ORDER)], list(choice.unused))

    def test_a_vendor_file_is_used_before_an_mzml_and_an_mzxml(self) -> None:
        choice = _choose(["S1.mzXML", "S1.mzML", "S1.raw"])

        self.assertEqual("S1.raw", choice.used)
        self.assertEqual(
            {"rule": RULE, "used": "S1.raw",
             "unused": [{"path": "S1.mzML", "reason": LOWER_IN_ENCODING_ORDER},
                        {"path": "S1.mzXML", "reason": LOWER_IN_ENCODING_ORDER}]},
            choice.record(),
        )

    def test_an_mzml_is_used_before_an_mzxml(self) -> None:
        self.assertEqual("S1.mzML", _choose(["S1.mzXML", "S1.mzML"]).used)

    def test_a_vendor_folder_is_a_vendor_format(self) -> None:
        self.assertEqual("x/S1.d", _choose(["x/S1.mzML", "x/S1.d"]).used)

    def test_one_candidate_is_used_alone(self) -> None:
        choice = _choose(["S1.mzXML"])
        self.assertEqual(("S1.mzXML", ()), (choice.used, choice.unused))


class Clause2TiesGoToTheFirstPathWithoutCase(unittest.TestCase):
    def test_copies_in_two_folders_tie_and_the_first_path_is_used(self) -> None:
        choice = _choose(["b/S1.raw", "a/S1.raw"])

        self.assertEqual("a/S1.raw", choice.used)
        self.assertEqual([("b/S1.raw", TIE_LEXICOGRAPHIC)], list(choice.unused))

    def test_a_path_is_compared_whole_not_by_depth(self) -> None:
        # 'raw/s1.raw' sorts before 's1.raw': the rule says the first path, not the one nearest the data root.
        self.assertEqual("RAW/S1.raw", _choose(["S1.raw", "RAW/S1.raw"]).used)

    def test_case_does_not_order_paths(self) -> None:
        self.assertEqual("a/S1.raw", _choose(["B/S1.raw", "a/S1.raw"]).used)

    def test_separators_are_slashes(self) -> None:
        self.assertEqual("a\\S1.raw", _choose(["b/S1.raw", "a\\S1.raw"]).used)
        self.assertEqual("a/S1.raw", _choose(["b/S1.raw", "a\\S1.raw"]).record()["used"])

    def test_two_vendor_formats_tie(self) -> None:
        choice = _choose(["S1.raw", "S1.d"])

        self.assertEqual("S1.d", choice.used)
        self.assertEqual([("S1.raw", TIE_LEXICOGRAPHIC)], list(choice.unused))

    def test_a_lower_encoding_after_a_tie_is_lower_not_tied(self) -> None:
        choice = _choose(["b/S1.raw", "a/S1.raw", "S1.mzML"])

        self.assertEqual(
            [("b/S1.raw", TIE_LEXICOGRAPHIC), ("S1.mzML", LOWER_IN_ENCODING_ORDER)], list(choice.unused)
        )


class Clause3AnUnreadableChoiceGivesWayToTheNext(unittest.TestCase):
    def test_an_undecodable_mzml_gives_way_to_its_copy(self) -> None:
        choice = _choose(["a/S1.mzML", "b/S1.mzML"], {"a/S1.mzML": UNDECODABLE})

        self.assertEqual("b/S1.mzML", choice.used)
        self.assertEqual([("a/S1.mzML", UNDECODABLE)], list(choice.unused))

    def test_an_undecodable_mzml_gives_way_to_an_mzxml(self) -> None:
        choice = _choose(["S1.mzML", "S1.mzXML"], {"S1.mzML": UNDECODABLE})

        self.assertEqual("S1.mzXML", choice.used)
        self.assertEqual([("S1.mzML", UNDECODABLE)], list(choice.unused))

    def test_a_failed_conversion_gives_way_to_the_next_mzxml(self) -> None:
        choice = _choose(["a/S1.mzXML", "b/S1.mzXML"], {"a/S1.mzXML": CONVERSION_FAILED})

        self.assertEqual("b/S1.mzXML", choice.used)
        self.assertEqual([("a/S1.mzXML", CONVERSION_FAILED)], list(choice.unused))

    def test_where_none_can_be_read_none_is_used_and_each_says_why(self) -> None:
        choice = _choose(["S1.mzML", "S1.mzXML"], {"S1.mzML": UNDECODABLE, "S1.mzXML": REQUIRES_CONVERSION})

        self.assertIsNone(choice.used)
        self.assertEqual(
            {"rule": RULE, "used": None,
             "unused": [{"path": "S1.mzML", "reason": UNDECODABLE}, {"path": "S1.mzXML", "reason": REQUIRES_CONVERSION}]},
            choice.record(),
        )

    def test_readability_is_asked_in_order_and_only_until_one_is_readable(self) -> None:
        # An mzXML is converted only where nothing before it can be read, and an mzML scanned only where no vendor
        # encoding is used.
        asked: list[str] = []
        _choose(["S1.mzXML", "S1.mzML", "S1.raw"], asked=asked)
        self.assertEqual(["S1.raw"], asked)

        asked.clear()
        _choose(["S1.mzXML", "S1.mzML"], {"S1.mzML": UNDECODABLE}, asked=asked)
        self.assertEqual(["S1.mzML", "S1.mzXML"], asked)


class Clause3AVendorFileTheLeaseCannotReadGivesWay(unittest.TestCase):
    def test_a_header_that_cannot_be_read_gives_way_to_the_mzml(self) -> None:
        for reason in (RAW_HEADER_UNREADABLE, RAW_HEADER_UNSUPPORTED_FORMAT, INCOMPLETE_CONTAINER):
            with self.subTest(reason=reason):
                choice = _choose(["S1.lcd", "S1.mzML"], {"S1.lcd": reason})

                self.assertEqual("S1.mzML", choice.used)
                self.assertEqual({"rule": RULE, "used": "S1.mzML", "unused": [{"path": "S1.lcd", "reason": reason}]},
                                 choice.record())

    def test_the_lease_gives_the_preflights_reasons(self) -> None:
        self.assertEqual(
            ("raw_header_unreadable", "raw_header_unsupported_format", "incomplete_container"),
            (RAW_HEADER_UNREADABLE, RAW_HEADER_UNSUPPORTED_FORMAT, INCOMPLETE_CONTAINER),
        )
        self.assertEqual(frozenset({RAW_HEADER_UNREADABLE, RAW_HEADER_UNSUPPORTED_FORMAT}), encoding_rule.RAW_HEADER_REASONS)


class Clause5EveryFileNotUsedIsRecorded(unittest.TestCase):
    def test_the_record_names_the_file_used_and_each_other_once_in_order(self) -> None:
        choice = _choose(
            ["S1.mzXML", "b/S1.raw", "a/S1.raw", "S1.mzML", "c/S1.mzXML"], {"a/S1.raw": "unreadable_for_a_test"}
        )

        self.assertEqual(
            {"rule": RULE, "used": "b/S1.raw",
             "unused": [{"path": "a/S1.raw", "reason": "unreadable_for_a_test"},
                        {"path": "S1.mzML", "reason": LOWER_IN_ENCODING_ORDER},
                        {"path": "c/S1.mzXML", "reason": LOWER_IN_ENCODING_ORDER},
                        {"path": "S1.mzXML", "reason": LOWER_IN_ENCODING_ORDER}]},
            choice.record(),
        )
        self.assertEqual(sorted(["S1.mzXML", "b/S1.raw", "a/S1.raw", "S1.mzML", "c/S1.mzXML"]), sorted(choice.candidates))

    def test_the_units_record_lists_only_samples_with_a_choice(self) -> None:
        records = choices_record([_choose(["S2.raw"]), _choose(["S1.mzML", "S1.raw"]), _choose(["z.mzML"], {"z.mzML": UNDECODABLE})])

        self.assertEqual([{"rule": RULE, "used": "S1.raw",
                           "unused": [{"path": "S1.mzML", "reason": LOWER_IN_ENCODING_ORDER}]}], records)

    def test_the_rule_is_named(self) -> None:
        self.assertEqual("one_encoding_per_sample_2026_10_09", encoding_rule.RULE)


if __name__ == "__main__":
    unittest.main()

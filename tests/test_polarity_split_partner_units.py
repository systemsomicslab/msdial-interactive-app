"""Partner units of a polarity-switching deposit (0.5.71).

A repository may register a positive and a negative analysis over one set of polarity-switching files as two Catalog
units. By the author's decision of 2026-11-20 each runs once, at its own polarity, as a polarity_split child of itself
that names the other unit in polarity_split.partner_unit_id. These tests hold:

- partner_unit_id is optional; a string naming another unit, or null, is accepted and recorded; anything else is
  refused with curated_selection_polarity_split_invalid. The run key stays <unit_id>-pos or <unit_id>-neg.
- Two partner units naming each other at opposite polarities pass the cross-unit check over their shared files (rules
  P and D), as two siblings of one unit do; one that names a unit that does not name it back, or two of one polarity,
  are refused as before.

Synthetic names only; nothing is downloaded and no Console runs.
"""

from __future__ import annotations

from pathlib import Path

from msdial_app import curated_selection as cs

from test_curated_added_only_and_polarity_split import (
    SWITCHING, SWITCHING_ARCHIVE, _Case, _abstention, _split, _triage,
)
from test_curated_selection import _manifest

POSITIVE_UNIT, NEGATIVE_UNIT = "AN-SYN-SWP", "AN-SYN-SWN"


def _partner_split(ion: str, unit_id: str, partner: object) -> dict:
    return {**_split(ion, unit_id), "partner_unit_id": partner}


class APartnerField(_Case):
    def own(self, partner: object, *, unit_id: str = POSITIVE_UNIT):
        handoff, path, sha = self.handoff(unit_id, "Positive", SWITCHING, SWITCHING_ARCHIVE,
                                          class_proposal=_abstention(unit_id, [Path(n).stem for n in SWITCHING]))
        triage = _triage(sha, unit_id, SWITCHING, [0, 1, 2], ion="positive", row_ion="switching",
                         polarity_split=_partner_split("positive", unit_id, partner))
        return triage, handoff, path

    def test_a_partner_or_null_is_accepted_recorded_and_keyed_as_a_split(self) -> None:
        for partner in (NEGATIVE_UNIT, None):
            with self.subTest(partner=partner):
                triage, handoff, path = self.own(partner)
                project, _workspace, _triage_path = self.project(triage, handoff, path)
                self.assertEqual(f"{POSITIVE_UNIT}-pos", project["analysis_unit_id"])
                self.assertEqual("Positive", project["ion_mode"])
                split = project["repository_metadata"]["curated_selection"]["polarity_split"]
                self.assertEqual(partner, split["partner_unit_id"])
                self.assertEqual(POSITIVE_UNIT, split["parent_unit_id"])
                self.assertEqual(f"{POSITIVE_UNIT}-pos", cs.unit_key(triage))

    def test_without_the_field_nothing_is_recorded_for_it(self) -> None:
        handoff, path, sha = self.handoff(POSITIVE_UNIT, "Positive", SWITCHING, SWITCHING_ARCHIVE,
                                          class_proposal=_abstention(POSITIVE_UNIT, [Path(n).stem for n in SWITCHING]))
        triage = _triage(sha, POSITIVE_UNIT, SWITCHING, [0, 1, 2], ion="positive", row_ion="switching",
                         polarity_split=_split("positive", POSITIVE_UNIT))
        self.assertNotIn("partner_unit_id", cs.polarity_split_of(triage))

    def test_a_malformed_partner_is_refused_with_the_split_code(self) -> None:
        for name, partner in {"own unit": POSITIVE_UNIT, "empty": " ", "number": 7, "list": [NEGATIVE_UNIT]}.items():
            with self.subTest(name):
                triage, handoff, path = self.own(partner)
                self.refused(cs.CURATED_SELECTION_POLARITY_SPLIT_INVALID, triage, handoff, path)


class PartnersInTheCrossUnitCheck(_Case):
    def pair(self, positive_partner: object, negative_partner: object, *, negative_ion: str = "negative"):
        headers = {member: "PolaritySwitching" for member in SWITCHING}
        mode = negative_ion.capitalize()
        positive = _manifest(f"{POSITIVE_UNIT}-pos", "Positive", "HILIC", SWITCHING, headers=headers)
        negative = _manifest(f"{NEGATIVE_UNIT}-{negative_ion[:3]}", mode, "HILIC", SWITCHING, headers=headers)
        positive["project"]["repository_metadata"]["curated_selection"]["polarity_split"] = _partner_split(
            "positive", POSITIVE_UNIT, positive_partner)
        negative["project"]["repository_metadata"]["curated_selection"]["polarity_split"] = _partner_split(
            negative_ion, NEGATIVE_UNIT, negative_partner)
        return positive, negative

    def test_partners_naming_each_other_pass_over_their_shared_files(self) -> None:
        result = cs.cross_unit_attribution(list(self.pair(NEGATIVE_UNIT, POSITIVE_UNIT)))
        self.assertEqual("passed", result["verdict"], result)

    def test_a_partner_not_named_back_is_refused_as_before(self) -> None:
        for name, (mine, theirs) in {"one-sided": (NEGATIVE_UNIT, None), "a third unit": (NEGATIVE_UNIT, "AN-OTHER"),
                                     "neither": (None, None)}.items():
            with self.subTest(name):
                result = cs.cross_unit_attribution(list(self.pair(mine, theirs)))
                self.assertEqual("refused", result["verdict"], result)

    def test_partners_of_one_polarity_are_not_partners(self) -> None:
        positive, other = self.pair(NEGATIVE_UNIT, POSITIVE_UNIT, negative_ion="positive")
        self.assertFalse(cs.polarity_split_siblings(
            {"analysis_unit_id": "a", "polarity_split": positive["project"]["repository_metadata"]["curated_selection"]["polarity_split"]},
            {"analysis_unit_id": "b", "polarity_split": other["project"]["repository_metadata"]["curated_selection"]["polarity_split"]}))
        result = cs.cross_unit_attribution([positive, other])
        self.assertEqual("refused", result["verdict"], result)


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()

"""The attribution routes R1-R4 and R6 of the stage3b triage (2026-11-15).

The triage of the 162 attribute failures of campaign stage3b-poola-20261106 found the matcher reading Metabolomics
Workbench's joined RAW_FILE_NAME cells whole (R1), deciding polarity after one-to-one (R3), not reading a trailing
+ or - (R4), and never reading a row's sample_id (R6); a declared .raw deposited as its .mzML (R2) was paired by
0.5.60. The author decided on 2026-11-15 that the routes are fixed only where the files exist, whole-token,
polarity-scoped and one to one.

Every name here is synthetic, shaped like the triage's examples. No raw data.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app.repository_analysis_rows import build_repository_analysis_rows
    from msdial_app.repository_reanalysis import (
        CONVERTED_MEMBER_STEM_PAIRING,
        JOINED_NAME_OF_ANOTHER_UNIT,
        JOINED_NAMES_ANOTHER_UNIT,
        JOINED_NAMES_SEVERAL_SAMPLES,
        LEADING_IDENTIFIER_TOKEN_PAIRING,
        MARKED_MEMBER_STEM_PAIRING,
        PREFIXED_MEMBER_PAIRING,
        SAMPLE_ID_MEMBER_STEM_PAIRING,
        RepositoryProject,
        _file_key,
        _member_name_pairings,
        _sample_file_names,
        name_polarities,
        polarity_marker,
        sample_raw_file_reading,
        split_joined_raw_file,
    )

from test_replicate_injections import UNIT_SCOPED, _st001264, _Workspace


def _project(rows: list[tuple[str, str]], ion_mode: str = "Positive", chromatography: str = "") -> RepositoryProject:
    project, _ = _st001264([], rows)
    project.ion_mode = ion_mode
    if chromatography:
        project.repository_metadata = {"catalog_handoff": {"technical_settings": {"chromatography": chromatography}}}
    return project


def _reading(raw_file: str, ion_mode: str = "Positive", chromatography: str = "") -> dict:
    return sample_raw_file_reading({"sample_id": "s", "raw_file": raw_file}, _project([], ion_mode, chromatography))


class _Pairings(_Workspace):
    def pairings(
        self, members: list[str], samples: list[tuple[str, str]], ion_mode: str = "Positive", **changes
    ) -> dict:
        """_member_name_pairings over these member names: {"paired": {member: (declared, rule)}, "refused": [...]}."""
        data_root = self.root / "raw" / "data"
        for name in members:
            path = data_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"raw bytes")
        project, _ = _st001264(members, samples)
        project.ion_mode = ion_mode
        for key, value in changes.items():
            setattr(project, key, value)
        extracted = {_file_key(str(data_root / name)): {"path": name} for name in members}
        found = _member_name_pairings(project, extracted, data_root)
        names = {_file_key(str(data_root / name)): name for name in members}
        return {
            "paired": {names[key]: (item["declared_raw_file"], item["paired_by"]) for key, item in found["paired"].items()},
            "refused": found["refused"],
        }


# ---- R1: a joined raw_file cell -----------------------------------------------------------------------------------


class AJoinedCellIsSplitAndReadForTheUnit(unittest.TestCase):
    def test_a_cell_is_split_on_semicolons_and_on_whitespace_between_file_names(self) -> None:
        self.assertEqual((["pRPLC_1", "nRPLC_1"], True), split_joined_raw_file("pRPLC_1; nRPLC_1"))
        self.assertEqual((["NEG_D1.mzML", "POS_D1.mzML"], True), split_joined_raw_file("NEG_D1.mzML POS_D1.mzML"))
        self.assertEqual(
            (["Neg_Bk_1(1).raw", "Neg_Bk_1(1).mzML"], True), split_joined_raw_file("Neg_Bk_1(1).raw; -; Neg_Bk_1(1).mzML")
        )

    def test_a_name_holding_a_space_is_one_name(self) -> None:
        """ST004945's Pos _QC1.wiff and ST002823's Ctrl1- Fecal.wiff: not every word ends in a file suffix."""
        self.assertEqual((["Pos _QC1.wiff"], False), split_joined_raw_file("Pos _QC1.wiff"))
        self.assertEqual((["Ctrl1- Fecal.wiff"], False), split_joined_raw_file("Ctrl1- Fecal.wiff"))
        self.assertEqual((["1 HIGF-1"], False), split_joined_raw_file("1 HIGF-1"))
        self.assertEqual(([], False), split_joined_raw_file("-"))

    def test_the_unit_keeps_the_names_of_its_own_polarity(self) -> None:
        self.assertEqual(["POS_D1.mzML"], _reading("NEG_D1.mzML POS_D1.mzML")["names"])
        self.assertEqual(["NEG_D1.mzML"], _reading("NEG_D1.mzML POS_D1.mzML", "Negative")["names"])
        reading = _reading("Neg_X.raw; Pos_X.raw; Neg_X.mzML; Pos_X.mzML", "Negative")
        # Two encodings of one sample: the one encoding rule chooses between them.
        self.assertEqual(["Neg_X.raw", "Neg_X.mzML"], reading["names"])
        self.assertEqual({"Pos_X.raw", "Pos_X.mzML"}, {item["name"] for item in reading["dropped"]})

    def test_the_unit_keeps_the_names_of_its_own_chromatography(self) -> None:
        cell = (
            "190201MR_PET_Hilic_neg_015; 190128MR_PET_RP_pos_015; 190122MR_PET_RP_neg_021; 190130MR_PET_Hilic_pos_015"
        )
        self.assertEqual(["190122MR_PET_RP_neg_021"], _reading(cell, "Negative", "Reversed phase")["names"])
        self.assertEqual(["190201MR_PET_Hilic_neg_015"], _reading(cell, "Negative", "HILIC")["names"])

    def test_compound_tokens_state_a_polarity_and_a_chromatography(self) -> None:
        """ST001922's RPPOS and ST002260's pRPLC and nRPLC."""
        cell = "ELN_MAC_HILIC_NEG_MAC_1; ELN_MAC_HILIC_POS_MAC_1; ELN_MAC_RPPOS_MAC_1"
        self.assertEqual(["ELN_MAC_RPPOS_MAC_1"], _reading(cell, "Positive", "Reversed phase")["names"])
        self.assertEqual(["pRPLC_1"], _reading("pRPLC_1; nRPLC_1", "Positive", "Reversed phase")["names"])
        self.assertEqual(["nRPLC_1"], _reading("pRPLC_1; nRPLC_1", "Negative")["names"])

    def test_a_cell_whose_names_for_the_unit_are_two_samples_gives_no_name(self) -> None:
        """ST002044 states no chromatography: D_10.mzML may be the other column's positive run. Never a guess."""
        reading = _reading("D_10.mzML D_10NEG.mzML D10_POS.mzML D10_NEG.mzML", "Positive", "Reversed phase")
        self.assertEqual(([], JOINED_NAMES_SEVERAL_SAMPLES), (reading["names"], reading["reason"]))
        # A unit of both polarities keeps both names of its chromatography, two samples, and is refused too.
        both = _reading("X_HILIC_NEG_1; X_HILIC_POS_1; X_RPPOS_1", "Both", "HILIC")
        self.assertEqual(([], JOINED_NAMES_SEVERAL_SAMPLES), (both["names"], both["reason"]))

    def test_a_cell_naming_only_the_other_polarity_is_another_units_row(self) -> None:
        reading = _reading("-; Pos_Bk_1.raw; -; Pos_Bk_1.mzML", "Negative")
        self.assertEqual(([], JOINED_NAMES_ANOTHER_UNIT), (reading["names"], reading["reason"]))

    def test_a_cell_of_one_name_is_read_as_it_always_was(self) -> None:
        reading = _reading("QC_neg_01.raw", "Positive")
        self.assertEqual((["QC_neg_01.raw"], False), (reading["names"], reading["joined"]))

    def test_the_sample_names_are_the_units_own(self) -> None:
        project = _project([("D1", "NEG_D1.raw POS_D1.raw"), ("D2", "NEG_D2.raw POS_D2.raw")], "Positive")
        exact, stems = _sample_file_names(project)
        self.assertEqual({"pos_d1.raw", "pos_d2.raw"}, exact)
        self.assertEqual(set(), stems)


class TheLeaseRunsAJoinedCellsOwnFile(_Workspace):
    MEMBERS = ["NEG_D1.raw", "POS_D1.raw", "NEG_D2.raw", "POS_D2.raw", "NEG_D3.raw", "POS_D3.raw"]
    ROWS = [("DMSO-1", "NEG_D1.raw POS_D1.raw"), ("DMSO-2", "NEG_D2.raw POS_D2.raw"), ("DMSO-3", "NEG_D3.raw POS_D3.raw")]

    def test_an_st003720_shaped_positive_unit_runs_its_positive_files_and_the_csv_finds_their_rows(self) -> None:
        project, payloads = _st001264(self.MEMBERS, self.ROWS)
        manifest = self.lease(project, payloads)

        self.assertEqual(["POS_D1.raw", "POS_D2.raw", "POS_D3.raw"],
                         sorted(Path(item).name for item in manifest["input_candidates"]))
        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        self.assertEqual([("DMSO-1", 0), ("DMSO-2", 1), ("DMSO-3", 2)],
                         sorted((row["sample_id"], row["sample_row_index"]) for row in built["rows"]))

    def test_a_unit_scoped_archive_never_takes_the_other_polaritys_files_as_unattributed(self) -> None:
        project, payloads = _st001264(self.MEMBERS, self.ROWS)
        project.download_scope = dict(UNIT_SCOPED)
        manifest = self.lease(project, payloads)

        self.assertEqual(["POS_D1.raw", "POS_D2.raw", "POS_D3.raw"],
                         sorted(Path(item).name for item in manifest["input_candidates"]))
        left_out = manifest["unattributed_members"]["left_out"]
        self.assertEqual({(f"NEG_D{index}.raw", JOINED_NAME_OF_ANOTHER_UNIT) for index in (1, 2, 3)},
                         {(item["member_name"], item["reason"]) for item in left_out})


# ---- R2: a declared .raw deposited as its .mzML (0.5.60), with no unique leading key ------------------------------


class ADeclaredRawDepositedAsItsMzmlIsPairedWithoutAUniqueKey(_Pairings):
    def test_an_st005052_shaped_unit_whose_rows_share_their_key_is_paired_by_stem(self) -> None:
        rows = [(f"LV{index}", f"POS_Ig04GST_LV{index}.raw") for index in (10, 11, 12)]
        members = [f"POS_Ig04GST_LV{index}.mzML" for index in (10, 11, 12)] + ["POS_Ig04GST_Blank1.mzML"]
        found = self.pairings(members, rows)

        self.assertEqual(
            {f"POS_Ig04GST_LV{index}.mzML": (f"POS_Ig04GST_LV{index}.raw", CONVERTED_MEMBER_STEM_PAIRING)
             for index in (10, 11, 12)},
            found["paired"],
        )


# ---- R3: another polarity's claimant is dropped before one to one -------------------------------------------------


class AnotherPolaritysClaimantIsDroppedFirst(_Pairings):
    def test_an_st003616_shaped_name_is_paired_with_its_own_polaritys_member(self) -> None:
        members = ["For NIH backup/Project2_Neg_P-Batch-1-Preterm-1.mzML",
                   "For NIH backup/Project2_POS_P-Batch-1-Preterm-1.mzML"]
        found = self.pairings(members, [("p1", "P-Batch-1-Preterm-1")], ion_mode="Negative")

        self.assertEqual({members[0]: ("P-Batch-1-Preterm-1", PREFIXED_MEMBER_PAIRING)}, found["paired"])
        self.assertEqual(
            [("polarity_token_contradicts_ion_mode", PREFIXED_MEMBER_PAIRING)],
            [(item["reason"], item["rule"]) for item in found["refused"]],
        )

    def test_an_st003700_shaped_declared_mzml_is_paired_with_its_own_polaritys_member(self) -> None:
        members = ["221026_SVC_Panel_NEG_S01.mzML", "221026_SVC_Panel_POS_S01.mzML"]
        found = self.pairings(members, [("S01", "S01.mzML")], ion_mode="Positive")
        self.assertEqual({members[1]: ("S01.mzML", PREFIXED_MEMBER_PAIRING)}, found["paired"])

    def test_with_no_stated_polarity_both_claimants_still_pair_neither(self) -> None:
        members = ["221026_SVC_Panel_NEG_S01.mzML", "221026_SVC_Panel_POS_S01.mzML"]
        self.assertEqual({}, self.pairings(members, [("S01", "S01.mzML")], ion_mode="Unknown")["paired"])

    def test_the_token_rule_drops_another_polaritys_member_before_uniqueness(self) -> None:
        """VV_13's key names one positive and one negative member: the negative one no longer makes it not unique."""
        members = ["VV_13_HEpG2_C1_exp344_pos.raw", "VV_13_HEpG2_C1_exp344_neg.raw"]
        found = self.pairings(members, [("c1", "VV_13_HEpG2_C1.raw")], ion_mode="Positive")
        self.assertEqual(
            {"VV_13_HEpG2_C1_exp344_pos.raw": ("VV_13_HEpG2_C1.raw", LEADING_IDENTIFIER_TOKEN_PAIRING)}, found["paired"]
        )


# ---- R4: a trailing polarity marker -------------------------------------------------------------------------------


class ATrailingMarkerIsAPolarity(_Pairings):
    def test_the_marker_is_read_only_at_the_end_of_the_stem(self) -> None:
        self.assertEqual(("ds1-93-001", "Positive"), polarity_marker("DS1-93-001+.raw"))
        self.assertEqual(("f2-27-01", "Negative"), polarity_marker("raw data/negative raw/F2-27-01-.raw"))
        self.assertEqual(("ag-88-1", "Negative"), polarity_marker("AG-88-1_r1-.raw"))
        for name in ("DS1-93-001.raw", "x--.raw", "-.raw", "+.raw", "a-b.raw", "Ctrl1- Fecal.wiff"):
            self.assertIsNone(polarity_marker(name), name)
        self.assertEqual({"Positive"}, name_polarities("DS1-93-001+.raw", polarity_named=False))
        self.assertEqual({"Negative"}, name_polarities("x/F2-27-01-.raw", polarity_named=False))

    def test_an_st003328_shaped_negative_unit_takes_the_minus_file(self) -> None:
        members = ["DS1-93 raw files/positive/DS1-93-001+.raw", "DS1-93 raw files/negative/DS1-93-001-.raw",
                   "DS1-93 raw files/positive/DS1-93-002+.raw", "DS1-93 raw files/negative/DS1-93-002-.raw"]
        rows = [("AMC A P1", "DS1-93-001"), ("AMC A P2", "DS1-93-002")]
        found = self.pairings(members, rows, ion_mode="Negative")

        self.assertEqual(
            {members[1]: ("DS1-93-001", MARKED_MEMBER_STEM_PAIRING), members[3]: ("DS1-93-002", MARKED_MEMBER_STEM_PAIRING)},
            found["paired"],
        )
        self.assertEqual({"polarity_token_contradicts_ion_mode"}, {item["reason"] for item in found["refused"]})

    def test_a_run_order_suffix_is_part_of_the_marker_and_two_runs_of_one_sample_pair_neither(self) -> None:
        """ST003792: AG-88-1_r1-.raw is AG-88-1's; AG-88-5 has two negative runs and stays for review."""
        members = ["neg/AG-88-1_r1-.raw", "pos/AG-88-1_r5+.raw", "neg/AG-88-5_r2-.raw", "neg/AG-88-5_r3-.raw"]
        found = self.pairings(members, [("1", "AG-88-1"), ("5", "AG-88-5")], ion_mode="Negative")

        self.assertEqual({"neg/AG-88-1_r1-.raw": ("AG-88-1", MARKED_MEMBER_STEM_PAIRING)}, found["paired"])
        self.assertIn(("neg/AG-88-5_r2-.raw", "not_one_to_one"),
                      {(item["member_name"], item["reason"]) for item in found["refused"]})

    def test_one_marked_name_in_two_folders_pairs_neither(self) -> None:
        """ST003184/ST003185: negative global lipids/ and negative fatty acids/ share their names (Catalog's, R7)."""
        members = ["plasma/negative global lipids/DS2-039-001-.raw", "plasma/negative fatty acids/DS2-039-001-.raw",
                   "plasma/positive global lipids/DS2-039-001+.raw"]
        found = self.pairings(members, [("DS2-039-001", "DS2-039-001")], ion_mode="Negative")

        self.assertEqual({}, found["paired"])
        self.assertEqual(
            {("plasma/negative global lipids/DS2-039-001-.raw", "not_one_to_one"),
             ("plasma/negative fatty acids/DS2-039-001-.raw", "not_one_to_one"),
             ("plasma/positive global lipids/DS2-039-001+.raw", "polarity_token_contradicts_ion_mode")},
            {(item["member_name"], item["reason"]) for item in found["refused"]},
        )

    def test_a_marker_that_contradicts_its_folder_is_refused(self) -> None:
        found = self.pairings(["negative/F2-27-01+.raw"], [("1", "F2-27-01")], ion_mode="Positive")
        self.assertEqual({}, found["paired"])


# ---- R6: the row's sample id ---------------------------------------------------------------------------------------


class ARowsSampleIdThatIsAMemberStem(_Pairings):
    MEMBER = "Vdudejasmoke/Fecal samples_Positive/12-5-2020Ctrl1- Fecal.wiff"

    def test_an_st002823_shaped_row_is_paired_by_its_sample_id_in_its_own_polarity(self) -> None:
        rows = [("12-5-2020Ctrl1- Fecal", "Ctrl1- Fecal.wiff")]
        found = self.pairings([self.MEMBER], rows, ion_mode="Positive")
        self.assertEqual({self.MEMBER: ("Ctrl1- Fecal.wiff", SAMPLE_ID_MEMBER_STEM_PAIRING)}, found["paired"])

        refused = self.pairings([self.MEMBER], rows, ion_mode="Negative")
        self.assertEqual({}, refused["paired"])
        self.assertEqual(["polarity_token_contradicts_ion_mode"], [item["reason"] for item in refused["refused"]])

    def test_a_sample_id_is_matched_whole_and_only_where_it_is_one_rows(self) -> None:
        self.assertEqual({}, self.pairings(["S10.raw"], [("S1", ".RAW")])["paired"])
        self.assertEqual({}, self.pairings(["S1.raw"], [("S1", ".RAW"), ("S1", "other.raw")])["paired"])

    def test_a_row_whose_name_a_member_carries_is_never_paired_by_its_sample_id(self) -> None:
        found = self.pairings(["A1.raw", "S1.raw"], [("S1", "A1.raw")])
        self.assertEqual({}, found["paired"])

    def test_a_joined_cell_r1_refused_is_never_read_through_its_sample_id(self) -> None:
        """Two samples for the unit in one cell: which is the row's is unknown, and so is the sample id's file."""
        found = self.pairings(["S1.raw"], [("S1", "A_1_pos.raw; B_1.raw")])
        self.assertEqual({}, found["paired"])

    def test_the_csv_finds_the_row_through_the_sample_id(self) -> None:
        project, payloads = _st001264(["b1_pos1.raw", "b1_pos2.raw", "b1_neg1.raw"],
                                      [("b1_pos1", ".RAW"), ("b1_pos2", ".RAW"), ("b1_neg1", ".RAW")])
        manifest = self.lease(project, payloads)

        self.assertEqual(["b1_pos1.raw", "b1_pos2.raw"], sorted(Path(item).name for item in manifest["input_candidates"]))
        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        self.assertEqual([("b1_pos1", SAMPLE_ID_MEMBER_STEM_PAIRING), ("b1_pos2", SAMPLE_ID_MEMBER_STEM_PAIRING)],
                         sorted((row["sample_id"], row["raw_file_paired_by"]) for row in built["rows"]))


if __name__ == "__main__":
    unittest.main()

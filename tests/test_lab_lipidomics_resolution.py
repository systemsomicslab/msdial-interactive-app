"""Finding the internal standards in a run, the table normalize reads, and checking its numbers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lab_export_fixture import LBM, SAMPLES, candidate, spot, write_candidates, write_export
from msdial_app.lab_lipidomics import (
    InternalStandard,
    LabInputError,
    annotated_lipid_class,
    compare_normalize_stdout,
    load_lipid_class_vocabulary,
    name_equivalent,
    parse_normalize_stdout,
    predict_peak_id_mismatches,
    read_alignment_export,
    read_candidate_table,
    resolution_digest,
    resolve_standards,
    standards_rows,
    verify_normalized,
    write_resolution_tsv,
    write_standards_tsv,
)

TEXT = "internal_standards_pos"


def standard(name: str = "PC 15:0_18:1(d7)", mz: float = 753.6134, rt: float = 9.5,
             adduct: str = "[M+H]+", ontology: str = "PC") -> InternalStandard:
    return InternalStandard(name=name, mz=mz, rt=rt, adduct=adduct, ontology=ontology,
                            mz_text=repr(mz), rt_text=repr(rt))


class Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()


class NameEquivalenceTests(unittest.TestCase):
    def test_chain_and_sum_level_names_of_one_standard_are_equivalent(self) -> None:
        self.assertTrue(name_equivalent("PC 33:1(d7)", "PC 15:0_18:1(d7)"))
        self.assertTrue(name_equivalent("PC 33:1(d7)|PC 15:0_18:1(d7)", "PC 15:0_18:1(d7)"))
        self.assertTrue(name_equivalent("TG 48:1(d7)", "TG 15:0_18:1(d7)_15:0"))
        self.assertTrue(name_equivalent("SM 18:1;2O/18:1(d9)", "SM 18:1;O2/18:1(d9)"))
        self.assertTrue(name_equivalent("Cer 33:1;O2(d7)", "Cer 18:1;O2/15:0(d7)"))

    def test_another_lipid_or_another_label_is_not(self) -> None:
        self.assertFalse(name_equivalent("PC 33:1", "PC 15:0_18:1(d7)"))
        self.assertFalse(name_equivalent("PC 33:1(d9)", "PC 15:0_18:1(d7)"))
        self.assertFalse(name_equivalent("PE 33:1(d7)", "PC 15:0_18:1(d7)"))
        self.assertFalse(name_equivalent("Unknown", "PC 15:0_18:1(d7)"))

    def test_two_chain_resolved_names_must_agree_chain_by_chain(self) -> None:
        # Isobaric endogenous lipids of an unlabelled odd-chain standard, and another labelled isomer.
        self.assertFalse(name_equivalent("TG 51:0|TG 16:0_17:0_18:0", "TG 17:0_17:0_17:0"))
        self.assertFalse(name_equivalent("PC 34:0|PC 16:0_18:0", "PC 17:0_17:0"))
        self.assertFalse(name_equivalent("PC 15:0_18:1(d7)", "PC 16:0_17:1(d7)"))
        self.assertTrue(name_equivalent("TG 51:0|TG 17:0_17:0_17:0", "TG 17:0_17:0_17:0"))
        self.assertTrue(name_equivalent("PC 18:1_15:0(d7)", "PC 15:0_18:1(d7)"))
        # A name that merges some chains into one agrees with the chains it merges.
        self.assertTrue(name_equivalent("TG 15:0_33:1(d7)", "TG 15:0_18:1(d7)_15:0"))
        self.assertFalse(name_equivalent("TG 16:0_32:1(d7)", "TG 15:0_18:1(d7)_15:0"))


class ResolverTests(Workspace):
    def resolve(self, spots, candidates=(), standards=None, samples=SAMPLES, **options):
        export = read_alignment_export(write_export(self.root / "height.txt", spots, samples))
        table = read_candidate_table(write_candidates(self.root / "candidates.tsv", list(candidates)))
        options.setdefault("annotation_rt_tolerance", 1.5)
        return resolve_standards(export, table, standards or [standard()], polarity="Positive", **options)

    def only(self, resolution):
        self.assertEqual(1, len(resolution["standards"]))
        return resolution["standards"][0]

    def test_a_standard_the_lbm_library_annotated_is_resolved_by_it(self) -> None:
        entry = self.only(self.resolve(
            [spot(1, 753.6131, 9.45, "PC 33:1(d7)", method=LBM)],
            [candidate(1, "PC 33:1(d7)|PC 15:0_18:1(d7)")],
        ))
        self.assertEqual(("A", "resolved_lbm", "1", False), (entry["tier"], entry["status"], entry["alignment_id"], entry["needs_confirmation"]))
        self.assertAlmostEqual(-0.3, entry["delta_mz_mda"], places=3)

    def test_a_standard_its_text_row_annotated_is_resolved_by_it(self) -> None:
        entry = self.only(self.resolve(
            [spot(2, 753.613, 9.52, "PC 15:0_18:1(d7)", method=TEXT)],
            [candidate(2, "PC 15:0_18:1(d7)", annotator=TEXT, source="TextDB", ontology="PC")],
        ))
        self.assertEqual(("B", "resolved_text"), (entry["tier"], entry["status"]))

    def test_the_lbm_annotation_comes_first_and_intensity_decides_within_it(self) -> None:
        resolution = self.resolve([
            spot(1, 753.6131, 9.45, "PC 33:1(d7)", method=LBM, cells=[1000, 1000, 0]),
            spot(2, 753.6132, 9.50, "PC 15:0_18:1(d7)", method=TEXT, cells=[9000, 9000, 0]),
            spot(3, 753.6133, 9.40, "PC 15:0_18:1(d7)", method=LBM, cells=[2000, 2000, 0]),
        ], [candidate(1, "PC 33:1(d7)"), candidate(3, "PC 15:0_18:1(d7)")])
        entry = self.only(resolution)
        self.assertEqual(("A", "3"), (entry["tier"], entry["alignment_id"]))
        self.assertEqual(["3", "1", "2"], [item["alignment_id"] for item in entry["candidates"]])

    def test_a_hidden_text_match_needs_the_persons_confirmation(self) -> None:
        entry = self.only(self.resolve(
            [spot(4, 753.6131, 9.5, "PE 34:1", method=LBM)],
            [candidate(4, "PE 34:1"),
             candidate(4, "PC 15:0_18:1(d7)", annotator=TEXT, representative=False, source="TextDB")],
        ))
        self.assertEqual(("C", "resolved_hidden_text", True), (entry["tier"], entry["status"], entry["needs_confirmation"]))
        self.assertEqual("PE 34:1", entry["spot"]["name"])

    def test_a_spot_found_only_by_mz_and_rt_needs_the_persons_confirmation(self) -> None:
        entry = self.only(self.resolve([spot(5, 753.614, 9.6)]))
        self.assertEqual(("D", "resolved_search", True), (entry["tier"], entry["status"], entry["needs_confirmation"]))

    def test_an_lbm_suggestion_is_not_an_lbm_annotation(self) -> None:
        entry = self.only(self.resolve(
            [spot(1, 753.6131, 9.45, "PC 33:1(d7)", method=LBM)],
            [candidate(1, "PC 33:1(d7)", matched=False, suggested=True)],
        ))
        self.assertEqual("D", entry["tier"])

    def test_a_more_intense_spot_of_another_adduct_never_competes(self) -> None:
        # PC d7 as [M+Na]+ at 775.595: same name, same RT, ten times the height.
        resolution = self.resolve([
            spot(1, 753.6131, 9.45, "PC 33:1(d7)", method=LBM, cells=[1000, 1000, 0]),
            spot(6, 775.5953, 9.45, "PC 33:1(d7)", adduct="[M+Na]+", method=LBM, cells=[10000, 10000, 0]),
        ], [candidate(1, "PC 33:1(d7)"), candidate(6, "PC 33:1(d7)")])
        entry = self.only(resolution)
        self.assertEqual("1", entry["alignment_id"])
        self.assertEqual(["6"], [item["alignment_id"] for item in entry["other_observations"]])

    def test_an_isotopic_spot_is_recorded_and_not_chosen(self) -> None:
        entry = self.only(self.resolve([
            spot(7, 753.6134, 9.5, isotope=1, cells=[90000, 90000, 0]),
            spot(8, 753.6135, 9.5, cells=[100, 100, 0]),
        ]))
        self.assertEqual("8", entry["alignment_id"])
        self.assertEqual(["7"], entry["isotopic_spots_in_window"])

    def test_the_blank_does_not_decide_which_spot_is_the_standard(self) -> None:
        entry = self.only(self.resolve([
            spot(9, 753.6134, 9.5, cells=[100, 100, 100000]),
            spot(10, 753.6134, 9.6, cells=[500, 500, 0]),
        ]))
        self.assertEqual("10", entry["alignment_id"])
        self.assertEqual("resolved_search", entry["status"])
        self.assertEqual(["Blank_Pos"], entry["tolerated_zero_height_files"])

    def test_a_standard_with_no_height_in_a_sample_is_invalid(self) -> None:
        resolution = self.resolve([spot(1, 753.6131, 9.45, "PC 33:1(d7)", method=LBM, cells=[1000, 0, 1000])],
                                  [candidate(1, "PC 33:1(d7)")])
        entry = self.only(resolution)
        self.assertEqual(("invalid", ["S2_Pos"]), (entry["status"], entry["zero_height_files"]))
        self.assertEqual({}, resolution["peak_ids"])
        self.assertEqual(["PC 15:0_18:1(d7)"], resolution["invalid"])

    def test_one_spot_cannot_be_two_standards(self) -> None:
        resolution = self.resolve([spot(11, 753.6134, 9.5)], standards=[standard(), standard("PC other(d7)")])
        self.assertEqual(["conflict", "conflict"], [entry["status"] for entry in resolution["standards"]])
        self.assertEqual({}, resolution["peak_ids"])

    def test_the_retention_window_includes_its_edge(self) -> None:
        self.assertEqual("12", self.only(self.resolve([spot(12, 753.6134, 10.0)]))["alignment_id"])
        self.assertEqual("unresolved_missing", self.only(self.resolve([spot(13, 753.6134, 10.01)]))["status"])

    def test_the_mz_window_widens_to_20_ppm_above_500(self) -> None:
        heavy = standard("X(d7)", mz=800.0)
        light = standard("Y(d7)", mz=400.0)
        self.assertEqual("14", self.only(self.resolve([spot(14, 800.0155, 9.5)], standards=[heavy]))["alignment_id"])
        self.assertEqual("unresolved_missing", self.only(self.resolve([spot(15, 400.0105, 9.5)], standards=[light]))["status"])

    def test_ties_are_broken_by_fill_then_closeness_then_id(self) -> None:
        by_fill = self.only(self.resolve([spot(21, 753.6134, 9.5, fill=0.5), spot(22, 753.6134, 9.5, fill=1.0)]))
        self.assertEqual("22", by_fill["alignment_id"])
        by_rt = self.only(self.resolve([spot(23, 753.6134, 9.7), spot(24, 753.6134, 9.6)]))
        self.assertEqual("24", by_rt["alignment_id"])
        by_id = self.only(self.resolve([spot(26, 753.6134, 9.5), spot(25, 753.6134, 9.5)]))
        self.assertEqual("25", by_id["alignment_id"])

    def test_a_standard_missing_from_this_polarity_is_expected_and_one_not_found_is_not(self) -> None:
        resolution = self.resolve([spot(1, 600.0, 9.5)], standard_names=["PC 15:0_18:1(d7)", "FA 18:0(d3)"])
        self.assertEqual(["PC 15:0_18:1(d7)"], resolution["unresolved_missing"])
        self.assertEqual(["FA 18:0(d3)"], resolution["unresolved_expected"])
        self.assertTrue(resolution["warnings"])

    def test_an_lbm_annotation_beyond_the_library_rt_is_used_and_the_rt_called_stale(self) -> None:
        alone = self.only(self.resolve([spot(1, 753.6131, 10.5, "PC 33:1(d7)", method=LBM, cells=[100, 100, 0])],
                                       [candidate(1, "PC 33:1(d7)")]))
        self.assertEqual(("A", "1", False), (alone["tier"], alone["alignment_id"], alone["needs_confirmation"]))
        self.assertTrue(any("stale" in item for item in alone["warnings"]))
        # With another spot inside the internal-standard window, which one is the standard is the person's call.
        resolution = self.resolve([
            spot(1, 753.6131, 10.5, "PC 33:1(d7)", method=LBM, cells=[100, 100, 0]),
            spot(2, 753.6131, 9.5, cells=[9000, 9000, 0]),
        ], [candidate(1, "PC 33:1(d7)")])
        entry = self.only(resolution)
        self.assertEqual(("A", "1", True), (entry["tier"], entry["alignment_id"], entry["needs_confirmation"]))
        self.assertIn("spot(s) 2 lie within", entry["reason"])
        self.assertEqual(["PC 15:0_18:1(d7)"], resolution["needs_confirmation"])
        lookup = {"rows": [{"standard": "PC 15:0_18:1(d7)", "target_class": "PC", "amount": 66.3917}]}
        self.assertEqual(-1, standards_rows(lookup, resolution)[0]["PeakID"])
        accepted = standards_rows(lookup, resolution, accepted=["PC 15:0_18:1(d7)"])[0]
        self.assertEqual((1, False), (accepted["PeakID"], accepted["AssignName"]))

    def test_inside_tier_a_the_spot_in_the_standards_window_beats_a_more_intense_farther_one(self) -> None:
        # The 2026-09-06 positive audit export: ID 275 is the 13C peak of PC 34:2 [M+H]+, 0.88 min from the
        # PG d7 library RT; ID 276 is the spiked PG d7 itself, in the blank too.
        pg = standard("PG 15:0_18:1(d7)", mz=759.58765, rt=8.42, adduct="[M+NH4]+", ontology="PG")
        resolution = self.resolve([
            spot(275, 759.57446, 9.303, "PG 15:0_18:1(d7)", adduct="[M+NH4]+", method=LBM,
                 cells=[134000, 134500, 27]),
            spot(276, 759.58796, 8.345, "PG 33:1(d7)|PG 15:0_18:1(d7)", adduct="[M+NH4]+", method=LBM,
                 cells=[39000, 39500, 30513]),
        ], [candidate(275, "PG 15:0_18:1(d7)"), candidate(276, "PG 33:1(d7)|PG 15:0_18:1(d7)")], standards=[pg])
        entry = self.only(resolution)
        self.assertEqual(("A", "resolved_lbm", "276", False),
                         (entry["tier"], entry["status"], entry["alignment_id"], entry["needs_confirmation"]))
        self.assertEqual(["276", "275"], [item["alignment_id"] for item in entry["candidates"]])

    def test_an_lbm_annotation_beyond_the_annotation_tolerance_is_not_tier_a(self) -> None:
        entry = self.only(self.resolve([
            spot(1, 753.6131, 11.5, "PC 33:1(d7)", method=LBM, cells=[90000, 90000, 0]),
            spot(2, 753.6132, 9.55, "PC 15:0_18:1(d7)", method=TEXT, cells=[1000, 1000, 0]),
        ], [candidate(1, "PC 33:1(d7)"), candidate(2, "PC 15:0_18:1(d7)", annotator=TEXT, source="TextDB")]))
        self.assertEqual(("B", "2"), (entry["tier"], entry["alignment_id"]))
        self.assertEqual(["1"], [item["alignment_id"] for item in entry["other_observations"]])

    def test_a_text_match_below_the_score_cutoff_is_only_a_candidate_needing_confirmation(self) -> None:
        # MS-DIAL names the annotator in the Comment for a suggestion too, and the export drops its prefix.
        resolution = self.resolve([
            spot(1, 753.6131, 9.45, "PC 15:0_18:1(d7)", method=TEXT, cells=[1000, 1000, 0]),
            spot(2, 753.6133, 9.90, "PC 15:0_18:1(d7)", method=TEXT, cells=[50000, 50000, 0]),
        ], [candidate(1, "PC 15:0_18:1(d7)", annotator=TEXT, source="TextDB"),
            candidate(2, "PC 15:0_18:1(d7)", annotator=TEXT, source="TextDB", matched=False, suggested=True)])
        entry = self.only(resolution)
        self.assertEqual(("B", "1", False), (entry["tier"], entry["alignment_id"], entry["needs_confirmation"]))
        self.assertEqual([("1", "B"), ("2", "C")],
                         [(item["alignment_id"], item["tier"]) for item in entry["candidates"]])
        alone = self.only(self.resolve(
            [spot(2, 753.6133, 9.90, "PC 15:0_18:1(d7)", method=TEXT)],
            [candidate(2, "PC 15:0_18:1(d7)", annotator=TEXT, source="TextDB", matched=False, suggested=True)],
        ))
        self.assertEqual(("C", "resolved_hidden_text", True),
                         (alone["tier"], alone["status"], alone["needs_confirmation"]))

    def test_an_isobaric_lipid_with_other_chains_is_not_the_standard(self) -> None:
        tg = standard("TG 17:0_17:0_17:0", mz=902.8174, rt=14.40, adduct="[M+NH4]+", ontology="TG")
        entry = self.only(self.resolve([
            spot(10, 902.8170, 14.40, "TG 51:0|TG 17:0_17:0_17:0", adduct="[M+NH4]+", method=LBM,
                 cells=[2000, 2000, 0]),
            spot(11, 902.8178, 14.55, "TG 51:0|TG 16:0_17:0_18:0", adduct="[M+NH4]+", method=LBM,
                 cells=[9000, 9000, 0]),
        ], [candidate(10, "TG 51:0|TG 17:0_17:0_17:0"), candidate(11, "TG 51:0|TG 16:0_17:0_18:0")], standards=[tg]))
        self.assertEqual(("A", "10"), (entry["tier"], entry["alignment_id"]))

    def test_a_standard_file_without_the_standard_is_allowed_and_does_not_rank(self) -> None:
        samples = [("S1_Pos", "SPF", "Sample"), ("S2_Pos", "GF", "Sample"), ("Mix_Pos", "Mix", "Standard"),
                   ("Blank_Pos", "Blank", "Blank")]
        entry = self.only(self.resolve([spot(1, 753.6131, 9.45, "PC 33:1(d7)", method=LBM, cells=[1000, 1000, 0, 0])],
                                       [candidate(1, "PC 33:1(d7)")], samples=samples))
        self.assertEqual(("resolved_lbm", [], ["Mix_Pos", "Blank_Pos"]),
                         (entry["status"], entry["zero_height_files"], entry["tolerated_zero_height_files"]))
        ranked = self.only(self.resolve([
            spot(2, 753.6134, 9.5, cells=[100, 100, 900000, 0]),
            spot(3, 753.6134, 9.6, cells=[500, 500, 0, 0]),
        ], samples=samples))
        self.assertEqual("3", ranked["alignment_id"])

    def test_a_label_that_disagrees_with_the_library_adduct_is_recorded(self) -> None:
        entry = self.only(self.resolve([spot(3, 753.6134, 9.5, adduct="[M+NH4]+")]))
        self.assertEqual({"label": "[M+NH4]+", "library_adduct": "[M+H]+"}, entry["label_disagreement"])

    def test_the_digest_binds_the_resolution_to_its_inputs(self) -> None:
        first = self.resolve([spot(5, 753.614, 9.6)])
        second = self.resolve([spot(5, 753.614, 9.6, cells=[1001, 1000, 1000])])
        self.assertNotEqual(resolution_digest(first), resolution_digest(second))
        self.assertEqual(resolution_digest(first), resolution_digest(first))

    def test_the_resolution_table_lists_every_standard_and_its_candidates(self) -> None:
        resolution = self.resolve(
            [spot(1, 753.6131, 9.45, "PC 33:1(d7)", method=LBM), spot(2, 753.614, 9.6)],
            [candidate(1, "PC 33:1(d7)")], standard_names=["FA 18:0(d3)"],
        )
        written = write_resolution_tsv(resolution, self.root / "quant" / "is_resolution_pos.tsv")
        rows = (self.root / "quant" / "is_resolution_pos.tsv").read_text(encoding="utf-8").splitlines()
        header = rows[0].split("\t")
        first = dict(zip(header, rows[1].split("\t")))
        self.assertEqual(2, written["rows"])
        self.assertEqual(("PC 15:0_18:1(d7)", "A", "resolved_lbm", "1"),
                         (first["standard"], first["tier"], first["status"], first["alignment_id"]))
        self.assertIn("2 D Unknown", first["candidates"])
        self.assertEqual(64, len(first["height_export_sha256"]))
        self.assertEqual("unresolved_expected", dict(zip(header, rows[2].split("\t")))["status"])


LOOKUP = {"rows": [
    {"standard": "PC 15:0_18:1(d7)", "target_class": "PC", "amount": 66.3917},
    {"standard": "PC 15:0_18:1(d7)", "target_class": "EtherPC", "amount": 66.3917},
    {"standard": "LPC 18:1(d7)", "target_class": "LPC", "amount": 94.56835},
    {"standard": "Cer 18:1;O2/15:0(d7)", "target_class": "Cer-NS", "amount": 94.17435},
    {"standard": "CE 18:1(d7)", "target_class": "CE", "amount": 75.969369150358588},
    {"standard": "LPC 18:1(d7)", "target_class": "Any others", "amount": 94.56835},
    {"standard": "LPC 18:1(d7)", "target_class": "CAR", "amount": 94.56835},
]}
RESOLUTION = {"standards": [
    {"name": "PC 15:0_18:1(d7)", "status": "resolved_lbm", "alignment_id": "10"},
    {"name": "LPC 18:1(d7)", "status": "resolved_search", "alignment_id": "12"},
    {"name": "Cer 18:1;O2/15:0(d7)", "status": "invalid", "alignment_id": None},
    {"name": "CE 18:1(d7)", "status": "unresolved_expected", "alignment_id": None},
]}


class StandardsTableTests(Workspace):
    def test_only_usable_standards_carry_an_id_and_an_accepted_search_is_named(self) -> None:
        rows = standards_rows(LOOKUP, RESOLUTION)
        self.assertEqual([10, 10, -1, -1, -1, -1, -1], [row["PeakID"] for row in rows])
        accepted = standards_rows(LOOKUP, RESOLUTION, accepted=["LPC 18:1(d7)"])
        self.assertEqual([10, 10, 12, -1, -1, 12, 12], [row["PeakID"] for row in accepted])
        self.assertEqual([False, False, True, False, False, True, True], [row["AssignName"] for row in accepted])
        self.assertEqual("75.96936915035859", accepted[4]["Concentration"])

    def test_the_file_is_the_console_layout(self) -> None:
        rows = standards_rows(LOOKUP, RESOLUTION, accepted=["LPC 18:1(d7)"])
        written = write_standards_tsv(rows, self.root / "quant" / "standards_pos.tsv")
        data = (self.root / "quant" / "standards_pos.tsv").read_bytes()
        lines = data.decode("ascii").split("\r\n")
        self.assertEqual("StandardName\tTargetClass\tConcentration\tPeakID\tAssignName", lines[0])
        self.assertEqual("LPC 18:1(d7)\tLPC\t94.56835\t12\ttrue", lines[3])
        self.assertEqual("Cer 18:1;O2/15:0(d7)\tCer-NS\t94.17435\t-1\tfalse", lines[4])
        self.assertEqual(7, written["rows"])

    def test_mismatches_are_predicted_from_the_stored_name(self) -> None:
        table = read_candidate_table(write_candidates(self.root / "c.tsv", [
            candidate(10, "PC 33:1(d7)|PC 15:0_18:1(d7)"),
            candidate(12, "LPC 18:1"),
            candidate(30, "SM 36:2;O2(d9)"),
            candidate(31, "PE 33:1(d7)|PE 15:0_18:1(d7)", matched=False, suggested=True),
            candidate(32, "PG 33:1(d7)", matched=False, suggested=True),
        ]))
        rows = [
            {"StandardName": "PC 15:0_18:1(d7)", "PeakID": 10, "AssignName": False},
            {"StandardName": "LPC 18:1(d7)", "PeakID": 12, "AssignName": True},
            {"StandardName": "SM 18:1;O2/18:1(d9)", "PeakID": 30, "AssignName": False},
            {"StandardName": "PE 15:0_18:1(d7)", "PeakID": 31, "AssignName": False},
            {"StandardName": "PG 33:1(d7)", "PeakID": 32, "AssignName": False},
            {"StandardName": "TG 48:1(d7)", "PeakID": 33, "AssignName": False},
            {"StandardName": "CE 18:1(d7)", "PeakID": -1, "AssignName": False},
        ]
        predicted = predict_peak_id_mismatches(rows, table)
        # A sum-level LBM name does not answer to the chain-level standard name; a suggestion's
        # first part carries its prefix; an unannotated spot answers to nothing.
        self.assertEqual(
            ["LPC 18:1(d7)", "SM 18:1;O2/18:1(d9)", "PG 33:1(d7)", "TG 48:1(d7)"],
            [item["standard"] for item in predicted],
        )
        self.assertTrue(predicted[0]["assign_name"])


NEW_STDOUT = """\
  PC 15:0_18:1(d7) via alignment ID 10, concentration 66.3917
      covers 2 class(es): PC, EtherPC
  LPC 18:1(d7) via annotation, alignment ID 12, concentration 94.56835
      covers 3 class(es): LPC, Any others, CAR
  AMBIGUOUS LPC 18:1(d7): 2 aligned peaks carry this annotation; the most abundant (ID 12) was used
  UNRESOLVED CE 18:1(d7) -- named for 1 class(es): CE
  UNRESOLVED Cer 18:1;O2/15:0(d7) -- named for 1 class(es): Cer-NS
WARNING: 1 standard(s) were taken from an alignment ID that holds a different compound: LPC 18:1(d7) (alignment ID 12 is annotated 'Unknown').
WARNING: 2 internal standard(s) did not resolve: CE 18:1(d7) (for CE), Cer 18:1;O2/15:0(d7) (for Cer-NS). Rows in the lipid classes they cover carry no concentration and say so.
3 row(s) had no standard of their own class and carry no concentration.
Normalized unit: pmol_per_microL_plasma
Dilution factor applied: False
D:\\x\\quant\\pos\\AlignResult_Height.txt
D:\\x\\quant\\pos\\AlignResult_NormalizedHeight.txt
"""
OLD_STDOUT = """\ufeff  PC 15:0_18:1(d7) -> PC via annotation, alignment ID 160, concentration 66.3917
  NOTE LPC 18:1(d7): 2 aligned peaks carry this annotation; the most abundant (ID 90) was used.
  LPC 18:1(d7) -> LPC via annotation, alignment ID 90, concentration 94.56835
  UNRESOLVED CE 18:1(d7) -> CE
  PC 15:0_18:1(d7) -> EtherPC via annotation, alignment ID 160, concentration 66.3917
"""


class NormalizeReportTests(unittest.TestCase):
    def test_the_current_report_is_read(self) -> None:
        parsed = parse_normalize_stdout(NEW_STDOUT)
        self.assertEqual(10, parsed["resolved"]["PC 15:0_18:1(d7)"]["alignment_id"])
        self.assertEqual(["LPC", "Any others", "CAR"], parsed["resolved"]["LPC 18:1(d7)"]["classes"])
        self.assertEqual({"CE 18:1(d7)": ["CE"], "Cer 18:1;O2/15:0(d7)": ["Cer-NS"]}, parsed["unresolved"])
        self.assertEqual([{"standard": "LPC 18:1(d7)", "alignment_id": 12, "annotated": "Unknown"}], parsed["mismatched"])
        self.assertEqual(("pmol_per_microL_plasma", False, 3), (parsed["unit"], parsed["dilution_factor_applied"], parsed["redacted_rows"]))
        self.assertEqual(2, len(parsed["outputs"]))
        self.assertEqual(1, len(parsed["ambiguous"]))
        self.assertEqual([], parsed["other_lines"])

    def test_the_earlier_report_is_read_too(self) -> None:
        parsed = parse_normalize_stdout(OLD_STDOUT)
        self.assertEqual(160, parsed["resolved"]["PC 15:0_18:1(d7)"]["alignment_id"])
        self.assertEqual(["PC", "EtherPC"], parsed["resolved"]["PC 15:0_18:1(d7)"]["classes"])
        self.assertEqual({"CE 18:1(d7)": ["CE"]}, parsed["unresolved"])

    def test_a_standard_the_table_left_unresolved_but_normalize_found_by_name_is_caught(self) -> None:
        # What a Console ignoring --peak-ids-only does: it looks the -1 standard up by name.
        rows = [{"StandardName": "PC 15:0_18:1(d7)", "PeakID": 10}, {"StandardName": "CE 18:1(d7)", "PeakID": -1}]
        report = ("  PC 15:0_18:1(d7) via alignment ID 10, concentration 66.3917\n"
                  "  CE 18:1(d7) via annotation, alignment ID 40, concentration 75.97\n")
        self.assertEqual(["normalize resolved CE 18:1(d7), which the table left unresolved."],
                         compare_normalize_stdout(parse_normalize_stdout(report), rows))

    def test_a_report_that_differs_from_the_table_is_caught(self) -> None:
        rows = [
            {"StandardName": "PC 15:0_18:1(d7)", "PeakID": 10},
            {"StandardName": "LPC 18:1(d7)", "PeakID": 12},
            {"StandardName": "CE 18:1(d7)", "PeakID": -1},
            {"StandardName": "Cer 18:1;O2/15:0(d7)", "PeakID": -1},
        ]
        self.assertEqual([], compare_normalize_stdout(parse_normalize_stdout(NEW_STDOUT), rows))
        rows[1] = {"StandardName": "LPC 18:1(d7)", "PeakID": 13}
        rows[3] = {"StandardName": "Cer 18:1;O2/15:0(d7)", "PeakID": 14}
        problems = compare_normalize_stdout(parse_normalize_stdout(NEW_STDOUT), rows)
        self.assertEqual(2, len(problems))


# True (unrounded) heights per spot over S1, S2 and the Blank; the Height export rounds them.
TRUE_HEIGHTS = {
    10: [30000.4, 28000.2, 0.0],      # PC d7, absent from the blank
    12: [20000.3, 21000.1, 19000.0],  # LPC d7, the Any-others standard
    20: [60001.4, 14000.3, 50.2],     # PC 34:1
    21: [5000.2, 6000.4, 0.0],        # Cer_NS: its standard did not resolve
    22: [800.4, 900.2, 0.0],          # PC, but only a suggestion: Any others
    23: [700.1, 650.3, 0.0],          # a text-library spot whose Ontology is not a VS2 name
    24: [300.3, 310.2, 0.0],          # CAR, quantified by LPC d7 by name
    25: [100.2, 120.4, 30.1],         # unannotated
}
TABLE = [
    {"StandardName": "PC 15:0_18:1(d7)", "TargetClass": "PC", "Concentration": "66.3917", "PeakID": 10},
    {"StandardName": "PC 15:0_18:1(d7)", "TargetClass": "EtherPC", "Concentration": "66.3917", "PeakID": 10},
    {"StandardName": "LPC 18:1(d7)", "TargetClass": "LPC", "Concentration": "94.56835", "PeakID": 12},
    {"StandardName": "Cer 18:1;O2/15:0(d7)", "TargetClass": "Cer-NS", "Concentration": "94.17435", "PeakID": -1},
    {"StandardName": "LPC 18:1(d7)", "TargetClass": "Any others", "Concentration": "94.56835", "PeakID": 12},
    {"StandardName": "LPC 18:1(d7)", "TargetClass": "CAR", "Concentration": "94.56835", "PeakID": 12},
]
STANDARD_OF = {20: 10, 22: 12, 23: 12, 24: 12, 25: 12}
AMOUNT_OF = {10: 66.3917, 12: 94.56835}
ONTOLOGY = {10: "PC", 12: "LPC", 20: "PC", 21: "Cer_NS", 22: "PC", 23: "Cer_NS", 24: "CAR", 25: "null"}
UNIT = "pmol_per_microL_plasma"


class VerificationTests(Workspace):
    def candidates(self):
        return read_candidate_table(write_candidates(self.root / "c.tsv", [
            candidate(10, "PC 33:1(d7)|PC 15:0_18:1(d7)", ontology="PC"),
            candidate(12, "LPC 18:1(d7)", ontology="LPC"),
            candidate(20, "PC 34:1", ontology="PC"),
            candidate(21, "Cer 34:1;O2", ontology="Cer_NS"),
            candidate(22, "PC 36:2", ontology="PC", matched=False, suggested=True),
            candidate(23, "Cer something", ontology="Cer_NS", annotator=TEXT, source="TextDB"),
            candidate(24, "CAR 16:0", ontology="CAR"),
        ]))

    def normalized_cells(self, alignment_id: int, factors=(1.0, 1.0, 1.0)) -> list[str]:
        if alignment_id in AMOUNT_OF:
            return [repr(AMOUNT_OF[alignment_id] * factor) for factor in factors]
        if alignment_id == 21:
            return ["", "", ""]
        standard_id = STANDARD_OF[alignment_id]
        cells = []
        for target, base, factor in zip(TRUE_HEIGHTS[alignment_id], TRUE_HEIGHTS[standard_id], factors):
            cells.append("" if base <= 0 else repr(AMOUNT_OF[standard_id] * target / base * factor))
        return cells

    def write(self, overrides=None, factors=(1.0, 1.0, 1.0)):
        overrides = overrides or {}
        height = [spot(key, 500.0 + key, 5.0, f"spot {key}", ontology=ONTOLOGY[key], unit="Height",
                       cells=[round(value) for value in values]) for key, values in TRUE_HEIGHTS.items()]
        normalized = []
        for key in TRUE_HEIGHTS:
            if key == 21:
                comment = "NOT QUANTIFIED: the Cer_NS standard Cer 18:1;O2/15:0(d7) did not resolve in this alignment"
                item = spot(key, 500.0 + key, 5.0, f"spot {key}", ontology=ONTOLOGY[key], comment=comment,
                            cells=["", "", ""])
            else:
                item = spot(key, 500.0 + key, 5.0, f"spot {key}", ontology=ONTOLOGY[key], unit=UNIT,
                            cells=self.normalized_cells(key, factors))
            item.update(overrides.get(key, {}))
            normalized.append(item)
        return (write_export(self.root / "AlignResult_Height.txt", height),
                write_export(self.root / "AlignResult_NormalizedHeight.txt", normalized))

    def verify(self, *paths, **options):
        return verify_normalized(*paths, TABLE, self.candidates(), unit=UNIT, **options)

    def test_values_that_follow_from_the_unrounded_heights_pass(self) -> None:
        report = self.verify(*self.write())
        self.assertTrue(report["passed"], report["failed_rows"])
        self.assertEqual((8, 5, 1, 2), (report["rows_checked"], report["quantified_rows"],
                                        report["not_quantified_rows"], report["internal_standard_rows"]))
        # PC 34:1's blank cell is empty because PC d7 has no height there; that is recorded.
        self.assertEqual([("20", "Blank_Pos")], [(item["alignment_id"], item["file"]) for item in
                                                 report["emptied_for_missing_standard_height"]])

    def test_the_rounded_heights_alone_would_not_reproduce_the_values_to_a_millionth(self) -> None:
        height_ratio = round(60001.4) / round(30000.4)
        true_ratio = 60001.4 / 30000.4
        self.assertGreater(abs(height_ratio - true_ratio) / true_ratio, 1e-6)

    def test_a_wrong_cell_fails_its_row(self) -> None:
        cells = self.normalized_cells(20)
        cells[0] = repr(float(cells[0]) * 1.01)
        report = self.verify(*self.write({20: {"cells": cells}}))
        self.assertFalse(report["passed"])
        self.assertEqual(["20"], report["failed_ids"])
        self.assertIn("S1_Pos", report["failed_rows"][0]["files"][0])

    def test_a_class_without_its_standard_left_with_numbers_fails(self) -> None:
        # What the Console did before it compared classes through the VS2 conversion.
        cells = [repr(94.56835 * h / b) for h, b in zip(TRUE_HEIGHTS[21][:2], TRUE_HEIGHTS[12][:2])] + ["0"]
        report = self.verify(*self.write({21: {"cells": cells, "Comment": f"Normalized unit: {UNIT}"}}))
        self.assertEqual(["21"], report["failed_ids"])
        self.assertIn("NOT QUANTIFIED", report["failed_rows"][0]["reason"])

    def test_a_row_exported_as_raw_intensity_fails(self) -> None:
        report = self.verify(*self.write({25: {"Comment": "Normalized unit: Intensity"}}))
        self.assertEqual(["25"], report["failed_ids"])

    def test_an_internal_standard_not_at_its_amount_fails(self) -> None:
        report = self.verify(*self.write({10: {"cells": ["66.3917", "66.4", "66.3917"]}}))
        self.assertEqual(["10"], report["failed_ids"])

    def test_a_suggestion_is_quantified_against_any_others_whatever_its_ontology(self) -> None:
        # Normalized against its own class's standard, as an Ontology-based check would expect.
        own = [repr(66.3917 * h / b) for h, b in zip(TRUE_HEIGHTS[22][:2], TRUE_HEIGHTS[10][:2])] + [""]
        report = self.verify(*self.write({22: {"cells": own}}))
        self.assertEqual(["22"], report["failed_ids"])

    def test_a_value_where_the_standard_has_no_height_fails(self) -> None:
        cells = self.normalized_cells(20)
        cells[2] = repr(66.3917 * 50.2 / 1.0)
        report = self.verify(*self.write({20: {"cells": cells}}))
        self.assertEqual(["20"], report["failed_ids"])

    def test_dilution_factors_multiply(self) -> None:
        paths = self.write(factors=(2.0, 1.0, 1.0))
        self.assertTrue(self.verify(*paths, dilution_factors={"S1_Pos": 2.0})["passed"])
        self.assertFalse(self.verify(*paths)["passed"])

    def test_a_row_in_another_unit_fails(self) -> None:
        report = self.verify(*self.write({20: {"Comment": "Normalized unit: pmol_per_mg_tissue"}}))
        self.assertEqual(["20"], report["failed_ids"])
        self.assertIn("pmol_per_mg_tissue", report["failed_rows"][0]["reason"])

    def test_matrices_of_other_samples_fail_the_whole_check(self) -> None:
        height, normalized = self.write()
        text = normalized.read_text(encoding="ascii").replace("\tS2_Pos\t", "\tS3_Pos\t")
        normalized.write_text(text, encoding="ascii", newline="")
        report = self.verify(height, normalized)
        self.assertFalse(report["passed"])
        self.assertEqual([], report["failed_rows"])
        self.assertIn("different samples", report["problems"][0])

    def test_the_candidate_table_is_required(self) -> None:
        with self.assertRaises(LabInputError):
            verify_normalized(*self.write(), TABLE, None, unit=UNIT)

    def test_the_class_normalize_assigns_follows_its_own_rule(self) -> None:
        vocabulary = load_lipid_class_vocabulary()
        self.assertEqual("Cer-NS", annotated_lipid_class({"is_reference_matched": True, "ontology": "Cer_NS", "source": "MspDB"}, vocabulary))
        self.assertEqual("Cer-NS", annotated_lipid_class({"is_reference_matched": True, "ontology": "Cer-NS", "source": "TextDB"}, vocabulary))
        self.assertEqual("Unknown", annotated_lipid_class({"is_reference_matched": True, "ontology": "Cer_NS", "source": "TextDB"}, vocabulary))
        self.assertEqual("Unknown", annotated_lipid_class({"is_reference_matched": False, "ontology": "PC", "source": "MspDB"}, vocabulary))
        self.assertEqual("Undefined", annotated_lipid_class({"is_reference_matched": True, "ontology": "LipidA", "source": "MspDB"}, vocabulary))
        self.assertEqual("Unknown", annotated_lipid_class(None, vocabulary))


class ExportReaderTests(Workspace):
    def test_the_metadata_columns_are_found_by_name(self) -> None:
        export = read_alignment_export(write_export(self.root / "h.txt", [
            spot(1, 753.6131, 9.45, "PC 33:1(d7)", method=LBM, isotope=2, fill=0.5),
        ]))
        item = export["spots"][0]
        self.assertEqual(("1", 753.6131, 9.45, LBM, 2, 0.5), (item["id"], item["mz"], item["rt"], item["method"],
                                                            item["isotope_weight"], item["fill"]))
        self.assertEqual([name for name, _, _ in SAMPLES], export["sample_names"])
        self.assertEqual(["Sample", "Sample", "Blank"], export["file_types"])


if __name__ == "__main__":
    unittest.main()

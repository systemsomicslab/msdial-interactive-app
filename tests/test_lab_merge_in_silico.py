"""The laboratory merges never count an in-silico (predicted-spectrum) match as a reference match.

The MS-DIAL author's decision of 2026-10-28, applied to the QA in 0.5.50 and here to the laboratory merges.
MS-DIAL's candidate table marks an accepted in-silico match is_reference_matched, so the LC-MS metabolomics
merge reads what the match rests on (the candidate table's evidence_source, the export's Evidence source,
code 450, and the libraries the contract searched as predicted spectra). An LBM2 match is rule-based MS/MS
diagnostics and stays a reference match. The lipidomics merge runs only the LBM2 and the internal-standard
text libraries, so no in-silico match reaches it; its tests pin that. Synthetic fixtures only; nothing here
runs MS-DIAL.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab_export_fixture import CANDIDATE_EVIDENCE_COLUMNS, candidate, write_candidates
from lab_metabolomics_fixture import POSITIVE_SPOTS, MetabolomicsBatch, capabilities_patches, write_run

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import lab_lcms_metabolomics as core
    from msdial_app import lab_lcms_metabolomics_agent as agent
    from msdial_app import lab_lipidomics_agent
    from msdial_app import lipidome_merge

GLUTAMINE = "ZDXPYRJPNDTMRX-VKHMYHEASA-N"
CITRULLINE = "RHGKLRLOHDJJDR-BYPYZUCNSA-N"
TRYPTOPHAN = "QIVBCDIJIAJPQS-VIFPVBQESA-N"


class InSilicoRepresentative(unittest.TestCase):
    """What the merge reads as an in-silico match, from each place a Console may say it."""

    def test_the_evidence_source_decides_where_it_is_written(self) -> None:
        read = core.is_in_silico_representative
        self.assertTrue(read({"evidence_source": "InSilico", "annotation_tag_vs1": "430"}))
        self.assertFalse(read({"evidence_source": "ReferenceSpectrum", "annotation_tag_vs1": "450"}))
        # The export's column stands in for a candidates file without one; "null" is none recorded.
        self.assertTrue(read({"annotation_tag_vs1": "430"}, evidence_source="InSilico"))
        self.assertTrue(read({}, evidence_source="null", code="450"))
        self.assertFalse(read({"annotation_tag_vs1": "430"}, evidence_source="null"))

    def test_an_lbm2_match_and_an_acquired_msp_match_are_none(self) -> None:
        read = core.is_in_silico_representative
        in_silico = {"lib4_in_silico"}
        self.assertFalse(read({"annotator_id": "LbmDB: lab", "evidence_source": "RuleBased", "annotation_tag_vs1": "410"},
                              in_silico_annotators=in_silico))
        self.assertFalse(read({"annotator_id": "LbmDB: lab", "annotation_tag_vs1": "410"}, in_silico_annotators=in_silico))
        self.assertFalse(read({"annotator_id": "lib2_msp_strict", "annotation_tag_vs1": "430"},
                              in_silico_annotators=in_silico))
        # A Console before the in-silico code gives 430 and records no evidence: the contract's library says it.
        self.assertTrue(read({"annotator_id": "lib4_in_silico", "annotation_tag_vs1": "430"}, in_silico_annotators=in_silico))

    def test_only_an_msp_of_predicted_spectra_is_an_in_silico_library(self) -> None:
        entries = [
            {"kind": "lbm", "annotator_id": "lbm"},
            {"kind": "msp", "annotator_id": "lib1_in_house", "library_kind": "acquired"},
            # Predicted retention times, acquired spectra: lab_rt_tiers' predicted_rt.
            {"kind": "msp", "annotator_id": "lib2_predicted_rt", "library_kind": ""},
            {"kind": "msp", "annotator_id": "lib3_in_silico", "library_kind": "predicted"},
            {"kind": "msp", "annotator_id": "lib4_glacier", "library_kind": "InSilico"},
        ]
        self.assertEqual({"lib3_in_silico", "lib4_glacier"}, core.in_silico_annotator_ids(entries))

    def test_an_in_silico_ms2_match_is_no_ms2_match_to_the_evidence_rule(self) -> None:
        self.assertEqual(2, core.evidence_level({"ms2_matched": True}))
        self.assertEqual(0, core.evidence_level({"ms2_matched": True, "in_silico": True}))
        self.assertEqual(1, core.evidence_level({"rt_matched": True, "ms2_matched": True, "in_silico": True}))


class MetabolomicsMergeLeavesInSilicoOut(unittest.TestCase):
    """The campaign preset's four tiers, positive only, through the contract, normalize and the merge."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.batch = MetabolomicsBatch(Path(self.directory.name), ("Positive",))
        self.patches = capabilities_patches()
        self.patches.__enter__()
        library = self.batch.root / "lib"
        lbm, in_silico = library / "lab.lbm2", library / "insilico_pos.msp"
        lbm.write_bytes(b"lbm")
        in_silico.write_text("NAME: a predicted record\nPRECURSORMZ: 100\nNum Peaks: 0\n", encoding="ascii")
        self.contract = self.batch.contract(annotation={"preset": "campaign", "libraries": {
            "lbm": str(lbm), "msp": str(self.batch.libraries["no_rt"]), "in_silico": str(in_silico)}})
        entries = agent.load_contract(self.contract)["annotation"]["libraries"]
        ids = {entry["id"]: entry for entry in entries}
        self.lbm = core.console_lbm_annotator_id(lbm)
        self.strict = ids["msp_strict"]["annotator_id"]
        self.in_silico = ids["in_silico"]["annotator_id"]

    def tearDown(self) -> None:
        self.patches.__exit__(None, None, None)
        self.directory.cleanup()

    def spots(self) -> list[dict]:
        spots = [dict(item) for item in POSITIVE_SPOTS if item["alignment_id"] in (1, 2)]
        spots += [
            # An LBM2 lipid, rule-based: a reference match.
            dict(alignment_id=20, mz=760.5851, rt=9.10, name="PC 34:1", method=self.lbm, inchikey="null",
                 evidence_source="RuleBased", code=410, cells=[3000, 3000, 3000, 0]),
            # Glutamine against an acquired spectrum.
            dict(alignment_id=21, mz=147.0764, rt=0.70, name="L-Glutamine", method=self.strict, inchikey=GLUTAMINE,
                 evidence_source="ReferenceSpectrum", code=430, cells=[4000, 4000, 4000, 0]),
            # Citrulline against a predicted spectrum, as a Console with the in-silico code writes it.
            dict(alignment_id=22, mz=176.1030, rt=0.65, name="Citrulline", method=self.in_silico, inchikey=CITRULLINE,
                 evidence_source="InSilico", code=450, cells=[700, 900, 800, 0]),
            # Tryptophan against a predicted spectrum, as a Console before it: 430 and no evidence recorded.
            dict(alignment_id=23, mz=205.0972, rt=3.40, name="L-Tryptophan", method=self.in_silico, inchikey=TRYPTOPHAN,
                 evidence_source="", code=430, cells=[9000, 9000, 9000, 0]),
            # Glutamine again, more intense, against a predicted spectrum.
            dict(alignment_id=25, mz=147.0764, rt=0.72, name="L-Glutamine", method=self.in_silico, inchikey=GLUTAMINE,
                 evidence_source="InSilico", code=450, cells=[40000, 40000, 40000, 0]),
        ]
        return spots

    def merged(self, candidate_evidence: bool, **options) -> tuple[dict, dict[str, dict], dict[str, str]]:
        job = write_run(self.batch, self.contract, "Positive", spots=self.spots(), candidate_evidence=candidate_evidence)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        record = agent.finish_normalize(agent.plan_normalize(
            self.contract, job, resolved["polarities"]["Positive"]["resolution_digest"]))
        self.assertTrue(record["verified"], record.get("verification"))
        report = agent.merge(self.contract, write=True, **options)
        kept = {row["Alignment ID"]: row for row in csv.DictReader(io.StringIO(
            Path(report["outputs"]["metabolome"]["path"]).read_text(encoding="utf-8")))}
        removed = {row["Alignment ID"]: row["Reason"] for row in csv.DictReader(io.StringIO(
            Path(report["outputs"]["removed"]["path"]).read_text(encoding="utf-8")), delimiter="\t")}
        return report, kept, removed

    def test_reference_matched_only_drops_every_in_silico_row_and_counts_them(self) -> None:
        for candidate_evidence in (True, False):
            with self.subTest(candidate_evidence=candidate_evidence):
                report, kept, removed = self.merged(candidate_evidence)
                # Before 0.5.51 the merge kept 20, 22, 23 and 25 (25 winning glutamine from 21 by intensity).
                self.assertEqual(["20", "21"], sorted(kept))
                self.assertEqual({"22": "in-silico matched", "23": "in-silico matched", "25": "in-silico matched"},
                                 {key: value for key, value in removed.items() if key in {"22", "23", "25"}})
                self.assertEqual(3, report["counts"]["in_silico_matched_removed"])
                self.assertEqual(3, report["counts"]["by_polarity"]["Positive"]["in-silico matched"])
                self.assertNotIn("not reference matched", report["counts"]["by_polarity"]["Positive"])
                self.assertEqual(0, report["counts"]["in_silico_matched_kept"])
                self.assertEqual([self.in_silico], report["in_silico_annotators"])
                self.assertIn("in-silico", report["reference_match"])
                # The LBM2 lipid and the acquired match are reference matched, not in silico.
                for spot_id in ("20", "21"):
                    self.assertEqual(("TRUE", "FALSE"), (kept[spot_id]["Reference matched"], kept[spot_id]["In-silico matched"]))

    def test_without_the_filter_the_in_silico_rows_are_kept_in_their_own_column(self) -> None:
        report, kept, removed = self.merged(True, reference_matched_only=False)
        self.assertEqual(["20", "21", "22", "23"], sorted(kept))
        for spot_id in ("22", "23"):
            self.assertEqual(("FALSE", "TRUE"), (kept[spot_id]["Reference matched"], kept[spot_id]["In-silico matched"]))
        # The reference match keeps glutamine over the more intense in-silico one.
        self.assertEqual("duplicate InChIKey first block", removed["25"])
        self.assertEqual(2, report["counts"]["in_silico_matched_kept"])
        self.assertEqual(0, report["counts"]["in_silico_matched_removed"])
        header = next(csv.reader(io.StringIO(Path(report["outputs"]["metabolome"]["path"]).read_text(encoding="utf-8"))))
        self.assertEqual(["Reference matched", "In-silico matched", "Preferred adduct"], header[4:7])

    def test_the_evidence_rule_ranks_an_in_silico_ms2_match_below_an_acquired_one(self) -> None:
        _report, kept, removed = self.merged(True, reference_matched_only=False, duplicate_rule="evidence")
        self.assertIn("21", kept)
        self.assertEqual("duplicate InChIKey first block", removed["25"])

    def test_an_in_silico_match_of_a_standards_name_is_not_tier_a(self) -> None:
        # CUDA's only spot is named by the in-silico library: a library annotation, but no reference match.
        spots = [dict(item) for item in POSITIVE_SPOTS if item["alignment_id"] == 1]
        spots.append(dict(alignment_id=9, mz=341.2799, rt=2.88, name="CUDA", method=self.in_silico,
                          evidence_source="InSilico", code=450, cells=[5000] * 4))
        spots.append(dict(alignment_id=21, mz=147.0764, rt=0.70, name="L-Glutamine", method=self.strict,
                          inchikey=GLUTAMINE, cells=[4000, 4000, 4000, 0]))
        job = write_run(self.batch, self.contract, "Positive", spots=spots, candidate_evidence=True)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        cuda = next(item for item in resolved["polarities"]["Positive"]["summary"]["standards"] if item["name"] == "CUDA")
        self.assertEqual("D", cuda["tier"])
        self.assertTrue(cuda["needs_confirmation"])
        plan = agent.plan_normalize(self.contract, job, resolved["polarities"]["Positive"]["resolution_digest"])
        self.assertNotIn("9", plan["internal_standard_ids"])
        self.assertTrue(any("Spot 9 ('CUDA')" in item and "drops it as in-silico matched" in item
                            for item in plan["warnings"]), plan["warnings"])
        # The LBM2's annotation of a standard is still tier A, as before.
        spots = [dict(item) for item in POSITIVE_SPOTS if item["alignment_id"] == 1]
        spots.append(dict(alignment_id=9, mz=341.2799, rt=2.88, name="CUDA", method=self.lbm,
                          evidence_source="RuleBased", code=410, cells=[5000] * 4))
        job = write_run(self.batch, self.contract, "Positive", spots=spots, candidate_evidence=True)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        cuda = next(item for item in resolved["polarities"]["Positive"]["summary"]["standards"] if item["name"] == "CUDA")
        self.assertEqual("A", cuda["tier"])


class LipidomicsMergeSeesNoInSilicoMatch(unittest.TestCase):
    """The lipidomics merge reads is_reference_matched as written: its runs search no in-silico library."""

    def test_an_lbm2_representative_is_reference_matched_and_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = [candidate(7, "PC 34:1", annotator="LbmDB: lab", source="LbmDB"),
                    candidate(8, "PE 36:2", annotator="LbmDB: lab", source="LbmDB", matched=False, suggested=True)]
            for row in rows:
                row.update(annotation_tag_vs1="410", evidence_source="RuleBased")
            table = write_candidates(Path(directory) / "a.mdcandidate.tsv", rows, CANDIDATE_EVIDENCE_COLUMNS)
            self.assertEqual({"7"}, lipidome_merge.reference_matched_ids(table))

    def test_a_lipidomics_run_takes_no_msp_library(self) -> None:
        # The plan fixes the libraries to the LBM2 (and the internal-standard texts): another library is refused.
        self.assertTrue(lab_lipidomics_agent._same_answer("libraries", {"lbm_path": "a.lbm2"}, {"lbm_path": "a.lbm2"}))
        self.assertFalse(lab_lipidomics_agent._same_answer(
            "libraries", {"lbm_path": "a.lbm2", "msp_path": "insilico.msp"}, {"lbm_path": "a.lbm2"}))
        for key in ("msp_annotators", "msp_path", "msp_annotator_settings_file_path"):
            refused = lab_lipidomics_agent.contract_override_problems({key: "insilico.msp"})
            self.assertTrue(refused["blockers"], key)


if __name__ == "__main__":
    unittest.main()

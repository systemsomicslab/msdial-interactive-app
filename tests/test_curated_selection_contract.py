"""The curated selection's cross-PR contract (WS0, 2026-11-16), pinned with synthetic names only.

The transparency doc (msdial-public-reanalysis#59, docs/llm-assisted-curation.md) is normative for the selection
(section 3.1, msdial-selection-canonical.v1); the campaign runner (#60) imports curated_selection.selection_canonical
from the pinned Interactive. What is pinned here:

- selection_canonical / selection_sha256 reproduce the doc's two worked-example hashes, and the provenance records the
  value computed with the archive listing the decision record binds; an approved manifest naming another value refuses;
- the decision record is found by the triage file's own stem (a runner copy <unit_id>.json, a revision
  <unit_id>.r<N>.json) or by an explicit decision_record_path; the listing it binds must hash as it says;
- a handoff copy is tied to the triaged handoff by comparing it with the triaged bytes, never by an asserted sha256;
- workspace_unit_key gives a reopened unit or a child a folder of its own, validated;
- cross-unit: shared QC and reference injections the coverage gate calls samples are left out (rule S no longer
  refuses units sharing LTRs); a refusal is lifted only over a group holding whom it was against; a new lease and the
  parts of a split carry it; the result keys a runner reads are exactly the documented ones;
- guards a mutation spot-check found untested (S at exactly 0.5, S with one chromatography unknown, C after P, the
  verified-against-asserted handoff check, the hold beside an exclusion, the input_conversions source).

Set MSDIAL_LLM_CURATION_DOC to the doc's path to also read the worked example from the doc itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app import curated_selection as cs
from msdial_app import mcp_server, repository_reanalysis
from msdial_app.archives import ExtractionLimits
from msdial_app.raw_metadata_preflight import decide_disposition
from msdial_app.repository_analysis_rows import ATTRIBUTION_COVERAGE_REFUSALS
from msdial_app.repository_reanalysis import create_download_lease, project_from_dict, read_manifest

from test_curated_selection import (
    ARCHIVE,
    ARCHIVE_BYTES,
    ARCHIVE_URL,
    MEMBERS,
    _Client,
    _Files,
    _handoff,
    _manifest,
    _preflight,
    _triage,
)

# docs/llm-assisted-curation.md section 3.1 at msdial-public-reanalysis#59 eca331f: the worked example and its hashes.
DOC_EXAMPLE = {
    "triage": {"unit_id": "AN000001",
               "rows": [{"row_index": 2, "member_path": "study\\POS\\S02.mzML", "archive": "ST000001_rawdata.zip"},
                        {"row_index": 1, "member_path": "study/POS/S01.mzML", "archive": "ST000001_rawdata.zip"}],
               "added_members": [{"member_path": "study/POS/QC01.mzML", "archive": "ST000001_rawdata.zip"}]},
    "listing": {"study/pos/s01.mzml": {"archive": "ST000001_rawdata.zip", "crc32": "abcdef01", "size": 1234},
                "study/pos/s02.mzml": {"archive": "ST000001_rawdata.zip", "crc32": "0BCDEF2", "size": 5678}},
}
DOC_SHA256_WITH_LISTING = "4c609902ea5315cf4ab6886cbfdd72ca45ff2892c172e4060822975b8ef10804"
DOC_SHA256_WITHOUT_LISTING = "e33f229c94650c9b06bb1fa48c9180923aa029ab38af41ad325e4d131138898f"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TheSelectionIsTheDocsCanonicalForm(unittest.TestCase):
    def test_the_worked_example_gives_the_docs_two_hashes(self) -> None:
        self.assertEqual(DOC_SHA256_WITH_LISTING, cs.selection_sha256(DOC_EXAMPLE["triage"], DOC_EXAMPLE["listing"]))
        self.assertEqual(DOC_SHA256_WITHOUT_LISTING, cs.selection_sha256(DOC_EXAMPLE["triage"], {}))
        self.assertEqual(DOC_SHA256_WITHOUT_LISTING, cs.selection_sha256(DOC_EXAMPLE["triage"]))
        self.assertEqual("msdial-selection-canonical.v1", cs.SELECTION_CANONICAL_FORM)
        self.assertIs(cs.selection_entries, cs.selection_canonical)

    def test_the_entries_are_in_the_docs_order_with_padded_crc32(self) -> None:
        entries = cs.selection_canonical(DOC_EXAMPLE["triage"], DOC_EXAMPLE["listing"])
        self.assertEqual(
            [("study/POS/QC01.mzML", None, None, None), ("study/POS/S01.mzML", "ABCDEF01", 1234, 1),
             ("study/POS/S02.mzML", "00BCDEF2", 5678, 2)],
            [(e["member_path"], e["crc32"], e["size"], e["sample_row_index"]) for e in entries],
        )
        self.assertEqual({"AN000001"}, {e["unit_id"] for e in entries})
        self.assertEqual(
            {"unit_id", "child_key", "member_path", "archive_name", "crc32", "size", "sample_row_index"},
            set(entries[0]),
        )

    def test_a_childs_unit_id_is_bare_and_its_key_is_recorded(self) -> None:
        triage = dict(DOC_EXAMPLE["triage"], children=[
            {"child_key": "pos", "member_paths": ["study/POS/S01.mzML", "study\\POS\\QC01.mzML"]},
        ])
        by_key = cs.selection_canonical(triage, DOC_EXAMPLE["listing"], "pos")
        by_item = cs.selection_canonical(triage, DOC_EXAMPLE["listing"], triage["children"][0])
        self.assertEqual(by_key, by_item)
        self.assertEqual([("AN000001", "pos", "study/POS/QC01.mzML"), ("AN000001", "pos", "study/POS/S01.mzML")],
                         [(e["unit_id"], e["child_key"], e["member_path"]) for e in by_key])
        with self.assertRaises(ValueError):
            cs.selection_canonical(triage, {}, "neg")
        with self.assertRaises(TypeError):
            cs.selection_sha256(triage, "pos")

    def test_the_listing_parser_reads_the_docs_columns(self) -> None:
        data = ("archive\tpath\tsize\tcrc32\textra\n"
                "ST000001_rawdata.zip\tstudy/POS/S01.mzML\t1234\tabcdef01\tx\n"
                "ST000001_rawdata.zip\tstudy\\POS\\S02.mzML\t5678\t0BCDEF2\ty\n").encode("utf-8")
        parsed = cs.parse_archive_listing(data)
        self.assertEqual(DOC_SHA256_WITH_LISTING, cs.selection_sha256(DOC_EXAMPLE["triage"], parsed))
        with self.assertRaises(ValueError):
            cs.parse_archive_listing(b"archive\tpath\tsize\tcrc32\nA\tp\tlarge\t00\n")

    @unittest.skipUnless(os.environ.get("MSDIAL_LLM_CURATION_DOC"), "MSDIAL_LLM_CURATION_DOC not set")
    def test_the_doc_itself_gives_the_same_example_and_hashes(self) -> None:
        text = Path(os.environ["MSDIAL_LLM_CURATION_DOC"]).read_text(encoding="utf-8")
        section = text.split("### 3.1", 1)[1].split("### 3.2", 1)[0]
        example = json.loads(re.search(r"```json selection-example\n(.*?)```", section, re.S).group(1))
        expected = re.search(r"```text selection-example-sha256\n\s*([0-9a-f]{64})", section).group(1)
        without = re.search(r"no listing gives `([0-9a-f]{64})`", section).group(1)
        self.assertEqual(DOC_EXAMPLE, example)
        self.assertEqual(expected, cs.selection_sha256(example["triage"], example["listing"]))
        self.assertEqual(without, cs.selection_sha256(example["triage"], {}))


def _listing_tsv(rows: list[tuple[str, int, str]]) -> bytes:
    lines = ["archive\tpath\tsize\tcrc32"] + [f"{ARCHIVE}\t{path}\t{size}\t{crc}" for path, size, crc in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


class _Recorded(_Files):
    """A triage directory as doc section 2.1 lays it out: <unit>.json, <unit>.record.json, inputs/."""

    UNIT = "AN-SYN-POS"

    def setUp(self) -> None:
        super().setUp()
        self.handoff, self.handoff_path, self.handoff_sha = self.handoff_file(unit_id=self.UNIT, ion_mode="Positive")
        self.triage = _triage(self.handoff_sha, [0, 1, 2], unit_id=self.UNIT)
        self.listing_rows = [(MEMBERS[i][0], 100 + i, f"{i + 10:X}") for i in range(6)]

    def triage_dir(self, base: str = "campaign/triage/STSYN0001", name: str | None = None,
                   listing: bytes | None = None, handoff_input: bool = True, triage: dict | None = None) -> Path:
        triage = triage if triage is not None else self.triage
        directory = self.root / base
        triage_path = self.write(f"{base}/{name or self.UNIT}.json", triage)
        listing_path = directory / "inputs" / "STSYN0001_rawdata.members.tsv"
        listing_path.parent.mkdir(parents=True, exist_ok=True)
        listing_path.write_bytes(listing if listing is not None else _listing_tsv(self.listing_rows))
        handoff_copy = directory / "inputs" / self.UNIT / "handoff.json"
        handoff_copy.parent.mkdir(parents=True, exist_ok=True)
        handoff_copy.write_bytes(self.handoff_path.read_bytes())
        inputs = [{"role": "archive_listing", "path": "inputs/STSYN0001_rawdata.members.tsv",
                   "sha256": _sha(listing_path), "bytes": listing_path.stat().st_size}]
        if handoff_input:
            inputs.append({"role": "catalog_handoff", "path": f"inputs/{self.UNIT}/handoff.json",
                           "sha256": _sha(handoff_copy), "bytes": handoff_copy.stat().st_size})
        self.write(f"{base}/{Path(triage_path).stem}.record.json", {
            "schema": "msdial-llm-decision-record.v1", "decision_id": _sha(triage_path), "kind": "unit_triage",
            "unit_id": self.UNIT, "payload": {"path": triage_path.name, "sha256": _sha(triage_path)},
            "inputs": inputs,
        })
        return triage_path

    def context(self, triage_path: Path, handoff: dict | None = None, handoff_path: Path | None = None, **options):
        return mcp_server._curated_selection_context(
            str(triage_path), handoff or self.handoff, str(handoff_path or self.handoff_path), **options
        )

    def applied(self, triage_path: Path, handoff: dict | None = None, handoff_path: Path | None = None, **options):
        curated = self.context(triage_path, handoff, handoff_path, **options)
        project, _workspace = mcp_server._project_from_analysis_unit_handoff(
            handoff or self.handoff, curated_selection=curated
        )
        return project["repository_metadata"]["curated_selection"], curated

    def refuses(self, code: str, call, *args, **kwargs) -> cs.CuratedSelectionError:
        with self.assertRaises(cs.CuratedSelectionError) as caught:
            call(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        self.assertIn(code, ATTRIBUTION_COVERAGE_REFUSALS)
        return caught.exception


class TheProvenanceSelectionUsesTheRecordsListing(_Recorded):
    def test_crc32_and_size_come_from_the_bound_listing(self) -> None:
        record, curated = self.applied(self.triage_dir())
        listing = cs.parse_archive_listing(_listing_tsv(self.listing_rows))
        self.assertEqual(cs.selection_sha256(self.triage, listing), record["selection_sha256"])
        self.assertNotEqual(cs.selection_sha256(self.triage, {}), record["selection_sha256"])
        self.assertEqual("msdial-selection-canonical.v1", record["selection_canonical_form"])
        self.assertEqual(6, record["selection_listing_entries"])
        self.assertEqual([("inputs/STSYN0001_rawdata.members.tsv", 6)],
                         [(item["path"], item["entries"]) for item in record["triage_record"]["archive_listings"]])
        self.assertIsNone(record["selection_sha256_matches_manifest"])

    def test_a_listing_whose_bytes_changed_or_is_missing_refuses(self) -> None:
        triage_path = self.triage_dir()
        listing = triage_path.parent / "inputs" / "STSYN0001_rawdata.members.tsv"
        listing.write_bytes(_listing_tsv(self.listing_rows[:3]))
        self.refuses(cs.CURATED_SELECTION_RECORD_MISMATCH, self.context, triage_path)
        listing.unlink()
        error = self.refuses(cs.CURATED_SELECTION_RECORD_MISMATCH, self.context, triage_path)
        self.assertIn("decision_record_path", str(error))


class TheApprovedSelectionMustBeThisOne(_Recorded):
    def approval(self, entry: dict) -> Path:
        manifest_path = self.write("campaign/manifest.json", {"schema": "synthetic", "units": [entry]})
        return self.write("campaign/approval.json", {
            "schema": "msdial-campaign-authorization.v1", "approval_id": "approval-syn", "campaign_id": "synthetic",
            "manifest_digest": "sha256:" + _sha(manifest_path), "campaign_manifest_path": str(manifest_path),
            "approved_by": "A. Person", "approved_at": "2026-11-16T10:00:00+09:00",
            "statement": "Run the listed units as planned.", "covers": [1], "units": [self.UNIT],
            "raw_retention_policy": "keep", "libraries": [], "revoked_at": None,
        })

    def test_an_equal_selection_is_recorded_as_matching_and_another_refuses(self) -> None:
        triage_path = self.triage_dir()
        record_path = triage_path.with_name(f"{self.UNIT}.record.json")
        listing = cs.parse_archive_listing(_listing_tsv(self.listing_rows))
        entry = {"unit_key": self.UNIT, "curated_selection": {
            "triage_sha256": _sha(triage_path), "triage_record_sha256": _sha(record_path),
            "selection_sha256": cs.selection_sha256(self.triage, listing)}}
        record, _curated = self.applied(triage_path, campaign_authorization_path=str(self.approval(entry)))
        self.assertTrue(record["selection_sha256_matches_manifest"])
        self.assertEqual("accepted_decision", record["standing"])

        entry["curated_selection"]["selection_sha256"] = cs.selection_sha256(self.triage, {})
        error = self.refuses(cs.CURATED_SELECTION_SELECTION_MISMATCH, self.applied, triage_path,
                             campaign_authorization_path=str(self.approval(entry)))
        self.assertFalse(error.retryable)


class TheDecisionRecordIsFoundByTheTriagesName(_Recorded):
    def test_the_runners_copy_named_by_unit_id_finds_its_record(self) -> None:
        triage_path = self.triage_dir(base=f"campaign/handoffs/{self.UNIT}")
        record, _curated = self.applied(triage_path)
        self.assertEqual(f"{self.UNIT}.record.json", record["triage_record"]["name"])
        self.assertEqual("beside_triage", record["triage_record"]["located"])
        self.assertEqual(_sha(triage_path.with_name(f"{self.UNIT}.record.json")), record["triage_record_sha256"])

    def test_a_revision_reads_its_own_record_never_the_base_one(self) -> None:
        base = self.triage_dir()
        revised = dict(self.triage, rationale="Revised after a person's answer.")
        revision = self.triage_dir(name=f"{self.UNIT}.r2", triage=revised)
        record, _curated = self.applied(revision)
        self.assertEqual(f"{self.UNIT}.r2.record.json", record["triage_record"]["name"])
        revision.with_name(f"{self.UNIT}.r2.record.json").unlink()
        self.assertTrue(base.with_name(f"{self.UNIT}.record.json").is_file())
        self.assertEqual([revision.with_name(f"{self.UNIT}.r2.record.json")],
                         cs.decision_record_candidates(revision, self.UNIT))
        record, _curated = self.applied(revision)
        self.assertEqual("", record["triage_record_sha256"])

    def test_an_explicit_record_path_wins_and_a_missing_one_refuses(self) -> None:
        original = self.triage_dir()
        copy = self.write(f"campaign/handoffs/{self.UNIT}/triage.json", self.triage)
        record, _curated = self.applied(copy, decision_record_path=str(original.with_name(f"{self.UNIT}.record.json")))
        self.assertEqual("explicit", record["triage_record"]["located"])
        self.assertEqual(6, record["selection_listing_entries"])
        self.refuses(cs.CURATED_SELECTION_RECORD_MISMATCH, self.context, copy,
                     decision_record_path=str(self.root / "nowhere.record.json"))

    def test_the_tools_take_decision_record_path(self) -> None:
        original = self.triage_dir()
        copy = self.write(f"campaign/handoffs/{self.UNIT}/elsewhere.json", self.triage)
        planned = mcp_server.msdial_repository_reanalysis_plan(
            "metabolomics_workbench", "STSYN0001", str(self.root / "analysis"),
            analysis_unit_handoff_path=str(self.handoff_path), curated_selection_path=str(copy),
            decision_record_path=str(original.with_name(f"{self.UNIT}.record.json")),
        )
        self.assertEqual(self.UNIT, planned["project"]["analysis_unit_id"])
        plan = mcp_server.msdial_repository_batch_plan(
            workspace_root=str(self.root / "analysis"), analysis_unit_handoff_paths=[str(self.handoff_path)],
            analysis_purpose="Synthetic purpose.",
            curated_selection_paths={self.UNIT: {"path": str(copy), "decision_record_path": str(self.root / "no.json")}},
        )
        self.assertEqual(["curated_selection:" + cs.CURATED_SELECTION_RECORD_MISMATCH],
                         plan["runs"][0]["blocking_reasons"])


class TheHandoffCopyIsComparedWithTheTriagedBytes(_Recorded):
    def copy(self, **changes) -> tuple[dict, Path]:
        copy = json.loads(json.dumps(self.handoff))
        copy.update(changes)
        return copy, self.write("campaign/handoffs/copy.json", copy)

    def test_an_asserted_sha_is_verified_against_the_records_handoff_input(self) -> None:
        triage_path = self.triage_dir()
        copy, copy_path = self.copy(sample_table_path="")
        record, _curated = self.applied(triage_path, copy, copy_path, original_handoff_sha256=self.handoff_sha)
        self.assertEqual("original_read_and_copy_compared", record["catalog_handoff_verification"])
        self.assertEqual("decision_record_input", record["original_handoff"]["source"])

        changed, changed_path = self.copy(technical_settings=dict(self.handoff["technical_settings"], separation="GC"))
        error = self.refuses(cs.CURATED_SELECTION_HANDOFF_MISMATCH, self.applied, triage_path, changed, changed_path,
                             original_handoff_sha256=self.handoff_sha)
        self.assertEqual(["technical_settings"], error.detail["differing_keys"])

    def test_an_assertion_with_nothing_to_compare_is_a_hold_not_a_pass(self) -> None:
        triage_path = self.triage_dir(handoff_input=False)
        copy, copy_path = self.copy(sample_table_path="")
        self.refuses(cs.CURATED_SELECTION_HANDOFF_MISMATCH, self.applied, triage_path, copy, copy_path,
                     original_handoff_sha256=self.handoff_sha)

    def test_the_records_handoff_must_be_the_one_asserted(self) -> None:
        triage_path = self.triage_dir()
        copy, copy_path = self.copy(sample_table_path="")
        self.refuses(cs.CURATED_SELECTION_HANDOFF_MISMATCH, self.applied, triage_path, copy, copy_path,
                     original_handoff_sha256="44" * 32)

    def test_an_asserted_sha_that_is_not_the_original_read_refuses(self) -> None:
        """Mutant of 3f5f472: the asserted-against-verified check removed survived every test."""
        # The same values in other bytes: the copy compares equal with it, but it is not the triaged handoff.
        other = self.root / "handoffs" / "other-original.json"
        other.write_text(json.dumps(self.handoff), encoding="utf-8")
        self.assertNotEqual(self.handoff_sha, _sha(other))
        copy, copy_path = self.copy(sample_table_path="")
        with self.assertRaises(cs.CuratedSelectionError) as caught:
            self.applied(self.triage_dir(), copy, copy_path, original_handoff_sha256=self.handoff_sha,
                         original_handoff_path=str(other))
        self.assertEqual(cs.CURATED_SELECTION_HANDOFF_MISMATCH, caught.exception.code)
        self.assertIn("is not the sha256 of the original handoff read", str(caught.exception))


class AWorkspaceUnitKeyKeepsEarlierRuns(_Files):
    def test_the_name_is_validated(self) -> None:
        good = ("AN-1", "AN-1-pos", "AN-1@stage3c", "AN-1.r2", "AN-1+x", "AN-1_reopen")
        for name in good:
            self.assertEqual(name, repository_reanalysis.validate_workspace_unit_key(name, "AN-1"))
        for name in ("AN-10", "BN-1", "x-AN-1", "AN-1/..", "AN-1\\x", "AN-1..x", "AN-1.", "AN-1 x", "", "AN-1" + "x" * 130,
                     "_dl"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                repository_reanalysis.validate_workspace_unit_key(name, "_dl" if name == "_dl" else "AN-1")

    def test_a_reopened_unit_leases_into_its_own_folder(self) -> None:
        limits = patch.object(repository_reanalysis, "LEASE_EXTRACTION_LIMITS", ExtractionLimits(reserve_bytes=0))
        limits.start()
        self.addCleanup(limits.stop)
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        triage_path = self.write("triage/AN-SYN-POS.json", _triage(sha, [0, 1, 2], unit_id="AN-SYN-POS"))
        manifests = []
        for key in ("", "AN-SYN-POS@reopen-1"):
            project, _workspace = mcp_server._repository_inspection(
                "metabolomics_workbench", "STSYN0001", None, str(path), "127.0.0.1", 0,
                curated_selection_path=str(triage_path), workspace_unit_key=key,
            )
            typed = project_from_dict(project)
            typed.eligible, typed.selection_status, typed.blocking_reasons = True, "eligible", []
            lease = create_download_lease(typed, self.root / "analysis", 10**9,
                                          client=_Client({ARCHIVE_URL: ARCHIVE_BYTES}))
            manifests.append(Path(lease["manifest_path"]))
        first, reopened = manifests
        self.assertEqual("AN-SYN-POS", first.parent.parent.name)
        self.assertEqual("AN-SYN-POS@reopen-1", reopened.parent.parent.name)
        self.assertEqual("prepared", read_manifest(first)["status"])
        self.assertNotIn("previous_manifest", read_manifest(reopened))
        self.assertEqual("AN-SYN-POS", read_manifest(reopened)["project"]["analysis_unit_id"])

    def test_the_batch_plan_names_the_folder_and_blocks_only_a_bad_name(self) -> None:
        handoff, path, sha = self.handoff_file()
        triage = _triage(sha, list(range(6)), ion_mode="both", children=[
            {"child_key": "pos", "ion_mode": "positive", "acquisition_mode": "DDA",
             "member_paths": [MEMBERS[i][0] for i in (0, 1, 2)]},
            {"child_key": "neg", "ion_mode": "negative", "acquisition_mode": "DDA",
             "member_paths": [MEMBERS[i][0] for i in (3, 4, 5)]},
        ])
        triage["excluded_rows"] = []
        triage_path = self.write("triage/AN-SYN-BOTH.json", triage)
        plan = mcp_server.msdial_repository_batch_plan(
            workspace_root=str(self.root / "analysis"), analysis_unit_handoff_paths=[str(path)],
            analysis_purpose="Synthetic purpose.",
            curated_selection_paths={"AN-SYN-BOTH": [
                {"path": str(triage_path), "child_key": "pos", "workspace_unit_key": "AN-SYN-BOTH-pos@reopen-1"},
                {"path": str(triage_path), "child_key": "neg", "workspace_unit_key": "OTHER-neg"},
            ]},
        )
        runs = {run["analysis_unit_id"]: run for run in plan["runs"]}
        self.assertEqual("AN-SYN-BOTH-pos@reopen-1", Path(runs["AN-SYN-BOTH-pos"]["workspace"]).name)
        self.assertEqual("AN-SYN-BOTH-pos@reopen-1", runs["AN-SYN-BOTH-pos"]["workspace_unit_key"])
        self.assertNotIn("workspace_unit_key:invalid", runs["AN-SYN-BOTH-pos"]["blocking_reasons"])
        self.assertIn("workspace_unit_key:invalid", runs["AN-SYN-BOTH-neg"]["blocking_reasons"])
        self.assertFalse(runs["AN-SYN-BOTH-neg"]["ready"])

    def test_a_key_needs_a_handoff(self) -> None:
        with self.assertRaises(ValueError):
            mcp_server._repository_inspection("metabolomics_workbench", "STSYN0001", None, "", "127.0.0.1", 0,
                                              workspace_unit_key="AN-1@x")


# --------------------------------------------------------------------------------------------------------------------
# Cross-unit.


def _shared(unit: str, own: list[str], shared: list[str], role: str = "") -> dict:
    manifest = _manifest(unit, "Positive", "RP", own + shared)
    for row in manifest["project"]["repository_metadata"]["curated_selection"]["rows_kept"]:
        row["role"] = role
    return manifest


class SharedReferenceInjectionsAreNoSamples(unittest.TestCase):
    def test_the_names_the_coverage_gate_calls_samples(self) -> None:
        for name in ("LTR_1.mzML", "SR_1.mzML", "PoolQC_2.mzML", "QCpool1.mzML", "batchQC1.mzML", "PBQC_01.mzML",
                     "MQC1.mzML", "LQC.mzML", "PooledQC_1.mzML", "Solvent_01.mzML", "Water_01.mzML", "MeOH_01.mzML"):
            self.assertTrue(cs.shared_reference_name(name), name)
        for name in ("liver_1.mzML", "plasma_0.mzML", "S1.mzML", "seawater_2.mzML", "SA_01_C18pos.mzML"):
            self.assertFalse(cs.shared_reference_name(name), name)

    def test_units_sharing_their_references_pass_rule_s(self) -> None:
        liver = [f"liver_{i}.mzML" for i in range(4)]
        plasma = [f"plasma_{i}.mzML" for i in range(6)]
        for stem in ("LTR", "PoolQC", "QCpool", "batchQC", "Solvent", "SR"):
            shared = [f"{stem}_{i}.mzML" for i in range(1, 5)]
            for role in ("", "sample", "unknown"):
                with self.subTest(stem=stem, role=role):
                    result = cs.cross_unit_attribution([
                        _shared("AN-LIVER", liver, shared, role), _shared("AN-PLASMA", plasma, shared, role),
                    ])
                    self.assertEqual("passed", result["verdict"], result["units"])
                    self.assertEqual([4, 6], [item["sample_inputs"] for item in result["units"]])


class TheMutantsThatSurvived(_Files):
    """Guards of 3f5f472 that a mutation spot-check removed without any test failing."""

    def test_rule_s_refuses_at_exactly_half(self) -> None:
        result = cs.cross_unit_attribution([
            _manifest("AN-1", "Positive", "RP", ["S1.mzML", "S2.mzML", "S3.mzML", "S4.mzML"]),
            _manifest("AN-2", "Positive", "RP", ["S1.mzML", "S2.mzML", "T3.mzML", "T4.mzML"]),
        ])
        self.assertEqual([cs.CROSS_UNIT_CONTAINED_INPUTS] * 2, [item["reason"] for item in result["units"]])

    def test_rule_s_refuses_when_one_chromatography_is_unknown(self) -> None:
        result = cs.cross_unit_attribution([
            _manifest("AN-RP", "Positive", "RP", ["S1.mzML", "S2.mzML", "S3.mzML", "S4.mzML"]),
            _manifest("AN-UNK", "Positive", "", ["S1.mzML", "S2.mzML", "S3.mzML", "U4.mzML"]),
        ])
        self.assertEqual("", result["units"][1]["chromatography"])
        self.assertEqual([cs.CROSS_UNIT_CONTAINED_INPUTS] * 2, [item["reason"] for item in result["units"]])

    def test_rule_c_compares_a_pair_p_already_refused(self) -> None:
        files = ["S1.mzML", "S2.mzML"]
        result = cs.cross_unit_attribution([
            _manifest("AN-POS-RP", "Positive", "RP", files, headers={name: "Negative" for name in files}),
            _manifest("AN-POS-HILIC", "Positive", "HILIC", files),
        ])
        self.assertIn(cs.CROSS_UNIT_POLARITY_CONTRADICTION, result["units"][0]["reasons"])
        self.assertEqual(["refused", "refused"], [item["verdict"] for item in result["units"]])
        self.assertIn(cs.CROSS_UNIT_INDISTINGUISHABLE_CHROMATOGRAPHY, result["units"][1]["reasons"])
        self.assertEqual(["AN-POS-RP"], result["units"][1]["refused_against"])

    def test_a_cross_unit_hold_beside_an_exclusion_leaves_it_an_exclusion(self) -> None:
        manifest = {"cross_unit_attribution": {"verdict": "refused", "rule": cs.CROSS_UNIT_RULE,
                                               "reason_code": cs.CROSS_UNIT_DUPLICATE_INPUTS,
                                               "reasons": [cs.CROSS_UNIT_DUPLICATE_INPUTS]}}
        disposition = cs.apply_cross_unit_hold(manifest, {"disposition": "exclude", "reasons": ["gc_ms"],
                                                          "assignments": {"a": 1}})
        self.assertEqual("exclude", disposition["disposition"])
        self.assertNotIn("hold", disposition)
        self.assertEqual(["gc_ms"], disposition["reasons"])
        self.assertEqual([cs.CROSS_UNIT_DUPLICATE_INPUTS], disposition["cross_unit_attribution_hold"]["reasons"])

    def test_a_conversion_recorded_only_in_input_conversions_reconciles_to_its_source(self) -> None:
        root = "D:/ws/AN/raw"
        manifest = {
            "input_directory": root + "/data", "raw_directory": root,
            "project": {"repository_metadata": {"curated_selection": {
                "triage_sha256": "t", "analysis_unit_key": "AN",
                "applied_technical": {"ion_mode": "positive", "acquisition_mode": "DDA"},
                "rows_kept": [{"member_path": "POS/S1.mzXML", "row_index": 0, "ion_mode": "positive",
                               "acquisition_mode": "DDA", "basis": "declared"}],
            }}},
            "input_lineage": {"rows": [{"path": root + "/converted/POS/S1.mzML", "kind": "converted"}]},
            "input_conversions": {"records": [{"status": "converted", "output": {"path": root + "/converted/POS/S1.mzML"},
                                               "source": {"path": root + "/data/POS/S1.mzXML"}}]},
            "raw_metadata_preflight": {"summary": {"per_file": [
                {"file": root + "/converted/POS/S1.mzML", "outcome": "ok", "polarity": "Positive",
                 "acquisition_mode": "DDA"},
            ]}},
        }
        block = cs.reconcile_curated_selection(manifest)
        self.assertEqual("agree", block["verdict"], block["files"])
        self.assertEqual("POS/S1.mzXML", block["files"][0]["member_path"])


class _CrossUnitTool(_Files):
    def written(self, unit: str, files: list[str]) -> str:
        manifest = _manifest(unit, "Positive", "RP", files)
        manifest["execution_allowed"] = True
        path = self.root / unit / "provenance" / "run-manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        repository_reanalysis._write_json(path, manifest)
        return str(path)


class ARefusalIsLiftedOnlyOverItsGroup(_CrossUnitTool):
    def test_a_check_leaving_out_the_sibling_keeps_the_refusal(self) -> None:
        a = self.written("AN-A", ["S1.mzML", "S2.mzML", "S3.mzML"])
        b = self.written("AN-B", ["S1.mzML", "S2.mzML", "S3.mzML"])
        c = self.written("AN-C", ["T1.mzML", "T2.mzML"])
        first = mcp_server.msdial_repository_cross_unit_check([a, b])
        self.assertEqual(["passed", "refused"], [item["verdict"] for item in first["units"]])
        self.assertEqual(["AN-A"], read_manifest(b)["cross_unit_attribution"]["refused_against"])

        second = mcp_server.msdial_repository_cross_unit_check([b, c])
        unit_b = second["units"][0]
        self.assertEqual(("refused", True), (unit_b["verdict"], unit_b["prior_refusal_stands"]))
        self.assertEqual([cs.CROSS_UNIT_DUPLICATE_INPUTS], unit_b["reasons"])
        self.assertEqual("refused", second["verdict"])
        stored = read_manifest(b)["cross_unit_attribution"]
        self.assertEqual(("refused", True), (stored["verdict"], stored["prior_refusal_stands"]))
        self.assertIsNotNone(cs.cross_unit_refusal(read_manifest(b)))

    def test_a_check_of_the_whole_group_that_passes_lifts_it(self) -> None:
        a = self.written("AN-A", ["S1.mzML", "S2.mzML", "S3.mzML"])
        b = self.written("AN-B", ["S1.mzML", "S2.mzML", "S3.mzML"])
        mcp_server.msdial_repository_cross_unit_check([a, b])
        repository_reanalysis._write_json(
            Path(b), {**_manifest("AN-B", "Positive", "RP", ["B1.mzML", "B2.mzML", "B3.mzML"]),
                      "cross_unit_attribution": read_manifest(b)["cross_unit_attribution"]},
        )
        again = mcp_server.msdial_repository_cross_unit_check([a, b])
        self.assertEqual(["passed", "passed"], [item["verdict"] for item in again["units"]])
        self.assertIsNone(cs.cross_unit_refusal(read_manifest(b)))


class TheCrossUnitResultKeysAreTheDocumentedOnes(_CrossUnitTool):
    UNIT_KEYS = {
        "index", "manifest_path", "label", "analysis_unit_id", "accession", "polarity", "polarity_basis",
        "chromatography", "sample_inputs", "identity_bases", "listed_contents", "verdict", "reason", "reasons",
        "refused_against", "warnings", "details", "retryable", "prior_refusal_stands",
    }
    BLOCK_KEYS = {"schema", "rule", "verdict", "reason_code", "reasons", "refused_against", "warnings", "details",
                  "retryable", "group", "overlaps", "prior_refusal_stands"}

    def test_the_result_and_the_manifest_block(self) -> None:
        a = self.written("AN-A", ["S1.mzML", "S2.mzML"])
        b = self.written("AN-B", ["S1.mzML", "S2.mzML"])
        result = mcp_server.msdial_repository_cross_unit_check([a, b])
        self.assertEqual({"ok", "recorded", "schema", "rule", "containment_minimum", "verdict", "units", "overlaps"},
                         set(result))
        for item in result["units"]:
            self.assertEqual(self.UNIT_KEYS, set(item))
            self.assertIn(item["verdict"], {"passed", "refused"})
        self.assertEqual({"units", "rule", "basis", "shared", "containment"}, set(result["overlaps"][0]))
        self.assertEqual(self.BLOCK_KEYS, set(read_manifest(b)["cross_unit_attribution"]))
        self.assertEqual({"analysis_unit_id", "polarity", "chromatography", "sample_inputs", "verdict"},
                         set(read_manifest(b)["cross_unit_attribution"]["group"][0]))
        doc = mcp_server.msdial_repository_cross_unit_check.__doc__ or ""
        for key in self.UNIT_KEYS | self.BLOCK_KEYS:
            self.assertIn(key, doc)


class ARefusalTravelsWithTheUnit(_Files):
    def setUp(self) -> None:
        super().setUp()
        limits = patch.object(repository_reanalysis, "LEASE_EXTRACTION_LIMITS", ExtractionLimits(reserve_bytes=0))
        limits.start()
        self.addCleanup(limits.stop)

    def lease(self) -> Path:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        triage_path = self.write("triage/AN-SYN-POS.json", _triage(sha, [0, 1, 2], unit_id="AN-SYN-POS"))
        curated = mcp_server._curated_selection_context(str(triage_path), handoff, str(path))
        project, _workspace = mcp_server._project_from_analysis_unit_handoff(handoff, curated_selection=curated)
        typed = project_from_dict(project)
        typed.eligible, typed.selection_status, typed.blocking_reasons = True, "eligible", []
        lease = create_download_lease(typed, self.root / "analysis", 10**9, client=_Client({ARCHIVE_URL: ARCHIVE_BYTES}))
        return Path(lease["manifest_path"])

    def test_a_new_lease_of_a_refused_unit_keeps_the_refusal(self) -> None:
        manifest_path = self.lease()

        def refuse(current: dict) -> None:
            current["cross_unit_attribution"] = {
                "schema": cs.CROSS_UNIT_SCHEMA, "rule": cs.CROSS_UNIT_RULE, "verdict": "refused",
                "reason_code": cs.CROSS_UNIT_DUPLICATE_INPUTS, "reasons": [cs.CROSS_UNIT_DUPLICATE_INPUTS],
                "refused_against": ["AN-SYN-OTHER"], "retryable": False,
            }
            current["execution_allowed"] = False

        repository_reanalysis.update_manifest(manifest_path, refuse)
        self.assertEqual(manifest_path, self.lease())
        manifest = read_manifest(manifest_path)
        self.assertEqual("refused", manifest["cross_unit_attribution"]["verdict"])
        self.assertEqual(str(manifest_path), manifest["cross_unit_attribution"]["carried_from"]["manifest_path"])
        self.assertFalse(manifest["execution_allowed"])
        held = decide_disposition(_preflight(manifest))
        self.assertTrue(held.get("hold"))
        self.assertIn(cs.CROSS_UNIT_DUPLICATE_INPUTS, held["reasons"])


class TheReconciliationItemShape(unittest.TestCase):
    def test_the_nested_keys_a_runner_reads(self) -> None:
        manifest = _manifest("AN-1", "Positive", "RP", ["S1.mzML"], headers={"S1.mzML": "Negative"})
        block = manifest["project"]["repository_metadata"]["curated_selection"]
        block.update(triage_sha256="t", analysis_unit_key="AN-1",
                     applied_technical={"ion_mode": "positive", "acquisition_mode": "DDA"})
        block["rows_kept"][0].update(row_index=0, ion_mode="positive", acquisition_mode="DDA")
        manifest["raw_metadata_preflight"]["summary"]["per_file"][0].update(outcome="ok", acquisition_mode="DDA")
        result = cs.reconcile_curated_selection(manifest)
        self.assertEqual("disagree", result["verdict"])
        item = result["files"][0]
        self.assertEqual({"file", "member_path", "row_index", "basis", "outcome", "ion_mode", "acquisition_mode",
                          "result", "reasons"}, set(item))
        self.assertEqual({"triage", "triage_basis", "header", "result"}, set(item["ion_mode"]))
        self.assertEqual(("positive", "row", "Negative", "disagree"),
                         tuple(item["ion_mode"][key] for key in ("triage", "triage_basis", "header", "result")))
        self.assertEqual(["polarity"], item["reasons"])

        manifest["raw_metadata_preflight"]["summary"]["per_file"].append(
            {"file": manifest["input_directory"] + "/OTHER/T1.mzML", "outcome": "ok", "polarity": "Positive"}
        )
        stranger = cs.reconcile_curated_selection(manifest)["files"][-1]
        self.assertEqual(("OTHER/T1.mzML", None, "disagree", ["not_in_selection"]),
                         (stranger["file"], stranger["member_path"], stranger["result"], stranger["reasons"]))
        doc = mcp_server.msdial_repository_raw_metadata_preflight.__doc__ or ""
        for key in ("triage_basis", "kept_rows_not_inspected", "not_in_selection", "header_unread_for_inferred_row"):
            self.assertIn(key, doc)


if __name__ == "__main__":
    unittest.main()

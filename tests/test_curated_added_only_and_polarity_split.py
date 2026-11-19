"""Two shapes of curated selection 0.5.70 admits, with synthetic accessions, archives and names only.

- A selection that keeps no Catalog row and keeps added members runs on those members (the shape of Metabolomics
  Workbench ST004700-ST004811, where every Catalog row is an MS1-only sample and only the pooled DDA runs carry
  MS/MS): one input and one sample row per member, typed by its role, in the run's one Class. A selection that keeps
  neither is still refused (curated_selection_empty), and added members beside kept rows are still not admitted.
- A polarity_split child (the author's decision for units acquired with polarity switching: each file analysed once
  as Positive and once as Negative) is validated, keyed <unit_id>-pos / -neg, and a header of both polarities is the
  child's polarity at the preflight; a header of the other polarity alone is refused for that child, and without
  polarity_split a switching header is excluded and holds the unit as before. The two siblings pass the cross-unit
  check over their shared files.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from msdial_app import curated_selection as cs
from msdial_app import mcp_server, repository_reanalysis
from msdial_app.archives import ExtractionLimits
from msdial_app.raw_metadata_preflight import POLARITY_SPLIT_NO_SCANS, decide_disposition
from msdial_app.repository_analysis_rows import ATTRIBUTION_COVERAGE_REFUSALS, build_repository_analysis_rows
from msdial_app.repository_reanalysis import create_download_lease, project_from_dict, read_manifest

from test_curated_selection import MZML, _Client, _Files, _manifest, _zip

ACCESSION = "STSYN0700"
ARCHIVE = "STSYN0700_mzml.zip"
URL = "https://repository.invalid/STSYN0700_mzml.zip"
# Catalog rows: three MS1-only samples. Added members: the pooled DDA run and a blank, which no row names.
ROWS = ["SYN-L1-S1-fullms.mzML", "SYN-L1-S2-fullms.mzML", "SYN-L1-S3-fullms.mzML"]
POOL = "SYN-L1-UnlPool-PosMSMS-1.mzML"
BLANK = "SYN-L1-Solvent-PosMSMS-1.mzML"
ADDED_ARCHIVE = _zip({name: MZML for name in [*ROWS, POOL, BLANK]})
# A polarity-switching unit: every file holds both polarities, and the names state none.
SWITCHING = ["SW/S1_01.mzML", "SW/S2_01.mzML", "SW/S3_01.mzML"]
SWITCHING_ARCHIVE = _zip({name: MZML for name in SWITCHING})


def _abstention(unit_id: str, sample_ids: list[str]) -> dict:
    return {
        "proposal_id": "proposal-1",
        "unit_id": unit_id,
        "status": "accepted",
        "selected_fields": [],
        "rationale": "No declared factor groups the samples.",
        "contrast_definition": {"kind": "abstention", "class_label": "All", "reason": "no_declared_factor"},
        "assignments": [{"sample_id": sample_id, "class_label": "All"} for sample_id in sample_ids],
    }


def _handoff(unit_id: str, ion_mode: str, names: list[str], archive_bytes: bytes, *, class_proposal=None) -> dict:
    return {
        "schema": "msdial-repository-reanalysis-handoff.v1",
        "repository": "metabolomics_workbench",
        "accession": ACCESSION,
        "analysis_unit_id": unit_id,
        "title": ACCESSION,
        "technical_settings": {
            "separation": "LC-MS", "ion_mode": ion_mode, "acquisition_mode": "DDA", "untargeted": True,
            "target_omics": "Metabolomics", "chromatography": "HILIC",
        },
        "files": [{"path": ARCHIVE, "role": "raw", "size_bytes": len(archive_bytes), "checksum": "", "download_url": URL}],
        "download_scope": {"kind": "shared_raw_archive", "file_count": 1, "bundle_bytes": len(archive_bytes)},
        "sample_count": len(names),
        "sample_metadata": [
            {"sample_id": Path(name).stem, "raw_file": Path(name).name, "attributes": {"Group": "All"}} for name in names
        ],
        "class_proposal": class_proposal,
        "blocking_reasons": [],
    }


def _item(name: str, ion: str, role: str) -> dict:
    return {"member_path": name, "archive": ARCHIVE, "role": role, "ion_mode": ion, "chromatography": "HILIC",
            "acquisition_mode": "DDA", "basis": "declared", "confidence": "high",
            "evidence": [{"source": "filename_token", "field": "member_path", "value": name}],
            "rationale": "Synthetic."}


def _triage(handoff_sha: str, unit_id: str, names: list[str], keep: list[int], *, ion: str = "positive",
            row_ion: str | None = None, added: list[dict] | None = None, polarity_split: dict | None = None) -> dict:
    triage = {
        "schema": "msdial-unit-triage.v2",
        "repository": "metabolomics_workbench",
        "accession": ACCESSION,
        "unit_id": unit_id,
        "catalog_handoff_sha256": handoff_sha,
        "catalog_row_count": len(names),
        "standing": "model_inference",
        "phase": "pre_download",
        "decided_by": {"agent": "triage-agent", "model": "synthetic-model", "skill": "msdial-unit-triage",
                       "skill_version": "1.3.0"},
        "decided_at": "2026-11-20T09:00:00+09:00",
        "purpose": None,
        "unit_technical": {"ion_mode": ion, "chromatography": "HILIC", "acquisition_mode": "DDA",
                           "declared": {"ion_mode": "Both", "chromatography": "HILIC", "acquisition_mode": "DDA"}},
        "status": "ready",
        "rows": [
            {"row_index": index, "sample_id": Path(names[index]).stem, "raw_file": Path(names[index]).name,
             **_item(names[index], row_ion or ion, "sample")}
            for index in keep
        ],
        "added_members": list(added or []),
        "excluded_rows": [
            {"row_index": index, "sample_id": Path(names[index]).stem, "raw_file": Path(names[index]).name,
             "member_path": names[index], "reason": "ms1_only_no_msms", "belongs_to_unit": None, "detail": "MS1 only."}
            for index in range(len(names)) if index not in keep
        ],
        "excluded_members": [],
        "shared_with": [],
        "needs_person": [],
        "flags": [],
    }
    if polarity_split is not None:
        triage["polarity_split"] = polarity_split
    return triage


def _split(ion: str, unit_id: str = "AN-SYN-SW") -> dict:
    return {"ion_mode": ion, "reason": "polarity_switching", "parent_unit_id": unit_id,
            "sibling_ion_mode": "negative" if ion == "positive" else "positive"}


def _headers(manifest: dict, polarities: dict[str, str], default: str) -> dict:
    files = list(manifest["input_candidates"])
    manifest["raw_metadata_preflight"] = {
        "declared": {"separation": "LC-MS", "ion_mode": manifest["project"]["ion_mode"], "acquisition_mode": "DDA",
                     "untargeted": True},
        "extractor": {"sha256": "ab" * 32, "pinned": True},
        "summary": {
            "acquisition_mode": "DDA",
            "ion_mode": "Both",
            "per_file": [
                {"file": path, "outcome": "ok", "acquisition_mode": "DDA",
                 "polarity": polarities.get(Path(path).name, default), "ms_levels": [1, 2], "has_ms1": True,
                 "has_ms2": True, "has_ion_mobility": False, "format": "mzML", "separation": "LC"}
                for path in files
            ],
            "coverage": {"inspected": len(files)},
        },
    }
    return manifest


class _Case(_Files):
    def setUp(self) -> None:
        super().setUp()
        limits = patch.object(repository_reanalysis, "LEASE_EXTRACTION_LIMITS", ExtractionLimits(reserve_bytes=0))
        limits.start()
        self.addCleanup(limits.stop)

    def handoff(self, unit_id: str, ion_mode: str, names: list[str], archive_bytes: bytes, **options):
        handoff = _handoff(unit_id, ion_mode, names, archive_bytes, **options)
        path = self.write(f"handoffs/{unit_id}.json", handoff)
        return handoff, path, hashlib.sha256(path.read_bytes()).hexdigest()

    def lease(self, project: dict, archive_bytes: bytes) -> dict:
        typed = project_from_dict(project)
        typed.eligible, typed.selection_status, typed.blocking_reasons = True, "eligible", []
        lease = create_download_lease(typed, self.root / "analysis" / project["analysis_unit_id"], 10**9,
                                      client=_Client({URL: archive_bytes}))
        manifest = read_manifest(lease["manifest_path"])
        manifest["manifest_path"] = lease["manifest_path"]
        return manifest


class ASelectionOfAddedMembersOnly(_Case):
    def added_only(self, *, added=None, keep=(), class_proposal="abstention"):
        proposal = _abstention("AN-SYN-POOL", [Path(name).stem for name in ROWS]) if class_proposal == "abstention" \
            else class_proposal
        handoff, path, sha = self.handoff("AN-SYN-POOL", "Positive", ROWS, ADDED_ARCHIVE, class_proposal=proposal)
        members = [_item(POOL, "positive", "qc"), _item(BLANK, "positive", "blank")] if added is None else added
        triage = _triage(sha, "AN-SYN-POOL", ROWS, list(keep), added=members)
        return triage, handoff, path

    def test_it_runs_on_its_added_members_typed_by_role_in_the_runs_one_class(self) -> None:
        triage, handoff, path = self.added_only()

        project, workspace, _triage_path = self.project(triage, handoff, path)

        self.assertEqual("AN-SYN-POOL", project["analysis_unit_id"])
        self.assertEqual([BLANK, POOL], [item["path"] for item in project["analysis_inputs"]])
        self.assertEqual({True}, {item["triage_added_member"] for item in project["analysis_inputs"]})
        self.assertEqual([None, None], [item["triage_row_index"] for item in project["analysis_inputs"]])
        self.assertEqual(2, project["sample_count"])
        self.assertEqual([Path(BLANK).stem, Path(POOL).stem], [row["sample_id"] for row in project["sample_metadata"]])
        self.assertEqual(["Blank", "QC"], [row["values"]["sample_type"] for row in project["sample_metadata"]])
        self.assertEqual(2, project["download_scope"]["analysis_file_count"])
        self.assertEqual("Positive", project["ion_mode"])
        self.assertEqual({"All"}, {item["class_label"] for item in project["class_proposal"]["assignments"]})
        self.assertEqual(2, len(project["class_proposal"]["assignments"]))
        self.assertEqual(2, len(workspace["rows"]))
        record = project["repository_metadata"]["curated_selection"]
        self.assertEqual("added_members_only", record["admitted_from"])
        self.assertEqual(0, record["rows_kept_count"])
        self.assertEqual(2, record["added_members_admitted_count"])
        self.assertEqual([], record["added_members_not_admitted"])
        self.assertEqual(3, record["rows_dropped_count"])
        self.assertIsNone(record["polarity_split"])

    def test_a_selection_keeping_neither_rows_nor_members_is_still_refused(self) -> None:
        triage, handoff, path = self.added_only(added=[])
        error = self.refused(cs.CURATED_SELECTION_EMPTY, triage, handoff, path)
        self.assertIn("no archive member", str(error))

    def test_added_members_beside_kept_rows_are_still_not_admitted(self) -> None:
        triage, handoff, path = self.added_only(keep=(0, 1, 2))

        project, _workspace, _triage_path = self.project(triage, handoff, path)

        self.assertEqual(ROWS, [item["path"] for item in project["analysis_inputs"]])
        record = project["repository_metadata"]["curated_selection"]
        self.assertEqual("rows", record["admitted_from"])
        self.assertEqual([BLANK, POOL], sorted(item["member_path"] for item in record["added_members_not_admitted"]))

    def test_a_contrast_of_two_levels_keeps_no_side_on_added_members(self) -> None:
        proposal = _abstention("AN-SYN-POOL", [Path(name).stem for name in ROWS])
        proposal["contrast_definition"] = {"kind": "two_group", "groups": ["A", "B"]}
        for item, label in zip(proposal["assignments"], ["A", "B", "B"]):
            item["class_label"] = label
        triage, handoff, path = self.added_only(class_proposal=proposal)
        self.refused(cs.CURATED_SELECTION_CLASS_CONTRAST_LOST, triage, handoff, path)

    def test_the_lease_admits_the_members_and_the_csv_types_them(self) -> None:
        triage, handoff, path = self.added_only()
        project, _workspace, _triage_path = self.project(triage, handoff, path)

        manifest = self.lease(project, ADDED_ARCHIVE)

        self.assertEqual("prepared", manifest["status"], manifest.get("download_failure"))
        self.assertEqual(sorted([POOL, BLANK]), sorted(Path(item).name for item in manifest["input_candidates"]))
        rows = manifest["input_lineage"]["rows"]
        self.assertEqual({"curated_selection"}, {row["selected_by"] for row in rows})
        self.assertEqual({True}, {row.get("triage_added_member") for row in rows})
        self.assertNotEqual("refused", manifest["attribution_coverage"]["status"], manifest["attribution_coverage"])
        built = build_repository_analysis_rows(manifest)
        types = {Path(row["file_path"]).name: (row["file_type"], row["class_id"]) for row in built["rows"]}
        self.assertEqual({POOL: ("QC", "All"), BLANK: ("Blank", "All")}, types)
        block = cs.reconcile_curated_selection(_headers(manifest, {}, "Positive"))
        self.assertEqual("agree", block["verdict"], block)
        self.assertEqual(2, block["agree"])


class APolaritySplitChild(_Case):
    def child(self, ion: str, *, split: dict | None = "default", row_ion: str = "switching",
              unit_ion: str | None = None):
        handoff, path, sha = self.handoff("AN-SYN-SW", "Both", SWITCHING, SWITCHING_ARCHIVE,
                                          class_proposal=_abstention("AN-SYN-SW", [Path(n).stem for n in SWITCHING]))
        triage = _triage(sha, "AN-SYN-SW", SWITCHING, [0, 1, 2], ion=unit_ion or ion, row_ion=row_ion,
                         polarity_split=_split(ion) if split == "default" else split)
        return triage, handoff, path

    def test_a_valid_split_is_its_own_run_at_its_polarity_and_records_the_declaration(self) -> None:
        for ion, suffix, mode in (("positive", "pos", "Positive"), ("negative", "neg", "Negative")):
            with self.subTest(ion=ion):
                triage, handoff, path = self.child(ion)
                project, _workspace, _triage_path = self.project(triage, handoff, path)
                self.assertEqual(f"AN-SYN-SW-{suffix}", project["analysis_unit_id"])
                self.assertEqual(mode, project["ion_mode"])
                split = project["repository_metadata"]["curated_selection"]["polarity_split"]
                self.assertEqual(ion, split["ion_mode"])
                self.assertEqual(mode, split["run_ion_mode"])
                self.assertEqual("Both", split["handoff_declared_ion_mode"])
                self.assertEqual("AN-SYN-SW", split["parent_unit_id"])
                self.assertEqual(f"AN-SYN-SW-{suffix}", cs.unit_key(triage))

    def test_a_malformed_split_is_refused_with_its_own_code(self) -> None:
        cases = {
            "not an object": "positive",
            "same sibling": {**_split("positive"), "sibling_ion_mode": "positive"},
            "no ion mode": {**_split("positive"), "ion_mode": "both"},
            "another reason": {**_split("positive"), "reason": "two_methods"},
            "another parent": {**_split("positive"), "parent_unit_id": "AN-OTHER"},
        }
        for name, split in cases.items():
            with self.subTest(name):
                triage, handoff, path = self.child("positive", split=split)
                self.refused(cs.CURATED_SELECTION_POLARITY_SPLIT_INVALID, triage, handoff, path)
        with self.subTest("unit_technical disagrees"):
            triage, handoff, path = self.child("positive", unit_ion="negative")
            self.refused(cs.CURATED_SELECTION_POLARITY_SPLIT_INVALID, triage, handoff, path)
        self.assertIn(cs.CURATED_SELECTION_POLARITY_SPLIT_INVALID, ATTRIBUTION_COVERAGE_REFUSALS)

    def test_rows_of_the_other_polarity_alone_are_still_refused(self) -> None:
        triage, handoff, path = self.child("positive", row_ion="negative")
        self.refused(cs.CURATED_SELECTION_MIXED_POLARITY, triage, handoff, path)

    def test_without_a_split_a_switching_row_is_refused_as_before(self) -> None:
        triage, handoff, path = self.child("positive", split=None)
        self.refused(cs.CURATED_SELECTION_MIXED_POLARITY, triage, handoff, path)

    def leased(self, ion: str, split: dict | None = "default") -> dict:
        triage, handoff, path = self.child(ion, split=split, row_ion="unknown")
        project, _workspace, _triage_path = self.project(triage, handoff, path)
        return self.lease(project, SWITCHING_ARCHIVE)

    def test_a_switching_header_runs_as_the_childs_polarity(self) -> None:
        for ion, mode in (("positive", "Positive"), ("negative", "Negative")):
            with self.subTest(ion=ion):
                manifest = _headers(self.leased(ion), {}, "PolaritySwitching")
                disposition = decide_disposition(manifest)
                self.assertEqual("run", disposition["disposition"], disposition)
                self.assertEqual(mode, disposition["ion_mode"])
                self.assertEqual(3, len(disposition["assignments"]))
                self.assertEqual({"polarity_split"},
                                 {item["polarity_source"] for item in disposition["assignments"].values()})
                self.assertEqual("agree", disposition["curated_selection_reconciliation"]["verdict"])
                self.assertEqual(3, len(disposition["polarity_split"]["switching_inputs"]))
                self.assertEqual("whole_file_header", disposition["polarity_split"]["scan_basis"])

    def test_without_a_split_a_switching_header_is_excluded_and_disagrees(self) -> None:
        manifest = self.leased("positive", split=None)
        manifest = _headers(manifest, {}, "PolaritySwitching")
        disposition = decide_disposition(manifest)
        self.assertEqual("exclude", disposition["disposition"], disposition)
        self.assertEqual({"polarity_switching"}, {item["reason"] for item in disposition["excluded_inputs"]})
        self.assertEqual("disagree", disposition["curated_selection_reconciliation"]["verdict"])
        self.assertNotIn("polarity_split", disposition)

    def test_a_file_with_no_scans_of_the_childs_polarity_is_refused_for_that_child(self) -> None:
        manifest = _headers(self.leased("positive"), {"S2_01.mzML": "Negative"}, "PolaritySwitching")

        disposition = decide_disposition(manifest)

        self.assertEqual("run", disposition["disposition"], disposition)
        refused = [item for item in disposition["excluded_inputs"] if item["reason"] == POLARITY_SPLIT_NO_SCANS]
        self.assertEqual(["S2_01.mzML"], [Path(item["path"]).name for item in refused])
        self.assertEqual(2, len(disposition["assignments"]))
        self.assertEqual("agree", disposition["curated_selection_reconciliation"]["verdict"])
        self.assertEqual(["S2_01.mzML"], [Path(p).name for p in disposition["polarity_split"]["refused_inputs"]])

        every = _headers(self.leased("negative"), {}, "Positive")
        self.assertEqual("exclude", decide_disposition(every)["disposition"])

    def test_the_preflight_keeps_the_childs_ion_mode_where_the_headers_say_both(self) -> None:
        manifest = _headers(self.leased("negative"), {}, "PolaritySwitching")
        execution = {"outcomes": {}, "records": [], "counts": {}, "chunks": [], "command_template": [], "exit_code": 0}
        summary = dict(manifest["raw_metadata_preflight"]["summary"], separation="LC-MS", evidence=[])
        with patch.object(repository_reanalysis, "_summarize_raw_metadata", return_value=summary), \
                patch.object(repository_reanalysis, "_per_input_entries", side_effect=lambda entries, *_: entries):
            repository_reanalysis._record_preflight(
                manifest, execution=execution, extractor={"sha256": "ab" * 32}, inputs=[], output=self.root / "o.json",
                declared=manifest["raw_metadata_preflight"]["declared"], confirm_untargeted=False, capped=False,
                campaign={"approval_id": "synthetic"}, started_at="2026-11-20T00:00:00+00:00",
            )
        self.assertEqual("Negative", manifest["project"]["ion_mode"])
        self.assertEqual("run", manifest["campaign_disposition"]["disposition"], manifest["campaign_disposition"])
        self.assertEqual("Negative", manifest["campaign_disposition"]["ion_mode"])

    def test_the_siblings_pass_the_cross_unit_check_over_their_shared_files(self) -> None:
        members = SWITCHING
        headers = {member: "PolaritySwitching" for member in members}
        positive = _manifest("AN-SYN-SW-pos", "Positive", "HILIC", members, headers=headers)
        negative = _manifest("AN-SYN-SW-neg", "Negative", "HILIC", members, headers=headers)

        self.assertEqual("refused", cs.cross_unit_attribution([positive, negative])["verdict"])

        for manifest, ion in ((positive, "positive"), (negative, "negative")):
            manifest["project"]["repository_metadata"]["curated_selection"]["polarity_split"] = _split(ion)
        result = cs.cross_unit_attribution([positive, negative])
        self.assertEqual("passed", result["verdict"], result)

    def test_a_batch_plans_both_siblings_from_one_handoff(self) -> None:
        handoff, path, sha = self.handoff("AN-SYN-SW", "Both", SWITCHING, SWITCHING_ARCHIVE,
                                          class_proposal=_abstention("AN-SYN-SW", [Path(n).stem for n in SWITCHING]))
        entries = []
        for ion in ("positive", "negative"):
            triage = _triage(sha, "AN-SYN-SW", SWITCHING, [0, 1, 2], ion=ion, row_ion="switching",
                             polarity_split=_split(ion))
            triage_path = self.write(f"triage/AN-SYN-SW-{ion}.json", triage)
            entries.append({"path": str(triage_path), "workspace_unit_key": f"AN-SYN-SW-{ion[:3]}"})

        plan = mcp_server.msdial_repository_batch_plan(
            workspace_root=str(self.root / "analysis"),
            analysis_unit_handoff_paths=[str(path)],
            analysis_purpose="Synthetic purpose.",
            curated_selection_paths={"AN-SYN-SW": entries},
        )

        runs = {run["analysis_unit_id"]: run for run in plan["runs"]}
        self.assertEqual(["AN-SYN-SW-neg", "AN-SYN-SW-pos"], sorted(runs))
        self.assertEqual("Positive", runs["AN-SYN-SW-pos"]["project"]["ion_mode"])
        self.assertEqual("Negative", runs["AN-SYN-SW-neg"]["project"]["ion_mode"])
        self.assertNotEqual(runs["AN-SYN-SW-pos"]["workspace"], runs["AN-SYN-SW-neg"]["workspace"])


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()

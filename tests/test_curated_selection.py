"""A triaged unit is held to its file list: curated_selection_path, its provenance, its header check, and the
cross-unit check (0.5.69).

Campaign stage3b was stopped on 2026-11-15 after multi-unit accessions ran every unit on the whole study's files. The
author approved agent-based triage before download (msdial-unit-triage.v1 files, checked by validate-triage.py, carried
by an approved manifest digest). What is pinned here, with synthetic accessions, archives and names only (the shapes
are those of three public Metabolomics Workbench studies, named where a test follows one):

- the cut keeps only the triaged rows and declares their inputs by archive member path, so a lease of the whole-study
  archive admits only them; the provenance carries every hash a third party needs (triage, decision record, Catalog
  handoff, canonical selection) and says whether the selection is a model inference or an accepted decision;
- the triage must be for this handoff, ready, approved where a campaign manifest is named, and resolve every kept row
  to a member; a Both unit is refused without a single-polarity child (ST002798's shape), and accepted with one;
- an approved Class contrast that loses a side on the kept rows is refused;
- after the preflight, a header that disagrees with the triage holds the unit (triage_header_disagreement), and an
  unread header for a row the triage only inferred holds it too (ST004544's shape, whose names carry no polarity);
- the cross-unit check refuses an identical set after its first unit (D), byte copies across chromatographies on both
  sides (C, ST002283's shape), a polarity contradiction (P), and passes shared blanks and QCs and same-named files of
  different archives;
- every refusal is retryable false, and every code is in ATTRIBUTION_COVERAGE_REFUSALS.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from msdial_app import curated_selection as cs
from msdial_app import mcp_server, repository_reanalysis
from msdial_app.archives import ExtractionLimits
from msdial_app.raw_metadata_preflight import decide_disposition
from msdial_app.repository_analysis_rows import ATTRIBUTION_COVERAGE_REFUSALS
from msdial_app.repository_reanalysis import create_download_lease, project_from_dict, read_manifest

from test_mzml_encoding import dda_spectra, mzml

ACCESSION = "STSYN0001"
ARCHIVE = "STSYN0001_rawdata.zip"
ARCHIVE_URL = "https://repository.invalid/STSYN0001_rawdata.zip"
MZML = mzml(dda_spectra(2))
# ST002798's shape: one reversed-phase study, both polarities, in folders of one archive.
MEMBERS = [
    ("RPLC_POSITIVE/SA_01_C18pos.mzML", "positive", "sample", "A"),
    ("RPLC_POSITIVE/SB_01_C18pos.mzML", "positive", "sample", "B"),
    ("RPLC_POSITIVE/QC_01_C18pos.mzML", "positive", "qc", "QC"),
    ("RPLC_NEGATIVE/SA_01_C18neg.mzML", "negative", "sample", "A"),
    ("RPLC_NEGATIVE/SB_01_C18neg.mzML", "negative", "sample", "B"),
    ("RPLC_NEGATIVE/QC_01_C18neg.mzML", "negative", "qc", "QC"),
]


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for name, data in members.items():
            handle.writestr(zipfile.ZipInfo(name, date_time=(2026, 11, 16, 9, 0, 0)), data)
    return buffer.getvalue()


ARCHIVE_BYTES = _zip({path: MZML for path, *_rest in MEMBERS})


def _stem(path: str) -> str:
    return Path(path).stem


def _handoff(unit_id: str = "AN-SYN-BOTH", ion_mode: str = "Both", *, class_proposal=None) -> dict:
    """A Catalog handoff whose sample rows are the whole study's, as stage3b's units were: one shared archive."""
    return {
        "schema": "msdial-repository-reanalysis-handoff.v1",
        "repository": "metabolomics_workbench",
        "accession": ACCESSION,
        "analysis_unit_id": unit_id,
        "title": ACCESSION,
        "technical_settings": {
            "separation": "LC-MS",
            "ion_mode": ion_mode,
            "acquisition_mode": "DDA",
            "untargeted": True,
            "target_omics": "Metabolomics",
            "chromatography": "Reversed phase",
        },
        "files": [
            {
                "path": ARCHIVE,
                "role": "raw",
                "size_bytes": len(ARCHIVE_BYTES),
                "checksum": "",
                "download_url": ARCHIVE_URL,
            }
        ],
        "download_scope": {"kind": "shared_raw_archive", "file_count": 1, "bundle_bytes": len(ARCHIVE_BYTES)},
        "sample_count": len(MEMBERS),
        "sample_metadata": [
            {"sample_id": _stem(path), "raw_file": Path(path).name, "attributes": {"Group": group}}
            for path, _ion, _role, group in MEMBERS
        ],
        "class_proposal": class_proposal,
        "blocking_reasons": [],
    }


def _triage(handoff_sha: str, keep: list[int], *, unit_id: str = "AN-SYN-BOTH", ion_mode: str = "positive",
            status: str = "ready", children=None, basis: str = "declared", **overrides) -> dict:
    rows = []
    for index in keep:
        path, ion, role, _group = MEMBERS[index]
        rows.append({
            "row_index": index,
            "sample_id": _stem(path),
            "raw_file": Path(path).name,
            "member_path": path,
            "archive": ARCHIVE,
            "role": role,
            "ion_mode": ion,
            "chromatography": "RP",
            "acquisition_mode": "DDA",
            "basis": basis,
            "evidence": [{"source": "folder_token", "field": "folder", "value": path.split("/")[0]}],
        })
    excluded = [
        {
            "row_index": index,
            "sample_id": _stem(MEMBERS[index][0]),
            "raw_file": Path(MEMBERS[index][0]).name,
            "member_path": MEMBERS[index][0],
            "reason": "belongs_to_other_unit",
            "belongs_to_unit": "AN-SYN-NEG",
            "detail": "The folder states negative.",
        }
        for index in range(len(MEMBERS))
        if index not in keep
    ]
    triage = {
        "schema": "msdial-unit-triage.v1",
        "repository": "metabolomics_workbench",
        "accession": ACCESSION,
        "unit_id": unit_id,
        "catalog_handoff_sha256": handoff_sha,
        "catalog_row_count": len(MEMBERS),
        "standing": "model_inference",
        "phase": "pre_download",
        "decided_by": {"agent": "triage-agent", "model": "synthetic-model", "skill": "msdial-unit-triage",
                       "skill_version": "1.0.0"},
        "decided_at": "2026-11-16T09:00:00+09:00",
        "purpose": None,
        "unit_technical": {
            "ion_mode": ion_mode,
            "chromatography": "RP",
            "acquisition_mode": "DDA",
            "declared": {"chromatography": "Reversed phase", "ion_mode": "Both", "acquisition_mode": "DDA"},
        },
        "status": status,
        "rows": rows,
        "added_members": [],
        "excluded_rows": excluded,
        "excluded_members": [],
        "shared_with": [],
        "needs_person": [],
        "flags": [],
    }
    if children is not None:
        triage["children"] = children
    triage.update(overrides)
    return triage


class _Files(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, name: str, value: dict) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=1), encoding="utf-8")
        return path

    def handoff_file(self, **options) -> tuple[dict, Path, str]:
        handoff = _handoff(**options)
        path = self.write("handoffs/handoff.json", handoff)
        return handoff, path, hashlib.sha256(path.read_bytes()).hexdigest()

    def project(self, triage: dict, handoff: dict, handoff_path: Path, **context):
        triage_path = self.write(f"triage/{triage.get('unit_id')}.json", triage)
        curated = mcp_server._curated_selection_context(str(triage_path), handoff, str(handoff_path), **context)
        project, workspace = mcp_server._project_from_analysis_unit_handoff(handoff, curated_selection=curated)
        return project, workspace, triage_path

    def refused(self, code: str, triage: dict, handoff: dict, handoff_path: Path, **context) -> cs.CuratedSelectionError:
        with self.assertRaises(cs.CuratedSelectionError) as caught:
            self.project(triage, handoff, handoff_path, **context)
        self.assertEqual(code, caught.exception.code)
        self.assertFalse(caught.exception.retryable)
        self.assertIn(code, ATTRIBUTION_COVERAGE_REFUSALS)
        return caught.exception


class TheCutKeepsOnlyTheTriagedRows(_Files):
    def test_the_kept_rows_are_the_samples_and_their_members_the_declared_inputs(self) -> None:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        triage = _triage(sha, [0, 1, 2], unit_id="AN-SYN-POS")

        project, workspace, _triage_path = self.project(triage, handoff, path)

        self.assertEqual("AN-SYN-POS", project["analysis_unit_id"])
        self.assertEqual(3, project["sample_count"])
        self.assertEqual(["SA_01_C18pos", "SB_01_C18pos", "QC_01_C18pos"],
                         [row["sample_id"] for row in project["sample_metadata"]])
        self.assertEqual(3, len(workspace["rows"]))
        self.assertTrue(project["analysis_inputs_declared"])
        self.assertEqual([MEMBERS[i][0] for i in (0, 1, 2)], [item["path"] for item in project["analysis_inputs"]])
        self.assertEqual({"curated_selection"}, {item["selected_by"] for item in project["analysis_inputs"]})
        self.assertEqual(3, project["download_scope"]["analysis_file_count"])
        self.assertEqual("Positive", project["ion_mode"])
        self.assertEqual(
            "superseded_by_curated_selection",
            project["repository_metadata"]["analysis_input_check"]["status"],
        )
        # The archive is still the one download object: one archive serves all of an accession's units.
        self.assertEqual([ARCHIVE], [item["name"] for item in project["files"]])

    def test_the_provenance_carries_every_hash_and_says_it_is_a_model_inference(self) -> None:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        triage = _triage(sha, [0, 1, 2], unit_id="AN-SYN-POS")
        validator = {"script_sha256": "ef" * 32, "verdict": "pass"}

        project, _workspace, triage_path = self.project(triage, handoff, path, validator=validator)
        record = project["repository_metadata"]["curated_selection"]

        self.assertEqual(cs.PROVENANCE_SCHEMA, record["schema"])
        self.assertEqual(hashlib.sha256(triage_path.read_bytes()).hexdigest(), record["triage_sha256"])
        self.assertEqual(sha, record["catalog_handoff_sha256"])
        self.assertEqual(sha, record["copy_handoff_sha256"])
        self.assertEqual("model_inference", record["standing"])
        self.assertEqual({"agent": "triage-agent", "model": "synthetic-model", "skill": "msdial-unit-triage",
                          "skill_version": "1.0.0"}, record["decided_by"])
        self.assertEqual(validator, record["validator"])
        self.assertEqual([0, 1, 2], [row["row_index"] for row in record["rows_kept"]])
        self.assertEqual(
            [(3, "belongs_to_other_unit", "AN-SYN-NEG"), (4, "belongs_to_other_unit", "AN-SYN-NEG"),
             (5, "belongs_to_other_unit", "AN-SYN-NEG")],
            [(row["row_index"], row["reason"], row["belongs_to_unit"]) for row in record["rows_dropped"]],
        )
        self.assertEqual("", record["triage_record_sha256"])
        self.assertTrue(any("curated selection" in line for line in project["evidence"]))

    def test_the_selection_hash_is_recomputable_from_the_triage_alone(self) -> None:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        triage = _triage(sha, [0, 1, 2], unit_id="AN-SYN-POS")
        project, _workspace, _path = self.project(triage, handoff, path)

        expected = sorted(
            (
                {"unit_id": "AN-SYN-POS", "archive_name": ARCHIVE, "member_path": MEMBERS[i][0], "crc32": None,
                 "size": None, "sample_row_index": i, "child_key": None}
                for i in (0, 1, 2)
            ),
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
        digest = hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        self.assertEqual(digest, project["repository_metadata"]["curated_selection"]["selection_sha256"])

    def test_a_triage_made_from_another_handoff_is_refused(self) -> None:
        handoff, path, _sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        self.refused(cs.CURATED_SELECTION_HANDOFF_MISMATCH, _triage("00" * 32, [0, 1, 2], unit_id="AN-SYN-POS"),
                     handoff, path)

    def test_a_rewritten_copy_is_verified_by_the_original_sha_the_runner_passes(self) -> None:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        copy = dict(handoff, runner_note="rewritten by copy_handoff")
        copy_path = self.write("handoffs/copy.json", copy)
        triage = _triage(sha, [0, 1, 2], unit_id="AN-SYN-POS")

        self.refused(cs.CURATED_SELECTION_HANDOFF_MISMATCH, triage, copy, copy_path)
        project, _workspace, _path = self.project(triage, copy, copy_path, original_handoff_sha256=sha)
        record = project["repository_metadata"]["curated_selection"]
        self.assertEqual(sha, record["original_handoff_sha256"])
        self.assertEqual(hashlib.sha256(copy_path.read_bytes()).hexdigest(), record["copy_handoff_sha256"])

    def test_a_triage_that_is_not_ready_is_refused_as_st004544s_abstention(self) -> None:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        triage = _triage(sha, [], unit_id="AN-SYN-POS", status="abstained")
        error = self.refused(cs.CURATED_SELECTION_NOT_READY, triage, handoff, path)
        self.assertEqual("abstained", error.detail["status"])

    def test_a_kept_row_without_a_member_is_refused_not_matched_by_name(self) -> None:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        triage = _triage(sha, [0, 1, 2], unit_id="AN-SYN-POS")
        triage["rows"][1]["member_path"] = None
        error = self.refused(cs.CURATED_SELECTION_MEMBER_UNRESOLVED, triage, handoff, path)
        self.assertEqual([1], error.detail["row_indices"])

    def test_a_triage_for_another_unit_or_schema_is_refused(self) -> None:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        self.refused(cs.CURATED_SELECTION_HANDOFF_MISMATCH, _triage(sha, [0], unit_id="AN-OTHER"), handoff, path)
        self.refused(cs.CURATED_SELECTION_INVALID,
                     _triage(sha, [0], unit_id="AN-SYN-POS", schema="msdial-unit-triage.v0"), handoff, path)

    def test_a_decision_record_beside_the_triage_is_bound_and_a_stale_one_refused(self) -> None:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        triage = _triage(sha, [0, 1, 2], unit_id="AN-SYN-POS")
        triage_path = self.write("triage/AN-SYN-POS.json", triage)
        triage_sha = hashlib.sha256(triage_path.read_bytes()).hexdigest()
        record_path = self.write("triage/AN-SYN-POS.record.json", {
            "decision_id": triage_sha, "kind": "unit_triage",
            "payload": {"path": "AN-SYN-POS.json", "schema": "msdial-unit-triage.v1", "sha256": triage_sha},
        })

        project, _workspace, _path = self.project(triage, handoff, path)
        self.assertEqual(hashlib.sha256(record_path.read_bytes()).hexdigest(),
                         project["repository_metadata"]["curated_selection"]["triage_record_sha256"])

        self.write("triage/AN-SYN-POS.record.json", {"payload": {"sha256": "11" * 32}})
        self.refused(cs.CURATED_SELECTION_RECORD_MISMATCH, triage, handoff, path)


class ABothUnitNeedsASinglePolarityChild(_Files):
    """ST002798's shape: the Catalog unit says Both; MS-DIAL runs one polarity per analysis."""

    CHILDREN = [
        {"child_key": "pos", "ion_mode": "positive", "acquisition_mode": "DDA",
         "member_paths": [MEMBERS[i][0] for i in (0, 1, 2)]},
        {"child_key": "neg", "ion_mode": "negative", "acquisition_mode": "DDA",
         "member_paths": [MEMBERS[i][0] for i in (3, 4, 5)]},
    ]

    def triage(self, sha: str) -> dict:
        triage = _triage(sha, list(range(6)), ion_mode="both", children=self.CHILDREN)
        triage["excluded_rows"] = []
        return triage

    def test_without_a_child_it_is_refused(self) -> None:
        handoff, path, sha = self.handoff_file()
        error = self.refused(cs.CURATED_SELECTION_MIXED_POLARITY, self.triage(sha), handoff, path)
        self.assertIn("pos", str(error))

    def test_a_child_is_a_unit_of_its_own_with_its_own_rows(self) -> None:
        handoff, path, sha = self.handoff_file()

        project, _workspace, _path = self.project(self.triage(sha), handoff, path, child_key="pos")
        record = project["repository_metadata"]["curated_selection"]

        self.assertEqual("AN-SYN-BOTH-pos", project["analysis_unit_id"])
        self.assertEqual("Positive", project["ion_mode"])
        self.assertEqual([MEMBERS[i][0] for i in (0, 1, 2)], [item["path"] for item in project["analysis_inputs"]])
        self.assertEqual("pos", record["child_key"])
        self.assertEqual({"other_child"}, {row["reason"] for row in record["rows_dropped"]})
        self.assertEqual({"AN-SYN-BOTH-neg"}, {row["belongs_to_unit"] for row in record["rows_dropped"]})
        self.assertEqual(cs.selection_sha256(self.triage(sha), "pos"), record["selection_sha256"])

    def test_an_unknown_child_is_refused(self) -> None:
        handoff, path, sha = self.handoff_file()
        self.refused(cs.CURATED_SELECTION_CHILD_UNKNOWN, self.triage(sha), handoff, path, child_key="dia")

    def test_kept_rows_of_two_polarities_in_a_positive_unit_are_refused(self) -> None:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        triage = _triage(sha, [0, 1, 3], unit_id="AN-SYN-POS")
        error = self.refused(cs.CURATED_SELECTION_MIXED_POLARITY, triage, handoff, path)
        self.assertEqual(["negative", "positive"], error.detail["row_polarities"])

    def test_the_batch_plan_records_the_refusal_and_plans_the_other_unit(self) -> None:
        both, both_path, both_sha = self.handoff_file()
        positive = _handoff(unit_id="AN-SYN-POS", ion_mode="Positive")
        positive_path = self.write("handoffs/positive.json", positive)
        positive_sha = hashlib.sha256(positive_path.read_bytes()).hexdigest()
        triage_both = self.write("triage/AN-SYN-BOTH.json", self.triage(both_sha))
        triage_pos = self.write("triage/AN-SYN-POS.json", _triage(positive_sha, [0, 1, 2], unit_id="AN-SYN-POS"))

        plan = mcp_server.msdial_repository_batch_plan(
            workspace_root=str(self.root / "analysis"),
            analysis_unit_handoff_paths=[str(both_path), str(positive_path)],
            analysis_purpose="Synthetic purpose.",
            curated_selection_paths={"AN-SYN-BOTH": str(triage_both), "AN-SYN-POS": {"path": str(triage_pos)}},
        )

        runs = {run["analysis_unit_id"]: run for run in plan["runs"]}
        self.assertEqual(["curated_selection:" + cs.CURATED_SELECTION_MIXED_POLARITY],
                         runs["AN-SYN-BOTH"]["blocking_reasons"])
        self.assertFalse(runs["AN-SYN-BOTH"]["refusal"]["retryable"])
        self.assertEqual(3, runs["AN-SYN-POS"]["project"]["sample_count"])

    def test_the_plan_tool_returns_the_code_not_retryable(self) -> None:
        handoff, path, sha = self.handoff_file()
        triage_path = self.write("triage/AN-SYN-BOTH.json", self.triage(sha))

        result = mcp_server.msdial_repository_reanalysis_plan(
            "metabolomics_workbench", ACCESSION, str(self.root / "analysis"),
            analysis_unit_handoff_path=str(path), curated_selection_path=str(triage_path),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(cs.CURATED_SELECTION_MIXED_POLARITY, result["reason_code"])
        self.assertFalse(result["retryable"])

        planned = mcp_server.msdial_repository_reanalysis_plan(
            "metabolomics_workbench", ACCESSION, str(self.root / "analysis"),
            analysis_unit_handoff_path=str(path), curated_selection_path=str(triage_path),
            curated_selection_child_key="neg",
        )
        self.assertEqual("AN-SYN-BOTH-neg", planned["project"]["analysis_unit_id"])


class AnApprovedManifestMustCarryTheTriage(_Files):
    def approval(self, entry: dict | None) -> Path:
        manifest = {"schema": "synthetic-campaign-manifest", "units": [entry] if entry else []}
        manifest_path = self.write("campaign/manifest.json", manifest)
        digest = "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        return self.write("campaign/approval.json", {
            "schema": "msdial-campaign-authorization.v1",
            "approval_id": "approval-syn",
            "campaign_id": "synthetic",
            "manifest_digest": digest,
            "campaign_manifest_path": str(manifest_path),
            "approved_by": "A. Person",
            "approved_at": "2026-11-16T10:00:00+09:00",
            "statement": "Run the listed units as planned.",
            "covers": [1],
            "units": ["AN-SYN-POS"],
            "raw_retention_policy": "keep",
            "libraries": [],
            "revoked_at": None,
        })

    def setUp(self) -> None:
        super().setUp()
        self.handoff, self.path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        self.triage = _triage(sha, [0, 1, 2], unit_id="AN-SYN-POS")
        self.triage_sha = hashlib.sha256(
            self.write("triage/AN-SYN-POS.json", self.triage).read_bytes()
        ).hexdigest()

    def test_the_approved_triage_is_an_accepted_decision(self) -> None:
        approval = self.approval({"unit_id": "AN-SYN-POS", "curated_selection": {"triage_sha256": self.triage_sha}})

        project, _workspace, _path = self.project(
            self.triage, self.handoff, self.path, campaign_authorization_path=str(approval)
        )
        record = project["repository_metadata"]["curated_selection"]
        self.assertEqual("accepted_decision", record["standing"])
        self.assertEqual("approval-syn", record["approval_id"])
        self.assertTrue(record["manifest_digest"].startswith("sha256:"))

    def test_another_triage_or_no_entry_is_refused(self) -> None:
        for entry in (
            {"unit_id": "AN-SYN-POS", "curated_selection": {"triage_sha256": "22" * 32}},
            {"unit_id": "AN-SYN-POS"},
            None,
        ):
            with self.subTest(entry=entry):
                approval = self.approval(entry)
                self.refused(cs.CURATED_SELECTION_NOT_APPROVED, self.triage, self.handoff, self.path,
                             campaign_authorization_path=str(approval))


class AContrastThatLosesASideIsRefused(_Files):
    def proposal(self, kind: str) -> dict:
        return {
            "proposal_id": "class-syn",
            "assignments": [
                {"sample_id": _stem(path), "class_label": group} for path, _ion, _role, group in MEMBERS
            ],
            "contrast_definition": {"kind": kind, "class_label": "All" if kind == "abstention" else ""},
        }

    def test_keeping_only_one_group_loses_the_contrast(self) -> None:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive",
                                               class_proposal=self.proposal("declared_factor"))
        error = self.refused(cs.CURATED_SELECTION_CLASS_CONTRAST_LOST, _triage(sha, [0, 2], unit_id="AN-SYN-POS"),
                             handoff, path)
        self.assertEqual(["A", "QC"], error.detail["levels_after"])

    def test_the_cut_proposal_keeps_its_rows_assignments_and_says_so(self) -> None:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive",
                                               class_proposal=self.proposal("declared_factor"))
        project, _workspace, _path = self.project(_triage(sha, [0, 1, 2], unit_id="AN-SYN-POS"), handoff, path)

        proposal = project["class_proposal"]
        self.assertEqual(["SA_01_C18pos", "SB_01_C18pos", "QC_01_C18pos"],
                         [item["sample_id"] for item in proposal["assignments"]])
        self.assertEqual(6, proposal["curated_from"]["assignments_in_proposal"])

    def test_an_abstention_has_no_contrast_to_lose(self) -> None:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive",
                                               class_proposal=self.proposal("abstention"))
        project, _workspace, _path = self.project(_triage(sha, [0], unit_id="AN-SYN-POS"), handoff, path)
        self.assertEqual(1, len(project["class_proposal"]["assignments"]))


class _Client:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def download(self, url, destination, _maximum_bytes, progress_callback=None):
        data = self.payloads[url]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return {
            "path": str(destination),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "md5": hashlib.md5(data).hexdigest(),
            "resumed_from_bytes": 0,
        }


class TheLeaseOfTheWholeArchiveAdmitsOnlyTheTriagedMembers(_Files):
    def setUp(self) -> None:
        super().setUp()
        limits = patch.object(repository_reanalysis, "LEASE_EXTRACTION_LIMITS", ExtractionLimits(reserve_bytes=0))
        limits.start()
        self.addCleanup(limits.stop)

    def lease(self, basis: str = "declared") -> dict:
        handoff, path, sha = self.handoff_file(unit_id="AN-SYN-POS", ion_mode="Positive")
        project, _workspace, _path = self.project(_triage(sha, [0, 1, 2], unit_id="AN-SYN-POS", basis=basis),
                                                  handoff, path)
        typed = project_from_dict(project)
        typed.eligible, typed.selection_status, typed.blocking_reasons = True, "eligible", []
        lease = create_download_lease(typed, self.root / "analysis", 10**9, client=_Client({ARCHIVE_URL: ARCHIVE_BYTES}))
        return read_manifest(lease["manifest_path"])

    def test_the_inputs_are_the_kept_members_and_each_row_says_so(self) -> None:
        manifest = self.lease()

        self.assertEqual("prepared", manifest["status"], manifest.get("download_failure"))
        names = sorted(Path(item).name for item in manifest["input_candidates"])
        self.assertEqual(sorted(Path(MEMBERS[i][0]).name for i in (0, 1, 2)), names)
        rows = manifest["input_lineage"]["rows"]
        self.assertEqual({"curated_selection"}, {row["selected_by"] for row in rows})
        self.assertEqual([0, 1, 2], sorted(row["triage_row_index"] for row in rows))
        self.assertEqual("passed", manifest["attribution_coverage"]["status"])

    def preflighted(self, manifest: dict, polarities: dict[str, str], outcome: str = "ok") -> dict:
        manifest["raw_metadata_preflight"] = {
            "declared": {"separation": "LC-MS", "ion_mode": "Positive", "acquisition_mode": "DDA", "untargeted": True},
            "extractor": {"sha256": "ab" * 32, "pinned": True},
            "summary": {
                "acquisition_mode": "DDA",
                "per_file": [
                    {
                        "file": path, "outcome": outcome,
                        "acquisition_mode": "DDA" if outcome == "ok" else "",
                        "polarity": polarities.get(Path(path).name, "Positive") if outcome == "ok" else "",
                        "ms_levels": [1, 2], "has_ms1": True, "has_ms2": True, "has_ion_mobility": False,
                        "format": "mzML", "separation": "LC",
                    }
                    for path in manifest["input_candidates"]
                ],
                "coverage": {"inspected": len(manifest["input_candidates"])},
            },
        }
        return manifest

    def test_matching_headers_agree_and_the_unit_runs(self) -> None:
        manifest = self.preflighted(self.lease(), {})

        block = cs.reconcile_curated_selection(manifest)
        disposition = decide_disposition(manifest)

        self.assertEqual("agree", block["verdict"])
        self.assertEqual(3, block["agree"])
        self.assertEqual("run", disposition["disposition"], disposition["reasons"])
        self.assertEqual("agree", disposition["curated_selection_reconciliation"]["verdict"])

    def test_one_negative_header_in_a_positive_triage_holds_the_unit(self) -> None:
        manifest = self.preflighted(self.lease(), {"SB_01_C18pos.mzML": "Negative"})

        disposition = decide_disposition(manifest)

        self.assertEqual("skip", disposition["disposition"])
        self.assertTrue(disposition["hold"])
        self.assertIn(cs.TRIAGE_HEADER_DISAGREEMENT, disposition["reasons"])
        self.assertEqual({}, disposition["assignments"])
        self.assertNotIn("ion_mode", disposition)
        block = disposition["curated_selection_reconciliation"]
        self.assertEqual(["RPLC_POSITIVE/SB_01_C18pos.mzML"],
                         [item["member_path"] for item in block["files"] if item["result"] == "disagree"])
        self.assertFalse(block["retryable"])

    def test_an_unread_header_holds_a_row_the_triage_only_inferred_as_st004544s(self) -> None:
        inferred = self.preflighted(self.lease(basis="inferred"), {}, outcome="failed")
        self.assertEqual("disagree", cs.reconcile_curated_selection(inferred)["verdict"])

    def test_an_unread_header_of_a_declared_row_is_no_disagreement(self) -> None:
        declared = self.preflighted(self.lease(), {}, outcome="failed")
        self.assertEqual("agree", cs.reconcile_curated_selection(declared)["verdict"])

    def test_a_disagreement_recorded_by_the_preflight_stops_execution_outside_a_campaign(self) -> None:
        manifest = self.preflighted(self.lease(), {"SA_01_C18pos.mzML": "Negative"})
        manifest["execution_allowed"] = True
        execution = {"outcomes": {}, "records": [], "counts": {}, "chunks": [], "command_template": [], "exit_code": 0}
        with patch.object(repository_reanalysis, "_summarize_raw_metadata",
                          return_value={"per_file": manifest["raw_metadata_preflight"]["summary"]["per_file"],
                                        "acquisition_mode": "DDA"}), \
                patch.object(repository_reanalysis, "_per_input_entries", side_effect=lambda per_file, *_: per_file):
            repository_reanalysis._record_preflight(
                manifest, execution=execution, extractor={"sha256": "ab" * 32}, inputs=[], output=self.root / "o.json",
                declared=manifest["raw_metadata_preflight"]["declared"], confirm_untargeted=False, capped=False,
                campaign=None, started_at="2026-11-16T00:00:00+00:00",
            )
        self.assertFalse(manifest["execution_allowed"])
        self.assertEqual("disagree", manifest["curated_selection_reconciliation"]["verdict"])


def _row(path: str, *, archive_sha: str = "a" * 64, member: str | None = None, declared: str = "",
         tsv: str = "") -> dict:
    return {
        "path": path,
        "kind": "extracted_member",
        "sample_id": "",
        "source": {"archive": {"sha256": archive_sha, "url": ARCHIVE_URL}, "member": Path(path).name},
        "checksums": {"declared": declared} if declared else {},
        "basis": {"listing": [{"archive_sha256": archive_sha, "member": member or path.split("/raw/data/")[-1],
                               "members_tsv": tsv}]},
    }


def _manifest(unit_id: str, polarity: str, chromatography: str, members: list[str], *, root: str = "D:/ws",
              headers: dict[str, str] | None = None, archive_sha: str = "a" * 64, tsv: str = "",
              basis: str = "declared") -> dict:
    data_root = f"{root}/{unit_id}/raw/data"
    rows = [_row(f"{data_root}/{member}", archive_sha=archive_sha, member=member, tsv=tsv) for member in members]
    return {
        "input_directory": data_root,
        "project": {
            "analysis_unit_id": unit_id,
            "accession": ACCESSION,
            "ion_mode": polarity,
            "acquisition_mode": "DDA",
            "separation": "LC-MS",
            "repository_metadata": {
                "curated_selection": {
                    "unit_technical": {"chromatography": chromatography},
                    "rows_kept": [{"member_path": member, "role": "sample", "basis": basis} for member in members],
                }
            },
        },
        "input_lineage": {"rows": rows},
        "raw_metadata_preflight": {
            "summary": {"per_file": [
                {"file": f"{data_root}/{member}", "polarity": (headers or {}).get(member, "")} for member in members
            ]}
        },
    }


class TheCrossUnitCheck(_Files):
    def test_an_identical_set_refuses_only_the_second_unit(self) -> None:
        files = [f"S{i}.mzML" for i in range(1, 5)]
        result = cs.cross_unit_attribution([
            _manifest("AN-1", "Positive", "RP", files), _manifest("AN-2", "Positive", "RP", files),
        ])
        verdicts = [(item["analysis_unit_id"], item["verdict"], item["reason"]) for item in result["units"]]
        self.assertEqual([("AN-1", "passed", ""), ("AN-2", "refused", cs.CROSS_UNIT_DUPLICATE_INPUTS)], verdicts)
        self.assertFalse(result["units"][1]["retryable"])

    def test_shared_blanks_and_qcs_alone_pass(self) -> None:
        result = cs.cross_unit_attribution([
            _manifest("AN-1", "Positive", "HILIC", ["Blank_01.mzML", "QC_01.mzML", "H_S1.mzML"]),
            _manifest("AN-2", "Positive", "RP", ["Blank_01.mzML", "QC_01.mzML", "R_S1.mzML"]),
        ])
        self.assertEqual("passed", result["verdict"], result)

    def test_one_name_in_two_archives_with_different_checksums_passes(self) -> None:
        result = cs.cross_unit_attribution([
            _manifest("AN-1", "Positive", "RP", ["S1.raw"], archive_sha="a" * 64),
            _manifest("AN-2", "Positive", "RP", ["S1.raw"], archive_sha="b" * 64),
        ])
        self.assertEqual("passed", result["verdict"], result)

    def test_byte_copies_across_chromatographies_refuse_both_sides_as_st002283s(self) -> None:
        """Hilic_Neg and Lipid_Neg hold byte-identical files (same CRC32 and size) under other names; the copies are
        named HILIC and are reversed phase, so the names settle nothing."""
        hilic = [f"Human_Hilic_Neg/D{i}_Hilic_Neg.mzML" for i in range(1, 9)]
        lipid = [f"Human_Lipid_Neg/D{i}_Lipid_Neg.mzML" for i in range(1, 9)]
        tsv = self.root / "members.tsv"
        with tsv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(["path", "type", "size", "crc32"])
            for i, (h, l) in enumerate(zip(hilic, lipid), start=1):
                writer.writerow([h, "file", 1000 + i, f"{i:08X}"])
                writer.writerow([l, "file", 1000 + i, f"{i:08X}"])
        result = cs.cross_unit_attribution([
            _manifest("AN-HILIC-NEG", "Negative", "HILIC", hilic, tsv=str(tsv)),
            _manifest("AN-RP-NEG", "Negative", "RP", lipid, tsv=str(tsv)),
        ])
        self.assertEqual(
            [cs.CROSS_UNIT_INDISTINGUISHABLE_CHROMATOGRAPHY] * 2, [item["reason"] for item in result["units"]]
        )
        self.assertEqual("listed_content_crc32_size", result["overlaps"][0]["basis"])

    def test_the_whole_archive_under_two_chromatographies_is_refused_as_stage3b_ran_it(self) -> None:
        files = [f"Human_Lipid_Neg/D{i}.mzML" for i in range(1, 9)]
        result = cs.cross_unit_attribution([
            _manifest("AN-HILIC-NEG", "Negative", "HILIC", files), _manifest("AN-RP-NEG", "Negative", "RP", files),
        ])
        self.assertEqual(["refused", "refused"], [item["verdict"] for item in result["units"]])

    def test_partly_shared_inputs_warn(self) -> None:
        result = cs.cross_unit_attribution([
            _manifest("AN-1", "Positive", "HILIC", ["S1.mzML", "S2.mzML", "S3.mzML", "S4.mzML"]),
            _manifest("AN-2", "Positive", "RP", ["S1.mzML", "T2.mzML", "T3.mzML", "T4.mzML"]),
        ])
        self.assertEqual("passed", result["verdict"])
        self.assertIn(cs.CROSS_UNIT_PARTLY_SHARED_WARNING, result["units"][0]["warnings"])

    def test_a_header_of_the_other_polarity_held_by_a_sibling_refuses_the_unit(self) -> None:
        files = ["S1.mzML", "S2.mzML"]
        result = cs.cross_unit_attribution([
            _manifest("AN-POS", "Positive", "RP", files, headers={"S1.mzML": "Negative", "S2.mzML": "Negative"}),
            _manifest("AN-NEG", "Negative", "RP", files, headers={"S1.mzML": "Negative", "S2.mzML": "Negative"}),
        ])
        self.assertEqual([cs.CROSS_UNIT_POLARITY_CONTRADICTION, ""], [item["reason"] for item in result["units"]])

    def test_disjoint_units_as_st004544_resolves_pass(self) -> None:
        result = cs.cross_unit_attribution([
            _manifest("AN-HILIC-POS", "Positive", "HILIC", ["S_1.mzML", "S_3.mzML"], basis="inferred",
                      headers={"S_1.mzML": "Positive", "S_3.mzML": "Positive"}),
            _manifest("AN-RP-NEG", "Negative", "RP", ["S_2.mzML", "S_4.mzML"], basis="inferred",
                      headers={"S_2.mzML": "Negative", "S_4.mzML": "Negative"}),
        ])
        self.assertEqual("passed", result["verdict"], result)

    def test_the_tool_records_each_verdict_and_stops_the_refused_unit(self) -> None:
        files = ["S1.mzML", "S2.mzML"]
        paths = []
        for unit in ("AN-1", "AN-2"):
            manifest = _manifest(unit, "Positive", "RP", files)
            manifest["execution_allowed"] = True
            path = self.root / unit / "provenance" / "run-manifest.json"
            path.parent.mkdir(parents=True)
            repository_reanalysis._write_json(path, manifest)
            paths.append(str(path))

        result = mcp_server.msdial_repository_cross_unit_check(paths)

        self.assertTrue(result["ok"])
        first, second = (read_manifest(path) for path in paths)
        self.assertEqual("passed", first["cross_unit_attribution"]["verdict"])
        self.assertTrue(first["execution_allowed"])
        self.assertEqual(cs.CROSS_UNIT_DUPLICATE_INPUTS, second["cross_unit_attribution"]["reason_code"])
        self.assertFalse(second["execution_allowed"])


class TheCanonicalHashes(unittest.TestCase):
    def test_the_analysis_csv_hashes_alike_on_two_machines(self) -> None:
        first = (
            "\ufeffFile path,File name,Type\r\n"
            "C:\\a\\analysis\\mw\\ST\\U1\\raw\\data\\S1.mzML,S1,Sample\r\n"
            "C:\\a\\analysis\\mw\\ST\\U1\\output\\x.txt,x,Sample\r\n"
        ).encode("utf-8")
        second = (
            "File path,File name,Type\n"
            "E:/other/root/mw/ST/U1/raw/data/S1.mzML,S1,Sample\n"
            "E:/other/root/mw/ST/U1/output/x.txt,x,Sample\n"
        ).encode("utf-8")
        a = cs.canonical_analysis_csv(first, "C:\\a\\analysis\\mw\\ST\\U1\\raw")
        b = cs.canonical_analysis_csv(second, "E:/other/root/mw/ST/U1/raw")
        self.assertEqual(a, b)
        self.assertIn(b"data/S1.mzML", a)
        self.assertIn(b"../output/x.txt", a)

    def test_the_replay_projection_carries_no_timestamp_job_or_absolute_path(self) -> None:
        manifest = _manifest("AN-1", "Positive", "RP", ["S1.mzML"])
        manifest.update(finalized_at="2026-11-16T00:00:00Z", finalized_run={"job_id": "job-1"})
        projection = cs.replay_projection(manifest, "0.5.69")
        text = json.dumps(projection)
        self.assertNotIn("D:/ws", text)
        self.assertNotIn("job-1", text)
        self.assertNotIn("2026-11-16", text)
        moved = _manifest("AN-1", "Positive", "RP", ["S1.mzML"], root="E:/elsewhere")
        self.assertEqual(cs.replay_projection_sha256(manifest, "0.5.69"), cs.replay_projection_sha256(moved, "0.5.69"))


if __name__ == "__main__":
    unittest.main()

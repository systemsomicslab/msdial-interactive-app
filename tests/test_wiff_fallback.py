"""A .wiff in place of its .wiff2 after a Console job failed on the .wiff2: the user's decision of 2026-10-31.

MB-POST MPST000048 (SCIEX ZenoTOF 7600, declared DDA) deposits S.wiff2, S.wiff, S.wiff.scan and S.timeseries.data per
sample. Its positive part ran every sample from its .wiff2, as the shared encoding rule says, and its production run
hung three times out of three in the .wiff2 reader while files were read in parallel. The .wiff stays the fallback, never
the preference: where a diagnostic or production run of a DDA unit failed or timed out and every .wiff2 it analyses has
its .wiff beside it with that .wiff's .wiff.scan, the next attempt runs on the .wiff, once, on record. A DIA unit is never
switched (ZT Scan DIA data can only be analysed from the .wiff2). The fixture is a split part as Interactive writes one,
with synthetic files.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import mcp_server, wiff_fallback
    from msdial_app.materials_methods import _wiff_fallback_sentence
    from msdial_app.repository_analysis_rows import build_repository_analysis_rows
    from msdial_app.repository_reanalysis import (
        _write_json,
        evaluate_repository_execution_gate,
        lineage_stands_for,
        plan_split_parent_cleanup,
        read_manifest,
        update_manifest,
    )

from subprocess import CompletedProcess

from test_raw_metadata_preflight import _header

SAMPLES = ("241202_Tomita_18_pos", "241202_Tomita_19_pos", "241202_Tomita_20_pos")
UNIT = "u48-dda-pos"


class _WiffExtractor:
    """A stand-in for RawMetadataConsoleApp reading .wiff headers, installed as run_extractor's runner.

    ``verdicts`` maps a file name to keyword arguments for _header (polarity, method, ...) with ``samples`` (how many
    samples the record lists, 1 by default), or to "fail" (exit 1). Every other file reads as one positive DDA sample.
    """

    def __init__(self, verdicts: dict | None = None) -> None:
        self.verdicts = verdicts or {}
        self.read: list[str] = []

    def __call__(self, command, **kwargs):
        inputs = [command[index + 1] for index, token in enumerate(command) if token == "--input"]
        output = Path(command[command.index("--output") + 1])
        records = []
        for path in inputs:
            self.read.append(Path(path).name)
            verdict = self.verdicts.get(Path(path).name, {})
            if verdict == "fail":
                return CompletedProcess(command, 1, stdout="", stderr="System.IO.InvalidDataException")
            verdict = dict(verdict)
            samples = verdict.pop("samples", 1)
            record = _header(path, **{"method": "DDA", "polarity": "Positive", **verdict})
            if samples is not None:
                # None: a record that lists no samples at all.
                record["samples"] = [{"id": {"value": f"s{index}"}} for index in range(int(samples))]
            records.append(record)
        output.write_text(json.dumps(records[0] if len(records) == 1 else records), encoding="utf-8")
        return CompletedProcess(command, 0, stdout=str(output), stderr="")


def _authorization(root: Path, covers: list) -> Path:
    path = root / "authorization.json"
    path.write_text(json.dumps({
        "schema": "msdial-campaign-authorization.v1", "approval_id": "approval-wiff", "campaign_id": "campaign-wiff",
        "manifest_digest": "sha256:" + "ab" * 32, "approved_by": "A. Person", "approved_at": "2026-10-31T10:00:00+09:00",
        "statement": "Run the listed units as planned.", "covers": covers, "units": [UNIT],
        "raw_retention_policy": "keep", "libraries": [], "revoked_at": None,
    }), encoding="utf-8")
    return path


class _Part(unittest.TestCase):
    """A positive split part of a SCIEX DDA unit whose production run hung on its .wiff2."""

    FOLDER = ""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.data = self.root / "mb_post" / "MPST000048" / UNIT / "raw" / "data"
        # Where the samples' files lie under the data root: in it, or in the folder an archive unpacks into.
        self.files = self.data / self.FOLDER if self.FOLDER else self.data
        self.files.mkdir(parents=True)
        workspace = self.data.parent.parent
        (workspace / "provenance").mkdir()
        (workspace / "output").mkdir()
        downloads, files, rows, per_file, declared, samples = [], [], [], [], [], []
        for stem in SAMPLES:
            for suffix in (".wiff2", ".wiff", ".wiff.scan", ".timeseries.data"):
                path = self.files / (stem + suffix)
                payload = f"{stem}{suffix}".encode("utf-8")
                path.write_bytes(payload)
                md5 = hashlib.md5(payload).hexdigest()
                downloads.append({"path": str(path), "source_url": f"https://example.org/{path.name}",
                                  "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "md5": md5,
                                  "declared_checksum": md5, "declared_checksum_verified": True})
                files.append({"name": path.name, "size_bytes": len(payload), "url": f"https://example.org/{path.name}",
                              "role": "raw" if suffix == ".wiff2" else "raw_alternate" if suffix == ".wiff" else "sidecar"})
            wiff2 = str(self.files / (stem + ".wiff2"))
            rows.append({"path": wiff2, "kind": "file", "declared_names": [stem + ".wiff2"], "sample_id": stem,
                         "file_name": stem, "acquisition_type": "DDA",
                         "source": {"url": f"https://example.org/{stem}.wiff2", "download_path": wiff2},
                         "checksums": {"sha256": "0" * 64}})
            per_file.append({"file": wiff2, "outcome": "unsupported_format", "format": "sciex_wiff2",
                             "console_acquisition_type": "DDA"})
            declared.append({"path": stem + ".wiff2", "kind": "file", "sample_id": stem})
            samples.append({"sample_id": stem, "raw_file": stem + ".wiff2", "values": {"Group": "g"}})
        self.manifest_path = workspace / "provenance" / "run-manifest.json"
        _write_json(self.manifest_path, {
            "schema": "msdial-public-reanalysis-run.v1",
            "status": "run_failed",
            "workspace": str(workspace),
            "raw_directory": str(self.data.parent),
            "input_directory": str(self.data),
            "output_directory": str(workspace / "output"),
            "execution_allowed": True,
            "raw_retention_policy": "keep",
            "downloads": downloads,
            "input_candidates": [str(self.files / (stem + ".wiff2")) for stem in SAMPLES],
            "input_lineage": {"schema": "msdial-input-lineage.v1", "rows": rows},
            "split_from": {"analysis_unit_id": "u48", "acquisition_mode": "DDA"},
            "raw_metadata_preflight": {"summary": {"per_file": per_file}},
            "campaign_disposition": {"schema": "msdial-campaign-disposition.v1", "disposition": "run", "applied": True,
                                     "declared_acquisition_source": "declaration", "console_acquisition_type": "DDA",
                                     "excluded_inputs": [], "reasons": [], "warnings": []},
            "analysis_csv": {"status": "written", "rows": len(SAMPLES)},
            "run_attempts": [
                {"attempt_id": "a1", "job_id": "dg1", "kind": "tuning", "started_at": "2026-10-30T00:00:00+00:00",
                 "ended_at": "2026-10-30T00:10:00+00:00", "exit_code": 0, "reason": "exited"},
                {"attempt_id": "a2", "job_id": "rn1", "kind": "run", "started_at": "2026-10-30T01:00:00+00:00",
                 "ended_at": "2026-10-30T08:00:00+00:00", "exit_code": -3, "reason": "idle_timeout"},
            ],
            "project": {
                "repository": "mb_post", "accession": "MPST000048", "analysis_unit_id": UNIT, "separation": "LC-MS",
                "acquisition_mode": "DDA", "ion_mode": "Positive", "untargeted": True, "files": files,
                "analysis_inputs": declared, "analysis_inputs_declared": True, "sample_metadata": samples,
            },
        })

        # The extractor that reads the .wiff headers: a stub file chosen as a pinned build, and a runner standing in for
        # its process (_WiffExtractor), so the records are parsed as run_extractor parses the real one's.
        self.extractor_file = self.root / "RawMetadataConsoleApp.exe"
        self.extractor_file.write_bytes(b"stub")
        self.extractor = _WiffExtractor()
        real = wiff_fallback.read_wiff_headers
        chosen = (self.extractor_file, {"path": str(self.extractor_file), "sha256": "ab" * 32, "pinned": True})
        for patcher in (
            patch.object(wiff_fallback, "read_wiff_headers",
                         side_effect=lambda extractor, paths, work: real(extractor, paths, work, runner=self.extractor)),
            patch.object(mcp_server, "_wiff_fallback_extractor", return_value=chosen),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def change(self, function) -> None:
        update_manifest(self.manifest_path, function)

    def switch(self, jobs=("rn1",), **options) -> dict:
        return mcp_server.msdial_switch_repository_unit_to_wiff(str(self.manifest_path), list(jobs), **options)

    def as_part(self) -> Path:
        """The unit as the split writes a part: no downloads of its own, its raw tree its parent's, which names it."""
        parent_path = self.data.parent.parent / "provenance" / "parent-manifest.json"
        part = read_manifest(self.manifest_path)
        _write_json(parent_path, {
            "schema": "msdial-public-reanalysis-run.v1", "status": "split_by_acquisition",
            "workspace": str(self.data.parent.parent), "raw_directory": str(self.data.parent),
            "input_directory": str(self.data), "raw_retention_policy": "delete_after_validated_output",
            "execution_allowed": False, "downloads": part["downloads"],
            "input_candidates": list(part["input_candidates"]),
            "split_into": [{"analysis_unit_id": UNIT, "manifest_path": str(self.manifest_path),
                            "input_candidates": list(part["input_candidates"])}],
            "project": {"analysis_unit_id": "u48"},
        })

        def split(manifest: dict) -> None:
            manifest.pop("downloads")
            manifest["split_from"] = {"analysis_unit_id": "u48", "manifest_path": str(parent_path), "acquisition_mode": "DDA"}
            manifest["raw_owned_by"] = str(parent_path)

        self.change(split)
        return parent_path


class TheSwitch(_Part):
    def test_a_preview_writes_nothing(self) -> None:
        before = self.manifest_path.read_bytes()
        result = self.switch()

        self.assertEqual((True, False, True), (result["ok"], result["switched"], result["confirmation_required"]))
        self.assertEqual(3, result["preview"]["swap_count"])
        self.assertEqual({"from": SAMPLES[0] + ".wiff2", "to": SAMPLES[0] + ".wiff", "sample_id": SAMPLES[0]},
                         result["preview"]["swaps"][0])
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_the_inputs_become_the_wiff_on_record(self) -> None:
        result = self.switch(confirmed=True)
        manifest = read_manifest(self.manifest_path)

        self.assertTrue(result["switched"])
        self.assertEqual([str(self.data / (stem + ".wiff")) for stem in SAMPLES], manifest["input_candidates"])
        record = manifest["encoding_fallback"]
        self.assertEqual((wiff_fallback.WIFF_FALLBACK_RULE, wiff_fallback.RECORD_SCHEMA),
                         (record["rule"], record["schema"]))
        self.assertEqual([("rn1", "run", "idle_timeout", -3)],
                         [(item["job_id"], item["kind"], item["failure"], item["exit_code"]) for item in record["failed_jobs"]])
        self.assertEqual([str(self.data / (stem + ".wiff2")) for stem in SAMPLES], record["input_candidates_before"])
        self.assertEqual([(stem + ".wiff2", stem + ".wiff", str(self.data / (stem + ".wiff.scan"))) for stem in SAMPLES],
                         [(item["from_relative"], item["to_relative"], item["companion"]) for item in record["swaps"]])
        self.assertIn(wiff_fallback.WIFF_FALLBACK_WARNING, manifest["warnings"])
        row = manifest["input_lineage"]["rows"][0]
        self.assertEqual(str(self.data / (SAMPLES[0] + ".wiff")), row["path"])
        self.assertEqual(("file", SAMPLES[0], [SAMPLES[0] + ".wiff"]), (row["kind"], row["sample_id"], row["declared_names"]))
        self.assertEqual(hashlib.sha256(f"{SAMPLES[0]}.wiff".encode()).hexdigest(), row["checksums"]["sha256"])
        self.assertTrue(row["checksums"]["declared_verified"])
        self.assertNotIn("acquisition_type", row, "the CSV that wrote it is made again")
        self.assertEqual(str(self.data / (SAMPLES[0] + ".wiff2")), row["encoding_fallback"]["in_place_of"])
        self.assertEqual(str(self.data / (SAMPLES[0] + ".wiff2")), row["encoding_fallback"]["in_place_of_row"]["path"])
        self.assertEqual(str(self.data / (SAMPLES[0] + ".wiff2")),
                         lineage_stands_for(manifest)[str((self.data / (SAMPLES[0] + ".wiff")).resolve()).casefold()])

    def test_the_analysis_csv_is_built_on_the_wiff_as_the_samples_and_types_the_wiff2_had(self) -> None:
        self.switch(confirmed=True)
        built = build_repository_analysis_rows(read_manifest(self.manifest_path))

        self.assertEqual([], built["failures"])
        self.assertEqual([stem + ".wiff" for stem in SAMPLES], [Path(row["input_path"]).name for row in built["rows"]])
        self.assertEqual(list(SAMPLES), [row["sample_id"] for row in built["rows"]])
        self.assertEqual({("DDA", "campaign_disposition")},
                         {(row["acquisition_type"], row["acquisition_type_source"]) for row in built["rows"]})
        self.assertIn(wiff_fallback.WIFF_FALLBACK_WARNING, built["warnings"])
        self.assertEqual(wiff_fallback.WIFF_FALLBACK_RULE, built["encoding_fallback"]["rule"])

    def test_the_execution_gate_admits_the_wiff_and_holds_them_to_the_wiff2s_type(self) -> None:
        self.switch(confirmed=True)
        state = {"repository_run_manifest": str(self.manifest_path), "ion_mode": "Positive",
                 "files": [{"file_path": str(self.data / (stem + ".wiff")), "acquisition_type": "DDA"} for stem in SAMPLES]}
        self.assertEqual([], evaluate_repository_execution_gate(state)["blockers"])

        state["files"][0]["acquisition_type"] = "SWATH"
        self.assertTrue(any("campaign disposition decided" in item
                            for item in evaluate_repository_execution_gate(state)["blockers"]))
        stale = {**state, "files": [{"file_path": str(self.data / (stem + ".wiff2")), "acquisition_type": "DDA"}
                                    for stem in SAMPLES]}
        self.assertTrue(any("not among the files" in item for item in evaluate_repository_execution_gate(stale)["blockers"]))

    def test_a_diagnostic_that_failed_is_answered_too(self) -> None:
        self.change(lambda manifest: manifest["run_attempts"][0].update(exit_code=1, reason="exited"))
        self.assertTrue(self.switch(["dg1"], confirmed=True)["switched"])


class WhatIsNeverSwitched(_Part):
    def assertRefused(self, reason: str, jobs=("rn1",)) -> dict:
        before = self.manifest_path.read_bytes()
        result = self.switch(jobs, confirmed=True)
        self.assertEqual((False, False, reason), (result["ok"], result["switched"], result["reason"]), result)
        self.assertEqual(before, self.manifest_path.read_bytes())
        return result

    def test_the_switch_is_made_once(self) -> None:
        self.assertTrue(self.switch(confirmed=True)["switched"])
        refused = self.assertRefused(wiff_fallback.ALREADY_SWITCHED)
        self.assertEqual(["rn1"], [item["job_id"] for item in refused["encoding_fallback"]["failed_jobs"]])

    def test_a_dia_unit_is_never_switched(self) -> None:
        for mode in ("SWATH", "DIA", "AIF"):
            with self.subTest(mode=mode):
                self.change(lambda manifest: manifest["project"].update(acquisition_mode=mode))
                self.assertRefused(wiff_fallback.NOT_DDA)

    def test_an_input_decided_as_another_type_is_never_switched(self) -> None:
        self.change(lambda manifest: manifest["raw_metadata_preflight"]["summary"]["per_file"][1].update(
            console_acquisition_type="SWATH"))
        self.assertRefused(wiff_fallback.NOT_DDA)

    def test_a_wiff_without_its_scan_is_no_fallback(self) -> None:
        (self.data / (SAMPLES[1] + ".wiff.scan")).unlink()
        self.assertRefused(wiff_fallback.WIFF_SCAN_MISSING)

    def test_a_wiff2_without_a_wiff_beside_it_leaves_the_whole_unit_as_it_is(self) -> None:
        (self.data / (SAMPLES[2] + ".wiff")).unlink()
        self.assertRefused(wiff_fallback.WIFF_NOT_BESIDE)

    def test_a_wiff_no_download_record_vouches_for_is_no_fallback(self) -> None:
        self.change(lambda manifest: manifest.update(downloads=[item for item in manifest["downloads"]
                                                                if not item["path"].endswith(".wiff")]))
        self.assertRefused(wiff_fallback.WIFF_PROVENANCE_UNRECORDED)

    def test_only_a_console_job_of_this_unit_that_failed_is_answered(self) -> None:
        self.assertRefused(wiff_fallback.NO_CONSOLE_FAILURE, jobs=())
        self.assertRefused(wiff_fallback.NO_CONSOLE_FAILURE, jobs=("rn9",))
        self.assertRefused(wiff_fallback.NO_CONSOLE_FAILURE, jobs=("dg1",))
        self.change(lambda manifest: manifest["run_attempts"][1].update(reason="cancelled", exit_code=-4))
        self.assertRefused(wiff_fallback.NO_CONSOLE_FAILURE)

    def test_a_unit_whose_run_finished_is_not_switched(self) -> None:
        self.change(lambda manifest: manifest.update(status="mztab_validated"))
        self.assertRefused(wiff_fallback.UNIT_NOT_SWITCHABLE)

    def test_a_unit_that_analyses_no_wiff2_has_nothing_to_switch(self) -> None:
        self.switch(confirmed=True)
        self.change(lambda manifest: manifest.pop("encoding_fallback"))
        self.assertRefused(wiff_fallback.NO_WIFF2_INPUT)


class UnderACampaignApproval(_Part):
    def test_an_approval_covering_boundary_4_stands_in_for_the_confirmation_and_is_recorded(self) -> None:
        result = self.switch(campaign_authorization_path=str(_authorization(self.root, [1, 3, 4, 5, "split"])))
        manifest = read_manifest(self.manifest_path)

        self.assertTrue(result["switched"])
        crossing = manifest["campaign_authorizations"][-1]
        self.assertEqual((4, wiff_fallback.ENTRY_POINT, UNIT), (crossing["boundary"], crossing["entry_point"], crossing["unit_id"]))
        self.assertEqual("approval-wiff", manifest["encoding_fallback"]["campaign_authorization"]["approval_id"])

    def test_an_approval_that_does_not_cover_it_is_a_refusal(self) -> None:
        before = self.manifest_path.read_bytes()
        result = self.switch(campaign_authorization_path=str(_authorization(self.root, [1, 3, 5])))

        self.assertEqual((False, "campaign_authorization_refused"), (result["ok"], result["reason"]))
        self.assertEqual(before, self.manifest_path.read_bytes())


class ASplitPart(_Part):
    """Review of #88: a part's manifest carries no downloads (the split writes none), and its parent's release counts
    the parts' inputs. A switched part is read through its raw owner and its record."""

    def test_a_part_without_downloads_of_its_own_is_switched_from_its_raw_owners_records(self) -> None:
        self.as_part()
        result = self.switch(confirmed=True)

        self.assertTrue(result["switched"], result)
        row = read_manifest(self.manifest_path)["input_lineage"]["rows"][0]
        self.assertEqual(hashlib.sha256(f"{SAMPLES[0]}.wiff".encode()).hexdigest(), row["checksums"]["sha256"])
        self.assertEqual(f"https://example.org/{SAMPLES[0]}.wiff", row["source"]["url"])

    def test_the_parent_is_released_as_before_once_its_part_switched(self) -> None:
        parent = self.as_part()
        before = [item for item in plan_split_parent_cleanup(parent)["blockers"] if "each once" in item]
        self.assertTrue(self.switch(confirmed=True)["switched"])
        after = [item for item in plan_split_parent_cleanup(parent)["blockers"] if "each once" in item]

        self.assertEqual([], before)
        self.assertEqual([], after, "the part's .wiff claim the parent's .wiff2 they stand in for")

    def test_a_part_whose_inputs_changed_otherwise_still_blocks_the_release(self) -> None:
        parent = self.as_part()
        self.switch(confirmed=True)
        self.change(lambda manifest: manifest["input_candidates"].pop())
        self.assertTrue([item for item in plan_split_parent_cleanup(parent)["blockers"] if "each once" in item])


TAR_URL = "https://repository.massbank.jp/api/download/MPST000048.0"
ARCHIVE_ROOT = "MB-POST_files_MPST000048.0"


def _key(path: str) -> str:
    return str(Path(path).resolve()).casefold()


class AnArchiveMember(_Part):
    """0.5.57. MB-POST serves an accession as one tar, so every .wiff, .wiff.scan and .wiff2 of an MB-POST unit came out
    of it: no download record names a .wiff of its own, and the switch refused every MB-POST unit
    (wiff_provenance_unrecorded) although the raw owner's lease recorded the .wiff as archive members. The fixture's
    parent is shaped as the v6-mpst48-20261031 parent manifest is: one tar download whose declared checksum is its first
    member's, never verified as the tar's; extracted_files; one archive extraction with its member listing; and an
    allow-list validation that verified every declared member checksum."""

    FOLDER = ARCHIVE_ROOT

    def as_archive_part(self, sizes: dict | None = None) -> Path:
        """The unit as a split part of a parent that downloaded one tar and extracted every file from it. ``sizes``
        overrides the listed size of a member, by file name."""
        workspace = self.data.parent.parent
        provenance = workspace / "provenance"
        tar = self.root / "mb_post" / "MPST000048" / "_dl" / "o" / "dab0caa64111410f" / "obj" / "MPST000048.tar"
        tar.parent.mkdir(parents=True)
        tar.write_bytes(b"tar")
        tar_sha256 = hashlib.sha256(b"tar").hexdigest()
        members = sorted(path for path in self.files.iterdir() if path.is_file())
        lines = ["path\ttype\tsize\tcrc32\tmodified\tdepth\tarchive\tmember_name\tdisposition",
                 f"{ARCHIVE_ROOT}\tdir\t0\t\t2026-10-31 14:38:05\t1\tMPST000048.tar\t{ARCHIVE_ROOT}\textracted"]
        for path in members:
            size = (sizes or {}).get(path.name, path.stat().st_size)
            lines.append(f"{ARCHIVE_ROOT}/{path.name}\tfile\t{size}\t\t2025-06-17 07:48:56\t1\tMPST000048.tar\t"
                         f"{ARCHIVE_ROOT}/{path.name}\textracted")
        listing = provenance / f"archive-members-{tar_sha256[:12]}.tsv"
        listing.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
        md5 = {path.name: hashlib.md5(path.read_bytes()).hexdigest() for path in members}
        part = read_manifest(self.manifest_path)
        parent_path = provenance / "parent-manifest.json"
        _write_json(parent_path, {
            "schema": "msdial-public-reanalysis-run.v1", "status": "split_by_acquisition",
            "workspace": str(workspace), "raw_directory": str(self.data.parent), "input_directory": str(self.data),
            "raw_retention_policy": "delete_after_validated_output", "execution_allowed": False,
            "downloads": [{
                "path": str(tar), "size_bytes": 3, "sha256": tar_sha256, "md5": hashlib.md5(b"tar").hexdigest(),
                "source_url": TAR_URL, "declared_checksum": md5[members[0].name], "declared_checksum_verified": None,
                "archive": {"format": "tar", "name_format": "tar", "signature": "tar", "name_mismatch": False},
            }],
            "extracted_files": [str(path) for path in members],
            "allowlist_checksum_validation": {"required": True, "verified": len(members), "skipped": 0,
                                              "archives_verified_at_download": 0, "archives": []},
            "archive_extractions": [{
                "schema": "msdial-archive-extraction.v1", "format": "tar", "archive_name": "MPST000048.tar",
                "archive_path": str(tar), "archive_sha256": tar_sha256, "destination": str(self.data),
                "members_tsv": {"path": str(listing), "sha256": hashlib.sha256(listing.read_bytes()).hexdigest(),
                                "rows": len(lines) - 1},
                "nested": [], "rejected_members": [], "crc_verified": False, "placement": "",
                "source_url": TAR_URL, "download_path": str(tar), "extracted_file_count": len(members),
            }],
            "input_candidates": list(part["input_candidates"]),
            "split_into": [{"analysis_unit_id": UNIT, "manifest_path": str(self.manifest_path),
                            "input_candidates": list(part["input_candidates"])}],
            "project": {"analysis_unit_id": "u48", "repository": "mb_post",
                        "files": [{"name": path.name, "size_bytes": path.stat().st_size, "url": TAR_URL,
                                   "checksum": md5[path.name]} for path in members]},
        })

        def split(manifest: dict) -> None:
            # A part downloads nothing; its project lists only its own .wiff2, as the split writes it.
            manifest.pop("downloads")
            manifest["project"]["files"] = [item for item in manifest["project"]["files"]
                                            if item["name"].endswith(".wiff2")]
            manifest["split_from"] = {"analysis_unit_id": "u48", "manifest_path": str(parent_path), "acquisition_mode": "DDA"}
            manifest["raw_owned_by"] = str(parent_path)

        self.change(split)
        self.md5 = md5
        return parent_path

    def assertRefusedUnrecorded(self, said: str) -> None:
        before = self.manifest_path.read_bytes()
        result = self.switch(confirmed=True)
        self.assertEqual((False, wiff_fallback.WIFF_PROVENANCE_UNRECORDED), (result["ok"], result["reason"]), result)
        self.assertIn(said, result["detail"])
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_a_part_whose_parent_downloaded_a_tar_is_switched_on_its_archive_members(self) -> None:
        parent = self.as_archive_part()
        result = self.switch(confirmed=True)

        self.assertTrue(result["switched"], result)
        manifest = read_manifest(self.manifest_path)
        self.assertEqual([str(self.files / (stem + ".wiff")) for stem in SAMPLES], manifest["input_candidates"])
        row = manifest["input_lineage"]["rows"][0]
        name = SAMPLES[0] + ".wiff"
        self.assertEqual((str(self.files / name), "extracted_member", SAMPLES[0]),
                         (row["path"], row["kind"], row["sample_id"]))
        tar = read_manifest(parent)["downloads"][0]
        self.assertEqual({"archive": {"url": TAR_URL, "download_path": tar["path"], "sha256": tar["sha256"],
                                      "md5": tar["md5"], "declared_checksum": tar["declared_checksum"],
                                      "declared_checksum_verified": None},
                          "member": f"{ARCHIVE_ROOT}/{name}"}, row["source"])
        self.assertEqual({"declared": self.md5[name], "declared_algorithm": "md5", "declared_verified": True},
                         row["checksums"], "the member's own declared MD5, which the allow-list validation verified")
        basis = row["basis"]
        self.assertEqual(("member_declared_checksum", "md5", self.md5[name], True),
                         (basis["kind"], basis["algorithm"], basis["value"], basis["verified"]))
        self.assertEqual("extracted from an archive, and its own published MD5 matched", basis["statement"])
        self.assertEqual([tar["path"]], [item["download_path"] for item in basis["archives"]])
        self.assertEqual([(f"{ARCHIVE_ROOT}/{name}", "MPST000048.tar")],
                         [(item["member"], item["listed_archive"]) for item in basis["listing"]])
        self.assertEqual(read_manifest(parent)["archive_extractions"][0]["members_tsv"]["sha256"],
                         basis["listing"][0]["members_tsv_sha256"])
        self.assertEqual(str(self.files / (SAMPLES[0] + ".wiff2")), row["encoding_fallback"]["in_place_of"])
        self.assertEqual(f"{ARCHIVE_ROOT}/{name}", manifest["encoding_fallback"]["swaps"][0]["to_relative"])

        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        self.assertEqual(list(SAMPLES), [row["sample_id"] for row in built["rows"]])
        state = {"repository_run_manifest": str(self.manifest_path), "ion_mode": "Positive",
                 "files": [{"file_path": str(self.files / (stem + ".wiff")), "acquisition_type": "DDA"} for stem in SAMPLES]}
        self.assertEqual([], evaluate_repository_execution_gate(state)["blockers"])

    def test_the_row_is_the_one_the_lease_writes_for_an_extracted_member(self) -> None:
        """The same helper writes both (repository_reanalysis.extracted_member_fields): the row build_input_lineage
        would have written for the .wiff, had the encoding rule used it, is the switched row's provenance."""
        from msdial_app.repository_reanalysis import RepositoryProject, build_input_lineage

        parent = read_manifest(self.as_archive_part())
        self.switch(confirmed=True)
        switched = read_manifest(self.manifest_path)["input_lineage"]["rows"][0]
        target = str(self.files / (SAMPLES[0] + ".wiff"))
        built = build_input_lineage(
            [target], [], {_key(target): parent["downloads"][0]}, self.data, self.root / "nowhere",
            RepositoryProject(repository="mb_post", accession="MPST000048"),
            {_key(target): {"declared": self.md5[SAMPLES[0] + ".wiff"], "declared_algorithm": "md5", "verified": True}},
            extracted_members={_key(target): {"member": f"{ARCHIVE_ROOT}/{SAMPLES[0]}.wiff",
                                              "archive": "MPST000048.tar", "extraction": 0}},
            archive_extractions=parent["archive_extractions"],
        )["rows"][0]
        self.assertEqual({key: built[key] for key in ("kind", "source", "checksums", "basis")},
                         {key: switched[key] for key in ("kind", "source", "checksums", "basis")})

    def test_a_member_whose_size_disagrees_with_its_listing_is_refused(self) -> None:
        self.as_archive_part(sizes={SAMPLES[1] + ".wiff": 4692480})
        self.assertRefusedUnrecorded("and the member listing of MPST000048.tar gives 4692480")

    def test_a_scan_whose_size_disagrees_with_its_listing_is_refused(self) -> None:
        self.as_archive_part(sizes={SAMPLES[2] + ".wiff.scan": 32940003})
        self.assertRefusedUnrecorded(f"{SAMPLES[2]}.wiff.scan: it is")

    def test_a_wiff_no_extraction_record_and_no_download_accounts_for_is_refused(self) -> None:
        parent = self.as_archive_part()
        update_manifest(parent, lambda manifest: manifest.update(
            extracted_files=[item for item in manifest["extracted_files"] if not item.endswith(SAMPLES[0] + ".wiff")]))
        self.assertRefusedUnrecorded("none of the files the unit's lease extracted")

    def test_a_member_listing_that_changed_since_the_extraction_vouches_for_nothing(self) -> None:
        parent = self.as_archive_part()
        listing = Path(read_manifest(parent)["archive_extractions"][0]["members_tsv"]["path"])
        listing.write_bytes(listing.read_bytes() + b"\n")
        self.assertRefusedUnrecorded("has changed since its sha256 was recorded")

    def test_a_listing_that_does_not_list_the_wiff_as_extracted_vouches_for_nothing(self) -> None:
        parent = self.as_archive_part()
        listing = Path(read_manifest(parent)["archive_extractions"][0]["members_tsv"]["path"])
        text = listing.read_text(encoding="utf-8").replace(
            f"{ARCHIVE_ROOT}/{SAMPLES[0]}.wiff\textracted", f"{ARCHIVE_ROOT}/{SAMPLES[0]}.wiff\tdropped_metadata")
        listing.write_bytes(text.encode("utf-8"))
        update_manifest(parent, lambda manifest: manifest["archive_extractions"][0]["members_tsv"].update(
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest()))
        self.assertRefusedUnrecorded("no member listing of the unit's archives lists it as extracted")

    def test_without_a_validation_that_accounts_for_every_declared_checksum_the_member_claims_none(self) -> None:
        parent = self.as_archive_part()
        update_manifest(parent, lambda manifest: manifest["allowlist_checksum_validation"].update(verified=3))
        self.assertTrue(self.switch(confirmed=True)["switched"])
        row = read_manifest(self.manifest_path)["input_lineage"]["rows"][0]
        self.assertEqual(("extracted_member", {}), (row["kind"], row["checksums"]))
        self.assertEqual("archive_download_hash", row["basis"]["kind"], "the tar's declared checksum was never its own")

    def test_the_parent_is_released_as_before_once_its_part_switched_on_archive_members(self) -> None:
        parent = self.as_archive_part()
        self.assertTrue(self.switch(confirmed=True)["switched"])
        self.assertEqual([], [item for item in plan_split_parent_cleanup(parent)["blockers"] if "each once" in item])

    def test_the_methods_text_counts_the_switched_members_by_their_basis(self) -> None:
        from msdial_app.repository_reanalysis import input_integrity_statement

        self.as_archive_part()
        self.switch(confirmed=True)
        self.assertEqual("Of the 3 analysis inputs, 3 were extracted from an archive, and its own published MD5 "
                         "matched.", input_integrity_statement(read_manifest(self.manifest_path)))
        self.assertIn("The 3 SCIEX analysis file(s)",
                      _wiff_fallback_sentence({"repository_run_manifest": str(self.manifest_path)}))


class TheWiffHeaders(_Part):
    """Each .wiff's own header is read at the switch and recorded beside it, never in the preflight's per-file records:
    the switch is refused where one cannot be read, holds several samples, is not DDA or states another polarity."""

    def test_each_wiff_header_is_read_and_recorded_beside_the_switch(self) -> None:
        self.switch(confirmed=True)
        manifest = read_manifest(self.manifest_path)
        headers = manifest["encoding_fallback"]["wiff_headers"]

        self.assertEqual([stem + ".wiff" for stem in SAMPLES], sorted(self.extractor.read))
        self.assertEqual([(stem + ".wiff", stem + ".wiff2", "Positive", "DDA", 1) for stem in SAMPLES],
                         [(item["file_relative"], item["in_place_of_relative"], item["polarity"],
                           item["header_console_acquisition_type"], item["sample_count"]) for item in headers])
        self.assertEqual(str(self.extractor_file), manifest["encoding_fallback"]["extractor"]["path"])
        per_file = manifest["raw_metadata_preflight"]["summary"]["per_file"]
        self.assertTrue(all(item["file"].endswith(".wiff2") for item in per_file), "the decision record stays the .wiff2's")

    def test_a_preview_reads_no_header(self) -> None:
        self.switch()
        self.assertEqual([], self.extractor.read)

    def assertRefusedFor(self, verdict, said: str) -> None:
        self.extractor.verdicts[SAMPLES[1] + ".wiff"] = verdict
        before = self.manifest_path.read_bytes()
        result = self.switch(confirmed=True)
        self.assertEqual((False, wiff_fallback.WIFF_HEADER_REFUSED), (result["ok"], result["reason"]), result)
        self.assertIn(said, result["detail"])
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_an_unreadable_header_refuses(self) -> None:
        self.assertRefusedFor("fail", "could not be read")

    def test_a_wiff_of_several_samples_refuses(self) -> None:
        self.assertRefusedFor({"samples": 2}, "lists 2 samples")

    def test_a_wiff_of_no_sample_or_no_sample_count_refuses(self) -> None:
        """Review of #88, low 1: exactly one sample, so a count of 0 and a record with no sample list refuse too."""
        self.assertRefusedFor({"samples": 0}, "lists 0 samples")
        self.assertRefusedFor({"samples": None}, "lists no sample count")

    def test_a_header_that_is_not_dda_refuses(self) -> None:
        self.assertRefusedFor({"method": "DIA"}, "not DDA")

    def test_a_header_of_another_polarity_refuses(self) -> None:
        self.assertRefusedFor({"polarity": "Negative"}, "states Negative, and the unit runs in Positive")

    def test_without_a_single_ion_mode_the_wiff2s_recorded_polarity_is_the_one_compared(self) -> None:
        def both(manifest: dict) -> None:
            manifest["project"]["ion_mode"] = "Both"
            manifest["polarity_inferences"] = [{"path": item, "polarity": "Positive", "polarity_source": "file_name"}
                                               for item in manifest["input_candidates"]]

        self.change(both)
        self.assertTrue(self.switch(confirmed=True)["switched"])


class AThresholdMeasuredOnTheFilesThatRun(_Part):
    """Review of #88, low 4: a production run of a switched unit runs at a threshold a diagnostic measured on the
    .wiff, never one a superseded diagnostic, one before the switch, or one of a file that no longer runs produced."""

    def _state(self, threshold) -> dict:
        return {"repository_run_manifest": str(self.manifest_path), "ion_mode": "Positive", "minimum_peak_height": threshold,
                "files": [{"file_path": str(self.data / (stem + ".wiff")), "acquisition_type": "DDA"} for stem in SAMPLES]}

    def _diagnostic(self, threshold: int, representative: str, recorded_at: str = "2099-01-01T00:00:00+00:00") -> dict:
        return {"recorded_at": recorded_at, "job_id": f"dg-{threshold}", "minimum_peak_height": threshold,
                "representative": {"file_path": str(self.data / representative), "file_name": Path(representative).stem}}

    def blocked(self, threshold, *, production: bool = True) -> bool:
        gate = evaluate_repository_execution_gate(self._state(threshold), production=production)
        return any("no peak-count diagnostic measured on the files that now run" in item for item in gate["blockers"])

    def test_the_wiff2_diagnostics_threshold_is_refused_and_the_wiffs_runs(self) -> None:
        self.change(lambda manifest: manifest.update(
            peak_height_diagnostics=[self._diagnostic(1200, SAMPLES[0] + ".wiff2", "2026-10-30T00:00:00+00:00")]))
        self.switch(confirmed=True)

        self.assertTrue(self.blocked(1200), "measured before the switch, on the .wiff2, and superseded")
        self.assertFalse(self.blocked(1200, production=False), "the diagnostic itself is not held to a threshold")
        self.change(lambda manifest: manifest["peak_height_diagnostics"].append(
            self._diagnostic(900, SAMPLES[0] + ".wiff")))
        self.assertFalse(self.blocked(900))
        self.assertTrue(self.blocked(1200))

    def test_a_diagnostic_after_the_switch_on_a_file_that_no_longer_runs_does_not_count(self) -> None:
        self.switch(confirmed=True)
        self.change(lambda manifest: manifest.setdefault("peak_height_diagnostics", []).append(
            self._diagnostic(900, SAMPLES[0] + ".wiff2")))
        self.assertTrue(self.blocked(900))

    def test_a_diagnostic_marked_superseded_does_not_count(self) -> None:
        self.switch(confirmed=True)

        def superseded(manifest: dict) -> None:
            item = self._diagnostic(900, SAMPLES[0] + ".wiff")
            item["superseded_by_encoding_fallback"] = {"rule": wiff_fallback.WIFF_FALLBACK_RULE}
            manifest.setdefault("peak_height_diagnostics", []).append(item)

        self.change(superseded)
        self.assertTrue(self.blocked(900))

    def test_a_unit_that_never_switched_is_not_held_to_it(self) -> None:
        self.change(lambda manifest: manifest.update(
            peak_height_diagnostics=[self._diagnostic(1200, SAMPLES[0] + ".wiff2", "2026-10-30T00:00:00+00:00")]))
        state = {**self._state(5), "files": [{"file_path": str(self.data / (stem + ".wiff2")), "acquisition_type": "DDA"}
                                             for stem in SAMPLES]}
        self.assertEqual([], [item for item in evaluate_repository_execution_gate(state, production=True)["blockers"]
                              if "peak-count diagnostic" in item])


class WhatTheSwitchSupersedes(_Part):
    def test_the_csv_the_prepared_run_and_the_latest_diagnostic_are_marked_superseded(self) -> None:
        output = self.data.parent.parent / "output"
        (output / "run-manifest.json").write_text("{}", encoding="utf-8")
        self.change(lambda manifest: manifest.update(
            analysis_csv={"status": "written", "path": str(output / "analysis_files.csv"), "rows": 3},
            peak_height_diagnostics=[{"job_id": "dg1", "minimum_peak_height": 1200, "recorded_at": "2026-10-30"}]))
        self.switch(confirmed=True)
        manifest = read_manifest(self.manifest_path)
        superseded = manifest["encoding_fallback"]["superseded"]

        self.assertEqual("superseded", manifest["analysis_csv"]["status"])
        self.assertEqual(wiff_fallback.WIFF_FALLBACK_RULE,
                         manifest["peak_height_diagnostics"][-1]["superseded_by_encoding_fallback"]["rule"])
        self.assertEqual(("written", "dg1"), (superseded["analysis_csv"]["status"], superseded["diagnostic"]["job_id"]))
        self.assertEqual(str(output / "run-manifest.json"), superseded["prepared_run"]["run_manifest"])

    def test_the_methods_text_says_the_wiff_was_read_in_place_of_the_wiff2_and_why(self) -> None:
        workflow = {"repository_run_manifest": str(self.manifest_path)}
        self.assertEqual("", _wiff_fallback_sentence(workflow))
        self.switch(confirmed=True)
        sentence = _wiff_fallback_sentence(workflow)
        self.assertIn("The 3 SCIEX analysis file(s)", sentence)
        self.assertIn("production MS-DIAL Console run on the .wiff2 files failed or timed out", sentence)
        self.assertIn(wiff_fallback.WIFF_FALLBACK_RULE, sentence)


class TheReviewsLowFindings(_Part):
    def test_a_wiff2_that_is_no_file_refuses_rather_than_being_dropped(self) -> None:
        (self.data / (SAMPLES[0] + ".wiff2")).unlink()
        result = self.switch(confirmed=True)
        self.assertEqual(wiff_fallback.WIFF2_NOT_A_FILE, result["reason"])

    def test_a_record_under_any_rule_refuses_a_second_switch(self) -> None:
        self.change(lambda manifest: manifest.update(encoding_fallback={"rule": "some_other_rule"}))
        result = self.switch(confirmed=True)
        self.assertEqual(wiff_fallback.ALREADY_SWITCHED, result["reason"])


if __name__ == "__main__":
    unittest.main()

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
    from msdial_app.repository_analysis_rows import build_repository_analysis_rows
    from msdial_app.repository_reanalysis import (
        _write_json,
        evaluate_repository_execution_gate,
        lineage_stands_for,
        read_manifest,
        update_manifest,
    )

SAMPLES = ("241202_Tomita_18_pos", "241202_Tomita_19_pos", "241202_Tomita_20_pos")
UNIT = "u48-dda-pos"


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

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.data = self.root / "mb_post" / "MPST000048" / UNIT / "raw" / "data"
        self.data.mkdir(parents=True)
        workspace = self.data.parent.parent
        (workspace / "provenance").mkdir()
        (workspace / "output").mkdir()
        downloads, files, rows, per_file, declared, samples = [], [], [], [], [], []
        for stem in SAMPLES:
            for suffix in (".wiff2", ".wiff", ".wiff.scan", ".timeseries.data"):
                path = self.data / (stem + suffix)
                payload = f"{stem}{suffix}".encode("utf-8")
                path.write_bytes(payload)
                md5 = hashlib.md5(payload).hexdigest()
                downloads.append({"path": str(path), "source_url": f"https://example.org/{path.name}",
                                  "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "md5": md5,
                                  "declared_checksum": md5, "declared_checksum_verified": True})
                files.append({"name": path.name, "size_bytes": len(payload), "url": f"https://example.org/{path.name}",
                              "role": "raw" if suffix == ".wiff2" else "raw_alternate" if suffix == ".wiff" else "sidecar"})
            wiff2 = str(self.data / (stem + ".wiff2"))
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
            "input_candidates": [str(self.data / (stem + ".wiff2")) for stem in SAMPLES],
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

    def tearDown(self) -> None:
        self._directory.cleanup()

    def change(self, function) -> None:
        update_manifest(self.manifest_path, function)

    def switch(self, jobs=("rn1",), **options) -> dict:
        return mcp_server.msdial_switch_repository_unit_to_wiff(str(self.manifest_path), list(jobs), **options)


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


if __name__ == "__main__":
    unittest.main()

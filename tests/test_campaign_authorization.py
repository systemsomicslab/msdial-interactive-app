"""One recorded campaign approval, checked the same way at every step it stands in for.

A campaign of several hundred units cannot be confirmed unit by unit in a conversation. What replaces
those confirmations is one approval of one campaign manifest digest, covering named boundaries for named
units - and the designs for it had invented five different "campaign modes". This is the one record,
msdial-campaign-authorization.v1, and the one question asked of it.

Three properties matter more than any other and are pinned here:

- Without an approval nothing changes: every step still asks for confirmed=true.
- An approval that was offered and does not hold is a refusal, even with confirmed=true beside it. A
  caller that passed an approval and had it silently ignored would believe it had been checked.
- Every crossing made under an approval is written into the unit's own manifest before the step runs.

Library entries are named by file name and sha256; a record that carries a location is refused, because
the record is copied into every unit's provenance and the production library is private. The paths in
these tests are synthetic.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import mcp_server, server
    from msdial_app.campaign_authorization import (
        CampaignAuthorization,
        CampaignAuthorizationError,
        authorize,
    )
    from msdial_app.repository_reanalysis import (
        RepositoryFile,
        RepositoryProject,
        _write_json,
        create_download_lease,
        finalize_download_lease,
        read_manifest,
    )

TEMPLATE = Path(__file__).resolve().parents[1] / "resources" / "msdial_console_param4lipidomics.txt"
DIGEST = "sha256:" + "ab" * 32


def _record(**overrides) -> dict:
    record = {
        "schema": "msdial-campaign-authorization.v1",
        "approval_id": "approval-0001",
        "campaign_id": "declared-pool",
        "manifest_digest": DIGEST,
        "approved_by": "A. Person",
        "approved_at": "2026-09-30T10:00:00+09:00",
        "statement": "Run the listed units as planned.",
        "covers": [1, 3, 4, 5, "split"],
        "units": ["unit-neg", "unit-mixed"],
        "raw_retention_policy": "delete_after_validated_output",
        "libraries": [{"name": "Synthetic-Private-Neg.msp", "sha256": "cd" * 32}],
        "revoked_at": None,
    }
    record.update(overrides)
    return record


class _Records:
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write(self, name: str = "approval.json", **overrides) -> Path:
        path = self.root / "campaign" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_record(**overrides)), encoding="utf-8")
        return path


class TheRecordAndItsOneQuestion(_Records, unittest.TestCase):
    def test_a_covered_boundary_for_a_listed_unit_gives_a_crossing_record(self) -> None:
        path = self.write()

        crossing = authorize(path, "unit-neg", 4, entry_point="agent_run")

        self.assertEqual("approval-0001", crossing["approval_id"])
        self.assertEqual(DIGEST, crossing["manifest_digest"])
        self.assertEqual(4, crossing["boundary"])
        self.assertEqual("unit-neg", crossing["unit_id"])
        self.assertEqual("listed", crossing["covered_as"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), crossing["authorization_sha256"])

    def test_no_record_means_no_change(self) -> None:
        for empty in ("", None, "   "):
            self.assertIsNone(authorize(empty, "unit-neg", 1, entry_point="x"))

    def test_another_unit_is_refused(self) -> None:
        with self.assertRaises(CampaignAuthorizationError) as caught:
            authorize(self.write(), "unit-pos", 1, entry_point="x")
        self.assertIn("unit_not_covered", caught.exception.codes)
        self.assertTrue(str(caught.exception).startswith("campaign_authorization_refused [unit_not_covered]"))

    def test_a_revoked_approval_is_refused(self) -> None:
        with self.assertRaises(CampaignAuthorizationError) as caught:
            authorize(self.write(revoked_at="2026-10-01T00:00:00Z"), "unit-neg", 4, entry_point="x")
        self.assertIn("revoked", caught.exception.codes)

    def test_an_uncovered_boundary_is_refused(self) -> None:
        with self.assertRaises(CampaignAuthorizationError) as caught:
            authorize(self.write(covers=[1, 3]), "unit-neg", 4, entry_point="x")
        self.assertIn("boundary_not_covered", caught.exception.codes)

    def test_boundaries_2_and_6_can_never_be_covered(self) -> None:
        for boundary in (2, 6):
            with self.subTest(boundary=boundary):
                with self.assertRaises(CampaignAuthorizationError) as caught:
                    CampaignAuthorization.load(self.write(covers=[1, boundary]))
                self.assertIn(f"covers_{boundary}", caught.exception.codes)
                with self.assertRaises(CampaignAuthorizationError):
                    authorize(self.write(), "unit-neg", boundary, entry_point="x")

    def test_a_deletion_needs_an_approval_that_deletes(self) -> None:
        with self.assertRaises(CampaignAuthorizationError) as caught:
            authorize(self.write(raw_retention_policy="keep"), "unit-neg", 5, entry_point="x")
        self.assertIn("retention_keep", caught.exception.codes)

    def test_a_step_asking_for_another_retention_is_refused(self) -> None:
        with self.assertRaises(CampaignAuthorizationError) as caught:
            authorize(self.write(), "unit-neg", 1, entry_point="x", raw_retention_policy="keep")
        self.assertIn("retention_mismatch", caught.exception.codes)

    def test_a_library_location_in_the_record_is_refused(self) -> None:
        """The record is copied into every unit's provenance; a private library's location must not be."""
        for entry in (
            {"name": "Synthetic.msp", "sha256": "cd" * 32, "path": "X:\\synthetic\\lib\\Synthetic.msp"},
            {"name": "X:\\synthetic\\lib\\Synthetic.msp", "sha256": "cd" * 32},
            {"name": "//server/share/Synthetic.msp", "sha256": "cd" * 32},
        ):
            with self.subTest(entry=entry):
                with self.assertRaises(CampaignAuthorizationError) as caught:
                    CampaignAuthorization.load(self.write(libraries=[entry]))
                self.assertIn("library_location_recorded", caught.exception.codes)

    def test_a_malformed_digest_or_an_empty_scope_is_refused(self) -> None:
        for overrides, code in (
            ({"manifest_digest": "sha256:abc"}, "manifest_digest"),
            ({"units": []}, "units"),
            ({"covers": []}, "covers"),
            ({"schema": "something-else"}, "schema"),
            ({"approval_id": ""}, "incomplete"),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(CampaignAuthorizationError) as caught:
                    CampaignAuthorization.load(self.write(**overrides))
                self.assertIn(code, caught.exception.codes)

    def test_a_named_campaign_manifest_must_still_hash_to_the_approved_digest(self) -> None:
        manifest = self.root / "campaign" / "campaign-manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('{"units": ["unit-neg"]}', encoding="utf-8")
        digest = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()

        self.assertTrue(
            authorize(
                self.write(manifest_digest=digest, campaign_manifest_path=str(manifest)),
                "unit-neg", 1, entry_point="x", raw_retention_policy="delete_after_validated_output",
            )
        )
        manifest.write_text('{"units": ["unit-neg", "unit-pos"]}', encoding="utf-8")
        with self.assertRaises(CampaignAuthorizationError) as caught:
            CampaignAuthorization.load(self.write(manifest_digest=digest, campaign_manifest_path=str(manifest)))
        self.assertIn("manifest_digest_mismatch", caught.exception.codes)

    def test_an_unreadable_record_is_refused_not_ignored(self) -> None:
        with self.assertRaises(CampaignAuthorizationError) as caught:
            authorize(self.root / "absent.json", "unit-neg", 1, entry_point="x")
        self.assertIn("unreadable", caught.exception.codes)

    def test_a_part_is_covered_through_its_parent_only_when_the_split_is(self) -> None:
        crossing = authorize(
            self.write(), "unit-mixed-dda", 4, entry_point="x", parent_unit_id="unit-mixed"
        )
        self.assertEqual("derived_part_of:unit-mixed", crossing["covered_as"])

        with self.assertRaises(CampaignAuthorizationError):
            authorize(self.write(covers=[1, 4]), "unit-mixed-dda", 4, entry_point="x", parent_unit_id="unit-mixed")
        with self.assertRaises(CampaignAuthorizationError):
            # The id alone proves nothing; the parent comes from the part's own manifest.
            authorize(self.write(), "unit-mixed-dda", 4, entry_point="x")


def _handoff(unit_id: str = "unit-neg") -> dict:
    return {
        "schema": "msdial-repository-reanalysis-handoff.v1",
        "repository": "mb_post",
        "accession": "MPST-CAMPAIGN",
        "analysis_unit_id": unit_id,
        "technical_settings": {
            "separation": "LC-MS",
            "ion_mode": "Negative",
            "acquisition_mode": "DDA",
            "target_omics": "Lipidomics",
            "untargeted": True,
        },
        "files": [
            {
                "path": "FILES/sample_neg.raw",
                "role": "raw",
                "size_bytes": 1024,
                "download_url": "https://example.org/MPST-CAMPAIGN.tar",
            }
        ],
        "sample_metadata": [{"sample_id": "sample_neg", "raw_file": "sample_neg.raw", "attributes": {}}],
        "class_proposal": {"proposal_id": "class-1", "assignments": []},
        "blocking_reasons": [],
        "download_scope": {"file_count": 1, "bundle_bytes": 2048},
        "sample_count": 1,
    }


class TheDownloadUnderAnApproval(_Records, unittest.TestCase):
    PURPOSE = "Annotate every experimental spectrum."

    def _download(self, **kwargs):
        return mcp_server.msdial_download_repository_raw(
            "mb_post",
            "MPST-CAMPAIGN",
            str(self.root / "analysis"),
            analysis_unit_handoff=_handoff(kwargs.pop("unit_id", "unit-neg")),
            analysis_purpose=self.PURPOSE,
            **kwargs,
        )

    def test_without_an_approval_the_confirmation_is_still_asked_for(self) -> None:
        with patch.object(mcp_server, "_request_json") as request:
            result = self._download()

        request.assert_not_called()
        self.assertFalse(result["started"])
        self.assertTrue(result["confirmation_required"])

    def test_a_covering_approval_stands_in_for_the_confirmation_and_is_passed_on(self) -> None:
        path = self.write()
        with patch.object(mcp_server, "_request_json", return_value={"job_id": "download-job"}) as request:
            result = self._download(
                raw_retention_policy="delete_after_validated_output", campaign_authorization_path=str(path)
            )

        self.assertTrue(result["started"], result)
        self.assertEqual(str(path), request.call_args.kwargs["body"]["campaign_authorization_path"])
        self.assertEqual("approval-0001", result["preview"]["campaign_authorization"]["approval_id"])

    def test_an_approval_that_does_not_hold_is_refused_even_with_confirmed_true(self) -> None:
        with patch.object(mcp_server, "_request_json") as request:
            result = self._download(
                unit_id="unit-pos",
                confirmed=True,
                raw_retention_policy="delete_after_validated_output",
                campaign_authorization_path=str(self.write()),
            )

        request.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual("campaign_authorization_refused", result["reason"])
        self.assertIn("unit_not_covered", result["codes"])

    def test_the_approval_lifts_no_size_limit(self) -> None:
        with patch.object(mcp_server, "_request_json") as request:
            result = self._download(
                maximum_gb=0.000001,
                raw_retention_policy="delete_after_validated_output",
                campaign_authorization_path=str(self.write()),
            )

        request.assert_not_called()
        self.assertTrue(result["blocked"])
        self.assertIn("size_limit:exceeded", result["preview"]["blocking_reasons"])

    def test_the_plan_tools_report_coverage_and_block_an_uncovered_unit(self) -> None:
        path = str(self.write())
        batch = mcp_server.msdial_repository_batch_plan(
            [_handoff("unit-neg"), _handoff("unit-pos")],
            str(self.root / "analysis"),
            raw_retention_policy="delete_after_validated_output",
            analysis_purpose=self.PURPOSE,
            campaign_authorization_path=path,
        )
        runs = {run["analysis_unit_id"]: run for run in batch["runs"]}

        self.assertTrue(runs["unit-neg"]["campaign_authorization"]["valid"])
        self.assertTrue(runs["unit-neg"]["ready"])
        self.assertFalse(runs["unit-pos"]["ready"])
        self.assertIn("campaign_authorization:unit_not_covered", runs["unit-pos"]["blocking_reasons"])

    def test_the_backend_checks_again_and_the_lease_records_the_crossing(self) -> None:
        project, _ = mcp_server._project_from_analysis_unit_handoff(_handoff("unit-neg"))
        path = str(self.write())
        started: list[tuple] = []
        backend = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()

        def post(body: dict) -> tuple[int, dict]:
            request = urllib.request.Request(
                f"http://127.0.0.1:{backend.server_port}/api/repository/download",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                payload = json.loads(error.read().decode("utf-8"))
                error.close()
                return error.code, payload

        try:
            with patch.object(server, "_run_repository_download_job", lambda *a, **k: started.append((a, k))), \
                    patch.object(server, "_persist_jobs_locked", lambda: None), patch.object(server, "JOBS", {}):
                body = {
                    "project": project,
                    "workspace_root": str(self.root / "analysis"),
                    "maximum_gb": 1,
                    "raw_retention_policy": "delete_after_validated_output",
                    "campaign_authorization_path": path,
                }
                accepted = post(body)
                refused = post({**body, "raw_retention_policy": "keep"})
        finally:
            backend.shutdown()
            backend.server_close()
            thread.join(timeout=5)

        self.assertEqual(200, accepted[0], accepted[1])
        self.assertEqual("approval-0001", accepted[1]["campaign_authorization"]["approval_id"])
        self.assertEqual("approval-0001", started[0][1]["campaign_authorization"]["approval_id"])
        self.assertEqual(400, refused[0])
        self.assertTrue(refused[1]["error"].startswith("campaign_authorization_refused [retention_mismatch]"))
        self.assertEqual(1, len(started), "a refused approval starts no download")

        class _Client:
            def download(self, url, destination, _maximum, progress_callback=None):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"x")
                return {"path": str(destination), "size_bytes": 1, "sha256": "", "md5": ""}

        crossing = {**accepted[1]["campaign_authorization"], "job_id": "download-job"}
        lease = create_download_lease(
            RepositoryProject(
                repository="test", accession="LEASE", analysis_unit_id="unit-neg", eligible=True,
                selection_status="eligible",
                files=[RepositoryFile("FILES/sample_neg.raw", 1, "https://example.org/sample_neg.raw")],
                total_download_bytes=1,
                sample_metadata=[{"sample_id": "sample_neg", "raw_file": "sample_neg.raw"}],
            ),
            self.root / "analysis",
            100,
            client=_Client(),
            raw_retention_policy="delete_after_validated_output",
            campaign_authorization=crossing,
        )
        self.assertEqual([crossing], lease["campaign_authorizations"])
        self.assertEqual([crossing], read_manifest(lease["manifest_path"])["campaign_authorizations"])


class _ValidatedUnit(_Records):
    def unit(self, retention: str = "delete_after_validated_output", status_after: str = "") -> tuple[Path, Path]:
        root = self.root / "analysis" / "mb_post" / "MPST-CAMPAIGN" / "unit-neg"
        raw, output, provenance = root / "raw", root / "output", root / "provenance"
        for directory in (raw, output, provenance):
            directory.mkdir(parents=True)
        (raw / "sample_neg.raw").write_bytes(b"x" * 64)
        (output / "result.mzTab").write_text("MTD\tmzTab-version\t2.0.0-M\nSMH\tSML_ID\nSML\t1\n", encoding="ascii")
        manifest = provenance / "run-manifest.json"
        _write_json(
            manifest,
            {
                "status": "prepared",
                "workspace": str(root),
                "raw_directory": str(raw),
                "output_directory": str(output),
                "raw_retention_policy": retention,
                "cleanup_allowed": False,
                "project": {"analysis_unit_id": "unit-neg"},
            },
        )
        if status_after != "unfinalized":
            finalize_download_lease(manifest)
        return manifest, raw


class TheDeletionUnderAnApproval(_ValidatedUnit, unittest.TestCase):
    def _cleanup(self, manifest: Path, path: Path) -> dict:
        return mcp_server.msdial_cleanup_repository_raw(
            manifest_path=str(manifest), campaign_authorization_path=str(path)
        )

    def test_a_covering_deleting_approval_deletes_and_records_first(self) -> None:
        manifest, raw = self.unit()

        result = self._cleanup(manifest, self.write())
        recorded = read_manifest(manifest)

        self.assertTrue(result["deleted"], result)
        self.assertFalse(raw.exists())
        self.assertEqual("raw_cleaned", recorded["status"])
        self.assertEqual([5], [item["boundary"] for item in recorded["campaign_authorizations"]])

    def test_an_approval_that_keeps_raw_data_deletes_nothing(self) -> None:
        manifest, raw = self.unit()

        result = self._cleanup(manifest, self.write(raw_retention_policy="keep"))

        self.assertFalse(result["ok"])
        self.assertIn("retention_keep", result["codes"])
        self.assertTrue(raw.is_dir())

    def test_a_unit_that_chose_to_keep_its_raw_data_is_not_deleted_by_the_campaign(self) -> None:
        manifest, raw = self.unit(retention="keep")

        result = self._cleanup(manifest, self.write())

        self.assertFalse(result["ok"])
        self.assertIn("retention_mismatch", result["codes"])
        self.assertTrue(raw.is_dir())

    def test_every_guard_of_the_preview_still_applies(self) -> None:
        manifest, raw = self.unit(status_after="unfinalized")

        result = self._cleanup(manifest, self.write())

        self.assertFalse(result["deleted"])
        self.assertTrue(result["blockers"])
        self.assertTrue(raw.is_dir())
        self.assertNotIn("campaign_authorizations", read_manifest(manifest), "nothing crossed, nothing recorded")


class ThePreparationAndTheSplitUnderAnApproval(_Records, unittest.TestCase):
    MODES = {"a_DDA_1.mzML": "DDA", "b_DIA_1.mzML": "DIA", "c_DDA_2.mzML": "DDA", "d_DIA_2.mzML": "DIA"}

    def _unit(self) -> Path:
        from test_manifest_reentry import _Unit

        unit = _Unit()
        unit.MODES = self.MODES
        manifest = unit.make(self.root / "unit")
        _write_json(manifest, {**read_manifest(manifest), "project": {
            **read_manifest(manifest)["project"], "analysis_unit_id": "unit-mixed",
        }})
        self._fixture = unit
        return manifest

    def test_preparation_needs_no_confirmation_under_boundary_3_and_records_it(self) -> None:
        manifest = self._unit()
        with patch.object(mcp_server, "_request_json", side_effect=AssertionError("no backend")):
            prepared = mcp_server.msdial_prepare_repository_reanalysis(
                hierarchy=["Group"], manifest_path=str(manifest), campaign_authorization_path=str(self.write())
            )
            refused = mcp_server.msdial_prepare_repository_reanalysis(
                hierarchy=["Group"], manifest_path=str(manifest),
                campaign_authorization_path=str(self.write("no-class.json", covers=[1, 4])),
            )

        self.assertTrue(prepared["prepared"], prepared)
        self.assertEqual([3], [item["boundary"] for item in read_manifest(manifest)["campaign_authorizations"]])
        self.assertFalse(refused["ok"])
        self.assertIn("boundary_not_covered", refused["codes"])

    def test_the_split_needs_no_confirmation_when_the_approval_covers_it(self) -> None:
        from msdial_app.repository_reanalysis import run_raw_metadata_preflight

        manifest = self._unit()
        stub, run = self._fixture.extractor(self.root / "unit")
        with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=run):
            run_raw_metadata_preflight(manifest, stub)
        with patch.object(server, "JOBS", {}), patch.object(server, "_persist_jobs_locked", lambda: None):
            with self.assertRaises(CampaignAuthorizationError):
                server._split_repository_download(
                    {"manifest_path": str(manifest),
                     "campaign_authorization_path": str(self.write("no-split.json", covers=[1, 4]))}
                )
            self.assertEqual("preflight_mixed_acquisition", read_manifest(manifest)["status"])
            result = server._split_repository_download(
                {"manifest_path": str(manifest), "campaign_authorization_path": str(self.write())}
            )
        parent = read_manifest(manifest)

        self.assertTrue(result["written"], result)
        self.assertEqual("split_by_acquisition", parent["status"])
        self.assertEqual(["split"], [item["boundary"] for item in parent["campaign_authorizations"]])
        part = read_manifest(result["parts"][0]["manifest_path"])
        crossing = authorize(
            self.write(), part["project"]["analysis_unit_id"], 4, entry_point="agent_run",
            parent_unit_id=part["split_from"]["analysis_unit_id"],
        )
        self.assertEqual("derived_part_of:unit-mixed", crossing["covered_as"])


class TheRunUnderAnApproval(_Records, unittest.TestCase):
    """The production run and the diagnostic are checked and recorded by the backend."""

    def setUp(self) -> None:
        super().setUp()
        unit = self.root / "analysis" / "unit-neg"
        self.output = unit / "output"
        data = unit / "raw" / "data"
        data.mkdir(parents=True)
        self.output.mkdir(parents=True)
        self.sample = data / "sample.mzML"
        self.sample.write_text("", encoding="ascii")
        self.manifest = unit / "provenance" / "run-manifest.json"
        _write_json(
            self.manifest,
            {
                "status": "preflight_passed",
                "workspace": str(unit),
                "output_directory": str(self.output),
                "input_candidates": [str(self.sample)],
                "execution_allowed": True,
                "raw_retention_policy": "keep",
                "project": {"analysis_unit_id": "unit-neg", "ion_mode": "Negative"},
            },
        )
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "MSDIALCUI.exe").write_bytes(b"not really a console binary")
        (tools / "lab.lbm2").write_bytes(b"laboratory library")
        self.answers = {
            "project_type": "lcms",
            "ion_mode": "Negative",
            "target_omics": "Lipidomics",
            "parameter_strategy": "default",
            "execute_rt_correction": False,
            "library_strategy": "existing",
            "libraries": {"lbm_path": str(tools / "lab.lbm2")},
            "run_qa": False,
            "generate_materials_methods": False,
            "console_path": str(tools / "MSDIALCUI.exe"),
            "template_path": str(TEMPLATE),
            "output_root": str(self.output),
            "class_assignment_confirmed": True,
            "workflow_overrides": {"repository_run_manifest": str(self.manifest)},
        }
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.backend.shutdown()
        self.backend.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def _run(self, **body) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.backend.server_port}/api/agent/run",
            data=json.dumps({"input_path": str(self.sample.parent), "answers": self.answers, **body}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read().decode("utf-8"))
            error.close()
            return error.code, payload

    def test_without_an_approval_the_run_still_asks(self) -> None:
        with patch.object(server, "_run_job", lambda *args: None):
            status, response = self._run()

        self.assertEqual(200, status, response)
        self.assertFalse(response["started"])
        self.assertTrue(response["confirmation_required"])

    def test_a_covering_approval_starts_the_run_and_records_its_job(self) -> None:
        jobs: dict = {}
        with patch.object(server, "_run_job", lambda *args: None), patch.object(server, "JOBS", jobs), \
                patch.object(server, "_persist_jobs_locked", lambda: None):
            status, response = self._run(campaign_authorization_path=str(self.write()))

        self.assertEqual(200, status, response.get("error"))
        self.assertTrue(response["started"])
        crossing = read_manifest(self.manifest)["campaign_authorizations"][-1]
        self.assertEqual(4, crossing["boundary"])
        self.assertEqual(response["job_id"], crossing["job_id"])
        self.assertIn(response["job_id"], jobs)

    def test_an_approval_for_another_unit_is_refused_even_with_confirmed_true(self) -> None:
        jobs: dict = {}
        with patch.object(server, "_run_job", lambda *args: None), patch.object(server, "JOBS", jobs):
            status, response = self._run(
                confirmed=True, campaign_authorization_path=str(self.write(units=["unit-pos"]))
            )

        self.assertEqual(400, status)
        self.assertTrue(response["error"].startswith("campaign_authorization_refused [unit_not_covered]"))
        self.assertEqual({}, jobs)
        self.assertNotIn("campaign_authorizations", read_manifest(self.manifest))
        refused = mcp_server._refused_authorization_codes(response["error"])
        self.assertEqual(["unit_not_covered"], refused)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

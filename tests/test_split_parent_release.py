"""The release of a split parent's raw tree, and the approval-taking cleanup and discard.

A split parent owns the one raw tree every part reads. It was refused by every deletion: its status is not
cleanup-ready, and a part's raw directory is not its own. So every unit that split kept its raw data for
good, which at campaign scale fills the disk; and simply relabelling the parent raw_cleaned would break the
gate's SPL-1, which needs it split_by_acquisition. Now:

- plan_split_parent_cleanup / cleanup_split_parent release the tree once every part has ended - validated,
  failed after its retries, skipped, excluded or discarded - under a lock and an intent record that a later
  call resumes; the parent stays split_by_acquisition, and each part is told;
- cleanup_download_lease and discard_download_lease take a campaign approval for boundary 5, and under one a
  failed unit whose output holds an unvalidated mzTab-M is discarded with that mzTab-M kept;
- every deletion refuses while a retained artifact lies under its target, unlinks a multiply-linked file
  without touching its attributes, and is recorded in the manifest;
- the backend's post-run hook only records the pending plan: the runner is the one trigger;
- a unit whose raw data were released never runs again.

Every path here is synthetic.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import mcp_server, repository_reanalysis, run_finalisation, server, sharing
    from msdial_app.campaign_authorization import CampaignAuthorizationError
    from msdial_app.process_liveness import process_created_at
    from msdial_app.repository_reanalysis import (
        FAILURE_ARTIFACTS_DIRECTORY,
        FAILURE_RUN_RECORD,
        FAILURE_VALIDATION_RECORD,
        _write_json,
        cleanup_download_lease,
        cleanup_split_parent,
        discard_download_lease,
        evaluate_repository_execution_gate,
        finalize_download_lease,
        plan_split_parent_cleanup,
        read_manifest,
        record_campaign_authorization,
        run_raw_metadata_preflight,
        split_unit_by_acquisition,
        update_manifest,
    )

from test_repository_reanalysis import _MixedUnitFixture

WINDOWS = os.name == "nt"
HELD = "Windows refuses to delete a file another handle holds open; POSIX does not"
DELETE = "delete_after_validated_output"
VALID_MZTAB = "MTD\tmzTab-version\t2.0.0-M\nSMH\tSML_ID\nSML\t1\n"
# No MTD section: the validator fails it.
INVALID_MZTAB = "COM\tthe Console stopped before it wrote the metadata\n"


def _approval_record(**overrides) -> dict:
    record = {
        "schema": "msdial-campaign-authorization.v1",
        "approval_id": "approval-release",
        "campaign_id": "declared-pool",
        "manifest_digest": "sha256:" + "ab" * 32,
        "approved_by": "A. Person",
        "approved_at": "2026-10-01T10:00:00+09:00",
        "statement": "Run the listed units and delete their raw data as agreed.",
        "covers": [1, 3, 4, 5, "split"],
        "units": ["unit-mixed", "unit-neg"],
        "raw_retention_policy": DELETE,
        "libraries": [{"name": "Synthetic-Private-Neg.msp", "sha256": "cd" * 32}],
        "revoked_at": None,
    }
    record.update(overrides)
    return record


class _Approvals:
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def approval(self, name: str = "approval.json", **overrides) -> Path:
        path = self.root / "campaign" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_approval_record(**overrides)), encoding="utf-8")
        return path


class _SplitParent(_Approvals, _MixedUnitFixture):
    """A Mixed unit split into its DDA and DIA parts, as the preflight and the split write them."""

    def split(self, retention: str = DELETE) -> tuple[Path, dict[str, Path]]:
        manifest, extractor, _files = self._workspace(self.root / "analysis" / "unit-mixed", self.MODES)

        def policy(current: dict) -> None:
            current["raw_retention_policy"] = retention

        update_manifest(manifest, policy)
        with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(self.MODES)):
            run_raw_metadata_preflight(manifest, extractor)
        result = split_unit_by_acquisition(manifest, confirmed=True)
        self.assertTrue(result["written"], result["blockers"])
        self.parent = manifest
        self.raw = Path(read_manifest(manifest)["raw_directory"])
        # An MS-DIAL container a failed attempt left beside the inputs: it goes with the tree.
        (self.raw / "data" / "a_DDA_1_2026101012.dcl").write_bytes(b"superseded")
        return manifest, {part["acquisition_mode"]: Path(part["manifest_path"]) for part in result["parts"]}

    def validate(self, part: Path) -> None:
        output = Path(read_manifest(part)["output_directory"])
        (output / "AlignResult-2026101013.mzTab").write_text(VALID_MZTAB, encoding="ascii")
        finalized = finalize_download_lease(part)
        self.assertEqual("mztab_validated", finalized["status"])

    def run_job(self, part: Path, job_id: str, mztab: str | None, name: str = "AlignResult-2026101013.mzTab") -> dict:
        """One production job on a part through the backend's _run_job, its Console a stand-in that exits 0.

        It writes every expected export, and ``mztab`` as the run's mzTab-M under ``name`` (none when None).
        Returns the job's record.
        """
        recorded = read_manifest(part)
        output = Path(recorded["output_directory"])
        inputs = [Path(item) for item in recorded["input_candidates"]]
        csv_path = output / "analysis_files.csv"
        csv_path.write_text(
            "file_path,file_name,file_type,class_id,acquisition_type\n"
            + "".join(f"{path},{path.stem},Sample,All,DDA\n" for path in inputs),
            encoding="ascii",
        )
        (output / "workflow-settings.json").write_text(
            json.dumps({"project_type": "lcms", "repository_run_manifest": str(part), "output_root": str(output),
                        "files": [{"file_path": str(path), "file_name": path.stem} for path in inputs]}),
            encoding="utf-8",
        )
        (output / "run-manifest.json").write_text(json.dumps({"libraries": []}), encoding="utf-8")
        preparation = {
            "command": ["MSDIALCUI.exe"],
            "run_directory": str(output),
            "export_folder_path": str(output),
            "repository_run_manifest": str(part),
            "repository_raw_retention_policy": DELETE,
            "expected_analysis_exports": [str(output / f"{path.stem}.mdpeak") for path in inputs],
            "qa_matrix_expected": False,
            "input_csv": str(csv_path),
            "settings_file": str(output / "workflow-settings.json"),
            "manifest": str(output / "run-manifest.json"),
        }

        def console(_preparation, _log, **_watch):
            for path in inputs:
                (output / f"{path.stem}.mdpeak").write_bytes(b"Height\n")
            if mztab is not None:
                (output / name).write_text(mztab, encoding="ascii")
            return 0

        # As the run route takes it, so an earlier attempt's outputs are not this job's.
        baseline = server._snapshot_run_artifacts(preparation)
        jobs = {job_id: {"id": job_id, "status": "queued", "kind": "run", "logs": [], "preparation": preparation,
                         "artifact_baseline": baseline}}
        with patch.object(server, "JOBS", jobs), patch.object(server, "_persist_jobs_locked", lambda: None), \
                patch.object(server, "run_console", console):
            server._run_job(job_id, preparation)
        return jobs[job_id]

    def set_status(self, part: Path, status: str, **fields) -> None:
        def change(current: dict) -> None:
            current["status"] = status
            current.update(fields)

        update_manifest(part, change)


class TheSplitParentRelease(_SplitParent, unittest.TestCase):
    def test_without_an_approval_or_a_confirmation_nothing_is_deleted(self) -> None:
        parent, parts = self.split()
        for part in parts.values():
            self.validate(part)

        preview = cleanup_split_parent(parent)

        self.assertFalse(preview["deleted"])
        self.assertTrue(preview["confirmation_required"])
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertEqual(5, preview["deletion_file_count"])
        self.assertTrue(self.raw.is_dir())
        self.assertNotIn("raw_release", read_manifest(parent))

    def test_a_released_parent_deletes_its_tree_and_tells_every_part(self) -> None:
        parent, parts = self.split()
        for part in parts.values():
            self.validate(part)

        result = cleanup_split_parent(parent, campaign_authorization_path=self.approval())
        recorded = read_manifest(parent)
        release = recorded["raw_release"]

        self.assertTrue(result["deleted"], result.get("blockers"))
        self.assertFalse(self.raw.exists())
        # The parent stays what SPL-1 needs it to be, and records the release in full.
        self.assertEqual("split_by_acquisition", recorded["status"])
        self.assertFalse(recorded["execution_allowed"])
        self.assertEqual("msdial-split-parent-raw-release.v1", release["schema"])
        self.assertEqual(("deleted", "released"), (release["state"], release["kind"]))
        self.assertEqual((5, 5), (release["file_count"], release["passes"][-1]["removed_files"]))
        self.assertEqual("approval-release", release["authorized_by"]["approval_id"])
        self.assertEqual({"unit-mixed-dda", "unit-mixed-dia"}, {item["analysis_unit_id"] for item in release["parts"]})
        self.assertTrue(all(item["mztab_files"] and item["mztab_files"][0]["sha256"] for item in release["parts"]))
        self.assertEqual([5], [item["boundary"] for item in recorded["campaign_authorizations"] if item["boundary"] == 5])
        for part in parts.values():
            with self.subTest(part=part.parent.parent.name):
                after = read_manifest(part)
                self.assertEqual("raw_cleaned", after["status"])
                self.assertEqual(str(parent.resolve()), after["raw_released_by"])
                self.assertTrue(Path(after["output_directory"], "AlignResult-2026101013.mzTab").is_file())
        # Asked again, the release says it was made, and deletes nothing more.
        again = cleanup_split_parent(parent, campaign_authorization_path=self.approval())
        self.assertTrue(again["deleted"])
        self.assertTrue(again["already_released"])
        self.assertEqual(1, len(read_manifest(parent)["raw_release"]["passes"]))

    def test_a_validated_part_and_a_skipped_one_release_the_tree(self) -> None:
        parent, parts = self.split()
        self.validate(parts["DDA"])
        self.set_status(parts["DIA"], "skipped_by_preflight")

        result = cleanup_split_parent(parent, campaign_authorization_path=self.approval())

        self.assertTrue(result["deleted"], result.get("blockers"))
        self.assertEqual("released", read_manifest(parent)["raw_release"]["kind"])
        skipped = read_manifest(parts["DIA"])
        self.assertEqual("skipped_by_preflight", skipped["status"])
        self.assertIn("raw_released_at", skipped)
        self.assertEqual(str(parent.resolve()), skipped["raw_released_by"])

    def test_all_parts_skipped_gives_the_parent_discarded(self) -> None:
        parent, parts = self.split()
        for part in parts.values():
            self.set_status(part, "skipped_by_preflight")

        # The runner discards a parent none of whose parts produced validated output.
        result = discard_download_lease(parent, campaign_authorization_path=self.approval())
        release = read_manifest(parent)["raw_release"]

        self.assertTrue(result["deleted"], result.get("blockers"))
        self.assertFalse(self.raw.exists())
        self.assertEqual(("deleted", "discarded"), (release["state"], release["kind"]))
        self.assertEqual("split_by_acquisition", read_manifest(parent)["status"])
        for part in parts.values():
            self.assertEqual("skipped_by_preflight", read_manifest(part)["status"], "no part is marked raw_cleaned")

    def test_a_part_failed_after_its_retries_has_ended_and_one_with_retries_left_has_not(self) -> None:
        parent, parts = self.split()
        self.validate(parts["DDA"])
        failure = {"reason": "MS-DIAL Console exited with code 1.", "exit_code": 1}
        self.set_status(parts["DIA"], "run_failed", run_failures=[failure, failure])

        waiting = plan_split_parent_cleanup(parent)
        self.set_status(parts["DIA"], "run_failed", run_failures=[failure, failure, failure])
        ended = plan_split_parent_cleanup(parent)

        self.assertFalse(waiting["ready"])
        self.assertTrue(any("2 of 3 runs failed" in item for item in waiting["blockers"]), waiting["blockers"])
        self.assertTrue(ended["ready"], ended["blockers"])
        self.assertEqual("failed", {item["analysis_unit_id"]: item["state"] for item in ended["parts"]}["unit-mixed-dia"])

    def test_an_authorized_discard_of_a_part_ends_it_and_deletes_nothing(self) -> None:
        # Refused before production, retried, and ended by the runner: no run failure is recorded for it.
        parent, parts = self.split()
        self.validate(parts["DDA"])
        self.set_status(parts["DIA"], "prepared")

        ended = discard_download_lease(parts["DIA"], campaign_authorization_path=self.approval())
        part = read_manifest(parts["DIA"])

        self.assertFalse(ended["deleted"])
        self.assertTrue(ended["part_ended"])
        self.assertTrue(self.raw.is_dir(), "a part never deletes the tree its siblings read")
        self.assertEqual("discarded", part["status"])
        self.assertEqual(str(parent.resolve()), part["raw_release_deferred_to"])
        self.assertTrue(ended["split_parent_plan"]["ready"], ended["split_parent_plan"]["blockers"])
        self.assertTrue(cleanup_split_parent(parent, campaign_authorization_path=self.approval())["deleted"])

    def test_the_refusal_matrix(self) -> None:
        def keep(parent: Path, parts: dict[str, Path]) -> None:
            for path in (parent, *parts.values()):
                self.set_status(path, read_manifest(path)["status"], raw_retention_policy="keep")

        def not_ended(parent: Path, parts: dict[str, Path]) -> None:
            self.set_status(parts["DIA"], "prepared")

        def retries_left(parent: Path, parts: dict[str, Path]) -> None:
            self.set_status(parts["DIA"], "run_failed", run_failures=[{"reason": "exit 1"}])

        def artifact_missing(parent: Path, parts: dict[str, Path]) -> None:
            output = Path(read_manifest(parts["DIA"])["output_directory"])
            (output / "AlignResult-2026101013.mzTab").unlink()

        def artifact_under_tree(parent: Path, parts: dict[str, Path]) -> None:
            inside = self.raw / "data" / "qa-matrix.tsv"
            inside.write_text("x", encoding="ascii")
            self.set_status(
                parts["DIA"], "mztab_validated",
                retained_artifacts=[*read_manifest(parts["DIA"])["retained_artifacts"], str(inside)],
            )

        def not_a_partition(parent: Path, parts: dict[str, Path]) -> None:
            dia = read_manifest(parts["DIA"])
            self.set_status(parts["DIA"], dia["status"], input_candidates=dia["input_candidates"][:1])

        def another_owner(parent: Path, parts: dict[str, Path]) -> None:
            self.set_status(parts["DIA"], "mztab_validated", raw_owned_by=str(self.root / "other" / "run-manifest.json"))

        def raw_elsewhere(parent: Path, parts: dict[str, Path]) -> None:
            self.set_status(parent, "split_by_acquisition", workspace=str(self.root / "elsewhere"))

        def console_running(parent: Path, parts: dict[str, Path]) -> None:
            attempt = {
                "attempt_id": "a1", "job_id": "run9", "kind": "run", "started_at": "2026-10-01T00:00:00+00:00",
                "ended_at": None, "backend": {"pid": os.getpid(), "process_created_at": process_created_at()},
                "console_pid": None,
            }
            self.set_status(parts["DIA"], "mztab_validated", run_attempts=[attempt])

        def finalisation_held(parent: Path, parts: dict[str, Path]) -> None:
            hold = run_finalisation._hold(["raw_deletion"], "finalisation", "run9", "finalisation stopped")
            self.set_status(parts["DIA"], "mztab_validated", finalisation_holds=[hold])

        cases = {
            "the parent keeps its raw data": (keep, "raw retention policy is 'keep'"),
            "a part has not ended": (not_ended, "has not ended"),
            "a part failed with retries left": (retries_left, "1 of 3 runs failed"),
            "a part's retained artifact is missing": (artifact_missing, "retained artifacts are missing"),
            "a retained artifact lies under the tree": (artifact_under_tree, "under the parent's raw tree"),
            "the parts do not partition the inputs": (not_a_partition, "each once"),
            "a part reads another unit's tree": (another_owner, "raw_owned_by names another unit"),
            "the raw directory is not the workspace's": (raw_elsewhere, "expected 'raw' folder"),
            "a part's Console may be running": (console_running, "Console running"),
            "a part's finalisation is held": (finalisation_held, "MS-DIAL containers are still in the raw directory"),
        }
        for name, (mutate, phrase) in cases.items():
            with self.subTest(case=name):
                self.directory.cleanup()
                self.directory = tempfile.TemporaryDirectory()
                self.root = Path(self.directory.name)
                parent, parts = self.split()
                for part in parts.values():
                    self.validate(part)
                mutate(parent, parts)

                result = cleanup_split_parent(parent, confirmed=True)

                self.assertFalse(result["deleted"])
                self.assertTrue(any(phrase in item for item in result["blockers"]), result["blockers"])
                self.assertTrue(self.raw.is_dir())
                self.assertNotIn("raw_release", read_manifest(parent))

    def test_an_approval_that_does_not_hold_deletes_and_records_nothing(self) -> None:
        cases = {
            "boundary 5 is not covered": ({"covers": [1, 3, 4, "split"]}, "boundary_not_covered"),
            "another unit": ({"units": ["unit-other"]}, "unit_not_covered"),
            "the approval keeps raw data": ({"raw_retention_policy": "keep"}, "retention_keep"),
        }
        parent, parts = self.split()
        for part in parts.values():
            self.validate(part)
        for name, (overrides, code) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(CampaignAuthorizationError) as caught:
                    cleanup_split_parent(parent, campaign_authorization_path=self.approval(f"{code}.json", **overrides))
                self.assertIn(code, caught.exception.codes)
                self.assertTrue(self.raw.is_dir())
                self.assertNotIn("campaign_authorizations", read_manifest(parent))

    def test_a_crash_during_the_deletion_is_resumed(self) -> None:
        parent, parts = self.split()
        for part in parts.values():
            self.validate(part)
        real = repository_reanalysis.unlink_tree

        def crash(root):
            next(path for path in Path(root).rglob("*") if path.is_file()).unlink()
            raise RuntimeError("the backend stopped mid-deletion")

        with patch.object(repository_reanalysis, "unlink_tree", side_effect=crash):
            with self.assertRaises(RuntimeError):
                cleanup_split_parent(parent, campaign_authorization_path=self.approval())
        stopped = read_manifest(parent)["raw_release"]
        self.assertEqual("deleting", stopped["state"])
        self.assertEqual("mztab_validated", read_manifest(parts["DDA"])["status"], "no part is told before the tree goes")
        plan = plan_split_parent_cleanup(parent)
        self.assertTrue(plan["resumable"])

        with patch.object(repository_reanalysis, "unlink_tree", side_effect=real):
            resumed = cleanup_split_parent(parent, campaign_authorization_path=self.approval())
        release = read_manifest(parent)["raw_release"]

        self.assertTrue(resumed["deleted"], resumed.get("blockers"))
        self.assertFalse(self.raw.exists())
        self.assertEqual("deleted", release["state"])
        self.assertEqual(stopped["planned_at"], release["planned_at"])
        self.assertEqual(5, release["file_count"], "what was planned is what the first call found")
        self.assertEqual(1, len(release["resumed_at"]))
        self.assertEqual("raw_cleaned", read_manifest(parts["DDA"])["status"])

    @unittest.skipUnless(WINDOWS, HELD)
    def test_a_file_held_open_leaves_the_release_partial_until_it_is_let_go(self) -> None:
        parent, parts = self.split()
        for part in parts.values():
            self.validate(part)
        held = (self.raw / "data" / "b_DIA_1.mzML").open("rb")
        try:
            first = cleanup_split_parent(parent, campaign_authorization_path=self.approval())
        finally:
            held.close()
        partial = read_manifest(parent)["raw_release"]
        status_while_partial = read_manifest(parts["DIA"])["status"]
        second = cleanup_split_parent(parent, campaign_authorization_path=self.approval())
        release = read_manifest(parent)["raw_release"]

        self.assertFalse(first["deleted"])
        self.assertTrue(first["partial"])
        self.assertTrue(any("could not be removed" in item for item in first["blockers"]), first["blockers"])
        self.assertEqual(("partial", 1), (partial["state"], partial["remaining_file_count"]))
        self.assertEqual("busy", partial["passes"][0]["kept"][0]["reason"])
        self.assertEqual("mztab_validated", status_while_partial, "no part is told while the tree remains")
        self.assertTrue(second["deleted"], second.get("blockers"))
        self.assertEqual("deleted", release["state"])
        self.assertEqual(2, len(release["passes"]))
        self.assertFalse(self.raw.exists())

    def test_a_parts_own_cleanup_is_refused_and_shows_its_parents_plan(self) -> None:
        parent, parts = self.split()
        for part in parts.values():
            self.validate(part)

        preview = cleanup_download_lease(parts["DDA"])
        authorized = cleanup_download_lease(parts["DDA"], campaign_authorization_path=self.approval())

        self.assertTrue(any("not the expected 'raw' folder" in item for item in preview["blockers"]))
        self.assertTrue(preview["split_parent_plan"]["ready"], preview["split_parent_plan"]["blockers"])
        self.assertFalse(authorized["deleted"])
        self.assertTrue(self.raw.is_dir())

    def test_the_cleanup_tool_releases_a_split_parent(self) -> None:
        parent, parts = self.split()
        for part in parts.values():
            self.validate(part)

        preview = mcp_server.msdial_cleanup_repository_raw(manifest_path=str(parent))
        result = mcp_server.msdial_cleanup_repository_raw(
            manifest_path=str(parent), campaign_authorization_path=str(self.approval())
        )

        self.assertFalse(preview["deleted"])
        self.assertTrue(result["deleted"], result.get("blockers"))
        self.assertEqual(
            "msdial_cleanup_repository_raw",
            [item for item in read_manifest(parent)["campaign_authorizations"] if item["boundary"] == 5][0]["entry_point"],
        )


class ThePostRunHook(_SplitParent, unittest.TestCase):
    """A part's finished run records the parent's pending release and deletes nothing, approval or not."""

    def test_the_hook_records_a_pending_plan_and_only_an_authorized_call_deletes(self) -> None:
        parent, parts = self.split()
        record_campaign_authorization(parent, {"approval_id": "approval-release", "boundary": "split", "unit_id": "unit-mixed"})
        self.set_status(parts["DIA"], "skipped_by_preflight")

        job = self.run_job(parts["DDA"], "run1", VALID_MZTAB)

        recorded = read_manifest(parent)
        self.assertEqual("completed", job["status"], job.get("error"))
        self.assertEqual("cleanup_pending_confirmation", read_manifest(parts["DDA"])["status"])
        self.assertTrue(self.raw.is_dir(), "a run job never deletes raw data")
        self.assertTrue(recorded["raw_release_pending"]["ready"], recorded["raw_release_pending"]["blockers"])
        self.assertFalse(recorded["raw_release_pending"]["deleted"])
        self.assertNotIn("raw_release", recorded)
        self.assertTrue(job["repository_retention"]["split_parent"]["ready"])
        self.assertTrue(any("NOT performed" in line for line in job["logs"]))

        unauthorized = cleanup_split_parent(parent)
        self.assertFalse(unauthorized["deleted"])
        self.assertTrue(self.raw.is_dir())
        authorized = cleanup_split_parent(parent, campaign_authorization_path=self.approval())
        self.assertTrue(authorized["deleted"], authorized.get("blockers"))
        self.assertNotIn("raw_release_pending", read_manifest(parent))


class APartWithAPendingNewRunIsHeld(_SplitParent, unittest.TestCase):
    """Review r7-62: a part whose new run, prepared after a validated run, has not validated has not ended.

    Its raw data are held for that run as a unit's own are: its discard under an approval is refused, and the
    parent's release counts it as not ended, whatever its status and however many runs failed.
    """

    def new_run_on(self, part: Path) -> None:
        """Validate the part, then prepare a new run on it as a confirmed new_run=true prepare commits one."""
        from msdial_app.repository_reanalysis import start_new_production_run

        self.validate(part)
        record, _view = start_new_production_run({**read_manifest(part), "manifest_path": str(part)}, write=True)
        self.assertTrue(record["started"], record)

    def assert_held(self, parent: Path) -> None:
        plan = plan_split_parent_cleanup(parent)
        self.assertFalse(plan["ready"])
        self.assertEqual("pending", {item["analysis_unit_id"]: item["state"] for item in plan["parts"]}["unit-mixed-dia"])
        self.assertTrue(any("held for that new run" in item for item in plan["blockers"]), plan["blockers"])
        released = cleanup_split_parent(parent, campaign_authorization_path=self.approval())
        self.assertFalse(released["deleted"])
        self.assertTrue(self.raw.is_dir())
        self.assertNotIn("raw_release", read_manifest(parent))

    def test_review_r7_62_probe_an_approved_discard_of_the_part_is_refused(self) -> None:
        # The reviewer's probe: DDA validated, DIA left as a confirmed new_run=true prepare leaves it.
        from msdial_app.repository_reanalysis import plan_download_discard, superseded_validated_run

        parent, parts = self.split()
        self.validate(parts["DDA"])

        def superseded(current: dict) -> None:
            current["superseded_runs"] = [{"status": "mztab_validated", "cleanup_allowed": True,
                                           "output_directory": str(Path(current["output_directory"]))}]
            current["status"] = "preflight_passed"

        update_manifest(parts["DIA"], superseded)
        self.assertIsNotNone(superseded_validated_run(read_manifest(parts["DIA"])))
        self.assertTrue(any("A superseded run of this unit validated" in item
                            for item in plan_download_discard(parts["DIA"])["blockers"]))

        refused = discard_download_lease(parts["DIA"], campaign_authorization_path=self.approval())

        self.assertFalse(refused["deleted"])
        self.assertNotIn("part_ended", refused)
        self.assertTrue(any("A superseded run of this unit validated" in item for item in refused["blockers"]), refused)
        part = read_manifest(parts["DIA"])
        self.assertEqual("preflight_passed", part["status"])
        self.assertNotIn("discard_reason", part)
        self.assertNotIn("raw_release_deferred_to", part)
        self.assert_held(parent)

    def test_a_new_run_prepared_failed_or_failed_after_its_retries_holds_the_parent(self) -> None:
        parent, parts = self.split()
        self.validate(parts["DDA"])
        self.new_run_on(parts["DIA"])
        failure = {"reason": "MS-DIAL Console exited with code 1.", "exit_code": 1}
        for status, failures in (("preflight_passed", []), ("run_failed", [failure]),
                                 ("run_failed", [failure] * 3), ("validation_failed", [failure] * 3)):
            with self.subTest(status=status, failures=len(failures)):
                self.set_status(parts["DIA"], status, run_failures=failures)
                refused = discard_download_lease(parts["DIA"], campaign_authorization_path=self.approval())
                self.assertFalse(refused["deleted"])
                self.assertNotIn("part_ended", refused)
                self.assertEqual(status, read_manifest(parts["DIA"])["status"])
                self.assert_held(parent)

    def test_a_part_discarded_while_its_new_run_was_pending_still_holds_the_parent(self) -> None:
        # As 52b470b let an approval leave it.
        parent, parts = self.split()
        self.validate(parts["DDA"])
        self.new_run_on(parts["DIA"])
        self.set_status(parts["DIA"], "discarded", raw_release_deferred_to=str(parent.resolve()))

        self.assert_held(parent)

    def test_once_the_new_run_validates_the_parent_is_released(self) -> None:
        parent, parts = self.split()
        self.validate(parts["DDA"])
        self.new_run_on(parts["DIA"])
        self.assert_held(parent)

        self.validate(parts["DIA"])
        result = cleanup_split_parent(parent, campaign_authorization_path=self.approval())

        self.assertTrue(result["deleted"], result.get("blockers"))
        self.assertFalse(self.raw.exists())
        self.assertEqual("raw_cleaned", read_manifest(parts["DIA"])["status"])


class AnUnvalidatedRunIsAFailedRun(_SplitParent, unittest.TestCase):
    """A Console that exits 0 without a validated mzTab-M is recorded as a failed run, so its part can end.

    The hook recorded nothing for it: the job completed, the part stayed split_from_parent with no run failure,
    and its parent's raw tree was held for good however often it was retried.
    """

    def test_a_part_whose_mztab_fails_validation_three_times_has_ended(self) -> None:
        parent, parts = self.split()
        self.validate(parts["DIA"])

        for attempt in (1, 2, 3):
            job = self.run_job(parts["DDA"], f"run{attempt}", INVALID_MZTAB, name=f"AlignResult-20261010{attempt:02d}.mzTab")
            recorded = read_manifest(parts["DDA"])
            with self.subTest(attempt=attempt):
                self.assertEqual("completed", job["status"], "the job itself stays as it was")
                self.assertTrue(job["repository_retention"]["run_failure_recorded"])
                self.assertEqual(("run_failed", attempt), (recorded["status"], len(recorded["run_failures"])))
                self.assertFalse(recorded["cleanup_allowed"])
                self.assertEqual(0, recorded["run_failures"][-1]["exit_code"])
                self.assertIn("failed validation", recorded["run_failures"][-1]["reason"])
            if attempt < 3:
                self.assertFalse(plan_split_parent_cleanup(parent)["ready"])
        plan = plan_split_parent_cleanup(parent)

        self.assertTrue(plan["ready"], plan["blockers"])
        self.assertEqual("failed", {item["analysis_unit_id"]: item["state"] for item in plan["parts"]}["unit-mixed-dda"])
        self.assertTrue(self.raw.is_dir(), "a run job never deletes raw data")

    def test_a_run_that_wrote_no_mztab_is_a_failed_run(self) -> None:
        parent, parts = self.split()

        job = self.run_job(parts["DDA"], "run1", None)
        recorded = read_manifest(parts["DDA"])

        self.assertEqual("completed", job["status"])
        self.assertEqual(("run_failed", 1), (recorded["status"], len(recorded["run_failures"])))
        self.assertIn("wrote no mzTab-M", recorded["run_failures"][0]["reason"])

    def test_a_valid_run_beside_an_earlier_attempts_invalid_mztab_is_counted_as_validation_failed(self) -> None:
        # Finalisation validates every mzTab-M in the output, the failed attempt's too.
        parent, parts = self.split()
        self.run_job(parts["DDA"], "run1", INVALID_MZTAB, name="AlignResult-2026101001.mzTab")

        job = self.run_job(parts["DDA"], "run2", VALID_MZTAB, name="AlignResult-2026101002.mzTab")
        recorded = read_manifest(parts["DDA"])

        self.assertEqual("completed", job["status"])
        self.assertEqual(("validation_failed", 2), (recorded["status"], len(recorded["run_failures"])))
        self.assertTrue(recorded["finalized_at"], "the finalisation stands")
        self.assertIn("finalised as validation_failed", recorded["run_failures"][-1]["reason"])
        self.assertIsNone(job["repository_retention"]["cleanup"], "no deletion is requested")
        self.assertNotIn("raw_release_pending", read_manifest(parent))


class AReleasedUnitNeverRunsAgain(_SplitParent, unittest.TestCase):
    def _gate(self, manifest: Path) -> dict:
        payload = read_manifest(manifest)
        return evaluate_repository_execution_gate(
            {
                "repository_run_manifest": str(manifest),
                "output_root": payload["output_directory"],
                "files": [{"file_path": path, "acquisition_type": "DDA"} for path in payload["input_candidates"]],
            }
        )

    def test_raw_cleaned_is_refused(self) -> None:
        parent, parts = self.split()
        self.set_status(parts["DDA"], "raw_cleaned", execution_allowed=True)

        gate = self._gate(parts["DDA"])

        self.assertFalse(gate["allowed"])
        self.assertTrue(any("status 'raw_cleaned'" in item for item in gate["blockers"]), gate["blockers"])

    def test_discarded_is_refused(self) -> None:
        parent, parts = self.split()
        self.set_status(parts["DDA"], "discarded", execution_allowed=True)

        self.assertFalse(self._gate(parts["DDA"])["allowed"])

    def test_a_part_whose_parent_released_its_tree_is_refused(self) -> None:
        parent, parts = self.split()
        self.set_status(parts["DDA"], "preflight_passed", execution_allowed=True)
        self.set_status(parent, "split_by_acquisition", raw_release={"state": "partial"})

        gate = self._gate(parts["DDA"])

        self.assertFalse(gate["allowed"])
        self.assertTrue(any("parent's raw tree has been released" in item for item in gate["blockers"]), gate["blockers"])


class _Unit(_Approvals):
    """One unsplit repository unit: raw data, an output, and a manifest in the state a test sets."""

    def unit(self, *, status: str, retention: str = DELETE, mztab: str | None = None, **fields) -> tuple[Path, Path]:
        root = self.root / "analysis" / "mb_post" / "MPST-CAMPAIGN" / "unit-neg"
        raw, output, provenance = root / "raw", root / "output", root / "provenance"
        for directory in (raw / "data", output, provenance):
            directory.mkdir(parents=True, exist_ok=True)
        (raw / "data" / "sample_neg.mzML").write_bytes(b"x" * 64)
        (raw / "data" / "sample_neg_2026101012.dcl").write_bytes(b"y" * 16)
        if mztab is not None:
            (output / "AlignResult-2026101012.mzTab").write_text(mztab, encoding="ascii")
        manifest = provenance / "run-manifest.json"
        _write_json(
            manifest,
            {
                "status": status,
                "workspace": str(root),
                "raw_directory": str(raw),
                "output_directory": str(output),
                "raw_retention_policy": retention,
                "cleanup_allowed": False,
                "project": {"analysis_unit_id": "unit-neg"},
                **fields,
            },
        )
        return manifest, raw


class TheAuthorizedCleanupAndDiscard(_Unit, unittest.TestCase):
    FAILURES = [{"reason": f"MS-DIAL Console exited with code 1 (attempt {n}).", "exit_code": 1} for n in (1, 2, 3)]

    def test_a_failed_unit_with_an_unvalidated_mztab_is_discarded_and_its_mztab_kept(self) -> None:
        manifest, raw = self.unit(status="run_failed", mztab=INVALID_MZTAB, run_failures=self.FAILURES)
        output = Path(read_manifest(manifest)["output_directory"])
        mztab = output / "AlignResult-2026101012.mzTab"

        with self.assertRaisesRegex(ValueError, "mzTab-M output exists"):
            discard_download_lease(manifest, confirmed=True)
        self.assertTrue(raw.is_dir(), "without an approval such a unit is refused, as before")
        result = discard_download_lease(manifest, campaign_authorization_path=self.approval())
        recorded = read_manifest(manifest)

        self.assertTrue(result["deleted"], result.get("blockers"))
        self.assertFalse(raw.exists())
        self.assertEqual("discarded", recorded["status"])
        self.assertEqual(INVALID_MZTAB, mztab.read_text(encoding="ascii"), "the mzTab-M is kept, unchanged")
        kept = {item["kind"]: item for item in recorded["failure_artifacts"]["files"]}
        self.assertEqual({"mztab", "mztab_validation", "run_failure_record"}, set(kept))
        validation = json.loads((output / FAILURE_ARTIFACTS_DIRECTORY / FAILURE_VALIDATION_RECORD).read_text(encoding="utf-8"))
        self.assertEqual("failed", validation["summary"]["status"])
        self.assertEqual("failed", recorded["failure_artifacts"]["mztab_validation_status"])
        failure = json.loads((output / FAILURE_ARTIFACTS_DIRECTORY / FAILURE_RUN_RECORD).read_text(encoding="utf-8"))
        self.assertEqual(3, len(failure["run_failures"]))
        for item in kept.values():
            self.assertTrue(Path(item["path"]).is_file())
        deletion = recorded["raw_deletion"]
        self.assertEqual(("discard", "deleted"), (deletion["kind"], deletion["state"]))
        self.assertEqual((2, 80), (deletion["file_count"], deletion["bytes"]))
        self.assertEqual([str(mztab.resolve())], deletion["kept"]["mztab_kept"])
        self.assertEqual("approval-release", deletion["authorized_by"]["approval_id"])
        self.assertEqual([5], [item["boundary"] for item in recorded["campaign_authorizations"]])
        self.assertIn("failure artifacts", recorded["discard_reason"])
        # The kept records are no mzTab-M: nothing later mistakes them for an output to validate.
        from msdial_app.mztab_validation import find_mztab_files

        self.assertEqual([mztab.resolve()], find_mztab_files(output))

    def test_a_refused_discard_under_an_approval_deletes_and_records_nothing(self) -> None:
        manifest, raw = self.unit(status="mztab_validated", mztab=VALID_MZTAB)

        result = discard_download_lease(manifest, campaign_authorization_path=self.approval())

        self.assertFalse(result["deleted"])
        self.assertTrue(any("must use the normal cleanup" in item for item in result["blockers"]))
        self.assertTrue(raw.is_dir())
        self.assertNotIn("campaign_authorizations", read_manifest(manifest))
        self.assertNotIn("failure_artifacts", read_manifest(manifest))

    def test_a_unit_that_keeps_its_raw_data_is_not_discarded_under_a_deleting_approval(self) -> None:
        manifest, raw = self.unit(status="skipped_by_preflight", retention="keep")

        with self.assertRaises(CampaignAuthorizationError) as caught:
            discard_download_lease(manifest, campaign_authorization_path=self.approval())

        self.assertIn("retention_mismatch", caught.exception.codes)
        self.assertTrue(raw.is_dir())

    def test_a_retained_artifact_under_the_target_refuses_the_cleanup_and_the_discard(self) -> None:
        manifest, raw = self.unit(status="prepared", mztab=VALID_MZTAB)
        finalize_download_lease(manifest)
        inside = raw / "data" / "quality-control.tsv"
        inside.write_text("kept", encoding="ascii")

        def retain(current: dict) -> None:
            current["retained_artifacts"] = [*current["retained_artifacts"], str(inside)]

        update_manifest(manifest, retain)

        with self.assertRaisesRegex(ValueError, "lie under the raw directory"):
            cleanup_download_lease(manifest, confirmed=True)
        authorized = cleanup_download_lease(manifest, campaign_authorization_path=self.approval())
        self.assertFalse(authorized["deleted"])
        self.assertTrue(any("lie under the raw directory" in item for item in authorized["blockers"]))
        self.set_retained_status(manifest, "validation_failed")
        with self.assertRaisesRegex(ValueError, "lie under the raw directory"):
            discard_download_lease(manifest, confirmed=True)
        self.assertTrue(inside.is_file())

    def set_retained_status(self, manifest: Path, status: str) -> None:
        def change(current: dict) -> None:
            current["status"] = status
            current["cleanup_allowed"] = False
            # A failed validation leaves no mzTab-M worth keeping in this test, only the artifact under raw.
            for path in Path(current["output_directory"]).glob("*.mzTab"):
                path.unlink()

        update_manifest(manifest, change)

    def test_a_cleanup_under_an_approval_records_what_it_deleted_and_what_it_kept(self) -> None:
        manifest, raw = self.unit(status="prepared", mztab=VALID_MZTAB)
        finalize_download_lease(manifest)

        result = cleanup_download_lease(manifest, campaign_authorization_path=self.approval())
        recorded = read_manifest(manifest)
        deletion = recorded["raw_deletion"]

        self.assertTrue(result["deleted"], result.get("blockers"))
        self.assertEqual("raw_cleaned", recorded["status"])
        self.assertEqual(("cleanup", "deleted"), (deletion["kind"], deletion["state"]))
        self.assertEqual((2, 80), (deletion["file_count"], deletion["bytes"]))
        self.assertEqual(2, deletion["passes"][0]["removed_files"])
        self.assertEqual(len(recorded["retained_artifacts"]), deletion["kept"]["retained_artifact_count"])
        self.assertEqual(64, len(deletion["kept"]["retained_artifact_inventory_sha256"]))
        self.assertEqual("campaign_authorization", deletion["authorized_by"]["kind"])

    def test_a_multiply_linked_read_only_file_loses_this_name_only(self) -> None:
        manifest, raw = self.unit(status="prepared", mztab=VALID_MZTAB)
        finalize_download_lease(manifest)
        store = self.root / "analysis" / "mb_post" / "MPST-CAMPAIGN" / "_dl" / "o" / "obj"
        store.parent.mkdir(parents=True)
        store.write_bytes(b"shared object")
        os.chmod(store, stat.S_IREAD)
        try:
            os.link(store, raw / "data" / "linked.mzML")

            result = cleanup_download_lease(manifest, confirmed=True)

            self.assertTrue(result["deleted"], result.get("blockers"))
            self.assertFalse(raw.exists())
            self.assertEqual(b"shared object", store.read_bytes())
            self.assertEqual(1, os.stat(store).st_nlink)
            self.assertFalse(os.stat(store).st_mode & stat.S_IWRITE, "the store copy is still read-only")
            self.assertEqual(1, read_manifest(manifest)["raw_deletion"]["passes"][0]["removed_links"])
        finally:
            os.chmod(store, stat.S_IWRITE | stat.S_IREAD)

    def test_a_finished_discard_asked_for_again_rewrites_nothing(self) -> None:
        # A runner that stopped after the discard and before it recorded the result asks for it again.
        manifest, raw = self.unit(status="run_failed", mztab=INVALID_MZTAB, run_failures=self.FAILURES)
        first = discard_download_lease(manifest, campaign_authorization_path=self.approval())
        before = read_manifest(manifest)
        record = Path(before["output_directory"]) / FAILURE_ARTIFACTS_DIRECTORY / FAILURE_RUN_RECORD
        written = record.read_bytes()

        again = discard_download_lease(manifest, campaign_authorization_path=self.approval())
        confirmed = discard_download_lease(manifest, confirmed=True)

        self.assertTrue(first["deleted"], first.get("blockers"))
        for result in (again, confirmed):
            self.assertTrue(result["deleted"])
            self.assertTrue(result["already_discarded"])
            self.assertEqual(before["raw_deletion"], result["raw_deletion"])
        self.assertEqual(before, read_manifest(manifest), "the deletion's accounting, discarded_at and crossings stand")
        self.assertEqual((2, 80), (before["raw_deletion"]["file_count"], before["raw_deletion"]["bytes"]))
        self.assertEqual(written, record.read_bytes())
        self.assertEqual("run_failed", json.loads(written)["status"])
        self.assertEqual([5], [item["boundary"] for item in before["campaign_authorizations"]])

    def test_files_back_under_a_discarded_tree_are_refused_and_the_record_kept(self) -> None:
        manifest, raw = self.unit(status="skipped_by_preflight")
        discard_download_lease(manifest, campaign_authorization_path=self.approval())
        before = read_manifest(manifest)
        (raw / "data").mkdir(parents=True)
        (raw / "data" / "late.mzML").write_bytes(b"z")

        refused = discard_download_lease(manifest, campaign_authorization_path=self.approval())
        with self.assertRaisesRegex(ValueError, "holds 1 file"):
            discard_download_lease(manifest, confirmed=True)

        self.assertFalse(refused["deleted"])
        self.assertTrue(any("which no deletion of this unit made" in item for item in refused["blockers"]))
        self.assertEqual(before, read_manifest(manifest))
        self.assertTrue((raw / "data" / "late.mzML").is_file())

    def test_a_unit_discarded_before_deletions_were_recorded(self) -> None:
        manifest, raw = self.unit(status="discarded", discarded_at="2026-09-01T00:00:00+00:00")
        shutil.rmtree(raw)

        authorized = discard_download_lease(manifest, campaign_authorization_path=self.approval())
        self.assertTrue(authorized["already_discarded"])
        self.assertNotIn("raw_deletion", read_manifest(manifest))
        self.assertNotIn("campaign_authorizations", read_manifest(manifest))
        # Without an approval it is discarded again, as it always was.
        confirmed = discard_download_lease(manifest, confirmed=True)
        self.assertTrue(confirmed["deleted"])
        self.assertNotIn("already_discarded", confirmed)

    def test_a_discard_stopped_part_way_resumes_with_the_failure_artifacts_it_wrote_first(self) -> None:
        manifest, raw = self.unit(status="run_failed", mztab=INVALID_MZTAB, run_failures=self.FAILURES)

        with patch.object(repository_reanalysis, "unlink_tree", side_effect=RuntimeError("the backend stopped")):
            with self.assertRaises(RuntimeError):
                discard_download_lease(manifest, campaign_authorization_path=self.approval())
        stopped = read_manifest(manifest)
        record = Path(stopped["output_directory"]) / FAILURE_ARTIFACTS_DIRECTORY / FAILURE_RUN_RECORD
        written = record.read_bytes()
        resumed = discard_download_lease(manifest, campaign_authorization_path=self.approval())
        after = read_manifest(manifest)

        self.assertEqual(("deleting", "run_failed"), (stopped["raw_deletion"]["state"], stopped["status"]))
        self.assertTrue(resumed["deleted"], resumed.get("blockers"))
        self.assertFalse(raw.exists())
        self.assertEqual("discarded", after["status"])
        self.assertEqual(stopped["failure_artifacts"], after["failure_artifacts"])
        self.assertEqual(written, record.read_bytes(), "written from the failed unit, and not again")
        self.assertEqual(stopped["raw_deletion"]["planned_at"], after["raw_deletion"]["planned_at"])
        self.assertEqual((2, 80), (after["raw_deletion"]["file_count"], after["raw_deletion"]["bytes"]))
        self.assertEqual(1, len(after["raw_deletion"]["resumed_at"]))

    def test_the_failure_artifacts_carry_no_local_path(self) -> None:
        # The pinned Console prints a library's location when it cannot open it (CommonProcess.cs, ParseLibraries).
        location = r"\\synthetic-nas\private-libs\Synthetic_Private_Pos.msp"
        missing = r"D:\synthetic-elsewhere\out\sample_neg.mdpeak"
        failures = [
            {
                "reason": f"MS-DIAL returned success but produced 0 of 1 expected analysis exports. Missing: {missing}.",
                "exit_code": None,
                "recorded_at": "2026-10-01T10:00:00+09:00",
                "log_tail": [f"MSP file was not found: {location}", "Loading libraries"],
            }
        ]
        attempts = [
            {
                "attempt_id": "a1", "attempt": 1, "job_id": "run1", "kind": "run",
                "started_at": "2026-10-01T00:00:00+00:00", "ended_at": "2026-10-01T00:10:00+00:00",
                "exit_code": 0, "reason": "exited", "output_directory": r"D:\synthetic-elsewhere\out",
                "backend": {"pid": 4242, "host": "SYNTHETIC-HOST"},
                "console": {"version": "5.5.0", "binary_sha256": "ab" * 32},
                "detail": {"stopped_by": r"D:\synthetic-elsewhere\stop.flag"},
            }
        ]
        manifest, raw = self.unit(status="run_failed", mztab=INVALID_MZTAB, run_failures=failures, run_attempts=attempts)

        result = discard_download_lease(manifest, campaign_authorization_path=self.approval())
        recorded = read_manifest(manifest)
        directory = Path(recorded["output_directory"]) / FAILURE_ARTIFACTS_DIRECTORY

        self.assertTrue(result["deleted"], result.get("blockers"))
        for name in (FAILURE_RUN_RECORD, FAILURE_VALIDATION_RECORD):
            text = (directory / name).read_text(encoding="utf-8")
            with self.subTest(record=name):
                self.assertEqual([], [kind for kind, pattern in sharing._DETECTORS if pattern.search(text)])
                for local in ("synthetic-nas", "synthetic-elsewhere", "SYNTHETIC-HOST", "stop.flag"):
                    self.assertNotIn(local, text)
                self.assertNotIn(sharing.fold(str(self.root.resolve())), sharing.fold(text))
                self.assertEqual(sharing.PATH_POLICY, json.loads(text)["shared_path_policy"])
        failure = json.loads((directory / FAILURE_RUN_RECORD).read_text(encoding="utf-8"))
        self.assertIn("Missing: <local path withheld", failure["run_failures"][0]["reason"])
        self.assertEqual(2, failure["run_failures"][0]["log_tail_line_count"])
        self.assertNotIn("log_tail", failure["run_failures"][0])
        self.assertFalse({"output_directory", "backend", "detail", "console_pid"} & set(failure["run_attempts"][0]))
        self.assertEqual({"version": "5.5.0", "binary_sha256": "ab" * 32}, failure["run_attempts"][0]["console"])
        validation = json.loads((directory / FAILURE_VALIDATION_RECORD).read_text(encoding="utf-8"))
        self.assertEqual("output", validation["run_directory"])
        self.assertEqual(["output/AlignResult-2026101012.mzTab"], [item["file"] for item in validation["files"]])
        # The full record stays where it was, in the provenance manifest.
        self.assertEqual(failures[0]["log_tail"], recorded["run_failures"][0]["log_tail"])
        self.assertEqual("SYNTHETIC-HOST", recorded["run_attempts"][0]["backend"]["host"])

    def test_a_record_the_redaction_misses_is_written_without_its_free_text(self) -> None:
        missing = r"D:\synthetic-elsewhere\out\sample_neg.mdpeak"
        failures = [{"reason": f"Missing: {missing}.", "exit_code": 1, "log_tail": []}]
        manifest, raw = self.unit(status="run_failed", mztab=INVALID_MZTAB, run_failures=failures)

        with patch.object(sharing.SharingContext, "text", lambda _self, value: value), \
                patch.object(sharing.SharingContext, "view", lambda _self, value: value):
            result = discard_download_lease(manifest, campaign_authorization_path=self.approval())
        kept = {item["kind"]: item for item in read_manifest(manifest)["failure_artifacts"]["files"]}

        self.assertTrue(result["deleted"], result.get("blockers"))
        for kind in ("run_failure_record", "mztab_validation"):
            with self.subTest(kind=kind):
                text = Path(kept[kind]["path"]).read_text(encoding="utf-8")
                self.assertTrue(kept[kind]["free_text_withheld"])
                self.assertEqual([], [name for name, pattern in sharing._DETECTORS if pattern.search(text)])
        failure = json.loads(Path(kept["run_failure_record"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual({"exit_code", "recorded_at", "log_tail_line_count"}, set(failure["run_failures"][0]))

    def test_the_discard_tool_previews_and_takes_an_approval(self) -> None:
        manifest, raw = self.unit(status="skipped_by_preflight")

        preview = mcp_server.msdial_discard_repository_raw(manifest_path=str(manifest))
        refused = mcp_server.msdial_discard_repository_raw(
            manifest_path=str(manifest), campaign_authorization_path=str(self.approval(units=["unit-pos"]))
        )
        result = mcp_server.msdial_discard_repository_raw(
            manifest_path=str(manifest), campaign_authorization_path=str(self.approval())
        )

        self.assertFalse(preview["deleted"])
        self.assertEqual(2, preview["plan"]["deletion_file_count"])
        self.assertEqual([], preview["plan"]["blockers"])
        self.assertFalse(refused["ok"])
        self.assertIn("unit_not_covered", refused["codes"])
        self.assertTrue(result["deleted"], result.get("blockers"))
        self.assertFalse(raw.exists())
        self.assertEqual("discarded", read_manifest(manifest)["status"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

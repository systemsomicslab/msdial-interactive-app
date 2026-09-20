"""The retention policy must outlive the job registry that used to be its only home.

The policy is chosen once, at download, and decides whether a unit's raw data may ever be deleted.
It was held in the in-memory JOBS registry alone, which is persisted truncated to the hundred most
recently updated jobs -- so at campaign scale later work evicted the policy while the data it
governed was still on disk.

cleanup_download_lease's preview already reported `manifest.get("raw_retention_policy")`, and
nothing had ever written that key, so it reported None. A person asked to confirm an irreversible
deletion was shown a blank where the intent should have been: the field existed, the read existed,
and the write did not.

The manifest is the unit's own durable record and outlives every registry, so the policy is written
there at download time.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_app.repository_reanalysis import cleanup_download_lease


class TheCleanupPreviewReportsTheChosenPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.workspace = self.root / "unit"
        self.raw = self.workspace / "raw"
        self.raw.mkdir(parents=True)
        (self.raw / "sample.raw").write_bytes(b"0123456789")
        self.retained = self.workspace / "output" / "result.mzTab"
        self.retained.parent.mkdir(parents=True)
        self.retained.write_text("MTD\tmzTab-version\t2.0.0-M\n", encoding="utf-8")
        self.manifest = self.workspace / "run-manifest.json"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _write_manifest(self, **extra) -> None:
        manifest = {
            "schema": "msdial-public-reanalysis-run.v1",
            "status": "mztab_validated",
            "workspace": str(self.workspace),
            "raw_directory": str(self.raw),
            "output_directory": str(self.workspace / "output"),
            "cleanup_allowed": True,
            "retained_artifacts": [str(self.retained)],
            "retained_artifact_inventory": [{"path": str(self.retained)}],
        }
        manifest.update(extra)
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    def test_the_policy_reaches_the_person_confirming_the_deletion(self) -> None:
        """THE REGRESSION. This field read None however the download was requested."""
        self._write_manifest(raw_retention_policy="delete_after_validated_output")

        preview = cleanup_download_lease(self.manifest, confirmed=False)

        self.assertEqual("delete_after_validated_output", preview["retention_policy"])
        self.assertTrue(preview["ready_for_confirmation"], preview["blockers"])
        self.assertTrue(self.raw.exists(), "a preview deletes nothing")

    def test_a_keep_policy_is_reported_as_keep_rather_than_as_nothing(self) -> None:
        """"keep" and "the policy was lost" must not look the same to a reader."""
        self._write_manifest(raw_retention_policy="keep")

        self.assertEqual("keep", cleanup_download_lease(self.manifest, confirmed=False)["retention_policy"])

    def test_a_manifest_written_before_this_change_still_previews(self) -> None:
        """Units downloaded earlier carry no policy key; they must still be inspectable.

        Reporting None is the honest answer for those -- the intent genuinely was not recorded --
        and it must not become a blocker, because the guards that matter are status, cleanup_allowed
        and the retained artifacts, not the policy.
        """
        self._write_manifest()

        preview = cleanup_download_lease(self.manifest, confirmed=False)

        self.assertIsNone(preview["retention_policy"])
        self.assertTrue(preview["ready_for_confirmation"], preview["blockers"])

    def test_the_policy_does_not_authorise_anything_by_itself(self) -> None:
        """A delete policy is a wish, not an approval, and the guards outrank it.

        This is the contract's rule: deletion needs a human confirmation after validated output and
        a retained-artifact inventory. A unit that asked for deletion but has not validated must
        still be refused.
        """
        self._write_manifest(
            raw_retention_policy="delete_after_validated_output",
            status="prepared",
            cleanup_allowed=False,
        )

        preview = cleanup_download_lease(self.manifest, confirmed=False)

        self.assertFalse(preview["ready_for_confirmation"])
        self.assertTrue(any("cleanup_allowed" in blocker for blocker in preview["blockers"]))
        self.assertTrue(self.raw.exists())


class _FakeClient:
    """Stands in for the network: writes the bytes the lease expects and reports them."""

    def download(self, _url, destination, _maximum_bytes, progress_callback=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"12345678")
        if progress_callback:
            progress_callback(8, 8)
        return {"path": str(destination), "size_bytes": 8, "sha256": "", "md5": ""}


class TheDownloadWritesThePolicyIntoTheManifest(unittest.TestCase):
    """The other half: the read above is only useful if something writes the key.

    A field that is read and never written is the shape this programme keeps finding, and it is
    what this was: cleanup_download_lease had asked the manifest for raw_retention_policy since it
    was written, and create_download_lease had never put one there.
    """

    def test_the_chosen_policy_is_recorded_where_it_outlives_the_job(self) -> None:
        from msdial_app.repository_reanalysis import (
            RepositoryFile,
            RepositoryProject,
            create_download_lease,
        )

        project = RepositoryProject(
            repository="test",
            accession="RETENTION",
            eligible=True,
            selection_status="eligible",
            files=[RepositoryFile("sample.mzML", 8, "https://example.org/sample.mzML")],
            total_download_bytes=8,
        )
        with tempfile.TemporaryDirectory() as temporary:
            lease = create_download_lease(
                project,
                Path(temporary),
                100,
                client=_FakeClient(),
                raw_retention_policy="delete_after_validated_output",
            )
            manifest = json.loads(Path(lease["manifest_path"]).read_text(encoding="utf-8"))

        self.assertEqual("delete_after_validated_output", manifest["raw_retention_policy"])

    def test_the_default_is_the_contracts_default(self) -> None:
        """"keep" unless someone says otherwise, so an omission never authorises a deletion."""
        from msdial_app.repository_reanalysis import (
            RepositoryFile,
            RepositoryProject,
            create_download_lease,
        )

        project = RepositoryProject(
            repository="test",
            accession="DEFAULT",
            eligible=True,
            selection_status="eligible",
            files=[RepositoryFile("sample.mzML", 8, "https://example.org/sample.mzML")],
            total_download_bytes=8,
        )
        with tempfile.TemporaryDirectory() as temporary:
            lease = create_download_lease(project, Path(temporary), 100, client=_FakeClient())
            manifest = json.loads(Path(lease["manifest_path"]).read_text(encoding="utf-8"))

        self.assertEqual("keep", manifest["raw_retention_policy"])


class AnUnreadablePolicyIsNormalisedAndReported(unittest.TestCase):
    """An unrecognised policy resolves towards keeping, and the caller is told.

    The download endpoint validates the policy, so the governed chain cannot emit a bad one. A
    caller that hand-writes answers["workflow_overrides"] reaches prepare_run unchecked, and the
    comparison downstream was a bare string equality with no casefolding and no strip -- so
    "delete", "Delete" and a trailing space were all keep-equivalent, and the run logged that the
    data were kept without ever saying the policy had not been understood. True, and no help to
    someone who believed they had asked for deletion.

    Found by an adversarial audit of the deletion path on 2026-09-20, which also caught that this
    project's own regression test pinned the meaningless literal "delete".
    """

    def test_the_only_value_that_is_not_keep_survives(self) -> None:
        from msdial_app.repository_reanalysis import normalize_raw_retention_policy

        policy, unrecognized = normalize_raw_retention_policy("delete_after_validated_output")

        self.assertEqual("delete_after_validated_output", policy)
        self.assertFalse(unrecognized)

    def test_a_near_miss_keeps_the_data_and_says_it_was_not_understood(self) -> None:
        from msdial_app.repository_reanalysis import normalize_raw_retention_policy

        for wrong in ("delete", "Delete", "DELETE_AFTER_VALIDATED_OUTPUT",
                      "delete_after_validated_outputs", "remove"):
            policy, unrecognized = normalize_raw_retention_policy(wrong)
            self.assertEqual("keep", policy, wrong)
            self.assertTrue(unrecognized, wrong)

    def test_surrounding_space_is_not_a_different_policy(self) -> None:
        from msdial_app.repository_reanalysis import normalize_raw_retention_policy

        policy, unrecognized = normalize_raw_retention_policy(" delete_after_validated_output ")

        self.assertEqual("delete_after_validated_output", policy)
        self.assertFalse(unrecognized, "a trailing space used to make this keep-equivalent")

    def test_saying_nothing_is_the_default_and_is_not_an_error(self) -> None:
        from msdial_app.repository_reanalysis import normalize_raw_retention_policy

        for empty in ("", None, "   "):
            policy, unrecognized = normalize_raw_retention_policy(empty)
            self.assertEqual("keep", policy)
            self.assertFalse(unrecognized, "an omission is the contract's default, not a mistake")

    def test_prepare_run_warns_rather_than_swallowing_the_request(self) -> None:
        """The warning reaches the caller before the run, not the log tail after it."""
        from msdial_app.agent_workflow import build_guided_plan
        from msdial_app.workflow import prepare_run

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sample.mzML").write_text("", encoding="ascii")
            console = root / "MSDIALCUI.exe"
            console.write_bytes(b"not really a console binary")
            lbm = root / "lab.lbm2"
            lbm.write_bytes(b"laboratory library")
            template = Path(__file__).resolve().parents[1] / "resources" / "msdial_console_param4lipidomics.txt"
            plan = build_guided_plan(
                str(root),
                {
                    "project_type": "lcms",
                    "ion_mode": "Negative",
                    "target_omics": "Lipidomics",
                    "parameter_strategy": "default",
                    "execute_rt_correction": False,
                    "library_strategy": "existing",
                    "libraries": {"lbm_path": str(lbm)},
                    "run_qa": False,
                    "generate_materials_methods": False,
                    "console_path": str(console),
                    "template_path": str(template),
                    "output_root": str(root / "out"),
                    "class_assignment_confirmed": True,
                    "workflow_overrides": {"repository_raw_retention_policy": "delete"},
                },
            )
            prepared = prepare_run(plan["workflow"])

        self.assertEqual("keep", prepared["repository_raw_retention_policy"])
        self.assertTrue(
            any("not recognised" in item["message"] for item in prepared["warnings"]),
            prepared["warnings"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

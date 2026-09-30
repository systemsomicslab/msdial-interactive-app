"""A download lease is recorded before its first byte, and a lease that fails says so and why.

The unit manifest used to be written once, after every object had arrived, been extracted and been
attributed. A lease that failed on the way - a checksum mismatch, an allow-list that attributed nothing,
a backend that stopped - left bytes under raw\\ and no record of whose they were. discard_download_lease,
the only way to release them, reads the manifest to find the raw directory it may remove, so it could
not act: at campaign scale that is disk filling with orphans nobody can release.

And the input lineage: one row per analysis input saying what it is, where its bytes came from and what
vouches for them, written where those facts are known, so the gate, the analysis-CSV builder and the
conversion step read one record rather than each re-deriving it from downloads and extracted_files.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from msdial_app import repository_reanalysis
from msdial_app.process_liveness import process_created_at
from msdial_app.repository_reanalysis import (
    LEASE_INCOMPLETE_STATUSES,
    RepositoryFile,
    RepositoryProject,
    _retained_result_paths,
    _write_json,
    create_download_lease,
    discard_download_lease,
    is_manifest_scratch_file,
    lease_owner_state,
    load_unit_manifest,
    read_manifest,
    update_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ("metabolights", "MTBLS-LEASE", "unit-a", "provenance", "run-manifest.json")


class _Client:
    """Stands in for the network. Serves fixed bytes per URL and can fail on a chosen URL."""

    def __init__(self, payloads: dict[str, bytes], fail_on: str = "", observe=None) -> None:
        self.payloads = payloads
        self.fail_on = fail_on
        self.observe = observe

    def download(self, url, destination, _maximum_bytes, progress_callback=None):
        if self.observe:
            self.observe(url, destination)
        data = self.payloads[url]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if url == self.fail_on:
            raise ValueError("Connection reset after the bytes had landed.")
        return {
            "path": str(destination),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "md5": hashlib.md5(data).hexdigest(),
            "resumed_from_bytes": 0,
        }


def _project(files: list[RepositoryFile], samples: list[str], unit: str = "unit-a") -> RepositoryProject:
    return RepositoryProject(
        repository="metabolights",
        accession="MTBLS-LEASE",
        analysis_unit_id=unit,
        eligible=True,
        selection_status="eligible",
        files=files,
        total_download_bytes=sum(item.size_bytes for item in files),
        sample_metadata=[{"sample_id": Path(name).stem, "raw_file": name} for name in samples],
    )


class TheLeaseIsRecordedBeforeItsFirstByte(unittest.TestCase):
    def test_the_manifest_says_downloading_while_the_objects_arrive(self) -> None:
        seen: list[dict] = []

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def observe(_url, _destination):
                manifest = root / "metabolights" / "MTBLS-LEASE" / "unit-a" / "provenance" / "run-manifest.json"
                seen.append(read_manifest(manifest))

            project = _project(
                [RepositoryFile("FILES/a.mzML", 3, "https://example.org/a.mzML")], ["a.mzML"]
            )
            lease = create_download_lease(
                project, root, 100, client=_Client({"https://example.org/a.mzML": b"abc"}, observe=observe)
            )
            final = read_manifest(lease["manifest_path"])

        self.assertEqual("downloading", seen[0]["status"])
        self.assertFalse(seen[0]["execution_allowed"])
        self.assertFalse(seen[0]["cleanup_allowed"])
        self.assertTrue(seen[0]["raw_directory"].endswith("raw"))
        self.assertEqual("prepared", final["status"])
        self.assertEqual(seen[0]["download_started_at"], final["download_started_at"])
        self.assertIn("download_completed_at", final)

    def test_a_lease_that_fails_after_bytes_land_is_recorded_and_can_be_discarded(self) -> None:
        """THE REGRESSION. Before this there was no manifest, and discard raised FileNotFoundError."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = _project(
                [
                    RepositoryFile("FILES/a.mzML", 3, "https://example.org/a.mzML"),
                    RepositoryFile("FILES/b.mzML", 3, "https://example.org/b.mzML"),
                ],
                ["a.mzML", "b.mzML"],
            )
            client = _Client(
                {"https://example.org/a.mzML": b"abc", "https://example.org/b.mzML": b"def"},
                fail_on="https://example.org/b.mzML",
            )
            with self.assertRaisesRegex(ValueError, "Connection reset"):
                create_download_lease(project, root, 100, client=client)

            manifest_path = root / "metabolights" / "MTBLS-LEASE" / "unit-a" / "provenance" / "run-manifest.json"
            manifest = read_manifest(manifest_path)
            raw = Path(manifest["raw_directory"])
            self.assertTrue(any(raw.rglob("*.mzML")), "the bytes did land")

            self.assertEqual("download_failed", manifest["status"])
            self.assertIn("Connection reset", manifest["download_failure"]["reason"])
            self.assertEqual("ValueError", manifest["download_failure"]["error_type"])
            self.assertEqual(1, manifest["download_failure"]["objects_completed"])
            self.assertEqual(2, manifest["download_failure"]["objects_declared"])
            self.assertEqual(1, len(manifest["downloads"]))
            self.assertFalse(manifest["execution_allowed"])

            discarded = discard_download_lease(manifest_path, confirmed=True)

            self.assertTrue(discarded["deleted"])
            self.assertFalse(raw.exists())
            self.assertEqual("discarded", read_manifest(manifest_path)["status"])

    def test_a_checksum_mismatch_is_recorded_with_its_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = _project(
                [RepositoryFile("FILES/a.mzML", 3, "https://example.org/a.mzML", checksum="0" * 32)],
                ["a.mzML"],
            )
            with self.assertRaisesRegex(ValueError, "MD5 checksum mismatch"):
                create_download_lease(project, root, 100, client=_Client({"https://example.org/a.mzML": b"abc"}))

            manifest = read_manifest(
                root / "metabolights" / "MTBLS-LEASE" / "unit-a" / "provenance" / "run-manifest.json"
            )

        self.assertEqual("download_failed", manifest["status"])
        self.assertIn("MD5 checksum mismatch", manifest["download_failure"]["reason"])

    def test_an_allow_list_that_attributes_nothing_is_recorded(self) -> None:
        """The shape every Workbench archive unit fails in today: the whole archive arrives, then the
        attribution refuses. The bytes are on disk and must stay releasable."""
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("study/unrelated.mzML", b"x")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = _project(
                [RepositoryFile("study.zip", 10, "https://example.org/study.zip", role="raw_archive")],
                ["sample_01.mzML"],
            )
            with self.assertRaisesRegex(ValueError, "did not contain an MS-DIAL input"):
                create_download_lease(
                    project, root, 10_000, client=_Client({"https://example.org/study.zip": archive.getvalue()})
                )
            manifest_path = root / "metabolights" / "MTBLS-LEASE" / "unit-a" / "provenance" / "run-manifest.json"

            self.assertEqual("download_failed", read_manifest(manifest_path)["status"])
            self.assertTrue(discard_download_lease(manifest_path, confirmed=True)["deleted"])

    def test_a_retried_lease_says_what_it_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = _project(
                [RepositoryFile("FILES/a.mzML", 3, "https://example.org/a.mzML")], ["a.mzML"]
            )
            failing = _Client({"https://example.org/a.mzML": b"abc"}, fail_on="https://example.org/a.mzML")
            with self.assertRaises(ValueError):
                create_download_lease(project, root, 100, client=failing)
            lease = create_download_lease(project, root, 100, client=_Client({"https://example.org/a.mzML": b"abc"}))
            provenance = Path(lease["manifest_path"]).parent
            copies = sorted(path.name for path in provenance.glob("run-manifest.superseded-*"))

        self.assertEqual("download_failed", lease["previous_manifest"]["status"])
        self.assertEqual(64, len(lease["previous_manifest"]["sha256"]))
        self.assertEqual("prepared", lease["status"])
        # A failed lease holds nothing the retry does not supersede, so it is summarised, not copied.
        self.assertNotIn("superseded_copy", lease["previous_manifest"])
        self.assertEqual([], copies)

    def test_the_download_job_path_refuses_an_unfinished_lease_by_manifest_too(self) -> None:
        """A download job had to be completed before anything used it; the manifest route keeps that."""
        for status in sorted(LEASE_INCOMPLETE_STATUSES):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                manifest = Path(temporary) / "run-manifest.json"
                manifest.write_text(
                    json.dumps({"status": status, "project": {}, "download_failure": {"reason": "reset"}}),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, status):
                    load_unit_manifest(manifest)

    def test_a_lease_still_recorded_as_downloading_cannot_be_discarded(self) -> None:
        """Its transfer may still be writing into the tree a discard would delete."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw"
            raw.mkdir()
            (raw / "part.mzML.part").write_bytes(b"x")
            manifest = root / "provenance" / "run-manifest.json"
            manifest.parent.mkdir()
            manifest.write_text(
                json.dumps(
                    {
                        "status": "downloading",
                        "workspace": str(root),
                        "raw_directory": str(raw),
                        "output_directory": str(root / "output"),
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "still downloading"):
                discard_download_lease(manifest, confirmed=True)
            self.assertTrue(raw.is_dir())


class EachInputHasOneLineageRow(unittest.TestCase):
    def _rows(self, lease: dict) -> dict[str, dict]:
        lineage = lease["input_lineage"]
        self.assertEqual("msdial-input-lineage.v1", lineage["schema"])
        return {Path(row["path"]).name: row for row in lineage["rows"]}

    def test_a_file_downloaded_on_its_own_carries_its_download_checksums(self) -> None:
        data = b"mzml bytes"
        md5 = hashlib.md5(data).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            project = _project(
                [RepositoryFile("FILES/a.mzML", len(data), "https://example.org/a.mzML", checksum=md5)],
                ["a.mzML"],
            )
            lease = create_download_lease(
                project, Path(temporary), 100, client=_Client({"https://example.org/a.mzML": data})
            )

        row = self._rows(lease)["a.mzML"]
        self.assertEqual("file", row["kind"])
        self.assertEqual("https://example.org/a.mzML", row["source"]["url"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), row["checksums"]["sha256"])
        self.assertEqual(md5, row["checksums"]["declared"])
        self.assertTrue(row["checksums"]["declared_verified"])
        self.assertEqual("md5", row["checksums"]["declared_algorithm"])
        self.assertEqual(["FILES/a.mzML"], row["declared_names"])
        self.assertEqual("a", row["sample_id"])
        self.assertEqual("", row["file_name"], "the CSV builder names the file, not the lease")
        self.assertEqual(1, len(lease["input_lineage"]["rows"]))

    def test_archive_members_and_archived_containers_name_the_archive(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("study/a.mzML", b"a")
            handle.writestr("study/X.d/AcqData/MSScan.bin", b"scan")
            handle.writestr("study/X.d/AcqData/Contents.xml", b"<xml/>")
        data = archive.getvalue()
        with tempfile.TemporaryDirectory() as temporary:
            project = _project(
                [RepositoryFile("study.zip", len(data), "https://example.org/study.zip", role="raw_archive")],
                ["a.mzML", "X.d"],
            )
            lease = create_download_lease(
                project, Path(temporary), 10_000, client=_Client({"https://example.org/study.zip": data})
            )

        rows = self._rows(lease)
        self.assertEqual({"a.mzML", "X.d"}, set(rows))
        member = rows["a.mzML"]
        self.assertEqual("extracted_member", member["kind"])
        self.assertEqual("https://example.org/study.zip", member["source"]["archive"]["url"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), member["source"]["archive"]["sha256"])
        self.assertEqual("study/a.mzML", member["source"]["member"])
        self.assertIsNone(member["source"]["archive"]["declared_checksum_verified"])
        container = rows["X.d"]
        self.assertEqual("archived_container", container["kind"])
        self.assertEqual(["https://example.org/study.zip"], [item["url"] for item in container["source"]["archives"]])
        self.assertEqual("X", container["sample_id"])

    def test_a_vendor_folder_assembled_from_objects_has_one_digest_over_its_members(self) -> None:
        payloads = {
            "https://example.org/S1.raw/_FUNC001.DAT": b"function one",
            "https://example.org/S1.raw/_extern.inf": b"extern",
        }
        with tempfile.TemporaryDirectory() as temporary:
            project = _project(
                [
                    RepositoryFile("FILES/S1.raw/_FUNC001.DAT", 12, "https://example.org/S1.raw/_FUNC001.DAT"),
                    RepositoryFile("FILES/S1.raw/_extern.inf", 6, "https://example.org/S1.raw/_extern.inf"),
                ],
                ["S1.raw"],
            )
            lease = create_download_lease(project, Path(temporary), 100, client=_Client(payloads))
            again = create_download_lease(project, Path(temporary), 100, client=_Client(payloads))
            payloads["https://example.org/S1.raw/_extern.inf"] = b"changed"
            changed = create_download_lease(project, Path(temporary), 100, client=_Client(payloads))

        row = self._rows(lease)["S1.raw"]
        self.assertEqual("vendor_folder", row["kind"])
        self.assertEqual({"objects": 2}, row["source"])
        self.assertEqual(2, row["checksums"]["member_objects"])
        self.assertEqual(64, len(row["checksums"]["members_sha256"]))
        self.assertEqual(row["checksums"], self._rows(again)["S1.raw"]["checksums"])
        self.assertNotEqual(
            row["checksums"]["members_sha256"], self._rows(changed)["S1.raw"]["checksums"]["members_sha256"]
        )

    def test_a_file_whose_declared_sha256_was_verified_says_so(self) -> None:
        """The download loop compares only a 32-digit md5. The allow-list check compares md5, sha1 and
        sha256 against the files themselves, and used to return only counts, so a verified input read as
        unverified in the lineage the gate reads."""
        data = b"mzml bytes"
        sha256 = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            project = _project(
                [RepositoryFile("FILES/a.mzML", len(data), "https://example.org/a.mzML", checksum=sha256)],
                ["a.mzML"],
            )
            lease = create_download_lease(
                project, Path(temporary), 100, client=_Client({"https://example.org/a.mzML": data})
            )

        row = self._rows(lease)["a.mzML"]
        self.assertEqual({"required": True, "verified": 1, "skipped": 0}, lease["allowlist_checksum_validation"])
        self.assertEqual(sha256, row["checksums"]["declared"])
        self.assertEqual("sha256", row["checksums"]["declared_algorithm"])
        self.assertIs(True, row["checksums"]["declared_verified"])

    def test_an_extracted_member_whose_declared_checksum_was_verified_says_so(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("study/a.mzML", b"a-bytes")
        data = archive.getvalue()
        md5 = hashlib.md5(b"a-bytes").hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            project = _project(
                [
                    RepositoryFile("study.zip", len(data), "https://example.org/study.zip", role="raw_archive"),
                    RepositoryFile("study/a.mzML", 7, "https://example.org/study.zip", checksum=md5),
                ],
                ["a.mzML"],
            )
            lease = create_download_lease(
                project, Path(temporary), 10_000, client=_Client({"https://example.org/study.zip": data})
            )

        row = self._rows(lease)["a.mzML"]
        self.assertEqual(1, lease["allowlist_checksum_validation"]["verified"])
        self.assertEqual("extracted_member", row["kind"])
        self.assertEqual(
            {"declared": md5, "declared_algorithm": "md5", "declared_verified": True}, row["checksums"]
        )
        # The archive itself published no checksum; the member's does not vouch for it.
        self.assertIsNone(row["source"]["archive"]["declared_checksum_verified"])


class ARetryNeverLosesTheRecordItReplaces(unittest.TestCase):
    """A lease writes "downloading" before its first byte. A retry over a finished unit that then failed,
    or was killed, left only that record where the unit's had been - its status, retained artifacts and
    their inventory, finalised run, failures and campaign crossings - with a five-field summary as the
    only trace that any of it existed."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project = _project([RepositoryFile("FILES/a.mzML", 3, "https://example.org/a.mzML")], ["a.mzML"])
        self.payloads = {"https://example.org/a.mzML": b"abc"}
        lease = create_download_lease(self.project, self.root, 100, client=_Client(self.payloads))
        self.manifest_path = Path(lease["manifest_path"])

        def finalise(manifest: dict) -> None:
            manifest.update(
                {
                    "status": "raw_cleaned",
                    "finalized_at": "2026-09-30T00:00:00+00:00",
                    "raw_cleaned_at": "2026-09-30T01:00:00+00:00",
                    "retained_artifacts": ["X:/synthetic/unit/output/result.mzTab"],
                    "retained_artifact_inventory": [{"path": "X:/synthetic/unit/output/result.mzTab"}],
                    "finalized_run": {"job_id": "run-1", "run_directory": "X:/synthetic/unit/output/run-1"},
                    "run_failures": [{"job_id": "run-0", "reason": "console exited 1"}],
                    "campaign_authorizations": [{"approval_id": "A1", "boundary": 5}],
                }
            )

        update_manifest(self.manifest_path, finalise)
        self.finalised = self.manifest_path.read_bytes()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _failing(self) -> _Client:
        return _Client(self.payloads, fail_on="https://example.org/a.mzML")

    def _assert_recoverable(self, reference: dict) -> Path:
        copy = Path(reference["path"])
        self.assertEqual(self.finalised, copy.read_bytes(), "copied byte for byte")
        self.assertEqual(hashlib.sha256(self.finalised).hexdigest(), reference["sha256"])
        recovered = read_manifest(copy)
        self.assertEqual("raw_cleaned", recovered["status"])
        self.assertEqual({"job_id": "run-1", "run_directory": "X:/synthetic/unit/output/run-1"}, recovered["finalized_run"])
        self.assertEqual(["X:/synthetic/unit/output/result.mzTab"], recovered["retained_artifacts"])
        self.assertEqual(1, len(recovered["retained_artifact_inventory"]))
        self.assertEqual("run-0", recovered["run_failures"][0]["job_id"])
        self.assertEqual([{"approval_id": "A1", "boundary": 5}], recovered["campaign_authorizations"])
        return copy

    def test_a_failed_retry_over_a_finalised_manifest_leaves_its_record_recoverable(self) -> None:
        """THE REGRESSION. The reviewer's reproduction: status download_failed, and finalized_run,
        retained_artifacts and campaign_authorizations gone, with no copy anywhere."""
        with self.assertRaisesRegex(ValueError, "Connection reset"):
            create_download_lease(self.project, self.root, 100, client=self._failing())

        after = read_manifest(self.manifest_path)
        self.assertEqual("download_failed", after["status"])
        self.assertEqual("raw_cleaned", after["previous_manifest"]["status"])
        copy = self._assert_recoverable(after["previous_manifest"]["superseded_copy"])
        self.assertEqual([str(copy)], [item["path"] for item in after["superseded_manifests"]])
        self.assertEqual(self.manifest_path.parent, copy.parent)
        self.assertTrue(copy.name.startswith("run-manifest.superseded-"), copy.name)

    def test_a_retry_killed_mid_transfer_has_already_put_the_record_aside(self) -> None:
        """A process killed during the retry writes nothing more; what is on disk at the first byte is
        what survives it."""
        seen: list[dict] = []

        def observe(_url, _destination):
            seen.append(read_manifest(self.manifest_path))

        create_download_lease(self.project, self.root, 100, client=_Client(self.payloads, observe=observe))

        self.assertEqual("downloading", seen[0]["status"])
        self._assert_recoverable(seen[0]["previous_manifest"]["superseded_copy"])

    def test_a_chain_of_retries_keeps_the_pointer_to_the_record_they_replaced(self) -> None:
        for _ in range(2):
            with self.assertRaises(ValueError):
                create_download_lease(self.project, self.root, 100, client=self._failing())
        lease = create_download_lease(self.project, self.root, 100, client=_Client(self.payloads))

        self.assertEqual("prepared", lease["status"])
        self.assertEqual("download_failed", lease["previous_manifest"]["status"])
        self.assertEqual(1, len(lease["superseded_manifests"]), "failed leases are summarised, not copied")
        self._assert_recoverable(lease["superseded_manifests"][0])
        self.assertEqual(1, len(list(self.manifest_path.parent.glob("run-manifest.superseded-*"))))

    def test_the_copy_is_a_provenance_record_that_finalisation_keeps(self) -> None:
        with self.assertRaises(ValueError):
            create_download_lease(self.project, self.root, 100, client=self._failing())
        copy = Path(read_manifest(self.manifest_path)["previous_manifest"]["superseded_copy"]["path"])

        _, records = _retained_result_paths(
            self.manifest_path.parent.parent / "output", self.manifest_path.parent, self.manifest_path.resolve()
        )

        self.assertFalse(is_manifest_scratch_file(copy))
        self.assertIn(copy.resolve(), records)

    def test_a_split_parent_is_not_re_leased(self) -> None:
        """Its parts read its raw files in place and name its manifest, which must stay split."""
        def split(manifest: dict) -> None:
            manifest["status"] = "split_by_acquisition"
            manifest["split_into"] = [{"analysis_unit_id": "unit-a__dda"}, {"analysis_unit_id": "unit-a__dia"}]

        update_manifest(self.manifest_path, split)
        before = self.manifest_path.read_bytes()
        calls: list[str] = []

        with self.assertRaisesRegex(ValueError, "split by acquisition mode into 2 part"):
            create_download_lease(
                self.project, self.root, 100, client=_Client(self.payloads, observe=lambda url, _: calls.append(url))
            )

        self.assertEqual(before, self.manifest_path.read_bytes())
        self.assertEqual([], calls, "nothing was downloaded")
        self.assertEqual([], list(self.manifest_path.parent.glob("run-manifest.superseded-*")))


_CHILD_LEASE = r'''
import os, sys, time
sys.path.insert(0, ROOT)
from pathlib import Path
from msdial_app.repository_reanalysis import RepositoryFile, RepositoryProject, create_download_lease

class Client:
    def download(self, url, destination, _maximum, progress_callback=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"half of the bytes")
        print("downloading", flush=True)
        if MODE == "die":
            os._exit(1)  # a reboot, or a backend killed mid-transfer
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            time.sleep(0.05)
        os._exit(2)

project = RepositoryProject(
    repository="metabolights", accession="MTBLS-LEASE", analysis_unit_id="unit-a", eligible=True,
    selection_status="eligible", files=[RepositoryFile("FILES/a.mzML", 3, "https://example.org/a.mzML")],
    total_download_bytes=3, sample_metadata=[{"sample_id": "a", "raw_file": "a.mzML"}],
)
create_download_lease(project, Path(WORK), 100, client=Client(), job_id="job-owner")
'''


class ALeaseKnowsItsOwner(unittest.TestCase):
    """A lease killed together with its process writes nothing and stays "downloading" for good. The
    manifest recorded no owner, so neither discard nor the runner could tell it from a live one, and
    the only way out was to download the unit again."""

    def _child(self, work: str, mode: str) -> subprocess.Popen:
        code = f"ROOT={str(ROOT)!r}\nWORK={work!r}\nMODE={mode!r}\n" + _CHILD_LEASE
        return subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_a_lease_killed_with_its_process_can_be_discarded(self) -> None:
        """THE REGRESSION. discard refused "still downloading", and the bytes stayed on disk."""
        with tempfile.TemporaryDirectory() as temporary:
            child = self._child(temporary, "die")
            child.communicate(timeout=120)
            manifest_path = Path(temporary).joinpath(*MANIFEST)
            manifest = read_manifest(manifest_path)
            raw = Path(manifest["raw_directory"])

            self.assertEqual(1, child.returncode)
            self.assertEqual("downloading", manifest["status"])
            owner = manifest["lease_owner"]
            self.assertEqual(child.pid, owner["pid"])
            self.assertEqual("job-owner", owner["job_id"])
            self.assertEqual(socket.gethostname(), owner["host"])
            self.assertIsInstance(owner["process_created_at"], float)
            self.assertTrue(manifest["download_progress_at"])
            self.assertTrue(any(path.is_file() for path in raw.rglob("*")), "the bytes did land")
            self.assertEqual("gone", lease_owner_state(manifest)["state"])

            discarded = discard_download_lease(manifest_path, confirmed=True)
            after = read_manifest(manifest_path)

            self.assertTrue(discarded["deleted"])
            self.assertTrue(discarded["stale_lease_discarded"])
            self.assertFalse(raw.exists())
            self.assertEqual("discarded", after["status"])
            record = after["stale_lease_discarded"]
            self.assertEqual(owner, record["lease_owner"])
            self.assertIn(f"Process {child.pid}", record["evidence"])
            self.assertEqual(manifest["download_progress_at"], record["last_heartbeat_at"])

    def test_a_lease_in_a_live_process_is_neither_discarded_nor_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            child = self._child(temporary, "wait")
            try:
                self.assertEqual("downloading", child.stdout.readline().strip())
                manifest_path = Path(temporary).joinpath(*MANIFEST)
                before = manifest_path.read_bytes()
                raw = Path(read_manifest(manifest_path)["raw_directory"])

                self.assertEqual("alive", lease_owner_state(read_manifest(manifest_path))["state"])
                with self.assertRaisesRegex(ValueError, "still downloading"):
                    discard_download_lease(manifest_path, confirmed=True)
                with self.assertRaisesRegex(ValueError, "job job-owner.*still downloading into this workspace"):
                    create_download_lease(
                        _project([RepositoryFile("FILES/a.mzML", 3, "https://example.org/a.mzML")], ["a.mzML"]),
                        Path(temporary),
                        100,
                        client=_Client({"https://example.org/a.mzML": b"abc"}),
                    )
                self.assertEqual(before, manifest_path.read_bytes())
                self.assertTrue(raw.is_dir())
            finally:
                child.kill()
                child.communicate(timeout=60)

            self.assertEqual("gone", lease_owner_state(read_manifest(manifest_path))["state"])
            self.assertTrue(discard_download_lease(manifest_path, confirmed=True)["deleted"])

    def test_a_lease_this_process_is_running_is_alive(self) -> None:
        states: list[str] = []
        refusals: list[str] = []

        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary).joinpath(*MANIFEST)

            def observe(_url, _destination):
                states.append(lease_owner_state(read_manifest(manifest_path))["state"])
                try:
                    discard_download_lease(manifest_path, confirmed=True)
                except ValueError as error:
                    refusals.append(str(error))

            project = _project([RepositoryFile("FILES/a.mzML", 3, "https://example.org/a.mzML")], ["a.mzML"])
            lease = create_download_lease(
                project, Path(temporary), 100, client=_Client({"https://example.org/a.mzML": b"abc"}, observe=observe),
                job_id="job-here",
            )

        self.assertEqual(["alive"], states)
        self.assertEqual(1, len(refusals))
        self.assertIn("still downloading", refusals[0])
        self.assertEqual("prepared", lease["status"])
        self.assertEqual("job-here", lease["lease_owner"]["job_id"], "the final record keeps who downloaded it")

    def _downloading_manifest(self, root: Path, owner: dict) -> Path:
        raw = root / "raw"
        (raw / "data").mkdir(parents=True)
        (raw / "data" / "a.mzML.part").write_bytes(b"x")
        manifest = root / "provenance" / "run-manifest.json"
        _write_json(
            manifest,
            {
                "status": "downloading",
                "workspace": str(root),
                "raw_directory": str(raw),
                "output_directory": str(root / "output"),
                "lease_owner": owner,
            },
        )
        return manifest

    def test_a_lease_naming_this_process_that_it_is_not_running_is_gone(self) -> None:
        """It ended without recording how, or an earlier process had the same id; either way it is not
        running, and this process is the one that would be running it."""
        owner = {
            "lease_id": "not-held-by-anyone",
            "pid": os.getpid(),
            "process_created_at": process_created_at(),
            "host": socket.gethostname(),
        }
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._downloading_manifest(Path(temporary), owner)

            self.assertEqual("gone", lease_owner_state(read_manifest(manifest))["state"])
            self.assertTrue(discard_download_lease(manifest, confirmed=True)["deleted"])

    def _sleeper(self) -> subprocess.Popen:
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait(timeout=60)))
        return sleeper

    def test_a_lease_whose_process_id_now_names_a_later_process_is_gone(self) -> None:
        """Windows reuses process ids. A running process with the lease's id but a later creation time is
        a different process."""
        sleeper = self._sleeper()
        owner = {
            "lease_id": "earlier",
            "pid": sleeper.pid,
            "process_created_at": 1.0,  # 1970: the id has been reused since
            "host": socket.gethostname(),
        }
        state = lease_owner_state({"lease_owner": owner})
        alive = lease_owner_state(
            {"lease_owner": {**owner, "process_created_at": process_created_at(sleeper.pid)}}
        )

        self.assertEqual("gone", state["state"])
        self.assertIn("started later", state["reason"])
        self.assertEqual("alive", alive["state"])

    def test_a_lease_taken_on_another_host_or_with_no_owner_is_not_discarded(self) -> None:
        for owner in (
            {"lease_id": "x", "pid": 4, "process_created_at": 1.0, "host": "another-host.invalid"},
            None,
        ):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as temporary:
                manifest = self._downloading_manifest(Path(temporary), owner)

                self.assertEqual("unknown", lease_owner_state(read_manifest(manifest))["state"])
                preview = discard_download_lease(manifest, confirmed=False)
                self.assertEqual("unknown", preview["lease_owner_state"]["state"])
                with self.assertRaisesRegex(ValueError, "not provably gone"):
                    discard_download_lease(manifest, confirmed=True)
                self.assertTrue((Path(temporary) / "raw").is_dir())

    def test_liveness_is_never_read_by_signalling(self) -> None:
        """On Windows os.kill(pid, 0) sends CTRL_C_EVENT; it is not a probe."""
        sleeper = self._sleeper()
        with patch("os.kill", side_effect=AssertionError("os.kill is not a liveness probe")):
            for pid in (sleeper.pid, 999_999_999):
                lease_owner_state(
                    {"lease_owner": {"lease_id": "x", "pid": pid, "process_created_at": 1.0, "host": socket.gethostname()}}
                )

    def test_the_heartbeat_moves_while_bytes_arrive(self) -> None:
        beats: list[tuple[str, int]] = []

        class _Progressing(_Client):
            def download(self, url, destination, maximum_bytes, progress_callback=None):
                for received in (1, 2, 3):
                    time.sleep(0.01)
                    progress_callback(received, 3)
                    current = read_manifest(destination.parents[2] / "provenance" / "run-manifest.json")
                    beats.append((current["download_progress_at"], current.get("download_received_bytes")))
                return super().download(url, destination, maximum_bytes, progress_callback)

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            repository_reanalysis, "LEASE_HEARTBEAT_SECONDS", 0.0
        ):
            project = _project([RepositoryFile("FILES/a.mzML", 3, "https://example.org/a.mzML")], ["a.mzML"])
            lease = create_download_lease(
                project, Path(temporary), 100, client=_Progressing({"https://example.org/a.mzML": b"abc"})
            )

        self.assertEqual([1, 2, 3], [received for _, received in beats])
        stamps = [stamp for stamp, _ in beats]
        self.assertEqual(sorted(stamps), stamps)
        self.assertLess(lease["download_started_at"], stamps[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

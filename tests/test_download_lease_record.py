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
import tempfile
import unittest
import zipfile
from pathlib import Path

from msdial_app.repository_reanalysis import (
    LEASE_INCOMPLETE_STATUSES,
    RepositoryFile,
    RepositoryProject,
    create_download_lease,
    discard_download_lease,
    load_unit_manifest,
    read_manifest,
)


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

        self.assertEqual("download_failed", lease["previous_manifest"]["status"])
        self.assertEqual(64, len(lease["previous_manifest"]["sha256"]))
        self.assertEqual("prepared", lease["status"])

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""A file a raw-data reader writes into an input container is recorded, not taken for a member.

Bruker's baf2sql writes analysis.sqlite into a BAF .d that arrived without one, during the raw-header
preflight and very likely during the MS-DIAL run. That file is reader_created: not an analysis input,
not a member of the container, and not a checksum failure. msdial_app.reader_created tells it from the
files the container arrived with; the input lineage and the unit manifest record it with its size and
sha256; a container member check sets it aside (container_members).

The .d folders here are synthetic: an analysis.baf of a few bytes stands for Bruker's data.
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from msdial_app.reader_created import (
    container_members,
    reader_created_files,
    reader_created_names,
    reader_created_reader,
)
from msdial_app.repository_reanalysis import (
    RepositoryFile,
    create_download_lease,
    read_manifest,
    reader_created_block,
    record_reader_created_files,
)
from msdial_app.run_finalisation import finalise_console_run

from test_download_lease_record import _Client, _project


SQLITE = b"SQLite format 3\x00 written by baf2sql"


def _baf(folder: Path, *, sqlite: bool = False) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "analysis.baf").write_bytes(b"baf")
    (folder / "analysis.baf_idx").write_bytes(b"idx")
    (folder / "Analysis.method").mkdir(exist_ok=True)
    (folder / "Analysis.method" / "submethods.xml").write_text("<method/>", encoding="ascii")
    if sqlite:
        (folder / "analysis.sqlite").write_bytes(SQLITE)
    return folder


class TheHelper(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    def test_a_baf_container_names_what_baf2sql_writes(self) -> None:
        container = _baf(self.root / "S1.d")

        self.assertEqual("bruker_baf2sql", reader_created_names(container)["analysis.sqlite"])
        self.assertEqual([], reader_created_files(container), "nothing written yet")

    def test_the_file_baf2sql_wrote_is_reported_with_its_size_and_sha256(self) -> None:
        container = _baf(self.root / "S1.d")
        (container / "analysis.sqlite").write_bytes(SQLITE)
        (container / "analysis.sqlite-journal").write_bytes(b"journal")

        found = reader_created_files(container, members=["analysis.baf", "analysis.baf_idx"])

        self.assertEqual(
            [
                {"path": "analysis.sqlite", "size": len(SQLITE), "sha256": hashlib.sha256(SQLITE).hexdigest(),
                 "reader": "bruker_baf2sql"},
                {"path": "analysis.sqlite-journal", "size": 7, "sha256": hashlib.sha256(b"journal").hexdigest(),
                 "reader": "bruker_baf2sql"},
            ],
            found,
        )

    def test_a_file_the_container_arrived_with_is_its_own_whatever_its_name(self) -> None:
        container = _baf(self.root / "S1.d", sqlite=True)

        self.assertEqual([], reader_created_files(container, members=["Analysis.SQLite", "analysis.baf"]))
        self.assertEqual("", reader_created_reader(container, "analysis.sqlite", ["analysis.sqlite"]))
        self.assertEqual("bruker_baf2sql", reader_created_reader(container, "analysis.sqlite"))

    def test_no_rule_applies_to_other_containers(self) -> None:
        agilent = self.root / "S2.d"
        (agilent / "AcqData").mkdir(parents=True)
        (agilent / "analysis.sqlite").write_bytes(SQLITE)
        waters = self.root / "S3.raw"
        waters.mkdir()
        (waters / "analysis.sqlite").write_bytes(SQLITE)

        for container in (agilent, waters):
            with self.subTest(container=container.name):
                self.assertEqual({}, reader_created_names(container))
                self.assertEqual([], reader_created_files(container))

    def test_a_member_check_sets_the_reader_files_aside(self) -> None:
        container = _baf(self.root / "S1.d", sqlite=True)
        found = ["analysis.baf", "analysis.baf_idx", "Analysis.method/submethods.xml", "analysis.sqlite"]

        own, created = container_members(container, found, members=found[:3])
        shipped, none = container_members(container, found, members=found)

        self.assertEqual((found[:3], ["analysis.sqlite"]), (own, created))
        self.assertEqual((found, []), (shipped, none))


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        for name, data in entries.items():
            handle.writestr(name, data)
    return buffer.getvalue()


BAF_MEMBERS = {"S1.d/analysis.baf": b"baf", "S1.d/analysis.baf_idx": b"idx",
               "S1.d/Analysis.method/submethods.xml": b"<method/>"}


class TheLeaseRecordsThem(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    def lease(self, entries: dict[str, bytes]) -> dict:
        data = _zip(entries)
        project = _project([RepositoryFile("study.zip", len(data), "https://example.org/study.zip", role="raw_archive")],
                           ["S1.d"])
        return create_download_lease(project, self.root, 10_000_000, client=_Client({"https://example.org/study.zip": data}))

    @staticmethod
    def row(lease: dict) -> dict:
        [row] = lease["input_lineage"]["rows"]
        return row

    def test_a_first_lease_records_that_nothing_was_written_yet(self) -> None:
        lease = self.lease(BAF_MEMBERS)

        row = self.row(lease)
        self.assertEqual(("archived_container", []), (row["kind"], row["reader_created_files"]))
        self.assertNotIn("reader_named_members", row)
        self.assertIsNone(reader_created_block(lease, "run"))

    def test_a_retry_after_a_preflight_records_the_file_and_is_no_failure(self) -> None:
        first = self.lease(BAF_MEMBERS)
        container = Path(first["input_candidates"][0])
        (container / "analysis.sqlite").write_bytes(SQLITE)   # what the preflight's baf2sql left

        again = self.lease(BAF_MEMBERS)

        self.assertEqual("prepared", again["status"])
        self.assertEqual([str(container)], again["input_candidates"], "not an input")
        row = self.row(again)
        self.assertEqual([("analysis.sqlite", len(SQLITE), hashlib.sha256(SQLITE).hexdigest())],
                         [(item["path"], item["size"], item["sha256"]) for item in row["reader_created_files"]])

    def test_a_container_published_with_its_sqlite_keeps_it_as_a_member(self) -> None:
        lease = self.lease({**BAF_MEMBERS, "S1.d/analysis.sqlite": SQLITE})

        row = self.row(lease)
        self.assertEqual([], row["reader_created_files"])
        self.assertEqual(["analysis.sqlite"], row["reader_named_members"])
        self.assertIsNone(reader_created_block(lease, "run"), "later runs do not report it either")

    def test_a_container_no_rule_applies_to_records_nothing_new(self) -> None:
        lease = self.lease({"S1.d/AcqData/MSScan.bin": b"scan", "S1.d/AcqData/Contents.xml": b"<xml/>"})

        self.assertNotIn("reader_created_files", self.row(lease))

    def test_after_the_run_the_unit_manifest_lists_them_per_container(self) -> None:
        lease = self.lease(BAF_MEMBERS)
        container = Path(lease["input_candidates"][0])
        (container / "analysis.sqlite").write_bytes(SQLITE)   # what the run's baf2sql left

        record = finalise_console_run(
            "run1", {"repository_run_manifest": lease["manifest_path"], "run_directory": lease["output_directory"]},
            {}, 0, {}, lambda _line: None,
        )
        written = read_manifest(lease["manifest_path"])["reader_created_files"]

        self.assertEqual(1, record["reader_created_files"])
        self.assertEqual(("msdial-reader-created-files.v1", "run"), (written["schema"], written["stage"]))
        [entry] = written["containers"]
        self.assertEqual(str(container), entry["path"])
        self.assertEqual(["analysis.sqlite"], [item["path"] for item in entry["files"]])
        self.assertNotIn("members_known", entry)
        self.assertTrue((container / "analysis.sqlite").is_file(), "recorded, not moved")

    def test_a_preflight_can_record_them_the_same_way(self) -> None:
        lease = self.lease(BAF_MEMBERS)
        (Path(lease["input_candidates"][0]) / "analysis.sqlite").write_bytes(SQLITE)

        block = record_reader_created_files(lease["manifest_path"], "preflight")

        self.assertEqual(block, read_manifest(lease["manifest_path"])["reader_created_files"])
        self.assertEqual("preflight", block["stage"])

    def test_a_lineage_written_before_the_rule_reports_with_membership_unknown(self) -> None:
        lease = self.lease(BAF_MEMBERS)
        manifest = read_manifest(lease["manifest_path"])
        for row in manifest["input_lineage"]["rows"]:
            row.pop("reader_created_files", None)
        (Path(lease["input_candidates"][0]) / "analysis.sqlite").write_bytes(SQLITE)

        [entry] = reader_created_block(manifest, "run")["containers"]

        self.assertFalse(entry["members_known"])
        self.assertEqual(["analysis.sqlite"], [item["path"] for item in entry["files"]])


if __name__ == "__main__":
    unittest.main()

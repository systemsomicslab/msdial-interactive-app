"""One stored copy of MS-DIAL's loaded-library copy per content, beside the units, and nothing deleted unproven.

Finalisation of a campaign unit deleted <project>_Loaded.msp2.dbs - with the private VS21 pair a 0.28 or 0.86 GB
copy of the private library - and its project could then not be opened again. The store keeps one copy per
content: MS-DIAL writes the .dbs as a new zip on every save, so two copies of the same libraries differ in their
DOS timestamps and nowhere else. A copy is removed only once the store holds one proven equal.

Every synthetic .dbs here is laid out as MS-DIAL writes one: a Storage entry and one DataBase entry per loaded
library, named with backslashes (Path.Combine). zipfile.ZipInfo turns os.sep into '/', so the name is set after
the ZipInfo is made.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import random
import re
import tempfile
import threading
import unittest
import zipfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from msdial_app import library_copy_store
from msdial_app.download_store import StoreLock, StoreLockTimeout
from msdial_app.library_copy_store import (
    DEDUPLICATED,
    DEDUPLICATED_BY_CONTENT,
    OBJECT_FILE,
    PENDING,
    QUARANTINED,
    STORE_DIRECTORY_NAME,
    STORED,
    LibraryCopyPending,
    LibraryCopyStore,
    LibraryCopyStoreError,
    annotation_settings,
    attach,
    dbs_structure,
    describe_error,
    library_copy_store_for,
    read_copy_identity,
    settle_console_temp,
    workspace_root_for,
)
from msdial_app.repository_reanalysis import RESERVED_WORKSPACE_NAMES, _write_json, read_manifest, update_manifest
from msdial_app.run_finalisation import store_loaded_library_copies

WINDOWS = os.name == "nt"
DATABASES = {
    "MetabolomicsDB\\MspDB_1_Synthetic-Private-pos\\DataBase": b"synthetic msp records " * 400,
    "MetabolomicsDB\\LbmDB\\DataBase": b"synthetic lbm records " * 300,
}
STORAGE = b"\x92\xa7Storage synthetic annotator settings"
LIBRARIES = [
    {"name": "Synthetic-Private-pos.msp", "filename": "Synthetic-Private-pos.msp", "sha256": "ef" * 32,
     "private": True, "bytes": 99},
    {"name": "Synthetic.lbm2", "filename": "Synthetic.lbm2", "sha256": "ab" * 32, "private": False, "bytes": 9},
]
UNIT = {"repository": "metabolights", "accession": "MTBLS-SYN", "unit_id": "unit-syn",
        "manifest": "metabolights/MTBLS-SYN/unit-syn/provenance/run-manifest.json"}


def synthetic_dbs(
    path: Path,
    *,
    date_time: tuple[int, ...] = (2026, 10, 1, 22, 12, 58),
    storage: bytes = STORAGE,
    method: int = zipfile.ZIP_DEFLATED,
    databases: dict[str, bytes] | None = None,
) -> Path:
    """A .dbs laid out as MS-DIAL writes one, entry names with backslashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in [*(databases or DATABASES).items(), ("Storage", storage)]:
            info = zipfile.ZipInfo("entry", date_time=date_time)
            info.filename = name  # after construction: ZipInfo would turn os.sep into '/'
            info.compress_type = method
            archive.writestr(info, data)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _stop_after_install():
    """An install whose record cannot be written and whose copy cannot be taken back out of the store."""
    write, replace, back = library_copy_store._write_json_atomic, library_copy_store._replace, library_copy_store.rename_back

    def refuse_entry(path, value):
        if Path(path).name == "entry.json":
            raise PermissionError(13, "Access is denied", str(path))
        return write(path, value)

    def holds(move):
        def held(source, target, *args, **kwargs):
            if Path(source).name == OBJECT_FILE and Path(source).parent.parent.name == "o":
                raise PermissionError(13, "held by a scanner", str(source))
            return move(source, target, *args, **kwargs)
        return held

    with patch.object(library_copy_store, "_write_json_atomic", refuse_entry), \
            patch.object(library_copy_store, "_replace", holds(replace)), \
            patch.object(library_copy_store, "rename_back", holds(back)):
        yield


def entry_data_offset(path: Path, index: int = 0) -> tuple[int, int]:
    """(where entry ``index``'s compressed data starts in the file, how long it is): to damage the data itself."""
    with zipfile.ZipFile(path) as archive:
        info = archive.infolist()[index]
    data = path.read_bytes()
    name_length, extra_length = int.from_bytes(data[info.header_offset + 26:info.header_offset + 28], "little"), \
        int.from_bytes(data[info.header_offset + 28:info.header_offset + 30], "little")
    return info.header_offset + 30 + name_length + extra_length, info.compress_size


def flip(path: Path, offset: int, mask: int = 0xFF) -> None:
    data = bytearray(path.read_bytes())
    data[offset] ^= mask
    path.write_bytes(bytes(data))


def assert_names_no_root(case: unittest.TestCase, root: Path, text: str) -> None:
    """No absolute path of ``root`` in ``text``: as written, as JSON escapes it, and no drive-rooted path at all."""
    case.assertNotIn(str(root), text)
    case.assertNotIn(str(root).replace("\\", "\\\\"), text)
    case.assertIsNone(re.search(r"[A-Za-z]:(?:\\\\|\\|/)", text), text)


class _Store(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.output = self.root / "metabolights" / "MTBLS-SYN" / "unit-syn" / "output"
        self.output.mkdir(parents=True)
        self.store = LibraryCopyStore(self.root)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def copy(self, name: str = "Project-1_Loaded.msp2.dbs", **options) -> Path:
        return synthetic_dbs(self.output / name, **options)

    def put(self, path: Path, job_id: str = "job1", **options) -> dict:
        return self.store.put(path, read_copy_identity(path), unit=UNIT, job_id=job_id, libraries=LIBRARIES,
                              **options)


class IdentityTests(_Store):
    def test_one_content_saved_twice_has_one_key_and_one_normalised_digest(self) -> None:
        first = self.copy("a.dbs", date_time=(2026, 10, 1, 22, 12, 58))
        second = self.copy("b.dbs", date_time=(2026, 10, 3, 9, 0, 4))
        one, two = read_copy_identity(first), read_copy_identity(second)

        self.assertTrue(one.recognised)
        self.assertEqual(one.content_key, two.content_key)
        self.assertEqual(one.normalized_sha256, two.normalized_sha256)
        self.assertNotEqual(one.sha256, two.sha256)
        self.assertEqual(_sha(first), one.sha256, "the sha256 is the file's own, as finalisation recorded it")
        self.assertEqual(32, len(one.object_id))
        self.assertTrue(one.content_key.startswith(one.object_id))
        # The timestamps that tell them apart are kept, so either file could be rebuilt from the other.
        self.assertNotEqual(one.dos_times, two.dos_times)
        self.assertEqual({"MetabolomicsDB/MspDB_1_Synthetic-Private-pos/DataBase", "MetabolomicsDB/LbmDB/DataBase",
                          "Storage"}, {item["entry"] for item in one.dos_times})
        self.assertEqual(hashlib.sha256(STORAGE).hexdigest(), one.storage_sha256)

    def test_other_settings_or_bytes_that_are_no_copy_are_other_identities(self) -> None:
        one = read_copy_identity(self.copy("a.dbs"))
        other = read_copy_identity(self.copy("b.dbs", storage=STORAGE + b"!"))
        self.assertNotEqual(one.content_key, other.content_key)

        loose = self.output / "c.dbs"
        loose.write_bytes(b"not a zip")
        identity = read_copy_identity(loose)
        self.assertFalse(identity.recognised)
        self.assertEqual(("file_sha256", _sha(loose), _sha(loose)),
                         (identity.content_key_kind, identity.content_key, identity.normalized_sha256))

        laid_out_otherwise = self.output / "d.dbs"
        with zipfile.ZipFile(laid_out_otherwise, "w") as archive:
            archive.writestr("Storage", b"x")
            archive.writestr("Other/DataBase", b"y")
        self.assertFalse(read_copy_identity(laid_out_otherwise).recognised)

    def test_the_layout_is_one_storage_and_msdial_databases(self) -> None:
        self.assertTrue(dbs_structure(["Storage", "MetabolomicsDB\\LbmDB\\DataBase"]))
        self.assertTrue(dbs_structure(["Storage", "ProteomicsDB/x", "EadLipidomicsDB/y/DataBase"]))
        self.assertFalse(dbs_structure(["Storage", "Storage", "MetabolomicsDB/x/DataBase"]))
        self.assertFalse(dbs_structure(["Storage"]))
        self.assertFalse(dbs_structure(["MetabolomicsDB/x/DataBase"]))
        self.assertFalse(dbs_structure(["Storage", "MetabolomicsDB/x/DataBase", "readme.txt"]))


class PutTests(_Store):
    def test_a_new_content_is_moved_in_tested_and_claimed(self) -> None:
        copy = self.copy()
        inode = os.stat(copy).st_ino
        record = self.put(copy)
        object_path = self.store.object_path(record["object_id"])

        self.assertEqual((STORED, "testzip", "rename"), (record["outcome"], record["verified_by"], record["stored_by"]))
        self.assertFalse(copy.exists())
        self.assertEqual(inode, os.stat(object_path).st_ino, "renamed on one volume, not copied")
        entry = json.loads(self.store.entry_path(record["object_id"]).read_text(encoding="utf-8"))
        self.assertEqual("passed", entry["testzip"]["result"])
        self.assertEqual([{"name": item["name"], "sha256": item["sha256"], "private": item["private"]}
                          for item in LIBRARIES], entry["libraries"])
        self.assertEqual("local_only", entry["sharing"])
        self.assertEqual("local_only", json.loads((self.root / STORE_DIRECTORY_NAME / "STORE.json")
                                                  .read_text(encoding="utf-8"))["sharing"])
        [claim] = self.store.claims(record["object_id"])
        self.assertEqual(("metabolights/MTBLS-SYN/unit-syn", "job1", STORED),
                         (claim["unit"], claim["job_id"], claim["outcome"]))
        self.assertEqual(record["claim"], next(self.store.claims_directory(record["object_id"]).iterdir()).name)
        self.assertEqual([], list((self.root / STORE_DIRECTORY_NAME / "incoming").iterdir()))
        for text in (json.dumps(entry), json.dumps(claim), json.dumps(record)):
            self.assertNotIn(str(self.root), text)
            self.assertIsNone(re.search(r"[A-Za-z]:[\\/]", text), text)

    def test_an_equal_copy_is_removed_against_the_object_re_read_and_claimed_again(self) -> None:
        first = self.put(self.copy("Project-1_Loaded.msp2.dbs", date_time=(2026, 10, 1, 10, 0, 0)))
        object_path = self.store.object_path(first["object_id"])
        before = os.stat(object_path)
        second_copy = self.copy("Project-2_Loaded.msp2.dbs", date_time=(2026, 10, 2, 11, 30, 2))
        second = self.put(second_copy, job_id="job2")

        self.assertEqual((DEDUPLICATED, "normalized_sha256"), (second["outcome"], second["verified_by"]))
        self.assertEqual(first["object_id"], second["object_id"])
        self.assertFalse(second_copy.exists())
        after = os.stat(object_path)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(2, len(self.store.claims(first["object_id"])))

    def test_an_object_damaged_inside_is_never_taken_as_proof(self) -> None:
        first = self.put(self.copy(date_time=(2026, 10, 1, 10, 0, 0)))
        object_path = self.store.object_path(first["object_id"])
        start, length = entry_data_offset(object_path)
        flip(object_path, start + length // 2)  # inside the first entry's data: size and entry table unchanged
        copy = self.copy(date_time=(2026, 10, 2, 10, 0, 0))

        record = self.put(copy, job_id="job2")

        # entry.json alone would have called the copy equal and deleted it; the object re-read says otherwise.
        self.assertEqual((STORED, True), (record["outcome"], record.get("repaired_object")))
        self.assertFalse(copy.exists())
        self.assertIsNone(self.store.problem(first["object_id"], full=True))
        self.assertEqual(1, len(list(object_path.parent.glob("damaged-*.dbs"))))

    def test_the_same_entries_compressed_otherwise_are_one_content(self) -> None:
        first = self.put(self.copy(method=zipfile.ZIP_STORED))
        copy = self.copy("Project-2_Loaded.msp2.dbs", method=zipfile.ZIP_DEFLATED)
        record = self.put(copy, job_id="job2")

        self.assertEqual((DEDUPLICATED_BY_CONTENT, "decompressed_content"),
                         (record["outcome"], record["verified_by"]))
        self.assertEqual(first["object_id"], record["object_id"])
        self.assertFalse(copy.exists())

    def test_a_copy_with_an_objects_key_and_other_content_is_quarantined(self) -> None:
        first = self.put(self.copy(method=zipfile.ZIP_STORED))
        copy = self.copy("Project-2_Loaded.msp2.dbs", method=zipfile.ZIP_DEFLATED)
        with patch.object(library_copy_store, "_compare_inflated", return_value="different"):
            record = self.put(copy, job_id="job2")

        self.assertEqual(QUARANTINED, record["outcome"])
        self.assertFalse(copy.exists())
        [folder] = (self.root / STORE_DIRECTORY_NAME / "quarantine").iterdir()
        reason = json.loads((folder / "reason.json").read_text(encoding="utf-8"))
        self.assertEqual(first["object_id"], reason["object_id"])
        self.assertTrue((folder / "library-copy.dbs").is_file())
        self.assertEqual(f"{STORE_DIRECTORY_NAME}/quarantine/{folder.name}", record["quarantine"])

    def test_a_truncated_object_is_replaced_and_kept_beside(self) -> None:
        first = self.put(self.copy())
        object_path = self.store.object_path(first["object_id"])
        object_path.write_bytes(object_path.read_bytes()[:100])
        record = self.put(self.copy("Project-2_Loaded.msp2.dbs"), job_id="job2")

        self.assertEqual((STORED, True), (record["outcome"], record["repaired_object"]))
        self.assertIn("bytes", record["repaired_problem"])
        self.assertIsNone(self.store.problem(first["object_id"], full=True))
        [kept] = object_path.parent.glob("damaged-*.dbs")
        self.assertEqual(100, kept.stat().st_size)
        self.assertEqual(1, len(self.store.entry(first["object_id"])["repairs"]))

    def test_a_file_that_is_no_library_copy_is_kept_by_its_sha256_and_never_guessed_equal(self) -> None:
        loose = self.output / "Project-1_Loaded.msp2.dbs"
        loose.write_bytes(b"MS-DIAL wrote something else")
        record = self.put(loose)
        self.assertEqual((STORED, "sha256", False), (record["outcome"], record["verified_by"], record["recognised"]))
        self.assertEqual(_sha(self.store.object_path(record["object_id"])), record["sha256"])

        again = self.output / "Project-2_Loaded.msp2.dbs"
        again.write_bytes(b"MS-DIAL wrote something else")
        self.assertEqual((DEDUPLICATED, "sha256"), (lambda r: (r["outcome"], r["verified_by"]))(self.put(again)))
        other = self.output / "Project-3_Loaded.msp2.dbs"
        other.write_bytes(b"MS-DIAL wrote something other")
        self.assertNotEqual(record["object_id"], self.put(other)["object_id"])

    def test_a_lock_that_is_not_taken_leaves_the_copy_where_it_is_as_an_oserror(self) -> None:
        copy = self.copy()
        with patch.object(library_copy_store.StoreLock, "acquire", side_effect=StoreLockTimeout("held by pid 1")):
            with self.assertRaises(OSError) as raised:
                self.put(copy)
        self.assertIsInstance(raised.exception, LibraryCopyStoreError)
        self.assertTrue(copy.exists())
        self.assertIn("held by pid 1", str(raised.exception))

    def test_another_volume_is_copied_compared_and_only_then_removed(self) -> None:
        copy = self.copy()
        replace = os.replace

        def cross_volume(source, target):
            if str(target).endswith("library-copy.dbs") and "incoming" in str(target):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return replace(source, target)

        with patch.object(library_copy_store.os, "replace", cross_volume):
            record = self.put(copy)
        self.assertEqual((STORED, "copy"), (record["outcome"], record["stored_by"]))
        self.assertFalse(copy.exists())
        self.assertIsNone(self.store.problem(record["object_id"], full=True))

    def test_a_copy_that_breaks_while_it_is_staged_goes_back_where_it_was(self) -> None:
        copy = self.copy()
        content = copy.read_bytes()
        replace = library_copy_store._replace

        def refuse_install(source, target, *args, **kwargs):
            if Path(target).parent.parent.name == "o":
                raise PermissionError(13, "held by another program")
            return replace(source, target, *args, **kwargs)

        with patch.object(library_copy_store, "_replace", refuse_install):
            with self.assertRaises(PermissionError):
                self.put(copy)
        self.assertEqual(content, copy.read_bytes())
        self.assertEqual([], list((self.root / STORE_DIRECTORY_NAME / "incoming").iterdir()))

    def test_a_record_that_cannot_be_written_takes_the_install_back(self) -> None:
        copy = self.copy()
        content = copy.read_bytes()
        write = library_copy_store._write_json_atomic

        def refuse_entry(path, value):
            if Path(path).name == "entry.json":
                raise PermissionError(13, "Access is denied", str(path))
            return write(path, value)

        with patch.object(library_copy_store, "_write_json_atomic", refuse_entry):
            with self.assertRaises(PermissionError):
                self.put(copy)
        # Back where it was, so that its caller holds it there; nothing of it is left in the store.
        self.assertEqual(content, copy.read_bytes())
        self.assertEqual([], self.store.object_ids())
        self.assertEqual([], list((self.root / STORE_DIRECTORY_NAME / "incoming").iterdir()))

    def test_a_claim_that_cannot_be_written_leaves_the_copy_stored_and_named(self) -> None:
        copy = self.copy()
        with patch.object(LibraryCopyStore, "claim", side_effect=PermissionError(13, "Access is denied", "x.json")):
            record = self.put(copy)
        self.assertEqual((STORED, None), (record["outcome"], record["claim"]))
        self.assertIn("PermissionError", record["claim_error"])
        self.assertFalse(copy.exists())
        self.assertIsNone(self.store.problem(record["object_id"], full=True))
        self.assertEqual([], self.store.orphans())

    def _stop_after_install(self):
        """An install whose record cannot be written and whose copy cannot be taken back out of the store."""
        return _stop_after_install()

    def _unit_manifest(self, job_id: str = "job1") -> Path:
        manifest = self.root / UNIT["manifest"]
        _write_json(manifest, {"workspace": str(manifest.parent.parent), "output_directory": str(self.output),
                               "project": {"repository": "metabolights", "accession": "MTBLS-SYN",
                                           "analysis_unit_id": "unit-syn"},
                               "console_run_finalisation": {"job_id": job_id}})
        return manifest

    def test_a_copy_that_left_and_whose_install_stopped_is_pending_and_repair_completes_its_record(self) -> None:
        manifest = self._unit_manifest()
        copy = self.copy()
        lines: list[str] = []
        with self._stop_after_install():
            copies, errors = store_loaded_library_copies(self.output, self.store, unit=UNIT, job_id="job1",
                                                         libraries=LIBRARIES, log=lines.append)
        [pending] = copies
        # It is not in the output, so no hold there would keep it; the unit names it as pending instead.
        self.assertFalse(copy.exists())
        self.assertEqual((PENDING, copy.name), (pending["outcome"], pending["name"]))
        self.assertTrue(pending["removed_at"])
        self.assertEqual(1, len(errors))
        self.assertTrue(any("left the output for the library-copy store" in line for line in lines), lines)
        self.assertFalse(any("held in the output" in line for line in lines), lines)
        assert_names_no_root(self, self.root, json.dumps(copies) + json.dumps(errors) + "".join(lines))

        def record(current):
            current["console_run_finalisation"]["loaded_library_copy"] = {"copies": copies, "errors": errors}

        update_manifest(manifest, record)
        [orphan] = self.store.orphans()
        self.assertEqual("record", orphan["action"])
        [done] = self.store.repair(confirmed=True)["repaired"]

        self.assertEqual((STORED, pending["object_id"]), (done["outcome"], done["object_id"]))
        self.assertIsNone(self.store.problem(pending["object_id"], full=True))
        unit = read_manifest(manifest)
        [item] = unit["console_run_finalisation"]["loaded_library_copy"]["copies"]
        self.assertEqual((STORED, pending["object_id"]), (item["outcome"], item["object_id"]))
        self.assertNotIn("library_copy_records", unit, "completed where the run names its copy, not twice")

    def test_a_file_set_aside_with_no_record_is_named_and_its_unit_found_equal(self) -> None:
        manifest = self._unit_manifest()
        copy = self.copy(date_time=(2026, 10, 1, 10, 0, 0))
        with self._stop_after_install():
            with self.assertRaises(LibraryCopyPending) as raised:
                self.put(copy)
        pending = {**raised.exception.record, "outcome": PENDING}

        def record(current):
            current["console_run_finalisation"]["loaded_library_copy"] = {"copies": [pending], "errors": []}

        update_manifest(manifest, record)
        # Another unit stores the same libraries before anyone repairs: the file with no record is moved aside.
        later = self.put(self.copy("Project-2_Loaded.msp2.dbs", date_time=(2026, 10, 2, 10, 0, 0)), job_id="job2")
        self.assertEqual(STORED, later["outcome"])
        [repair] = self.store.entry(later["object_id"])["repairs"]
        self.assertEqual(later["set_aside"], repair["kept_as"])
        self.assertTrue((self.store.object_directory(later["object_id"]) / repair["kept_as"]).is_file())

        [done] = self.store.repair(confirmed=True)["repaired"]
        self.assertEqual(DEDUPLICATED, done["outcome"])
        [item] = read_manifest(manifest)["console_run_finalisation"]["loaded_library_copy"]["copies"]
        self.assertEqual((DEDUPLICATED, later["object_id"]), (item["outcome"], item["object_id"]))

    @unittest.skipUnless(WINDOWS, "hard links are compared by NTFS file id")
    def test_a_name_other_files_share_is_copied_so_no_object_shares_a_file_record(self) -> None:
        copy = self.copy()
        elsewhere = self.root / "_verification" / "V3d" / "output" / copy.name
        elsewhere.parent.mkdir(parents=True)
        os.link(copy, elsewhere)
        record = self.put(copy)

        self.assertEqual("copy", record["stored_by"])
        self.assertFalse(copy.exists())
        self.assertTrue(elsewhere.exists())
        self.assertEqual(1, os.stat(elsewhere).st_nlink)
        self.assertNotEqual(os.stat(elsewhere).st_ino, os.stat(self.store.object_path(record["object_id"])).st_ino)

    def test_two_units_storing_one_content_at_once_store_it_once(self) -> None:
        copies = [self.copy(f"Project-{index}_Loaded.msp2.dbs", date_time=(2026, 10, 1, 10, index, 0))
                  for index in range(2)]
        identities = [read_copy_identity(path) for path in copies]
        results: list[dict] = []

        def store(index: int) -> None:
            results.append(LibraryCopyStore(self.root).put(copies[index], identities[index], unit=UNIT,
                                                           job_id=f"job{index}"))

        threads = [threading.Thread(target=store, args=(index,)) for index in range(2)]

        def quick(store_, object_id, job_id=""):
            return StoreLock(store_.root / "locks" / f"{object_id}.lock", job_id=job_id, poll_seconds=0.05)

        with patch.object(LibraryCopyStore, "lock", quick):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(sorted([STORED, DEDUPLICATED]), sorted(item["outcome"] for item in results))
        self.assertFalse(any(path.exists() for path in copies))

    def test_an_object_that_cannot_be_read_is_a_problem_named_relative_to_the_root(self) -> None:
        record = self.put(self.copy())
        object_path = self.store.object_path(record["object_id"])
        denied = PermissionError(13, "The process cannot access the file", str(object_path))
        with patch.object(library_copy_store, "entry_table", side_effect=denied):
            problem = self.store.problem(record["object_id"])
        self.assertIn("cannot be read", problem)
        self.assertIn(f"{STORE_DIRECTORY_NAME}/o/{record['object_id']}/library-copy.dbs", problem)
        assert_names_no_root(self, self.root, problem)
        with patch.object(library_copy_store, "_read_json", side_effect=PermissionError(13, "denied", str(object_path))):
            problem = self.store.problem(record["object_id"])
        self.assertIn("its record cannot be read", problem)
        assert_names_no_root(self, self.root, problem)

    def test_errors_name_files_relative_to_the_workspace_root(self) -> None:
        target = self.root / STORE_DIRECTORY_NAME / "o" / ("0" * 32) / "library-copy.dbs"
        error = PermissionError(13, "Access is denied", str(target))
        text = describe_error(error, self.root)
        self.assertNotIn(str(self.root), text)
        self.assertIn(f"{STORE_DIRECTORY_NAME}/o/{'0' * 32}/library-copy.dbs", text)
        self.assertNotIn(str(self.root), describe_error(LibraryCopyStoreError(f"x {self.root} y"), self.root))


class SavedOverTests(_Store):
    """MS-DIAL saves the library copy by deleting it and moving a new file into place, and holds nothing open
    between saves: a save can land between the read of a copy's identity and its removal. What is removed or moved
    is only ever the file that was identified."""

    def save_over(self, path: Path) -> bytes:
        path.unlink()
        synthetic_dbs(path, storage=b"saved by MS-DIAL's GUI", date_time=(2026, 10, 12, 9, 30, 0))
        return path.read_bytes()

    def assert_no_private_names(self) -> None:
        self.assertEqual([], [path.name for path in self.output.iterdir() if "~" in path.name or path.name.startswith(".")])

    def test_an_equal_copy_saved_over_after_it_was_identified_is_left_and_unclaimed(self) -> None:
        first = self.put(self.copy(date_time=(2026, 10, 1, 10, 0, 0)))
        copy = self.copy("Project-2_Loaded.msp2.dbs", date_time=(2026, 10, 2, 10, 0, 0))
        identity = read_copy_identity(copy)
        saved = self.save_over(copy)

        with self.assertRaisesRegex(LibraryCopyStoreError, "changed after it was identified"):
            self.store.put(copy, identity, unit=UNIT, job_id="job2", libraries=LIBRARIES)
        self.assertEqual(saved, copy.read_bytes(), "the GUI's file is never the one removed")
        self.assertEqual(["job1"], [claim["job_id"] for claim in self.store.claims(first["object_id"])])
        self.assert_no_private_names()

    def test_a_new_copy_saved_over_after_it_was_identified_goes_back_and_nothing_is_stored(self) -> None:
        copy = self.copy()
        identity = read_copy_identity(copy)
        saved = self.save_over(copy)

        with self.assertRaisesRegex(LibraryCopyStoreError, "changed after it was identified"):
            self.store.put(copy, identity, unit=UNIT, job_id="job1", libraries=LIBRARIES)
        self.assertEqual(saved, copy.read_bytes())
        self.assertEqual([], self.store.object_ids())
        self.assertEqual([], list((self.root / STORE_DIRECTORY_NAME / "incoming").iterdir()))

    def test_a_copy_saved_over_while_its_bytes_were_copied_in_is_left_in_place(self) -> None:
        copy = self.copy()
        os.link(copy, self.root / "elsewhere.dbs")  # another name: copied, never renamed
        identity = read_copy_identity(copy)
        claim = LibraryCopyStore.claim
        saved: list[bytes] = []

        def save_then_claim(store, *args, **kwargs):
            saved.append(self.save_over(copy))  # between the install and the removal of the source
            return claim(store, *args, **kwargs)

        with patch.object(LibraryCopyStore, "claim", save_then_claim):
            with self.assertRaisesRegex(LibraryCopyStoreError, "changed after it was identified"):
                self.store.put(copy, identity, unit=UNIT, job_id="job1", libraries=LIBRARIES)
        self.assertEqual(saved[0], copy.read_bytes())
        # The identified bytes are in the store all the same: they were copied in and compared first.
        self.assertIsNone(self.store.problem(identity.object_id, full=True))
        self.assert_no_private_names()

    def test_a_copy_taken_back_never_overwrites_a_file_that_took_its_place(self) -> None:
        copy = self.copy()
        write = library_copy_store._write_json_atomic
        saved: list[bytes] = []

        def save_then_refuse(path, value):
            if Path(path).name == "entry.json":
                synthetic_dbs(copy, storage=b"saved by MS-DIAL's GUI")
                saved.append(copy.read_bytes())
                raise PermissionError(13, "Access is denied", str(path))
            return write(path, value)

        with patch.object(library_copy_store, "_write_json_atomic", save_then_refuse):
            with self.assertRaises(LibraryCopyPending):
                self.put(copy)
        self.assertEqual(saved[0], copy.read_bytes(), "never written over")
        [done] = self.store.repair(confirmed=True)["repaired"]
        self.assertEqual(STORED, done["outcome"])
        self.assertIsNone(self.store.problem(done["object_id"], full=True))

    def test_a_put_back_refuses_a_name_that_exists(self) -> None:
        one, two = self.output / "one.bin", self.output / "two.bin"
        one.write_bytes(b"one")
        two.write_bytes(b"two")
        with self.assertRaises(FileExistsError):
            library_copy_store.rename_back(one, two)
        self.assertEqual((b"one", b"two"), (one.read_bytes(), two.read_bytes()))


# Entries larger than one read, compressible and not: a flipped bit in their deflate data is either a zlib.error
# or other bytes that only the CRC at the entry's end names.
_ALPHABET = random.Random(7)
BIG_DATABASES = {
    "MetabolomicsDB\\MspDB_1_Synthetic-Private-pos\\DataBase": bytes(_ALPHABET.choice(b"ACGT0123456789,.\n")
                                                                      for _ in range(60_000)),
    "MetabolomicsDB\\LbmDB\\DataBase": random.Random(8).randbytes(20_000),
}


class DamageTests(_Store):
    def offsets(self, path: Path, index: int = 0, count: int = 8) -> list[int]:
        start, length = entry_data_offset(path, index)
        return [start + 100 + step * (length - 200) // count for step in range(count)]

    def test_a_copy_damaged_inside_its_deflate_data_is_held_and_nothing_raises(self) -> None:
        template = synthetic_dbs(self.root / "template" / "P_Loaded.msp2.dbs", databases=BIG_DATABASES,
                                 storage=bytes(_ALPHABET.choice(b"xyz") for _ in range(8_000)))
        offsets = self.offsets(template) + self.offsets(template, index=2, count=3)  # entry 2: Storage
        for number, offset in enumerate(offsets):
            with self.subTest(offset=offset):
                output = self.output.parent / f"output-{number}"
                output.mkdir()
                copy = output / "Project-1_Loaded.msp2.dbs"
                copy.write_bytes(template.read_bytes())
                flip(copy, offset, 0x55)
                lines: list[str] = []
                copies, errors = store_loaded_library_copies(output, self.store, unit=UNIT, job_id=f"job{number}",
                                                             libraries=LIBRARIES, log=lines.append)
                self.assertEqual(([], 1), (copies, len(errors)))
                self.assertTrue(copy.exists(), "held where it is, never deleted")
                self.assertTrue(any("held in the output" in line for line in lines), lines)
        self.assertEqual([], self.store.object_ids())

    def test_an_object_damaged_inside_an_entry_larger_than_a_read_is_repaired_never_a_reason_to_quarantine(self) -> None:
        with patch.object(library_copy_store, "_CHUNK", 4096):
            template = synthetic_dbs(self.root / "template" / "P1_Loaded.msp2.dbs", databases=BIG_DATABASES)
            for number, offset in enumerate(self.offsets(template) + self.offsets(template, index=1, count=4)):
                with self.subTest(offset=offset):
                    store = LibraryCopyStore(self.root / f"w{number}")
                    first = synthetic_dbs(store.workspace_root / "u" / "P1_Loaded.msp2.dbs", databases=BIG_DATABASES)
                    stored = store.put(first, read_copy_identity(first), unit=None, job_id="job1")
                    flip(store.object_path(stored["object_id"]), offset, 0x10)
                    # The same content written otherwise: one key, other bytes, compared entry by entry.
                    copy = synthetic_dbs(store.workspace_root / "u" / "P2_Loaded.msp2.dbs", databases=BIG_DATABASES,
                                         method=zipfile.ZIP_STORED, date_time=(2026, 10, 2, 10, 0, 0))
                    record = store.put(copy, read_copy_identity(copy), unit=None, job_id="job2")

                    self.assertEqual((STORED, True), (record["outcome"], record.get("repaired_object")))
                    self.assertFalse(copy.exists())
                    self.assertFalse((store.root / "quarantine").exists())
                    self.assertIsNone(store.problem(stored["object_id"], full=True))
                    self.assertEqual(1, len(list(store.object_directory(stored["object_id"]).glob("damaged-*.dbs"))))

    def test_a_difference_is_never_believed_of_an_object_that_differs_from_its_record(self) -> None:
        first = self.put(self.copy(method=zipfile.ZIP_STORED))
        object_path = self.store.object_path(first["object_id"])
        start, length = entry_data_offset(object_path)
        flip(object_path, start + length // 2)
        copy = self.copy("Project-2_Loaded.msp2.dbs", method=zipfile.ZIP_DEFLATED)
        with patch.object(library_copy_store, "_compare_inflated", return_value="different"):
            record = self.put(copy, job_id="job2")
        self.assertEqual((STORED, True), (record["outcome"], record.get("repaired_object")))
        self.assertFalse((self.root / STORE_DIRECTORY_NAME / "quarantine").exists())

    @unittest.skipUnless(WINDOWS, "a sharing violation is how Windows refuses a file another handle holds")
    def test_an_object_msdial_holds_open_is_never_taken_for_damaged(self) -> None:
        import ctypes
        from ctypes import wintypes

        first = self.put(self.copy(date_time=(2026, 10, 1, 10, 0, 0)))
        object_path = self.store.object_path(first["object_id"])
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        # As DirectoryTreeStreamManager.Get opens it while MS-DIAL loads a restored project: File.Open(file,
        # FileMode.Open, FileAccess.Read), which shares nothing.
        handle = kernel32.CreateFileW(str(object_path), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotIn(handle, (None, ctypes.c_void_p(-1).value))
        copy = self.copy("Project-2_Loaded.msp2.dbs", date_time=(2026, 10, 2, 10, 0, 0))
        try:
            with self.assertRaisesRegex(LibraryCopyStoreError, "could not be read"):
                self.put(copy, job_id="job2")
        finally:
            kernel32.CloseHandle(handle)
        self.assertTrue(copy.exists())
        self.assertEqual([], list(object_path.parent.glob("damaged-*.dbs")))
        self.assertNotIn("repairs", self.store.entry(first["object_id"]))
        self.assertEqual(DEDUPLICATED, self.put(copy, job_id="job2")["outcome"])


class RepairTests(_Store):
    def test_a_crash_after_staging_is_completed_not_quarantined(self) -> None:
        copy = self.copy()
        manifest = self.output.parent / "provenance" / "run-manifest.json"
        _write_json(manifest, {"workspace": str(self.output.parent), "project": {
            "repository": "metabolights", "accession": "MTBLS-SYN", "analysis_unit_id": "unit-syn"}})
        replace = library_copy_store._replace
        died: list[bool] = []

        def crash(source, target, *args, **kwargs):
            if died or Path(target).parent.parent.name == "o":
                died.append(True)
                raise KeyboardInterrupt  # the process dies between the move and the install: nothing after runs
            return replace(source, target, *args, **kwargs)

        with patch.object(library_copy_store, "_replace", crash), \
                patch.object(library_copy_store, "rename_back", crash):
            with self.assertRaises(KeyboardInterrupt):
                self.put(copy)
        self.assertFalse(copy.exists(), "the only copy is in incoming/")
        status = self.store.summary()
        [orphan] = status["unfinished_ingests"]
        self.assertEqual(("complete", True), (orphan["action"], orphan["staged_copy"]))
        self.assertEqual([], status["objects"])

        preview = self.store.repair()
        self.assertEqual([], preview["repaired"])
        repaired = self.store.repair(confirmed=True)

        [done] = repaired["repaired"]
        self.assertEqual(STORED, done["outcome"])
        self.assertIsNone(self.store.problem(done["object_id"], full=True))
        self.assertEqual([], self.store.orphans())
        [record] = read_manifest(manifest)["library_copy_records"]
        self.assertEqual(("repair", "job1", done["object_id"]), (record["action"], record["job_id"], record["object_id"]))

    def test_a_staged_copy_cut_short_is_set_aside_and_recorded_nowhere(self) -> None:
        manifest = self.output.parent / "provenance" / "run-manifest.json"
        _write_json(manifest, {"workspace": str(self.output.parent), "project": {
            "repository": "metabolights", "accession": "MTBLS-SYN", "analysis_unit_id": "unit-syn"}})
        copy = self.copy()
        identity = read_copy_identity(copy)
        # A hard kill during the copy of a source another name shares: the intent, and a staged file cut short.
        incoming = self.root / STORE_DIRECTORY_NAME / "incoming" / ("1" * 32)
        incoming.mkdir(parents=True)
        (incoming / "intent.json").write_text(json.dumps({
            "schema": "msdial-library-copy-intent.v1", "object_id": identity.object_id,
            "copy": identity.record(copy.name), "unit": UNIT, "job_id": "job1", "how": "retry",
            "libraries": [], "annotation_settings": None, "keep_source": False,
        }), encoding="utf-8")
        (incoming / OBJECT_FILE).write_bytes(copy.read_bytes()[:300])

        [done] = self.store.repair(confirmed=True)["repaired"]

        self.assertEqual(QUARANTINED, done["outcome"])
        self.assertEqual([], self.store.object_ids())
        self.assertNotIn("library_copy_records", read_manifest(manifest))
        reason = json.loads((self.root / done["quarantine"] / "reason.json").read_text(encoding="utf-8"))
        self.assertEqual((identity.object_id, 300), (reason["object_id"], reason["found"]["size_bytes"]))
        self.assertTrue(copy.exists(), "its source is untouched")


class SeedAndAttachTests(_Store):
    def setUp(self) -> None:
        super().setUp()
        self.reference = self.root / "_verification" / "V3d" / "output"
        self.reference.mkdir(parents=True)
        _settings(self.reference, "D:\\libs")
        (self.reference / "run-manifest.json").write_text(json.dumps({"libraries": LIBRARIES}), encoding="utf-8")
        (self.reference / "workflow-settings.json").write_text(json.dumps({
            "project_type": "lcms", "msp_annotators": [{"msp_file_path": "D:\\libs\\Synthetic-Private-pos.msp"}],
            "lbm_path": "D:\\libs\\Synthetic.lbm2",
        }), encoding="utf-8")

    def test_a_seed_copies_and_never_moves_and_a_preview_writes_nothing(self) -> None:
        source = synthetic_dbs(self.reference / "Project-9_Loaded.msp2.dbs")
        digest = _sha(source)
        preview = self.store.seed(source)
        self.assertFalse(preview["seeded"])
        self.assertFalse((self.root / STORE_DIRECTORY_NAME).exists())
        self.assertEqual("_verification/V3d/output/Project-9_Loaded.msp2.dbs", preview["source"])

        seeded = self.store.seed(source, confirmed=True)
        self.assertTrue(seeded["seeded"])
        self.assertEqual(digest, _sha(source))
        self.assertEqual(1, os.stat(source).st_nlink)
        entry = self.store.entry(seeded["object_id"])
        self.assertEqual("seed", entry["ingested"]["how"])
        self.assertEqual({"Synthetic-Private-pos.msp", "Synthetic.lbm2"}, {item["name"] for item in entry["libraries"]})
        self.assertIsNotNone(entry["annotation_settings"])
        self.assertFalse(self.store.seed(source, confirmed=True)["seeded"], "already held: nothing copied")

        loose = self.reference / "other.dbs"
        loose.write_bytes(b"no zip")
        with self.assertRaises(LibraryCopyStoreError):
            self.store.seed(loose, confirmed=True)

    def test_annotation_settings_read_paths_as_names(self) -> None:
        moved = self.root / "moved"
        moved.mkdir()
        _settings(moved, "E:\\new place\\libs")
        self.assertEqual(annotation_settings(self.reference)["sha256"], annotation_settings(moved)["sha256"])
        _settings(moved, "E:\\new place\\libs", cutoff="0.7")
        self.assertNotEqual(annotation_settings(self.reference)["sha256"], annotation_settings(moved)["sha256"])
        self.assertIsNone(annotation_settings(self.root / "nowhere"))

    def _finalised_unit(self, libraries=None, settings_root: str = "F:\\libs", **settings) -> Path:
        workspace = self.output.parent
        _settings(self.output, settings_root, **settings)
        manifest = workspace / "provenance" / "run-manifest.json"
        _write_json(manifest, {
            "workspace": str(workspace), "output_directory": str(self.output), "status": "mztab_validated",
            "project": {"repository": "metabolights", "accession": "MTBLS-SYN", "analysis_unit_id": "unit-syn"},
            "console_run_finalisation": {
                "job_id": "job-legacy", "campaign_approval_recorded": True, "libraries": libraries or LIBRARIES,
                "loaded_library_copies_deleted": [{"name": "Project-1_Loaded.msp2.dbs", "size_bytes": 1234,
                                                   "sha256": "12" * 32, "deleted_at": "2026-10-02T00:00:00+00:00"}],
            },
        })
        return manifest

    def test_attach_names_the_object_a_deleted_copy_stands_for_when_libraries_and_settings_agree(self) -> None:
        object_id = self.store.seed(synthetic_dbs(self.reference / "P_Loaded.msp2.dbs"), confirmed=True)["object_id"]
        manifest = self._finalised_unit()
        before = manifest.read_bytes()

        preview = attach(manifest, object_id)
        self.assertEqual([], preview["refused"])
        self.assertEqual(before, manifest.read_bytes())
        done = attach(manifest, object_id, confirmed=True)

        self.assertTrue(done["attached"])
        [record] = read_manifest(manifest)["library_copy_records"]
        self.assertEqual(("attach", "library_and_settings_identity", "job-legacy", object_id),
                         (record["action"], record["verified_by"], record["job_id"], record["object_id"]))
        self.assertEqual("msdial-library-copy-record.v1", record["schema"])
        self.assertIn("attach", {claim["action"] for claim in self.store.claims(object_id)})
        self.assertNotIn("loaded_library_copy", read_manifest(manifest)["console_run_finalisation"])
        with self.assertRaisesRegex(LibraryCopyStoreError, "already names"):
            attach(manifest, object_id)

    def test_attach_reads_a_copy_the_retry_of_a_held_deletion_deleted(self) -> None:
        object_id = self.store.seed(synthetic_dbs(self.reference / "P_Loaded.msp2.dbs"), confirmed=True)["object_id"]
        manifest = self._finalised_unit()
        deleted = read_manifest(manifest)["console_run_finalisation"]["loaded_library_copies_deleted"]

        def held_then_retried(current):
            # Up to 0.5.36: none deleted at finalisation, the copy held, and the hold's retry deleting it.
            current["console_run_finalisation"]["loaded_library_copies_deleted"] = []
            current["finalisation_hold_resolutions"] = [
                {"hold": "h1", "step": "loaded_library_copy", "job_id": "job-earlier", "deleted": [{"name": "x"}]},
                {"hold": "h2", "step": "loaded_library_copy", "job_id": "job-legacy", "deleted": deleted},
            ]

        update_manifest(manifest, held_then_retried)
        done = attach(manifest, object_id, confirmed=True)
        self.assertTrue(done["attached"], done)
        self.assertEqual(deleted[0]["sha256"], done["deleted_copy"]["sha256"])

    def test_attach_refuses_other_libraries_or_other_settings(self) -> None:
        object_id = self.store.seed(synthetic_dbs(self.reference / "P_Loaded.msp2.dbs"), confirmed=True)["object_id"]
        other = [{**LIBRARIES[0], "sha256": "00" * 32}, LIBRARIES[1]]
        refused = attach(self._finalised_unit(libraries=other), object_id, confirmed=True)
        self.assertFalse(refused["attached"])
        self.assertTrue(any("libraries" in item for item in refused["refused"]))

        refused = attach(self._finalised_unit(cutoff="0.9"), object_id, confirmed=True)
        self.assertFalse(refused["attached"])
        self.assertTrue(any("weighted dot product cutoff for msp-based annotation" in item
                            for item in refused["refused"]), refused["refused"])
        self.assertNotIn("library_copy_records", read_manifest(self.output.parent / "provenance" / "run-manifest.json"))

    def test_the_command_line_previews_and_reports(self) -> None:
        source = synthetic_dbs(self.reference / "P_Loaded.msp2.dbs")
        out = io.StringIO()
        with redirect_stdout(out):
            code = library_copy_store.main(["seed", str(source), "--workspace-root", str(self.root)])
        self.assertEqual(0, code)
        self.assertFalse(json.loads(out.getvalue())["seeded"])
        out = io.StringIO()
        with redirect_stdout(out):
            library_copy_store.main(["seed", str(source), "--workspace-root", str(self.root), "--confirmed"])
        self.assertTrue(json.loads(out.getvalue())["seeded"])
        out = io.StringIO()
        with redirect_stdout(out):
            library_copy_store.main(["status", "--workspace-root", str(self.root), "--verify"])
        status = json.loads(out.getvalue())
        [item] = status["objects"]
        self.assertEqual((None, "full", "passed"), (item["problem"], item["verified"], item["testzip"]))
        assert_names_no_root(self, self.root, json.dumps(status))


def _settings(directory: Path, library_root: str, cutoff: str = "0.6") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "method.txt").write_text("\n".join([
        f"MSP annotator settings file path: {directory / 'msp_annotator_settings.tsv'}",
        "Annotation pipeline profile: lipid-rule-msp-high-low-v1",
        "Minimum peak height: 1000",
        "Ion mode: Positive",
        f"Lbm file path: {library_root}\\Synthetic.lbm2",
        "Searched adduct ions: [M+H]+,[M+Na]+",
        f"Weighted dot product cutoff for MSP-based annotation: {cutoff}",
        f"Export folder path: {directory}",
    ]) + "\n", encoding="utf-8")
    (directory / "msp_annotator_settings.tsv").write_text(
        "annotator_id\tmsp_file_path\tpriority\n"
        f"msp_annotator_1\t{library_root}\\Synthetic-Private-pos.msp\t1\n", encoding="ascii")


class WhereTests(unittest.TestCase):
    def test_the_workspace_root_is_found_from_the_lease_or_the_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = {"repository": "metabolights", "accession": "MTBLS2207"}
            part = root / "metabolights" / "MTBLS2207" / "a22083b091a0ccd04489-dda"
            self.assertEqual(root, workspace_root_for({"workspace": str(part), "project": project})[0])
            self.assertEqual(root, workspace_root_for(
                {"workspace": str(root / "metabolights" / "MTBLS2207"), "project": project})[0])
            self.assertEqual(root, workspace_root_for({"download_cache": {"workspace_root": str(root)}})[0])
            store, why = library_copy_store_for({"workspace": str(root / "elsewhere" / "unit"), "project": project})
            self.assertIsNone(store)
            self.assertIn("download_cache.workspace_root", why)

    def test_the_store_the_console_temp_and_the_viewing_directory_are_never_units(self) -> None:
        self.assertTrue({"_library_copies", "_tmp", "_viewing", "_dl", "_campaigns"} <= RESERVED_WORKSPACE_NAMES)


class ConsoleTempTests(_Store):
    def test_leftovers_are_listed_a_library_copy_is_stored_and_the_rest_removed(self) -> None:
        manifest = self.output.parent / "provenance" / "run-manifest.json"
        _write_json(manifest, {"workspace": str(self.output.parent), "project": {
            "repository": "metabolights", "accession": "MTBLS-SYN", "analysis_unit_id": "unit-syn"}})
        temp = self.root / "_tmp" / "job1"
        synthetic_dbs(temp / "tmp4F2A.tmp")
        (temp / "tmp4F2B.tmp").write_bytes(b"x" * 10)
        (temp / "nested").mkdir()
        (temp / "nested" / "tmp4F2C.tmp").write_bytes(b"y" * 5)

        settled = settle_console_temp(temp, manifest_path=manifest, job_id="job1", libraries=LIBRARIES)

        self.assertEqual("_tmp/job1", settled["directory"])
        self.assertEqual({"tmp4F2A.tmp", "tmp4F2B.tmp", "nested/tmp4F2C.tmp"},
                         {item["name"] for item in settled["leftovers"]})
        [stored] = settled["library_copies"]
        self.assertEqual((STORED, "console_temp"), (stored["outcome"],
                                                    self.store.entry(stored["object_id"])["ingested"]["how"]))
        self.assertEqual((2, 15), (settled["removed_files"], settled["removed_bytes"]))
        self.assertEqual([], settled["held"])
        self.assertFalse(temp.exists())
        assert_names_no_root(self, self.root, json.dumps(settled))

    @unittest.skipUnless(WINDOWS, "a directory junction is an NTFS reparse point")
    def test_a_junction_in_it_is_listed_and_never_followed(self) -> None:
        import _winapi

        manifest = self.output.parent / "provenance" / "run-manifest.json"
        _write_json(manifest, {"workspace": str(self.output.parent)})
        victim = self.root / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_bytes(b"not the Console's")
        synthetic_dbs(victim / "Elsewhere_Loaded.msp2.dbs")
        temp = self.root / "_tmp" / "job1"
        temp.mkdir(parents=True)
        (temp / "tmp1.tmp").write_bytes(b"x" * 3)
        _winapi.CreateJunction(str(victim), str(temp / "j"))

        settled = settle_console_temp(temp, manifest_path=manifest, job_id="job1")

        self.assertEqual({"tmp1.tmp", "j"}, {item["name"] for item in settled["leftovers"]})
        self.assertEqual([True], [item.get("link") for item in settled["leftovers"] if item["name"] == "j"])
        self.assertEqual(([], 1), (settled["library_copies"], settled["removed_files"]))
        self.assertTrue((victim / "keep.txt").is_file())
        self.assertTrue((victim / "Elsewhere_Loaded.msp2.dbs").is_file())
        self.assertFalse(temp.exists(), "the junction itself goes with the directory")

    def test_a_library_copy_with_no_store_is_held_where_it_is(self) -> None:
        manifest = self.root / "loose" / "provenance" / "run-manifest.json"
        _write_json(manifest, {"workspace": str(manifest.parent.parent)})
        temp = self.root / "_tmp" / "job2"
        copy = synthetic_dbs(temp / "tmp1.tmp")
        settled = settle_console_temp(temp, manifest_path=manifest, job_id="job2")
        self.assertEqual([str(copy)], settled["held"])
        self.assertTrue(copy.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

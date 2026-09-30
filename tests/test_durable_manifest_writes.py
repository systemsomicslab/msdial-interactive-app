"""A unit manifest is replaced whole or not at all, and two writers cannot lose each other's change.

The manifest is the only record of a unit that outlives the job registry, and it was written with a
plain write_text: open, which truncates, then write. A process stopped between the two - a reboot, a
backend restart, a value that could not be encoded - left an empty or half-written file where the unit's
record had been. At campaign scale, with weeks of crash-and-resume, the record a resumed run needs is
exactly the one most likely to be caught mid-write.

Two writers were also possible and nothing ordered them: the campaign runner calls these functions in its
own process while the backend's run job finalises the same manifest, and each read the whole file, changed
it and wrote it back over the other's change.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app import repository_reanalysis
from msdial_app.repository_reanalysis import (
    _write_json,
    finalize_download_lease,
    is_manifest_scratch_file,
    manifest_lock,
    read_manifest,
    update_manifest,
)

ROOT = Path(__file__).resolve().parents[1]


class AWriteThatFailsLeavesThePreviousRecord(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.manifest = Path(self.directory.name) / "provenance" / "run-manifest.json"
        self.manifest.parent.mkdir()
        _write_json(self.manifest, {"status": "mztab_validated", "retained_artifacts": ["a", "b"]})

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _leftovers(self) -> list[str]:
        return sorted(
            path.name for path in self.manifest.parent.iterdir() if path.name.endswith(".tmp")
        )

    def test_a_value_that_cannot_be_encoded_does_not_truncate_the_manifest(self) -> None:
        """THE REGRESSION. write_text opened the file for writing - truncating it - before it encoded a
        single character, so an unencodable value (a lone surrogate from an undecodable file name, say)
        left an empty file where the unit's record had been."""
        with self.assertRaises(UnicodeEncodeError):
            _write_json(self.manifest, {"status": "prepared", "file": "sample\udc80.raw"})

        self.assertEqual("mztab_validated", read_manifest(self.manifest)["status"])
        self.assertEqual([], self._leftovers())

    def test_a_crash_before_the_rename_leaves_the_previous_manifest(self) -> None:
        with patch.object(repository_reanalysis.os, "replace", side_effect=OSError("power lost")):
            with self.assertRaises(OSError):
                _write_json(self.manifest, {"status": "raw_cleaned"})

        self.assertEqual("mztab_validated", read_manifest(self.manifest)["status"])
        self.assertEqual([], self._leftovers(), "an unfinished temporary file is removed")

    def test_a_crash_while_flushing_leaves_the_previous_manifest(self) -> None:
        with patch.object(repository_reanalysis.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                _write_json(self.manifest, {"status": "raw_cleaned"})

        self.assertEqual(["a", "b"], read_manifest(self.manifest)["retained_artifacts"])
        self.assertEqual([], self._leftovers())

    def test_a_reader_never_sees_a_partial_manifest(self) -> None:
        """Readers take no lock; the single rename is what keeps them safe."""
        stop = threading.Event()
        partial: list[str] = []

        def read() -> None:
            while not stop.is_set():
                try:
                    json.loads(self.manifest.read_text(encoding="utf-8"))
                except (PermissionError, FileNotFoundError):
                    continue  # the instant of a rename on Windows; never a torn file
                except ValueError as error:
                    partial.append(str(error))

        reader = threading.Thread(target=read)
        reader.start()
        try:
            for index in range(150):
                _write_json(self.manifest, {"status": "prepared", "rows": ["x" * 200] * (index % 40 + 1)})
        finally:
            stop.set()
            reader.join(timeout=10)

        self.assertEqual([], partial)


class ConcurrentWritersDoNotLoseEachOther(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.manifest = Path(self.directory.name) / "run-manifest.json"
        _write_json(self.manifest, {"count": 0, "writers": []})

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_threads_updating_one_manifest_keep_every_update(self) -> None:
        def increment(label: str) -> None:
            for _ in range(25):
                def change(manifest: dict) -> None:
                    manifest["count"] += 1
                    manifest["writers"].append(label)

                update_manifest(self.manifest, change)

        threads = [threading.Thread(target=increment, args=(f"t{index}",)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        manifest = read_manifest(self.manifest)
        self.assertEqual(150, manifest["count"])
        self.assertEqual(150, len(manifest["writers"]))

    def test_processes_updating_one_manifest_keep_every_update(self) -> None:
        """The runner and the backend are two processes; a thread lock alone would not order them."""
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from msdial_app.repository_reanalysis import update_manifest\n"
            "def change(manifest):\n"
            "    manifest['count'] += 1\n"
            "for _ in range(40):\n"
            f"    update_manifest({str(self.manifest)!r}, change)\n"
        )
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        processes = [
            subprocess.Popen([sys.executable, "-c", script], env=environment) for _ in range(3)
        ]
        for process in processes:
            self.assertEqual(0, process.wait(timeout=120))

        self.assertEqual(120, read_manifest(self.manifest)["count"])

    def test_a_writer_that_dies_holding_the_lock_does_not_block_the_next(self) -> None:
        """An operating-system lock dies with its process, so there is no stale lock to recover and no
        process id to probe for liveness."""
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from msdial_app.repository_reanalysis import manifest_lock\n"
            f"with manifest_lock({str(self.manifest)!r}):\n"
            "    print('held', flush=True)\n"
            "    os._exit(0)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertIn("held", completed.stdout)

        with manifest_lock(self.manifest, timeout=5):
            pass

    def test_the_lock_is_reentrant_within_a_thread(self) -> None:
        with manifest_lock(self.manifest, timeout=1):
            _write_json(self.manifest, {"count": 7, "writers": []})

        self.assertEqual(7, read_manifest(self.manifest)["count"])

    def test_updating_a_missing_manifest_creates_nothing(self) -> None:
        missing = Path(self.directory.name) / "absent" / "run-manifest.json"

        with self.assertRaises(FileNotFoundError):
            update_manifest(missing, lambda manifest: None)

        self.assertFalse(missing.parent.exists())


class TheWritersScratchFilesAreNeverArtifacts(unittest.TestCase):
    def test_locks_and_temporary_files_are_not_retained(self) -> None:
        """Finalisation keeps every provenance file. A lock or an abandoned temporary file of a write is
        not a record of the unit, and listing it would put noise into the inventory a deletion is judged
        against."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            output = root / "output"
            provenance = root / "provenance"
            for directory in (root / "raw", output, provenance):
                directory.mkdir(parents=True)
            (output / "result.mzTab").write_text(
                "MTD\tmzTab-version\t2.0.0-M\nSMH\tSML_ID\nSML\t1\n", encoding="ascii"
            )
            manifest = provenance / "run-manifest.json"
            _write_json(
                manifest,
                {
                    "status": "prepared",
                    "workspace": str(root),
                    "raw_directory": str(root / "raw"),
                    "output_directory": str(output),
                },
            )
            _write_json(provenance / "repository-metadata.json", {"accession": "X"})
            (provenance / ".run-manifest.json.abandoned.tmp").write_text("{", encoding="utf-8")

            result = finalize_download_lease(manifest)

            names = [Path(path).name for path in result["retained_artifacts"]]
            self.assertIn("repository-metadata.json", names)
            self.assertFalse([name for name in names if is_manifest_scratch_file(name)], names)
            self.assertTrue((provenance / "run-manifest.json.lock").is_file(), "the lock stays in place")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

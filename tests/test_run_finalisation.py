"""What a finished Console run leaves behind is put where it belongs before validation and cleanup.

MS-DIAL writes its per-file and alignment containers beside the files it reads, so deleting a unit's raw data
deleted them; every earlier attempt left a set of its own under a minute-resolution name; and the output held
<project>_Loaded.msp2.dbs, a serialised copy of every library the run loaded - with the private VS21 pair, a
copy of the private library in every unit.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from msdial_app import server
from msdial_app.repository_reanalysis import _write_json, read_manifest
from msdial_app.run_finalisation import (
    INTERMEDIATES_DIRECTORY,
    delete_loaded_library_copies,
    relocate_intermediates,
)

CURRENT = "20269271740"
EARLIER = "20269261728"
DIAGNOSTIC = "2026925025"
SIBLING = "20269271814"
ALIGNMENT_SUFFIXES = (".arf2", ".dcl", ".EIC.aef", "_PeakProperties.arf", "_DriftSopts.arf", "_tags.xml")
PRIVATE = "E:\\lab libs\\Synthetic-Private-pos.msp"  # synthetic; nothing is read from it


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _raw_tree(data: Path, inputs: list[str]) -> dict[str, list[Path]]:
    """MTBLS2207's shape: this run's set, an earlier failed attempt, a diagnostic, and a split sibling's run."""
    made: dict[str, list[Path]] = {"current": [], "superseded": [], "foreign": []}
    for name in inputs:
        _write(data / f"{name}.mzML", b"<mzML/>")
        for suffix in (".dcl", ".pai2", "_tags.xml"):
            made["current"].append(_write(data / f"{name}_{CURRENT}{suffix}", f"{name}{suffix}".encode()))
            made["superseded"].append(_write(data / f"{name}_{EARLIER}{suffix}"))
        for suffix in (".dcl", ".pai2"):
            made["superseded"].append(_write(data / f"{name}_{DIAGNOSTIC}{suffix}"))
    # An AIF run's per-energy deconvolution: <file>_<ts>_<CE x 100>.dcl.
    made["current"].append(_write(data / f"{inputs[0]}_{CURRENT}_3450.dcl"))
    for suffix in ALIGNMENT_SUFFIXES:
        made["current"].append(_write(data / f"AlignResult-{CURRENT}{suffix}", suffix.encode()))
        made["superseded"].append(_write(data / f"AlignResult-{EARLIER}{suffix}"))
        made["foreign"].append(_write(data / f"AlignResult-{SIBLING}{suffix}"))
    made["foreign"].append(_write(data / f"Sibling_DIA_{SIBLING}.dcl"))
    return made


def _csv(path: Path, data: Path, inputs: list[str]) -> Path:
    with path.open("w", encoding="ascii", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["file_path", "file_name", "file_type", "class_id", "acquisition_type"])
        for name in inputs:
            writer.writerow([str(data / f"{name}.mzML"), name, "Sample", "All", "DDA"])
    return path


class RelocationTests(unittest.TestCase):
    INPUTS = ["QC", "QC_2", "Sample_A"]  # "QC" is a prefix of "QC_2": each set goes to its own input

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "unit"
        self.raw = self.root / "raw"
        self.data = self.raw / "data"
        self.output = self.root / "output"
        self.output.mkdir(parents=True)
        self.made = _raw_tree(self.data, self.INPUTS)
        self.csv = _csv(self.output / "analysis_files.csv", self.data, self.INPUTS)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _relocate(self, exported: list[str]) -> dict:
        return relocate_intermediates("job1", self.csv, self.raw, self.output, exported)

    def test_the_finalised_runs_set_moves_into_the_output_with_its_raw_relative_path(self) -> None:
        contents = {path.name: path.read_bytes() for path in self.made["current"]}
        record = self._relocate([f"AlignResult-{CURRENT}.mzTab"])
        destination = self.output / "job1" / INTERMEDIATES_DIRECTORY

        self.assertEqual([], record["errors"])
        self.assertEqual({f"data/{path.name}" for path in self.made["current"]},
                         {item["relative_path"] for item in record["moved"]})
        for path in self.made["current"]:
            with self.subTest(file=path.name):
                self.assertFalse(path.exists())
                self.assertEqual(contents[path.name], (destination / "data" / path.name).read_bytes())
        self.assertEqual({"alignment_timestamp"}, {item["rule"] for item in record["selection"]})
        self.assertEqual(CURRENT, record["alignment_timestamp"])
        by_input = {item["relative_path"]: item["input"] for item in record["moved"] if item["kind"] == "per_file"}
        self.assertEqual("QC", by_input[f"data/QC_{CURRENT}.dcl"])
        self.assertEqual("QC_2", by_input[f"data/QC_2_{CURRENT}.dcl"])
        self.assertFalse(record["project_reopen"]["reopenable_in_place"])

    def test_earlier_attempts_are_recorded_as_superseded_and_another_runs_set_is_left_alone(self) -> None:
        record = self._relocate([f"AlignResult-{CURRENT}.mzTab"])

        self.assertEqual({f"data/{path.name}" for path in self.made["superseded"]},
                         {item["relative_path"] for item in record["superseded"]})
        for path in (*self.made["superseded"], *self.made["foreign"]):
            with self.subTest(file=path.name):
                self.assertTrue(path.exists())
        recorded = {item["relative_path"] for item in (*record["moved"], *record["superseded"])}
        self.assertFalse(recorded & {f"data/{path.name}" for path in self.made["foreign"]})
        for name in self.INPUTS:
            self.assertTrue((self.data / f"{name}.mzML").exists())

    def test_a_moved_container_is_never_read_as_the_runs_mztab(self) -> None:
        from msdial_app.mztab_validation import find_mztab_files

        container = _write(self.output / "job1" / INTERMEDIATES_DIRECTORY / "data" / f"mztab_std_{CURRENT}.dcl")
        result = _write(self.output / f"AlignResult-{CURRENT}.mzTab", b"MTD\tmzTab-version\t2.0.0-M\n")

        self.assertEqual([result.resolve()], find_mztab_files(self.output))
        self.assertTrue(container.exists())

    def test_without_an_mztab_the_newest_set_of_each_input_is_the_run_s(self) -> None:
        old = time.time() - 3600
        for path in self.made["superseded"]:
            os.utime(path, (old, old))
        record = self._relocate([])

        self.assertEqual({"newest"}, {item["rule"] for item in record["selection"]})
        self.assertEqual({f"data/{path.name}" for path in self.made["current"]},
                         {item["relative_path"] for item in record["moved"]})


class LoadedLibraryCopyTests(unittest.TestCase):
    def test_the_serialised_library_copy_is_deleted_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            copy = _write(output / "Project-2609270540_Loaded.msp2.dbs", b"every loaded library")
            loaded = _write(output / "Project-2609270540_Loaded.msp2", b"")
            project = _write(output / "Project-2609270540.mdproject", b"PK")
            deleted, errors = delete_loaded_library_copies(output)

            self.assertEqual([], errors)
            self.assertFalse(copy.exists())
            self.assertTrue(loaded.exists())
            self.assertTrue(project.exists())
        self.assertEqual(
            [{"name": copy.name, "size_bytes": 20, "sha256": hashlib.sha256(b"every loaded library").hexdigest()}],
            [{key: item[key] for key in ("name", "size_bytes", "sha256")} for item in deleted],
        )


def _mztab(raw: Path) -> str:
    return "\r\n".join([
        "MTD\tmzTab-version\t2.0.0-M",
        f"MTD\tms_run[1]-location\tfile://{raw.as_posix()}/data/Sample_A.mzML",
        "MTD\tdatabase[1]-prefix\tMspDB_1_Synthetic-Private-pos",
        "MTD\tdatabase[1]-version\tSynthetic-Private-pos.msp",
        "MTD\tdatabase[1]-uri\tfile://E:/lab libs/Synthetic-Private-pos.msp",
        "MTD\tcustom[1]\t[,, MS-DIAL library statistics database[1], 10 records; 9 compounds; sha256:0011223344556677]",
        "",
        "SMH\tSML_ID",
        "SML\t1",
        "",
    ])


class RunJobTests(unittest.TestCase):
    """One production job, its Console replaced by a fake that writes what the real one writes."""

    INPUTS = ["Sample_A", "QC_01"]

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "unit"
        self.raw = self.root / "raw"
        self.data = self.raw / "data"
        self.output = self.root / "output"
        self.output.mkdir(parents=True)
        (self.root / "provenance").mkdir()
        for name in self.INPUTS:
            _write(self.data / f"{name}.mzML", b"<mzML/>")
        self.manifest = self.root / "provenance" / "run-manifest.json"
        _write_json(self.manifest, {
            "schema": "msdial-public-reanalysis-run.v1",
            "status": "prepared",
            "workspace": str(self.root),
            "raw_directory": str(self.raw),
            "output_directory": str(self.output),
            "raw_retention_policy": "keep",
            "project": {"repository": "metabolights", "accession": "MTBLS-SYN", "analysis_unit_id": "unit-syn"},
        })
        _csv(self.output / "analysis_files.csv", self.data, self.INPUTS)
        (self.output / "workflow-settings.json").write_text(json.dumps({
            "project_type": "lcms",
            "repository_run_manifest": str(self.manifest),
            "output_root": str(self.output),
            "msp_annotators": [{"msp_file_path": PRIVATE}],
            "library_provenance": [{"path": PRIVATE, "license": "institutional/private"}],
            "files": [{"file_path": str(self.data / f"{name}.mzML"), "file_name": name} for name in self.INPUTS],
        }), encoding="utf-8")
        (self.output / "run-manifest.json").write_text(json.dumps({"libraries": [
            {"name": "Synthetic-Private-pos.msp", "filename": "Synthetic-Private-pos.msp", "sha256": "ef" * 32,
             "bytes": 99, "private": True, "distribution": "private"},
        ]}), encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _preparation(self, manifest: bool = True) -> dict:
        return {
            "command": ["MSDIALCUI.exe"],
            "run_directory": str(self.output),
            "export_folder_path": str(self.output),
            "repository_run_manifest": str(self.manifest) if manifest else "",
            "repository_raw_retention_policy": "keep",
            "expected_analysis_exports": [str(self.output / f"{name}.mdpeak") for name in self.INPUTS],
            "qa_matrix_expected": False,
            "input_csv": str(self.output / "analysis_files.csv"),
            "settings_file": str(self.output / "workflow-settings.json"),
            "manifest": str(self.output / "run-manifest.json"),
        }

    def _console(self, exit_code: int = 0):
        def console(_preparation, _log):
            for name in self.INPUTS:
                for suffix in (".dcl", ".pai2", "_tags.xml"):
                    _write(self.data / f"{name}_{CURRENT}{suffix}", suffix.encode())
                _write(self.output / f"{name}.mdpeak", b"Height\n")
            for suffix in ALIGNMENT_SUFFIXES:
                _write(self.data / f"AlignResult-{CURRENT}{suffix}", suffix.encode())
            (self.output / f"AlignResult-{CURRENT}.mzTab").write_bytes(_mztab(self.raw).encode())
            _write(self.output / "Project-2609270540.mdproject", b"project")
            _write(self.output / "Project-2609270540_Loaded.msp2", b"")
            _write(self.output / "Project-2609270540_Loaded.msp2.dbs", b"a copy of the private library")
            return exit_code
        return console

    def _run(self, preparation: dict, exit_code: int = 0) -> dict:
        jobs = {"run1": {"id": "run1", "status": "queued", "kind": "run", "logs": [], "preparation": preparation,
                         "artifact_baseline": {}}}
        with patch.object(server, "JOBS", jobs), patch.object(server, "_persist_jobs_locked", lambda: None), \
                patch.object(server, "run_console", self._console(exit_code)):
            server._run_job("run1", preparation)
        return jobs["run1"]

    def test_a_repository_run_keeps_its_containers_and_no_library_copy(self) -> None:
        job = self._run(self._preparation())
        manifest = read_manifest(self.manifest)
        destination = self.output / "run1" / INTERMEDIATES_DIRECTORY / "data"

        self.assertEqual("completed", job["status"], job.get("error"))
        self.assertEqual("mztab_validated", manifest["status"])
        moved = sorted(path.name for path in destination.iterdir())
        self.assertEqual(sorted([f"{name}_{CURRENT}{suffix}" for name in self.INPUTS
                                 for suffix in (".dcl", ".pai2", "_tags.xml")]
                                + [f"AlignResult-{CURRENT}{suffix}" for suffix in ALIGNMENT_SUFFIXES]), moved)
        self.assertEqual(sorted(f"{name}.mzML" for name in self.INPUTS), sorted(path.name for path in self.data.iterdir()))
        # Retained file by file, with their checksums; not zipped again into the project archive.
        inventory = {Path(item["path"]).name: item for item in manifest["retained_artifact_inventory"]}
        for name in moved:
            with self.subTest(file=name):
                self.assertEqual("msdial_intermediate", inventory[name]["role"])
                self.assertEqual(hashlib.sha256((destination / name).read_bytes()).hexdigest(), inventory[name]["sha256"])
        with zipfile.ZipFile(self.output / "msdial-project-artifacts.zip") as archive:
            self.assertEqual(["Project-2609270540.mdproject"], archive.namelist())
        # The library copy is gone, recorded by name, size and sha256; nothing names it as an artifact.
        self.assertFalse((self.output / "Project-2609270540_Loaded.msp2.dbs").exists())
        finalisation = manifest["console_run_finalisation"]
        self.assertEqual(["Project-2609270540_Loaded.msp2.dbs"],
                         [item["name"] for item in finalisation["loaded_library_copies_deleted"]])
        self.assertNotIn("Project-2609270540_Loaded.msp2.dbs", json.dumps(job["artifacts"]))
        # The mzTab-M that was validated is the redacted one.
        text = (self.output / f"AlignResult-{CURRENT}.mzTab").read_text(encoding="utf-8")
        self.assertIn("MTD\tdatabase[1]-uri\tnull", text)
        self.assertIn("MTD\tms_run[1]-location\traw/data/Sample_A.mzML", text)
        self.assertEqual("local_only", inventory["mztab-redaction.local.json"]["sharing"])
        self.assertEqual("passed", manifest["mztab_validation"]["summary"]["status"])

    def test_a_failed_run_leaves_its_containers_for_the_raw_cleanup_but_not_the_library_copy(self) -> None:
        job = self._run(self._preparation(), exit_code=1)
        manifest = read_manifest(self.manifest)

        self.assertEqual("failed", job["status"])
        self.assertFalse((self.output / "run1").exists())
        self.assertTrue((self.data / f"AlignResult-{CURRENT}.arf2").exists())
        self.assertFalse((self.output / "Project-2609270540_Loaded.msp2.dbs").exists())
        self.assertNotIn("msdial_intermediates", manifest["console_run_finalisation"])

    def test_a_laboratory_run_keeps_its_project_and_loses_only_the_private_library_location(self) -> None:
        settings = self.output / "workflow-settings.json"
        state = json.loads(settings.read_text(encoding="utf-8"))
        state.pop("repository_run_manifest")
        settings.write_text(json.dumps(state), encoding="utf-8")
        job = self._run(self._preparation(manifest=False))

        self.assertEqual("completed", job["status"], job.get("error"))
        self.assertTrue((self.output / "Project-2609270540_Loaded.msp2.dbs").exists())
        self.assertTrue((self.data / f"AlignResult-{CURRENT}.arf2").exists())
        text = (self.output / f"AlignResult-{CURRENT}.mzTab").read_text(encoding="utf-8")
        self.assertIn("MTD\tdatabase[1]-uri\tnull", text)
        self.assertIn(f"file://{self.raw.as_posix()}/data/Sample_A.mzML", text)
        self.assertEqual("laboratory", job["console_run_finalisation"]["scope"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

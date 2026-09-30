"""What a finished Console run leaves behind is put where it belongs before validation and cleanup.

MS-DIAL writes its per-file and alignment containers beside the files it reads, so deleting a unit's raw data
deleted them; every earlier attempt left a set of its own under a minute-resolution name; and the output held
<project>_Loaded.msp2.dbs, a serialised copy of every library the run loaded - with the private VS21 pair, a
copy of the private library in every unit. Both were decided for the campaign; a unit no campaign approval
covers keeps the project MS-DIAL can reopen.

On Windows a file another process holds open cannot be replaced or deleted, and a container's path below the
output can pass MAX_PATH. What a run could not do is held in the unit manifest, and the raw cleanup refuses
while a container is still beside the inputs.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from msdial_app import run_finalisation, server
from msdial_app.diagnostic_paths import extended_path, intermediate_files, path_is_file
from msdial_app.repository_reanalysis import (
    _write_json,
    cleanup_download_lease,
    discard_download_lease,
    finalize_download_lease,
    plan_download_cleanup,
    read_manifest,
)
from msdial_app.run_finalisation import (
    HOLDS,
    FinalisationHeld,
    INTERMEDIATES_DIRECTORY,
    campaign_approval_recorded,
    delete_loaded_library_copies,
    relocate_intermediates,
    resolve_finalisation_holds,
)

CURRENT = "20269271740"
EARLIER = "20269261728"
DIAGNOSTIC = "2026925025"
SIBLING = "20269271814"
ALIGNMENT_SUFFIXES = (".arf2", ".dcl", ".EIC.aef", "_PeakProperties.arf", "_DriftSopts.arf", "_tags.xml")
PRIVATE = "E:\\lab libs\\Synthetic-Private-pos.msp"  # synthetic; nothing is read from it
CROSSING = {
    "schema": "msdial-campaign-authorization.v1", "approval_id": "approval-syn", "campaign_id": "campaign-syn",
    "manifest_digest": "sha256:" + "0" * 64, "raw_retention_policy": "delete_after_validated_output",
    "boundary": 4, "unit_id": "unit-syn", "covered_as": "listed", "entry_point": "agent_run", "job_id": "run1",
}
WINDOWS = os.name == "nt"
# A file held open by another process, as a viewer, the indexer or antivirus holds one. Python opens a file
# without FILE_SHARE_DELETE, so on Windows it can then be neither replaced nor deleted.
HELD = "Windows refuses to replace or delete a file another handle holds open; POSIX does not"


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _quick(budget: float = 0.3):
    """Retries short enough for a test: a held file is given up on within ``budget`` seconds."""
    return patch.multiple(run_finalisation, RETRY_DELAYS_SECONDS=(0.05,) * 400, RETRY_BUDGET_SECONDS=budget)


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

    def _relocate(self, exported: list[str], **options) -> dict:
        return relocate_intermediates("job1", self.csv, self.raw, self.output, exported, **options)

    def test_the_finalised_runs_set_moves_into_the_output_with_its_raw_relative_path(self) -> None:
        contents = {path.name: path.read_bytes() for path in self.made["current"]}
        record = self._relocate([f"AlignResult-{CURRENT}.mzTab"])
        # No job id in the path, which is already longer than the one the Console wrote; the record names it.
        destination = self.output / INTERMEDIATES_DIRECTORY

        self.assertEqual([], record["errors"])
        self.assertEqual([], record["pending"])
        self.assertEqual("job1", record["job_id"])
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

        container = _write(self.output / INTERMEDIATES_DIRECTORY / "data" / f"mztab_std_{CURRENT}.dcl")
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

    def test_a_retry_pinned_to_the_first_choice_moves_what_is_left_and_nothing_older(self) -> None:
        old = time.time() - 3600
        for path in self.made["superseded"]:
            os.utime(path, (old, old))
        left = self.data / f"Sample_A_{CURRENT}.pai2"
        replace = os.replace

        def refuse_one(source, target):
            if Path(source).name == left.name:
                raise OSError(28, "No space left on device")
            return replace(source, target)

        with patch.object(run_finalisation.os, "replace", refuse_one), \
                patch.object(run_finalisation.shutil, "copy2", side_effect=OSError(28, "No space left")):
            first = self._relocate([])
        self.assertEqual([f"data/{left.name}"], first["pending"])
        self.assertTrue(left.exists())

        # Every other file of this run's set has left the raw tree, so the newest set left of those inputs is
        # an earlier attempt's. The retry must not take it for the run's.
        retry = self._relocate([], pinned=run_finalisation._pinned(first))

        self.assertEqual([], retry["errors"])
        self.assertEqual([f"data/{left.name}"], [item["relative_path"] for item in retry["moved"]])
        self.assertEqual({"pinned"}, {item["rule"] for item in retry["selection"]})
        for path in self.made["superseded"]:
            with self.subTest(file=path.name):
                self.assertTrue(path.exists())

    @unittest.skipUnless(WINDOWS, HELD)
    def test_a_container_held_by_another_process_is_moved_once_it_is_let_go(self) -> None:
        held = self.data / f"QC_{CURRENT}.dcl"
        handle = held.open("rb")
        timer = threading.Timer(0.3, handle.close)
        timer.start()
        try:
            with _quick(budget=10.0):
                record = self._relocate([f"AlignResult-{CURRENT}.mzTab"])
        finally:
            timer.join()

        self.assertEqual([], record["errors"])
        self.assertFalse(held.exists())
        self.assertTrue((self.output / INTERMEDIATES_DIRECTORY / "data" / held.name).exists())

    @unittest.skipUnless(WINDOWS, HELD)
    def test_a_container_held_past_the_budget_is_left_pending_in_place(self) -> None:
        held = self.data / f"QC_{CURRENT}.dcl"
        with held.open("rb"), _quick():
            record = self._relocate([f"AlignResult-{CURRENT}.mzTab"])

        self.assertEqual([f"data/{held.name}"], record["pending"])
        self.assertTrue(held.exists())
        self.assertEqual(len(self.made["current"]) - 1, len(record["moved"]))


class LongPathTests(unittest.TestCase):
    """A container the Console could write (under MAX_PATH) whose destination is longer.

    LongPathsEnabled is 0 on the campaign host: without the extended-length form every move failed with
    WinError 3, and the containers were then deleted with the raw tree.
    """

    TS = "20269301200"

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(self.directory.name).resolve()
        self.workspace = base / "u"
        self.raw = self.workspace / "raw"
        self.data = self.raw / "data"
        self.output = self.workspace / "output"
        self.provenance = self.workspace / "provenance"
        for directory in (self.data, self.output, self.provenance):
            directory.mkdir(parents=True)
        # The longest container is 250 characters: the Console can write it, and it is longer below the output.
        budget = 250 - len(str(self.data / "x")) - len(f"_{self.TS}_tags.xml") + 1
        if budget < 10:
            self.skipTest("the temporary directory is too deep to build a path of this length")
        self.name = "S" * budget
        _write(self.data / f"{self.name}.mzML", b"<mzML/>")
        self.containers = [_write(self.data / f"{self.name}_{self.TS}{suffix}", suffix.encode())
                           for suffix in (".dcl", ".pai2", "_tags.xml")]
        self.containers += [_write(self.data / f"AlignResult-{self.TS}{suffix}", suffix.encode())
                            for suffix in (".arf2", ".dcl", ".EIC.aef")]
        self.csv = self.output / "analysis_files.csv"
        with self.csv.open("w", encoding="ascii", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["file_path", "file_name", "file_type", "class_id", "acquisition_type"])
            writer.writerow([str(self.data / f"{self.name}.mzML"), self.name, "Sample", "All", "DDA"])

    def tearDown(self) -> None:
        # A plain rmtree cannot remove a path longer than MAX_PATH; the extended form can.
        shutil.rmtree(extended_path(Path(self.directory.name).resolve(), always=True), ignore_errors=True)
        self.directory.cleanup()

    def test_a_destination_longer_than_max_path_is_moved_retained_and_inventoried(self) -> None:
        destination = self.output / INTERMEDIATES_DIRECTORY / "data" / self.containers[2].name
        self.assertLess(max(len(str(path)) for path in self.containers), 260)
        self.assertGreaterEqual(len(str(destination)), 260)

        record = relocate_intermediates("0" * 32, self.csv, self.raw, self.output, [f"AlignResult-{self.TS}.mzTab"])

        self.assertEqual([], record["errors"])
        self.assertEqual(6, len(record["moved"]))
        for path in self.containers:
            with self.subTest(file=path.name[-24:]):
                self.assertFalse(path.exists())
        self.assertTrue(path_is_file(destination))
        self.assertIn(destination, intermediate_files(self.output))

        # Retained and inventoried with its checksum, and not reported missing by the cleanup plan.
        mztab = _write(self.output / f"AlignResult-{self.TS}.mzTab",
                       b"MTD\tmzTab-version\t2.0.0-M\r\nMTD\tmzTab-ID\tx\r\n\r\nSMH\tSML_ID\r\nSML\t1\r\n")
        manifest = self.provenance / "run-manifest.json"
        _write_json(manifest, {"status": "prepared", "workspace": str(self.workspace),
                               "raw_directory": str(self.raw), "output_directory": str(self.output)})
        with patch("msdial_app.mztab_validation.validate_mztab_outputs",
                   return_value={"summary": {"failed": 0}, "files": [{"file": str(mztab)}]}):
            finalize_download_lease(manifest)
        inventory = {item["path"]: item for item in read_manifest(manifest)["retained_artifact_inventory"]}
        self.assertEqual("msdial_intermediate", inventory[str(destination)]["role"])
        self.assertEqual(hashlib.sha256(b"_tags.xml").hexdigest(), inventory[str(destination)]["sha256"])
        self.assertEqual([], plan_download_cleanup(manifest)["missing_retained_artifacts"])


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
    DBS = "Project-2609270540_Loaded.msp2.dbs"

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
        self._unit_manifest(campaign=True)
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
        self.handles: list = []

    def tearDown(self) -> None:
        self._let_go()
        self.directory.cleanup()

    def _unit_manifest(self, campaign: bool) -> None:
        _write_json(self.manifest, {
            "schema": "msdial-public-reanalysis-run.v1",
            "status": "prepared",
            "workspace": str(self.root),
            "raw_directory": str(self.raw),
            "output_directory": str(self.output),
            "raw_retention_policy": "delete_after_validated_output" if campaign else "keep",
            "project": {"repository": "metabolights", "accession": "MTBLS-SYN", "analysis_unit_id": "unit-syn"},
            # A campaign run records the approval it crossed boundary 4 under before it starts.
            **({"campaign_authorizations": [CROSSING]} if campaign else {}),
        })

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

    def _console(self, exit_code: int = 0, hold: tuple[str, ...] = ()):
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
            _write(self.output / self.DBS, b"a copy of the private library")
            # Another process opens what the Console has just written, and keeps it open.
            self.handles.extend((self.root / relative).open("rb") for relative in hold)
            return exit_code
        return console

    def _run(self, preparation: dict, exit_code: int = 0, hold: tuple[str, ...] = ()) -> dict:
        jobs = {"run1": {"id": "run1", "status": "queued", "kind": "run", "logs": [], "preparation": preparation,
                         "artifact_baseline": {}}}
        with patch.object(server, "JOBS", jobs), patch.object(server, "_persist_jobs_locked", lambda: None), \
                patch.object(server, "run_console", self._console(exit_code, hold)), _quick():
            server._run_job("run1", preparation)
        return jobs["run1"]

    def _let_go(self) -> None:
        for handle in self.handles:
            handle.close()
        self.handles.clear()

    def test_a_campaign_run_keeps_its_containers_and_no_library_copy(self) -> None:
        job = self._run(self._preparation())
        manifest = read_manifest(self.manifest)
        destination = self.output / INTERMEDIATES_DIRECTORY / "data"

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
        self.assertFalse((self.output / self.DBS).exists())
        finalisation = manifest["console_run_finalisation"]
        self.assertTrue(finalisation["campaign_approval_recorded"])
        self.assertEqual([self.DBS], [item["name"] for item in finalisation["loaded_library_copies_deleted"]])
        self.assertNotIn(self.DBS, json.dumps(job["artifacts"]))
        self.assertEqual([], manifest[HOLDS])
        # The mzTab-M that was validated is the redacted one.
        text = (self.output / f"AlignResult-{CURRENT}.mzTab").read_text(encoding="utf-8")
        self.assertIn("MTD\tdatabase[1]-uri\tnull", text)
        self.assertIn("MTD\tms_run[1]-location\traw/data/Sample_A.mzML", text)
        self.assertEqual("local_only", inventory["mztab-redaction.local.json"]["sharing"])
        self.assertEqual("passed", manifest["mztab_validation"]["summary"]["status"])

    def test_a_trial_repository_run_without_a_campaign_approval_keeps_the_project_msdial_reopens(self) -> None:
        # The raw data are kept, so the project stays openable: its containers beside the inputs the .mddata
        # names, and the library copy its loader reads. Only the privacy redaction applies.
        self._unit_manifest(campaign=False)
        job = self._run(self._preparation())
        manifest = read_manifest(self.manifest)

        self.assertEqual("completed", job["status"], job.get("error"))
        self.assertFalse((self.output / INTERMEDIATES_DIRECTORY).exists())
        for name in self.INPUTS:
            for suffix in (".dcl", ".pai2", "_tags.xml"):
                self.assertTrue((self.data / f"{name}_{CURRENT}{suffix}").exists())
        self.assertTrue((self.data / f"AlignResult-{CURRENT}.arf2").exists())
        self.assertTrue((self.output / self.DBS).exists())
        finalisation = manifest["console_run_finalisation"]
        self.assertFalse(finalisation["campaign_approval_recorded"])
        self.assertNotIn("msdial_intermediates", finalisation)
        self.assertNotIn("loaded_library_copies_deleted", finalisation)
        self.assertEqual(["msdial_intermediates", "loaded_library_copy"], finalisation["left_as_before"]["steps"])
        text = (self.output / f"AlignResult-{CURRENT}.mzTab").read_text(encoding="utf-8")
        self.assertIn("MTD\tdatabase[1]-uri\tnull", text)

    def test_a_failed_campaign_run_leaves_its_containers_for_the_raw_cleanup_but_not_the_library_copy(self) -> None:
        job = self._run(self._preparation(), exit_code=1)
        manifest = read_manifest(self.manifest)

        self.assertEqual("failed", job["status"])
        self.assertFalse((self.output / INTERMEDIATES_DIRECTORY).exists())
        self.assertTrue((self.data / f"AlignResult-{CURRENT}.arf2").exists())
        self.assertFalse((self.output / self.DBS).exists())
        self.assertNotIn("msdial_intermediates", manifest["console_run_finalisation"])

    def test_a_laboratory_run_keeps_its_project_and_loses_only_the_private_library_location(self) -> None:
        settings = self.output / "workflow-settings.json"
        state = json.loads(settings.read_text(encoding="utf-8"))
        state.pop("repository_run_manifest")
        settings.write_text(json.dumps(state), encoding="utf-8")
        job = self._run(self._preparation(manifest=False))

        self.assertEqual("completed", job["status"], job.get("error"))
        self.assertTrue((self.output / self.DBS).exists())
        self.assertTrue((self.data / f"AlignResult-{CURRENT}.arf2").exists())
        text = (self.output / f"AlignResult-{CURRENT}.mzTab").read_text(encoding="utf-8")
        self.assertIn("MTD\tdatabase[1]-uri\tnull", text)
        self.assertIn(f"file://{self.raw.as_posix()}/data/Sample_A.mzML", text)
        self.assertEqual("laboratory", job["console_run_finalisation"]["scope"])

    def test_a_defect_in_finalisation_holds_the_unit_for_a_person_and_the_run_is_still_validated(self) -> None:
        with patch.object(run_finalisation, "relocate_intermediates", side_effect=RuntimeError("synthetic defect")):
            job = self._run(self._preparation())
        manifest = read_manifest(self.manifest)

        self.assertEqual("completed", job["status"], job.get("error"))
        self.assertEqual("mztab_validated", manifest["status"])
        [hold] = manifest[HOLDS]
        self.assertEqual((["sharing", "raw_deletion"], "finalisation"), (hold["blocks"], hold["step"]))
        self.assertIn("synthetic defect", hold["reason"])
        # Nothing says what is left to do, so no retry clears it.
        self.assertEqual([hold["id"]], [item["id"] for item in resolve_finalisation_holds(self.manifest)])
        self.assertFalse(cleanup_download_lease(self.manifest, confirmed=False)["ready_for_confirmation"])

    @unittest.skipUnless(WINDOWS, HELD)
    def test_a_container_held_past_the_budget_holds_the_raw_deletion_until_a_retry_moves_it(self) -> None:
        held = f"raw/data/Sample_A_{CURRENT}.pai2"
        job = self._run(self._preparation(), hold=(held,))
        manifest = read_manifest(self.manifest)

        self.assertEqual("completed", job["status"], job.get("error"))
        self.assertEqual("mztab_validated", manifest["status"])
        [hold] = manifest[HOLDS]
        self.assertEqual((["raw_deletion"], "msdial_intermediates", "run1"),
                         (hold["blocks"], hold["step"], hold["job_id"]))
        self.assertEqual([f"data/Sample_A_{CURRENT}.pai2"], hold["pending"])
        self.assertTrue(any("msdial_intermediates" in warning for warning in job["warnings"]))
        plan = plan_download_cleanup(self.manifest)
        self.assertFalse(plan["ready_for_confirmation"])
        self.assertTrue(any("MS-DIAL containers are still in the raw directory" in item for item in plan["blockers"]))

        # Still held: the confirmed cleanup retries the move first, then refuses, and deletes nothing.
        refused = r"^finalisation_held \[raw_deletion\]: .*raw cleanup was refused"
        with _quick(), self.assertRaisesRegex(FinalisationHeld, refused):
            cleanup_download_lease(self.manifest, confirmed=True)
        self.assertTrue((self.root / held).exists())

        # Let go: the preview retries and moves it, and the retained inventory lists it before anything goes.
        self._let_go()
        preview = cleanup_download_lease(self.manifest, confirmed=False)
        self.assertEqual([], preview["blockers"])
        self.assertTrue(preview["ready_for_confirmation"])
        moved = self.output / INTERMEDIATES_DIRECTORY / "data" / f"Sample_A_{CURRENT}.pai2"
        self.assertIn(str(moved.resolve()), read_manifest(self.manifest)["retained_artifacts"])
        cleanup_download_lease(self.manifest, confirmed=True)
        self.assertFalse(self.raw.exists())
        self.assertTrue(moved.exists())
        manifest = read_manifest(self.manifest)
        self.assertEqual([], manifest[HOLDS])
        self.assertEqual(["msdial_intermediates"], [item["step"] for item in manifest["finalisation_hold_resolutions"]])

    @unittest.skipUnless(WINDOWS, HELD)
    def test_a_held_library_copy_is_held_from_sharing_and_deleted_once_let_go(self) -> None:
        job = self._run(self._preparation(), hold=(f"output/{self.DBS}",))
        manifest = read_manifest(self.manifest)

        self.assertEqual("completed", job["status"], job.get("error"))
        [hold] = manifest[HOLDS]
        self.assertEqual((["sharing"], "loaded_library_copy"), (hold["blocks"], hold["step"]))
        self.assertTrue((self.output / self.DBS).exists())
        # It holds sharing, not the raw deletion: every container left the raw tree.
        self.assertTrue(plan_download_cleanup(self.manifest)["ready_for_confirmation"])

        self._let_go()
        self.assertEqual([], resolve_finalisation_holds(self.manifest))
        self.assertFalse((self.output / self.DBS).exists())
        [resolution] = read_manifest(self.manifest)["finalisation_hold_resolutions"]
        self.assertEqual([self.DBS], [item["name"] for item in resolution["deleted"]])


class HoldTests(unittest.TestCase):
    """What refuses a standing hold, whichever manifest it is recorded in."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _unit(self, name: str, **fields) -> Path:
        workspace = self.root / name
        _write(workspace / "raw" / "data" / "a.mzML", b"<mzML/>")
        (workspace / "output").mkdir(parents=True)
        manifest = workspace / "provenance" / "run-manifest.json"
        manifest.parent.mkdir(parents=True)
        _write_json(manifest, {"workspace": str(workspace), "raw_directory": str(workspace / "raw"),
                               "output_directory": str(workspace / "output"), **fields})
        return manifest

    def test_a_part_that_could_not_move_its_containers_holds_its_parents_raw_deletion(self) -> None:
        hold = run_finalisation._hold(["raw_deletion"], "msdial_intermediates", "job-part", "one container left")
        part = self._unit("part", **{HOLDS: [hold]})
        parent = self._unit("parent", status="mztab_validated", cleanup_allowed=True,
                            retained_artifacts=[str(part)], split_into=[{"manifest_path": str(part)}])

        plan = plan_download_cleanup(parent)
        self.assertFalse(plan["ready_for_confirmation"])
        self.assertEqual([str(part.resolve())], [item["manifest_path"] for item in plan["finalisation_holds"]])

    def test_a_hold_finalisation_could_not_describe_is_never_cleared_by_a_retry(self) -> None:
        hold = run_finalisation._hold(["sharing", "raw_deletion"], "finalisation", "job1", "finalisation stopped")
        manifest = self._unit("unit", status="raw_metadata_rejected", **{HOLDS: [hold]})

        self.assertEqual([hold["id"]], [item["id"] for item in resolve_finalisation_holds(manifest)])
        with self.assertRaisesRegex(FinalisationHeld, "discard was refused"):
            discard_download_lease(manifest, confirmed=True)
        self.assertTrue((self.root / "unit" / "raw" / "data" / "a.mzML").exists())

    def test_a_campaign_approval_recorded_on_the_unit_it_was_split_from_covers_a_part(self) -> None:
        parent = self._unit("parent", campaign_authorizations=[CROSSING])

        self.assertTrue(campaign_approval_recorded(read_manifest(parent)))
        self.assertTrue(campaign_approval_recorded({"split_from": {"manifest_path": str(parent)}}))
        self.assertFalse(campaign_approval_recorded({"split_from": {"manifest_path": str(self.root / "none.json")}}))
        self.assertFalse(campaign_approval_recorded({}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

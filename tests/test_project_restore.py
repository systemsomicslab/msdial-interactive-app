"""A finalised campaign unit is restored for viewing in MS-DIAL, held while it stands, and taken back exactly.

Finalisation moves a campaign unit's containers into output/msdial-intermediates and its loaded-library copy
into the library-copy store, so the project as it stands cannot be opened. A restore copies the containers back
to the paths the .mddata names - with the raw files there or released - and links the store object into the
output; a viewing_restore hold keeps the unit from being shared or its raw data deleted until the undo, which
removes exactly what the restore's record lists and reports, never deletes, anything else.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app import library_copy_store, project_restore, repository_reanalysis
from msdial_app.library_copy_store import LibraryCopyStore, LibraryCopyStoreError, read_copy_identity
from msdial_app.materials_methods import generate_publication_report
from msdial_app.process_liveness import process_created_at
from msdial_app.project_restore import RestoreRefused, plan_project_restore, restore_project_for_viewing, undo_project_restore
from msdial_app.repository_reanalysis import (
    _write_json,
    plan_download_cleanup,
    read_manifest,
    refresh_retained_artifacts,
    start_new_production_run,
    update_manifest,
)
from msdial_app.run_finalisation import (
    FinalisationHeld,
    _hold,
    _library_copies,
    resolve_finalisation_holds,
    restore_links,
    store_loaded_library_copies,
)
from test_library_copy_store import LIBRARIES, assert_names_no_root, synthetic_dbs

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import server

WINDOWS = os.name == "nt"
TS = "20261091730"
PROJECT = "Project-2610090530"
DBS = f"{PROJECT}_Loaded.msp2.dbs"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Finalised(unittest.TestCase):
    """A campaign unit as finalisation leaves it: containers retained in the output, its copy in the store."""

    INPUTS = ("Sample_A", "QC_01")

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.workspace = self.root / "metabolights" / "MTBLS-SYN" / "unit-syn"
        self.raw = self.workspace / "raw"
        self.data = self.raw / "data"
        self.output = self.workspace / "output"
        self.retained = self.output / "msdial-intermediates"
        self.manifest = self.workspace / "provenance" / "run-manifest.json"
        self.data.mkdir(parents=True)
        self.output.mkdir(parents=True)
        for name in self.INPUTS:
            (self.data / f"{name}.mzML").write_bytes(b"<mzML/>")
        with (self.output / "analysis_files.csv").open("w", encoding="ascii", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["file_path", "file_name"])
            for name in self.INPUTS:
                writer.writerow([str(self.data / f"{name}.mzML"), name])
        self.relatives = [f"data/{name}_{TS}{suffix}" for name in self.INPUTS for suffix in (".dcl", ".pai2", "_tags.xml")]
        self.relatives += [f"data/AlignResult-{TS}{suffix}" for suffix in (".arf2", ".dcl", ".EIC.aef", "_PeakProperties.arf")]
        moved, inventory = [], []
        for relative in self.relatives:
            path = self.retained / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"container {relative}".encode())
            moved.append({"relative_path": relative, "path": str(path), "kind": "per_file", "size_bytes": path.stat().st_size,
                          "method": "rename"})
            inventory.append({"path": str(path.resolve()), "sha256": _sha(path), "role": "msdial_intermediate"})
        (self.output / f"{PROJECT}.mdproject").write_bytes(b"PK project")
        (self.output / f"{PROJECT}.mddata").write_bytes(b"\xc7 mddata")
        (self.output / f"{PROJECT}_Loaded.msp2").write_bytes(b"")
        copy = synthetic_dbs(self.output / DBS)
        self.store = LibraryCopyStore(self.root)
        stored = self.store.put(copy, read_copy_identity(copy), unit=None, job_id="run1", libraries=LIBRARIES)
        self.object_id = stored["object_id"]
        _write_json(self.manifest, {
            "schema": "msdial-public-reanalysis-run.v1",
            "status": "mztab_validated",
            "workspace": str(self.workspace),
            "raw_directory": str(self.raw),
            "output_directory": str(self.output),
            "raw_retention_policy": "delete_after_validated_output",
            "cleanup_allowed": True,
            "finalized_at": "2026-10-09T08:00:00+00:00",
            "finalized_run": {"job_id": "run1", "run_directory": str(self.output)},
            "project": {"repository": "metabolights", "accession": "MTBLS-SYN", "analysis_unit_id": "unit-syn"},
            "retained_artifacts": [item["path"] for item in inventory],
            "retained_artifact_inventory": inventory,
            "finalisation_holds": [],
            "console_run_finalisation": {
                "schema": "msdial-console-run-finalisation.v1", "job_id": "run1", "campaign_approval_recorded": True,
                "libraries": LIBRARIES,
                "msdial_intermediates": {"raw_directory": str(self.raw.resolve()), "moved": moved, "pending": [],
                                         "errors": []},
                "loaded_library_copy": {"schema": "msdial-loaded-library-copy.v1", "store": "_library_copies",
                                        "copies": [stored], "errors": []},
            },
        })

    def tearDown(self) -> None:
        self.directory.cleanup()

    def restore(self, **options) -> dict:
        return restore_project_for_viewing(self.manifest, confirmed=True, **options)

    def undo(self, **options) -> dict:
        return undo_project_restore(self.manifest, confirmed=True, **options)

    def change(self, **fields) -> None:
        def apply(current: dict) -> None:
            current.update(fields)

        update_manifest(self.manifest, apply)

    def tree(self) -> dict[str, str]:
        """Every file of the unit but its manifest, which records the restore, and the lock files beside it
        (the manifest's writer lock and the raw-deletion lock a restore takes), which are never removed."""
        found = {}
        for directory, _folders, names in os.walk(self.workspace):
            for name in names:
                path = Path(directory) / name
                if path == self.manifest or (path.parent == self.manifest.parent and name.endswith(".lock")):
                    continue
                found[path.relative_to(self.workspace).as_posix()] = _sha(path)
        return found


class TheRestore(_Finalised):
    def test_a_preview_writes_nothing_and_names_paths_relative_to_the_workspace_root(self) -> None:
        before, tree = self.manifest.read_bytes(), self.tree()
        preview = plan_project_restore(self.manifest)
        tool = restore_project_for_viewing(self.manifest)

        self.assertEqual((before, tree), (self.manifest.read_bytes(), self.tree()))
        self.assertFalse((self.root / "_viewing").exists())
        self.assertFalse(tool["restored"])
        self.assertEqual((len(self.relatives), True), (preview["containers"], preview["raw_files_present"]))
        self.assertIn(f"metabolights/MTBLS-SYN/unit-syn/raw/data/Sample_A_{TS}.dcl", preview["targets"])
        self.assertEqual(f"_library_copies/o/{self.object_id}/library-copy.dbs", preview["library_copy"]["object"])
        self.assertEqual(f"metabolights/MTBLS-SYN/unit-syn/output/{DBS}", preview["library_copy"]["link"])
        self.assertEqual(["sharing", "raw_deletion"], preview["hold"]["blocks"])
        self.assertNotIn(str(self.root), json.dumps(tool))
        self.assertNotIn(str(self.root).replace("\\", "\\\\"), json.dumps(tool))

    def test_a_restore_puts_the_project_back_holds_the_unit_and_its_undo_takes_exactly_that_away(self) -> None:
        tree = self.tree()
        done = self.restore()

        self.assertTrue(done["restored"], done)
        for relative in self.relatives:
            with self.subTest(file=relative):
                self.assertEqual((self.retained / relative).read_bytes(), (self.raw / relative).read_bytes())
                self.assertTrue((self.retained / relative).exists(), "copied, never moved")
        link = self.output / DBS
        stored = self.store.object_path(self.object_id)
        self.assertEqual(os.stat(stored).st_ino, os.stat(link).st_ino, "a hard link to the store object")
        self.assertEqual(2, os.stat(stored).st_nlink)
        manifest = read_manifest(self.manifest)
        [hold] = manifest["finalisation_holds"]
        self.assertEqual(("viewing_restore", ["sharing", "raw_deletion"], "run1"),
                         (hold["step"], hold["blocks"], hold["job_id"]))
        [entry] = manifest["viewing_restores"]
        self.assertEqual(("restored", done["restore_id"], "link"), (entry["state"], entry["restore_id"], entry["library_method"]))
        record = json.loads((self.root / entry["record"]).read_text(encoding="utf-8"))
        self.assertEqual(len(self.relatives), len(record["created_files"]))
        self.assertEqual({f"{PROJECT}.mdproject", f"{PROJECT}.mddata", f"{PROJECT}_Loaded.msp2"},
                         {item["name"] for item in record["backups"]})
        self.assertEqual("metabolights/MTBLS-SYN/unit-syn/output", record["output"])
        assert_names_no_root(self, self.root, json.dumps(record))

        # While it stands: nothing is shared, nothing deleted, nothing re-inventoried, and no retry lifts it.
        with self.assertRaises(FinalisationHeld):
            generate_publication_report({"repository_run_manifest": str(self.manifest)}, None, self.output,
                                        app_version="test", console_version="test")
        self.assertFalse(plan_download_cleanup(self.manifest)["ready_for_confirmation"])
        self.assertEqual("viewing_restore_active", refresh_retained_artifacts(self.manifest)["reason"])
        self.assertEqual([hold["id"]], [item["id"] for item in resolve_finalisation_holds(self.manifest)])
        self.assertTrue(link.exists())
        with self.assertRaisesRegex(RestoreRefused, "already active"):
            plan_project_restore(self.manifest)

        preview = undo_project_restore(self.manifest)
        self.assertEqual((len(self.relatives), "remove"), (preview["files_to_remove"], preview["library_copy"]["action"]))
        self.assertTrue(link.exists())
        undone = self.undo()

        self.assertTrue(undone["undone"], undone)
        self.assertEqual(tree, self.tree(), "the unit is as it was")
        self.assertEqual(1, os.stat(stored).st_nlink)
        manifest = read_manifest(self.manifest)
        self.assertEqual([], manifest["finalisation_holds"])
        self.assertEqual("undone", manifest["viewing_restores"][0]["state"])
        self.assertNotIn("finalisation_hold_resolutions", manifest)
        self.assertTrue(plan_download_cleanup(self.manifest)["ready_for_confirmation"])
        with self.assertRaisesRegex(RestoreRefused, "no viewing restore stands"):
            undo_project_restore(self.manifest)

    def test_with_the_raw_data_released_it_makes_the_directories_and_takes_them_away_again(self) -> None:
        shutil.rmtree(self.raw)
        preview = plan_project_restore(self.manifest)
        self.assertFalse(preview["raw_files_present"])
        self.assertEqual(["metabolights/MTBLS-SYN/unit-syn/raw", "metabolights/MTBLS-SYN/unit-syn/raw/data"],
                         preview["directories_to_create"])
        self.assertIn("opens a project without them", restore_project_for_viewing(self.manifest)["message"])

        done = self.restore()
        self.assertTrue(done["restored"])
        self.assertTrue((self.raw / self.relatives[0]).is_file())
        self.undo()
        self.assertFalse(self.raw.exists(), "no released raw tree is left behind")

    @unittest.skipUnless(WINDOWS, "ERROR_NOT_SAME_DEVICE is a Windows error number")
    def test_on_another_volume_the_library_copy_is_copied_and_verified(self) -> None:
        with patch.object(project_restore.os, "link", side_effect=OSError(None, "not the same device", None, 17)):
            done = self.restore()
        self.assertEqual("copy", done["library_method"])
        link = self.output / DBS
        self.assertEqual(1, os.stat(link).st_nlink)
        self.assertEqual(self.store.entry(self.object_id)["sha256"], _sha(link))
        self.assertTrue(self.undo()["undone"])
        self.assertFalse(link.exists())


class TheRefusals(_Finalised):
    def refused(self, pattern: str) -> None:
        before = self.manifest.read_bytes()
        with self.assertRaisesRegex(RestoreRefused, pattern):
            restore_project_for_viewing(self.manifest, confirmed=True)
        self.assertEqual(before, self.manifest.read_bytes())
        self.assertFalse((self.root / "_viewing").exists())
        self.assertFalse((self.output / DBS).exists())

    def test_no_finalisation_no_campaign_or_nothing_moved(self) -> None:
        finalisation = read_manifest(self.manifest)["console_run_finalisation"]
        self.change(console_run_finalisation={**finalisation, "campaign_approval_recorded": False})
        self.refused("no campaign approval")
        self.change(console_run_finalisation={**finalisation, "msdial_intermediates": {"moved": [], "pending": []}})
        self.refused("moved no MS-DIAL containers")
        self.change(console_run_finalisation=None)
        self.refused("no run finalisation")

    def test_another_run_than_the_finalised_one(self) -> None:
        with self.assertRaisesRegex(RestoreRefused, "only the current run, run1"):
            restore_project_for_viewing(self.manifest, job_id="run0", confirmed=True)

    def test_no_store_object_for_the_run(self) -> None:
        finalisation = read_manifest(self.manifest)["console_run_finalisation"]
        legacy = {key: value for key, value in finalisation.items() if key != "loaded_library_copy"}
        self.change(console_run_finalisation={**legacy, "loaded_library_copies_deleted": [{"name": DBS}]})
        self.refused("library_copy_store attach")
        shutil.rmtree(self.store.object_directory(self.object_id))
        self.change(console_run_finalisation=finalisation)
        self.refused("does not match its record")

    def test_a_copy_a_retry_deleted_before_the_store_existed_asks_for_an_attach(self) -> None:
        finalisation = read_manifest(self.manifest)["console_run_finalisation"]
        legacy = {key: value for key, value in finalisation.items() if key != "loaded_library_copy"}
        # Up to 0.5.36 a copy held at finalisation was recorded there as none, and the retry that deleted it named
        # it only in its hold's resolution.
        self.change(console_run_finalisation={**legacy, "loaded_library_copies_deleted": []},
                    finalisation_hold_resolutions=[{"hold": "h1", "step": "loaded_library_copy", "job_id": "run1",
                                                    "deleted": [{"name": DBS, "size_bytes": 1, "sha256": "12" * 32}]}])
        self.refused("library_copy_store attach")

    def test_any_hold_or_a_live_run_or_a_deletion_under_way(self) -> None:
        hold = _hold(["raw_deletion"], "msdial_intermediates", "run1", "one container left")
        self.change(finalisation_holds=[hold])
        self.refused("a retry of any of them would undo a restore")
        self.change(finalisation_holds=[], run_attempts=[{"job_id": "run2", "kind": "run", "ended_at": None,
                                                          "console_pid": os.getpid(),
                                                          "console_process_created_at": process_created_at()}])
        self.refused("a run of this unit is live")
        self.change(run_attempts=[], raw_deletion={"state": "deleting"})
        self.refused("a deletion of this unit's raw data has begun")

    def test_a_target_that_exists_is_never_written_over(self) -> None:
        (self.raw / self.relatives[0]).write_bytes(b"someone else's file")
        self.refused("already exist")
        (self.raw / self.relatives[0]).unlink()
        (self.output / DBS).write_bytes(b"x")
        with self.assertRaisesRegex(RestoreRefused, "already holds"):
            restore_project_for_viewing(self.manifest, confirmed=True)

    def test_an_output_holding_another_library_copy(self) -> None:
        synthetic_dbs(self.output / "Other_Loaded.msp2.dbs")
        self.refused("already holds Other_Loaded.msp2.dbs")

    def test_paths_outside_the_workspace_root_it_records_against(self) -> None:
        # A lease naming another directory as its root, which holds a copy of the store: every path the
        # restore would write lies outside it, and its record could name none of them for the undo.
        elsewhere = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, elsewhere, True)
        shutil.copytree(self.root / "_library_copies", elsewhere / "_library_copies")
        self.change(download_cache={"workspace_root": str(elsewhere)})
        self.refused("outside the workspace root")
        self.assertFalse((elsewhere / "_viewing").exists())

    def test_a_live_retention_time_preview_of_the_unit(self) -> None:
        jobs = {"rt1": {"id": "rt1", "kind": "rt_correction", "status": "running",
                        "preparation": {"repository_run_manifest": str(self.manifest)}}}
        with patch.object(server, "JOBS", jobs):
            self.refused("job rt1 is queued or running")

    def test_containers_a_retry_moved_count_and_containers_none_moved_refuse(self) -> None:
        finalisation = read_manifest(self.manifest)["console_run_finalisation"]
        relocation = finalisation["msdial_intermediates"]
        *first, last = relocation["moved"]
        held = {**relocation, "moved": first, "pending": [last["relative_path"]],
                "errors": [f"{last['relative_path']}: held by another process"]}
        self.change(console_run_finalisation={**finalisation, "msdial_intermediates": held})
        self.refused("never moved")

        self.change(finalisation_hold_resolutions=[{"hold": "h1", "step": "msdial_intermediates", "job_id": "run1",
                                                    "moved": [last], "moved_bytes": last["size_bytes"]}])
        preview = plan_project_restore(self.manifest)
        self.assertEqual(len(self.relatives), preview["containers"])
        self.assertTrue(self.restore()["restored"])
        self.assertTrue((self.raw / last["relative_path"]).is_file())

    def test_a_record_it_cannot_read_is_refused_without_an_absolute_path(self) -> None:
        denied = PermissionError(13, "Access is denied", str(self.store.entry_path(self.object_id)))
        with patch.object(library_copy_store, "_read_json", side_effect=denied):
            with self.assertRaises(RestoreRefused) as raised:
                plan_project_restore(self.manifest)
        assert_names_no_root(self, self.root, str(raised.exception))
        self.assertIn(f"_library_copies/o/{self.object_id}/entry.json", str(raised.exception))

    def test_a_split_parent_has_no_run_of_its_own(self) -> None:
        self.change(split_into=[{"manifest_path": str(self.root / "p" / "dda" / "provenance" / "run-manifest.json")}])
        self.refused("split parent")

    def test_the_tool_reports_a_refusal_as_such(self) -> None:
        from msdial_app import mcp_server

        self.change(finalisation_holds=[_hold(["sharing"], "mztab_redaction", "run1", "held")])
        refused = mcp_server.msdial_restore_project_for_viewing(str(self.manifest), confirmed=True)
        self.assertEqual((False, "viewing_restore_refused"), (refused["ok"], refused["reason"]))
        preview = mcp_server.msdial_undo_project_restore(str(self.manifest))
        self.assertEqual("viewing_restore_refused", preview["reason"])


class TheUndo(_Finalised):
    def test_what_msdial_changed_is_put_back_and_what_it_made_is_reported_not_deleted(self) -> None:
        self.restore()
        mddata = self.output / f"{PROJECT}.mddata"
        mddata.write_bytes(b"saved by MS-DIAL")
        (self.output / f"{PROJECT}.mddata.mdproject").write_bytes(b"made by MS-DIAL")
        (self.raw / "data" / f"Sample_A_{TS}.pai2").write_bytes(b"rewritten on a switch of files")
        (self.raw / "data" / "Sample_A_20261101200.dcl").write_bytes(b"a reprocess wrote this")

        undone = self.undo()

        self.assertTrue(undone["undone"])
        self.assertEqual(b"\xc7 mddata", mddata.read_bytes())
        [saved] = (self.root / "_viewing").glob("r-*/gui-saved/*/*.mddata")
        self.assertEqual(b"saved by MS-DIAL", saved.read_bytes())
        self.assertEqual(["data/Sample_A_%s.pai2" % TS], [item.split("/raw/")[-1] for item in undone["changed_by_gui"]])
        self.assertFalse((self.raw / "data" / f"Sample_A_{TS}.pai2").exists())
        self.assertEqual({f"metabolights/MTBLS-SYN/unit-syn/output/{PROJECT}.mddata.mdproject",
                          "metabolights/MTBLS-SYN/unit-syn/raw/data/Sample_A_20261101200.dcl"},
                         set(undone["new_files"]))
        self.assertTrue((self.output / f"{PROJECT}.mddata.mdproject").exists())
        self.assertTrue((self.raw / "data" / "Sample_A_20261101200.dcl").exists())
        self.assertEqual(f"container {self.relatives[1]}".encode(), (self.retained / self.relatives[1]).read_bytes())

    def test_a_save_that_replaced_the_link_with_the_same_libraries_is_removed_through_the_store(self) -> None:
        self.restore()
        link = self.output / DBS
        link.unlink()
        synthetic_dbs(link, date_time=(2026, 10, 12, 9, 30, 0))  # MS-DIAL's save: a new zip, new timestamps

        preview = undo_project_restore(self.manifest)
        self.assertEqual("store", preview["library_copy"]["action"])
        undone = self.undo()

        self.assertTrue(undone["undone"])
        self.assertFalse(link.exists())
        [record] = read_manifest(self.manifest)["library_copy_records"]
        self.assertEqual(("undo_gui_copy", "deduplicated", self.object_id),
                         (record["action"], record["outcome"], record["object_id"]))
        self.assert_restores_from_its_own_object_again()

    def test_a_save_that_wrote_other_content_keeps_it_in_the_store(self) -> None:
        self.restore()
        link = self.output / DBS
        link.unlink()
        synthetic_dbs(link, storage=b"other settings")
        self.undo()

        self.assertFalse(link.exists())
        [record] = read_manifest(self.manifest)["library_copy_records"]
        self.assertEqual(("undo_gui_copy", "stored"), (record["action"], record["outcome"]))
        self.assertNotEqual(self.object_id, record["object_id"])
        self.assertIsNone(self.store.problem(record["object_id"], full=True))
        # The GUI's copy is the GUI's: the run is reopened from its own object, as often as asked.
        self.assert_restores_from_its_own_object_again()

    def assert_restores_from_its_own_object_again(self) -> None:
        self.assertEqual(self.object_id, plan_project_restore(self.manifest)["library_copy"]["object_id"])
        again = self.restore()
        self.assertTrue(again["restored"], again)
        self.assertEqual(os.stat(self.store.object_path(self.object_id)).st_ino, os.stat(self.output / DBS).st_ino)
        self.assertTrue(self.undo()["undone"])

    def test_a_save_as_library_copy_goes_through_the_store_and_one_it_cannot_take_keeps_the_hold(self) -> None:
        self.restore()
        other = synthetic_dbs(self.output / "Project-saved-as_Loaded.msp2.dbs", date_time=(2026, 10, 12, 9, 0, 0))
        preview = undo_project_restore(self.manifest)
        self.assertEqual(["metabolights/MTBLS-SYN/unit-syn/output/Project-saved-as_Loaded.msp2.dbs"],
                         preview["library_copies_to_store"])
        with patch.object(LibraryCopyStore, "_locked", side_effect=LibraryCopyStoreError("the lock was not taken")):
            partial = self.undo()
        self.assertFalse(partial["undone"])
        self.assertTrue(other.exists())
        self.assertEqual(["viewing_restore"], [item["step"] for item in read_manifest(self.manifest)["finalisation_holds"]])

        undone = self.undo()
        self.assertTrue(undone["undone"], undone)
        self.assertFalse(other.exists())
        self.assertEqual([], read_manifest(self.manifest)["finalisation_holds"])
        [record] = read_manifest(self.manifest)["library_copy_records"]
        self.assertEqual(("undo_gui_copy", "deduplicated", self.object_id),
                         (record["action"], record["outcome"], record["object_id"]))

    def test_a_manifest_it_cannot_write_leaves_the_hold_for_the_next_call_to_lift(self) -> None:
        self.restore()
        write = repository_reanalysis.update_manifest
        calls = []

        def busy_once(path, change):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("manifest lock timed out")
            return write(path, change)

        with patch.object(repository_reanalysis, "update_manifest", busy_once):
            first = self.undo()
        self.assertFalse(first["undone"])
        self.assertEqual("lifting_hold", first["state"])
        self.assertIn("Call again to lift it", first["message"])
        self.assertEqual(["viewing_restore"], [item["step"] for item in read_manifest(self.manifest)["finalisation_holds"]])
        self.assertFalse((self.raw / self.relatives[0]).exists(), "everything it made is taken back already")

        preview = undo_project_restore(self.manifest)
        self.assertTrue(preview["lift_only"])
        second = self.undo()
        self.assertTrue(second["undone"], second)
        manifest = read_manifest(self.manifest)
        self.assertEqual([], manifest["finalisation_holds"])
        self.assertEqual("undone", manifest["viewing_restores"][0]["state"])
        record = json.loads((self.root / manifest["viewing_restores"][0]["record"]).read_text(encoding="utf-8"))
        self.assertEqual("undone", record["state"])

    def test_a_directory_it_made_that_holds_files_it_did_not_keeps_the_restore_and_no_save_is_lost(self) -> None:
        shutil.rmtree(self.raw)
        self.restore()
        project = self.output / f"{PROJECT}.mdproject"
        project.write_bytes(b"first save")
        made = self.raw / "data" / "Sample_A_20261101200.dcl"
        made.write_bytes(b"a reprocess wrote this")

        partial = self.undo()
        self.assertFalse(partial["undone"])
        self.assertEqual(["metabolights/MTBLS-SYN/unit-syn/raw/data", "metabolights/MTBLS-SYN/unit-syn/raw"],
                         partial["kept_directories"])
        self.assertIn("hold files it did not make", partial["message"])
        self.assertEqual(["viewing_restore"], [item["step"] for item in read_manifest(self.manifest)["finalisation_holds"]])
        self.assertTrue(made.exists(), "reported, never deleted")

        project.write_bytes(b"second save")  # MS-DIAL, still open, saved again on close
        made.unlink()
        done = self.undo()
        self.assertTrue(done["undone"], done)
        self.assertFalse(self.raw.exists())
        self.assertEqual(b"PK project", project.read_bytes())
        saves = sorted(path.read_bytes() for path in (self.root / "_viewing").glob(f"r-*/gui-saved/*/{PROJECT}.mdproject"))
        self.assertEqual([b"first save", b"second save"], saves)

    def test_a_copy_of_the_object_the_restore_did_not_finish_is_removed_never_stored(self) -> None:
        link = self.output / DBS
        copy = shutil.copyfileobj

        def cut_short(reader, writer, length=0):
            if os.path.basename(writer.name) == DBS:
                writer.write(reader.read(200))
                raise OSError(28, "No space left on device")
            return copy(reader, writer, length)

        cross_volume = OSError(None, "not the same device", None, 17)
        cross_volume.errno = 18  # EXDEV, so that it reads as another volume on any system
        with patch.object(project_restore.os, "link", side_effect=cross_volume), \
                patch.object(project_restore.shutil, "copyfileobj", cut_short):
            done = self.restore()
        self.assertEqual("failed", done["state"])
        self.assertEqual(200, link.stat().st_size)

        preview = undo_project_restore(self.manifest)
        self.assertEqual("remove", preview["library_copy"]["action"])
        undone = self.undo()
        self.assertTrue(undone["undone"], undone)
        self.assertFalse(link.exists())
        self.assertNotIn("library_copy_records", read_manifest(self.manifest))
        self.assertEqual([self.object_id], self.store.object_ids())

    def test_a_copy_of_the_object_that_does_not_match_is_removed_by_the_restore(self) -> None:
        link = self.output / DBS
        copy = shutil.copyfileobj

        def garbled(reader, writer, length=0):
            if os.path.basename(writer.name) == DBS:
                data = bytearray(reader.read())
                data[100] ^= 0xFF
                writer.write(bytes(data))
                return None
            return copy(reader, writer, length)

        cross_volume = OSError(None, "not the same device", None, 17)
        cross_volume.errno = 18
        with patch.object(project_restore.os, "link", side_effect=cross_volume), \
                patch.object(project_restore.shutil, "copyfileobj", garbled):
            done = self.restore()
        self.assertEqual("failed", done["state"])
        self.assertFalse(link.exists())
        self.assertTrue(self.undo()["undone"])
        self.assertNotIn("library_copy_records", read_manifest(self.manifest))
        self.assertEqual([self.object_id], self.store.object_ids())

    def test_the_undo_takes_back_the_output_it_restored_into_and_no_new_run_starts_meanwhile(self) -> None:
        self.restore()
        refused, _ = start_new_production_run({**read_manifest(self.manifest), "manifest_path": str(self.manifest)},
                                              write=True)
        self.assertEqual((False, "viewing_restore_active"), (refused["started"], refused["reason"]))
        with self.assertRaises(server.UnitRestoredError) as raised:
            with server._single_console_per_unit(str(self.manifest), "job9"):
                pass
        self.assertEqual("viewing_restore_active", raised.exception.payload()["code"])

        mddata = self.output / f"{PROJECT}.mddata"
        mddata.write_bytes(b"saved by MS-DIAL")
        elsewhere = self.workspace / "output-run-2"
        elsewhere.mkdir()
        self.change(output_directory=str(elsewhere))  # however it came to be named
        undone = self.undo()
        self.assertTrue(undone["undone"], undone)
        self.assertEqual(b"\xc7 mddata", mddata.read_bytes())
        self.assertEqual([], list(elsewhere.iterdir()))
        self.assertFalse((self.output / DBS).exists())

    def test_a_finalisation_that_meets_a_restore_leaves_its_link_alone(self) -> None:
        self.restore()
        link = self.output / DBS
        links = restore_links(read_manifest(self.manifest), self.manifest)
        self.assertEqual({(os.stat(link).st_dev, os.stat(link).st_ino)}, links)
        copies, errors = store_loaded_library_copies(self.output, self.store, unit=None, job_id="run2", skip=links)
        self.assertEqual(([], []), (copies, errors))
        self.assertEqual([], _library_copies([self.output], links))
        self.assertTrue(link.exists())
        self.assertEqual(2, os.stat(self.store.object_path(self.object_id)).st_nlink)

    @unittest.skipUnless(WINDOWS, "Windows refuses to delete a file another handle holds open")
    def test_a_file_msdial_still_holds_leaves_the_restore_held_until_a_later_call(self) -> None:
        self.restore()
        held = (self.raw / self.relatives[0]).open("rb")
        try:
            partial = self.undo()
        finally:
            held.close()
        self.assertFalse(partial["undone"])
        self.assertEqual("partial", partial["state"])
        self.assertEqual(1, len(read_manifest(self.manifest)["finalisation_holds"]))
        self.assertIn("still open in another program", partial["message"])

        done = self.undo()
        self.assertTrue(done["undone"], done)
        self.assertFalse((self.raw / self.relatives[0]).exists())
        self.assertEqual([], read_manifest(self.manifest)["finalisation_holds"])

    def test_a_restore_that_stopped_part_way_is_held_and_taken_back_as_far_as_it_got(self) -> None:
        shutil.rmtree(self.raw)
        copies = []
        copy = shutil.copyfileobj
        targets = {Path(relative).name for relative in self.relatives}

        def stop_after_two(reader, writer, length=0):
            # Counted by what is written, not by call: a backup through shutil.copy2 may come here too.
            if os.path.basename(getattr(writer, "name", "")) in targets:
                if len(copies) == 2:
                    raise OSError(28, "No space left on device")
                copies.append(1)
            return copy(reader, writer, length)

        with patch.object(project_restore.shutil, "copyfileobj", stop_after_two):
            done = self.restore()
        self.assertEqual("failed", done["state"])
        self.assertIn("stopped after", done["message"])
        manifest = read_manifest(self.manifest)
        self.assertEqual(["viewing_restore"], [item["step"] for item in manifest["finalisation_holds"]])
        # Two copies made and the third begun: the record names all three, journalled before each was opened.
        self.assertEqual(3, sum(1 for path in (self.raw / "data").iterdir()))
        record = json.loads((self.root / manifest["viewing_restores"][0]["record"]).read_text(encoding="utf-8"))
        self.assertEqual(3, len(record["created_files"]))

        undone = self.undo()
        self.assertTrue(undone["undone"], undone)
        self.assertFalse(self.raw.exists())
        self.assertFalse((self.output / DBS).exists())


class TheRaces(_Finalised):
    """What the restore reads before it writes is read again where nothing can start in between: the deletion
    locks, and the manifest's writer lock as its hold is recorded."""

    def assert_nothing_recorded(self) -> None:
        manifest = read_manifest(self.manifest)
        self.assertEqual([], manifest["finalisation_holds"])
        self.assertNotIn("viewing_restores", manifest)
        self.assertEqual([], list((self.root / "_viewing").glob("r-*")))
        self.assertFalse((self.output / DBS).exists())

    def test_a_deletion_that_holds_its_lock_refuses_the_restore(self) -> None:
        ready, finished = threading.Event(), threading.Event()

        def deletion() -> None:
            with repository_reanalysis._raw_deletion_lock(self.manifest):
                ready.set()
                finished.wait(30)

        thread = threading.Thread(target=deletion)
        thread.start()
        try:
            self.assertTrue(ready.wait(30))
            with self.assertRaisesRegex(RestoreRefused, "holds its lock"):
                self.restore()
        finally:
            finished.set()
            thread.join(30)
        self.assert_nothing_recorded()
        self.assertTrue(self.restore()["restored"])

    def test_a_deletion_begun_after_the_plan_is_refused_as_the_hold_is_written(self) -> None:
        self.change(raw_deletion={"state": "deleting", "kind": "cleanup", "target": str(self.raw)})
        with patch.object(project_restore, "_refuse_live", lambda *_args: None):
            with self.assertRaisesRegex(RestoreRefused, "has begun and not finished"):
                self.restore()
        self.assert_nothing_recorded()

    def test_a_deletion_judged_before_the_restore_stood_deletes_nothing(self) -> None:
        self.restore()
        # Its own write of the intent reads the holds again: the restore's is there now.
        with self.assertRaises(FinalisationHeld):
            repository_reanalysis._delete_raw_tree(self.manifest, self.raw, kind="cleanup", authorized_by={},
                                                   kept={}, finish=lambda _current: None)
        self.assertTrue((self.raw / self.relatives[0]).is_file())
        self.assertTrue((self.data / "Sample_A.mzML").is_file())
        self.assertNotIn("raw_deletion", read_manifest(self.manifest))

    def test_a_console_job_still_being_prepared_refuses_the_restore(self) -> None:
        key = server._unit_key(str(self.manifest))
        with patch.dict(server.CONSOLE_SLOTS, {key: "job7"}):
            with self.assertRaisesRegex(RestoreRefused, "job job7"):
                self.restore()
            # And when it took the slot after the plan was read: refused as the hold is written.
            with patch.object(project_restore, "_refuse_live", lambda *_args: None):
                with self.assertRaisesRegex(RestoreRefused, "job job7"):
                    self.restore()
        self.assert_nothing_recorded()

    def test_a_job_prepared_while_a_restore_was_recorded_stops_before_its_console(self) -> None:
        self.restore()
        preparation = {"command": [sys.executable, "-c", "pass"], "run_directory": str(self.output),
                       "repository_run_manifest": str(self.manifest)}
        processes = {"job8": {"kind": "run", "cancel": threading.Event(), "timeout_seconds": None,
                              "idle_timeout_seconds": None, "pid": None}}
        with patch.object(server, "PROCESSES", processes), patch.object(server, "JOBS", {}), \
                patch.object(server, "_persist_jobs_locked", lambda: None), \
                patch.object(server, "run_console", side_effect=AssertionError("the Console started")):
            with self.assertRaises(server.UnitRestoredError):
                server._run_console_for_job("job8", "run", preparation, lambda _line: None)
        self.assertEqual({}, processes)
        self.assertNotIn("run_attempts", read_manifest(self.manifest))


class TheUndoAgainstSaves(_Finalised):
    """MS-DIAL saves the library copy by deleting it and moving a new file into place, and holds nothing open
    between saves: a save on close can land while the undo runs."""

    def test_a_save_between_the_plan_and_the_removal_of_the_link_is_stored_never_deleted(self) -> None:
        self.restore()
        link = self.output / DBS
        planned = project_restore._planned_undo

        def plan_then_save(*args, **kwargs):
            plan = planned(*args, **kwargs)
            if plan["link"] is not None and plan["link"]["action"] == "remove":
                link.unlink()
                synthetic_dbs(link, storage=b"a library the user added in the GUI")
            return plan

        with patch.object(project_restore, "_planned_undo", plan_then_save):
            undone = self.undo()
        self.assertTrue(undone["undone"], undone)
        self.assertFalse(link.exists())
        [record] = read_manifest(self.manifest)["library_copy_records"]
        self.assertEqual(("undo_gui_copy", "stored"), (record["action"], record["outcome"]))
        self.assertNotEqual(self.object_id, record["object_id"])
        self.assertIsNone(self.store.problem(record["object_id"], full=True))
        self.assertEqual(1, os.stat(self.store.object_path(self.object_id)).st_nlink)
        self.assertEqual([], [path.name for path in self.output.iterdir() if "~" in path.name])

    def test_a_save_after_the_link_was_removed_keeps_the_restore_held_for_the_next_call(self) -> None:
        self.restore()
        link = self.output / DBS
        remove = project_restore._remove

        def save_on_close(path, busy, root):
            if not link.exists():
                synthetic_dbs(link, storage=b"saved on close")
            return remove(path, busy, root)

        with patch.object(project_restore, "_remove", save_on_close):
            partial = self.undo()
        self.assertFalse(partial["undone"])
        self.assertIn("saved while the undo ran", partial["message"])
        self.assertTrue(link.exists(), "never removed unstored")
        self.assertEqual(["viewing_restore"], [item["step"] for item in read_manifest(self.manifest)["finalisation_holds"]])

        done = self.undo()
        self.assertTrue(done["undone"], done)
        self.assertFalse(link.exists())
        [record] = read_manifest(self.manifest)["library_copy_records"]
        self.assertEqual(("undo_gui_copy", "stored"), (record["action"], record["outcome"]))

    def busy_once(self):
        write = repository_reanalysis.update_manifest
        calls = []

        def busy(path, change):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("manifest lock timed out")
            return write(path, change)

        return patch.object(repository_reanalysis, "update_manifest", busy)

    def test_a_gui_copy_stored_by_a_call_whose_manifest_write_failed_is_named_by_the_call_that_lifts_the_hold(self) -> None:
        self.restore()
        link = self.output / DBS
        link.unlink()
        synthetic_dbs(link, storage=b"other settings")
        with self.busy_once():
            first = self.undo()
        self.assertEqual("lifting_hold", first["state"])
        [stored] = first["gui_library_copies"]
        self.assertNotIn("library_copy_records", read_manifest(self.manifest))

        second = self.undo()
        self.assertTrue(second["undone"], second)
        [record] = read_manifest(self.manifest)["library_copy_records"]
        self.assertEqual(("undo_gui_copy", "stored", stored["object_id"]),
                         (record["action"], record["outcome"], record["object_id"]))
        self.assertEqual([stored["object_id"]], [item["object_id"] for item in second["gui_library_copies"]])

    @unittest.skipUnless(WINDOWS, "Windows refuses to delete a file another handle holds open")
    def test_a_gui_copy_stored_by_a_partial_call_whose_manifest_write_failed_is_named_once(self) -> None:
        self.restore()
        link = self.output / DBS
        link.unlink()
        synthetic_dbs(link, storage=b"other settings")
        held = (self.raw / self.relatives[0]).open("rb")
        try:
            with self.busy_once():
                first = self.undo()
        finally:
            held.close()
        self.assertEqual("partial", first["state"])
        self.assertFalse(link.exists(), "stored already")

        second = self.undo()
        self.assertTrue(second["undone"], second)
        [record] = read_manifest(self.manifest)["library_copy_records"]
        self.assertEqual("undo_gui_copy", record["action"])
        self.assertEqual(record["object_id"], first["gui_library_copies"][0]["object_id"])


class TwoPartsOfOneTree(unittest.TestCase):
    """Two parts split from one unit read its raw tree and restore into it: released, the first restore makes its
    directories and the second restores into them."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        accession = self.root / "metabolights" / "MTBLS-SYN"
        self.raw = accession / "unit-syn" / "raw"
        self.parent = accession / "unit-syn" / "provenance" / "run-manifest.json"
        self.store = LibraryCopyStore(self.root)
        self.parts = {
            "dda": self.part(accession / "unit-syn-dda", "Sample_A", "20261091730", "Project-2610090530"),
            "dia": self.part(accession / "unit-syn-dia", "QC_01", "20261091745", "Project-2610090545"),
        }
        _write_json(self.parent, {
            "status": "split_by_acquisition", "workspace": str(self.parent.parent.parent),
            "raw_directory": str(self.raw),
            "project": {"repository": "metabolights", "accession": "MTBLS-SYN", "analysis_unit_id": "unit-syn"},
            "split_into": [{"manifest_path": str(path)} for path in self.parts.values()],
        })

    def tearDown(self) -> None:
        self.directory.cleanup()

    def part(self, workspace: Path, sample: str, stamp: str, project: str) -> Path:
        output = workspace / "output"
        retained = output / "msdial-intermediates"
        output.mkdir(parents=True)
        with (output / "analysis_files.csv").open("w", encoding="ascii", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["file_path", "file_name"])
            writer.writerow([str(self.raw / "data" / f"{sample}.mzML"), sample])
        moved = []
        for relative in [f"data/{sample}_{stamp}{suffix}" for suffix in (".dcl", ".pai2")] + [
                f"data/AlignResult-{stamp}.arf2"]:
            path = retained / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"container {relative}".encode())
            moved.append({"relative_path": relative, "path": str(path), "size_bytes": path.stat().st_size})
        (output / f"{project}.mdproject").write_bytes(b"PK project")
        (output / f"{project}.mddata").write_bytes(b"\xc7 mddata")
        copy = synthetic_dbs(output / f"{project}_Loaded.msp2.dbs")
        stored = self.store.put(copy, read_copy_identity(copy), unit=None, job_id=f"run-{sample}", libraries=LIBRARIES)
        manifest = workspace / "provenance" / "run-manifest.json"
        _write_json(manifest, {
            "status": "mztab_validated", "workspace": str(workspace), "raw_directory": str(self.raw),
            "output_directory": str(output), "raw_owned_by": str(self.parent), "finalisation_holds": [],
            "project": {"repository": "metabolights", "accession": "MTBLS-SYN", "analysis_unit_id": workspace.name},
            "console_run_finalisation": {
                "job_id": f"run-{sample}", "campaign_approval_recorded": True, "libraries": LIBRARIES,
                "msdial_intermediates": {"raw_directory": str(self.raw), "moved": moved, "pending": [], "errors": []},
                "loaded_library_copy": {"copies": [stored], "errors": []},
            },
        })
        return manifest

    def test_two_parts_restored_into_one_released_tree_wait_for_each_other_and_report_nothing_of_each_other(self) -> None:
        dda, dia = self.parts["dda"], self.parts["dia"]
        first = restore_project_for_viewing(dda, confirmed=True)
        second = restore_project_for_viewing(dia, confirmed=True)
        self.assertTrue(first["restored"] and second["restored"], (first, second))
        self.assertEqual(["metabolights/MTBLS-SYN/unit-syn/raw", "metabolights/MTBLS-SYN/unit-syn/raw/data"],
                         first["directories_to_create"])
        self.assertEqual([], second["directories_to_create"])
        theirs = sorted(path.name for path in (self.raw / "data").glob("QC_01_*"))

        preview = undo_project_restore(dda)
        self.assertEqual([], preview["new_files"])
        self.assertEqual([second["restore_id"]], [item["restore_id"] for item in preview["other_restores"]])
        waiting = undo_project_restore(dda, confirmed=True)
        self.assertFalse(waiting["undone"])
        self.assertEqual([], waiting["new_files"], "the other part's copies are not new files of this restore")
        self.assertEqual([], waiting["kept_directories"])
        self.assertEqual(["metabolights/MTBLS-SYN/unit-syn/raw/data", "metabolights/MTBLS-SYN/unit-syn/raw"],
                         waiting["shared_directories"])
        self.assertIn(f"restore {second['restore_id']}", waiting["message"])
        self.assertIn("undo that restore first", waiting["message"])
        self.assertNotIn("moves or removes them", waiting["message"])
        self.assertEqual(theirs, sorted(path.name for path in (self.raw / "data").glob("QC_01_*")))
        self.assertEqual(["viewing_restore"], [item["step"] for item in read_manifest(dda)["finalisation_holds"]])

        self.assertTrue(undo_project_restore(dia, confirmed=True)["undone"])
        done = undo_project_restore(dda, confirmed=True)
        self.assertTrue(done["undone"], done)
        self.assertFalse(self.raw.exists(), "no released raw tree is left behind")


class TheConsoleTempCopy(_Finalised):
    def test_a_copy_a_console_left_held_and_stored_later_is_never_the_runs_own(self) -> None:
        from test_console_temp_directory import _LEAVES

        preparation = {"command": [sys.executable, "-u", "-c", _LEAVES], "run_directory": str(self.output),
                       "export_folder_path": str(self.output), "repository_run_manifest": str(self.manifest),
                       "expected_analysis_exports": [], "qa_matrix_expected": False}
        jobs = {"run1": {"id": "run1", "status": "running", "kind": "run", "logs": [], "preparation": preparation}}
        # The run's own job: a Console of it left a library copy in its TEMP, and the store could not take it then.
        with patch.object(server, "JOBS", jobs), patch.object(server, "PROCESSES", {}), \
                patch.object(server, "_persist_jobs_locked", lambda: None), \
                patch.object(LibraryCopyStore, "_locked", side_effect=LibraryCopyStoreError("the lock was not taken")):
            server._run_console_for_job("run1", "run", preparation, lambda _line: None)
        [hold] = read_manifest(self.manifest)["finalisation_holds"]
        self.assertEqual(("loaded_library_copy", "console_temp", ["sharing"]), (hold["step"], hold["origin"], hold["blocks"]))
        self.assertEqual(["tmpA1B2.tmp"], [Path(item).name for item in hold["files"]])

        self.assertEqual([], resolve_finalisation_holds(self.manifest))
        manifest = read_manifest(self.manifest)
        [record] = manifest["library_copy_records"]
        self.assertEqual(("console_temp", "run1", "tmpA1B2.tmp", "stored"),
                         (record["action"], record["job_id"], record["name"], record["outcome"]))
        self.assertEqual([self.object_id], [item["object_id"] for item in
                                            manifest["console_run_finalisation"]["loaded_library_copy"]["copies"]])
        self.assertEqual(self.object_id, plan_project_restore(self.manifest)["library_copy"]["object_id"])
        self.assertFalse((self.root / "_tmp" / "run1").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

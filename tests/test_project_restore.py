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
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app import project_restore
from msdial_app.library_copy_store import LibraryCopyStore, read_copy_identity
from msdial_app.materials_methods import generate_publication_report
from msdial_app.process_liveness import process_created_at
from msdial_app.project_restore import RestoreRefused, plan_project_restore, restore_project_for_viewing, undo_project_restore
from msdial_app.repository_reanalysis import (
    _write_json,
    plan_download_cleanup,
    read_manifest,
    refresh_retained_artifacts,
    update_manifest,
)
from msdial_app.run_finalisation import FinalisationHeld, _hold, resolve_finalisation_holds
from test_library_copy_store import LIBRARIES, synthetic_dbs

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
        """Every file of the unit but its manifest, which records the restore."""
        found = {}
        for directory, _folders, names in os.walk(self.workspace):
            for name in names:
                path = Path(directory) / name
                if path == self.manifest:
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
        self.assertNotIn(str(self.root), json.dumps(record))

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
        [saved] = (self.root / "_viewing").glob("r-*/gui-saved/*.mddata")
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

    def test_a_save_that_wrote_other_content_keeps_it_in_the_store(self) -> None:
        self.restore()
        link = self.output / DBS
        link.unlink()
        synthetic_dbs(link, storage=b"other settings")
        self.undo()

        self.assertFalse(link.exists())
        [record] = read_manifest(self.manifest)["library_copy_records"]
        self.assertEqual("stored", record["outcome"])
        self.assertNotEqual(self.object_id, record["object_id"])
        self.assertIsNone(self.store.problem(record["object_id"], full=True))

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

        def stop_after_two(reader, writer, length=0):
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

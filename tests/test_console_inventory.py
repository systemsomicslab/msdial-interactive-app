"""A Console is the whole folder it runs from, not MSDIALCUI.exe alone.

The build record named MSDIALCUI.exe's sha256 and the git head. The mzML base64 fix is in
RawDataHandler.dll, a package the Console only references, so a folder whose RawDataHandler.dll
had been swapped for another build inspected as verified. The record now carries an inventory,
the sha256 of every file in the folder with the key assemblies' ProductVersions, and inspection
checks it file by file. A record written before inventories were still verifies, with a warning,
and console_management.record_console_inventory adds one without a rebuild.

The folders here are synthetic: a few small files standing for the Console's output folder.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from msdial_app import workflow
from msdial_app.console_management import build_local_console, record_console_inventory
from msdial_app.workflow import (
    CONSOLE_BUILD_PROVENANCE,
    CONSOLE_INVENTORY_NOT_RECORDED,
    CONSOLE_INVENTORY_TOO_LARGE,
    _console_provenance_warning,
    console_inventory,
    inspect_console_path,
)

from test_raw_metadata_extractor import _assembly


RDH_VERSION = "1.3.9769.328"


def _console_folder(root: Path) -> Path:
    """A net48 Console output folder: the exe, its dependencies, a vendor library and a satellite."""
    folder = root / "bin" / "Release" / "net48"
    _assembly(folder / "MSDIALCUI.exe", "5.5.260930+f56d4478a6e57e6b88782da8bdf04bcb207d9afd", b"cui")
    _assembly(folder / "RawDataHandler.dll", RDH_VERSION, b"rdh")
    _assembly(folder / "MsdialCore.dll", "1.0.0+f56d4478a6e57e6b88782da8bdf04bcb207d9afd", b"core")
    _assembly(folder / "Common.dll", "1.0.0+f56d4478a6e57e6b88782da8bdf04bcb207d9afd", b"common")
    (folder / "MSDIALCUI.exe.config").write_text("<configuration />", encoding="ascii")
    (folder / "lib" / "Waters").mkdir(parents=True)
    (folder / "lib" / "Waters" / "MassLynxRaw.dll").write_bytes(b"vendor")
    (folder / "ja").mkdir()
    (folder / "ja" / "MsdialCore.resources.dll").write_bytes(b"satellite")
    return folder / "MSDIALCUI.exe"


class _Probes(unittest.TestCase):
    """The Console is never started: its version and capabilities are stubbed."""

    def setUp(self) -> None:
        for target, value in (
            ("msdial_app.workflow.console_capabilities", {"capability_probe": "test", "capabilities": []}),
            ("msdial_app.workflow.console_version", "5.5.260930"),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.console = _console_folder(self.root)
        self.folder = self.console.parent

    def build(self) -> dict:
        """Record the folder as build_local_console does after a build."""
        process = Mock()
        process.stdout = io.StringIO("Build succeeded.\n")
        process.wait.return_value = 0
        plan = {
            "source_root": str(self.root),
            "framework": "net48",
            "configuration": "Release",
            "command": ["dotnet", "build"],
            "command_text": "dotnet build",
            "output_path": str(self.console),
        }
        with patch("msdial_app.console_management.subprocess.Popen", return_value=process), patch(
            "msdial_app.console_management.console_git_state", return_value={"head": "f56d4478a"}
        ), patch("msdial_app.console_management.save_path_settings"):
            return build_local_console(plan, lambda _message: None, select_after_build=False)

    def legacy_record(self) -> dict:
        """A record written before inventories were: the binary and the git head only."""
        record = {
            "schema_version": 1,
            "built_at": "2026-09-30T14:40:32+09:00",
            "binary_sha256": inspect_console_path(self.console)["assembly_sha256"],
            "binary_version": "5.5.260930",
            "git_head": "f56d4478a6e57e6b88782da8bdf04bcb207d9afd",
        }
        self.record_path.write_text(json.dumps(record), encoding="utf-8")
        return record

    @property
    def record_path(self) -> Path:
        return self.folder / CONSOLE_BUILD_PROVENANCE

    def record(self) -> dict:
        return json.loads(self.record_path.read_text(encoding="utf-8"))


class TheInventory(_Probes):
    def test_every_file_of_the_folder_is_listed_with_the_key_assemblies_versions(self) -> None:
        inventory = console_inventory(self.folder)

        self.assertEqual("complete", inventory["inventory_status"])
        self.assertEqual(
            ["Common.dll", "MSDIALCUI.exe", "MSDIALCUI.exe.config", "MsdialCore.dll", "RawDataHandler.dll",
             "ja/MsdialCore.resources.dll", "lib/Waters/MassLynxRaw.dll"],
            [entry["path"] for entry in inventory["inventory"]],
        )
        self.assertEqual(64, len(inventory["inventory_sha256"]))
        self.assertEqual(RDH_VERSION, inventory["key_assemblies"]["RawDataHandler.dll"]["product_version"])
        self.assertEqual({"MSDIALCUI.exe", "RawDataHandler.dll", "MsdialCore.dll", "Common.dll"},
                         set(inventory["key_assemblies"]))

    def test_the_record_logs_and_temporary_files_are_not_part_of_it(self) -> None:
        before = console_inventory(self.folder)["inventory_sha256"]
        self.record_path.write_text("{}", encoding="utf-8")
        (self.folder / "MSDIALCUI.log").write_text("run", encoding="ascii")
        (self.folder / "crash.dmp").write_bytes(b"dump")
        (self.folder / "logs").mkdir()
        (self.folder / "logs" / "today.txt").write_text("run", encoding="ascii")

        self.assertEqual(before, console_inventory(self.folder)["inventory_sha256"])

    def test_a_folder_past_the_bounds_is_not_hashed_through(self) -> None:
        with patch.object(workflow, "CONSOLE_INVENTORY_MAX_FILES", 3):
            inventory = console_inventory(self.folder)
            inspected = inspect_console_path(self.console)

        self.assertEqual(("too_large", [], ""), (inventory["inventory_status"], inventory["inventory"],
                                                 inventory["inventory_sha256"]))
        self.assertIn(CONSOLE_INVENTORY_TOO_LARGE, inspected["provenance_warnings"])


class ABuildRecordsItsFolder(_Probes):
    def test_the_build_record_carries_the_inventory_and_inspects_as_verified(self) -> None:
        built = self.build()
        record = self.record()

        self.assertEqual("verified", built["provenance_status"])
        self.assertEqual([], built["provenance_warnings"])
        self.assertEqual("at_build", record["inventory_recorded"])
        self.assertEqual(console_inventory(self.folder)["inventory_sha256"], record["inventory_sha256"])
        self.assertEqual(7, record["inventory_file_count"])
        self.assertEqual(RDH_VERSION, record["key_assemblies"]["RawDataHandler.dll"]["product_version"])
        self.assertNotIn("inventory", built["provenance"], "the list stays beside the binary")
        self.assertEqual(record["inventory_sha256"], built["provenance"]["inventory_sha256"])

    def test_a_swapped_raw_data_handler_is_stale_and_named(self) -> None:
        """THE REGRESSION. The exe is unchanged, so the record used to verify."""
        self.build()
        _assembly(self.folder / "RawDataHandler.dll", "1.3.9700.1", b"another reader")

        inspected = inspect_console_path(self.console)

        self.assertEqual("stale_mismatch", inspected["provenance_status"])
        self.assertFalse(inspected["provenance_verified"])
        mismatch = inspected["provenance_mismatch"]
        self.assertEqual(mismatch["recorded_binary_sha256"], mismatch["actual_binary_sha256"])
        self.assertEqual(["RawDataHandler.dll"], mismatch["changed"])
        self.assertEqual(
            {"RawDataHandler.dll": {"recorded": RDH_VERSION, "actual": "1.3.9700.1"}},
            mismatch["product_version_changed"],
        )
        self.assertIn("RawDataHandler.dll", mismatch["detail"])
        self.assertIn("describes a different one", _console_provenance_warning(inspected))

    def test_an_added_or_removed_dependency_is_stale_and_named(self) -> None:
        self.build()
        (self.folder / "lib" / "Waters" / "MassLynxRaw.dll").unlink()
        (self.folder / "Extra.dll").write_bytes(b"probed by the loader")

        mismatch = inspect_console_path(self.console)["provenance_mismatch"]

        self.assertEqual((["Extra.dll"], ["lib/Waters/MassLynxRaw.dll"]), (mismatch["added"], mismatch["removed"]))
        self.assertIn("Extra.dll (added)", mismatch["detail"])

    def test_a_log_beside_the_console_leaves_it_verified(self) -> None:
        self.build()
        (self.folder / "MSDIALCUI.log").write_text("a run", encoding="ascii")

        self.assertEqual("verified", inspect_console_path(self.console)["provenance_status"])

    def test_an_inventory_that_is_not_a_list_of_files_is_unreadable(self) -> None:
        self.build()
        record = self.record()
        record["inventory"] = "every file"
        self.record_path.write_text(json.dumps(record), encoding="utf-8")

        self.assertEqual("unreadable", inspect_console_path(self.console)["provenance_status"])

    def test_a_folder_grown_past_the_bounds_is_stale_not_all_removed(self) -> None:
        self.build()
        with patch.object(workflow, "CONSOLE_INVENTORY_MAX_FILES", 3):
            mismatch = inspect_console_path(self.console)["provenance_mismatch"]

        self.assertEqual(("too_large", [], []), (mismatch["inventory_status"], mismatch["removed"],
                                                 mismatch["changed"]))
        self.assertIn("too large", mismatch["detail"])


class ALegacyRecord(_Probes):
    def test_it_still_verifies_with_the_warning(self) -> None:
        self.legacy_record()

        inspected = inspect_console_path(self.console)

        self.assertEqual("verified", inspected["provenance_status"])
        self.assertEqual([CONSOLE_INVENTORY_NOT_RECORDED], inspected["provenance_warnings"])
        self.assertEqual(64, len(inspected["inventory_sha256"]), "the folder is still named by its digest")
        self.assertIn("record_console_inventory", _console_provenance_warning(inspected))

    def test_a_swapped_dependency_goes_unnoticed_until_the_inventory_is_recorded(self) -> None:
        self.legacy_record()
        _assembly(self.folder / "RawDataHandler.dll", "1.3.9700.1", b"another reader")

        self.assertEqual("verified", inspect_console_path(self.console)["provenance_status"])


class RecordingTheInventoryLater(_Probes):
    def test_the_preview_writes_nothing(self) -> None:
        self.legacy_record()
        before = self.record_path.read_bytes()

        plan = record_console_inventory(self.console)

        self.assertFalse(plan["written"])
        self.assertEqual(before, self.record_path.read_bytes())
        self.assertEqual(console_inventory(self.folder)["inventory_sha256"], plan["inventory_sha256"])
        self.assertIn("RawDataHandler.dll", plan["files"])

    def test_confirmed_it_upgrades_the_record_and_keeps_every_other_field(self) -> None:
        legacy = self.legacy_record()

        result = record_console_inventory(self.console, confirmed=True)
        record = self.record()

        self.assertTrue(result["written"])
        self.assertEqual("verified", result["inspection"]["provenance_status"])
        self.assertEqual([], result["inspection"]["provenance_warnings"])
        self.assertEqual(legacy, {key: record[key] for key in legacy})
        self.assertEqual("after_build", record["inventory_recorded"])
        self.assertTrue(record["inventory_recorded_at"])
        self.assertFalse(self.record_path.with_name(self.record_path.name + ".tmp").exists())

        _assembly(self.folder / "RawDataHandler.dll", "1.3.9700.1", b"another reader")
        self.assertEqual("stale_mismatch", inspect_console_path(self.console)["provenance_status"])

    def test_a_record_that_has_one_is_left_alone(self) -> None:
        self.build()
        before = self.record_path.read_bytes()

        result = record_console_inventory(self.console, confirmed=True)

        self.assertEqual(("already_recorded", False), (result["reason"], result["written"]))
        self.assertEqual(before, self.record_path.read_bytes())

    def test_a_record_of_another_binary_takes_none(self) -> None:
        self.legacy_record()
        _assembly(self.console, "5.5.260930", b"a rebuilt exe")

        with self.assertRaisesRegex(ValueError, "stale_mismatch"):
            record_console_inventory(self.console, confirmed=True)

    def test_a_console_without_a_record_takes_none(self) -> None:
        with self.assertRaisesRegex(ValueError, "absent"):
            record_console_inventory(self.console, confirmed=True)


if __name__ == "__main__":
    unittest.main()

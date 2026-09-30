import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app import raw_metadata_extractor as extractor
from msdial_app.raw_metadata_extractor import (
    EXTRACTOR_BUILD_PROVENANCE,
    assembly_product_version,
    extractor_build_command,
    extractor_build_environment,
    extractor_build_root,
    extractor_inventory,
    inspect_raw_metadata_extractor,
    inventory_sha256,
    plan_extractor_build,
    record_build,
    record_verification,
)


# The current built pin: msrawdataworkbench #41 (Waters DDA, WIFF2, Shimadzu .lcd) with MsdialWorkbench master.
RAW_HEAD = "a12293c612a4e29b23d1d584f1c19556d76863f6"
# The pin before it: msrawdataworkbench #40 (the mzML base64 last-element fix), still a built pin.
PREVIOUS_RAW_HEAD = "592b6dbce72177fa14d3e7cd407557b1c64a3046"
COMMON_HEAD = "f0583493a44e73723f53ae312e33955f62052dd7"
# The pair approved on 2026-09-29 and never built.
PLANNED_RAW_HEAD = "b34c857a5328e8f08c1918b3d890e7dae50b7d6d"
PLANNED_COMMON_HEAD = "c471463a576626650e0886e26bd064cca53a7ae3"
# The MsdialWorkbench merge commit the unpinned build's Common.dll carries.
OTHER_HEAD = "73391e2fd27543852c3f38e4f6bf41fdc9a77c6d"
# A NuGet feed and package folder as project.assets.json lists them; neither may reach the
# record.
SYNTHETIC_FEED = r"\\synthetic-nas.test\feeds\nuget-packages"
SYNTHETIC_PACKAGES = r"Q:\synthetic-profile\.nuget\packages"


def _aligned(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


def _version_block(
    key: str, value: bytes = b"", value_length: int = 0, value_type: int = 1, children: bytes = b""
) -> bytes:
    # wLength covers the header, the key, the value and the children, not the padding that
    # aligns the next sibling.
    body = _aligned(struct.pack("<HHH", 0, value_length, value_type) + key.encode("utf-16-le") + b"\0\0")
    body += value
    if children:
        body = _aligned(body) + children
    return struct.pack("<H", len(body)) + body[2:]


def _version_resource(product_version: str) -> bytes:
    """A VS_VERSIONINFO resource laid out as the SDK writes it."""

    def string(key: str, text: str) -> bytes:
        return _aligned(_version_block(key, (text + "\0").encode("utf-16-le"), len(text) + 1))

    table = _version_block(
        "040904b0",
        children=string("FileVersion", "1.0.0.0") + string("ProductVersion", product_version),
    )
    string_file_info = _version_block("StringFileInfo", children=_aligned(table))
    fixed = struct.pack("<13I", 0xFEEF04BD, *([0] * 12))
    return _version_block("VS_VERSION_INFO", fixed, len(fixed), 0, _aligned(string_file_info))


def _assembly(path: Path, product_version: str = "", body: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = _aligned(b"MZ\0\0" + body)
    if product_version:
        image += _version_resource(product_version)
    path.write_bytes(image)


def _write_build(
    root: Path,
    raw_head: str = RAW_HEAD,
    common_head: str = COMMON_HEAD,
    common_project_root: Path | None = None,
) -> tuple[Path, Path, Path]:
    """A built extractor tree and its Common sibling, with a restore record."""
    raw = root / "msrawdataworkbench"
    common = root / "MsdialWorkbench"
    common.mkdir(parents=True, exist_ok=True)
    output = extractor.extractor_output_directory(raw)
    _assembly(output / "RawMetadataConsoleApp.exe", f"1.0.0+{raw_head}", b"extractor")
    _assembly(output / "RawDataHandler.dll", f"1.2.9745.444+{raw_head}", b"handler")
    _assembly(output / "Common.dll", f"1.0.0+{common_head}", b"common")
    _assembly(output / "NCDK.dll", f"1.5.6+{common_head}", b"ncdk")
    # A package with a revision of its own, which says nothing about either tree.
    _assembly(output / "Newtonsoft.Json.dll", f"13.0.3+{'0a2e291c0d9c' * 3}0a2e", b"json")
    (output / "MassLynxRaw.dll").write_bytes(b"vendor masslynx")
    (output / "lib" / "Bruker").mkdir(parents=True)
    (output / "lib" / "Bruker" / "timsdata.dll").write_bytes(b"vendor tims")
    project = raw / "RawMetadataConsoleApp"
    referenced = common_project_root or common
    common_project = referenced / "src" / "Common" / "CommonStandard" / "CommonStandard.csproj"
    handler_project = raw / "RawDataHandlerStandard" / "RawDataHandlerStandard.csproj"
    relative_common = Path(os.path.relpath(common_project, project)).as_posix()
    assets = {
        "version": 3,
        "libraries": {
            "Common/1.0.0": {"type": "project", "path": relative_common, "msbuildProject": relative_common},
            "RawDataHandler/1.2.9745.444": {
                "type": "project",
                "path": "../RawDataHandlerStandard/RawDataHandlerStandard.csproj",
                "msbuildProject": "../RawDataHandlerStandard/RawDataHandlerStandard.csproj",
            },
            "Newtonsoft.Json/13.0.3": {"type": "package", "path": "newtonsoft.json/13.0.3"},
        },
        "project": {
            "restore": {
                "projectPath": str(project / "RawMetadataConsoleApp.csproj"),
                "packagesPath": SYNTHETIC_PACKAGES,
                "sources": {SYNTHETIC_FEED: {}},
                "frameworks": {
                    "net48": {
                        "projectReferences": {
                            str(common_project): {"projectPath": str(common_project)},
                            str(handler_project): {"projectPath": str(handler_project)},
                        }
                    }
                },
            }
        },
    }
    (project / "obj").mkdir(parents=True, exist_ok=True)
    (project / "obj" / "project.assets.json").write_text(json.dumps(assets), encoding="utf-8")
    return raw, common, output / "RawMetadataConsoleApp.exe"


def _git_state(dirty_tree: str = ""):
    def state(root):
        name = Path(root).name
        dirty = name == dirty_tree
        return {
            "available": True,
            "head": RAW_HEAD if name == "msrawdataworkbench" else COMMON_HEAD,
            "branch": "",
            "dirty": dirty,
            "changed_files": 1 if dirty else 0,
            "working_tree_state_sha256": "ab" * 32 if dirty else "",
            "working_tree_diff_sha256": "cd" * 32 if dirty else "",
            "origin_master_head": "",
        }

    return state


class ProductVersionTests(unittest.TestCase):
    def test_the_product_version_is_read_from_the_version_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "RawDataHandler.dll"
            _assembly(path, f"1.2.9745.444+{RAW_HEAD}", b"code")

            self.assertEqual(f"1.2.9745.444+{RAW_HEAD}", assembly_product_version(path))

    def test_a_product_version_string_outside_the_resource_is_not_read(self) -> None:
        # A user string in the metadata heap can spell the same key; only the one inside
        # VS_VERSION_INFO is the product version.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Common.dll"
            decoy = _aligned(b"\0\0\0\0\0\0" + "ProductVersion\0\0".encode("utf-16-le") + "9.9.9\0".encode("utf-16-le"))
            _assembly(path, f"1.0.0+{COMMON_HEAD}", decoy)

            self.assertEqual(f"1.0.0+{COMMON_HEAD}", assembly_product_version(path))

    def test_a_file_without_a_version_resource_has_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vendor.dll"
            _assembly(path, "", b"native code")

            self.assertEqual("", assembly_product_version(path))
            self.assertEqual("", assembly_product_version(Path(temporary) / "missing.dll"))


class BuildCommandTests(unittest.TestCase):
    def test_the_pins_are_the_approved_commits_and_name_the_build_folder(self) -> None:
        self.assertEqual(RAW_HEAD, extractor.PINNED_MSRAWDATAWORKBENCH_COMMIT)
        self.assertEqual(COMMON_HEAD, extractor.PINNED_MSDIALWORKBENCH_COMMIT)
        self.assertEqual(
            "RawMetadataExtractor-a12293c61-f0583493a",
            extractor_build_root(Path("synthetic-parent")).name,
        )

    def test_the_previous_build_stays_a_built_pin_but_is_not_the_current_one(self) -> None:
        previous = extractor.pinned_build(PREVIOUS_RAW_HEAD, COMMON_HEAD)
        self.assertIsNotNone(previous)
        self.assertEqual(extractor.PIN_BUILT, previous["state"])
        self.assertNotEqual(PREVIOUS_RAW_HEAD, extractor.PINNED_MSRAWDATAWORKBENCH_COMMIT)

    def test_the_build_passes_solution_dir_with_a_trailing_separator(self) -> None:
        root = Path("synthetic build") / "msrawdataworkbench"
        command = extractor_build_command(root, "dotnet")

        self.assertEqual(
            [
                "dotnet",
                "build",
                str(root / "RawMetadataConsoleApp" / "RawMetadataConsoleApp.csproj"),
                "--configuration",
                "Release",
                "--nologo",
                f"-p:SolutionDir={root}{os.sep}",
                "-p:ContinuousIntegrationBuild=true",
            ],
            command,
        )

    @unittest.skipUnless(os.name == "nt", "CreateProcess quoting is Windows-only")
    def test_the_command_text_keeps_the_trailing_separator_through_quoting(self) -> None:
        # A path with a space is quoted, and a backslash before the closing quote escapes it
        # unless it is doubled; the text must parse back to exactly the list.
        import ctypes
        from ctypes import wintypes

        parse = ctypes.windll.shell32.CommandLineToArgvW
        parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        parse.restype = ctypes.POINTER(wintypes.LPWSTR)
        command = extractor_build_command(Path(r"C:\synthetic build\msrawdataworkbench"), "dotnet")
        count = ctypes.c_int()
        argv = parse(extractor._command_text(command), ctypes.byref(count))
        try:
            parsed = [argv[index] for index in range(count.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv)

        self.assertEqual(command, parsed)
        self.assertTrue(parsed[6].endswith("\\"))

    def test_the_build_environment_drops_eazfuscator_whatever_its_case(self) -> None:
        environment = extractor_build_environment(
            {"EAZFUSCATOR_NET_HOME": "C:/eaz", "Eazfuscator_Net_Home": "C:/eaz", "PATH": "C:/bin"}
        )

        self.assertEqual({"PATH": "C:/bin"}, environment)


class RecordAndInspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _record(self, raw: Path, common: Path, binary: Path, dirty_tree: str = "", **options):
        with patch.object(extractor, "console_git_state", side_effect=_git_state(dirty_tree)), patch.object(
            extractor, "_git", return_value=""
        ):
            return record_build(binary, raw, common, sdk_version="10.0.401", **options)

    def _stored(self, binary: Path) -> dict:
        return json.loads((binary.parent / EXTRACTOR_BUILD_PROVENANCE).read_text(encoding="utf-8"))

    def test_a_record_that_matches_its_build_is_verified(self) -> None:
        raw, common, binary = _write_build(self.root)

        result = self._record(raw, common, binary)

        self.assertEqual("verified", result["provenance_status"])
        self.assertTrue(result["provenance_verified"])
        self.assertEqual(RAW_HEAD, result["msrawdataworkbench_commit"])
        self.assertEqual(COMMON_HEAD, result["msdialworkbench_commit"])
        self.assertTrue(result["pinned"])
        self.assertEqual(f"1.0.0+{RAW_HEAD}", result["product_version"])
        record = self._stored(binary)
        self.assertEqual("msdial-raw-metadata-extractor-build.v1", record["schema"])
        self.assertEqual(hashlib.sha256(binary.read_bytes()).hexdigest(), record["binary_sha256"])
        self.assertEqual(7, record["file_count"])
        self.assertIn("lib/Bruker/timsdata.dll", [entry["path"] for entry in record["inventory"]])
        expected_lines = "".join(
            f"{entry['path']}\t{hashlib.sha256((binary.parent / entry['path']).read_bytes()).hexdigest()}\n"
            for entry in sorted(record["inventory"], key=lambda entry: entry["path"])
        )
        self.assertEqual(hashlib.sha256(expected_lines.encode("utf-8")).hexdigest(), record["inventory_sha256"])
        self.assertEqual(
            {"RawMetadataConsoleApp.exe", "RawDataHandler.dll", "Common.dll", "NCDK.dll"},
            set(record["product_versions"]),
        )
        self.assertEqual(f"1.0.0+{COMMON_HEAD}", record["product_versions"]["Common.dll"]["product_version"])
        assets = raw / "RawMetadataConsoleApp" / "obj" / "project.assets.json"
        self.assertEqual(hashlib.sha256(assets.read_bytes()).hexdigest(), record["project_assets_sha256"])
        self.assertIn(
            {"tree": "MsdialWorkbench", "project": "src/Common/CommonStandard/CommonStandard.csproj"},
            record["restore_projects"],
        )
        self.assertEqual(RAW_HEAD, record["sources"]["msrawdataworkbench"]["head"])
        self.assertTrue(record["sources"]["MsdialWorkbench"]["matches_pin"])
        self.assertTrue(record["sources_clean"])
        self.assertEqual("10.0.401", record["sdk_version"])
        self.assertIn(f"-p:SolutionDir={raw}{os.sep}", record["command"])
        self.assertEqual("reconstructed", record["command_source"])
        self.assertEqual(("Release", "net48"), (record["configuration"], record["framework"]))
        # Without the plan nothing shows the build ran without EAZFUSCATOR_NET_HOME.
        self.assertEqual("not recorded", record["obfuscation"])

    def test_one_changed_library_beside_an_unchanged_exe_is_stale(self) -> None:
        raw, common, binary = _write_build(self.root)
        self._record(raw, common, binary)
        (binary.parent / "MassLynxRaw.dll").write_bytes(b"another vendor build")

        result = inspect_raw_metadata_extractor(binary)

        self.assertEqual("stale_mismatch", result["provenance_status"])
        self.assertFalse(result["provenance_verified"])
        mismatch = result["provenance_mismatch"]
        self.assertEqual(mismatch["recorded_binary_sha256"], mismatch["actual_binary_sha256"])
        self.assertEqual(["MassLynxRaw.dll"], mismatch["changed"])
        self.assertEqual(([], []), (mismatch["added"], mismatch["removed"]))

    def test_an_added_or_a_removed_library_is_stale(self) -> None:
        raw, common, binary = _write_build(self.root)
        self._record(raw, common, binary)
        (binary.parent / "lib" / "Bruker" / "timsdata.dll").unlink()
        (binary.parent / "x64").mkdir()
        (binary.parent / "x64" / "SQLite.Interop.dll").write_bytes(b"native")

        mismatch = inspect_raw_metadata_extractor(binary)["provenance_mismatch"]

        self.assertEqual(["x64/SQLite.Interop.dll"], mismatch["added"])
        self.assertEqual(["lib/Bruker/timsdata.dll"], mismatch["removed"])

    def test_no_record_is_absent(self) -> None:
        _raw, _common, binary = _write_build(self.root)

        result = inspect_raw_metadata_extractor(binary)

        self.assertEqual("absent", result["provenance_status"])
        self.assertTrue(result["exists"])
        self.assertEqual(hashlib.sha256(binary.read_bytes()).hexdigest(), result["binary_sha256"])

    def test_a_missing_extractor_is_absent_and_does_not_exist(self) -> None:
        result = inspect_raw_metadata_extractor(self.root / "RawMetadataConsoleApp.exe")

        self.assertEqual("absent", result["provenance_status"])
        self.assertFalse(result["exists"])

    def test_an_extractor_without_a_readable_record_is_still_identified_by_its_files(self) -> None:
        # The working-checkout build has no record. The vendor libraries it ran with are then
        # named by the inventory hash and nothing else.
        _raw, _common, binary = _write_build(self.root)
        expected = inventory_sha256(extractor_inventory(binary.parent))

        absent = inspect_raw_metadata_extractor(binary)
        (binary.parent / EXTRACTOR_BUILD_PROVENANCE).write_text("{ not json", encoding="utf-8")
        unreadable = inspect_raw_metadata_extractor(binary)

        for status, result in (("absent", absent), ("unreadable", unreadable)):
            with self.subTest(status=status):
                self.assertEqual(status, result["provenance_status"])
                self.assertEqual(expected, result["inventory_sha256"])
                self.assertEqual(7, result["file_count"])
        (binary.parent / "MassLynxRaw.dll").write_bytes(b"another vendor build")
        self.assertNotEqual(expected, inspect_raw_metadata_extractor(binary)["inventory_sha256"])

    def test_the_planned_binary_is_compared_after_normalisation(self) -> None:
        raw, common, binary = _write_build(self.root)
        spelled = raw / "RawMetadataConsoleApp" / ".." / "RawMetadataConsoleApp" / "bin" / "Release" / "net48"
        plan = {"binary_path": str(spelled / "RawMetadataConsoleApp.exe")}

        result = self._record(raw, common, binary, plan=plan)

        self.assertEqual("verified", result["provenance_status"])

    def test_a_build_folder_is_inspected_through_its_exe(self) -> None:
        raw, common, binary = _write_build(self.root)
        self._record(raw, common, binary)

        result = inspect_raw_metadata_extractor(binary.parent)

        self.assertEqual("verified", result["provenance_status"])
        self.assertEqual(str(binary), result["path"])

    def test_a_record_of_a_dirty_tree_is_dirty_source(self) -> None:
        raw, common, binary = _write_build(self.root)

        result = self._record(raw, common, binary, dirty_tree="MsdialWorkbench")

        self.assertEqual("dirty_source", result["provenance_status"])
        self.assertFalse(result["provenance_verified"])
        self.assertEqual(["MsdialWorkbench"], result["dirty_sources"])
        self.assertFalse(self._stored(binary)["sources_clean"])

    def test_a_record_that_cannot_be_read_is_unreadable(self) -> None:
        raw, common, binary = _write_build(self.root)
        self._record(raw, common, binary)
        record_path = binary.parent / EXTRACTOR_BUILD_PROVENANCE
        stored = self._stored(binary)
        without_inventory = {key: value for key, value in stored.items() if key != "inventory"}
        other_schema = {**stored, "schema": "msdial-console-build.v1"}
        for content in (
            "{ not json",
            "[]",
            json.dumps(without_inventory),
            json.dumps(other_schema),
            json.dumps({**stored, "sources": {"msrawdataworkbench": {}}}),
            json.dumps({**stored, "sources": {**stored["sources"], "MsdialWorkbench": {"dirty": False}}}),
            json.dumps({**stored, "product_versions": []}),
        ):
            with self.subTest(content=content[:40]):
                record_path.write_text(content, encoding="utf-8")

                self.assertEqual("unreadable", inspect_raw_metadata_extractor(binary)["provenance_status"])

    def test_a_record_edited_to_name_another_commit_is_stale(self) -> None:
        # The files still match, but the assemblies themselves say which commit built them.
        raw, common, binary = _write_build(self.root)
        self._record(raw, common, binary)
        record_path = binary.parent / EXTRACTOR_BUILD_PROVENANCE
        stored = self._stored(binary)
        stored["sources"]["MsdialWorkbench"]["head"] = OTHER_HEAD
        record_path.write_text(json.dumps(stored), encoding="utf-8")

        result = inspect_raw_metadata_extractor(binary)

        self.assertEqual("stale_mismatch", result["provenance_status"])
        self.assertEqual(
            ["Common.dll", "NCDK.dll"], result["provenance_mismatch"]["revision_contradicts_record"]
        )

    def test_the_record_keeps_only_the_checksum_of_the_restore_file(self) -> None:
        raw, common, binary = _write_build(self.root)
        self._record(raw, common, binary)

        text = (binary.parent / EXTRACTOR_BUILD_PROVENANCE).read_text(encoding="utf-8")

        for private in (SYNTHETIC_FEED, SYNTHETIC_PACKAGES):
            self.assertNotIn(private, text)
            self.assertNotIn(json.dumps(private)[1:-1], text)

    def test_the_verification_block_does_not_change_the_identity(self) -> None:
        raw, common, binary = _write_build(self.root)
        self._record(raw, common, binary)

        result = record_verification(binary, {"help_exit_code": 0, "negative_controls": {"mzxml": 82}})

        self.assertEqual("verified", result["provenance_status"])
        verification = self._stored(binary)["verification"]
        self.assertEqual(82, verification["negative_controls"]["mzxml"])
        self.assertIn("recorded_at", verification)

    def test_a_stale_build_cannot_be_verified(self) -> None:
        raw, common, binary = _write_build(self.root)
        self._record(raw, common, binary)
        (binary.parent / "MassLynxRaw.dll").write_bytes(b"another vendor build")

        with self.assertRaisesRegex(ValueError, "stale_mismatch"):
            record_verification(binary, {"help_exit_code": 0})

    def test_the_plan_records_the_command_and_that_no_obfuscation_was_applied(self) -> None:
        raw, common, binary = _write_build(self.root)
        plan = {
            "trees": {"msrawdataworkbench": {"commit": RAW_HEAD}, "MsdialWorkbench": {"commit": COMMON_HEAD}},
            "command": ["C:/synthetic/dotnet.exe", "build", "x.csproj"],
            "environment_removed": ["EAZFUSCATOR_NET_HOME"],
        }

        self._record(raw, common, binary, plan=plan)

        record = self._stored(binary)
        self.assertEqual("plan", record["command_source"])
        self.assertEqual(plan["command"], record["command"])
        self.assertEqual("not applied", record["obfuscation"])

    def test_an_assembly_without_a_revision_is_recorded_with_a_warning(self) -> None:
        raw, common, binary = _write_build(self.root)
        _assembly(binary.parent / "NCDK.dll", "1.5.6", b"ncdk")

        result = self._record(raw, common, binary)

        self.assertEqual("verified", result["provenance_status"])
        record = self._stored(binary)
        self.assertIsNone(record["product_versions"]["NCDK.dll"]["matches_tree_head"])
        self.assertTrue(any("NCDK.dll" in warning for warning in record["warnings"]))


class RecordRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _refused(self, raw: Path, common: Path, binary: Path, message: str, **options) -> None:
        with patch.object(extractor, "console_git_state", side_effect=_git_state()), patch.object(
            extractor, "_git", return_value=""
        ):
            with self.assertRaisesRegex(ValueError, message):
                record_build(binary, raw, common, sdk_version="10.0.401", **options)
        self.assertFalse((binary.parent / EXTRACTOR_BUILD_PROVENANCE).exists())

    def test_a_restore_graph_that_compiled_another_common_checkout_is_refused(self) -> None:
        # The unpinned build: its restore record resolved CommonStandard from the moving
        # MsdialWorkbench checkout, not from the tree beside it.
        raw, common, binary = _write_build(self.root, common_project_root=self.root / "moving" / "MsdialWorkbench")

        self._refused(raw, common, binary, "outside the recorded trees")

    def test_a_restore_record_of_another_project_is_refused(self) -> None:
        raw, common, binary = _write_build(self.root)
        assets_path = raw / "RawMetadataConsoleApp" / "obj" / "project.assets.json"
        assets = json.loads(assets_path.read_text(encoding="utf-8"))
        assets["project"]["restore"]["projectPath"] = str(self.root / "checkout" / "RawMetadataConsoleApp.csproj")
        assets_path.write_text(json.dumps(assets), encoding="utf-8")

        self._refused(raw, common, binary, "restore record describes")

    def test_a_missing_restore_record_is_refused(self) -> None:
        raw, common, binary = _write_build(self.root)
        (raw / "RawMetadataConsoleApp" / "obj" / "project.assets.json").unlink()

        self._refused(raw, common, binary, "package graph is unknown")

    def test_an_assembly_built_from_another_commit_is_refused(self) -> None:
        raw, common, binary = _write_build(self.root, common_head=OTHER_HEAD)

        self._refused(raw, common, binary, "not built from these trees: Common.dll names 73391e2fd275")

    def test_a_binary_outside_the_extractor_tree_is_refused(self) -> None:
        raw, common, _binary = _write_build(self.root)
        elsewhere = self.root / "copied" / "RawMetadataConsoleApp.exe"
        _assembly(elsewhere, f"1.0.0+{RAW_HEAD}", b"extractor")

        self._refused(raw, common, elsewhere, "is not inside the msrawdataworkbench tree")

    def test_a_binary_from_another_configuration_or_framework_is_refused(self) -> None:
        # The record says Release and net48. A Debug build is the same project built
        # otherwise, and Interactive's candidate list also searches bin\Release\net8.0-windows.
        for configuration, framework in (
            ("Debug", "net48"),
            ("Release", "net8.0-windows"),
            ("Debug", "net8.0-windows"),
        ):
            with self.subTest(configuration=configuration, framework=framework):
                raw, common, binary = _write_build(self.root / configuration / framework)
                other = raw / "RawMetadataConsoleApp" / "bin" / configuration / framework
                other.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(binary.parent), str(other))

                self._refused(raw, common, other / "RawMetadataConsoleApp.exe", "Release/net48 extractor")

    def test_a_file_other_than_the_extractor_is_refused(self) -> None:
        raw, common, binary = _write_build(self.root)

        self._refused(raw, common, binary.parent / "RawDataHandler.dll", "Release/net48 extractor")

    def test_a_binary_that_is_not_the_planned_one_is_refused(self) -> None:
        raw, common, binary = _write_build(self.root)
        planned = extractor.extractor_output_directory(self.root / "other-build" / "msrawdataworkbench")
        plan = {
            "trees": {"msrawdataworkbench": {"commit": RAW_HEAD}, "MsdialWorkbench": {"commit": COMMON_HEAD}},
            "binary_path": str(planned / "RawMetadataConsoleApp.exe"),
        }

        self._refused(raw, common, binary, "not the planned binary", plan=plan)

    def test_a_common_tree_that_is_not_the_referenced_sibling_is_refused(self) -> None:
        raw, _common, binary = _write_build(self.root)
        other = self.root / "elsewhere" / "MsdialWorkbench"
        other.mkdir(parents=True)

        self._refused(raw, other, binary, "Common tree it compiled")

    def test_an_incomplete_build_is_refused(self) -> None:
        raw, common, binary = _write_build(self.root)
        (binary.parent / "RawDataHandler.dll").unlink()

        self._refused(raw, common, binary, "lacks RawDataHandler.dll")

    def test_a_tree_not_at_the_planned_commit_is_refused(self) -> None:
        raw, common, binary = _write_build(self.root)
        plan = {"trees": {"msrawdataworkbench": {"commit": OTHER_HEAD}}}

        self._refused(raw, common, binary, "not the planned 73391e2fd275", plan=plan)

    def test_a_record_is_written_once_unless_replaced(self) -> None:
        raw, common, binary = _write_build(self.root)
        with patch.object(extractor, "console_git_state", side_effect=_git_state()), patch.object(
            extractor, "_git", return_value=""
        ):
            record_build(binary, raw, common, sdk_version="10.0.401")
            with self.assertRaisesRegex(ValueError, "already exists"):
                record_build(binary, raw, common, sdk_version="10.0.401")
            result = record_build(binary, raw, common, sdk_version="10.0.401", replace=True)

        self.assertEqual("verified", result["provenance_status"])


def _run_git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.test",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit_files(root: Path, files: dict[str, str], message: str) -> str:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _run_git(root, "add", "--all")
    _run_git(root, "commit", "-q", "-m", message)
    return _run_git(root, "rev-parse", "HEAD")


@unittest.skipUnless(shutil.which("git"), "git is required")
class PlanTests(unittest.TestCase):
    """Synthetic source repositories shaped like the user's checkouts."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.parent = Path(self.directory.name).resolve()
        self.raw_source = self.parent / "msrawdataworkbench"
        self.raw_source.mkdir()
        _run_git(self.raw_source, "init", "-q", "-b", "master")
        _run_git(self.raw_source, "remote", "add", "origin", "https://example.test/msrawdataworkbench.git")
        ignore = {".gitignore": "bin/\nobj/\n"}
        self.raw_old = _commit_files(
            self.raw_source,
            {**ignore, "RawMetadataConsoleApp/RawMetadataConsoleApp.csproj": "<Project />\n"},
            "extractor",
        )
        # The pin is reachable only from origin/master, as b34c857a5 is in the user's checkout,
        # which is on a branch that does not contain it.
        _run_git(self.raw_source, "checkout", "-q", "-b", "upstream")
        self.raw_pin = _commit_files(self.raw_source, {"RawDataHandlerStandard/Reader.cs": "// pinned\n"}, "pin")
        _run_git(self.raw_source, "update-ref", "refs/remotes/origin/master", self.raw_pin)
        _run_git(self.raw_source, "checkout", "-q", "master")
        _run_git(self.raw_source, "branch", "-q", "-D", "upstream")

        self.common_main = self.parent / "MsdialWorkbench"
        self.common_main.mkdir()
        _run_git(self.common_main, "init", "-q", "-b", "master")
        self.common_without_project = _commit_files(self.common_main, {"README.md": "common\n"}, "start")
        self.common_pin = _commit_files(
            self.common_main,
            {**ignore, "src/Common/CommonStandard/CommonStandard.csproj": "<Project />\n"},
            "common",
        )
        _commit_files(self.common_main, {"README.md": "moved on\n"}, "later")
        # The production Console's tree: a worktree of the moving checkout at the pin.
        self.common_source = self.parent / f"MsdialWorkbench-console-{self.common_pin[:9]}"
        _run_git(self.common_main, "worktree", "add", "-q", "--detach", str(self.common_source), self.common_pin)
        self.worktrees_before = {
            root: _run_git(root, "worktree", "list", "--porcelain") for root in (self.raw_source, self.common_main)
        }

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _plan(self, raw_commit: str = "", common_commit: str = "", **options) -> dict:
        with patch.object(extractor.shutil, "which", return_value="dotnet"):
            return plan_extractor_build(
                raw_commit or self.raw_pin, common_commit or self.common_pin, self.parent, **options
            )

    def test_the_plan_clones_the_local_checkouts_and_changes_nothing(self) -> None:
        plan = self._plan()

        self.assertEqual([], plan["blockers"])
        self.assertEqual([], plan["warnings"])
        self.assertTrue(plan["preview_only"])
        self.assertFalse(plan["executed"])
        build_root = self.parent / f"RawMetadataExtractor-{self.raw_pin[:9]}-{self.common_pin[:9]}"
        self.assertEqual(str(build_root), plan["build_root"])
        self.assertFalse(build_root.exists())
        trees = plan["trees"]
        self.assertEqual(self.raw_pin, trees["msrawdataworkbench"]["commit"])
        self.assertTrue(trees["msrawdataworkbench"]["commit_is_origin_master"])
        self.assertEqual(str(self.common_source), trees["MsdialWorkbench"]["source"])
        self.assertEqual(str(build_root / "MsdialWorkbench"), trees["MsdialWorkbench"]["destination"])
        commands = [step["command"] for step in plan["steps"]]
        self.assertFalse(any("worktree" in command for command in commands))
        clones = [command for command in commands if command[:2] == ["git", "clone"]]
        self.assertEqual(2, len(clones))
        self.assertTrue(all("--no-hardlinks" in command for command in clones))
        self.assertEqual(
            extractor_build_command(build_root / "msrawdataworkbench", "dotnet"), plan["command"]
        )
        self.assertEqual(["EAZFUSCATOR_NET_HOME"], plan["environment_removed"])
        build_step = next(step for step in plan["steps"] if step["command"][1:2] == ["build"])
        self.assertEqual(["EAZFUSCATOR_NET_HOME"], build_step["environment_removed"])
        self.assertEqual(
            str(build_root / "msrawdataworkbench" / "RawMetadataConsoleApp" / "bin" / "Release" / "net48" / "RawMetadataConsoleApp.exe"),
            plan["binary_path"],
        )
        # No fetch is planned: confirming the pin upstream is a network step.
        self.assertFalse(any("fetch" in command for command in commands))
        self.assertIn("fetch", plan["network_steps_not_included"][0]["command"])
        for root, listing in self.worktrees_before.items():
            self.assertEqual(listing, _run_git(root, "worktree", "list", "--porcelain"))

    def test_the_planned_clones_reach_a_commit_only_origin_master_holds_and_record_it(self) -> None:
        plan = self._plan()
        for step in plan["steps"]:
            if step["command"][0] == "git" and "status" not in step["command"]:
                subprocess.run(step["command"], capture_output=True, check=True)
        raw = Path(plan["trees"]["msrawdataworkbench"]["destination"])
        common = Path(plan["trees"]["MsdialWorkbench"]["destination"])

        self.assertEqual(self.raw_pin, _run_git(raw, "rev-parse", "HEAD"))
        self.assertEqual(self.common_pin, _run_git(common, "rev-parse", "HEAD"))
        self.assertEqual("", _run_git(raw, "status", "--porcelain", "--untracked-files=all"))
        # origin names upstream but holds no refs, so no stale origin/master is recorded.
        self.assertEqual("https://example.test/msrawdataworkbench.git", _run_git(raw, "config", "--get", "remote.origin.url"))
        self.assertEqual("", _run_git(raw, "for-each-ref", "refs/remotes/origin"))

        _raw, _common, binary = _write_build(raw.parent, raw_head=self.raw_pin, common_head=self.common_pin)
        self.assertEqual("", _run_git(raw, "status", "--porcelain", "--untracked-files=all"))
        result = record_build(binary, raw, common, plan=plan, sdk_version="10.0.401")

        self.assertEqual("verified", result["provenance_status"])
        self.assertFalse(result["pinned"])
        record = json.loads(Path(result["provenance_path"]).read_text(encoding="utf-8"))
        source = record["sources"]["msrawdataworkbench"]
        self.assertEqual(self.raw_pin, source["head"])
        self.assertTrue(source["matches_pin"])
        self.assertEqual("", source["origin_master_head"])
        self.assertEqual(self.raw_source, Path(source["cloned_from"]).resolve())
        self.assertEqual(self.common_source, Path(record["sources"]["MsdialWorkbench"]["cloned_from"]).resolve())

        (common / "src" / "Common" / "CommonStandard" / "CommonStandard.csproj").write_text("<Project Edited='1' />\n", encoding="utf-8")
        result = record_build(binary, raw, common, plan=plan, sdk_version="10.0.401", replace=True)

        self.assertEqual("dirty_source", result["provenance_status"])
        self.assertEqual(["MsdialWorkbench"], result["dirty_sources"])

    def test_a_commit_or_project_missing_from_a_source_is_a_blocker(self) -> None:
        plan = self._plan(raw_commit="deadbeef1", common_commit=self.common_without_project)

        self.assertTrue(any("commit deadbeef1 is not in" in blocker for blocker in plan["blockers"]))
        self.assertTrue(
            any("CommonStandard.csproj does not exist" in blocker for blocker in plan["blockers"])
        )

    def test_a_folder_inside_a_checkout_is_not_a_source(self) -> None:
        plan = self._plan(raw_source=self.raw_source / "RawMetadataConsoleApp")

        self.assertTrue(any("is not the top of a git working tree" in blocker for blocker in plan["blockers"]))

    def test_a_token_in_the_upstream_url_is_not_carried_into_the_plan(self) -> None:
        _run_git(
            self.raw_source, "remote", "set-url", "origin", "https://synthetic:token-123@example.test/msrawdataworkbench.git"
        )

        plan = self._plan()

        self.assertEqual(
            "https://example.test/msrawdataworkbench.git", plan["trees"]["msrawdataworkbench"]["upstream_url"]
        )
        self.assertNotIn("token-123", json.dumps(plan))

    def test_a_pin_that_is_not_origin_master_warns(self) -> None:
        plan = self._plan(raw_commit=self.raw_old)

        self.assertEqual([], plan["blockers"])
        self.assertTrue(any("is not msrawdataworkbench origin/master" in warning for warning in plan["warnings"]))

    def test_an_existing_build_folder_is_a_blocker(self) -> None:
        build_root = extractor_build_root(self.parent, self.raw_pin, self.common_pin)
        build_root.mkdir()
        (build_root / "leftover.txt").write_text("x", encoding="utf-8")

        plan = self._plan()

        self.assertTrue(any("already exists" in blocker for blocker in plan["blockers"]))

    def test_a_build_folder_inside_a_source_checkout_is_a_blocker(self) -> None:
        with patch.object(extractor.shutil, "which", return_value="dotnet"):
            plan = plan_extractor_build(
                self.raw_pin,
                self.common_pin,
                parent_directory=self.raw_source,
                raw_source=self.raw_source,
                common_source=self.common_source,
            )

        self.assertTrue(any("lies inside the source checkout" in blocker for blocker in plan["blockers"]))

    def test_no_dotnet_is_a_blocker(self) -> None:
        with patch.object(extractor.shutil, "which", return_value=None):
            plan = plan_extractor_build(self.raw_pin, self.common_pin, self.parent)

        self.assertTrue(any("dotnet was not found" in blocker for blocker in plan["blockers"]))

    def test_a_commit_that_is_not_hexadecimal_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            plan_extractor_build("origin/master", self.common_pin, self.parent)


if __name__ == "__main__":
    unittest.main()

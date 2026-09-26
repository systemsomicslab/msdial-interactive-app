"""A retained artifact has to say what produced it.

CLAUDE.md requires software versions in every retained artifact. A version string is
whatever an assembly claims and a path is where a file sat that day; neither identifies
the binary that ran or the 700 MB library it annotated against.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_app.agent_workflow import build_guided_plan
from msdial_app.workflow import file_identity, prepare_run

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "resources" / "msdial_console_param4lipidomics.txt"


class FileIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_two_files_with_the_same_content_have_the_same_identity(self) -> None:
        first = self.root / "a.lbm2"
        second = self.root / "b.lbm2"
        first.write_bytes(b"library content")
        second.write_bytes(b"library content")
        self.assertEqual(file_identity(first)["sha256"], file_identity(second)["sha256"])

    def test_editing_a_file_changes_its_identity(self) -> None:
        path = self.root / "library.lbm2"
        path.write_bytes(b"one")
        before = file_identity(path)["sha256"]
        path.write_bytes(b"two")
        self.assertNotEqual(before, file_identity(path)["sha256"])

    def test_a_missing_file_is_reported_rather_than_raising(self) -> None:
        # A library that has been moved must not stop a run from recording everything
        # else it can still establish.
        identity = file_identity(self.root / "gone.lbm2")
        self.assertEqual("", identity["sha256"])
        self.assertEqual("unreadable", identity["identity_error"])


class ManifestProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "sample.mzML").write_text("", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_bytes(b"not really a console binary")
        self.lbm = self.root / "lab.lbm2"
        self.lbm.write_bytes(b"laboratory library")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _manifest(self, output: str = "out") -> dict:
        plan = build_guided_plan(
            str(self.root),
            {
                "project_type": "lcms",
                "ion_mode": "Negative",
                "target_omics": "Lipidomics",
                "parameter_strategy": "default",
                "execute_rt_correction": False,
                "library_strategy": "existing",
                "libraries": {"lbm_path": str(self.lbm)},
                "run_qa": False,
                "generate_materials_methods": False,
                "console_path": str(self.console),
                "template_path": str(TEMPLATE),
                "output_root": str(self.root / output),
                "class_assignment_confirmed": True,
            },
        )
        self.assertIsNotNone(plan["workflow"], plan["blockers"])
        self.prepared = prepare_run(plan["workflow"])
        return json.loads(
            (Path(self.prepared["run_directory"]) / "run-manifest.json").read_text(
                encoding="utf-8"
            )
        )

    def test_the_manifest_identifies_the_binary_by_checksum(self) -> None:
        manifest = self._manifest()
        self.assertEqual(
            file_identity(self.console)["sha256"], manifest["console"]["binary_sha256"]
        )
        self.assertEqual(self.console.resolve(), Path(manifest["console"]["path"]).resolve())

    def test_the_manifest_identifies_the_library_by_checksum(self) -> None:
        # A laboratory's own LBM2 is in no catalogue, so version, source, doi and licence
        # are legitimately empty. The checksum is then the only thing that says which
        # library the annotations came from.
        manifest = self._manifest()
        libraries = manifest["libraries"]
        self.assertEqual(1, len(libraries))
        self.assertEqual(self.lbm.resolve(), Path(libraries[0]["path"]).resolve())
        self.assertEqual(file_identity(self.lbm)["sha256"], libraries[0]["sha256"])
        self.assertEqual(len(b"laboratory library"), libraries[0]["size"])

    def test_a_binary_with_no_build_record_is_reported_as_unprovenanced(self) -> None:
        manifest = self._manifest()
        self.assertEqual("absent", manifest["software_provenance_status"])
        warning = self.prepared["software_provenance"]["warning"]
        self.assertIn("No build record", warning)

    def test_the_warning_reaches_the_caller_and_not_only_the_file(self) -> None:
        self._manifest()
        provenance = self.prepared["software_provenance"]
        self.assertEqual("absent", provenance["status"])
        self.assertTrue(provenance["binary_sha256"])

    def test_two_net8_launchers_over_different_code_record_different_identities(self) -> None:
        # A net8 MSDIALCUI.exe is an apphost: builds from different commits differ only in
        # its version string, and a dirty rebuild at one HEAD not at all. The code is in the
        # MSDIALCUI.dll beside it, so that is what has to tell the two runs apart.
        recorded = []
        for name, assembly in (("first", b"net8 assembly one"), ("second", b"net8 assembly two")):
            folder = self.root / name
            folder.mkdir()
            self.console = folder / "MSDIALCUI.exe"
            self.console.write_bytes(b"identical apphost")
            (folder / "MSDIALCUI.runtimeconfig.json").write_text("{}", encoding="ascii")
            (folder / "MSDIALCUI.dll").write_bytes(assembly)
            manifest = self._manifest(output=f"out-{name}")
            recorded.append((folder, manifest["console"], self.prepared["software_provenance"]))

        (_, first, first_run), (_, second, second_run) = recorded
        self.assertEqual(first["binary_sha256"], second["binary_sha256"])
        self.assertNotEqual(first["assembly_sha256"], second["assembly_sha256"])
        self.assertNotEqual(first_run["assembly_sha256"], second_run["assembly_sha256"])
        for folder, console, run in recorded:
            dll = folder / "MSDIALCUI.dll"
            self.assertEqual(file_identity(folder / "MSDIALCUI.exe")["sha256"], console["binary_sha256"])
            self.assertEqual(file_identity(dll)["sha256"], console["assembly_sha256"])
            self.assertEqual(dll.resolve(), Path(console["assembly_path"]).resolve())
            self.assertEqual(console["assembly_path"], run["assembly_path"])
            self.assertEqual(console["assembly_sha256"], run["assembly_sha256"])

    def test_a_net48_console_is_its_own_assembly_despite_a_stale_dll_beside_it(self) -> None:
        # Unpacking a net48 archive over a net8 one leaves the net8 dll behind. With no
        # runtimeconfig.json the exe is the assembly, and the dll identifies nothing.
        (self.root / "MSDIALCUI.dll").write_bytes(b"stale net8 assembly")
        manifest = self._manifest()
        console = manifest["console"]
        own = file_identity(self.console)["sha256"]
        self.assertEqual(own, console["binary_sha256"])
        self.assertEqual(own, console["assembly_sha256"])
        self.assertEqual(self.console.resolve(), Path(console["assembly_path"]).resolve())
        self.assertEqual(own, self.prepared["software_provenance"]["assembly_sha256"])


if __name__ == "__main__":
    unittest.main()


class PlanReportsOneThresholdTests(unittest.TestCase):
    """A number in the plan has to be the number that runs."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "sample.wiff").write_text("", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_text("", encoding="ascii")
        self.lbm = self.root / "lab.lbm2"
        self.lbm.write_text("", encoding="ascii")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_the_per_file_value_is_named_as_a_suggestion(self) -> None:
        # It used to be reported as minimum_peak_height on every file, contradicting the
        # single applied value in the same plan and in the method file.
        plan = build_guided_plan(
            str(self.root),
            {
                "project_type": "lcms",
                "ion_mode": "Negative",
                "target_omics": "Lipidomics",
                "parameter_strategy": "default",
                "execute_rt_correction": False,
                "library_strategy": "existing",
                "libraries": {"lbm_path": str(self.lbm)},
                "run_qa": False,
                "generate_materials_methods": False,
                "console_path": str(self.console),
                "template_path": str(TEMPLATE),
                "minimum_peak_height": 300,
                "class_assignment_confirmed": True,
            },
        )
        for item in plan["input"]["files"]:
            self.assertNotIn("minimum_peak_height", item)
            self.assertEqual(100, item["suggested_minimum_peak_height"])
        self.assertEqual(300, plan["workflow"]["minimum_peak_height"])
        for item in plan["workflow"]["files"]:
            self.assertNotIn("minimum_peak_height", item)

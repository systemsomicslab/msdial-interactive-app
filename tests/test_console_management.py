import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from msdial_app.console_management import (
    build_local_console,
    fetch_and_compare_source,
    fetch_official_console_releases,
    prepare_local_console_build,
)
from msdial_app.workflow import CONSOLE_BUILD_PROVENANCE, inspect_console_path


class ConsoleManagementTests(unittest.TestCase):
    @patch("msdial_app.console_management.console_git_state")
    @patch("msdial_app.console_management.subprocess.run")
    def test_fetch_source_updates_remote_tracking_only(
        self, run: Mock, git_state: Mock
    ) -> None:
        run.return_value = Mock(returncode=0, stdout="", stderr="")
        git_state.return_value = {"behind_origin_master": 2}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            project = root / "tests/MSDIAL5/MsdialCoreTestApp/MsdialCoreTestApp.csproj"
            project.parent.mkdir(parents=True)
            project.write_text("<Project />", encoding="ascii")

            result = fetch_and_compare_source(root)

        self.assertEqual(2, result["git"]["behind_origin_master"])
        self.assertEqual(
            ["git", "-C", str(root.resolve()), "fetch", "origin", "--prune"],
            run.call_args.args[0],
        )

    @patch("msdial_app.console_management.urllib.request.urlopen")
    def test_release_check_separates_stable_and_prerelease(self, urlopen: Mock) -> None:
        payload = [
            {
                "tag_name": "MSDIAL-v5.5.260820",
                "name": "MSDIAL-v5.5.260820 console",
                "draft": False,
                "prerelease": True,
                "published_at": "2026-08-20T00:00:00Z",
                "html_url": "https://example.test/preview",
                "assets": [{"name": "MSDIAL.console.preview.zip", "size": 10}],
            },
            {
                "tag_name": "MSDIAL-v5.5.260817",
                "name": "MSDIAL-v5.5.260817",
                "draft": False,
                "prerelease": False,
                "published_at": "2026-08-17T00:00:00Z",
                "html_url": "https://example.test/stable",
                "assets": [{"name": "MSDIAL.console.stable.zip", "size": 20}],
            },
        ]
        response = Mock()
        response.read.return_value = json.dumps(payload).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        urlopen.return_value = response

        result = fetch_official_console_releases()

        self.assertEqual("MSDIAL-v5.5.260817", result["stable"]["tag"])
        self.assertEqual("MSDIAL-v5.5.260820", result["preview"]["tag"])

    @patch("msdial_app.console_management.console_git_state")
    @patch("msdial_app.console_management.shutil.which", return_value="dotnet")
    def test_prepare_local_build_reports_command_and_output(
        self, _which: Mock, git_state: Mock
    ) -> None:
        git_state.return_value = {"available": True, "head": "abc", "short_head": "abc"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "tests/MSDIAL5/MsdialCoreTestApp/MsdialCoreTestApp.csproj"
            project.parent.mkdir(parents=True)
            project.write_text("<Project />", encoding="ascii")

            plan = prepare_local_console_build(root, "net48")

        self.assertIn("build", plan["command"])
        self.assertTrue(plan["output_path"].endswith("Release\\net48\\MSDIALCUI.exe") or plan["output_path"].endswith("Release/net48/MSDIALCUI.exe"))
        self.assertEqual("abc", plan["git"]["head"])

    @patch("msdial_app.workflow.console_capabilities")
    @patch("msdial_app.workflow.console_version", return_value="5.5.0")
    @patch("msdial_app.workflow.console_git_state")
    def test_inspect_source_build_validates_matching_provenance(
        self, git_state: Mock, _version: Mock, capabilities: Mock
    ) -> None:
        git_state.return_value = {
            "available": True,
            "head": "abc123",
            "short_head": "abc123",
            "working_tree_state_sha256": "state",
        }
        capabilities.return_value = {"capability_probe": "test", "capabilities": ["x"]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "MsdialWorkbench"
            (root / ".git").mkdir(parents=True)
            project = root / "tests/MSDIAL5/MsdialCoreTestApp/MsdialCoreTestApp.csproj"
            project.parent.mkdir(parents=True)
            project.write_text("<Project />", encoding="ascii")
            binary = project.parent / "bin/Release/net48/MSDIALCUI.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"console")
            first = inspect_console_path(binary)
            (binary.parent / CONSOLE_BUILD_PROVENANCE).write_text(
                json.dumps(
                    {
                        "binary_sha256": first["binary_sha256"],
                        "git_head": "abc123",
                        "working_tree_state_sha256": "state",
                    }
                ),
                encoding="utf-8",
            )

            result = inspect_console_path(binary)

        self.assertEqual("local_source_build", result["source_kind"])
        self.assertTrue(result["provenance_verified"])
        self.assertTrue(result["matches_recorded_git_head"])

    @patch("msdial_app.console_management.save_path_settings")
    @patch("msdial_app.console_management.inspect_console_path")
    @patch("msdial_app.console_management.console_git_state")
    @patch("msdial_app.console_management.subprocess.Popen")
    def test_build_records_provenance_and_selects_binary(
        self, popen: Mock, git_state: Mock, inspect: Mock, save: Mock
    ) -> None:
        process = Mock()
        process.stdout = io.StringIO("Build succeeded.\n")
        process.wait.return_value = 0
        popen.return_value = process
        git_state.return_value = {
            "head": "abc",
            "branch": "feature",
            "dirty": True,
            "changed_files": 2,
            "working_tree_diff_sha256": "diff",
            "working_tree_state_sha256": "state",
        }
        inspect.side_effect = [
            {"binary_sha256": "hash", "version": "5.5"},
            {"path": "built.exe", "git": {"short_head": "abc"}},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "bin/MSDIALCUI.exe"
            output.parent.mkdir()
            output.write_bytes(b"console")
            plan = {
                "source_root": str(root),
                "framework": "net48",
                "configuration": "Release",
                "command": ["dotnet", "build"],
                "command_text": "dotnet build",
                "output_path": str(output),
            }

            result = build_local_console(plan, lambda _message: None)
            provenance = json.loads(
                (output.parent / CONSOLE_BUILD_PROVENANCE).read_text(encoding="utf-8")
            )

        self.assertTrue(result["selected"])
        self.assertEqual("abc", provenance["git_head"])
        self.assertEqual("state", provenance["working_tree_state_sha256"])
        save.assert_called_once()

    @patch("msdial_app.workflow.console_capabilities")
    @patch("msdial_app.workflow.console_version", return_value="5.5.0")
    def test_a_record_for_another_binary_is_stale_not_absent(
        self, _version: Mock, capabilities: Mock
    ) -> None:
        capabilities.return_value = {"capability_probe": "test", "capabilities": []}
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "MSDIALCUI.exe"
            binary.write_bytes(b"the binary that is actually here")
            (binary.parent / CONSOLE_BUILD_PROVENANCE).write_text(
                json.dumps(
                    {
                        "binary_sha256": "d2959b97a427fc",
                        "git_head": "14dc64ff",
                        "built_at": "2026-09-02T22:13:10+09:00",
                    }
                ),
                encoding="utf-8",
            )
            result = inspect_console_path(binary)

        self.assertEqual("stale_mismatch", result["provenance_status"])
        self.assertFalse(result["provenance_verified"])
        self.assertEqual({}, result["provenance"])
        mismatch = result["provenance_mismatch"]
        self.assertEqual("d2959b97a427fc", mismatch["recorded_binary_sha256"])
        self.assertEqual(result["binary_sha256"], mismatch["actual_binary_sha256"])
        self.assertEqual("14dc64ff", mismatch["recorded_git_head"])
        self.assertEqual("2026-09-02T22:13:10+09:00", mismatch["recorded_built_at"])

    @patch("msdial_app.workflow.console_capabilities")
    @patch("msdial_app.workflow.console_version", return_value="5.5.0")
    def test_no_record_at_all_is_absent(self, _version: Mock, capabilities: Mock) -> None:
        capabilities.return_value = {"capability_probe": "test", "capabilities": []}
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "MSDIALCUI.exe"
            binary.write_bytes(b"console")
            result = inspect_console_path(binary)

        self.assertEqual("absent", result["provenance_status"])
        self.assertFalse(result["provenance_verified"])
        self.assertNotIn("provenance_mismatch", result)

    @patch("msdial_app.workflow.console_capabilities")
    @patch("msdial_app.workflow.console_version", return_value="5.5.0")
    def test_an_unreadable_record_is_not_reported_as_absent(
        self, _version: Mock, capabilities: Mock
    ) -> None:
        capabilities.return_value = {"capability_probe": "test", "capabilities": []}
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "MSDIALCUI.exe"
            binary.write_bytes(b"console")
            (binary.parent / CONSOLE_BUILD_PROVENANCE).write_text("{ not json", encoding="utf-8")
            result = inspect_console_path(binary)

        self.assertEqual("unreadable", result["provenance_status"])
        self.assertFalse(result["provenance_verified"])
        self.assertIn("provenance_mismatch", result)

    @patch("msdial_app.console_management.save_path_settings")
    @patch("msdial_app.console_management.inspect_console_path")
    @patch("msdial_app.console_management.console_git_state")
    @patch("msdial_app.console_management.subprocess.Popen")
    def test_a_failed_build_leaves_no_record_rather_than_a_stale_one(
        self, popen: Mock, git_state: Mock, inspect: Mock, _save: Mock
    ) -> None:
        process = Mock()
        process.stdout = io.StringIO("Build FAILED." + chr(10))
        process.wait.return_value = 1
        popen.return_value = process
        git_state.return_value = {"head": "abc"}
        inspect.return_value = {"binary_sha256": "hash", "version": "5.5"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "bin/MSDIALCUI.exe"
            output.parent.mkdir()
            output.write_bytes(b"console")
            record = output.parent / CONSOLE_BUILD_PROVENANCE
            record.write_text(json.dumps({"binary_sha256": "previous"}), encoding="utf-8")
            plan = {
                "source_root": str(root),
                "framework": "net48",
                "configuration": "Release",
                "command": ["dotnet", "build"],
                "command_text": "dotnet build",
                "output_path": str(output),
            }

            with self.assertRaises(RuntimeError):
                build_local_console(plan, lambda _message: None)

            self.assertFalse(record.is_file())


if __name__ == "__main__":
    unittest.main()

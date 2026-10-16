"""scripts/setup_claude.py: every change is explained, asked for, backed up, and absent from a dry run.

Nothing here installs, registers or starts anything: every subprocess the setup would start goes to a
recorder, and the home folder, Claude's configuration and the checkout are temporary folders.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("setup_claude_under_test", ROOT / "scripts" / "setup_claude.py")
setup = importlib.util.module_from_spec(SPEC)
# Registered first: its dataclasses look their module up while they are made.
sys.modules[SPEC.name] = setup
SPEC.loader.exec_module(setup)

from msdial_app.workflow import (  # noqa: E402
    ANNOTATION_CANDIDATES_CONSOLE_MARKERS,
    AUTOMATIC_RT_LOCAL_SUPPORT_CONSOLE_MARKERS,
)

PYTHON = r"C:\Python312\python.exe"
SECRET = "do-not-print-this-value"


class Recorder:
    """Stands in for subprocess.run: records each command and answers as told."""

    def __init__(self, answers: dict[str, subprocess.CompletedProcess] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append([str(part) for part in command])
        for key, answer in self.answers.items():
            if key in " ".join(str(part) for part in command):
                return answer
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def ran(self, text: str) -> bool:
        return any(text in " ".join(call) for call in self.calls)


def snapshot(folder: Path) -> dict[str, bytes]:
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


class SetupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "checkout"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "scripts" / "msdial-interactive-mcp.py").write_text("# entry point\n", encoding="utf-8")
        for name in ("skill-one", "skill-two"):
            folder = self.root / "skills" / name
            (folder / "references").mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: A test skill.\n---\n# {name}\n", encoding="utf-8")
            (folder / "references" / "notes.md").write_text(f"{name} notes\n", encoding="utf-8")
        self.home = self.base / "home"
        self.home.mkdir()
        self.appdata = self.base / "AppData" / "Roaming"
        self.localappdata = self.base / "AppData" / "Local"
        self.appdata.mkdir(parents=True)
        self.localappdata.mkdir(parents=True)
        self.output: list[str] = []
        self.runner = Recorder()
        self.script = str(self.root / "scripts" / "msdial-interactive-mcp.py")

    def context(self, **overrides: Any) -> Any:
        values: dict[str, Any] = dict(
            root=self.root,
            python=PYTHON,
            home=self.home,
            env={"APPDATA": str(self.appdata), "LOCALAPPDATA": str(self.localappdata)},
            system="Windows",
            drive_roots=[],
            run=self.runner,
            which=lambda name: {"claude": r"C:\npm\claude.CMD"}.get(name),
            interactive=False,
            out=self.output.append,
            now=lambda: setup.dt.datetime(2026, 10, 16, 9, 30, 0),
        )
        values.update(overrides)
        return setup.Context(**values)

    @property
    def printed(self) -> str:
        return "\n".join(self.output)

    def desktop_config(self) -> Path:
        return self.appdata / "Claude" / "claude_desktop_config.json"

    def write_desktop_config(self, value: Any) -> Path:
        path = self.desktop_config()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        return path

    def write_claude_code_config(self, servers: dict[str, Any]) -> Path:
        path = self.home / ".claude.json"
        path.write_text(json.dumps({"numStartups": 3, "mcpServers": servers}), encoding="utf-8")
        return path


class DryRunTests(SetupTestCase):
    def test_a_dry_run_writes_nothing_and_starts_nothing_that_changes_anything(self) -> None:
        self.write_claude_code_config({"msdial-interactive": {"command": "old.exe", "args": ["old.py"], "env": {"KEY": SECRET}}})
        self.write_desktop_config({"preferences": {"a": 1}, "mcpServers": {"other": {"command": "x", "env": {"TOKEN": SECRET}}}})
        old_skill = self.home / ".claude" / "skills" / "skill-one"
        old_skill.mkdir(parents=True)
        (old_skill / "SKILL.md").write_text("an older copy\n", encoding="utf-8")
        console = self.base / "MSDIAL.console" / "MSDIALCUI.exe"
        console.parent.mkdir()
        console.write_bytes(b"not a real console")
        before = snapshot(self.base)
        missing = {name: {"installed": None, "ok": False} for name in setup.DEPENDENCIES}
        with mock.patch.object(setup, "dependency_status", return_value=missing):
            code = setup.run_setup(self.context(dry_run=True, console_paths=[str(console)]))
        self.assertEqual(0, code)
        self.assertEqual(before, snapshot(self.base))
        # Only read-only queries ran: the Console's own help.
        self.assertTrue(all(call[-2:] == ["normalize", "--help"] for call in self.runner.calls), self.runner.calls)
        self.assertFalse(self.runner.ran("pip"))
        self.assertFalse(self.runner.ran("mcp add") or self.runner.ran("mcp remove"))
        self.assertIn("dry run", self.printed)
        self.assertIn("would ask", self.printed)
        self.assertNotIn(SECRET, self.printed)

    def test_python_older_than_310_stops_before_anything_else(self) -> None:
        ctx = self.context()
        result = setup.check_python(ctx, (3, 9, 18))
        self.assertEqual("failed", result.status)
        self.assertIn("3.10", self.printed)
        self.assertEqual("ok", setup.check_python(ctx, (3, 10, 0)).status)


class DependencyTests(SetupTestCase):
    def test_pip_runs_from_the_checkout_only_when_a_package_is_missing_and_only_on_yes(self) -> None:
        missing = {"mcp": {"installed": None, "ok": False}, "psutil": {"installed": "7.0.0", "ok": True}}
        result = setup.install_dependencies(self.context(), missing)
        self.assertEqual("skipped", result.status)  # nobody to answer: no
        self.assertEqual([], self.runner.calls)
        result = setup.install_dependencies(self.context(assume_yes=True), missing)
        self.assertEqual("done", result.status)
        (command,) = self.runner.calls
        self.assertEqual([PYTHON, "-m", "pip", "install"], command[:4])
        self.assertEqual(["-e", ".[mcp]"], command[-2:])

    def test_an_installed_dependency_is_left_alone(self) -> None:
        present = {"mcp": {"installed": "2.1.1", "ok": True}, "psutil": {"installed": "7.2.2", "ok": True}}
        self.assertEqual("unchanged", setup.install_dependencies(self.context(assume_yes=True), present).status)
        self.assertEqual([], self.runner.calls)

    def test_versions_are_compared_with_the_extra_range(self) -> None:
        versions = {"mcp": "1.27.0", "psutil": "7.2.2"}
        status = setup.dependency_status(lambda name: versions[name])
        self.assertFalse(status["mcp"]["ok"])
        self.assertTrue(status["psutil"]["ok"])
        versions["mcp"] = "2.1.1"
        self.assertTrue(setup.dependency_status(lambda name: versions[name])["mcp"]["ok"])


class ClaudeCodeTests(SetupTestCase):
    def test_an_existing_registration_is_shown_and_kept_without_consent(self) -> None:
        self.write_claude_code_config(
            {"msdial-interactive": {"type": "stdio", "command": r"D:\old\python.exe", "args": [r"D:\old\mcp.py"], "env": {"WORKSPACE": SECRET}}}
        )
        result = setup.register_claude_code(self.context())
        self.assertEqual("skipped", result.status)
        self.assertIn("existing registration is kept", result.detail)
        self.assertEqual([], self.runner.calls)
        self.assertIn(r"D:\old\python.exe", self.printed)
        self.assertIn(r"D:\old\mcp.py", self.printed)
        self.assertIn("WORKSPACE", self.printed)
        self.assertNotIn(SECRET, self.printed)

    def test_on_yes_the_existing_registration_is_removed_and_this_one_added_at_user_scope(self) -> None:
        self.write_claude_code_config({"msdial-interactive": {"command": r"D:\old\python.exe", "args": [r"D:\old\mcp.py"]}})
        result = setup.register_claude_code(self.context(assume_yes=True))
        self.assertEqual("done", result.status)
        self.assertEqual(
            [
                [r"C:\npm\claude.CMD", "mcp", "remove", "--scope", "user", "msdial-interactive"],
                [r"C:\npm\claude.CMD", "mcp", "add", "--scope", "user", "msdial-interactive", "--", PYTHON, self.script],
            ],
            self.runner.calls,
        )

    def test_a_new_registration_is_added_on_yes(self) -> None:
        result = setup.register_claude_code(self.context(assume_yes=True))
        self.assertEqual("done", result.status)
        self.assertEqual(
            [[r"C:\npm\claude.CMD", "mcp", "add", "--scope", "user", "msdial-interactive", "--", PYTHON, self.script]],
            self.runner.calls,
        )

    def test_the_same_registration_is_left_alone(self) -> None:
        self.write_claude_code_config({"msdial-interactive": {"type": "stdio", "command": PYTHON.lower(), "args": [self.script]}})
        result = setup.register_claude_code(self.context(assume_yes=True))
        self.assertEqual("unchanged", result.status)
        self.assertEqual([], self.runner.calls)

    def test_without_the_claude_cli_it_says_how_to_register_by_hand(self) -> None:
        result = setup.register_claude_code(self.context(assume_yes=True, which=lambda name: None))
        self.assertEqual("skipped", result.status)
        self.assertEqual([], self.runner.calls)
        self.assertIn("claude mcp add --scope user msdial-interactive --", self.printed)

    def test_a_failed_removal_adds_nothing(self) -> None:
        self.write_claude_code_config({"msdial-interactive": {"command": "old", "args": []}})
        self.runner.answers["mcp remove"] = subprocess.CompletedProcess([], 1, stdout="", stderr="no such server")
        result = setup.register_claude_code(self.context(assume_yes=True))
        self.assertEqual("failed", result.status)
        self.assertFalse(self.runner.ran("mcp add"))

    def test_claude_config_dir_is_honoured(self) -> None:
        custom = self.base / "claude-config"
        custom.mkdir()
        (custom / ".claude.json").write_text(
            json.dumps({"mcpServers": {"msdial-interactive": {"command": PYTHON, "args": [self.script]}}}), encoding="utf-8"
        )
        ctx = self.context(env={"CLAUDE_CONFIG_DIR": str(custom)})
        self.assertEqual("unchanged", setup.register_claude_code(ctx).status)


class DesktopTests(SetupTestCase):
    def test_the_entry_is_merged_after_a_backup_and_every_other_key_is_kept(self) -> None:
        original = {
            "preferences": {"theme": "dark"},
            "mcpServers": {"other": {"command": "x.exe", "args": ["--flag"], "env": {"TOKEN": SECRET}}},
            "zzz": [1, 2],
        }
        path = self.write_desktop_config(original)
        before = path.read_bytes()
        (result,) = setup.configure_desktop(self.context(assume_yes=True))
        self.assertEqual("done", result.status)
        merged = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(["preferences", "mcpServers", "zzz"], list(merged))
        self.assertEqual(original["preferences"], merged["preferences"])
        self.assertEqual(original["zzz"], merged["zzz"])
        self.assertEqual(original["mcpServers"]["other"], merged["mcpServers"]["other"])
        self.assertEqual({"command": PYTHON, "args": [self.script]}, merged["mcpServers"]["msdial-interactive"])
        backup = path.with_name("claude_desktop_config.json.backup-20261016-093000")
        self.assertEqual(before, backup.read_bytes())
        self.assertNotIn(SECRET, self.printed)

    def test_a_differing_entry_is_replaced_only_on_yes(self) -> None:
        path = self.write_desktop_config({"mcpServers": {"msdial-interactive": {"command": "old.exe", "args": ["old.py"]}}})
        before = path.read_bytes()
        (result,) = setup.configure_desktop(self.context())
        self.assertEqual("skipped", result.status)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(["claude_desktop_config.json"], sorted(item.name for item in path.parent.iterdir()))
        self.assertIn("old.exe", self.printed)

    def test_the_same_entry_is_left_alone(self) -> None:
        path = self.write_desktop_config({"mcpServers": {"msdial-interactive": {"command": PYTHON, "args": [self.script]}}})
        before = path.read_bytes()
        (result,) = setup.configure_desktop(self.context(assume_yes=True))
        self.assertEqual("unchanged", result.status)
        self.assertEqual(before, path.read_bytes())

    def test_no_config_is_skipped_unless_desktop_is_asked_for(self) -> None:
        (result,) = setup.configure_desktop(self.context(assume_yes=True))
        self.assertEqual("skipped", result.status)
        self.assertFalse(self.desktop_config().exists())
        (result,) = setup.configure_desktop(self.context(assume_yes=True, desktop=True))
        self.assertEqual("done", result.status)
        self.assertEqual(
            {"mcpServers": {"msdial-interactive": {"command": PYTHON, "args": [self.script]}}},
            json.loads(self.desktop_config().read_text(encoding="utf-8")),
        )

    def test_the_store_installation_config_is_found(self) -> None:
        packaged = self.localappdata / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
        packaged.mkdir(parents=True)
        (packaged / "claude_desktop_config.json").write_text("{}", encoding="utf-8")
        (result,) = setup.configure_desktop(self.context(assume_yes=True))
        self.assertEqual("done", result.status)
        self.assertIn("msdial-interactive", json.loads((packaged / "claude_desktop_config.json").read_text(encoding="utf-8"))["mcpServers"])

    def test_a_config_that_is_not_json_is_left_as_it_is(self) -> None:
        path = self.desktop_config()
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")
        (result,) = setup.configure_desktop(self.context(assume_yes=True))
        self.assertEqual("failed", result.status)
        self.assertEqual("{ not json", path.read_text(encoding="utf-8"))

    def test_macos_location(self) -> None:
        ctx = self.context(system="Darwin")
        self.assertEqual(
            [self.home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"],
            setup.desktop_config_candidates(ctx),
        )


class SkillInstallTests(SetupTestCase):
    def skills_dir(self) -> Path:
        return self.home / ".claude" / "skills"

    def test_skills_are_copied_into_the_claude_code_skills_folder(self) -> None:
        results = setup.install_skills(self.context(assume_yes=True))
        self.assertEqual(["done", "done"], [item.status for item in results])
        for name in ("skill-one", "skill-two"):
            self.assertTrue(setup.same_tree(self.root / "skills" / name, self.skills_dir() / name))

    def test_a_differing_copy_is_backed_up_outside_the_skills_folder_first(self) -> None:
        existing = self.skills_dir() / "skill-one"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("an older copy\n", encoding="utf-8")
        (existing / "local-notes.md").write_text("kept in the backup\n", encoding="utf-8")
        results = setup.install_skills(self.context(assume_yes=True))
        self.assertEqual("done", results[0].status)
        self.assertTrue(setup.same_tree(self.root / "skills" / "skill-one", existing))
        backup = self.home / ".claude" / "skill-backups" / "skill-one-20261016-093000"
        self.assertEqual("an older copy\n", (backup / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual("kept in the backup\n", (backup / "local-notes.md").read_text(encoding="utf-8"))
        self.assertEqual(["skill-one", "skill-two"], sorted(item.name for item in self.skills_dir().iterdir()))

    def test_a_differing_copy_is_kept_without_consent(self) -> None:
        existing = self.skills_dir() / "skill-one"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("an older copy\n", encoding="utf-8")
        results = setup.install_skills(self.context())
        self.assertEqual("skipped", results[0].status)
        self.assertEqual("an older copy\n", (existing / "SKILL.md").read_text(encoding="utf-8"))
        self.assertFalse((self.home / ".claude" / "skill-backups").exists())

    def test_an_identical_copy_is_left_alone(self) -> None:
        setup.install_skills(self.context(assume_yes=True))
        self.output.clear()
        results = setup.install_skills(self.context(assume_yes=True))
        self.assertEqual(["unchanged", "unchanged"], [item.status for item in results])
        self.assertFalse((self.home / ".claude" / "skill-backups").exists())

    def linked_skill(self, make_link: Any) -> tuple[Path, Path]:
        elsewhere = self.base / "another-checkout" / "skill-one"
        elsewhere.mkdir(parents=True)
        (elsewhere / "SKILL.md").write_text("the other checkout's copy\n", encoding="utf-8")
        link = self.skills_dir() / "skill-one"
        link.parent.mkdir(parents=True)
        try:
            make_link(elsewhere, link)
        except (OSError, NotImplementedError):
            self.skipTest("this system does not allow creating that link")
        return elsewhere, link

    def test_a_linked_skill_is_unlinked_and_what_it_points_at_is_untouched(self) -> None:
        elsewhere, link = self.linked_skill(lambda target, path: os.symlink(target, path, target_is_directory=True))
        self.assert_replaced_link(elsewhere, link)

    @unittest.skipUnless(os.name == "nt", "a directory junction is a Windows link")
    def test_a_junctioned_skill_is_unlinked_and_what_it_points_at_is_untouched(self) -> None:
        import _winapi

        elsewhere, link = self.linked_skill(lambda target, path: _winapi.CreateJunction(str(target), str(path)))
        self.assertTrue(setup._is_link(link))
        self.assert_replaced_link(elsewhere, link)

    def test_a_link_to_this_checkouts_own_skill_is_left_alone(self) -> None:
        link = self.skills_dir() / "skill-one"
        link.parent.mkdir(parents=True)
        source = self.root / "skills" / "skill-one"
        try:
            if os.name == "nt":
                import _winapi

                _winapi.CreateJunction(str(source), str(link))
            else:
                os.symlink(source, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this system does not allow creating that link")
        results = setup.install_skills(self.context(assume_yes=True))
        self.assertEqual("unchanged", results[0].status)
        self.assertTrue(setup._is_link(link))

    def assert_replaced_link(self, elsewhere: Path, link: Path) -> None:
        results = setup.install_skills(self.context(assume_yes=True))
        self.assertEqual("done", results[0].status)
        self.assertFalse(setup._is_link(link))
        self.assertTrue(setup.same_tree(self.root / "skills" / "skill-one", link))
        self.assertEqual("the other checkout's copy\n", (elsewhere / "SKILL.md").read_text(encoding="utf-8"))
        record = self.home / ".claude" / "skill-backups" / "skill-one-20261016-093000" / "LINK_TARGET.txt"
        self.assertIn("another-checkout", record.read_text(encoding="utf-8"))

    def test_claude_config_dir_moves_the_skills_folder(self) -> None:
        custom = self.base / "claude-config"
        setup.install_skills(self.context(assume_yes=True, env={"CLAUDE_CONFIG_DIR": str(custom)}))
        self.assertTrue((custom / "skills" / "skill-one" / "SKILL.md").is_file())
        self.assertFalse(self.skills_dir().exists())


class SkillZipTests(SetupTestCase):
    def test_the_zips_are_built_into_dist_on_yes(self) -> None:
        result = setup.build_skill_zips(self.context(assume_yes=True))
        self.assertEqual("done", result.status)
        self.assertEqual(
            ["skill-one.skill.zip", "skill-two.skill.zip"], sorted(path.name for path in (self.root / "dist").iterdir())
        )
        self.assertIn("Customize > Skills", self.printed)

    def test_declined_builds_nothing(self) -> None:
        self.assertEqual("skipped", setup.build_skill_zips(self.context()).status)
        self.assertFalse((self.root / "dist").exists())


class ConsoleTests(SetupTestCase):
    def console(self, assembly: bytes, folder: str = "MSDIAL.console") -> Path:
        path = self.base / folder / "MSDIALCUI.exe"
        path.parent.mkdir(parents=True)
        path.write_bytes(assembly)
        return path

    @staticmethod
    def markers() -> bytes:
        text = " ".join(ANNOTATION_CANDIDATES_CONSOLE_MARKERS + AUTOMATIC_RT_LOCAL_SUPPORT_CONSOLE_MARKERS)
        return b"MZ " + text.encode("utf-16-le")

    def test_a_console_with_every_capability_is_ready(self) -> None:
        console = self.console(self.markers())
        self.runner.answers["normalize --help"] = subprocess.CompletedProcess(
            [], 0, stdout="Options:\n  --peak-ids-only  Take each standard from its PeakID.\n", stderr=""
        )
        result = setup.check_console(self.context(console_paths=[str(console)]))
        self.assertEqual("ok", result.status)
        self.assertEqual([[str(console), "normalize", "--help"]], self.runner.calls)
        self.assertIn("new enough", self.printed)

    def test_a_console_without_peak_ids_only_is_too_old_and_the_way_to_a_new_one_is_said(self) -> None:
        console = self.console(self.markers())
        self.runner.answers["normalize --help"] = subprocess.CompletedProcess([], 0, stdout="Options:\n  -d\n", stderr="")
        result = setup.check_console(self.context(console_paths=[str(console)]))
        self.assertEqual("attention", result.status)
        self.assertIn("--peak-ids-only", self.printed)
        self.assertIn(setup.CONSOLE_RELEASES_URL, self.printed)
        self.assertIn("msdial_build_console_from_local_source", self.printed)

    def test_the_assembly_capabilities_are_checked_too(self) -> None:
        console = self.console(b"MZ an old console")
        self.runner.answers["normalize --help"] = subprocess.CompletedProcess([], 0, stdout="--peak-ids-only", stderr="")
        verdict = setup.probe_console(self.context(), console)
        self.assertFalse(verdict["ready"])
        self.assertEqual(
            ["the annotation candidate table", "automatic RT correction's local test (MsdialWorkbench#826)"],
            verdict["missing"],
        )

    def test_the_saved_console_setting_is_read(self) -> None:
        console = self.console(self.markers(), "saved")
        settings = self.localappdata / "MSDIALInteractive" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"console_path": str(console)}), encoding="utf-8")
        found = setup.console_candidates(self.context())
        self.assertIn(("Interactive's saved setting", console), found)

    def test_no_console_found_points_to_the_prerelease_and_the_source_build(self) -> None:
        with mock.patch.object(setup, "console_candidates", return_value=[]):
            result = setup.check_console(self.context())
        self.assertEqual("attention", result.status)
        self.assertEqual([], self.runner.calls)
        self.assertIn(setup.CONSOLE_PRERELEASE_LABEL, self.printed)
        self.assertIn(setup.MSDIAL_SOURCE_URL, self.printed)


class AnswerTests(SetupTestCase):
    def test_answers(self) -> None:
        answers = iter(["", "maybe", "n", "yes"])
        ctx = self.context(interactive=True, ask_input=lambda prompt: next(answers))
        self.assertTrue(ctx.ask("Add it?", True))  # empty answer: the default
        self.assertFalse(ctx.ask("Replace it?", False))  # "maybe" is asked again, then "n"
        self.assertTrue(ctx.ask("Replace it?", False))
        self.assertIn("Please answer y or n.", self.printed)


if __name__ == "__main__":
    unittest.main()

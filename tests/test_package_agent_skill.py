"""scripts/package-agent-skill.py: one upload ZIP per skill, each with its skill folder at the root."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_NAMES = ["msdial-guided-analysis", "msdial-lipidomics-workflow"]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


packager = load("package_agent_skill_under_test", "package-agent-skill.py")


def write_skill(root: Path, folder: str, frontmatter: str, extra: dict[str, str] | None = None) -> Path:
    skill = root / folder
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n# {folder}\n", encoding="utf-8")
    for relative, text in (extra or {}).items():
        path = skill / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return skill


class BundledSkillsTests(unittest.TestCase):
    def test_both_skills_are_bundled_with_valid_frontmatter(self) -> None:
        self.assertEqual(SKILL_NAMES, packager.skill_names())
        for name in SKILL_NAMES:
            fields = packager.skill_frontmatter(ROOT / "skills" / name)
            self.assertEqual(name, fields["name"])
            self.assertTrue(fields["description"])
            self.assertLessEqual(len(fields["description"]), packager.DESCRIPTION_LIMIT)

    def test_every_skill_packages_into_its_own_zip_with_its_folder_at_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            written = packager.package_skills(None, out)
            self.assertEqual([f"{name}.skill.zip" for name in SKILL_NAMES], [path.name for path in written])
            self.assertTrue(all(path.parent.samefile(out) for path in written))
            for name in SKILL_NAMES:
                source = ROOT / "skills" / name
                with zipfile.ZipFile(out / f"{name}.skill.zip") as archive:
                    names = archive.namelist()
                    self.assertTrue(all(entry.startswith(f"{name}/") for entry in names), names)
                    self.assertIn(f"{name}/SKILL.md", names)
                    expected = sorted(f"{name}/{path.relative_to(source).as_posix()}" for path in packager.skill_files(source))
                    self.assertEqual(expected, sorted(names))
                    for entry in names:
                        relative = entry.split("/", 1)[1]
                        self.assertEqual((source / relative).read_bytes(), archive.read(entry), entry)

    def test_the_guided_analysis_zip_keeps_its_name_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = packager.package_skill(ROOT / "skills" / "msdial-guided-analysis", Path(temporary))
            self.assertEqual("msdial-guided-analysis.skill.zip", target.name)
            with zipfile.ZipFile(target) as archive:
                self.assertIn("msdial-guided-analysis/references/tool-reference.md", archive.namelist())

    def test_packaging_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            left = packager.package_skill(ROOT / "skills" / "msdial-lipidomics-workflow", Path(first))
            right = packager.package_skill(ROOT / "skills" / "msdial-lipidomics-workflow", Path(second))
            self.assertEqual(left.read_bytes(), right.read_bytes())

    def test_one_skill_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                code = packager.main(["--out-dir", temporary, "--skill", "msdial-lipidomics-workflow"])
            self.assertEqual(0, code)
            self.assertEqual(["msdial-lipidomics-workflow.skill.zip"], sorted(path.name for path in Path(temporary).iterdir()))
            self.assertIn("msdial-lipidomics-workflow.skill.zip", printed.getvalue())

    def test_an_unknown_name_is_refused_and_nothing_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                code = packager.main(["--out-dir", temporary, "--skill", "no-such-skill"])
            self.assertEqual(2, code)
            self.assertIn("no-such-skill", errors.getvalue())
            self.assertEqual([], list(Path(temporary).iterdir()))

    def test_list_writes_nothing(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(0, packager.main(["--list"]))
        self.assertEqual(SKILL_NAMES, printed.getvalue().split())


class SkillFolderTests(unittest.TestCase):
    def test_caches_are_left_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            skill = write_skill(
                root, "demo-skill", "name: demo-skill\ndescription: A demo.",
                {"references/a.md": "a", "__pycache__/x.pyc": "x", "scripts/tool.pyc": "y"},
            )
            target = packager.package_skill(skill, Path(temporary) / "out")
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(["demo-skill/SKILL.md", "demo-skill/references/a.md"], sorted(archive.namelist()))

    def test_frontmatter_must_name_its_own_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            write_skill(root, "folder-name", "name: another-name\ndescription: A demo.")
            with self.assertRaises(packager.SkillError):
                packager.package_skills(["folder-name"], Path(temporary) / "out", root)
            self.assertFalse((Path(temporary) / "out" / "folder-name.skill.zip").exists())

    def test_frontmatter_needs_a_description_and_a_valid_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            write_skill(root, "no-description", "name: no-description")
            write_skill(root, "Bad_Name", "name: Bad_Name\ndescription: A demo.")
            for name in ("no-description", "Bad_Name"):
                with self.assertRaises(packager.SkillError):
                    packager.skill_frontmatter(root / name)

    def test_crlf_frontmatter_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / "crlf-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_bytes(b"---\r\nname: crlf-skill\r\ndescription: Windows line ends.\r\n---\r\n")
            self.assertEqual("crlf-skill", packager.skill_frontmatter(skill)["name"])


class DistributionTests(unittest.TestCase):
    def test_the_distribution_carries_the_setup_the_examples_and_both_skills(self) -> None:
        distribution = load("build_distribution_under_test", "build-distribution.py")
        names = {path.relative_to(distribution.ROOT).as_posix() for path in distribution.iter_files()}
        for expected in (
            "Setup MS-DIAL for Claude.cmd",
            "scripts/setup_claude.py",
            "scripts/package-agent-skill.py",
            "examples/lab-lipidomics/quant_utility_table.xlsx",
            "examples/lab-lipidomics/README.md",
            "docs/tutorials/lab-lipidomics.md",
            "skills/msdial-guided-analysis/SKILL.md",
            "skills/msdial-lipidomics-workflow/SKILL.md",
        ):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()

"""Package the Agent Skills under skills/ as upload ZIPs, one per skill.

    python scripts/package-agent-skill.py                  every skill -> dist/<name>.skill.zip
    python scripts/package-agent-skill.py --skill NAME     one skill (repeat --skill for more)
    python scripts/package-agent-skill.py --list           the skill names, nothing written

Each ZIP holds one folder named after the skill, with SKILL.md at its top, which is the layout
Claude Desktop's Customize > Skills upload and other Agent Skills hosts expect. A skill is a
folder of skills/ that holds a SKILL.md whose frontmatter names it (name equal to the folder name)
and describes it. Entries are written in path order with a fixed timestamp, so the same skill
always packages to the same bytes.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
DEFAULT_OUT_DIR = ROOT / "dist"
SKILL_FILE = "SKILL.md"
ZIP_SUFFIX = ".skill.zip"
SKIPPED_PARTS = {"__pycache__", ".DS_Store"}
SKIPPED_SUFFIXES = {".pyc", ".pyo"}
# Agent Skills limits: a name of lowercase letters, digits and hyphens, at most 64 characters, and a
# description of at most 1024 characters.
NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
DESCRIPTION_LIMIT = 1024
# 1980-01-01, the earliest time a ZIP entry can carry: the archive does not depend on checkout times.
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)


class SkillError(ValueError):
    """A folder under skills/ that cannot be packaged as a skill."""


def skill_names(skills_root: Path = SKILLS) -> list[str]:
    """Every folder of skills_root that holds a SKILL.md, by name."""
    if not skills_root.is_dir():
        return []
    return sorted(path.name for path in skills_root.iterdir() if (path / SKILL_FILE).is_file())


def skill_frontmatter(skill_dir: Path) -> dict[str, str]:
    """The name and description a SKILL.md declares, checked against the folder that holds it."""
    path = skill_dir / SKILL_FILE
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise SkillError(f"{path} cannot be read: {error}") from None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"{path} does not start with a '---' frontmatter block.")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        raise SkillError(f"{path}: the frontmatter block is not closed with '---'.") from None
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#") or line.startswith((" ", "\t")):
            continue
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip().strip("\"'")
    name = fields.get("name", "")
    description = fields.get("description", "")
    if not NAME_PATTERN.match(name):
        raise SkillError(f"{path}: name '{name}' is not lowercase letters, digits and hyphens (at most 64).")
    if name != skill_dir.name:
        raise SkillError(f"{path}: name '{name}' differs from its folder '{skill_dir.name}'.")
    if not description:
        raise SkillError(f"{path}: the frontmatter has no description.")
    if len(description) > DESCRIPTION_LIMIT:
        raise SkillError(f"{path}: the description is {len(description)} characters; at most {DESCRIPTION_LIMIT}.")
    return {"name": name, "description": description}


def skill_files(skill_dir: Path) -> list[Path]:
    """The files a skill's package holds, in path order, without caches."""
    return sorted(
        path
        for path in skill_dir.rglob("*")
        if path.is_file()
        and not SKIPPED_PARTS.intersection(path.relative_to(skill_dir).parts)
        and path.suffix not in SKIPPED_SUFFIXES
    )


def package_skill(skill_dir: Path, out_dir: Path = DEFAULT_OUT_DIR) -> Path:
    """Write <out_dir>/<name>.skill.zip with the skill's folder at its root; returns its path."""
    skill_dir = Path(skill_dir).resolve()
    name = skill_frontmatter(skill_dir)["name"]
    out_dir = Path(out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{name}{ZIP_SUFFIX}"
    temporary = target.with_name(target.name + ".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in skill_files(skill_dir):
            entry = zipfile.ZipInfo(f"{name}/{path.relative_to(skill_dir).as_posix()}", date_time=FIXED_DATE_TIME)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, path.read_bytes())
    temporary.replace(target)
    return target


def package_skills(
    names: list[str] | None = None, out_dir: Path = DEFAULT_OUT_DIR, skills_root: Path = SKILLS
) -> list[Path]:
    """Package the named skills, or every skill when names is empty; an unknown name is refused."""
    available = skill_names(skills_root)
    wanted = list(dict.fromkeys(names or available))
    unknown = [name for name in wanted if name not in available]
    if unknown:
        raise SkillError(
            f"No skill named {', '.join(unknown)} under {skills_root}; the skills are: {', '.join(available) or 'none'}."
        )
    if not wanted:
        raise SkillError(f"No skill folder with a {SKILL_FILE} under {skills_root}.")
    return [package_skill(skills_root / name, out_dir) for name in wanted]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package the MS-DIAL Agent Skills for upload.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="where the ZIPs are written (default: dist)")
    parser.add_argument(
        "--skill", action="append", default=[], metavar="NAME",
        help="package only this skill; repeat for more (default: every skill under skills/)",
    )
    parser.add_argument("--list", action="store_true", help="print the skill names and write nothing")
    args = parser.parse_args(argv)
    if args.list:
        for name in skill_names():
            print(name)
        return 0
    try:
        targets = package_skills(args.skill, Path(args.out_dir))
    except SkillError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for target in targets:
        print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())

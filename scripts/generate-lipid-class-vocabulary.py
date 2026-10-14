"""Generate resources/lipid_class_vocabulary.tsv from the MS-DIAL source that defines it.

MS-DIAL spells a lipid class two ways. The LbmClass enum (`Cer_NS`, `PE_Cer`) is what the LBM
library, the `Searched lipid class` line and an export's Ontology column carry; the "VS2" class
definition (`Cer-NS`, `PE-Cer`) is what normalization looks a standard up by. Two switches in
LipidomicsConverter.cs convert between them, and a quantification table that uses the wrong
spelling in the wrong sheet falls silently to "Any others". Interactive validates both sheets
against these switches, so the table is generated from them rather than typed in.

Only active code is read: comments are removed first, because LipidomicsConverter.cs keeps whole
retired versions of both switches commented out beside the live ones.

    python scripts/generate-lipid-class-vocabulary.py <MsdialWorkbench checkout> [--commit <sha>]
    python scripts/generate-lipid-class-vocabulary.py <checkout> --check   # compare, write nothing

The checkout is only read. Without --commit the commit is taken from `git rev-parse HEAD` there.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "resources" / "lipid_class_vocabulary.tsv"
ENUM_SOURCE = Path("src/Common/CommonStandard/Database/LbmQuery.cs")
CONVERTER_SOURCE = Path("src/Common/CommonStandard/Lipidomics/LipidomicsConverter.cs")
ENUM_TO_VS2 = "public static string ConvertLbmClassEnumToMsdialClassDefinitionVS2(LbmClass"
NAME_TO_ENUM = "public static LbmClass ConvertMsdialClassDefinitionToLbmClassEnumVS2(string"
UNDEFINED = "Undefined"
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def strip_comments(source: str) -> str:
    """C# source with // and /* */ comments removed, string and character literals kept intact."""
    output: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        following = source[index + 1] if index + 1 < length else ""
        if char == "/" and following == "/":
            end = source.find("\n", index)
            index = length if end < 0 else end
            continue
        if char == "/" and following == "*":
            end = source.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if char in "\"'":
            # A URL inside a string literal is not a comment.
            end = index + 1
            while end < length and source[end] != char:
                end += 2 if source[end] == "\\" else 1
            output.append(source[index:end + 1])
            index = end + 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _block_after(source: str, marker: str) -> str:
    start = source.find(marker)
    if start < 0:
        raise ValueError(f"'{marker}' was not found in active code.")
    if source.find(marker, start + 1) >= 0:
        raise ValueError(f"'{marker}' appears more than once in active code.")
    opening = source.find("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index]
    raise ValueError(f"The block after '{marker}' is not closed.")


def parse_lbm_class_enum(source: str) -> list[str]:
    """The LbmClass members, in declaration order."""
    body = _block_after(strip_comments(source), "public enum LbmClass")
    members = [token.split("=")[0].strip() for token in body.split(",")]
    members = [member for member in members if member]
    for member in members:
        if not IDENTIFIER.match(member):
            raise ValueError(f"'{member}' is not an enum member name.")
    if len(set(members)) != len(members):
        raise ValueError("LbmClass declares a member twice.")
    return members


def _cases(body: str, label: str, value: str) -> list[tuple[str, str]]:
    """(case label, returned value) pairs, with stacked labels sharing the next return."""
    pairs: list[tuple[str, str]] = []
    pending: list[str] = []
    token = re.compile(rf"case\s+{label}\s*:|return\s+{value}\s*;|default\s*:")
    for match in token.finditer(body):
        text = match.group(0)
        if text.startswith("case"):
            pending.append(match.group(1))
        elif text.startswith("return"):
            returned = match.group(match.lastindex)
            pairs.extend((item, returned) for item in pending)
            pending = []
        else:
            break
    if pending:
        raise ValueError(f"case labels without a return: {pending}")
    return pairs


def parse_enum_to_vs2(source: str) -> dict[str, str]:
    """ConvertLbmClassEnumToMsdialClassDefinitionVS2 as a table; absent members return Undefined."""
    body = _block_after(strip_comments(source), ENUM_TO_VS2)
    pairs = _cases(body, r"LbmClass\.([A-Za-z0-9_]+)", r'"([^"]*)"')
    table: dict[str, str] = {}
    for member, name in pairs:
        if member in table:
            raise ValueError(f"LbmClass.{member} is converted twice.")
        table[member] = name
    if 'default: return "Undefined"' not in re.sub(r"\s+", " ", body):
        raise ValueError("The enum-to-VS2 switch no longer defaults to Undefined.")
    return table


def parse_name_to_enum(source: str) -> dict[str, str]:
    """ConvertMsdialClassDefinitionToLbmClassEnumVS2 as a table; absent names return Undefined."""
    body = _block_after(strip_comments(source), NAME_TO_ENUM)
    pairs = _cases(body, r'"([^"]*)"', r"LbmClass\.([A-Za-z0-9_]+)")
    table: dict[str, str] = {}
    for name, member in pairs:
        if name in table:
            raise ValueError(f"'{name}' is converted twice.")
        table[name] = member
    return table


def vocabulary_lines(checkout: Path, commit: str) -> list[str]:
    members = parse_lbm_class_enum((checkout / ENUM_SOURCE).read_text(encoding="utf-8-sig"))
    converter = (checkout / CONVERTER_SOURCE).read_text(encoding="utf-8-sig")
    to_vs2 = parse_enum_to_vs2(converter)
    to_enum = parse_name_to_enum(converter)
    unknown = sorted(set(to_vs2) - set(members)) + sorted(set(to_enum.values()) - set(members))
    if unknown:
        raise ValueError(f"The switches name members LbmClass does not declare: {unknown}")
    lines = [
        f"# Generated by scripts/generate-lipid-class-vocabulary.py from MsdialWorkbench {commit}",
        f"# ({ENUM_SOURCE.as_posix()}, {CONVERTER_SOURCE.as_posix()}). Do not edit by hand.",
        "# enum_to_vs2: every LbmClass member in declaration order, and the class definition",
        "#   ConvertLbmClassEnumToMsdialClassDefinitionVS2 returns for it (Undefined: no case).",
        "# name_to_enum: every name ConvertMsdialClassDefinitionToLbmClassEnumVS2 accepts.",
        "table\tfrom\tto",
    ]
    lines.extend(f"enum_to_vs2\t{member}\t{to_vs2.get(member, UNDEFINED)}" for member in members)
    lines.extend(f"name_to_enum\t{name}\t{member}" for name, member in to_enum.items())
    return lines


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("checkout", type=Path, help="an MsdialWorkbench checkout, read only")
    parser.add_argument("--commit", default="", help="the checkout's commit, recorded in the header")
    parser.add_argument("--check", action="store_true", help="compare with the resource; write nothing")
    arguments = parser.parse_args()
    commit = arguments.commit.strip()
    if not commit:
        result = subprocess.run(
            ["git", "-C", str(arguments.checkout), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        commit = result.stdout.strip() if result.returncode == 0 else "(unknown commit)"
    try:
        lines = vocabulary_lines(arguments.checkout, commit)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    text = "\n".join(lines) + "\n"
    if arguments.check:
        current = OUTPUT.read_text(encoding="ascii") if OUTPUT.exists() else ""
        # The commit line differs between checkouts of identical content.
        same = [line for line in current.splitlines() if not line.startswith("#")] == [
            line for line in lines if not line.startswith("#")
        ]
        print("unchanged" if same else "differs")
        return 0 if same else 1
    OUTPUT.write_text(text, encoding="ascii", newline="\n")
    pairs = sum(1 for line in lines if line.startswith("enum_to_vs2\t"))
    print(f"wrote {OUTPUT} ({pairs} LbmClass members, commit {commit})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

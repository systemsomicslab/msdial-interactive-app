"""Merge a positive-mode and a negative-mode MS-DIAL alignment into one lipidome.

A lipid class is quantified in whichever polarity and adduct measures it best, and
that choice is a per-class decision recorded in a rule table rather than anything the
data can decide for itself. Merging is therefore: keep the rows whose ontology and
adduct the table selects, from both polarities, and concatenate them.

The two exports must describe the same samples in the same order, because the merged
table keeps one set of sample columns and silently pairs them by position. Two runs
that were aligned separately can disagree -- a file excluded in one polarity, a
different injection order -- and nothing downstream would notice. That is checked
here and refused, rather than left to be discovered in the statistics.

Two lipid classes are annotated differently in the two polarities, and the laboratory
reconciles them before the selection (reconcile_polarities): a bis(monoacylglycero)phosphate
(BMP) is annotatable only in positive mode and is annotated as PG in negative mode, and a
plasmenyl PE (PE P-) and a plasmanyl PE (PE O-) are both annotated PE O- in negative mode.
A negative row that positive mode names as BMP or PE P- is renamed, so that sheet 3 can take
the class from the negative [M-H]- measurement.

The outputs are what the laboratory's own merge program wrote (metaboservice,
MetabolomicsServiceUtility.IntegratePosNegResults): lipid_metabolome.csv, the three tables
metabolomics_service.R reads, always under metaboservice's names (lipid_metabolome4stat.csv,
...4classsummary.csv and ...4summaryplot.csv), and the Summary_Dotplots, category_<category> and
subclass_<ontology> folders it reads too. ASCII, CRLF, comma separated and never quoted; rows in
the order .NET sorted them on Windows (nls_sort_key); numbers as .NET Framework printed a double
(net_double_tostring). Rows are chosen as metaboservice's isKeepLipid chose them: on the cells as
written, untrimmed, case-sensitive. On the 2024 hands-on exports and the rule table that produced
them, with the reconciliation off and the 2024 sample rule, every file is metaboservice's byte for
byte but one digit of one computed value (see net_double_tostring).

One deliberate difference: a cell outside ASCII is written as UTF-8 and warned about, where
metaboservice wrote one '?' per byte (two Japanese class labels of the same length would have
become one class). Rows are still ordered as metaboservice ordered those '?'.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Collection, Iterable

ROOT = Path(__file__).resolve().parent.parent
CATEGORY_TABLE_PATH = ROOT / "resources" / "lipid_category_table.tsv"

HEADER_ROW_COUNT = 5
CLASS_ROW = 0
FILE_TYPE_ROW = 1
INJECTION_ORDER_ROW = 2
COLUMN_NAME_ROW = 4

ALIGNMENT_ID_COLUMN = 0
RT_COLUMN = 1
METABOLITE_NAME_COLUMN = 3
ADDUCT_COLUMN = 4
FORMULA_COLUMN = 10
ONTOLOGY_COLUMN = 11
COMMENT_COLUMN = 18

# A row the merged lipidome never carries, whatever the rule table says.
INTERNAL_STANDARD_NAME_FRAGMENT = "(d"
EXCLUDED_NAME_FRAGMENTS = ("w/o", "RIKEN")
# Exports before MS-DIAL 2026-09-15 named suggestions this way. Current exports strip the
# prefix, so this filter no longer sees them; the candidate table does (reference_matched_ids).
EXCLUDED_NAME_PREFIXES = ("low score: ", "no MS2: ")
# comment.StartsWith("x") in isKeepLipid: lower-case x at the very start of the cell as written.
MANUAL_EXCLUSION_COMMENT_PREFIX = "x"
# Rows the rule table selects but that carry no valid concentration. They go to their own
# file rather than vanishing into a count, because a selected class with no numbers is a
# finding about the standards, not about the lipidome.
NOT_QUANTIFIED_REASON = "not quantified"
NOT_QUANTIFIED_COMMENT_PREFIX = "NOT QUANTIFIED"

# The columns the four tables and the dotplots are computed over. metaboservice took Sample
# columns until 2025 and Sample and QC since (the laboratory's decision, 2026-10-14); a Blank
# never. The 2024 rule is kept for reproducing the files the 2024 program wrote.
SAMPLE_FILE_TYPES = ("Sample", "QC")
METABOSERVICE_2024_SAMPLE_FILE_TYPES = ("Sample",)

# What metaboservice wrote beside lipid_metabolome.csv (cs:590-593), always under these names:
# metabolomics_service.R reads them by name, whatever the lipidome itself is called.
METABOSERVICE_TABLE_NAMES = {
    "stat": "lipid_metabolome4stat.csv",
    "class_summary": "lipid_metabolome4classsummary.csv",
    "summary_plot": "lipid_metabolome4summaryplot.csv",
}
# readReferenceFile (cs:916-931) keeps a rule only when its fourth field is exactly this.
SELECTED_FLAG = "TRUE"
# Spellings a person may mean as selected, which metaboservice did not take: said, never taken.
NEAR_SELECTED_FLAGS = frozenset({"true", "1", "yes"})
SUMMARY_DOTPLOTS = "Summary_Dotplots"
CATEGORY_PREFIX = "category_"
SUBCLASS_PREFIX = "subclass_"
# exportCsv4Dotplots writes a lipid's file only when it ranks within the top 41 by summed value.
DOTPLOT_MAX_RANK = 40
# Path.GetInvalidFileNameChars() on Windows, which metaboservice replaced with '_' in a file name.
INVALID_FILE_NAME_CHARACTERS = frozenset('"<>|:*?\\/') | {chr(code) for code in range(32)}

DEFAULT_RECONCILIATION_RT_TOLERANCE = 0.1


class MergeRefused(ValueError):
    """The two exports cannot be merged as they stand."""


# ---------------------------------------------------------------------------------------------
# .NET Framework behaviour the laboratory's program depended on
# ---------------------------------------------------------------------------------------------

INFINITY_SIGN = chr(0x221E)
NAN_SYMBOL = "NaN"                        # LOCALE_SNAN for en-US, en-GB and ja-JP on Windows 10/11
POSITIVE_INFINITY = INFINITY_SIGN         # LOCALE_SPOSINFINITY; the ASCII writer wrote it as "?"
NEGATIVE_INFINITY = "-" + INFINITY_SIGN   # LOCALE_SNEGINFINITY; written as "-?"

_NET_FLOAT = re.compile(r"^([+-]?)((?:\d[\d,]*)?)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$")


def net_double_parse(text: str) -> float:
    """double.Parse(text) under a '.'-decimal, ','-group culture (en-*, ja-JP).

    Raises ValueError where .NET throws FormatException: an empty cell, or text that is not a number.
    """
    value = text.strip(" \t\n\v\f\r")
    if value == NAN_SYMBOL:
        return math.nan
    if value in (POSITIVE_INFINITY, "+" + POSITIVE_INFINITY):
        return math.inf
    if value == NEGATIVE_INFINITY:
        return -math.inf
    match = _NET_FLOAT.match(value)
    if not match or not ((match.group(2) or "").replace(",", "") or match.group(3)):
        raise ValueError(f"double.Parse would throw FormatException on {text!r}")
    sign, integer, fraction, exponent = match.groups()
    rebuilt = sign + ((integer or "").replace(",", "") or "0")
    if fraction:
        rebuilt += "." + fraction
    if exponent:
        rebuilt += "e" + exponent
    return float(rebuilt)


def net_double_tostring(value: float) -> str:
    """.NET Framework 4.x Double.ToString(): 'G' with 15 significant digits.

    The exact binary value is rounded half-up to 15 significant digits (what x64 .NET
    Framework does; Python's '%.15G' rounds an exact tie to even instead). Fixed notation
    when the decimal exponent is in [-5, 14], otherwise d.ddddE+XX with at least two exponent
    digits; trailing zeros are dropped; -0.0 is "0"; NaN is "NaN"; an infinity is the culture's
    symbol, which number_text turns into what the ASCII writer wrote.

    One divergence cannot be reproduced: metaboservice.exe is flagged 32BITREQUIRED, so it ran on
    the x86 CLR, whose private x87 DoubleToNumber rounds UP some values lying within about 0.16
    ulp below a 15-digit midpoint (216 of 60,000 uniformly random doubles, 0.36 %, never the
    other way). Such a value differs by one unit in the 15th significant digit. Only computed
    values can meet it (class sums, the summary plot's log10 means); a value re-printed from
    MS-DIAL's own 15-digit text cannot.
    """
    if value != value:
        return NAN_SYMBOL
    if value == math.inf:
        return POSITIVE_INFINITY
    if value == -math.inf:
        return NEGATIVE_INFINITY
    if value == 0.0:
        return "0"  # .NET Framework drops the sign of -0.0 (.NET Core 3.0+ does not)
    # '%.15G' is correctly rounded; it differs from half-up only on an exact tie, which shows as
    # "...5" then "0" in the 16th and 17th significant digits.
    probe = "%.16e" % abs(value)
    if probe[16] == "5" and probe[17] == "0":
        exact = Decimal(abs(value))
        rounded = exact.quantize(Decimal(1).scaleb(exact.adjusted() - 14), rounding=ROUND_HALF_UP)
        return "%.15G" % math.copysign(float(rounded), value)
    return "%.15G" % value


def number_text(value: float) -> str:
    """A double as metaboservice wrote it: ToString() through an ASCII StreamWriter (-inf is "-?")."""
    return net_double_tostring(value).replace(INFINITY_SIGN, "?")


def _net_compare_double_key(value: float) -> tuple[int, float]:
    # Comparer<double>.Default: NaN below everything and equal to NaN.
    return (0, 0.0) if value != value else (1, value)


# Primary weights of LCMapStringEx(LCMAP_SORTKEY | NORM_LINGUISTIC_CASING) for ASCII, read from
# Windows 11 and identical for en-US, en-GB, ja-JP and invariant except the backslash (ja-JP
# sorts it as the yen sign). Letters share one weight across case; case goes to the tertiary
# level (lower 02 < upper 12). "'" and "-" (and control characters) have no primary weight
# ("word sort"): they only break ties, by position, at the special-weight level. Symbols sort
# before digits before letters. Checked byte for byte against Windows on 152,169 strings.
_PRIMARY = {
    " ": b"\x07\x02", "!": b"\x07\x1c", '"': b"\x07\x1d", "#": b"\x07\x1f", "$": b"\x07\x21",
    "%": b"\x07\x23", "&": b"\x07\x25", "(": b"\x07\x27", ")": b"\x07\x2a", "*": b"\x07\x2d",
    "+": b"\x08\x03", ",": b"\x07\x2f", ".": b"\x07\x33", "/": b"\x07\x35", ":": b"\x07\x37",
    ";": b"\x07\x3a", "<": b"\x08\x0e", "=": b"\x08\x12", ">": b"\x08\x14", "?": b"\x07\x3c",
    "@": b"\x07\x3e", "[": b"\x07\x3f", "\\": b"\x07\x41", "]": b"\x07\x42", "^": b"\x07\x43",
    "_": b"\x07\x44", "`": b"\x07\x48", "{": b"\x07\x4a", "|": b"\x07\x4c", "}": b"\x07\x4e",
    "~": b"\x07\x50",
    "\t": b"\x07\x05", "\n": b"\x07\x06", "\x0b": b"\x07\x07", "\x0c": b"\x07\x08", "\r": b"\x07\x09",
}
for _digit, _weight in zip("0123456789", (0x03, 0x1A, 0x1C, 0x1E, 0x20, 0x22, 0x24, 0x26, 0x28, 0x2A)):
    _PRIMARY[_digit] = bytes((0x0D, _weight))
for _letter, _weight in zip(
    "abcdefghijklmnopqrstuvwxyz",
    (0x02, 0x09, 0x0A, 0x1A, 0x21, 0x23, 0x25, 0x2C, 0x32, 0x35, 0x36, 0x48, 0x51,
     0x70, 0x7C, 0x7E, 0x89, 0x8A, 0x91, 0x99, 0x9F, 0xA2, 0xA4, 0xA6, 0xA7, 0xA9),
):
    _PRIMARY[_letter] = bytes((0x0E, _weight))
    _PRIMARY[_letter.upper()] = bytes((0x0E, _weight))
_SPECIAL = {"'": 0x80, "-": 0x82}
for _control in list(range(0x01, 0x09)) + list(range(0x0E, 0x20)) + [0x7F]:
    _SPECIAL[chr(_control)] = {0x7F: 0x1D}.get(_control, _control + (2 if _control < 9 else -3))


def nls_sort_key(text: str) -> bytes:
    """The Windows sort key .NET's default string comparison ordered metaboservice's rows by.

    LINQ OrderBy with Comparer<string>.Default is the current culture's CompareInfo.Compare,
    which on Windows is NLS "word sort": 'BileAcid' before 'BRSE', 'TG 56:1|' before 'TG 56:10|',
    '-' and "'" ignored but for a final tie-break. Sorting by these bytes, stably, is that order.
    A character outside ASCII reached metaboservice as one '?' per UTF-8 byte (it read ASCII), and
    is weighted so; the process locale is never touched.
    """
    primary = bytearray()
    case: list[int] = []
    special = bytearray()
    seen = 0
    for char in text:
        if ord(char) >= 0x80:
            count = len(char.encode("utf-8", "replace"))
            primary += _PRIMARY["?"] * count
            case.extend([0x02] * count)
            seen += count
            continue
        weight = _PRIMARY.get(char)
        if weight is not None:
            primary += weight
            case.append(0x12 if "A" <= char <= "Z" else 0x02)
            seen += 1
        elif char in _SPECIAL:
            position = 0xFFFF - seen
            special += bytes((position >> 8 & 0xFF, position & 0xFF, _SPECIAL[char], 0x12))
        # NUL has no weight at all.
    while case and case[-1] == 0x02:
        case.pop()
    return bytes(primary) + b"\x01\x01" + bytes(case) + b"\x01\x01" + bytes(special) + b"\x00"


# The order the merged rows are written in; one comparison for every sort here.
collation_key = nls_sort_key


@lru_cache(maxsize=4)
def load_lipid_category_table(path: str | None = None) -> tuple[dict[str, str], str, str]:
    """metaboservice's ConvertMsdialClassDefinitionToSuperClassVS2 as (cases, default, source).

    source is the converter the table was generated from, by path below the metaboservice source
    folder and sha256 (scripts/generate-lipid-category-table.py).
    """
    source = Path(path) if path else CATEGORY_TABLE_PATH
    cases: dict[str, str] = {}
    default = None
    origin = ""
    for line in source.read_text(encoding="ascii").splitlines():
        if line.startswith("#"):
            if not origin and line.startswith("# source: "):
                origin = line[len("# source: "):].strip()
            continue
        cells = line.split("\t")
        if len(cells) != 3 or cells[0] == "kind":
            continue
        if cells[0] == "case":
            cases[cells[1]] = cells[2]
        elif cells[0] == "default":
            default = cells[2]
    if not cases or default is None:
        raise MergeRefused(f"{source} holds no lipid category table.")
    return cases, default, origin


def lipid_category(ontology: str) -> str:
    """The lipid category metaboservice filed an ontology's dotplots under.

    From the converter metaboservice was built with, not current MS-DIAL's: a class newer than it
    (WE, NATau, ASHexCer, DGTA, ...) is "Unassigned lipid", as metaboservice filed it, where
    MsdialWorkbench now names a category.
    """
    cases, default, _ = load_lipid_category_table()
    return cases.get(ontology, default)


# ---------------------------------------------------------------------------------------------
# The rule table and the exports
# ---------------------------------------------------------------------------------------------


def read_adduct_rules(path: str | Path) -> dict[str, Any]:
    """Read the class/adduct table that says which measurement of a class to keep.

    Tab-separated, one row per class and adduct, with a column saying whether that
    combination is the selected one. Only selected rows are returned. A text table is read as
    metaboservice's readReferenceFile read it: a row is selected only when its flag is exactly
    TRUE, and Class and Adduct are compared as written, untrimmed. A flag such as 'true', '1' or
    'yes' selects nothing and is warned about. A quant_utility_table workbook (.xlsx) is read from
    its third sheet, which holds the same four columns as cells (parse_quant_info).
    """
    if Path(path).suffix.lower() == ".xlsx":
        return _rules_from_workbook(path)
    rows = _read_rows(path)
    if not rows:
        raise MergeRefused(f"The adduct rule table is empty: {path}")
    header = [cell.strip().lower() for cell in rows[0]]

    def column(*names: str, default: int = -1) -> int:
        for name in names:
            if name in header:
                return header.index(name)
        return default

    class_column = column("class", "ontology", default=0)
    mode_column = column("ion mode", "ionmode", "polarity", default=2)
    adduct_column = column("adduct", "adduct type", default=1)
    selected_column = column("isselected", "selected", default=3)

    selected: set[tuple[str, str]] = set()
    considered = 0
    by_mode: dict[str, int] = {}
    near: list[str] = []
    for number, row in enumerate(rows[1:], start=2):
        if len(row) <= max(class_column, adduct_column, selected_column):
            continue
        considered += 1
        flag = row[selected_column]
        if flag != SELECTED_FLAG:
            if flag.strip().lower() in NEAR_SELECTED_FLAGS:
                near.append(f"line {number} {row[class_column]} {row[adduct_column]} ({flag!r})")
            continue
        selected.add((row[class_column], row[adduct_column]))
        mode = row[mode_column].strip() if 0 <= mode_column < len(row) else ""
        by_mode[mode or "unspecified"] = by_mode.get(mode or "unspecified", 0) + 1
    flag_warnings = []
    if near:
        flag_warnings.append(
            f"{len(near)} row(s) of the rule table are flagged as if selected but not exactly {SELECTED_FLAG} "
            f"({'; '.join(near[:5])}). metaboservice selected only {SELECTED_FLAG}, and so does this merge: "
            f"they are not selected. Write {SELECTED_FLAG} where a row is meant."
        )
    if not selected:
        raise MergeRefused(
            f"No class/adduct combination is selected in {path}; "
            f"{considered} row(s) were read and every one is unselected (none is flagged exactly {SELECTED_FLAG})."
            + (" " + flag_warnings[0] if flag_warnings else "")
        )
    return {
        "path": str(Path(path).resolve()),
        "selected": selected,
        "flag_warnings": flag_warnings,
        "rule_count": considered,
        # An annotation search list and a quantification selection list have the same four
        # columns, and passing one for the other is accepted in silence -- it just keeps
        # far more rows. These counts make the substitution visible before the numbers are
        # read: a selection list keeps roughly one adduct per class per polarity.
        "selected_by_ion_mode": dict(sorted(by_mode.items())),
        "selected_classes": len({item[0] for item in selected}),
    }


def _rules_from_workbook(path: str | Path) -> dict[str, Any]:
    from .lab_lipidomics import LabInputError, parse_quant_info, read_xlsx

    try:
        workbook = read_xlsx(path)
    except LabInputError as error:
        raise MergeRefused(str(error)) from None
    if len(workbook.sheets) < 3:
        raise MergeRefused(f"{path} has no third sheet to read the class/adduct selection from.")
    rules = parse_quant_info(workbook.sheets[2], source=workbook.path)
    if rules["blockers"]:
        raise MergeRefused(f"The selection in {path} cannot be used: " + " ".join(rules["blockers"]))
    return rules


def describe_export(path: str | Path) -> dict[str, Any]:
    """The samples an MS-DIAL alignment export describes, in the order it lists them."""
    rows = _read_rows(path)
    if len(rows) <= HEADER_ROW_COUNT:
        raise MergeRefused(f"{path} has no data rows below its {HEADER_ROW_COUNT} header lines.")
    class_row = rows[CLASS_ROW]
    try:
        label_index = class_row.index("Class")
    except ValueError:
        raise MergeRefused(
            f"{path} does not look like an MS-DIAL alignment export: "
            "its first line names no 'Class' column."
        ) from None

    start = label_index + 1
    names: list[str] = []
    classes: list[str] = []
    column_names = rows[COLUMN_NAME_ROW]
    file_types = rows[FILE_TYPE_ROW]
    for index in range(start, len(class_row)):
        # The statistics columns that follow the samples are labelled NA here. metaboservice's
        # set_metadata stops there and nowhere else: a sample whose class is empty, or "Na", is
        # still a sample.
        if class_row[index] == "NA":
            break
        label = class_row[index].strip()
        name = column_names[index].strip() if index < len(column_names) else ""
        kind = file_types[index].strip() if index < len(file_types) else ""
        if not (label or name or kind):
            break  # an empty cell at the end of the line, not a column
        names.append(name)
        classes.append(label)
    if not names:
        raise MergeRefused(f"{path} lists no sample columns.")
    def column(name: str, fallback: int) -> int:
        # The constants describe the format as it stands; the header is what this file
        # actually says. Reading the name first means a changed export shifts nothing.
        try:
            return column_names.index(name)
        except ValueError:
            return fallback

    return {
        "path": str(Path(path).resolve()),
        "first_sample_column": start,
        "columns": {
            "alignment_id": column("Alignment ID", ALIGNMENT_ID_COLUMN),
            "rt": column("Average Rt(min)", RT_COLUMN),
            "metabolite_name": column("Metabolite name", METABOLITE_NAME_COLUMN),
            "adduct": column("Adduct type", ADDUCT_COLUMN),
            "formula": column("Formula", FORMULA_COLUMN),
            "ontology": column("Ontology", ONTOLOGY_COLUMN),
            "comment": column("Comment", COMMENT_COLUMN),
        },
        "sample_names": names,
        "sample_classes": classes,
        "file_types": [
            file_types[start + offset].strip() if start + offset < len(file_types) else ""
            for offset in range(len(names))
        ],
        "header": rows[:HEADER_ROW_COUNT],
        "rows": rows[HEADER_ROW_COUNT:],
    }


def compare_sample_order(positive: dict[str, Any], negative: dict[str, Any]) -> dict[str, Any]:
    """Whether the two exports describe the same samples in the same order.

    Sample names differ by the polarity they were acquired in, so they are compared
    with that suffix removed; everything else must match position by position.
    """
    left = [strip_polarity(name) for name in positive["sample_names"]]
    right = [strip_polarity(name) for name in negative["sample_names"]]
    problems: list[str] = []
    if len(left) != len(right):
        problems.append(
            f"positive lists {len(left)} sample(s) and negative lists {len(right)}"
        )
    for index in range(min(len(left), len(right))):
        if left[index] != right[index]:
            problems.append(
                f"position {index + 1}: positive '{positive['sample_names'][index]}' "
                f"and negative '{negative['sample_names'][index]}' are different samples"
            )
    if positive["first_sample_column"] != negative["first_sample_column"]:
        # Rows from both exports are concatenated and then read positionally, so two
        # exports whose metadata blocks are different widths would put every negative
        # value under the wrong sample -- quietly, and in every row.
        problems.append(
            f"the sample columns begin at index {positive['first_sample_column']} in positive "
            f"and {negative['first_sample_column']} in negative, so the two tables are not "
            "the same shape"
        )
    if positive.get("columns") != negative.get("columns"):
        problems.append(
            "the two exports place their metadata columns differently: "
            f"{positive.get('columns')} against {negative.get('columns')}"
        )
    for index in range(min(len(left), len(right))):
        if positive["sample_classes"][index] != negative["sample_classes"][index]:
            problems.append(
                f"position {index + 1}: class '{positive['sample_classes'][index]}' "
                f"in positive and '{negative['sample_classes'][index]}' in negative"
            )
    for index in range(min(len(positive.get("file_types", [])), len(negative.get("file_types", [])))):
        # The positive header decides which columns are samples, for the negative rows too.
        if positive["file_types"][index] != negative["file_types"][index]:
            problems.append(
                f"position {index + 1}: file type '{positive['file_types'][index]}' "
                f"in positive and '{negative['file_types'][index]}' in negative"
            )
    return {
        "matched": not problems,
        "problems": problems,
        "positive_sample_count": len(left),
        "negative_sample_count": len(right),
        "paired_samples": [
            {
                "position": index + 1,
                "positive": positive["sample_names"][index],
                "negative": negative["sample_names"][index],
                "class": positive["sample_classes"][index],
            }
            for index in range(min(len(left), len(right)))
        ],
    }


# ---------------------------------------------------------------------------------------------
# Which rows
# ---------------------------------------------------------------------------------------------


def _default_columns() -> dict[str, int]:
    return {
        "alignment_id": ALIGNMENT_ID_COLUMN,
        "rt": RT_COLUMN,
        "metabolite_name": METABOLITE_NAME_COLUMN,
        "adduct": ADDUCT_COLUMN,
        "formula": FORMULA_COLUMN,
        "ontology": ONTOLOGY_COLUMN,
        "comment": COMMENT_COLUMN,
    }


def _field(row: list[str], columns: dict[str, int], key: str) -> str:
    """A cell trimmed, for what the report says and for the merge's own comparisons."""
    index = columns.get(key, -1)
    return row[index].strip() if 0 <= index < len(row) else ""


def _cell(row: list[str], columns: dict[str, int], key: str) -> str:
    """A cell as the file holds it, untrimmed: what metaboservice's isKeepLipid compared."""
    index = columns.get(key, -1)
    return row[index] if 0 <= index < len(row) else ""


def row_filter_reason(
    row: list[str],
    columns: dict[str, int] | None = None,
    *,
    internal_standard_ids: Collection[str] | None = None,
    reference_matched_ids: Collection[str] | None = None,
) -> str:
    """Why a row is no lipid of the lipidome whatever sheet 3 selects; "" when it may be one.

    These run before the positive/negative reconciliation: a row they drop is evidence for nothing.
    The name and Comment tests are metaboservice's isKeepLipid (2025), on the cells as written:
    untrimmed and case-sensitive, so a Comment starting 'X' or ' x' keeps its row, as it did there.
    An empty name, the resolver's internal-standard IDs and the reference-match filter are this
    merge's own additions.
    """
    columns = columns or _default_columns()
    if len(row) <= columns["ontology"]:
        return "row is too short to carry an ontology"
    alignment_id = _field(row, columns, "alignment_id")
    name = _cell(row, columns, "metabolite_name")
    comment = _cell(row, columns, "comment")
    if not name.strip() or name == "Unknown":
        return "unannotated"
    if (internal_standard_ids is not None and alignment_id in internal_standard_ids) or (
        INTERNAL_STANDARD_NAME_FRAGMENT in name
    ):
        # The standards quantify the sample; they are not part of it. The resolver's IDs
        # catch a standard named without its label; "(d" catches a deuterated name.
        return "internal standard"
    if any(fragment in name for fragment in EXCLUDED_NAME_FRAGMENTS):
        # "w/o" an unresolved position, "RIKEN" an in-house identifier that names no compound.
        return "excluded compound name"
    if any(name.startswith(prefix) for prefix in EXCLUDED_NAME_PREFIXES):
        return "annotation below the reporting bar"
    if reference_matched_ids is not None and alignment_id not in reference_matched_ids:
        return "not reference-matched"
    if comment.startswith(MANUAL_EXCLUSION_COMMENT_PREFIX):
        return "manually excluded in the comment column"
    return ""


def selection_reason(
    row: list[str],
    rules: dict[str, Any],
    columns: dict[str, int] | None = None,
    *,
    expected_unit: str | None = None,
    unquantified_ids: Collection[str] | None = None,
    value_columns: Iterable[int] | None = None,
) -> str:
    """Why sheet 3 does not take a row, or why it has no valid concentration; "" when it is kept.

    A row without a valid concentration is refused with a reason starting "not quantified",
    which the merge writes to its own file instead of dropping. Ontology and Adduct are matched
    against the rules as written, untrimmed, as isKeepLipid matched them.
    """
    columns = columns or _default_columns()
    alignment_id = _field(row, columns, "alignment_id")
    adduct = _cell(row, columns, "adduct")
    ontology = _cell(row, columns, "ontology")
    comment = _field(row, columns, "comment")
    if (ontology, adduct) not in rules["selected"]:
        return f"{ontology or '(no ontology)'} is not quantified as {adduct or '(no adduct)'}"
    if comment.startswith(NOT_QUANTIFIED_COMMENT_PREFIX):
        return f"{NOT_QUANTIFIED_REASON}: its standard did not resolve"
    unit = _comment_unit(comment)
    if unit == "Intensity":
        return f"{NOT_QUANTIFIED_REASON}: exported as raw intensity"
    if expected_unit and unit and unit != expected_unit:
        return f"{NOT_QUANTIFIED_REASON}: its unit is {unit}, not {expected_unit}"
    if unquantified_ids is not None and alignment_id in unquantified_ids:
        return f"{NOT_QUANTIFIED_REASON}: its values failed verification"
    if value_columns is not None:
        empty = 0
        for index in value_columns:
            try:
                net_double_parse(row[index] if index < len(row) else "")
            except ValueError:
                empty += 1
        if empty:
            # metaboservice stopped on such a cell; here the row is set aside and said.
            return f"{NOT_QUANTIFIED_REASON}: {empty} sample or QC cell(s) hold no number"
    return ""


def should_keep(
    row: list[str],
    rules: dict[str, Any],
    columns: dict[str, int] | None = None,
    *,
    internal_standard_ids: Collection[str] | None = None,
    reference_matched_ids: Collection[str] | None = None,
    expected_unit: str | None = None,
    unquantified_ids: Collection[str] | None = None,
) -> tuple[bool, str]:
    """Whether one aligned row belongs in the merged lipidome, and why not if it does not.

    The first reason that applies is the one given: row_filter_reason, then selection_reason.
    The merge runs the two apart, with the positive/negative reconciliation between them.
    """
    reason = row_filter_reason(
        row, columns, internal_standard_ids=internal_standard_ids, reference_matched_ids=reference_matched_ids
    ) or selection_reason(row, rules, columns, expected_unit=expected_unit, unquantified_ids=unquantified_ids)
    return not reason, reason


# ---------------------------------------------------------------------------------------------
# Positive/negative reconciliation (the laboratory's rules, 2026-10-14)
# ---------------------------------------------------------------------------------------------

_PLASMENYL_PE = re.compile(r"^PE P-(\d+):(\d+)(.*)$", re.DOTALL)
_PLASMANYL_PE = re.compile(r"^PE O-(\d+):(\d+)(.*)$", re.DOTALL)


def _swap_class_token(name: str, old: str, new: str) -> str | None:
    # The class token is a '|' part's first word; only that word changes, in every part naming it.
    parts = []
    changed = False
    for part in name.split("|"):
        token, space, rest = part.partition(" ")
        if token == old and space:
            parts.append(new + space + rest)
            changed = True
        else:
            parts.append(part)
    return "|".join(parts) if changed else None


def bmp_to_pg_name(name: str) -> str | None:
    """The name negative mode gives a BMP: BMP -> PG in every '|' part (None if no part is BMP)."""
    return _swap_class_token(name, "BMP", "PG")


def pg_to_bmp_name(name: str) -> str | None:
    return _swap_class_token(name, "PG", "BMP")


def plasmenyl_to_plasmanyl_name(name: str) -> str | None:
    """The PE O- name negative mode gives a PE P-: 'P-x:y' -> 'O-x:(y+1)' in every '|' part.

    A plasmenyl chain carries one double bond more than its plasmanyl spelling shows:
    PE P-18:0_16:0 is PE O-18:1_16:0, PE P-34:1 is PE O-34:2. None unless every part is PE P-.
    """
    parts = []
    for part in name.split("|"):
        match = _PLASMENYL_PE.match(part)
        if not match:
            return None
        parts.append(f"PE O-{match.group(1)}:{int(match.group(2)) + 1}{match.group(3)}")
    return "|".join(parts)


def plasmanyl_to_plasmenyl_name(name: str) -> str | None:
    """The inverse: 'O-x:y' -> 'P-x:(y-1)' in every '|' part; None unless every part can be PE P-."""
    parts = []
    for part in name.split("|"):
        match = _PLASMANYL_PE.match(part)
        if not match or int(match.group(2)) < 1:
            return None
        parts.append(f"PE P-{match.group(1)}:{int(match.group(2)) - 1}{match.group(3)}")
    return "|".join(parts)


# Each rule: the positive ontology whose annotation is the evidence, the negative ontology it is
# annotated as, how the positive name reads in negative mode, and what the negative row becomes.
RECONCILIATION_RULES = (
    {
        "rule": "BMP",
        "positive_ontology": "BMP",
        "negative_ontology": "PG",
        "negative_name": bmp_to_pg_name,
        "renamed_ontology": "BMP",
        "renamed_name": pg_to_bmp_name,
        "statement": (
            "BMP is annotatable only in positive mode, and negative mode annotates a BMP as PG: a kept "
            "negative PG row whose name is a kept positive BMP row's with BMP -> PG in every '|' part, "
            "within the RT tolerance, is renamed BMP (name and Ontology). BMP and PG are both taken "
            "from negative [M-H]-."
        ),
    },
    {
        "rule": "EtherPE",
        "positive_ontology": "EtherPE",
        "negative_ontology": "EtherPE",
        "negative_name": plasmenyl_to_plasmanyl_name,
        "renamed_ontology": "EtherPE",
        "renamed_name": plasmanyl_to_plasmenyl_name,
        "statement": (
            "Negative mode annotates PE P- (plasmenyl) and PE O- (plasmanyl) both as PE O-; positive mode "
            "tells them apart. A kept negative EtherPE row whose name is a kept positive PE P- row's in its "
            "PE O- spelling (P-x:y -> O-x:(y+1) in every '|' part), within the RT tolerance, is renamed to "
            "the PE P- name. EtherPE is taken from negative [M-H]-."
        ),
    },
)
RECONCILED_CLASSES = tuple(rule["rule"] for rule in RECONCILIATION_RULES)


def reconciliation_parameters(enabled: bool = True, rt_tolerance: float = DEFAULT_RECONCILIATION_RT_TOLERANCE) -> dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "rt_tolerance_min": rt_tolerance,
        "name_match": "exact, after the class substitution only",
        "rules": {rule["rule"]: rule["statement"] for rule in RECONCILIATION_RULES},
        "ties": (
            "several negative candidates: the smallest |delta RT|, then the higher mean intensity over the "
            "sample and QC columns; one negative row claimed by several positive rows: the smallest "
            "|delta RT| wins (then the higher mean intensity of the positive row), and a positive row that loses "
            "takes its next candidate no other positive row is closer to, or is left unmatched when none is left "
            "(lost_claims). The claims are settled together, so the pairs do not depend on the rows' order"
        ),
        "conflicting_evidence": (
            "a renamed negative row whose own name positive mode also annotates within the tolerance (a positive "
            "PE O- of the same name, or a positive PG of the same name) is renamed as the rule says and reported "
            "as ambiguous: the two positive annotations disagree"
        ),
        "selection": (
            "the reconciliation renames and reports; it keeps and drops nothing. Sheet 3 decides: BMP and "
            "EtherPE positive adducts FALSE, BMP [M-H]- and EtherPE [M-H]- TRUE"
        ),
    }


def reconciliation_selection_warnings(
    selected: Iterable[tuple[str, str]], source: str = "sheet 3", *, bound: str | None = None
) -> list[str]:
    """What in a selection contradicts the reconciliation: a positive BMP or EtherPE, or no [M-H]-.

    Without bound, each warning says how to change the selection: the selection can still change.
    With bound (why it cannot change any more, as for sheet 3 once a contract holds the workbook),
    each one says what this lipidome counts, followed by that sentence.
    """
    selected = {(klass.strip(), adduct.strip()) for klass, adduct in selected}
    warnings: list[str] = []
    tail = f" {bound}" if bound else ""
    for klass in RECONCILED_CLASSES:
        positive = sorted(adduct for name, adduct in selected if name == klass and adduct.endswith("+"))
        if positive:
            if bound:
                warnings.append(
                    f"{source} selects {klass} {', '.join(positive)} in positive mode, so this lipidome counts {klass} "
                    f"from both polarities: the positive rows, and the negative [M-H]- rows the positive/negative "
                    f"reconciliation takes {klass} from.{tail}"
                )
            else:
                warnings.append(
                    f"{source} selects {klass} {', '.join(positive)} in positive mode. The merge's positive/negative "
                    f"reconciliation takes {klass} from negative [M-H]- (positive mode only names which negative rows "
                    f"are {klass}{' rather than PG' if klass == 'BMP' else ' plasmenyl'}), so {klass} would be counted "
                    "from both polarities: set the positive row(s) FALSE."
                )
        if (klass, "[M-H]-") not in selected:
            if bound:
                consequence = (
                    "the negative PG rows the reconciliation renamed BMP are in neither BMP nor PG"
                    if klass == "BMP" else "no EtherPE is kept from negative mode, renamed or not"
                )
                warnings.append(f"{source} does not select {klass} [M-H]-, so in this lipidome {consequence}.{tail}")
            else:
                consequence = (
                    "the negative PG rows it renames BMP would then be in neither BMP nor PG"
                    if klass == "BMP" else "no EtherPE would be kept from negative mode, renamed or not"
                )
                warnings.append(
                    f"{source} does not select {klass} [M-H]-, which the merge's positive/negative reconciliation "
                    f"takes {klass} from: {consequence}. Set {klass} [M-H]- TRUE."
                )
    return warnings


def _decimal_rt(text: str) -> Decimal | None:
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _mean(row: list[str], value_columns: list[int]) -> float:
    values = []
    for index in value_columns:
        try:
            values.append(net_double_parse(row[index] if index < len(row) else ""))
        except ValueError:
            continue
    values = [value for value in values if value == value]
    return sum(values) / len(values) if values else math.nan


def _intensity_order(value: float) -> float:
    # Higher first; a row without a number last.
    return -value if value == value else math.inf


def _delta_text(delta: Decimal) -> str:
    return format(delta, "f")


def reconcile_polarities(
    positive_rows: list[list[str]],
    negative_rows: list[list[str]],
    columns: dict[str, int] | None = None,
    value_columns: Iterable[int] = (),
    *,
    rt_tolerance: float = DEFAULT_RECONCILIATION_RT_TOLERANCE,
) -> tuple[list[list[str]], dict[str, Any]]:
    """Rename the negative rows positive mode identifies as BMP or PE P-; keep and drop nothing.

    positive_rows and negative_rows are the rows that passed row_filter_reason, in file order.
    Returns the negative rows (a renamed row is a new list; the others are the same objects) and
    the report: each matched pair, each positive row left unmatched (with the negative rows that
    nearly matched), each positive row with several candidates (ambiguous, with the one it was
    finally given), each positive row that lost every candidate to positive rows closer in RT
    (lost_claims), each renamed negative row whose own name positive mode also annotates
    (ambiguous, conflicting positive evidence), and each positive row of the class whose name names
    no negative row (not_converted: a PE O- positive mode names itself, with the negative rows of that
    very name, or a BMP without a BMP token).

    The claims are settled together: every positive row asks for its best candidate (smallest
    |delta RT|, then the more intense negative row); a negative row asked for twice goes to the closer
    positive row (then the more intense one, then the earlier), and the other asks for its next
    candidate. The pairs therefore do not depend on the rows' order, and a negative row is left
    unrenamed only when every positive row naming it has a closer pair.

    A renamed row's Comment says what it was. For each rule, positive_rows = matched + unmatched +
    lost_claims; rematched (within matched) counts the positive rows that lost a candidate and took
    another; positive_rows_not_converted is the length of its not_converted entries. Ontologies and
    names are compared as the cells are written, untrimmed: exact means exact.
    """
    columns = columns or _default_columns()
    value_columns = list(value_columns)
    if not (isinstance(rt_tolerance, (int, float)) and math.isfinite(rt_tolerance) and rt_tolerance >= 0):
        raise MergeRefused(f"The reconciliation RT tolerance must be a number of minutes >= 0, not {rt_tolerance!r}.")
    tolerance = Decimal(repr(float(rt_tolerance)))
    renamed_rows = list(negative_rows)

    def identity(row: list[str]) -> dict[str, Any]:
        rt = _decimal_rt(_field(row, columns, "rt"))
        return {
            "alignment_id": _field(row, columns, "alignment_id"), "name": _field(row, columns, "metabolite_name"),
            "adduct": _field(row, columns, "adduct"), "rt": float(rt) if rt is not None else None,
        }

    def row_id(row: list[str]) -> str:
        return _field(row, columns, "alignment_id")

    report: dict[str, Any] = {
        **reconciliation_parameters(True, rt_tolerance),
        "counts": {}, "matched": [], "unmatched": [], "ambiguous": [], "lost_claims": [], "not_converted": [],
    }
    for rule in RECONCILIATION_RULES:
        counts = {"positive_rows": 0, "matched": 0, "unmatched": 0, "lost_claims": 0, "rematched": 0,
                  "ambiguous": 0, "conflicting_positive_evidence": 0, "positive_rows_not_converted": 0}
        negatives = [
            (index, row, _decimal_rt(_field(row, columns, "rt")))
            for index, row in enumerate(negative_rows)
            if _cell(row, columns, "ontology") == rule["negative_ontology"]
        ]
        negative_rt = {index: rt for index, _, rt in negatives}
        negative_means: dict[int, float] = {}

        def negative_mean(index: int) -> float:
            if index not in negative_means:
                negative_means[index] = _mean(negative_rows[index], value_columns)
            return negative_means[index]

        claimants: list[dict[str, Any]] = []
        not_converted: list[list[str]] = []
        for order, row in enumerate(positive_rows):
            if _cell(row, columns, "ontology") != rule["positive_ontology"]:
                continue
            name = _cell(row, columns, "metabolite_name")
            expected = rule["negative_name"](name)
            if expected is None:
                # A positive EtherPE that is not a PE P- name (a PE O- in positive mode) is no evidence for a
                # rename; a positive BMP whose name has no BMP token should not occur, and is listed with it.
                not_converted.append(row)
                continue
            counts["positive_rows"] += 1
            rt = _decimal_rt(_field(row, columns, "rt"))
            if rt is None:
                report["unmatched"].append({
                    "rule": rule["rule"], "positive": identity(row), "negative_name": expected,
                    "reason": "it has no Average Rt(min) to compare",
                })
                counts["unmatched"] += 1
                continue
            candidates = [
                (abs(other_rt - rt), index)
                for index, negative, other_rt in negatives
                if other_rt is not None and _cell(negative, columns, "metabolite_name") == expected
                and abs(other_rt - rt) <= tolerance
            ]
            if not candidates:
                same_name = sorted(
                    (abs(other_rt - rt), index) for index, negative, other_rt in negatives
                    if other_rt is not None and _cell(negative, columns, "metabolite_name") == expected
                )
                nearby = sorted(
                    (abs(other_rt - rt), index) for index, negative, other_rt in negatives
                    if other_rt is not None and abs(other_rt - rt) <= tolerance
                )[:5]
                report["unmatched"].append({
                    "rule": rule["rule"], "positive": identity(row), "negative_name": expected,
                    "reason": (
                        f"the negative {expected} lies {_delta_text(same_name[0][0])} min away"
                        if same_name else f"no kept negative {rule['negative_ontology']} row is named {expected}"
                    ),
                    "same_name_outside_tolerance": [
                        {**identity(negative_rows[index]), "delta_rt": float(delta)} for delta, index in same_name[:3]
                    ],
                    "other_names_within_tolerance": [
                        {**identity(negative_rows[index]), "delta_rt": float(delta)} for delta, index in nearby
                    ],
                })
                counts["unmatched"] += 1
                continue
            candidates.sort(key=lambda item: (item[0], _intensity_order(negative_mean(item[1])), item[1]))
            claimants.append({"order": order, "row": row, "candidates": candidates, "next": 0,
                              "mean": _mean(row, value_columns), "held": None, "lost": []})

        # Settle the claims together. A positive row asks for its best candidate not yet refused to it; a
        # negative row keeps the closest claimant (then the more intense, then the earlier row), and a claimant
        # it turns away, or gives up for a closer one, asks for its next candidate.
        def claim_key(claimant: dict[str, Any], delta: Decimal) -> tuple[Any, ...]:
            return (delta, _intensity_order(claimant["mean"]), claimant["order"])

        holders: dict[int, dict[str, Any]] = {}
        waiting = deque(claimants)
        while waiting:
            claimant = waiting.popleft()
            if claimant["next"] >= len(claimant["candidates"]):
                continue
            delta, index = claimant["candidates"][claimant["next"]]
            claimant["next"] += 1
            holder = holders.get(index)
            if holder is None:
                holders[index], claimant["held"] = claimant, (delta, index)
            elif claim_key(claimant, delta) < claim_key(holder, holder["held"][0]):
                holder["lost"].append((index, holder["held"][0]))
                holder["held"] = None
                holders[index], claimant["held"] = claimant, (delta, index)
                waiting.append(holder)
            else:
                claimant["lost"].append((index, delta))
                waiting.append(claimant)

        def lost_entries(claimant: dict[str, Any]) -> list[dict[str, Any]]:
            # Who holds each lost row at the end: a row once claimed is always held by someone.
            return [
                {"negative": identity(negative_rows[index]), "delta_rt": float(delta),
                 "won_by": identity(holders[index]["row"]), "winner_delta_rt": float(holders[index]["held"][0])}
                for index, delta in claimant["lost"]
            ]

        for claimant in claimants:
            held = claimant["held"]
            if len(claimant["candidates"]) > 1:
                passed_over = {index for index, _ in claimant["lost"]}
                why = "the smallest |delta RT|, then the higher mean intensity"
                if passed_over and held:
                    why += "; a candidate a closer positive row took is passed over"
                elif not held:
                    why += "; every candidate went to a closer positive row (lost_claims)"
                report["ambiguous"].append({
                    "rule": rule["rule"], "kind": "several negative candidates", "positive": identity(claimant["row"]),
                    "candidates": [
                        {**identity(negative_rows[index]), "delta_rt": float(delta),
                         "mean_intensity": negative_mean(index) if negative_mean(index) == negative_mean(index) else None,
                         **({"taken_by": row_id(holders[index]["row"])} if index in passed_over else {})}
                        for delta, index in claimant["candidates"]
                    ],
                    "chosen_alignment_id": row_id(negative_rows[held[1]]) if held else None,
                    "why": why,
                })
                counts["ambiguous"] += 1
            if held is None:
                lost = lost_entries(claimant)
                first = lost[0]
                report["lost_claims"].append({
                    "rule": rule["rule"], "positive": identity(claimant["row"]), "delta_rt": first["delta_rt"],
                    "negative": first["negative"], "won_by": first["won_by"], "winner_delta_rt": first["winner_delta_rt"],
                    "lost": lost,
                    "why": (
                        "every negative row it names within the tolerance went to another positive row closer in RT "
                        "(then more intense), and none is left for it: it is left unmatched"
                    ),
                })
                counts["lost_claims"] += 1

        renamed_by: dict[int, dict[str, Any]] = {}
        for index in sorted(holders):
            winner = holders[index]
            delta = winner["held"][0]
            negative = negative_rows[index]
            positive = winner["row"]
            positive_name = _cell(positive, columns, "metabolite_name")
            before_name = _cell(negative, columns, "metabolite_name")
            before_ontology = _cell(negative, columns, "ontology")
            after_name = rule["renamed_name"](before_name)
            if after_name != positive_name:
                # The two conversions are inverse; a name they do not round-trip takes the positive spelling.
                after_name = positive_name
            delta_text = _delta_text(delta)
            positive_id = row_id(positive)
            if before_ontology != rule["renamed_ontology"]:
                note = (f"Reassigned from {before_ontology} to {rule['renamed_ontology']} by the positive-mode "
                        f"{rule['renamed_ontology']} annotation {positive_name} (positive alignment ID {positive_id}; "
                        f"delta RT {delta_text} min)")
            else:
                note = (f"Renamed from {before_name} to {after_name} by the positive-mode plasmenyl annotation "
                        f"{positive_name} (positive alignment ID {positive_id}; delta RT {delta_text} min)")
            updated = list(negative)
            updated[columns["metabolite_name"]] = after_name
            updated[columns["ontology"]] = rule["renamed_ontology"]
            comment_index = columns["comment"]
            if comment_index < len(updated):
                current = updated[comment_index]
                updated[comment_index] = f"{current}; {note}" if current.strip() else note
            renamed_rows[index] = updated
            renamed_by[index] = winner
            pair: dict[str, Any] = {
                "rule": rule["rule"],
                "positive": identity(positive),
                "negative": {**identity(negative), "ontology": before_ontology},
                "renamed_to": {"name": after_name, "ontology": rule["renamed_ontology"]},
                "delta_rt": float(delta),
            }
            if winner["lost"]:
                # It lost a nearer candidate to a closer positive row and took this one.
                pair["after_losing"] = lost_entries(winner)
                counts["rematched"] += 1
            report["matched"].append(pair)
            counts["matched"] += 1

            # Positive mode may also annotate the negative row's own name (a positive PE O- or PG of that very
            # name) within the tolerance: two positive annotations of one negative row that disagree.
            own_rt = negative_rt.get(index)
            conflicting = []
            for order, other in enumerate(positive_rows):
                if (_cell(other, columns, "ontology") != rule["negative_ontology"]
                        or _cell(other, columns, "metabolite_name") != before_name or own_rt is None):
                    continue
                other_rt = _decimal_rt(_field(other, columns, "rt"))
                if other_rt is not None and abs(other_rt - own_rt) <= tolerance:
                    conflicting.append((abs(other_rt - own_rt), order))
            if conflicting:
                report["ambiguous"].append({
                    "rule": rule["rule"], "kind": "conflicting positive evidence",
                    "positive": identity(positive), "negative": {**identity(negative), "ontology": before_ontology},
                    "renamed_to": {"name": after_name, "ontology": rule["renamed_ontology"]},
                    "delta_rt": float(delta),
                    "conflicting_positive": [
                        {**identity(positive_rows[order]), "ontology": rule["negative_ontology"],
                         "delta_rt_to_negative": float(other_delta)}
                        for other_delta, order in sorted(conflicting)
                    ],
                    "chosen_alignment_id": row_id(negative),
                    "why": (
                        f"positive mode also annotates this negative row's own name, {before_name} "
                        f"({rule['negative_ontology']}), within the tolerance. It was renamed as the {rule['rule']} rule "
                        "says, but the two positive annotations disagree: check the spectra"
                    ),
                })
                counts["ambiguous"] += 1
                counts["conflicting_positive_evidence"] += 1

        # The positive rows of the class that name no negative row, with the negative rows of their very name.
        for row in not_converted:
            counts["positive_rows_not_converted"] += 1
            if rule["rule"] == "BMP":
                report["not_converted"].append({
                    "rule": rule["rule"], "positive": identity(row), "reason": "its name has no BMP class token to read as PG",
                })
                continue
            name = _cell(row, columns, "metabolite_name")
            rt = _decimal_rt(_field(row, columns, "rt"))
            same = [] if rt is None else sorted(
                (abs(other_rt - rt), index) for index, negative, other_rt in negatives
                if other_rt is not None and _cell(negative, columns, "metabolite_name") == name
                and abs(other_rt - rt) <= tolerance
            )
            names_negative = [
                {**identity(negative_rows[index]), "delta_rt": float(delta),
                 "renamed_to": renamed_rows[index][columns["metabolite_name"]] if index in renamed_by else None,
                 "renamed_by": row_id(renamed_by[index]["row"]) if index in renamed_by else None}
                for delta, index in same
            ]
            taken = [item for item in names_negative if item["renamed_to"] is not None]
            if taken:
                reason = (
                    "positive mode names it PE O- (plasmanyl) itself, but the negative row of that very name within the "
                    "tolerance was renamed PE P- by another positive row ("
                    + "; ".join(f"{item['alignment_id']} -> {item['renamed_to']} by positive {item['renamed_by']}"
                                for item in taken)
                    + "): the two positive annotations disagree (ambiguous, conflicting positive evidence)"
                )
            elif names_negative:
                reason = (
                    "positive mode names it PE O- (plasmanyl) itself, and the negative row(s) of that very name within "
                    f"the tolerance ({', '.join(item['alignment_id'] for item in names_negative)}) keep that name"
                )
            else:
                reason = (
                    "its name is not PE P- in every '|' part (positive mode names it plasmanyl itself), and no kept "
                    "negative EtherPE row of that very name lies within the tolerance"
                )
            report["not_converted"].append({
                "rule": rule["rule"], "positive": identity(row), "reason": reason, "names_negative": names_negative,
            })
        report["counts"][rule["rule"]] = counts
    return renamed_rows, report


# ---------------------------------------------------------------------------------------------
# What metaboservice wrote
# ---------------------------------------------------------------------------------------------


def companion_names() -> dict[str, str]:
    """The three tables metaboservice wrote beside the lipidome, under its fixed names whatever the lipidome is called."""
    return dict(METABOSERVICE_TABLE_NAMES)


def _dotplot_folder(name: str) -> bool:
    return name == SUMMARY_DOTPLOTS or name.startswith(CATEGORY_PREFIX) or name.startswith(SUBCLASS_PREFIX)


def _file_name(text: str) -> str:
    return "".join("_" if char in INVALID_FILE_NAME_CHARACTERS else char for char in text)


def metaboservice_tables(
    header: list[list[str]],
    rows: list[list[str]],
    columns: dict[str, int],
    sample_columns: list[int],
) -> dict[str, Any]:
    """The tables and dotplot files IntegratePosNegResults wrote, as lines, from the sorted rows.

    sample_columns are the export columns taken as samples (the positive header's Sample and QC
    columns, or Sample only for the 2024 program). Every value must parse; the merge sets a row
    whose cells do not aside first.
    """
    name_index = columns["metabolite_name"]
    ontology_index = columns["ontology"]
    id_index = columns["alignment_id"]
    warnings: list[str] = []
    metabolites = [row[name_index].split("|")[-1] + "_" + row[id_index] for row in rows]   # cs:604
    ontologies = [row[ontology_index] for row in rows]                                      # cs:605
    ontologies_unique = list(dict.fromkeys(ontologies))                                     # cs:606
    class_ids = [header[CLASS_ROW][index] for index in sample_columns]                       # cs:811-826
    sample_names = [header[CLASS_ROW][index] + "_" + header[INJECTION_ORDER_ROW][index] for index in sample_columns]

    stat_table: list[list[float]] = []                                                      # cs:619-643
    class_table: list[list[float]] = []
    for index in sample_columns:
        values = [net_double_parse(row[index]) for row in rows]
        sums = {ontology: 0.0 for ontology in ontologies_unique}
        for row, value in zip(rows, values):
            sums[row[ontology_index]] += value
        stat_table.append(values)
        class_table.append([sums[ontology] for ontology in ontologies_unique])

    names = companion_names()
    files: dict[str, list[str]] = {}
    files[names["stat"]] = ["Metabolite,Class," + ",".join(metabolites)] + [            # cs:654-666
        ",".join([name, class_ids[i]] + [number_text(value) for value in stat_table[i]])
        for i, name in enumerate(sample_names)
    ]
    intensities = []                                                                        # cs:669-683
    for j in range(len(metabolites)):
        total = 0.0
        for i in range(len(sample_names)):
            total += stat_table[i][j]
        total = _net_divide(total, len(sample_names))
        intensities.append(_net_log10(total))
    files[names["class_summary"]] = ["Metabolite,Class," + ",".join(ontologies_unique)] + [  # cs:685-696
        ",".join([name, class_ids[i]] + [number_text(value) for value in class_table[i]])
        for i, name in enumerate(sample_names)
    ]
    order = sorted(range(len(metabolites)), key=lambda j: _net_compare_double_key(intensities[j]), reverse=True)
    # OrderByDescending is stable: equal intensities keep the rows' order. Python's reverse sort is too.
    files[names["summary_plot"]] = ["Metabolite,Subclass,Order,Intensity"] + [             # cs:698-705
        f"{metabolites[j]},{ontologies[j]},{rank},{number_text(intensities[j])}"
        for rank, j in enumerate(order, start=1)
    ]

    # cs:707-786: the dotplot folders.
    categories = [lipid_category(ontology) for ontology in ontologies]
    categories_unique = list(dict.fromkeys(categories))
    metabolite_to_ontology: dict[str, str] = {}
    for metabolite, ontology in zip(metabolites, ontologies):
        metabolite_to_ontology.setdefault(metabolite, ontology)
    ontology_to_category = {ontology: lipid_category(ontology) for ontology in ontologies_unique}
    dotplots: dict[str, list[str]] = {}
    folded: dict[str, str] = {}

    def put(folder: str, file_name: str, lines: list[str]) -> None:
        clean_folder = _file_name(folder)
        if clean_folder != folder:
            warnings.append(f"The dotplot folder '{folder}' is written as '{clean_folder}'.")
        relative = f"{clean_folder}/{file_name}"
        key = relative.casefold()
        if key in folded:
            # Windows file names ignore case: the later file replaced the earlier one.
            warnings.append(f"Two dotplot files are both {relative} on Windows; the later one is kept.")
            relative = folded[key]
        folded[key] = relative
        dotplots[relative] = lines

    for category in categories_unique:
        put(SUMMARY_DOTPLOTS, _file_name(category) + ".csv", ["Value,Class,Metabolite"] + [
            f"{number_text(intensity)},{ontology},{own}"
            for intensity, ontology, own in zip(intensities, ontologies, categories) if own == category
        ])

    def export(groups: list[str], prefix: str, members: list[str], member_group: dict[str, str],
               table: list[list[float]]) -> None:
        for group in groups:
            # statTable[i][j].ToString(): the text, not the number, is what is summed and ranked.
            values = [
                (net_double_tostring(table[i][j]), class_ids[i], member)
                for i in range(len(sample_names))
                for j, member in enumerate(members)
                if member_group[member] == group
            ]
            values.sort(key=lambda item: (nls_sort_key(item[2]), nls_sort_key(item[1])))
            uniques = list(dict.fromkeys(item[2] for item in values))
            totals = []
            for member in uniques:
                total = 0.0
                for item in values:
                    if item[2] == member:
                        total += net_double_parse(item[0])
                totals.append((member, total))
            ranked = sorted(totals, key=lambda item: _net_compare_double_key(item[1]), reverse=True)
            rank = {member: position for position, (member, _) in enumerate(ranked)}
            for member in uniques:
                if rank[member] > DOTPLOT_MAX_RANK:
                    continue
                put(prefix + group, _file_name(member) + ".csv",
                    ["Value,Class,Metabolite"] + [
                        ",".join((text.replace(INFINITY_SIGN, "?"), klass, name))
                        for text, klass, name in values if name == member
                    ])
            if not uniques:
                # An empty folder makes metabolomics_service.R's dotplot loop fail (1:0 counts down).
                warnings.append(f"The dotplot folder {prefix}{group} would be empty.")

    export(categories_unique, CATEGORY_PREFIX, ontologies_unique, ontology_to_category, class_table)
    export(ontologies_unique, SUBCLASS_PREFIX, metabolites, metabolite_to_ontology, stat_table)

    # What metabolomics_service.R needs of these tables, said before anyone runs it.
    if not sample_columns:
        warnings.append("No Sample or QC column was taken, so the statistics tables have no rows.")
    duplicates = sorted({name for name in sample_names if sample_names.count(name) > 1})
    if duplicates:
        warnings.append(
            f"Sample names (Class_Injection order) repeat: {', '.join(duplicates)}; the R heatmap needs them unique."
        )
    repeated = sorted({name for name in metabolites if metabolites.count(name) > 1})
    if repeated:
        warnings.append(
            f"{len(repeated)} lipid column name(s) repeat in {names['stat']} ({', '.join(repeated[:5])}): a positive "
            "and a negative row share the last name and the alignment ID, and their dotplots share one file."
        )
    commas = [name for name in metabolites if "," in name] + [name for name in sample_names if "," in name]
    if commas:
        warnings.append(f"{len(commas)} name(s) hold a comma ({', '.join(commas[:3])}) and shift the columns of the R tables.")
    constant = [
        metabolites[j] for j in range(len(metabolites))
        if len(sample_names) > 1 and len({stat_table[i][j] for i in range(len(sample_names))}) == 1
    ]
    if constant:
        warnings.append(
            f"{len(constant)} lipid(s) have one value across every sample ({', '.join(constant[:3])}); "
            "prcomp(scale=TRUE) in metabolomics_service.R fails on a constant column."
        )
    no_intensity = [metabolites[j] for j, value in enumerate(intensities) if not math.isfinite(value)]
    if no_intensity:
        warnings.append(
            f"{len(no_intensity)} lipid(s) have no finite log10 mean ({', '.join(no_intensity[:3])}): "
            f"{names['summary_plot']} writes -? or NaN there, which R reads as text."
        )
    return {
        "files": files,
        "dotplots": dotplots,
        "samples": [
            {"column": index, "name": sample_names[i], "class": class_ids[i]} for i, index in enumerate(sample_columns)
        ],
        "warnings": warnings,
        "categories": categories_unique,
        "ontologies": ontologies_unique,
        # Filed under metaboservice's default category because its converter does not name them.
        "unassigned_ontologies": [ontology for ontology in ontologies_unique
                                  if ontology not in load_lipid_category_table()[0]],
    }


def _net_divide(total: float, count: int) -> float:
    # .NET divides a double by zero without throwing: 0/0 is NaN, x/0 an infinity.
    if count:
        return total / count
    if total == 0 or total != total:
        return math.nan
    return math.copysign(math.inf, total)


def _net_log10(value: float) -> float:
    # Math.Log10: -inf at 0, NaN below it (and of NaN), +inf of +inf.
    if value != value or value < 0:
        return math.nan
    if value == 0:
        return -math.inf
    if value == math.inf:
        return math.inf
    return math.log10(value)


# ---------------------------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------------------------


def merge_pos_neg(
    positive_path: str | Path,
    negative_path: str | Path,
    rules_path: str | Path | None,
    output_path: str | Path,
    *,
    rules: dict[str, Any] | None = None,
    internal_standard_ids: dict[str, Iterable[str]] | None = None,
    reference_matched_ids: dict[str, Iterable[str] | None] | None = None,
    expected_unit: str | None = None,
    unquantified_ids: dict[str, Iterable[str]] | None = None,
    not_quantified_path: str | Path | None = None,
    report_path: str | Path | None = None,
    reconcile: bool = True,
    reconciliation_rt_tolerance: float = DEFAULT_RECONCILIATION_RT_TOLERANCE,
    sample_file_types: Collection[str] = SAMPLE_FILE_TYPES,
    replaceable_dotplot_files: dict[str, str] | None = None,
    rules_label: str = "The rule table",
    selection_bound: str | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Merge the two polarities into one table and metaboservice's other outputs beside it.

    The per-polarity filters are keyed "positive" and "negative": the alignment IDs of the
    resolved internal standards, the IDs whose representative annotation is reference
    matched (from the candidate table; None leaves that filter off, as for an old export),
    and the IDs whose normalized values failed verification. reconcile renames the negative
    rows positive mode names BMP or PE P- (reconcile_polarities) before sheet 3 selects;
    sample_file_types are the columns the statistics tables take. Everything is written into
    output_path's folder: the table, the three statistics tables under metaboservice's fixed names
    (metabolomics_service.R reads those names, whatever output_path is called), and the dotplot
    folders. replaceable_dotplot_files ({relative path: sha256}) are the dotplot files an
    earlier merge recorded writing there, which this one may remove when it does not write them
    again; any other file in those folders refuses the write, because metabolomics_service.R
    reads every file in them. With write false nothing is written and the report says what would
    be: a preview the person confirms.

    With the reconciliation on, a selection that contradicts it is warned about under rules_label.
    selection_bound says why the selection can no longer change (a contract holds the workbook);
    the warnings then say what this lipidome counts instead of how to change the selection.
    """
    rules = rules if rules is not None else read_adduct_rules(rules_path)
    positive = describe_export(positive_path)
    negative = describe_export(negative_path)
    order = compare_sample_order(positive, negative)
    if not order["matched"]:
        raise MergeRefused(
            "The positive and negative exports do not describe the same samples in the "
            "same order, and the merged table pairs them by position: "
            + "; ".join(order["problems"])
        )
    output_path = Path(output_path)
    if output_path.name.casefold() in {name.casefold() for name in METABOSERVICE_TABLE_NAMES.values()}:
        raise MergeRefused(
            f"{output_path.name} is one of the tables metaboservice writes beside the lipidome; name the lipidome otherwise."
        )
    columns = positive["columns"]
    file_types = set(sample_file_types)
    start = positive["first_sample_column"]
    sample_columns = [start + offset for offset, kind in enumerate(positive["file_types"]) if kind in file_types]
    unlabelled = [
        positive["sample_names"][offset] for offset, kind in enumerate(positive["file_types"])
        if kind in file_types and not positive["sample_classes"][offset]
    ]

    # 1. The rows that may be lipids at all, per polarity, in file order.
    candidates: dict[str, list[list[str]]] = {"positive": [], "negative": []}
    dropped: dict[str, int] = {}
    dropped_by_polarity: dict[str, dict[str, int]] = {"positive": {}, "negative": {}}

    def drop(label: str, reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1
        dropped_by_polarity[label][reason] = dropped_by_polarity[label].get(reason, 0) + 1

    def as_set(value: Any) -> Any:
        return set(value) if isinstance(value, (list, tuple, set, frozenset)) else value

    for label, export in (("positive", positive), ("negative", negative)):
        for row in export["rows"]:
            if not any(cell.strip() for cell in row):
                continue
            reason = row_filter_reason(
                row, export["columns"],
                internal_standard_ids=as_set((internal_standard_ids or {}).get(label)),
                reference_matched_ids=as_set((reference_matched_ids or {}).get(label)),
            )
            if reason:
                drop(label, reason)
            else:
                candidates[label].append(row)

    # 2. The laboratory's positive/negative reconciliation: renames, keeps and drops nothing.
    if reconcile:
        before = candidates["negative"]
        candidates["negative"], reconciliation = reconcile_polarities(
            candidates["positive"], before, columns, sample_columns, rt_tolerance=reconciliation_rt_tolerance,
        )
        renamed = {index for index, (old, new) in enumerate(zip(before, candidates["negative"])) if old is not new}
    else:
        reconciliation = reconciliation_parameters(False, reconciliation_rt_tolerance)
        renamed = set()

    # 3. Sheet 3's selection, and a valid concentration.
    kept: list[tuple[str, list[str]]] = []
    not_quantified: list[tuple[str, list[str], str]] = []
    renamed_outcome = {"kept": 0, "not_quantified": 0, "not_selected": 0}
    for label in ("positive", "negative"):
        for index, row in enumerate(candidates[label]):
            reason = selection_reason(
                row, rules, columns, expected_unit=expected_unit,
                unquantified_ids=as_set((unquantified_ids or {}).get(label)), value_columns=sample_columns,
            )
            outcome = "kept" if not reason else "not_quantified" if reason.startswith(NOT_QUANTIFIED_REASON) else "not_selected"
            if label == "negative" and index in renamed:
                renamed_outcome[outcome] += 1
            if not reason:
                kept.append((label, row))
            elif outcome == "not_quantified":
                not_quantified.append((label, row, reason))
            else:
                drop(label, reason)
    counts = {label: sum(1 for polarity, _ in kept if polarity == label) for label in ("positive", "negative")}

    # 4. Ontology then compound name, as .NET ordered them; stable, so equal keys keep positive
    # before negative and file order, as the laboratory's program wrote them.
    kept.sort(key=lambda item: (
        nls_sort_key(item[1][columns["ontology"]]), nls_sort_key(item[1][columns["metabolite_name"]])
    ))
    if not_quantified_path is None:
        for label, _, reason in not_quantified:
            drop(label, reason)

    folder = output_path.parent
    warnings: list[str] = list(rules.get("flag_warnings") or [])
    if unlabelled:
        warnings.append(
            f"{len(unlabelled)} {' or '.join(sorted(file_types))} column(s) have no class label ({', '.join(unlabelled[:5])}). "
            "They are taken, as metaboservice took them, with an empty class and a sample name of '_' and the "
            "injection order, which metabolomics_service.R compares as a group of its own."
        )
    delimiter = "," if output_path.suffix.lower() == ".csv" else "\t"
    main_lines = [delimiter.join(cells) for cells in positive["header"]] + [delimiter.join(row) for _, row in kept]
    with_delimiter = (["header"] if any(delimiter in cell for cells in positive["header"] for cell in cells) else []) + [
        _field(row, columns, "alignment_id") for _, row in kept if any(delimiter in cell for cell in row)
    ]
    if with_delimiter:
        warnings.append(
            f"{output_path.name}: {len(with_delimiter)} row(s) hold a '{delimiter}' inside a cell ("
            f"{', '.join(with_delimiter[:5])}); written unquoted, as the laboratory's program wrote them, those rows "
            "have extra columns."
        )
    tables = metaboservice_tables(positive["header"], [row for _, row in kept], columns, sample_columns)
    warnings.extend(tables["warnings"])
    planned: dict[str, bytes] = {output_path.name: _encode(main_lines)}
    for name, lines in tables["files"].items():
        planned[name] = _encode(lines)
    dotplots = {relative: _encode(lines) for relative, lines in tables["dotplots"].items()}
    side_lines = None
    if not_quantified_path is not None:
        side_lines = [delimiter.join(cells) for cells in positive["header"]] + [
            delimiter.join(row) for _, row, _ in not_quantified
        ]
    not_ascii = sorted(name for name, data in {**planned, **dotplots}.items() if not data.isascii())
    if not_ascii:
        # A raw file name, a class label or a comment outside ASCII is the analyst's text, not an error.
        # The laboratory's program wrote one '?' per byte, which can make two class labels one; the
        # text is kept as UTF-8 instead (a recorded decision), and the difference is said.
        warnings.append(
            f"{len(not_ascii)} file(s) hold non-ASCII text and are written as UTF-8 ({', '.join(not_ascii[:3])}), "
            "where metaboservice wrote one '?' per byte; rows keep the order those '?' gave."
        )

    # The dotplot folders as they stand: a file this merge does not write would be read by R with
    # the new ones. An earlier merge's own file is replaced; anything else stops the write.
    stale, foreign, other_folders = _dotplot_folder_state(folder, dotplots, replaceable_dotplot_files or {})
    if foreign:
        message = (
            f"The dotplot folders in {folder} hold {len(foreign)} file(s) this merge would not write and no earlier "
            f"merge recorded writing ({', '.join(foreign[:5])}). metabolomics_service.R reads every file in them, "
            "so they would be plotted as this lipidome's; move them out of those folders and merge again."
        )
        if write:
            raise MergeRefused(message)
        warnings.append(message + " The confirmed write is refused until then.")
    if other_folders:
        warnings.append(
            f"{folder} holds folder(s) metabolomics_service.R would also read as dotplot folders: "
            f"{', '.join(other_folders[:5])}."
        )

    written: dict[str, dict[str, Any]] = {}
    side_file = None
    if write:
        folder.mkdir(parents=True, exist_ok=True)
        for relative in stale:
            folder.joinpath(*relative.split("/")).unlink()
        for relative in stale:
            parent = folder / relative.split("/")[0]
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
    for name, data in planned.items():
        written[name] = _write_bytes(folder / name, data, write)
    for relative, data in dotplots.items():
        _write_bytes(folder.joinpath(*relative.split("/")), data, write)
    if side_lines is not None:
        side_file = _write_bytes(Path(not_quantified_path), _encode(side_lines), write)

    def cell(row: list[str], key: str) -> str:
        return _field(row, columns, key)

    # As the selection matched them: untrimmed.
    kept_pairs = {(_cell(row, columns, "ontology"), _cell(row, columns, "adduct")) for _, row in kept}
    not_quantified_pairs = {(_cell(row, columns, "ontology"), _cell(row, columns, "adduct")) for _, row, _ in not_quantified}
    ontologies_by_polarity = {
        label: {cell(row, "ontology") for polarity, row in kept if polarity == label}
        for label in ("positive", "negative")
    }
    main = written[output_path.name]
    if reconcile:
        reconciliation["renamed_rows"] = renamed_outcome
        if renamed_outcome["not_selected"]:
            warnings.append(
                f"{renamed_outcome['not_selected']} negative row(s) the reconciliation renamed are not selected by "
                f"{rules_label[:1].lower() + rules_label[1:]}, so this lipidome has them in neither class. {selection_bound}"
                if selection_bound else
                f"{renamed_outcome['not_selected']} negative row(s) the reconciliation renamed are not selected by "
                f"{rules_label[:1].lower() + rules_label[1:]}, so they are in neither class: select BMP [M-H]- and EtherPE [M-H]-."
            )
        warnings.extend(reconciliation_selection_warnings(rules["selected"], rules_label, bound=selection_bound))
    report = {
        "output": main["path"],
        "output_folder": str(folder.resolve()),
        "written": write,
        "output_sha256": main["sha256"],
        "output_bytes": main["bytes"],
        "format": {
            "delimiter": delimiter, "newline": "CRLF", "quoting": "none",
            "encoding": "ascii" if all(data.isascii() for data in planned.values()) else "utf-8",
            "numbers": ".NET Framework Double.ToString() (15 significant digits)",
            "row_order": "Ontology, then Metabolite name, as .NET compared them on Windows (stable)",
        },
        "outputs": {
            **{key: written[name] for key, name in (("lipid_metabolome", output_path.name),
                                                     *companion_names().items())},
            "dotplots": {
                "folders": sorted({relative.split("/")[0] for relative in dotplots}),
                "files": len(dotplots),
                "replaced_files_of_earlier_merge": len(stale),
                "foreign_files": foreign,
                "manifest": {relative: hashlib.sha256(data).hexdigest() for relative, data in dotplots.items()},
            },
            "category_table": load_lipid_category_table()[2],
            # Ontologies metaboservice's converter does not name, in category_Unassigned lipid as it filed them.
            "category_unassigned_ontologies": tables["unassigned_ontologies"],
        },
        "samples": {
            "file_types": sorted(file_types),
            "taken": tables["samples"],
            "not_taken": [
                {"column": start + offset, "file": positive["sample_names"][offset], "file_type": kind}
                for offset, kind in enumerate(positive["file_types"]) if kind not in file_types
            ],
        },
        "polarity_reconciliation": reconciliation,
        "rules": {
            "path": rules.get("path", ""),
            "selected_combinations": len(rules["selected"]),
            "selected_classes": rules.get("selected_classes"),
            "selected_by_ion_mode": rules.get("selected_by_ion_mode"),
            "rows_considered": rules.get("rule_count"),
            "multi_adduct_classes": rules.get("multi_adduct_classes", []),
        },
        "sample_pairing": order,
        "kept_rows": len(kept),
        "kept_from_positive": counts["positive"],
        "kept_from_negative": counts["negative"],
        "dropped_rows": sum(dropped.values()),
        "dropped_reasons": dict(sorted(dropped.items(), key=lambda item: -item[1])),
        "dropped_by_polarity": {
            label: dict(sorted(reasons.items(), key=lambda item: -item[1]))
            for label, reasons in dropped_by_polarity.items()
        },
        "ontologies": sorted({cell(row, "ontology") for _, row in kept}),
        "ontologies_from_both_polarities": sorted(
            ontologies_by_polarity["positive"] & ontologies_by_polarity["negative"]
        ),
        "selected_pairs_without_rows": [
            {"class": klass, "adduct": adduct}
            for klass, adduct in sorted(rules["selected"] - kept_pairs - not_quantified_pairs)
        ],
        "possible_double_counts": _same_formula_groups(kept, cell),
        "not_quantified": {
            "output": side_file["path"] if side_file else None,
            "output_sha256": side_file["sha256"] if side_file else None,
            "rows": len(not_quantified),
            "by_class": _count_by(not_quantified, lambda item: cell(item[1], "ontology")),
            "items": [
                {"polarity": label, "alignment_id": cell(row, "alignment_id"), "name": cell(row, "metabolite_name"),
                 "ontology": cell(row, "ontology"), "adduct": cell(row, "adduct"), "reason": reason}
                for label, row, reason in not_quantified
            ],
        },
        "warnings": list(dict.fromkeys(warnings)),
        "inputs": {
            "positive": positive["path"], "positive_sha256": _sha256(positive_path),
            "negative": negative["path"], "negative_sha256": _sha256(negative_path),
            "rules_sha256": _sha256(rules["path"]) if rules.get("path") and Path(rules["path"]).is_file() else None,
        },
        "filters": {
            "internal_standard_ids": {
                label: len(set(values)) for label, values in (internal_standard_ids or {}).items()
            },
            "reference_matched_filter": {
                label: values is not None for label, values in (reference_matched_ids or {}).items()
            },
            "expected_unit": expected_unit,
        },
    }
    if report_path is not None and write:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        report["report"] = str(target.resolve())
    return report


def without_manifest(report: dict[str, Any]) -> dict[str, Any]:
    """The report as a tool returns it: the per-file dotplot manifest stays in merge-report.json."""
    outputs = dict(report.get("outputs") or {})
    if isinstance(outputs.get("dotplots"), dict):
        outputs["dotplots"] = {key: value for key, value in outputs["dotplots"].items() if key != "manifest"}
    return {**report, "outputs": outputs}


def _dotplot_folder_state(
    folder: Path, planned: dict[str, bytes], replaceable: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """(stale files an earlier merge wrote, other files in the dotplot folders, other subfolders)."""
    if not folder.is_dir():
        return [], [], []
    targets = {relative.casefold() for relative in planned}
    replaceable_folded = {relative.casefold(): sha for relative, sha in replaceable.items()}
    stale: list[str] = []
    foreign: list[str] = []
    others: list[str] = []
    for child in sorted(folder.iterdir()):
        if not child.is_dir():
            continue
        if not _dotplot_folder(child.name):
            others.append(child.name)
            continue
        for entry in sorted(child.iterdir()):
            relative = f"{child.name}/{entry.name}"
            if relative.casefold() in targets:
                continue
            if entry.is_file() and replaceable_folded.get(relative.casefold()) == _sha256(entry):
                stale.append(relative)
            else:
                foreign.append(relative)
    return stale, foreign, others


def reference_matched_ids(candidate_path: str | Path) -> set[str]:
    """Alignment IDs whose representative annotation is a reference match, from .mdcandidate.tsv.

    A suggestion is the representative only when nothing matched, and the export no longer
    prefixes its name, so the candidate table is the only place this is still written.
    """
    from .lab_lipidomics import read_candidate_table

    table = read_candidate_table(candidate_path)
    return {
        spot_id for spot_id, rows in table["by_spot"].items()
        if any(row["is_representative"] and row["is_reference_matched"] for row in rows)
    }


def strip_polarity(name: str) -> str:
    value = name.strip()
    for suffix in ("_pos", "_neg", "_positive", "_negative"):
        if value.lower().endswith(suffix):
            return value[: -len(suffix)]
    return value


_strip_polarity = strip_polarity


def _comment_unit(comment: str) -> str:
    for part in comment.split("; "):
        if part.startswith("Normalized unit: "):
            return part[len("Normalized unit: "):].strip()
    return ""


def _same_formula_groups(kept: list[tuple[str, list[str]]], cell: Any) -> list[dict[str, Any]]:
    # The same molecule measured twice -- PE P-36:2 [M+H]+ and PE O-36:3 [M-H]- share a
    # formula, as does cholesterol as [M+NH4]+ and [M+H-H2O]+ -- is counted twice by any
    # sum over the table. Within one ontology, because isomers of two classes (PC 34:1 and
    # PE 37:1) share formulas too and are different lipids. Reported for the person to
    # decide; nothing is removed here.
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for label, row in kept:
        formula = cell(row, "formula")
        if not formula or formula.lower() == "null":
            continue
        groups.setdefault((cell(row, "ontology"), formula), []).append({
            "polarity": label, "alignment_id": cell(row, "alignment_id"), "name": cell(row, "metabolite_name"),
            "ontology": cell(row, "ontology"), "adduct": cell(row, "adduct"),
        })
    return [
        {"ontology": ontology, "formula": formula, "rows": rows}
        for (ontology, formula), rows in sorted(groups.items())
        if len({(item["polarity"], item["adduct"]) for item in rows}) > 1
    ]


def _count_by(items: Iterable[Any], key: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = key(item)
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _encode(lines: list[str]) -> bytes:
    """StreamWriter + WriteLine: CRLF after every line. ASCII, or UTF-8 where a cell is not ASCII."""
    text = "".join(line + "\r\n" for line in lines)
    try:
        return text.encode("ascii")
    except UnicodeEncodeError:
        return text.encode("utf-8")


def _write_bytes(path: Path, data: bytes, write: bool) -> dict[str, Any]:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return {"path": str(path.resolve()), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "encoding": "ascii" if data.isascii() else "utf-8"}


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_rows(path: str | Path) -> list[list[str]]:
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return [line.rstrip("\r\n").split("\t") for line in handle]

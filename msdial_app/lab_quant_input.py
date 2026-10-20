"""Format 1 of the laboratory lipidomics quantification input: read, check and write it.

The specification is docs/lab-lipidomics-quant-input.md. One Excel workbook (Settings, Standards,
Class standards, Selection), or the same four tables as .tsv or .csv files in one folder, holds what
the laboratory's quant_utility_table.xlsx and its two internal-standard text libraries hold: the
standards and their spiked amounts, each standard's peak in each polarity, which standard
quantifies which class, and which class and adduct each polarity reports.

Reading it gives the same shapes the earlier form gives (preparation, lookup, quant_info and a
library of InternalStandard records per polarity), so everything after the check is unchanged.
Every problem is reported with its table, row and column; nothing here needs a language model.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .lab_lipidomics import (
    ANY_OTHERS,
    FORMULA_MZ_WARNING_DA,
    NEGATIVE,
    POLARITIES,
    POSITIVE,
    UNDEFINED,
    InternalStandard,
    LabInputError,
    LipidClassVocabulary,
    XlsxCell,
    XlsxSheet,
    adduct_mz,
    column_letters,
    excel_lock_files,
    file_sha256,
    formula_mass,
    load_lipid_class_vocabulary,
    parse_adduct,
    parse_number,
    parse_quant_info,
    read_xlsx,
)

FORMAT_ID = "msdial-lipidomics-quant-input/1"
LEGACY_FORMAT = "quant_utility_table"
SPEC = "docs/lab-lipidomics-quant-input.md"

# The four tables: their key, the sheet name a template writes, and the file stem in a folder.
TABLES = ("settings", "standards", "class_standards", "selection")
SHEET_NAMES = {"settings": "Settings", "standards": "Standards", "class_standards": "Class standards",
               "selection": "Selection"}
FILE_STEMS = {"settings": "settings", "standards": "standards", "class_standards": "class_standards",
              "selection": "selection"}
TEXT_SUFFIXES = {".tsv": "\t", ".csv": ","}
WORKBOOK_SUFFIXES = (".xlsx", ".xlsm")

# Header names as _column_key compares them: lowercase letters and digits, text in parentheses dropped.
COLUMNS = {
    "settings": {"setting": ("setting", "key", "name"), "value": ("value",)},
    "standards": {
        "name": ("name", "standard", "standardname"),
        "class": ("class", "lipidclass", "ontology"),
        "spiked": ("spikedamount", "spiked", "addedamount", "amountspiked"),
        "formula": ("formula",),
        "inchikey": ("inchikey",),
        "smiles": ("smiles",),
        **{
            f"{prefix}_{part}": tuple(f"{word}{alias}" for word in words for alias in aliases)
            for prefix, words in (("pos", ("pos", "positive")), ("neg", ("neg", "negative")))
            for part, aliases in (("adduct", ("adduct",)), ("mz", ("mz",)), ("rt", ("rt", "rtmin")), ("ccs", ("ccs",)))
        },
    },
    "class_standards": {
        "class": ("class", "lipidclass", "targetclass"),
        "standard": ("standard", "standardname", "internalstandard"),
    },
    "selection": {
        "class": ("class", "lipidclass", "ontology"),
        "adduct": ("adduct", "adducttype"),
        "polarity": ("polarity", "ionmode"),
        "use": ("use", "isselected", "selected"),
    },
}
REQUIRED = {
    "settings": ("setting", "value"),
    "standards": ("name", "class", "spiked", "pos_adduct", "pos_mz", "pos_rt", "neg_adduct", "neg_mz", "neg_rt"),
    "class_standards": ("class", "standard"),
    "selection": ("class", "adduct", "polarity"),
}
HEADERS = {
    "settings": ("Setting", "Value"),
    "standards": ("Name", "Class", "Spiked amount", "Formula", "InChIKey", "SMILES", "Pos adduct", "Pos m/z",
                  "Pos RT", "Pos CCS", "Neg adduct", "Neg m/z", "Neg RT", "Neg CCS"),
    "class_standards": ("Class", "Standard"),
    "selection": ("Class", "Adduct", "Polarity", "Use"),
}
COLUMN_TITLES = {
    "settings": {"setting": "Setting", "value": "Value"},
    "standards": {key: title for key, title in zip(
        ("name", "class", "spiked", "formula", "inchikey", "smiles", "pos_adduct", "pos_mz", "pos_rt", "pos_ccs",
         "neg_adduct", "neg_mz", "neg_rt", "neg_ccs"), HEADERS["standards"])},
    "class_standards": {"class": "Class", "standard": "Standard"},
    "selection": {"class": "Class", "adduct": "Adduct", "polarity": "Polarity", "use": "Use"},
}
SETTINGS = {
    "format": ("format", "formatversion"),
    "sample_amount": ("sampleamount",),
    "sample_basis": ("samplebasis",),
    "spiked_unit": ("spikedamountunit", "amountunit"),
    "description": ("description", "notes", "note"),
}
SETTING_TITLES = {"format": "Format", "sample_amount": "Sample amount", "sample_basis": "Sample basis",
                  "spiked_unit": "Spiked amount unit", "description": "Description"}
# Sample basis -> (what the person wrote it as, the unit normalize's -u names).
BASES = {
    "ulplasma": ("uL plasma", "microL_plasma"), "microlplasma": ("uL plasma", "microL_plasma"),
    "mgtissue": ("mg tissue", "mg_tissue"),
    "10^6cells": ("10^6 cells", "10E6_cells"), "106cells": ("10^6 cells", "10E6_cells"),
    "10e6cells": ("10^6 cells", "10E6_cells"),
}
BASIS_CHOICES = ("uL plasma", "mg tissue", "10^6 cells")
SPIKED_UNITS = {"pmol": "p", "nmol": "n", "fmol": "f"}
POLARITY_PREFIX = {POSITIVE: "pos", NEGATIVE: "neg"}
POLARITY_TITLE = {POSITIVE: "Pos", NEGATIVE: "Neg"}


def _column_key(text: Any) -> str:
    """A header as compared: 'Spiked amount (pmol)' -> 'spikedamount', 'Pos m/z' -> 'posmz'."""
    without = re.sub(r"\([^)]*\)", "", str(text or ""))
    return re.sub(r"[^a-z0-9]", "", without.lower())


def _table_key(name: Any) -> str:
    return re.sub(r"[\s_\-]", "", str(name or "")).casefold()


def _basis_key(text: Any) -> str:
    value = str(text or "").lower().replace("µ", "u").replace("μ", "u")
    return re.sub(r"[^a-z0-9^]", "", value)


def _ascii(text: str) -> bool:
    return all(32 <= ord(character) < 127 for character in text)


# ---------------------------------------------------------------------------------------------
# Tables, from a workbook sheet or a text file
# ---------------------------------------------------------------------------------------------


@dataclass
class Cell:
    text: str
    value: Any = None  # float, bool, str or None
    problem: str = ""  # an Excel error value, or a formula Excel never calculated


@dataclass
class Table:
    key: str
    label: str  # 'Standards' or 'standards.tsv', as messages name it
    source: str
    columns: dict[str, int] = field(default_factory=dict)
    rows: list[tuple[int, list[Cell]]] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def where(self, row: int, column: str | None = None) -> str:
        title = COLUMN_TITLES[self.key].get(column, column) if column else ""
        return f"{self.label} row {row}" + (f", {title}" if title else "")

    def cell(self, cells: list[Cell], column: str) -> Cell:
        index = self.columns.get(column, -1)
        return cells[index] if 0 <= index < len(cells) else Cell("")

    def text(self, cells: list[Cell], column: str) -> str:
        return self.cell(cells, column).text.strip()


def _bind_columns(table: Table, header: list[str]) -> None:
    aliases = COLUMNS[table.key]
    for index, title in enumerate(header):
        key = _column_key(title)
        if not key:
            continue
        field_name = next((name for name, names in aliases.items() if key in names), None)
        if field_name is None:
            table.ignored_columns.append(title.strip())
        elif field_name in table.columns:
            table.problems.append(f"{table.label} has the column {COLUMN_TITLES[table.key][field_name]} twice.")
        else:
            table.columns[field_name] = index
    missing = [COLUMN_TITLES[table.key][name] for name in REQUIRED[table.key] if name not in table.columns]
    if missing:
        table.problems.append(
            f"{table.label} row 1 has no {', '.join(missing)} column(s); its header is: "
            f"{' | '.join(title for title in header if title.strip()) or '(empty)'}."
        )


def table_from_sheet(sheet: XlsxSheet, key: str, source: str) -> Table:
    table = Table(key, sheet.name, source)
    width = max((column for (_, column) in sheet.cells), default=-1) + 1

    def read(row: int, column: int) -> Cell:
        ref = f"{column_letters(column)}{row}"
        found = sheet.cell(ref)
        if found is None:
            return Cell("")
        if found.kind == "error":
            return Cell("", None, f"{found.ref} is an error value ({found.raw})")
        try:
            value = sheet.value(ref)
        except LabInputError:
            return Cell("", None, f"{found.ref} holds a formula Excel never calculated; open the workbook in Excel and save it")
        return Cell(sheet.text(ref), value)

    header = [read(1, column).text for column in range(width)]
    _bind_columns(table, header)
    for row in range(2, sheet.max_row + 1):
        cells = [read(row, column) for column in range(width)]
        if any(cell.text.strip() or cell.problem for cell in cells):
            table.rows.append((row, cells))
    return table


def table_from_text(path: Path, key: str) -> Table:
    table = Table(key, path.name, str(path))
    try:
        data = path.read_bytes()
    except OSError as error:
        table.problems.append(f"{path} cannot be read: {error}")
        return table
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        table.problems.append(f"{path.name} is not UTF-8 text; save it as UTF-8.")
        return table
    lines = text.splitlines()
    delimiter = TEXT_SUFFIXES[path.suffix.casefold()]
    numbered = [(number, line) for number, line in enumerate(lines, start=1) if not line.lstrip().startswith("#")]
    if not numbered or not numbered[0][1].strip():
        table.problems.append(f"{path.name} has no header on its first line.")
        return table
    parsed = list(csv.reader(io.StringIO("\n".join(line for _, line in numbered)), delimiter=delimiter))
    if len(parsed) != len(numbered):
        # A quoted field holding a line break: rare, and it would shift every line number after it.
        table.problems.append(f"{path.name} has a quoted field across lines; keep each row on one line.")
        return table
    header = parsed[0]
    _bind_columns(table, header)
    for (number, _), values in zip(numbered[1:], parsed[1:]):
        cells = [Cell(value, value) for value in values]
        if any(value.strip() for value in values):
            table.rows.append((number, cells))
    return table


def _number(cell: Cell, where: str, problems: list[str]) -> float | None:
    """A number from a cell, or None when it is blank; a problem is said for anything else."""
    if cell.problem:
        problems.append(f"{where}: {cell.problem}")
        return None
    if isinstance(cell.value, bool):
        problems.append(f"{where}: {cell.text} is not a number")
        return None
    if isinstance(cell.value, (int, float)):
        return float(cell.value)
    text = cell.text.strip()
    if not text:
        return None
    number = parse_number(text)
    if number is None:
        hint = " written with a decimal point" if re.fullmatch(r"[+-]?\d+,\d+", text) else ""
        problems.append(f"{where}: '{text}' is not a number{hint}")
    return number


def _cell_number(table: Table, row: int, cells: list[Cell], column: str, problems: list[str]) -> float | None:
    return _number(table.cell(cells, column), table.where(row, column), problems)


# ---------------------------------------------------------------------------------------------
# Finding the tables
# ---------------------------------------------------------------------------------------------


def detect_format(path: str | Path) -> str:
    """FORMAT_ID for a folder or a workbook with a Settings sheet, LEGACY_FORMAT for another workbook, '' otherwise."""
    source = Path(path).expanduser()
    if source.is_dir():
        return FORMAT_ID
    if not source.is_file() or source.suffix.casefold() not in WORKBOOK_SUFFIXES:
        return ""
    try:
        workbook = read_xlsx(source)
    except (LabInputError, OSError, ValueError):
        return LEGACY_FORMAT
    names = {_table_key(sheet.name) for sheet in workbook.sheets}
    return FORMAT_ID if "settings" in names else LEGACY_FORMAT


def folder_files(folder: Path) -> tuple[dict[str, Path], list[str]]:
    """The table files in a folder, and what is wrong with the set."""
    files: dict[str, Path] = {}
    problems: list[str] = []
    for key in TABLES:
        found = [folder / f"{FILE_STEMS[key]}{suffix}" for suffix in TEXT_SUFFIXES if (folder / f"{FILE_STEMS[key]}{suffix}").is_file()]
        if not found:
            problems.append(f"{folder} has no {FILE_STEMS[key]}.tsv (or .csv): the {SHEET_NAMES[key]} table is missing.")
        elif len(found) > 1:
            problems.append(f"{folder} has both {found[0].name} and {found[1].name}; keep one.")
        else:
            files[key] = found[0]
    return files, problems


def folder_digest(folder: Path, files: dict[str, Path] | None = None) -> str:
    """One digest of a folder input: its table files by name and content."""
    files = files if files is not None else folder_files(folder)[0]
    lines = "".join(f"{key}\t{path.name}\t{file_sha256(path)}\n" for key, path in sorted(files.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def input_sha256(path: str | Path) -> str:
    source = Path(path).expanduser()
    return folder_digest(source) if source.is_dir() else file_sha256(source)


def _load_tables(source: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"tables": {}, "problems": [], "warnings": [], "notes": [], "sheet_names": [],
                              "layout": "", "sha256": "", "files": []}
    if source.is_dir():
        result["layout"] = "folder"
        files, problems = folder_files(source)
        result["problems"].extend(problems)
        for key, path in files.items():
            result["tables"][key] = table_from_text(path, key)
            result["files"].append({"table": key, "path": str(path), "sha256": file_sha256(path)})
        result["sha256"] = folder_digest(source, files)
        result["sheet_names"] = [path.name for path in files.values()]
        return result
    if source.suffix.casefold() == ".xls":
        result["problems"].append(f"{source.name} is an old .xls workbook; save it as .xlsx.")
        return result
    if source.suffix.casefold() not in WORKBOOK_SUFFIXES:
        result["problems"].append(f"{source.name} is neither a workbook (.xlsx) nor a folder of .tsv or .csv tables.")
        return result
    result["layout"] = "xlsx"
    workbook = read_xlsx(source)
    result["sha256"] = workbook.sha256
    result["sheet_names"] = workbook.sheet_names
    result["warnings"].extend(workbook.warnings)
    for lock in excel_lock_files(source):
        result["warnings"].append(f"{Path(lock).name} shows the workbook is open in Excel; only what was last saved is read.")
    by_key: dict[str, list[XlsxSheet]] = {}
    for sheet in workbook.sheets:
        by_key.setdefault(_table_key(sheet.name), []).append(sheet)
    for key in TABLES:
        sheets = by_key.pop(_table_key(SHEET_NAMES[key]), [])
        if not sheets:
            result["problems"].append(f"{source.name} has no '{SHEET_NAMES[key]}' sheet.")
        elif len(sheets) > 1:
            result["problems"].append(f"{source.name} has two sheets named like '{SHEET_NAMES[key]}'; keep one.")
        else:
            result["tables"][key] = table_from_sheet(sheets[0], key, str(source))
    ignored = [sheet.name for sheets in by_key.values() for sheet in sheets]
    if ignored:
        result["notes"].append(f"Sheets not read: {', '.join(ignored)}.")
    return result


# ---------------------------------------------------------------------------------------------
# The four tables
# ---------------------------------------------------------------------------------------------


def parse_settings(table: Table) -> dict[str, Any]:
    problems: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    values: dict[str, tuple[int, Cell]] = {}
    for row, cells in table.rows:
        name = table.text(cells, "setting")
        key = _column_key(name)
        setting = next((item for item, aliases in SETTINGS.items() if key in aliases), None)
        if setting is None:
            if name:
                notes.append(f"{table.where(row)}: the setting '{name}' is not one this format reads; ignored.")
            continue
        if setting in values:
            problems.append(f"{table.where(row)}: {SETTING_TITLES[setting]} is given twice (rows {values[setting][0]} and {row}).")
            continue
        values[setting] = (row, table.cell(cells, "value"))
    result: dict[str, Any] = {"format": "", "sample_amount": None, "sample_basis": "", "unit": None,
                              "spiked_unit": "pmol", "description": "", "rows": {k: v[0] for k, v in values.items()}}

    def setting_text(name: str) -> str:
        return values[name][1].text.strip() if name in values else ""

    written = setting_text("format")
    result["format"] = written
    if not written:
        problems.append(f"{table.label} has no Format row; it must read {FORMAT_ID}.")
    elif written.casefold() != FORMAT_ID:
        problems.append(f"{table.label} row {values['format'][0]}: Format is '{written}'; this reader knows only {FORMAT_ID}.")
    if "sample_amount" not in values or not setting_text("sample_amount"):
        problems.append(f"{table.label}: Sample amount is required, a positive number (10 for 10 uL plasma).")
    else:
        row, cell = values["sample_amount"]
        amount = _number(cell, table.where(row, "value"), problems)
        if amount is not None and amount <= 0:
            problems.append(f"{table.where(row, 'value')}: Sample amount must be positive.")
        elif amount is not None:
            result["sample_amount"] = amount
    basis_text = setting_text("sample_basis")
    basis = BASES.get(_basis_key(basis_text))
    if not basis_text:
        problems.append(f"{table.label}: Sample basis is required: {', '.join(BASIS_CHOICES)}.")
    elif basis is None:
        problems.append(
            f"{table.where(values['sample_basis'][0], 'value')}: Sample basis '{basis_text}' is not one of "
            f"{', '.join(BASIS_CHOICES)}."
        )
    else:
        result["sample_basis"] = basis[0]
    unit_text = setting_text("spiked_unit") or "pmol"
    prefix = SPIKED_UNITS.get(unit_text.strip().lower())
    if prefix is None:
        problems.append(
            f"{table.where(values['spiked_unit'][0], 'value')}: Spiked amount unit '{unit_text}' is not pmol, nmol or fmol."
        )
    else:
        result["spiked_unit"] = unit_text.strip().lower()
        if basis is not None:
            result["unit"] = f"{prefix}mol_per_{basis[1]}"
    result["description"] = setting_text("description")
    return {**result, "problems": problems, "warnings": warnings, "notes": notes}


def _known_class(name: str, vocabulary: LipidClassVocabulary) -> str:
    """The class-definition spelling of a class written in either spelling; UNDEFINED if MS-DIAL knows neither."""
    if vocabulary.is_vs2(name):
        return name
    return vocabulary.canonical_vs2(name)


def parse_standards(table: Table, settings: dict[str, Any], vocabulary: LipidClassVocabulary) -> dict[str, Any]:
    problems: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    standards: list[dict[str, Any]] = []
    libraries: dict[str, list[InternalStandard]] = {POSITIVE: [], NEGATIVE: []}
    seen: dict[str, int] = {}
    sample_amount = settings.get("sample_amount")
    for row, cells in table.rows:
        row_problems: list[str] = []
        name = table.text(cells, "name")
        klass = table.text(cells, "class")
        formula = table.text(cells, "formula")
        if not name:
            row_problems.append(f"{table.where(row, 'name')}: a standard needs a name")
        elif not _ascii(name):
            row_problems.append(f"{table.where(row, 'name')}: '{name}' holds non-ASCII characters, which MS-DIAL reads as '?'")
        elif name.casefold() in seen:
            row_problems.append(f"{table.where(row, 'name')}: {name} is also on row {seen[name.casefold()]}")
        label = name or "this standard"
        ontology = klass
        if not klass:
            row_problems.append(f"{table.where(row, 'class')}: {label} needs its lipid class")
        else:
            known = _known_class(klass, vocabulary)
            if known == UNDEFINED:
                warnings.append(f"{table.where(row, 'class')}: '{klass}' is not a lipid class MS-DIAL knows; it only labels {label}.")
            elif known != klass:
                notes.append(f"{table.where(row, 'class')}: {klass} read as {known}.")
                ontology = known
        spiked = _cell_number(table, row, cells, "spiked", row_problems)
        if spiked is None and not table.text(cells, "spiked") and not table.cell(cells, "spiked").problem:
            row_problems.append(f"{table.where(row, 'spiked')}: the amount of {label} added to each sample is required")
        elif spiked is not None and spiked <= 0:
            row_problems.append(f"{table.where(row, 'spiked')}: the amount must be positive")
        neutral = None
        if formula:
            if not _ascii(formula):
                row_problems.append(f"{table.where(row, 'formula')}: '{formula}' holds non-ASCII characters")
            else:
                try:
                    neutral = formula_mass(formula)
                except ValueError as error:
                    row_problems.append(f"{table.where(row, 'formula')}: {error}")
        for text_column in ("inchikey", "smiles"):
            value = table.text(cells, text_column)
            if value and not _ascii(value):
                row_problems.append(f"{table.where(row, text_column)}: it holds non-ASCII characters")
        ions: dict[str, dict[str, Any]] = {}
        for polarity in POLARITIES:
            ion = _parse_ion(table, row, cells, polarity, label, neutral, row_problems, warnings, notes)
            if ion is not None:
                ions[polarity] = ion
        if not ions and not any(
            table.text(cells, f"{POLARITY_PREFIX[p]}_{part}") for p in POLARITIES for part in ("adduct", "mz", "rt", "ccs")
        ):
            row_problems.append(f"{table.where(row)}: {label} has a peak in neither polarity; fill Pos or Neg adduct, m/z and RT")
        if name and name.casefold() not in seen:
            seen[name.casefold()] = row
        if row_problems:
            problems.extend(item + "." for item in row_problems)
            continue
        amount = None
        if spiked is not None and sample_amount:
            amount = spiked / sample_amount
        standards.append({
            "name": name, "class_name": ontology, "spiked_amount": spiked, "amount": amount, "row": row,
            "formula": formula or None,
            "ions": {polarity: {key: value for key, value in ion.items() if key != "record"} for polarity, ion in ions.items()},
        })
        for polarity, ion in ions.items():
            record = ion["record"]
            record.ontology = ontology
            record.inchikey = table.text(cells, "inchikey")
            record.formula = formula
            record.smiles = table.text(cells, "smiles")
            libraries[polarity].append(record)
    if not table.rows:
        problems.append(f"{table.label} lists no standard below its header.")
    for polarity in POLARITIES:
        if standards and not libraries[polarity] and not problems:
            problems.append(f"{table.label}: no standard has a {polarity.lower()}-mode peak; each polarity needs at least one.")
    return {"standards": standards, "libraries": libraries, "problems": problems, "warnings": warnings, "notes": notes}


def _parse_ion(
    table: Table, row: int, cells: list[Cell], polarity: str, label: str, neutral: float | None,
    problems: list[str], warnings: list[str], notes: list[str],
) -> dict[str, Any] | None:
    prefix = POLARITY_PREFIX[polarity]
    columns = {part: f"{prefix}_{part}" for part in ("adduct", "mz", "rt", "ccs")}
    if not any(table.text(cells, column) or table.cell(cells, column).problem for column in columns.values()):
        return None
    before = len(problems)
    adduct = table.text(cells, columns["adduct"])
    ion = None
    if not adduct:
        problems.append(f"{table.where(row, columns['adduct'])}: {label} has a {polarity.lower()}-mode peak but no adduct")
    else:
        try:
            ion = parse_adduct(adduct)
            if ion.polarity != polarity:
                problems.append(f"{table.where(row, columns['adduct'])}: {adduct} is a {ion.polarity.lower()} ion")
        except ValueError as error:
            problems.append(f"{table.where(row, columns['adduct'])}: {error}")
    mz = _cell_number(table, row, cells, columns["mz"], problems)
    mz_text = table.text(cells, columns["mz"])
    rt = _cell_number(table, row, cells, columns["rt"], problems)
    ccs = _cell_number(table, row, cells, columns["ccs"], problems)
    rt_text = table.text(cells, columns["rt"])
    if rt is None and not rt_text and not table.cell(cells, columns["rt"]).problem:
        problems.append(f"{table.where(row, columns['rt'])}: the retention time of {label} on your chromatography is required")
    elif rt is not None and rt < 0:
        problems.append(f"{table.where(row, columns['rt'])}: a retention time cannot be negative")
    if ccs is not None and ccs <= 0:
        problems.append(f"{table.where(row, columns['ccs'])}: CCS must be positive")
    computed = None
    if neutral is not None and ion is not None and ion.polarity == polarity:
        computed = adduct_mz(neutral, ion)
    if mz is None and not mz_text and not table.cell(cells, columns["mz"]).problem:
        if computed is None:
            problems.append(f"{table.where(row, columns['mz'])}: the m/z is required, or a Formula to compute it from")
        else:
            mz = round(computed, 6)
            mz_text = ""
            notes.append(f"{table.where(row, columns['mz'])}: computed from {table.text(cells, 'formula')} {adduct}: {mz:.4f}.")
    elif mz is not None and mz <= 0:
        problems.append(f"{table.where(row, columns['mz'])}: the m/z must be positive")
    elif mz is not None and computed is not None and abs(computed - mz) > FORMULA_MZ_WARNING_DA:
        warnings.append(
            f"{table.where(row, columns['mz'])}: {mz} differs from {table.text(cells, 'formula')} {adduct} "
            f"({computed:.4f}) by {(mz - computed) * 1000:.1f} mDa; check the m/z and the adduct."
        )
    if len(problems) > before or mz is None or rt is None or ion is None:
        return None
    record = InternalStandard(
        name=label, mz=mz, rt=rt, adduct=adduct, ontology="", ccs=ccs, mz_text=mz_text if mz_text else "",
        rt_text=rt_text, ccs_text=table.text(cells, columns["ccs"]), line=row,
    )
    return {"adduct": adduct, "mz": mz, "rt": rt, "ccs": ccs, "mz_computed": not mz_text, "record": record}


def parse_class_standards(
    table: Table, standards: list[dict[str, Any]], known_names: set[str], vocabulary: LipidClassVocabulary
) -> dict[str, Any]:
    """Which standard quantifies which class, in the shape parse_lookup_table gives."""
    problems: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    rows: list[dict[str, Any]] = []
    amounts = {item["name"]: item["amount"] for item in standards}
    by_name = {name.casefold(): name for name in known_names}
    seen: dict[str, int] = {}
    others = 0
    for row, cells in table.rows:
        klass = table.text(cells, "class")
        standard = table.text(cells, "standard")
        if not klass or not standard:
            missing = "class" if not klass else "standard"
            problems.append(f"{table.where(row, 'class' if not klass else 'standard')}: the row has no {missing}.")
            continue
        if klass.casefold() == ANY_OTHERS.casefold():
            target = ANY_OTHERS
            others += 1
        else:
            target = _known_class(klass, vocabulary)
            if target == UNDEFINED:
                problems.append(
                    f"{table.where(row, 'class')}: '{klass}' is a class MS-DIAL knows in neither spelling, so no lipid "
                    f"would carry it; see the template's Class names sheet."
                )
                continue
            if target != klass:
                notes.append(f"{table.where(row, 'class')}: {klass} read as {target}.")
        name = by_name.get(standard.casefold())
        if name is None:
            problems.append(f"{table.where(row, 'standard')}: {standard} is not a Name in the Standards table.")
            continue
        if name != standard:
            notes.append(f"{table.where(row, 'standard')}: {standard} read as {name}.")
        if target in seen:
            problems.append(
                f"{table.where(row, 'class')}: {target} is also on row {seen[target]}; normalization would use the "
                "first and ignore this one."
            )
            continue
        seen[target] = row
        rows.append({"standard": name, "target_class": target, "amount": amounts.get(name), "row": row,
                     "cached_amount": None})
    if others != 1:
        problems.append(f"{table.label} must name '{ANY_OTHERS}' exactly once, for every class not listed; it does {others} time(s).")
    used = {row["standard"] for row in rows}
    return {"sheet": table.label, "rows": rows, "unused_standards": [item["name"] for item in standards if item["name"] not in used],
            "blockers": problems, "warnings": warnings, "notes": notes}


def parse_selection(table: Table, vocabulary: LipidClassVocabulary, source: str) -> dict[str, Any]:
    """Which class and adduct each polarity reports, in the shape parse_quant_info gives.

    The rows are checked by parse_quant_info itself, on a sheet that holds them as read: classes in
    the LBM spelling, Use blank or absent as TRUE.
    """
    problems: list[str] = []
    notes: list[str] = []
    cells_by_ref: dict[tuple[int, int], XlsxCell] = {}
    for column, title in enumerate(("Class", "Adduct", "Ion mode", "IsSelected")):
        cells_by_ref[(1, column)] = XlsxCell(f"{column_letters(column)}1", "string", title)
    last = 1
    for row, cells in table.rows:
        klass = table.text(cells, "class")
        if klass and not vocabulary.is_lbm_class(klass):
            converted = vocabulary.lbm_class(klass)
            if converted != UNDEFINED:
                notes.append(f"{table.where(row, 'class')}: {klass} read as {converted}.")
                klass = converted
        use_cell = table.cell(cells, "use")
        use_text = use_cell.text.strip()
        if isinstance(use_cell.value, bool):
            use = use_cell.value
        elif not use_text:
            use = True
        else:
            use = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}.get(use_text.lower())
        if use_cell.problem:
            problems.append(f"{table.where(row, 'use')}: {use_cell.problem}.")
            continue
        if use is None:
            problems.append(f"{table.where(row, 'use')}: '{use_text}' is not TRUE or FALSE.")
            continue
        values = (klass, table.text(cells, "adduct"), table.text(cells, "polarity"), "TRUE" if use else "FALSE")
        for column, value in enumerate(values):
            cells_by_ref[(row, column)] = XlsxCell(f"{column_letters(column)}{row}", "string", value)
        last = max(last, row)
    sheet = XlsxSheet(table.label, "", cells_by_ref, [], last)
    result = parse_quant_info(sheet, vocabulary, source)
    # parse_quant_info quotes a sheet's name; this table is named as every other message names it.
    quoted = f"'{table.label}'"
    result["blockers"] = problems + [item.replace(quoted, table.label) for item in result["blockers"]]
    result["warnings"] = [item.replace(quoted, table.label) for item in result["warnings"]]
    result["notes"] = notes
    return result


# ---------------------------------------------------------------------------------------------
# The whole input
# ---------------------------------------------------------------------------------------------


def read_quant_input(path: str | Path, vocabulary: LipidClassVocabulary | None = None) -> dict[str, Any]:
    """Read a format-1 input: the shapes read_quant_workbook gives, the libraries, and every problem by place."""
    vocabulary = vocabulary or load_lipid_class_vocabulary()
    source = Path(path).expanduser()
    blockers: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    result: dict[str, Any] = {
        "format": FORMAT_ID, "path": str(source), "sha256": "", "layout": "", "sheet_names": [], "files": [],
        "settings": {}, "standards_table": [], "libraries": {POSITIVE: [], NEGATIVE: []},
        "blockers": blockers, "warnings": warnings, "notes": notes,
    }
    if not source.exists():
        blockers.append(f"{source} does not exist.")
        return result
    try:
        loaded = _load_tables(source)
    except (LabInputError, OSError) as error:
        blockers.append(str(error))
        return result
    result.update({key: loaded[key] for key in ("layout", "sha256", "sheet_names", "files")})
    blockers.extend(loaded["problems"])
    warnings.extend(loaded["warnings"])
    notes.extend(loaded["notes"])
    tables: dict[str, Table] = loaded["tables"]
    for table in tables.values():
        blockers.extend(table.problems)
        if table.ignored_columns:
            notes.append(f"{table.label}: columns not read: {', '.join(table.ignored_columns)}.")
    if any(table.problems for table in tables.values()) or len(tables) < len(TABLES):
        return result
    settings = parse_settings(tables["settings"])
    result["settings"] = {key: value for key, value in settings.items() if key not in {"problems", "warnings", "notes"}}
    standards = parse_standards(tables["standards"], settings, vocabulary)
    lookup = parse_class_standards(
        tables["class_standards"], standards["standards"],
        set(_standard_names(tables["standards"])), vocabulary,
    )
    quant_info = parse_selection(tables["selection"], vocabulary, str(source))
    for part in (settings, standards):
        blockers.extend(part["problems"])
        warnings.extend(part["warnings"])
        notes.extend(part["notes"])
    for part in (lookup, quant_info):
        blockers.extend(part["blockers"])
        warnings.extend(part["warnings"])
        notes.extend(part.get("notes", []))
    unit = settings.get("unit")
    sample_amount = settings.get("sample_amount")
    basis = settings.get("sample_basis")
    preparation = {
        "sheet": tables["standards"].label, "amount_column": "",
        "amount_header": f"Spiked amount ({settings.get('spiked_unit')}) / Sample amount ({basis})",
        "unit": unit, "header_row": 1,
        "standards": [{key: item[key] for key in ("name", "class_name", "amount", "row", "formula", "spiked_amount")}
                      for item in standards["standards"]],
        "sample_parameter": {
            "label": f"Sample amount ({basis})" if basis else "Sample amount", "value": sample_amount,
            "label_ref": f"{tables['settings'].label} row {settings['rows'].get('sample_amount', '')}".strip(),
            "value_ref": f"{tables['settings'].label} row {settings['rows'].get('sample_amount', '')}".strip(),
        },
        "notes": [{"ref": f"{tables['settings'].label} Description", "text": settings["description"]}]
        if settings.get("description") else [],
        "blockers": [], "warnings": [],
    }
    result.update({
        "preparation": preparation, "lookup": lookup, "quant_info": quant_info, "unit": unit,
        "libraries": standards["libraries"],
        "standards_table": [
            {"name": item["name"], "class": item["class_name"], "spiked_amount": item["spiked_amount"],
             "spiked_unit": settings.get("spiked_unit"), "amount": item["amount"], "unit": unit, "ions": item["ions"]}
            for item in standards["standards"]
        ],
    })
    return result


def _standard_names(table: Table) -> list[str]:
    return [table.text(cells, "name") for _, cells in table.rows if table.text(cells, "name")]


def summary(read: dict[str, Any]) -> dict[str, Any]:
    """What a person looks over: the tables as read, without the library records."""
    quant_info = read.get("quant_info") or {}
    lookup = read.get("lookup") or {}
    return {
        "format": read.get("format"), "path": read.get("path"), "layout": read.get("layout"),
        "sha256": read.get("sha256"), "settings": read.get("settings"), "unit": read.get("unit"),
        "standards": read.get("standards_table", []),
        "class_standards": [{"class": row["target_class"], "standard": row["standard"]} for row in lookup.get("rows", [])],
        "unused_standards": lookup.get("unused_standards", []),
        "selected_by_ion_mode": quant_info.get("selected_by_ion_mode", {}),
        "selected_classes": quant_info.get("selected_classes", 0),
        "multi_adduct_classes": quant_info.get("multi_adduct_classes", []),
        "undefined_classes": quant_info.get("undefined_classes", []),
    }


def as_workbook(read: dict[str, Any]) -> dict[str, Any]:
    """The read in read_quant_workbook's shape, for the contract and the merge."""
    return {
        "format": FORMAT_ID, "path": read["path"], "sha256": read["sha256"], "sheet_names": read["sheet_names"],
        "layout": read.get("layout"), "files": read.get("files", []),
        "preparation": read.get("preparation", {}), "lookup": read.get("lookup", {}),
        "quant_info": read.get("quant_info", {}), "unit": read.get("unit"),
        "blockers": list(read["blockers"]), "warnings": list(read["warnings"]), "notes": list(read.get("notes", [])),
    }


# ---------------------------------------------------------------------------------------------
# Writing an input: an empty template, or the earlier form converted
# ---------------------------------------------------------------------------------------------

DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "resources" / "lab_quant_input_defaults.json"
ACETATE_ADDUCT = "[M+CH3COO]-"
FORMATE_ADDUCT = "[M+HCOO]-"
INSTRUCTIONS = (
    ("MS-DIAL lipidomics quantification input, format 1", "title"),
    ("Fill in the four sheets Settings, Standards, Class standards and Selection. The full specification is "
     f"{SPEC} in the MS-DIAL Interactive repository.", "body"),
    ("Settings", "section"),
    ("Format must stay msdial-lipidomics-quant-input/1.", "body"),
    ("Sample amount: how much sample one extract was made from (10 for 10 uL plasma). Sample basis: uL plasma, "
     "mg tissue or 10^6 cells. Spiked amount unit: pmol unless you write nmol or fmol.", "body"),
    ("Standards: one row per internal standard", "section"),
    ("Spiked amount: how much of the standard is in the extract of one sample. It is divided by the sample amount, "
     "so 663.917 pmol in an extract of 10 uL plasma is 66.3917 pmol/uL plasma.", "body"),
    ("Pos adduct, Pos m/z, Pos RT: the standard's peak in positive mode; Neg ... the same in negative mode. Leave a "
     "polarity's cells blank where the standard is not used in it.", "body"),
    ("RT: the retention time in minutes you observe on your own chromatography. The prefilled rows leave it blank.",
     "body"),
    ("m/z: leave it blank to have it computed from Formula and the adduct, or write it; a difference of more than "
     "5 mDa from the formula is reported.", "body"),
    ("Class standards", "section"),
    ("Which standard quantifies which lipid class. 'Any others' covers every class not listed, and is required "
     "exactly once.", "body"),
    ("Selection", "section"),
    ("Which class and adduct each polarity reports. Use TRUE or FALSE; a blank cell counts as TRUE. Ammonium "
     "acetate gives [M+CH3COO]- in negative mode, ammonium formate [M+HCOO]-.", "body"),
    ("Class names", "section"),
    ("Either spelling of a class is accepted (Cer_NS or Cer-NS). The Class names sheet lists both for every class.",
     "body"),
    ("Numbers are written with a decimal point (0.5, not 0,5). Names, adducts and formulas are ASCII.", "body"),
)


def lab_defaults() -> dict[str, Any]:
    """The laboratory's EquiSPLASH standards (without amounts or RTs), class standards and selection."""
    return json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))


def _formate_selection(selection: list[list[Any]]) -> list[list[Any]]:
    """The acetate selection for ammonium formate: each used negative acetate row becomes its formate row."""
    rows = [list(row) for row in selection]
    index = {(row[0], row[1], row[2]): row for row in rows}
    for row in list(rows):
        if row[2] == "Negative" and row[1] == ACETATE_ADDUCT and row[3] == "TRUE":
            row[3] = "FALSE"
            formate = index.get((row[0], FORMATE_ADDUCT, "Negative"))
            if formate is None:
                formate = [row[0], FORMATE_ADDUCT, "Negative", "TRUE"]
                rows.append(formate)
                index[(row[0], FORMATE_ADDUCT, "Negative")] = formate
            formate[3] = "TRUE"
    return rows


def _default_tables(prefill: str, salt: str) -> tuple[dict[str, list[list[Any]]], list[str]]:
    notes: list[str] = []
    settings = [["Format", FORMAT_ID], ["Sample amount", ""], ["Sample basis", ""], ["Spiked amount unit", "pmol"],
                ["Description", ""]]
    if prefill == "none":
        return {"settings": settings, "standards": [], "class_standards": [], "selection": []}, notes
    defaults = lab_defaults()
    formate = salt == "HCOONH4"
    standards = []
    for item in defaults["standards"]:
        neg = item.get("neg_adduct", "")
        if formate and neg == ACETATE_ADDUCT:
            neg = FORMATE_ADDUCT
        standards.append([item["name"], item["class"], "", item.get("formula", ""), item.get("inchikey", ""),
                          item.get("smiles", ""), item.get("pos_adduct", ""), "", "", "", neg, "", "", ""])
    selection = [list(row) for row in defaults["selection"]]
    if formate:
        selection = _formate_selection(selection)
        notes.append("The negative-mode acetate adducts became formate adducts, for ammonium formate; review them.")
    elif salt == "NH4HCO3":
        notes.append("The defaults are for ammonium acetate; review the negative-mode adducts for ammonium bicarbonate.")
    notes.append(
        "Standards holds the laboratory's EquiSPLASH set without amounts or retention times: fill in each Spiked "
        "amount and each RT you observe, delete the standards you did not add, and add any others."
    )
    return {"settings": settings, "standards": standards,
            "class_standards": [list(row) for row in defaults["class_standards"]], "selection": selection}, notes


def converted_tables(
    quant_table_path: str | Path, amount_column: str, is_text_pos: str | Path, is_text_neg: str | Path,
    vocabulary: LipidClassVocabulary | None = None,
) -> tuple[dict[str, list[list[Any]]], list[str], list[str]]:
    """The earlier form (quant_utility_table.xlsx, its amount column, two text libraries) as format-1 tables.

    Returns (tables, problems, notes). The spiked amount is sheet 1's pmol column where it has one,
    and otherwise the amount per sample unit times the sample amount in row 4.
    """
    from .lab_lipidomics import read_internal_standard_text, read_quant_workbook, split_by_polarity

    vocabulary = vocabulary or load_lipid_class_vocabulary()
    problems: list[str] = []
    notes: list[str] = []
    workbook_path = Path(quant_table_path).expanduser()
    workbook = read_quant_workbook(workbook_path, amount_column, vocabulary)
    problems.extend(workbook.get("blockers", []))
    preparation = workbook.get("preparation") or {}
    if problems or not preparation.get("standards"):
        return {}, problems or ["The workbook lists no standard."], notes
    unit = preparation.get("unit") or ""
    prefix, _, basis_unit = unit.partition("mol_per_")
    basis = next((written for written, normalize_unit in BASES.values() if normalize_unit == basis_unit), "")
    spiked_unit = {value: key for key, value in SPIKED_UNITS.items()}.get(prefix, "pmol")
    # Sheet 1's pmol column: what is in the extract, before the laboratory divides by the sample amount.
    sheet = read_xlsx(workbook_path).sheets[0]
    header_row = preparation["header_row"]
    pmol_column = next(
        (column_letters(column) for column in range(26)
         if _column_key(sheet.text(f"{column_letters(column)}{header_row}")) == "pmol"),
        None,
    )
    spiked: dict[str, float] = {}
    ratios: list[float] = []
    for item in preparation["standards"]:
        value = sheet.number(f"{pmol_column}{item['row']}") if pmol_column else None
        if value is not None and item["amount"]:
            spiked[item["name"]] = value
            ratios.append(value / item["amount"])
    parameter = (preparation.get("sample_parameter") or {}).get("value")
    consistent = len(ratios) == len(preparation["standards"]) and all(
        math.isclose(ratio, ratios[0], rel_tol=1e-9) for ratio in ratios
    )
    if ratios and consistent:
        sample_amount = float(f"{ratios[0]:.12g}")
        notes.append(
            f"Spiked amounts are sheet 1's column {pmol_column}; the sample amount, {sample_amount:g} {basis}, is "
            f"their ratio to column {amount_column}."
        )
    elif parameter:
        sample_amount = float(parameter)
        spiked = {item["name"]: float(f"{item['amount'] * sample_amount:.12g}") for item in preparation["standards"]}
        notes.append(
            f"Spiked amounts are column {amount_column} times the sample amount in row {header_row - 1} "
            f"({sample_amount:g} {basis})."
        )
    else:
        return {}, [
            f"Sheet 1 gives neither a pmol column nor a sample amount in row {header_row - 1}, so the spiked amounts "
            "are unknown."
        ], notes
    texts: dict[str, list[InternalStandard]] = {POSITIVE: [], NEGATIVE: []}
    sources: list[str] = []
    positive_path, negative_path = Path(is_text_pos).expanduser(), Path(is_text_neg).expanduser()
    if positive_path.resolve() == negative_path.resolve():
        read = read_internal_standard_text(positive_path)
        problems.extend(read["blockers"])
        texts = split_by_polarity(read["records"])
        sources.append(positive_path.name)
    else:
        for polarity, path in ((POSITIVE, positive_path), (NEGATIVE, negative_path)):
            read = read_internal_standard_text(path, polarity)
            problems.extend(read["blockers"])
            texts[polarity] = read["records"]
            sources.append(path.name)
    if problems:
        return {}, problems, notes
    by_name = {polarity: {record.name: record for record in records} for polarity, records in texts.items()}
    names = [item["name"] for item in preparation["standards"]]
    extra = list(dict.fromkeys(name for polarity in POLARITIES for name in by_name[polarity] if name not in names))
    for name in extra:
        notes.append(f"{name} is in a text library but not on sheet 1; its Spiked amount is left blank for you to fill.")
    sheet_one = {item["name"]: item for item in preparation["standards"]}
    standards: list[list[Any]] = []
    for name in names + extra:
        records = {polarity: by_name[polarity].get(name) for polarity in POLARITIES}
        first = records[POSITIVE] or records[NEGATIVE]
        item = sheet_one.get(name)
        klass = item["class_name"] if item and item["class_name"] else (first.ontology if first else "")
        row: list[Any] = [name, klass, spiked.get(name, ""), first.formula if first else "",
                          first.inchikey if first else "", first.smiles if first else ""]
        for polarity in POLARITIES:
            record = records[polarity]
            if record is None:
                row.extend(["", "", "", ""])
            else:
                row.extend([record.adduct, record.mz, record.rt, record.ccs if record.ccs is not None else ""])
        standards.append(row)
    settings = [
        ["Format", FORMAT_ID], ["Sample amount", sample_amount], ["Sample basis", basis],
        ["Spiked amount unit", spiked_unit],
        ["Description", f"Converted from {workbook_path.name} (column {amount_column}) and {', '.join(sources)}."],
    ]
    return {
        "settings": settings,
        "standards": standards,
        "class_standards": [[row["target_class"], row["standard"]] for row in workbook["lookup"]["rows"]],
        "selection": [[row["class"], row["adduct"], row["ion_mode"], "TRUE" if row["selected"] else "FALSE"]
                      for row in workbook["quant_info"]["rows"]],
    }, problems, notes


def _class_names_rows(vocabulary: LipidClassVocabulary) -> list[list[Any]]:
    return [[member, "" if vs2 == UNDEFINED else vs2] for member, vs2 in vocabulary.enum_to_vs2.items()]


def _text_value(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    return "" if value is None else str(value)


def write_input(target: Path, layout: str, tables: dict[str, list[list[Any]]], vocabulary: LipidClassVocabulary) -> list[str]:
    """Write the four tables (and, in a workbook, the Instructions and Class names sheets); the paths written."""
    if layout == "tsv":
        target.mkdir(parents=True, exist_ok=True)
        written = []
        for key in TABLES:
            path = target / f"{FILE_STEMS[key]}.tsv"
            buffer = io.StringIO()
            writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
            writer.writerow(HEADERS[key])
            for row in tables.get(key, []):
                writer.writerow([_text_value(value) for value in row])
            path.write_bytes(buffer.getvalue().encode("utf-8"))  # LF on every system
            written.append(str(path))
        return written
    from .supplementary_excel import _write_xlsx

    def sheet(key: str, widths: list[int], validations: list[tuple[str, list[str]]] | None = None) -> dict[str, Any]:
        rows = [{"values": list(HEADERS[key]), "style": "header"}]
        rows += [{"values": list(row), "style": "body_left"} for row in tables.get(key, [])]
        return {"name": SHEET_NAMES[key], "rows": rows, "widths": widths, "freeze_rows": 1,
                "validations": validations or []}

    last = 2000
    styles = {"title": "section", "section": "header", "body": "body_left"}
    sheets = [
        {"name": "Instructions", "widths": [120],
         "rows": [{"values": [text], "style": styles[style]} for text, style in INSTRUCTIONS]},
        sheet("settings", [22, 48], [("B3", list(BASIS_CHOICES)), ("B4", list(SPIKED_UNITS))]),
        sheet("standards", [30, 12, 15, 18, 30, 30, 14, 12, 9, 10, 14, 12, 9, 10]),
        sheet("class_standards", [18, 30]),
        sheet("selection", [16, 18, 12, 9], [(f"C2:C{last}", ["Positive", "Negative"]), (f"D2:D{last}", ["TRUE", "FALSE"])]),
        {"name": "Class names", "widths": [26, 40], "freeze_rows": 1,
         "rows": [{"values": ["LBM spelling (Selection)", "Class-definition spelling (Class standards)"], "style": "header"}]
         + [{"values": row, "style": "body_left"} for row in _class_names_rows(vocabulary)]},
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_xlsx(target, sheets)
    return [str(target)]


def quant_input_template(request: dict[str, Any]) -> dict[str, Any]:
    """Preview, then write, a format-1 input: empty with the laboratory's defaults, or converted from the earlier form."""
    from .lab_lipidomics import normalize_salt

    vocabulary = load_lipid_class_vocabulary()
    output = str(request.get("output_path") or "").strip()
    layout = str(request.get("layout") or "xlsx").strip().lower()
    prefill = str(request.get("prefill") or "lab-defaults").strip().lower()
    salt = str(request.get("salt") or "").strip()
    source = str(request.get("from_quant_table_path") or "").strip()
    if not output:
        raise ValueError("output_path is required: the .xlsx to write, or the folder for layout='tsv'.")
    if layout not in {"xlsx", "tsv"}:
        raise ValueError("layout is 'xlsx' (one workbook) or 'tsv' (a folder of four text files).")
    if prefill not in {"lab-defaults", "none"}:
        raise ValueError("prefill is 'lab-defaults' or 'none'.")
    if salt:
        mapped = normalize_salt(salt)
        if mapped.get("problem"):
            raise ValueError(f"salt: {mapped['problem']}")
        salt = mapped["salt"]
    target = Path(output).expanduser()
    if layout == "xlsx" and target.suffix.casefold() != ".xlsx":
        raise ValueError(f"{target.name}: a workbook is written as .xlsx.")
    if target.exists() and (target.is_file() or any(target.iterdir())):
        raise ValueError(f"{target} already exists; this tool never overwrites. Name a new file or an empty folder.")
    problems: list[str] = []
    if source:
        texts = [str(request.get(key) or "").strip() for key in ("is_text_pos", "is_text_neg")]
        amount_column = str(request.get("amount_column") or "").strip()
        if not all(texts) or not amount_column:
            raise ValueError("Converting the earlier form needs from_quant_table_path, amount_column, is_text_pos and is_text_neg.")
        tables, problems, notes = converted_tables(source, amount_column, texts[0], texts[1], vocabulary)
        kind = "converted"
    else:
        tables, notes = _default_tables(prefill, salt)
        kind = "template"
    preview = {
        "kind": kind, "layout": layout, "path": str(target), "format": FORMAT_ID,
        "rows": {SHEET_NAMES[key]: len(tables.get(key, [])) for key in TABLES},
        "standards": [row[0] for row in tables.get("standards", [])], "notes": notes, "problems": problems,
        "specification": SPEC,
    }
    if problems:
        return {"written": False, **preview, "message": "Nothing was written: the source has problems; fix them first."}
    if request.get("confirmed") is not True:
        return {"written": False, "confirmation_required": True, **preview,
                "message": "Show the person what would be written, then call again with confirmed=true."}
    written = write_input(target, layout, tables, vocabulary)
    check = read_quant_input(target, vocabulary)
    return {
        "written": True, **preview, "files": written,
        # What is still to fill in: an empty template's blanks are expected to be problems.
        "check": {"blockers": check["blockers"], "warnings": check["warnings"], "notes": check["notes"][:40]},
        "next": "Fill in what the check lists, save, and check it again with msdial_lipidomics_check_quant_input.",
    }

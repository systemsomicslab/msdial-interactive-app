"""Synthetic .xlsx workbooks for the laboratory lipidomics tests, built with zipfile only.

Each cell is given as a Python value: a str becomes a shared string, a bool an Excel boolean,
an int or float a number; Formula, Error, Inline and Rich cover the rest of what a saved
workbook can hold.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"
WORKSHEET = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
SHARED = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"


@dataclass
class Formula:
    text: str
    cached: Any = None


@dataclass
class Error:
    code: str


@dataclass
class Inline:
    text: str


@dataclass
class Rich:
    runs: list[str]
    phonetic: str = ""


@dataclass
class Sheet:
    name: str
    cells: dict[str, Any]
    merged: list[str] = field(default_factory=list)


def _split(ref: str) -> tuple[int, int]:
    letters = "".join(char for char in ref if char.isalpha())
    number = 0
    for char in letters:
        number = number * 26 + ord(char) - 64
    return int(ref[len(letters):]), number


def build_xlsx(path: Path, sheets: list[Sheet], *, full_calc: bool = False, part_names: list[str] | None = None) -> Path:
    shared: list[str] = []

    def shared_index(xml: str) -> int:
        shared.append(xml)
        return len(shared) - 1

    def cell_xml(ref: str, value: Any) -> str:
        if isinstance(value, Formula):
            cached = value.cached
            formula = f"<f>{escape(value.text)}</f>"
            if cached is None:
                return f'<c r="{ref}">{formula}</c>'
            if isinstance(cached, str):
                return f'<c r="{ref}" t="str">{formula}<v>{escape(cached)}</v></c>'
            return f'<c r="{ref}">{formula}<v>{cached!r}</v></c>'
        if isinstance(value, Error):
            return f'<c r="{ref}" t="e"><v>{escape(value.code)}</v></c>'
        if isinstance(value, Inline):
            return f'<c r="{ref}" t="inlineStr"><is><t>{escape(value.text)}</t></is></c>'
        if isinstance(value, Rich):
            runs = "".join(f"<r><t>{escape(run)}</t></r>" for run in value.runs)
            phonetic = f'<rPh sb="0" eb="1"><t>{escape(value.phonetic)}</t></rPh>' if value.phonetic else ""
            return f'<c r="{ref}" t="s"><v>{shared_index(runs + phonetic)}</v></c>'
        if isinstance(value, bool):
            return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"><v>{value!r}</v></c>'
        return f'<c r="{ref}" t="s"><v>{shared_index(f"<t>{escape(str(value))}</t>")}</v></c>'

    parts = part_names or [f"sheet{index + 1}.xml" for index in range(len(sheets))]
    sheet_parts = {}
    for sheet, part in zip(sheets, parts):
        rows: dict[int, list[tuple[int, str]]] = {}
        for ref, value in sheet.cells.items():
            row, column = _split(ref)
            rows.setdefault(row, []).append((column, cell_xml(ref, value)))
        body = "".join(
            f'<row r="{row}">' + "".join(xml for _, xml in sorted(cells)) + "</row>"
            for row, cells in sorted(rows.items())
        )
        merges = ""
        if sheet.merged:
            merges = f'<mergeCells count="{len(sheet.merged)}">' + "".join(
                f'<mergeCell ref="{item}"/>' for item in sheet.merged) + "</mergeCells>"
        sheet_parts[part] = f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{MAIN}"><sheetData>{body}</sheetData>{merges}</worksheet>'
    calc = '<calcPr calcId="191029" fullCalcOnLoad="1"/>' if full_calc else '<calcPr calcId="191029"/>'
    workbook = (
        f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{MAIN}" xmlns:r="{RELATIONSHIPS}"><sheets>'
        + "".join(
            f'<sheet name="{escape(sheet.name)}" sheetId="{index + 1}" r:id="rId{index + 1}"/>'
            for index, sheet in enumerate(sheets)
        )
        + f"</sheets>{calc}</workbook>"
    )
    rels = (
        f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PACKAGE}">'
        + "".join(
            f'<Relationship Id="rId{index + 1}" Type="{WORKSHEET}" Target="worksheets/{part}"/>'
            for index, part in enumerate(parts)
        )
        + f'<Relationship Id="rIdS" Type="{SHARED}" Target="sharedStrings.xml"/></Relationships>'
    )
    strings = (
        f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{MAIN}" count="{len(shared)}">'
        + "".join(f"<si>{item}</si>" for item in shared) + "</sst>"
    )
    root_rels = (
        f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PACKAGE}">'
        f'<Relationship Id="rId1" Type="{OFFICE}" Target="xl/workbook.xml"/></Relationships>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types/>')
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/sharedStrings.xml", strings)
        for part, xml in sheet_parts.items():
            archive.writestr(f"xl/worksheets/{part}", xml)
    return path


# A small quant_utility_table laid out as the laboratory's: amounts in rows 3-4, the header in
# row 5, the standards below it, then a gap and a worked example that repeats a standard.
STANDARDS = [
    # name, class, pmol in the vial
    ("PC 15:0_18:1(d7)", "PC", 663.917),
    ("LPC 18:1(d7)", "LPC", 945.6835),
    ("Cer 18:1;O2/15:0(d7)", "Cer-NS", 941.7435),
    ("CE 18:1(d7)", "CE", 759.6937),
    ("FA 16:0(d3)", "FA", 3200.0),
    ("FA 18:0(d3)", "FA", 3200.0),
]
LOOKUP = [
    ("PC 15:0_18:1(d7)", "PC"),
    ("PC 15:0_18:1(d7)", "EtherPC"),
    ("LPC 18:1(d7)", "LPC"),
    ("Cer 18:1;O2/15:0(d7)", "Cer-NS"),
    ("Cer 18:1;O2/15:0(d7)", "HexCer-NS"),
    ("CE 18:1(d7)", "CE"),
    ("CE 18:1(d7)", "AHexCS"),
    ("FA 18:0(d3)", "FA"),
    ("LPC 18:1(d7)", "Any others"),
    ("LPC 18:1(d7)", "CAR"),
]
QUANT_INFO = [
    ("PC", "[M+H]+", "Positive", False),
    ("PC", "[M+CH3COO]-", "Negative", True),
    ("PC", "[M+HCOO]-", "Negative", False),
    ("LPC", "[M+H]+", "Positive", True),
    ("Cer_NS", "[M+CH3COO]-", "Negative", True),
    ("HexCer_NS", "[M+H]+", "Positive", True),
    ("CE", "[M+NH4]+", "Positive", True),
    ("AHexCS", "[M+CH3COO]-", "Negative", True),
    ("FA", "[M-H]-", "Negative", True),
    ("EtherPE", "[M+H]+", "Positive", True),
    ("EtherPE", "[M-H]-", "Negative", True),
    ("LipidA", "[M-H]-", "Negative", False),
    ("WE", "[M+NH4]+", "Positive", True),
]


def preparation_sheet(standards=STANDARDS, *, plasma_microliters: float = 10.0, extra: dict[str, Any] | None = None) -> Sheet:
    cells: dict[str, Any] = {
        "E3": "Put liquid volume in LC-MS vial", "F3": "Put mg tissue weight in solvent",
        "G3": "Put cell number in solvent when 10^6 cells cosidered", "H3": "Put uL in solvent",
        # F4 differs from H4, so that reading one column for the other shows.
        "E4": 320, "F4": 5, "G4": 0.25, "H4": plasma_microliters,
        "B5": "Name", "C5": "mM in LC-MS vial", "D5": "Class name", "E5": "pmol",
        "F5": "int (pmol/mg tissue)", "G5": "int (pmol/10^6 cells)", "H5": "int (pmol/ul plasma)",
    }
    row = 6
    for name, klass, pmol in standards:
        cells[f"B{row}"] = name
        cells[f"C{row}"] = pmol / 320
        cells[f"D{row}"] = klass
        cells[f"E{row}"] = Formula(f"C{row}*$E$4", pmol)
        cells[f"F{row}"] = Formula(f"E{row}/$F$4", pmol / 5)
        cells[f"G{row}"] = Formula(f"E{row}*$G$4", pmol * 0.25)
        cells[f"H{row}"] = Formula(f"E{row}/$H$4", pmol / plasma_microliters)
        row += 1
    # A gap, then the worked example that must never be read as a standard.
    cells[f"C{row + 2}"] = "pmol/uL plasma"
    cells[f"B{row + 3}"] = standards[0][0]
    cells[f"C{row + 3}"] = 13.27834
    cells[f"F{row + 5}"] = "plasma 20 uL"
    cells.update(extra or {})
    return Sheet("1. IS preparation", cells)


def lookup_sheet(lookup=LOOKUP, standards=STANDARDS, *, plasma_microliters: float = 10.0, header: bool = False) -> Sheet:
    amounts = {name: pmol / plasma_microliters for name, _, pmol in standards}
    cells: dict[str, Any] = {}
    row = 1
    if header:
        cells.update({"A1": "StandardName", "B1": "Amount", "C1": "TargetClass"})
        row = 2
    for name, target in lookup:
        cells[f"A{row}"] = name
        cells[f"B{row}"] = Formula(f"VLOOKUP(A{row},$H$1:$I$15,2,FALSE)", amounts.get(name, 0.0))
        cells[f"C{row}"] = target
        cells[f"D{row}"] = -1
        row += 1
    return Sheet("2. Lookup table", cells)


def quant_sheet(rows=QUANT_INFO) -> Sheet:
    cells: dict[str, Any] = {"A1": "Class", "B1": "Adduct", "C1": "Ion mode", "D1": "IsSelected"}
    for index, (klass, adduct, mode, selected) in enumerate(rows, start=2):
        cells.update({f"A{index}": klass, f"B{index}": adduct, f"C{index}": mode, f"D{index}": selected})
    return Sheet("3. Quant info", cells)


def quant_workbook(path: Path, **options: Any) -> Path:
    return build_xlsx(path, [
        options.get("preparation") or preparation_sheet(),
        options.get("lookup") or lookup_sheet(),
        options.get("quant") or quant_sheet(),
    ], full_calc=options.get("full_calc", False))


POSITIVE_LIBRARY = (
    "NAME\tMZ\tRT\tAdduct\tInChIKey\tFormula\tSMILES\tOntology\tCCS\r\n"
    "PC 15:0_18:1(d7)\t753.6134\t9.5\t[M+H]+\tZEWLMKXMNQOCOQ-GCHPQBSENA-N\tC41H73D7NO8P\tC\tPC\t285.9533\r\n"
    "LPC 18:1(d7)\t529.39934\t6.33\t[M+H]+\tYAMUFBLWGFFICM-HNNXNMBSNA-N\tC26H45D7NO7P\tC\tLPC\t231.1935\r\n"
    "Cer 18:1;O2/15:0(d7)\t531.5476588\t9.34\t[M+H]+\tHBULQAPKKLNTLT-BXLQGFJZSA-N\tC33H58D7NO3\tC\tCer-NS\t254.166\r\n"
    "CE 18:1(d7)\t675.67785\t14.94\t[M+NH4]+\tRJECHNNFRHZQKU-LCUGTLGDNA-N\tC45H71D7O2\tC\tCE\t287.669\r\n"
)
NEGATIVE_LIBRARY = (
    "Name\tMZ\tRT\tAdduct\tInChIKey\tFormula\tSMILES\tOntology\tCCS\r\n"
    "PC 15:0_18:1(d7)\t811.6199\t9.5\t[M+CH3COO]-\tZEWLMKXMNQOCOQ-GCHPQBSENA-N\tC41H73D7NO8P\tC\tPC\t284.7864\r\n"
    "LPC 18:1(d7)\t587.40594\t6.33\t[M+CH3COO]-\tYAMUFBLWGFFICM-HNNXNMBSNA-N\tC26H45D7NO7P\tC\tLPC\t239.8291\r\n"
    "Cer 18:1;O2/15:0(d7)\t589.5542353\t9.34\t[M+CH3COO]-\tHBULQAPKKLNTLT-BXLQGFJZSA-N\tC33H58D7NO3\tC\tCer-NS\t249.1731\r\n"
    "FA 18:0(d3)\t286.2830842\t6.96\t[M-H]-\tQIQXTHQIDYTFRH-FIBGUPNXSA-N\tC18H33D3O2\tC\tFA\t177.728\r\n"
    "FA 16:0(d3)\t258.2517841\t6.3\t[M-H]-\tIPCSVZSSVZVIGE-FIBGUPNXSA-N\tC16H29D3O2\tC\tFA\t169.563\r\n"
)

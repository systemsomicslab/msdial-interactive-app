"""Laboratory lipidomics: the analyst's inputs, the internal standards in a run, and the check
of what MS-DIAL's normalize made of them.

A laboratory lipidomics batch arrives with three things besides the raw data: a text file per
polarity naming the internal standards (IS) and where they elute, a workbook saying how much
of each standard went into a sample and which standard quantifies which lipid class, and a
decision about which class and adduct to keep from which polarity. Each is a scientific input
someone prepared by hand, and each fails quietly when it disagrees with MS-DIAL: a class
spelled the way the other sheet spells it falls through to "Any others", a standard MS-DIAL
annotated as another adduct quantifies a class against the wrong peak, a formula cell Excel
never calculated reads as an empty amount. So everything here reads, compares and reports;
nothing here runs MS-DIAL or writes outside the paths it is handed.

The arithmetic belongs to MS-DIAL (`MSDIALCUI normalize`). What Interactive adds is the part
the Console cannot do on its own -- deciding which aligned peak *is* each standard, by the
adduct the analyst named, the retention time they measured and the intensity the samples show
-- and then recomputing every quantified cell from the Height matrix the same call wrote, so a
number that does not follow from its measurement is never handed on as a concentration.

Pure: the standard library only, and no file is written except by the writers that are given
a path (the canonical text library, the standards table and the resolution table).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

from .lipidome_merge import MergeRefused, describe_export, strip_polarity

ROOT = Path(__file__).resolve().parent.parent
VOCABULARY_PATH = ROOT / "resources" / "lipid_class_vocabulary.tsv"

POSITIVE = "Positive"
NEGATIVE = "Negative"
POLARITIES = (POSITIVE, NEGATIVE)
# The Text annotator that carries a polarity's internal standards in the run. Its ID is what an
# export's Comment names as the annotation method, so the resolver and the method writer must agree.
INTERNAL_STANDARD_ANNOTATOR_IDS = {POSITIVE: "internal_standards_pos", NEGATIVE: "internal_standards_neg"}
ANY_OTHERS = "Any others"
UNDEFINED = "Undefined"
UNKNOWN_CLASS = "Unknown"
# MS-DIAL's Text-DB m/z rule: Da up to m/z 500, the same relative width above it (LcmsTextDBAnnotator).
MS1_TOLERANCE = 0.01
MS1_TOLERANCE_PIVOT_MZ = 500.0
DEFAULT_INTERNAL_STANDARD_RT_TOLERANCE = 0.5
FORMULA_MZ_WARNING_DA = 0.005


class LabInputError(ValueError):
    """An input that cannot be read as what it claims to be."""


# ---------------------------------------------------------------------------------------------
# xlsx, with the standard library
# ---------------------------------------------------------------------------------------------

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_DOCUMENT_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OFFICE_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
_CELL_REF = re.compile(r"^([A-Z]{1,3})(\d+)$")


def column_index(letters: str) -> int:
    """Zero-based index of a column letter: A -> 0, H -> 7, AA -> 26."""
    value = 0
    for char in letters.strip().upper():
        if not "A" <= char <= "Z":
            raise ValueError(f"'{letters}' is not a column letter.")
        value = value * 26 + (ord(char) - 64)
    if value == 0:
        raise ValueError("An empty column letter.")
    return value - 1


def column_letters(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def split_ref(ref: str) -> tuple[int, int]:
    """(row, zero-based column) of a cell reference such as 'H4'."""
    match = _CELL_REF.match(ref.strip().upper())
    if not match:
        raise ValueError(f"'{ref}' is not a cell reference.")
    return int(match.group(2)), column_index(match.group(1))


@dataclass
class XlsxCell:
    ref: str
    # string, number, boolean, error, or uncached_formula: a formula Excel never calculated.
    kind: str
    value: Any
    raw: str = ""
    formula: str | None = None


@dataclass
class XlsxSheet:
    name: str
    part: str
    cells: dict[tuple[int, int], XlsxCell]
    merged: list[tuple[int, int, int, int]] = field(default_factory=list)
    max_row: int = 0

    def cell(self, ref: str) -> XlsxCell | None:
        """The cell at ref; inside a merged range, the range's top-left cell, which holds its value."""
        row, column = split_ref(ref)
        found = self.cells.get((row, column))
        if found is not None:
            return found
        for top, left, bottom, right in self.merged:
            if top <= row <= bottom and left <= column <= right:
                return self.cells.get((top, left))
        return None

    def value(self, ref: str) -> Any:
        found = self.cell(ref)
        if found is None:
            return None
        if found.kind == "uncached_formula":
            raise LabInputError(
                f"'{self.name}'!{found.ref} holds a formula with no saved value ({found.formula}). "
                "Open the workbook in Excel and save it, so the value it shows is the value read."
            )
        return found.value

    def text(self, ref: str) -> str:
        found = self.cell(ref)
        if found is None or found.kind == "error":
            return ""
        value = self.value(ref)
        if value is None:
            return ""
        if found.kind == "number":
            return found.raw
        if found.kind == "boolean":
            return "TRUE" if value else "FALSE"
        return str(value)

    def number(self, ref: str) -> float | None:
        found = self.cell(ref)
        if found is None:
            return None
        value = self.value(ref)
        if found.kind == "number":
            return float(value)
        if found.kind == "string":
            return parse_number(str(value))
        return None


@dataclass
class XlsxWorkbook:
    path: str
    sha256: str
    sheets: list[XlsxSheet]
    full_calc_on_load: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def sheet_names(self) -> list[str]:
        return [sheet.name for sheet in self.sheets]


def read_xlsx(path: str | Path) -> XlsxWorkbook:
    """Read every sheet's saved values: what Excel showed when the workbook was last saved.

    Formula cells are read from the value Excel cached beside the formula. A formula with no
    cached value is kept as such and refused when it is read, rather than read as empty. Rich
    text is joined from its runs, without the phonetic guides a Japanese Excel adds (rPh).
    """
    source = Path(path)
    try:
        data = source.read_bytes()
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as error:
        raise LabInputError(f"{source} cannot be read as an .xlsx workbook: {error}") from None
    with archive:
        names = set(archive.namelist())
        workbook_part = _office_document_part(archive, names)
        workbook = _xml(archive, workbook_part)
        relationships = _relationships(archive, names, workbook_part)
        shared = _shared_strings(archive, names, workbook_part, relationships)
        calc = workbook.find(f"{_MAIN}calcPr")
        full_calc = calc is not None and calc.get("fullCalcOnLoad") in {"1", "true"}
        sheets: list[XlsxSheet] = []
        sheets_element = workbook.find(f"{_MAIN}sheets")
        for element in sheets_element if sheets_element is not None else []:
            target = relationships.get(element.get(f"{_DOCUMENT_REL}id") or "", "")
            part = _resolve_part(workbook_part, target)
            if part not in names:
                raise LabInputError(f"{source}: sheet '{element.get('name')}' points at a missing part {part}.")
            sheets.append(_read_sheet(archive, part, element.get("name") or "", shared))
    warnings = []
    if full_calc:
        # Excel set this because the cached values may be out of date; a reader of the saved
        # values cannot recalculate, so the numbers below may not be the ones Excel would show.
        warnings.append(
            f"{source.name} asks Excel to recalculate every formula when it opens (fullCalcOnLoad); "
            "its saved values may be stale. Open it in Excel and save it before it is read."
        )
    return XlsxWorkbook(
        path=str(source.resolve()), sha256=hashlib.sha256(data).hexdigest(), sheets=sheets,
        full_calc_on_load=full_calc, warnings=warnings,
    )


def excel_lock_files(path: str | Path) -> list[str]:
    """Excel's lock files beside a workbook: it is open, and its unsaved edits are invisible here."""
    source = Path(path)
    candidates = {source.with_name("~$" + source.name), source.with_name("~$" + source.name[2:])}
    return sorted(str(candidate) for candidate in candidates if candidate.exists())


def _xml(archive: zipfile.ZipFile, part: str) -> ET.Element:
    try:
        return ET.fromstring(archive.read(part))
    except (KeyError, ET.ParseError) as error:
        raise LabInputError(f"The workbook part {part} cannot be read: {error}") from None


def _office_document_part(archive: zipfile.ZipFile, names: set[str]) -> str:
    if "_rels/.rels" in names:
        for item in _xml(archive, "_rels/.rels").iter(f"{_PACKAGE_REL}Relationship"):
            if item.get("Type") == _OFFICE_DOCUMENT:
                return _resolve_part("", item.get("Target") or "")
    if "xl/workbook.xml" in names:
        return "xl/workbook.xml"
    raise LabInputError("The archive has no workbook part; it is not an .xlsx workbook.")


def _relationships(archive: zipfile.ZipFile, names: set[str], part: str) -> dict[str, str]:
    folder, _, base = part.rpartition("/")
    rels = f"{folder}/_rels/{base}.rels" if folder else f"_rels/{base}.rels"
    if rels not in names:
        return {}
    return {
        item.get("Id") or "": item.get("Target") or ""
        for item in _xml(archive, rels).iter(f"{_PACKAGE_REL}Relationship")
    }


def _resolve_part(base_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    folder = base_part.rpartition("/")[0]
    parts = [item for item in (folder.split("/") if folder else []) if item]
    for item in target.split("/"):
        if item == "..":
            if parts:
                parts.pop()
        elif item and item != ".":
            parts.append(item)
    return "/".join(parts)


def _rich_text(element: ET.Element) -> str:
    # A string item is either one <t> or a run of <r><t>; <rPh> phonetic guides are not text.
    pieces = []
    for child in element:
        if child.tag == f"{_MAIN}t":
            pieces.append(child.text or "")
        elif child.tag == f"{_MAIN}r":
            text = child.find(f"{_MAIN}t")
            pieces.append(text.text or "" if text is not None else "")
    return "".join(pieces)


def _shared_strings(
    archive: zipfile.ZipFile, names: set[str], workbook_part: str, relationships: dict[str, str]
) -> list[str]:
    part = ""
    for target in relationships.values():
        if target.rsplit("/", 1)[-1].lower() == "sharedstrings.xml":
            part = _resolve_part(workbook_part, target)
    if not part:
        part = "xl/sharedStrings.xml"
    if part not in names:
        return []
    return [_rich_text(item) for item in _xml(archive, part).iter(f"{_MAIN}si")]


def _read_sheet(archive: zipfile.ZipFile, part: str, name: str, shared: list[str]) -> XlsxSheet:
    root = _xml(archive, part)
    cells: dict[tuple[int, int], XlsxCell] = {}
    max_row = 0
    for row_element in root.iter(f"{_MAIN}row"):
        for element in row_element.findall(f"{_MAIN}c"):
            ref = element.get("r") or ""
            try:
                row, column = split_ref(ref)
            except ValueError:
                continue
            cell = _read_cell(element, ref, shared)
            if cell is None:
                continue
            cells[(row, column)] = cell
            max_row = max(max_row, row)
    merged = []
    for item in root.iter(f"{_MAIN}mergeCell"):
        first, _, last = (item.get("ref") or "").partition(":")
        try:
            top, left = split_ref(first)
            bottom, right = split_ref(last or first)
        except ValueError:
            continue
        merged.append((top, left, bottom, right))
        max_row = max(max_row, bottom)
    return XlsxSheet(name=name, part=part, cells=cells, merged=merged, max_row=max_row)


def _read_cell(element: ET.Element, ref: str, shared: list[str]) -> XlsxCell | None:
    kind = element.get("t") or "n"
    value_element = element.find(f"{_MAIN}v")
    formula_element = element.find(f"{_MAIN}f")
    formula = None
    if formula_element is not None:
        formula = "=" + formula_element.text if formula_element.text else "=(shared formula)"
    raw = value_element.text if value_element is not None and value_element.text is not None else None
    if kind == "inlineStr":
        inline = element.find(f"{_MAIN}is")
        return XlsxCell(ref, "string", _rich_text(inline) if inline is not None else "", formula=formula)
    if raw is None:
        if formula is not None:
            return XlsxCell(ref, "uncached_formula", None, formula=formula)
        return None
    if kind == "s":
        try:
            return XlsxCell(ref, "string", shared[int(raw)], raw=raw, formula=formula)
        except (ValueError, IndexError):
            raise LabInputError(f"Cell {ref} names shared string {raw}, which the workbook does not hold.") from None
    if kind in {"str", "d"}:
        return XlsxCell(ref, "string", raw, raw=raw, formula=formula)
    if kind == "b":
        return XlsxCell(ref, "boolean", raw.strip() == "1", raw=raw, formula=formula)
    if kind == "e":
        # #N/A from a lookup that found nothing, #DIV/0! and the like: never a value.
        return XlsxCell(ref, "error", None, raw=raw, formula=formula)
    try:
        return XlsxCell(ref, "number", float(raw), raw=raw, formula=formula)
    except ValueError:
        return XlsxCell(ref, "string", raw, raw=raw, formula=formula)


def parse_number(text: Any) -> float | None:
    """A number written the invariant way; None for anything else, including a decimal comma."""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text) if math.isfinite(float(text)) else None
    value = str(text).strip()
    if not re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?", value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return {1: True, 0: False}.get(value)
    text = str(value if value is not None else "").strip().lower()
    return {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}.get(text)


def _header_key(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


# ---------------------------------------------------------------------------------------------
# Lipid class vocabulary
# ---------------------------------------------------------------------------------------------


class LipidClassVocabulary:
    """MS-DIAL's two spellings of a lipid class and the conversions between them.

    The LbmClass enum spelling (`Cer_NS`) is what the LBM library, `Searched lipid class`, an
    export's Ontology column and sheet 3 use. The VS2 class definition (`Cer-NS`) is what
    normalization looks a standard up by, and what sheet 2 must use. Generated from MS-DIAL's
    own switches (scripts/generate-lipid-class-vocabulary.py).
    """

    def __init__(self, enum_to_vs2: dict[str, str], name_to_enum: dict[str, str], source: str = "") -> None:
        self.enum_to_vs2 = dict(enum_to_vs2)
        self.name_to_enum = dict(name_to_enum)
        self.source = source
        names: list[str] = []
        for member, vs2 in self.enum_to_vs2.items():
            if vs2 != UNDEFINED and vs2 not in names:
                names.append(vs2)
        # LipidomicsConverter.GetLipidClasses: the defined VS2 names, in enum order.
        self.vs2_names = tuple(names)
        self._vs2_set = frozenset(names)

    def is_lbm_class(self, name: str) -> bool:
        return name in self.enum_to_vs2

    def vs2_of_enum(self, member: str) -> str:
        return self.enum_to_vs2.get(member, UNDEFINED)

    def lbm_class(self, name: str) -> str:
        """ConvertMsdialClassDefinitionToLbmClassEnumVS2: Undefined for a name it does not know."""
        return self.name_to_enum.get(name, UNDEFINED)

    def canonical_vs2(self, name: str) -> str:
        """The VS2 name MS-DIAL arrives at for any spelling, through the enum: Cer_NS -> Cer-NS."""
        return self.vs2_of_enum(self.lbm_class(name))

    def is_vs2(self, name: str) -> bool:
        return name in self._vs2_set


@lru_cache(maxsize=4)
def load_lipid_class_vocabulary(path: str | None = None) -> LipidClassVocabulary:
    source = Path(path) if path else VOCABULARY_PATH
    enum_to_vs2: dict[str, str] = {}
    name_to_enum: dict[str, str] = {}
    origin = ""
    for line in source.read_text(encoding="ascii").splitlines():
        if line.startswith("#"):
            if not origin and "MsdialWorkbench" in line:
                origin = line.lstrip("# ").strip()
            continue
        cells = line.split("\t")
        if len(cells) != 3 or cells[0] == "table":
            continue
        if cells[0] == "enum_to_vs2":
            enum_to_vs2[cells[1]] = cells[2]
        elif cells[0] == "name_to_enum":
            name_to_enum[cells[1]] = cells[2]
    if not enum_to_vs2 or not name_to_enum:
        raise LabInputError(f"{source} holds no lipid class vocabulary.")
    return LipidClassVocabulary(enum_to_vs2, name_to_enum, origin)


def vs2_class(lbm_class_name: str) -> str:
    """The VS2 class definition of an LbmClass member (Cer_NS -> Cer-NS); Undefined if it has none."""
    return load_lipid_class_vocabulary().vs2_of_enum(lbm_class_name)


def lbm_class(name: str) -> str:
    """The LbmClass member MS-DIAL reads a class name as (Cer-NS or Cer_NS -> Cer_NS)."""
    return load_lipid_class_vocabulary().lbm_class(name)


# ---------------------------------------------------------------------------------------------
# Masses and adducts
# ---------------------------------------------------------------------------------------------

ELECTRON_MASS = 0.00054857990946
ELEMENT_MASSES = {
    "H": 1.00782503207, "D": 2.0141017778, "C": 12.0, "N": 14.0030740048, "O": 15.99491461956,
    "P": 30.97376163, "S": 31.97207100, "Na": 22.9897692809, "K": 38.96370668, "Li": 7.01600455,
    "Cl": 34.96885268, "Br": 78.9183371, "F": 18.99840322, "I": 126.904473, "Si": 27.9769265325,
}
# The abbreviations MS-DIAL's adduct names use for whole molecules.
ADDUCT_GROUPS = {
    "Hac": "C2H4O2", "FA": "CH2O2", "ACN": "C2H3N", "IsoProp": "C3H8O", "DMSO": "C2H6OS",
    "TFA": "C2HF3O2", "MeOH": "CH4O",
}
_ADDUCT = re.compile(r"^\[(\d*)M((?:[+-]\d*[A-Z][A-Za-z0-9]*)*)\](\d*)([+-])$")
_ADDUCT_PART = re.compile(r"([+-])(\d*)([A-Z][A-Za-z0-9]*)")
_FORMULA_PART = re.compile(r"([A-Z][a-z]?)(\d*)")
ACETATE = frozenset({("C", 2), ("H", 3), ("O", 2)})
FORMATE = frozenset({("C", 1), ("H", 1), ("O", 2)})
BICARBONATE = frozenset({("C", 1), ("H", 1), ("O", 3)})


def formula_composition(formula: str) -> Counter:
    """Element counts of a plain formula such as C41H73D7NO8P; D is deuterium."""
    text = str(formula).strip()
    if not text:
        raise ValueError("An empty formula.")
    counts: Counter = Counter()
    position = 0
    for match in _FORMULA_PART.finditer(text):
        if match.start() != position:
            break
        element, count = match.group(1), match.group(2)
        if element not in ELEMENT_MASSES:
            raise ValueError(f"'{formula}' names an element this check does not know: {element}.")
        counts[element] += int(count) if count else 1
        position = match.end()
    if position != len(text):
        raise ValueError(f"'{formula}' is not a formula this check can read.")
    return counts


def composition_mass(composition: Counter) -> float:
    return sum(ELEMENT_MASSES[element] * count for element, count in composition.items())


def formula_mass(formula: str) -> float:
    """Monoisotopic mass of a neutral formula."""
    return composition_mass(formula_composition(formula))


@dataclass(frozen=True)
class AdductIon:
    name: str
    multimer: int
    charge: int
    sign: int  # +1 positive, -1 negative
    composition: tuple[tuple[str, int], ...]

    @property
    def polarity(self) -> str:
        return POSITIVE if self.sign > 0 else NEGATIVE

    @property
    def identity(self) -> tuple[Any, ...]:
        # [M+CH3COO]- and [M+Hac-H]- are one ion with two names; they share this.
        return (self.multimer, self.charge, self.sign, self.composition)

    @property
    def mass_shift(self) -> float:
        return composition_mass(Counter(dict(self.composition)))


def parse_adduct(text: str) -> AdductIon:
    """Read an adduct name such as [M+H]+, [M+CH3COO]-, [M+Hac-H]- or [M+2NH4]2+."""
    name = str(text).strip()
    match = _ADDUCT.match(name)
    if not match:
        raise ValueError(f"'{text}' is not an adduct name of the form [M+X]+ or [M-H]-.")
    composition: Counter = Counter()
    for sign, count, group in _ADDUCT_PART.findall(match.group(2)):
        try:
            part = formula_composition(ADDUCT_GROUPS.get(group, group))
        except ValueError:
            raise ValueError(f"'{text}' adds or removes '{group}', which is not a formula or a known group.") from None
        factor = (int(count) if count else 1) * (1 if sign == "+" else -1)
        for element, number in part.items():
            composition[element] += number * factor
    charge = int(match.group(3)) if match.group(3) else 1
    return AdductIon(
        name=name,
        multimer=int(match.group(1)) if match.group(1) else 1,
        charge=charge,
        sign=1 if match.group(4) == "+" else -1,
        composition=tuple(sorted((element, count) for element, count in composition.items() if count)),
    )


def adduct_mz(neutral_mass: float, adduct: AdductIon | str) -> float:
    ion = parse_adduct(adduct) if isinstance(adduct, str) else adduct
    return (ion.multimer * neutral_mass + ion.mass_shift - ion.sign * ion.charge * ELECTRON_MASS) / ion.charge


def same_adduct(left: str, right: str) -> bool:
    """Whether two adduct names describe the same ion; an unreadable name equals only itself."""
    try:
        return parse_adduct(left).identity == parse_adduct(right).identity
    except ValueError:
        return left.strip() == right.strip()


def ms1_tolerance_for(mz: float, tolerance: float = MS1_TOLERANCE) -> float:
    """MS-DIAL's Text-DB m/z window: tolerance Da up to m/z 500, the same ppm above (about 20 ppm)."""
    return tolerance if mz <= MS1_TOLERANCE_PIVOT_MZ else mz * tolerance / MS1_TOLERANCE_PIVOT_MZ


# ---------------------------------------------------------------------------------------------
# Salt
# ---------------------------------------------------------------------------------------------

SALTS = ("CH3COONH4", "HCOONH4", "NH4HCO3")
# Matched as substrings of the analyst's answer, lowercased. The Console accepts only the three
# enum strings and silently keeps CH3COONH4 for anything else, so every other spelling is mapped
# here and echoed back.
SALT_SYNONYMS = {
    "CH3COONH4": ("ch3coonh4", "ammonium acetate", "nh4oac", "nh4ac", "acetate", "acetic acid",
                  "酢酸アンモニウム", "酢酸"),
    "HCOONH4": ("hcoonh4", "ammonium formate", "nh4fa", "nh4hcoo", "formate", "formic acid",
                "ギ酸アンモニウム", "ギ酸", "蟻酸"),
    "NH4HCO3": ("nh4hco3", "ammonium bicarbonate", "ammonium hydrogen carbonate", "bicarbonate",
                "hydrogen carbonate", "重炭酸アンモニウム", "炭酸水素アンモニウム"),
}
# The negative-mode LBM records each salt removes before annotation (MspFileParcer.queryCheck).
SALT_DROPS = {
    "CH3COONH4": (FORMATE, BICARBONATE),
    "HCOONH4": (ACETATE, BICARBONATE),
    "NH4HCO3": (FORMATE, ACETATE),
}


def normalize_salt(answer: Any) -> dict[str, Any]:
    """Map an answer such as '酢酸アンモニウム' or 'ammonium formate' to the Console's enum string."""
    text = re.sub(r"\s+", " ", str(answer or "").strip())
    if text in SALTS:
        return {"salt": text, "answer": text, "matched": text, "problem": ""}
    lowered = text.lower()
    found = {
        salt: next(term for term in terms if term in lowered)
        for salt, terms in SALT_SYNONYMS.items()
        if any(term in lowered for term in terms)
    }
    if len(found) == 1:
        salt, term = next(iter(found.items()))
        return {"salt": salt, "answer": text, "matched": term, "problem": ""}
    if found:
        return {
            "salt": None, "answer": text, "matched": "",
            "problem": f"'{text}' names more than one anion ({', '.join(sorted(found))}); "
                       "the Console takes exactly one salt.",
        }
    return {
        "salt": None, "answer": text, "matched": "",
        "problem": f"'{text}' is not a salt this agent knows; the Console accepts {', '.join(SALTS)}.",
    }


def salt_drops(salt: str, adduct: str) -> bool:
    """Whether MS-DIAL removes negative-mode LBM records of this adduct under this salt."""
    try:
        ion = parse_adduct(adduct)
    except ValueError:
        return False
    if ion.sign > 0 or ion.multimer != 1 or ion.charge != 1:
        return False
    return frozenset(ion.composition) in SALT_DROPS.get(salt, ())


def salt_evidence(
    salt: str | None, negative_standards: Iterable["InternalStandard"], quant_info: dict[str, Any] | None
) -> dict[str, Any]:
    """What the negative-mode standards and sheet 3 say about the salt, and what the salt would drop.

    The salt only filters LBM records, and only in negative mode. A selected sheet-3 row it
    drops can never be annotated, so its class is silently absent: a blocker. A standard whose
    adduct it drops can still be found by its Text-DB row in the same run: a warning.
    """
    standard_adducts: dict[str, list[str]] = {}
    for record in negative_standards:
        standard_adducts.setdefault(record.adduct, []).append(record.name)
    selected = Counter(
        row["adduct"] for row in (quant_info or {}).get("rows", [])
        if row.get("selected") and row.get("ion_mode") == NEGATIVE
    )
    evidence = list(standard_adducts) + list(selected)
    compatible = [
        candidate for candidate in SALTS
        if not any(salt_drops(candidate, adduct) for adduct in evidence)
    ]
    blockers: list[str] = []
    warnings: list[str] = []
    standard_conflicts: list[str] = []
    selected_conflicts: list[dict[str, str]] = []
    if salt:
        for adduct, names in standard_adducts.items():
            if salt_drops(salt, adduct):
                standard_conflicts.extend(names)
                warnings.append(
                    f"{salt} removes negative-mode LBM records of {adduct}, so the LBM library cannot "
                    f"annotate {', '.join(names)}; only the internal-standard text library can."
                )
        for row in (quant_info or {}).get("rows", []):
            if row.get("selected") and row.get("ion_mode") == NEGATIVE and salt_drops(salt, row["adduct"]):
                selected_conflicts.append({"class": row["class"], "adduct": row["adduct"]})
        if selected_conflicts:
            listed = ", ".join(f"{item['class']} {item['adduct']}" for item in selected_conflicts)
            blockers.append(
                f"{salt} removes the negative-mode LBM records sheet 3 selects for "
                f"{len(selected_conflicts)} class(es) ({listed}); those classes could never be annotated. "
                "Check the salt, or the selection in sheet 3."
            )
    return {
        "salt": salt,
        "negative_standard_adducts": standard_adducts,
        "selected_negative_adducts": dict(sorted(selected.items())),
        "salts_consistent_with_evidence": compatible,
        "standard_conflicts": standard_conflicts,
        "selected_conflicts": selected_conflicts,
        "blockers": blockers,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------------------------
# quant_utility_table.xlsx
# ---------------------------------------------------------------------------------------------

# Sheet 1's amount column, read from its header: the unit says what the sample amount in row 4
# divides by, and becomes normalize's -u.
_AMOUNT_HEADER = re.compile(r"(?P<prefix>[npf])mol/(?P<basis>mgtissue|10\^?6cells|10e6cells|ulplasma)")
_AMOUNT_UNITS = {"mgtissue": "{p}mol_per_mg_tissue", "106cells": "{p}mol_per_10E6_cells",
                 "10e6cells": "{p}mol_per_10E6_cells", "ulplasma": "{p}mol_per_microL_plasma"}


def amount_unit_for_header(header: str) -> str | None:
    """The normalize unit a sheet-1 amount header names: 'int (pmol/ul plasma)' -> pmol_per_microL_plasma."""
    text = str(header or "").lower().replace("µ", "u").replace("μ", "u").replace(" ", "")
    match = _AMOUNT_HEADER.search(text)
    if not match:
        return None
    basis = match.group("basis").replace("^", "")
    return _AMOUNT_UNITS[basis].format(p=match.group("prefix"))


def parse_is_preparation(sheet: XlsxSheet, amount_column: str) -> dict[str, Any]:
    """Sheet 1: each standard, its class, and its amount per sample unit in the chosen column.

    The header is the first row with Name in B and Class name in D; the standards run from the
    next row to the first empty B. What follows that gap is a worked example that repeats a
    standard with other numbers, so it is never read as a standard.
    """
    blockers: list[str] = []
    warnings: list[str] = []
    result: dict[str, Any] = {
        "sheet": sheet.name, "amount_column": "", "amount_header": "", "unit": None,
        "header_row": None, "standards": [], "sample_parameter": None, "notes": [],
        "blockers": blockers, "warnings": warnings,
    }
    column = str(amount_column or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", column):
        blockers.append(f"'{amount_column}' is not a column letter; name the amount column (F, G or H).")
        return result
    result["amount_column"] = column
    try:
        header_row = next(
            (
                row for row in range(1, sheet.max_row + 1)
                if _header_key(sheet.text(f"B{row}")) == "name"
                and _header_key(sheet.text(f"D{row}")) == "classname"
            ),
            None,
        )
        if header_row is None:
            blockers.append(f"Sheet '{sheet.name}' has no header row with 'Name' in B and 'Class name' in D.")
            return result
        result["header_row"] = header_row
        header = sheet.text(f"{column}{header_row}")
        result["amount_header"] = header
        unit = amount_unit_for_header(header)
        result["unit"] = unit
        if unit is None:
            blockers.append(
                f"'{sheet.name}'!{column}{header_row} is headed '{header}', which names no amount per "
                "mg tissue, per 10^6 cells or per uL plasma; choose another column."
            )
            return result
        if header_row > 2:
            label = sheet.text(f"{column}{header_row - 2}")
            value = sheet.number(f"{column}{header_row - 1}")
            result["sample_parameter"] = {
                "label": label, "value": value,
                "label_ref": f"{column}{header_row - 2}", "value_ref": f"{column}{header_row - 1}",
            }
        seen: dict[str, int] = {}
        row = header_row + 1
        while row <= sheet.max_row and sheet.text(f"B{row}").strip():
            name = sheet.text(f"B{row}").strip()
            class_name = sheet.text(f"D{row}").strip()
            cell = sheet.cell(f"{column}{row}")
            amount = sheet.number(f"{column}{row}")
            if name.casefold() in seen:
                blockers.append(f"'{sheet.name}' lists {name} twice (rows {seen[name.casefold()]} and {row}).")
            seen[name.casefold()] = row
            if cell is not None and cell.kind == "error":
                blockers.append(f"'{sheet.name}'!{column}{row} for {name} is an error value ({cell.raw}).")
            elif amount is None or amount <= 0:
                blockers.append(f"'{sheet.name}'!{column}{row} for {name} is not a positive amount.")
            result["standards"].append({
                "name": name, "class_name": class_name, "amount": amount, "row": row,
                "formula": cell.formula if cell is not None else None,
            })
            row += 1
        if not result["standards"]:
            blockers.append(f"'{sheet.name}' lists no standard below its header row {header_row}.")
        first = result["standards"][0] if result["standards"] else None
        parameter = result["sample_parameter"]
        if first and parameter and first.get("formula"):
            # An amount per sample unit divides by the sample amount. Sheet 1's cells-column formula
            # multiplies, which is pmol per 10^6 cells only if row 4 holds the reciprocal.
            pattern = rf"\$?{column}\$?{header_row - 1}(?![0-9])"
            text = first["formula"].replace(" ", "")
            if re.search(rf"\*{pattern}|{pattern}\*", text):
                warnings.append(
                    f"'{sheet.name}'!{column}{first['row']} is {first['formula']}: it multiplies by the "
                    f"sample amount in {column}{header_row - 1} where an amount per sample unit divides."
                )
        # Free text below the standards, such as a note of the plasma volume actually extracted:
        # read back to the person beside the row-4 value, never interpreted.
        for (row_index, column_number), cell in sorted(sheet.cells.items()):
            if row_index > row and cell.kind == "string" and str(cell.value).strip():
                result["notes"].append({"ref": cell.ref, "text": str(cell.value).strip()})
        result["notes"] = result["notes"][:30]
    except LabInputError as error:
        blockers.append(str(error))
    return result


def parse_lookup_table(
    sheet: XlsxSheet, preparation: dict[str, Any], vocabulary: LipidClassVocabulary | None = None
) -> dict[str, Any]:
    """Sheet 2: which standard quantifies which lipid class (A standard, C class; no header).

    Every row down to the sheet's last is read; blank rows are skipped, never taken as the end.
    Column B is not read as the amount: it is a VLOOKUP into a local copy of one sheet-1 column,
    so it is recomputed from the column the person chose and a cached value that differs is
    reported. Column D (old peak IDs) is ignored. Classes use the VS2 spelling normalize looks up.
    """
    vocabulary = vocabulary or load_lipid_class_vocabulary()
    blockers: list[str] = []
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    result: dict[str, Any] = {"sheet": sheet.name, "rows": rows, "blockers": blockers, "warnings": warnings}
    amounts = {item["name"]: item["amount"] for item in preparation.get("standards", [])}
    try:
        start = 1
        first = sheet.text("A1").strip()
        if first and first not in amounts:
            if re.search(r"standard|name|compound", first, re.IGNORECASE):
                start = 2
        row = start
        while row <= sheet.max_row:
            standard = sheet.text(f"A{row}").strip()
            target = sheet.text(f"C{row}").strip()
            if not standard and not target:
                # A blank separator row does not end the table: a class below it read as missing
                # would fall to 'Any others' without a word.
                row += 1
                continue
            if not standard:
                blockers.append(f"'{sheet.name}'!C{row} names {target} but A{row} names no standard.")
                row += 1
                continue
            if not target:
                blockers.append(f"'{sheet.name}'!A{row} names {standard} but C{row} names no class.")
                row += 1
                continue
            cached_cell = sheet.cell(f"B{row}")
            cached = None
            if cached_cell is not None and cached_cell.kind == "number":
                cached = float(cached_cell.value)
            elif cached_cell is not None and cached_cell.kind == "error":
                warnings.append(f"'{sheet.name}'!B{row} is an error value ({cached_cell.raw}); it is recomputed.")
            amount = amounts.get(standard)
            if standard not in amounts:
                blockers.append(f"'{sheet.name}'!A{row} names {standard}, which sheet 1 does not list.")
            if cached is not None and amount is not None and not math.isclose(cached, amount, rel_tol=1e-9):
                warnings.append(
                    f"'{sheet.name}'!B{row} caches {cached:g} for {standard}; sheet 1's chosen column "
                    f"gives {amount:g}, which is what is used."
                )
            rows.append({"standard": standard, "target_class": target, "amount": amount, "row": row,
                         "cached_amount": cached})
            row += 1
    except LabInputError as error:
        blockers.append(str(error))
        return result
    seen: dict[str, int] = {}
    others = [row for row in rows if row["target_class"] == ANY_OTHERS]
    for row in rows:
        target = row["target_class"]
        if target in seen:
            # normalize takes the first row for a class and ignores the rest without a word.
            blockers.append(
                f"'{sheet.name}' names {target} on rows {seen[target]} and {row['row']}; "
                "normalize would use the first and ignore the second."
            )
        seen.setdefault(target, row["row"])
        if target == ANY_OTHERS:
            continue
        if target.casefold() == ANY_OTHERS.casefold():
            blockers.append(
                f"'{sheet.name}'!C{row['row']} reads '{target}'; normalize recognises only '{ANY_OTHERS}' exactly."
            )
        elif not vocabulary.is_vs2(target):
            canonical = vocabulary.canonical_vs2(target)
            hint = f" MS-DIAL spells it '{canonical}' here." if canonical != UNDEFINED else ""
            blockers.append(
                f"'{sheet.name}'!C{row['row']} names class '{target}', which normalize never assigns, so "
                f"its rows would fall to '{ANY_OTHERS}'.{hint}"
            )
    if len(others) != 1:
        blockers.append(
            f"'{sheet.name}' must name '{ANY_OTHERS}' exactly once; it does {len(others)} time(s)."
        )
    used = {row["standard"] for row in rows}
    result["unused_standards"] = [name for name in amounts if name not in used]
    return result


def parse_quant_info(
    sheet: XlsxSheet, vocabulary: LipidClassVocabulary | None = None, source: str = ""
) -> dict[str, Any]:
    """Sheet 3: which class and adduct each polarity's lipidome keeps.

    The result also has the shape lipidome_merge reads as a rule table (selected, rule_count,
    selected_by_ion_mode, selected_classes, path). Classes are LbmClass members, the spelling an
    export's Ontology column carries.
    """
    vocabulary = vocabulary or load_lipid_class_vocabulary()
    blockers: list[str] = []
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "path": source, "sheet": sheet.name, "rows": rows, "selected": set(), "rule_count": 0,
        "selected_by_ion_mode": {}, "selected_classes": 0, "multi_adduct_classes": [],
        "undefined_classes": [], "blockers": blockers, "warnings": warnings,
    }
    try:
        header_row = 1
        headers = {}
        for column_number in range(0, 26):
            key = _header_key(sheet.text(f"{column_letters(column_number)}{header_row}"))
            if key:
                headers[key] = column_letters(column_number)
        columns = {
            "class": headers.get("class") or headers.get("ontology"),
            "adduct": headers.get("adduct") or headers.get("adducttype"),
            "ion_mode": headers.get("ionmode") or headers.get("polarity"),
            "selected": headers.get("isselected") or headers.get("selected"),
        }
        missing = [name for name, letter in columns.items() if not letter]
        if missing:
            blockers.append(
                f"Sheet '{sheet.name}' row 1 needs Class, Adduct, Ion mode and IsSelected; "
                f"missing: {', '.join(missing)}."
            )
            return result
        for row in range(header_row + 1, sheet.max_row + 1):
            values = {name: sheet.text(f"{letter}{row}").strip() for name, letter in columns.items()}
            if not any(values.values()):
                continue
            selected = _flag(sheet.value(f"{columns['selected']}{row}"))
            mode_text = values["ion_mode"].lower()
            mode = (POSITIVE if mode_text in {"positive", "pos", "+"}
                    else NEGATIVE if mode_text in {"negative", "neg", "-"} else "")
            item = {"row": row, "class": values["class"], "adduct": values["adduct"], "ion_mode": mode,
                    "selected": bool(selected)}
            rows.append(item)
            problems: list[str] = []
            if selected is None:
                problems.append(f"IsSelected '{values['selected']}' is not TRUE or FALSE")
            if not mode:
                problems.append(f"ion mode '{values['ion_mode']}' is not Positive or Negative")
            if not vocabulary.is_lbm_class(values["class"]):
                problems.append(f"class '{values['class']}' is not an LbmClass name, so no export row can carry it")
            try:
                ion = parse_adduct(values["adduct"])
                if mode and ion.polarity != mode:
                    problems.append(f"{values['adduct']} is a {ion.polarity.lower()} ion in a {mode} row")
            except ValueError as error:
                problems.append(str(error))
            if problems:
                message = (f"'{sheet.name}' row {row} ({values['class']} {values['adduct']}): "
                           + "; ".join(problems) + ".")
                # An unselected row never reaches the lipidome, so its problem is only reported.
                (blockers if selected or selected is None else warnings).append(message)
                continue
            if vocabulary.vs2_of_enum(values["class"]) == UNDEFINED and selected:
                result["undefined_classes"].append(
                    {"class": values["class"], "adduct": values["adduct"], "ion_mode": mode})
    except LabInputError as error:
        blockers.append(str(error))
        return result
    counted: Counter = Counter((item["class"], item["adduct"], item["ion_mode"]) for item in rows)
    for (klass, adduct, mode), count in counted.items():
        if count > 1:
            warnings.append(f"'{sheet.name}' lists {klass} {adduct} ({mode}) {count} times.")
    chosen = [item for item in rows if item["selected"]]
    result["rule_count"] = len(rows)
    result["selected"] = {(item["class"], item["adduct"]) for item in chosen}
    if not chosen:
        blockers.append(f"Sheet '{sheet.name}' selects no class and adduct; the lipidome would be empty.")
    by_mode = Counter(item["ion_mode"] or "unspecified" for item in chosen)
    result["selected_by_ion_mode"] = dict(sorted(by_mode.items()))
    result["selected_classes"] = len({item["class"] for item in chosen})
    adducts_by_class: dict[str, list[str]] = {}
    for item in chosen:
        adducts_by_class.setdefault(item["class"], []).append(f"{item['adduct']} ({item['ion_mode']})")
    # Two selected adducts of one class keep the same lipid twice; reported, never resolved here.
    result["multi_adduct_classes"] = [
        {"class": klass, "adducts": adducts} for klass, adducts in sorted(adducts_by_class.items()) if len(adducts) > 1
    ]
    for item in result["undefined_classes"]:
        warnings.append(
            f"{item['class']} is selected ({item['adduct']}, {item['ion_mode']}) but MS-DIAL has no class "
            f"definition for it, so normalize quantifies it against '{ANY_OTHERS}'."
        )
    return result


def read_quant_workbook(
    path: str | Path, amount_column: str, vocabulary: LipidClassVocabulary | None = None
) -> dict[str, Any]:
    """quant_utility_table.xlsx: sheets 1, 2 and 3 by position, as the analyst numbers them."""
    vocabulary = vocabulary or load_lipid_class_vocabulary()
    workbook = read_xlsx(path)
    blockers: list[str] = []
    warnings = list(workbook.warnings)
    for lock in excel_lock_files(path):
        warnings.append(
            f"{Path(lock).name} shows the workbook is open in Excel; only what was last saved is read."
        )
    if len(workbook.sheets) < 3:
        blockers.append(f"{Path(path).name} has {len(workbook.sheets)} sheet(s); sheets 1, 2 and 3 are needed.")
        return {"path": workbook.path, "sha256": workbook.sha256, "sheet_names": workbook.sheet_names,
                "blockers": blockers, "warnings": warnings}
    preparation = parse_is_preparation(workbook.sheets[0], amount_column)
    lookup = parse_lookup_table(workbook.sheets[1], preparation, vocabulary)
    quant_info = parse_quant_info(workbook.sheets[2], vocabulary, workbook.path)
    for part in (preparation, lookup, quant_info):
        blockers.extend(part["blockers"])
        warnings.extend(part["warnings"])
    return {
        "path": workbook.path, "sha256": workbook.sha256, "sheet_names": workbook.sheet_names,
        "preparation": preparation, "lookup": lookup, "quant_info": quant_info,
        "unit": preparation.get("unit"), "blockers": blockers, "warnings": warnings,
    }


# ---------------------------------------------------------------------------------------------
# Internal-standard text libraries
# ---------------------------------------------------------------------------------------------

# Header names, compared as lowercase letters and digits. The Console itself reads the file by
# position (TextLibraryParser), so a file in any other order is rewritten before it is used.
IS_TEXT_COLUMNS = {
    "name": ("name", "metabolitename", "compoundname"),
    "mz": ("mz", "precursormz", "accuratemass"),
    "rt": ("rt", "rtmin", "retentiontime", "retentiontimemin"),
    "adduct": ("adduct", "adducttype", "adduction", "precursortype"),
    "inchikey": ("inchikey",),
    "formula": ("formula",),
    "smiles": ("smiles",),
    "ontology": ("ontology", "class", "lipidclass", "compoundclass"),
    "ccs": ("ccs",),
}
IS_TEXT_REQUIRED = ("name", "mz", "rt", "adduct", "ontology")
CANONICAL_TEXT_COLUMNS = ("Name", "MZ", "RT", "Adduct", "InChIKey", "Formula", "SMILES", "Ontology", "CCS")


@dataclass
class InternalStandard:
    name: str
    mz: float
    rt: float
    adduct: str
    ontology: str
    inchikey: str = ""
    formula: str = ""
    smiles: str = ""
    ccs: float | None = None
    mz_text: str = ""
    rt_text: str = ""
    ccs_text: str = ""
    line: int = 0

    @property
    def polarity(self) -> str:
        return parse_adduct(self.adduct).polarity

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_internal_standard_text(path: str | Path, polarity: str | None = None) -> dict[str, Any]:
    """Read an MS-DIAL text library of internal standards, by its header, in any column order.

    Name, MZ, RT, Adduct and Ontology are required; MZ is the adduct's m/z, because MS-DIAL
    matches the text library by m/z and the adduct column only labels the result.
    """
    source = Path(path)
    blockers: list[str] = []
    warnings: list[str] = []
    result: dict[str, Any] = {"path": str(source), "sha256": "", "encoding": "", "records": [],
                              "header": [], "polarity": polarity, "blockers": blockers, "warnings": warnings}
    try:
        data = source.read_bytes()
    except OSError as error:
        blockers.append(f"{source} cannot be read: {error}")
        return result
    result["sha256"] = hashlib.sha256(data).hexdigest()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("ascii")
        result["encoding"] = "ascii"
    except UnicodeDecodeError:
        try:
            text = data.decode("utf-8")
            result["encoding"] = "utf-8"
        except UnicodeDecodeError:
            blockers.append(f"{source.name} is neither ASCII nor UTF-8.")
            return result
    lines = text.splitlines()
    header_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if header_index is None:
        blockers.append(f"{source.name} is empty.")
        return result
    header = [cell.strip().strip('"') for cell in lines[header_index].split("\t")]
    result["header"] = header
    columns: dict[str, int] = {}
    for index, cell in enumerate(header):
        key = _header_key(cell)
        for field_name, aliases in IS_TEXT_COLUMNS.items():
            if key in aliases and field_name not in columns:
                columns[field_name] = index
    missing = [name for name in IS_TEXT_REQUIRED if name not in columns]
    if missing:
        blockers.append(
            f"{source.name} has no {', '.join(missing)} column(s); its header is: {' | '.join(header)}."
        )
        return result
    records: list[InternalStandard] = []
    # By name within a polarity: one file holding both polarities lists most standards twice,
    # once per adduct, and that is not a repeat.
    seen: dict[tuple[str, str | None], int] = {}
    for offset, line in enumerate(lines[header_index + 1:], start=header_index + 2):
        if not line.strip():
            continue
        cells = [cell.strip().strip('"') for cell in line.split("\t")]

        def cell(name: str) -> str:
            index = columns.get(name, -1)
            return cells[index] if 0 <= index < len(cells) else ""

        problems: list[str] = []
        if any(ord(char) > 127 for char in line):
            # MS-DIAL reads the text library as ASCII; a non-ASCII name would never match itself.
            problems.append("holds non-ASCII characters, which MS-DIAL reads as '?'")
        name = cell("name")
        mz = parse_number(cell("mz"))
        rt = parse_number(cell("rt"))
        adduct = cell("adduct")
        ontology = cell("ontology")
        ccs_text = cell("ccs")
        ccs = parse_number(ccs_text) if ccs_text else None
        if not name:
            problems.append("has no name")
        if mz is None or mz <= 0:
            problems.append(f"MZ '{cell('mz')}' is not a positive number written with a decimal point")
        if rt is None or rt < 0:
            # The Console drops a row whose RT does not parse; an IS with no RT cannot be searched.
            problems.append(f"RT '{cell('rt')}' is not a retention time in minutes")
        if not ontology:
            problems.append("has no Ontology")
        if ccs_text and (ccs is None or ccs <= 0):
            problems.append(f"CCS '{ccs_text}' is not a number")
        ion = None
        try:
            ion = parse_adduct(adduct)
            if polarity and ion.polarity != polarity:
                problems.append(f"{adduct} is a {ion.polarity.lower()} ion in the {polarity.lower()} library")
        except ValueError as error:
            problems.append(str(error))
        key = (name.casefold(), polarity or (ion.polarity if ion is not None else None))
        if name and key in seen:
            problems.append(f"repeats the name on line {seen[key]}")
        if problems:
            blockers.append(f"{source.name} line {offset} ({name or 'unnamed'}): " + "; ".join(problems) + ".")
            continue
        seen[key] = offset
        record = InternalStandard(
            name=name, mz=mz, rt=rt, adduct=adduct, ontology=ontology, inchikey=cell("inchikey"),
            formula=cell("formula"), smiles=cell("smiles"), ccs=ccs, mz_text=cell("mz"),
            rt_text=cell("rt"), ccs_text=ccs_text, line=offset,
        )
        if record.formula and ion is not None:
            try:
                expected = adduct_mz(formula_mass(record.formula), ion)
                if abs(expected - mz) > FORMULA_MZ_WARNING_DA:
                    warnings.append(
                        f"{source.name} line {offset} ({name}): MZ {mz} differs from {record.formula} {adduct} "
                        f"({expected:.4f}) by {(mz - expected) * 1000:.1f} mDa."
                    )
            except ValueError as error:
                warnings.append(f"{source.name} line {offset} ({name}): the formula was not checked: {error}")
        records.append(record)
    if not records and not blockers:
        blockers.append(f"{source.name} lists no internal standard.")
    if result["encoding"] == "utf-8":
        warnings.append(f"{source.name} is UTF-8; the library MS-DIAL reads is rewritten as ASCII.")
    result["records"] = records
    return result


def split_by_polarity(records: Iterable[InternalStandard]) -> dict[str, list[InternalStandard]]:
    """One library holding both polarities, split by each adduct's charge sign."""
    split: dict[str, list[InternalStandard]] = {POSITIVE: [], NEGATIVE: []}
    for record in records:
        split[record.polarity].append(record)
    return split


def _invariant(text: str, value: float) -> str:
    return text if parse_number(text) == value else repr(float(value))


def canonical_text_db_lines(records: Iterable[InternalStandard]) -> list[str]:
    """The text library in the Console's positional layout (TextLibraryParser).

    The Console reads columns by position and drops a row whose RT or CCS field is present but
    does not parse, an empty CCS cell included. So RT is always written and CCS only for a
    standard that has one: a row without it ends after Ontology rather than with an empty field.
    """
    lines = ["\t".join(CANONICAL_TEXT_COLUMNS)]
    for record in records:
        cells = [
            record.name, _invariant(record.mz_text, record.mz), _invariant(record.rt_text, record.rt),
            record.adduct, record.inchikey, record.formula, record.smiles, record.ontology,
        ]
        if record.ccs is not None:
            cells.append(_invariant(record.ccs_text, record.ccs))
        for value in cells:
            if "\t" in value or "\n" in value or any(ord(char) > 127 for char in value):
                raise LabInputError(f"{record.name}: '{value}' cannot be written into an ASCII text library.")
        lines.append("\t".join(cells))
    return lines


def write_canonical_text_db(records: Iterable[InternalStandard], path: str | Path) -> dict[str, Any]:
    lines = canonical_text_db_lines(records)
    data = ("\r\n".join(lines) + "\r\n").encode("ascii")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": str(target), "sha256": hashlib.sha256(data).hexdigest(), "records": len(lines) - 1}


def internal_standards_for_qa(
    records: Iterable[InternalStandard], rt_tolerance: float, mz_tolerance: float = MS1_TOLERANCE
) -> list[dict[str, Any]]:
    """The records in the shape QA's internal_standards answer takes."""
    return [
        {"name": record.name, "mz": record.mz, "rt": record.rt, "adduct": record.adduct,
         "mz_tolerance": mz_tolerance, "rt_tolerance": rt_tolerance}
        for record in records
    ]


def cross_check_standards(
    preparation: dict[str, Any], lookup: dict[str, Any],
    positive: Iterable[InternalStandard], negative: Iterable[InternalStandard],
) -> dict[str, Any]:
    """Sheet 1 against the two libraries: every prepared standard must be searchable somewhere."""
    blockers: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    libraries = {POSITIVE: list(positive), NEGATIVE: list(negative)}
    by_name = {pol: {record.name: record for record in records} for pol, records in libraries.items()}
    used = {row["standard"] for row in lookup.get("rows", [])}
    for standard in preparation.get("standards", []):
        name = standard["name"]
        present = [pol for pol in POLARITIES if name in by_name[pol]]
        if not present:
            blockers.append(f"{name} is prepared in sheet 1 but neither internal-standard library lists it.")
        for pol in present:
            record = by_name[pol][name]
            if standard["class_name"] and record.ontology != standard["class_name"]:
                warnings.append(
                    f"{name}: the {pol.lower()} library gives Ontology '{record.ontology}' and sheet 1 "
                    f"gives class '{standard['class_name']}'."
                )
        if name not in used:
            notes.append(f"{name} is prepared but sheet 2 assigns it no class; it is searched and not used.")
    prepared = {standard["name"] for standard in preparation.get("standards", [])}
    for pol in POLARITIES:
        for record in libraries[pol]:
            if record.name not in prepared:
                warnings.append(f"{record.name} is in the {pol.lower()} library but sheet 1 does not prepare it.")
    return {"blockers": blockers, "warnings": warnings, "notes": notes}


def expected_resolution_matrix(
    lookup: dict[str, Any], quant_info: dict[str, Any],
    positive: Iterable[InternalStandard], negative: Iterable[InternalStandard],
    vocabulary: LipidClassVocabulary | None = None,
) -> dict[str, Any]:
    """What each selected class will be quantified by, before anything runs.

    A selected class whose standard (or Any others) is absent from its polarity's library can
    only come out NOT QUANTIFIED; that is known from the inputs and is said before hours of
    processing rather than discovered in the export.
    """
    vocabulary = vocabulary or load_lipid_class_vocabulary()
    names = {POSITIVE: {record.name for record in positive}, NEGATIVE: {record.name for record in negative}}
    by_class = {}
    for row in lookup.get("rows", []):
        by_class.setdefault(row["target_class"], row["standard"])
    fallback = by_class.get(ANY_OTHERS)
    standards = sorted({row["standard"] for row in lookup.get("rows", [])})
    rows: list[dict[str, Any]] = []
    for item in quant_info.get("rows", []):
        if not item.get("selected") or item.get("ion_mode") not in POLARITIES:
            continue
        canonical = vocabulary.vs2_of_enum(item["class"])
        if canonical != UNDEFINED and canonical in by_class:
            standard, via = by_class[canonical], "own"
        else:
            standard, via = fallback, "undefined_class" if canonical == UNDEFINED else ANY_OTHERS
        rows.append({
            "class": item["class"], "adduct": item["adduct"], "ion_mode": item["ion_mode"],
            "vs2_class": canonical, "standard": standard, "via": via,
            "standard_in_library": bool(standard) and standard in names[item["ion_mode"]],
        })
    matrix = {}
    for pol in POLARITIES:
        selected = [row for row in rows if row["ion_mode"] == pol]
        matrix[pol] = {
            "unresolved_expected": [name for name in standards if name not in names[pol]],
            "not_quantified": sorted({row["class"] for row in selected if not row["standard_in_library"]}),
            "any_others_classes": sorted({row["class"] for row in selected if row["via"] != "own"}),
            "undefined_classes": sorted({row["class"] for row in selected if row["via"] == "undefined_class"}),
        }
    return {"rows": rows, "by_polarity": matrix, "any_others_standard": fallback}


# ---------------------------------------------------------------------------------------------
# Run outputs: the alignment export and the candidate table
# ---------------------------------------------------------------------------------------------

EXPORT_COLUMNS = {
    "alignment_id": "Alignment ID", "rt": "Average Rt(min)", "mz": "Average Mz",
    "name": "Metabolite name", "adduct": "Adduct type", "fill": "Fill %", "formula": "Formula",
    "ontology": "Ontology", "comment": "Comment", "isotope_weight": "Isotope tracking weight number",
}
EXPORT_REQUIRED = ("alignment_id", "rt", "mz", "name", "adduct", "comment")
CANDIDATE_REQUIRED = (
    "alignment_master_id", "is_representative", "annotator_id", "source", "name", "ontology",
    "is_reference_matched", "is_annotation_suggested",
)
_TRUE = {"true", "1", "yes"}


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def annotation_method(comment: str) -> str:
    """The annotator an export's Comment names: 'Normalized unit: X; Annotation method: Y' -> Y."""
    for part in str(comment or "").split("; "):
        if part.startswith("Annotation method: "):
            return part[len("Annotation method: "):].strip()
    return ""


def normalized_unit(comment: str) -> str:
    for part in str(comment or "").split("; "):
        if part.startswith("Normalized unit: "):
            return part[len("Normalized unit: "):].strip()
    return ""


def read_alignment_export(path: str | Path) -> dict[str, Any]:
    """An MS-DIAL alignment export (Height or NormalizedHeight) as spots with their sample cells."""
    try:
        export = describe_export(path)
    except MergeRefused as error:
        raise LabInputError(str(error)) from None
    names = export["header"][4]
    columns = {key: (names.index(title) if title in names else -1) for key, title in EXPORT_COLUMNS.items()}
    missing = [EXPORT_COLUMNS[key] for key in EXPORT_REQUIRED if columns[key] < 0]
    if missing:
        raise LabInputError(f"{path} has no {', '.join(missing)} column(s).")
    start = export["first_sample_column"]
    count = len(export["sample_names"])
    spots = []
    for row in export["rows"]:
        if not any(cell.strip() for cell in row):
            continue

        def cell(key: str) -> str:
            index = columns[key]
            return row[index].strip() if 0 <= index < len(row) else ""

        cells = [row[start + offset].strip() if start + offset < len(row) else "" for offset in range(count)]
        spots.append({
            "id": cell("alignment_id"),
            "rt": parse_number(cell("rt")),
            "mz": parse_number(cell("mz")),
            "name": cell("name"),
            "adduct": cell("adduct"),
            "fill": parse_number(cell("fill")) or 0.0,
            "formula": cell("formula"),
            "ontology": cell("ontology"),
            "comment": cell("comment"),
            "method": annotation_method(cell("comment")),
            "isotope_weight": int(parse_number(cell("isotope_weight")) or 0),
            "cells": cells,
        })
    return {
        "path": str(Path(path).resolve()),
        "sha256": file_sha256(path),
        "sample_names": export["sample_names"],
        "sample_classes": export["sample_classes"],
        "file_types": export["file_types"],
        "spots": spots,
    }


def read_candidate_table(path: str | Path) -> dict[str, Any]:
    """The .mdcandidate.tsv sidecar: every annotation candidate MS-DIAL kept for each spot."""
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fields = reader.fieldnames or []
        missing = [name for name in CANDIDATE_REQUIRED if name not in fields]
        if missing:
            raise LabInputError(f"{path} is not an MS-DIAL candidate table; it has no {', '.join(missing)}.")
        by_spot: dict[str, list[dict[str, Any]]] = {}
        for row in reader:
            item = dict(row)
            for key in ("is_representative", "is_reference_matched", "is_annotation_suggested"):
                item[key] = str(item.get(key, "")).strip().lower() in _TRUE
            by_spot.setdefault(str(item["alignment_master_id"]).strip(), []).append(item)
    return {"path": str(Path(path).resolve()), "sha256": file_sha256(path), "by_spot": by_spot}


def representative_candidate(candidates: dict[str, Any] | None, spot_id: str) -> dict[str, Any] | None:
    for row in (candidates or {}).get("by_spot", {}).get(str(spot_id), []):
        if row["is_representative"]:
            return row
    return None


# ---------------------------------------------------------------------------------------------
# Lipid name equivalence
# ---------------------------------------------------------------------------------------------

_LEGACY_OXYGEN = re.compile(r";(\d+)O(?![A-Za-z0-9])")
_LABEL = re.compile(r"\(d(\d+)\)")
_CHAIN = re.compile(r"^(?:(?P<ether>[OP])-)?(?P<carbon>\d+):(?P<bonds>\d+)(?P<oxygen>;O(?P<count>\d*))?$")
_NAME_PREFIXES = ("low score: ", "no MS2: ", "w/o MS2: ")


def _clean_name(name: str) -> str:
    text = str(name or "").strip()
    for prefix in _NAME_PREFIXES:
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
    # The sphingoid oxygen was written ;2O before MS-DIAL settled on ;O2.
    return _LEGACY_OXYGEN.sub(r";O\1", text)


def _lipid_chains(name: str) -> dict[str, Any] | None:
    """A lipid name as its class, its chains (ether, carbons, double bonds, oxygens) and its labels."""
    labels = tuple(sorted(_LABEL.findall(name)))
    text = _LABEL.sub("", name).strip()
    head, _, written = text.partition(" ")
    if not written:
        return None
    chains = []
    for part in re.split(r"[_/]", written.strip()):
        match = _CHAIN.match(part.strip())
        if not match:
            return None
        chains.append((
            match.group("ether") or "", int(match.group("carbon")), int(match.group("bonds")),
            int(match.group("count") or 1) if match.group("oxygen") else 0,
        ))
    return {"head": head.casefold(), "chains": chains, "labels": labels, "positional": "_" not in written}


def _chain_sum(chains: Iterable[tuple[str, int, int, int]]) -> tuple[Any, ...]:
    chains = list(chains)
    return (sum(chain[1] for chain in chains), sum(chain[2] for chain in chains), sum(chain[3] for chain in chains),
            tuple(sorted(chain[0] for chain in chains if chain[0])))


def _same_lipid(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Two parsed names of one lipid: equal sums, and, where both resolve chains, no chain in conflict.

    A sum composition (one chain) says nothing about the chains, so it agrees with any name of
    that sum. Two chain-resolved names must agree chain for chain; the shorter may merge some of
    the longer's chains into one (TG 15:0_33:1 for TG 15:0_18:1_15:0), never contradict them.
    """
    if left["head"] != right["head"] or left["labels"] != right["labels"]:
        return False
    if _chain_sum(left["chains"]) != _chain_sum(right["chains"]):
        return False
    short, long = sorted((left["chains"], right["chains"]), key=len)
    if len(short) == 1:
        return True
    if len(short) == len(long):
        if left["positional"] and right["positional"]:
            return left["chains"] == right["chains"]
        return sorted(short) == sorted(long)
    for index, merged in enumerate(short):
        rest = list(long)
        for chain in short[:index] + short[index + 1:]:
            if chain not in rest:
                break
            rest.remove(chain)
        else:
            if _chain_sum([merged]) == _chain_sum(rest):
                return True
    return False


def name_equivalent(spot_name: str, standard_name: str) -> bool:
    """Whether an annotation names this standard: PC 33:1(d7) is PC 15:0_18:1(d7), PC 16:0_17:1(d7) is not."""
    left = [_clean_name(part) for part in str(spot_name or "").split("|") if part.strip()]
    right = [_clean_name(part) for part in str(standard_name or "").split("|") if part.strip()]
    if not left or not right:
        return False
    if {part.casefold() for part in left} & {part.casefold() for part in right}:
        return True
    # Every part of the annotation must agree with the standard: MS-DIAL's sum-level part of
    # "TG 51:0|TG 16:0_17:0_18:0" matches TG 17:0_17:0_17:0, its chain-level part does not.
    parsed_left = [_lipid_chains(part) for part in left]
    parsed_right = [_lipid_chains(part) for part in right]
    if None in parsed_left or None in parsed_right:
        return False
    return any(all(_same_lipid(item, other) for item in parsed_left) for other in parsed_right)


def console_name_aliases(name: str) -> set[str]:
    """The names normalize lets a spot answer to: the whole name and each |-separated part, casefolded."""
    text = str(name or "").strip()
    if not text:
        return set()
    aliases = {text.casefold()}
    aliases.update(part.strip().casefold() for part in text.split("|") if part.strip())
    return aliases


# ---------------------------------------------------------------------------------------------
# Finding each internal standard in a run
# ---------------------------------------------------------------------------------------------

RESOLVED_STATUSES = ("resolved_lbm", "resolved_text", "resolved_hidden_text", "resolved_search")
NEEDS_CONFIRMATION = ("resolved_hidden_text", "resolved_search")
TIER_STATUS = {"A": "resolved_lbm", "B": "resolved_text", "C": "resolved_hidden_text", "D": "resolved_search"}


def _is_zero_height(text: str) -> bool:
    """Whether an exported Height cell means no peak: 0, empty, or MS-DIAL's replacement for zero.

    Heights are exported rounded to whole numbers; a non-whole value is the 'replace true zero
    values' substitute, which stands for a zero.
    """
    value = parse_number(text)
    if value is None or value < 0.5:
        return True
    return not float(value).is_integer()


# The files every standard must be in: the samples and QCs it quantifies. A Blank or an
# authentic-standard injection often carries none, and is not what the standard is chosen for.
STANDARD_REQUIRED_FILE_TYPES = ("sample", "qc")


def _mean_intensity(spot: dict[str, Any], file_types: list[str]) -> float:
    # Blanks and Standard files are left out: a blank that carries the standards would otherwise
    # decide which peak the samples are quantified against.
    values = [parse_number(cell) or 0.0 for cell, kind in zip(spot["cells"], file_types)
              if kind.lower() in STANDARD_REQUIRED_FILE_TYPES]
    if not values:
        values = [parse_number(cell) or 0.0 for cell in spot["cells"]]
    return sum(values) / len(values) if values else 0.0


def resolve_standards(
    export: dict[str, Any],
    candidates: dict[str, Any] | None,
    standards: Iterable[InternalStandard],
    *,
    polarity: str,
    annotation_rt_tolerance: float,
    internal_standard_rt_tolerance: float = DEFAULT_INTERNAL_STANDARD_RT_TOLERANCE,
    ms1_tolerance: float = MS1_TOLERANCE,
    standard_names: Iterable[str] = (),
    annotator_id: str | None = None,
    library_annotated: Callable[[dict[str, Any]], bool] | None = None,
    library_label: str = "LBM library",
) -> dict[str, Any]:
    """Decide which aligned spot is each internal standard of one polarity.

    Every candidate sits at the m/z of the adduct the library names, so a spot carrying the
    standard as another adduct never competes, however intense. Within that m/z:

    - A: the LBM library annotated the standard (name-equivalent, reference matched), within the
      annotation RT tolerance, or the IS RT tolerance where that is wider: an LBM match the IS
      window would admit as a text match is never passed over for one. The LBM decision comes
      first, as the analyst's procedure says.
    - B: the standard's own text-library row annotated the spot as a reference match, within the
      IS RT tolerance.
    - C: that row matched the spot, but another annotation won it or the match is only a
      suggestion below the score cutoff; needs the person's confirmation.
    - D: any spot within the IS RT tolerance; needs the person's confirmation.

    The first tier with a member decides. Within tier A a spot inside the IS RT tolerance comes
    before one only inside the annotation tolerance; then the most intense spot (mean height over
    the sample and QC files) is chosen; ties go to Fill %, then RT and m/z closeness, then the
    lower ID. A tier-A spot outside the IS RT tolerance chosen while another spot lies inside it
    needs the person's confirmation too. A chosen spot with no height in a sample or QC file is
    invalid; a Blank or Standard file without it is allowed and recorded, because a solvent blank
    or an authentic-standard injection often carries no internal standard.

    Every candidate is listed with the files it has no height in, and the valid ones other than
    the chosen spot as valid_alternatives: the person may take any of them instead (an invalid
    choice included), which plan_normalize accepts as accept_spot.

    library_annotated says which spots tier A reads as a library's annotation, the LBM library's by
    default; laboratory metabolomics passes its MSP libraries, named library_label in what is said.
    """
    annotator_id = annotator_id or INTERNAL_STANDARD_ANNOTATOR_IDS.get(polarity, "")
    spots = export["spots"]
    file_types = export["file_types"]
    sample_names = export["sample_names"]
    intensity = {spot["id"]: _mean_intensity(spot, file_types) for spot in spots}
    lbm_rt_tolerance = max(annotation_rt_tolerance, internal_standard_rt_tolerance)
    records = list(standards)
    entries: list[dict[str, Any]] = []

    def zero_heights(spot: dict[str, Any]) -> tuple[list[str], list[str]]:
        required: list[str] = []
        tolerated: list[str] = []
        for cell, kind, name in zip(spot["cells"], file_types, sample_names):
            if _is_zero_height(cell):
                (required if kind.lower() in STANDARD_REQUIRED_FILE_TYPES else tolerated).append(name)
        return required, tolerated

    for record in records:
        window_mz = ms1_tolerance_for(record.mz, ms1_tolerance)

        def at_mz(spot: dict[str, Any]) -> bool:
            return spot["mz"] is not None and abs(spot["mz"] - record.mz) <= window_mz + 1e-9

        def within(spot: dict[str, Any], tolerance: float) -> bool:
            return spot["rt"] is not None and abs(spot["rt"] - record.rt) <= tolerance + 1e-9

        tiers: dict[str, dict[str, Any]] = {}
        isotopic: list[str] = []
        for spot in spots:
            if not at_mz(spot):
                continue
            if spot["isotope_weight"] != 0:
                if within(spot, internal_standard_rt_tolerance):
                    isotopic.append(spot["id"])
                continue
            representative = representative_candidate(candidates, spot["id"])
            tier = None
            lbm = library_annotated(spot) if library_annotated else spot["method"].startswith("LbmDB")
            # Without a candidate table, an old export's name prefix is the only sign of a suggestion.
            # MS-DIAL names the annotator in the Comment for any database hit, a suggestion below the
            # score cutoff included, and the export no longer prefixes such a name.
            matched = (representative["is_reference_matched"] if representative
                       else not spot["name"].lower().startswith(tuple(item.lower() for item in _NAME_PREFIXES)))
            named = record.name.casefold() in console_name_aliases(spot["name"])
            if (lbm and matched and within(spot, lbm_rt_tolerance)
                    and name_equivalent(spot["name"], record.name)):
                tier = "A"
            elif within(spot, internal_standard_rt_tolerance):
                if spot["method"] == annotator_id and matched and named:
                    tier = "B"
                elif (spot["method"] == annotator_id and named) or any(
                    row["annotator_id"] == annotator_id
                    and (not row["is_representative"] or not row["is_reference_matched"])
                    and record.name.casefold() in console_name_aliases(row["name"])
                    for row in (candidates or {}).get("by_spot", {}).get(spot["id"], [])
                ):
                    tier = "C"
                else:
                    tier = "D"
            if tier:
                tiers[spot["id"]] = {"spot": spot, "tier": tier}

        def rank(item: dict[str, Any]) -> tuple[Any, ...]:
            spot = item["spot"]
            return (
                # Tier A reaches out to the annotation tolerance for a stale library RT; a spot in
                # the IS window still comes first, whatever a farther one's height.
                0 if within(spot, internal_standard_rt_tolerance) else 1,
                -intensity[spot["id"]], -spot["fill"], abs(spot["rt"] - record.rt), abs(spot["mz"] - record.mz),
                _id_number(spot["id"]),
            )

        listed = sorted(tiers.values(), key=lambda item: ("ABCD".index(item["tier"]), rank(item)))
        chosen = listed[0] if listed else None
        entry: dict[str, Any] = {
            "name": record.name, "polarity": polarity, "text_mz": record.mz, "text_rt": record.rt,
            "text_adduct": record.adduct, "tier": None, "status": "unresolved_missing",
            "alignment_id": None, "needs_confirmation": False, "spot": None,
            "candidates": [
                {"alignment_id": item["spot"]["id"], "tier": item["tier"], "name": item["spot"]["name"],
                 "adduct": item["spot"]["adduct"], "mz": item["spot"]["mz"], "rt": item["spot"]["rt"],
                 "intensity": intensity[item["spot"]["id"]], "method": item["spot"]["method"],
                 "zero_height_files": zero_heights(item["spot"])[0],
                 "valid": not zero_heights(item["spot"])[0]}
                for item in listed
            ],
            "valid_alternatives": [],
            "isotopic_spots_in_window": isotopic,
            "other_observations": [],
            "label_disagreement": None,
            "zero_height_files": [], "tolerated_zero_height_files": [],
            "warnings": [], "reason": "",
        }
        for spot in spots:
            if spot["id"] in tiers or not spot["name"] or not name_equivalent(spot["name"], record.name):
                continue
            entry["other_observations"].append({
                "alignment_id": spot["id"], "name": spot["name"], "adduct": spot["adduct"], "mz": spot["mz"],
                "rt": spot["rt"], "intensity": intensity[spot["id"]], "method": spot["method"],
            })
        if chosen is None:
            entry["reason"] = (
                f"no spot within {window_mz * 1000:.1f} mDa of {record.mz} ({record.adduct}) and "
                f"{internal_standard_rt_tolerance} min of {record.rt} min"
            )
            entry["warnings"].append(f"{record.name} was not found; every class it quantifies will be NOT QUANTIFIED.")
            entries.append(entry)
            continue
        spot = chosen["spot"]
        delta_mz = spot["mz"] - record.mz
        delta_rt = spot["rt"] - record.rt
        entry.update({
            "tier": chosen["tier"], "status": TIER_STATUS[chosen["tier"]], "alignment_id": spot["id"],
            "needs_confirmation": TIER_STATUS[chosen["tier"]] in NEEDS_CONFIRMATION,
            "spot": {"name": spot["name"], "adduct": spot["adduct"], "mz": spot["mz"], "rt": spot["rt"],
                     "method": spot["method"], "fill": spot["fill"], "intensity": intensity[spot["id"]],
                     "ontology": spot["ontology"]},
            "delta_mz_mda": round(delta_mz * 1000, 4), "delta_mz_ppm": round(delta_mz / record.mz * 1e6, 3),
            "delta_rt": round(delta_rt, 4),
            "reason": {
                "A": f"the {library_label} annotated it at the library adduct's m/z",
                "B": "its internal-standard text row annotated it",
                "C": "its text row matched, but another annotation was kept for the spot or the match is only "
                     "a suggestion below the score cutoff",
                "D": "the most intense spot at the library adduct's m/z and RT, whatever its annotation",
            }[chosen["tier"]],
        })
        if spot["adduct"] and not same_adduct(spot["adduct"], record.adduct):
            # The text adduct is used: the m/z is the library adduct's, whatever the label says.
            entry["label_disagreement"] = {"label": spot["adduct"], "library_adduct": record.adduct}
        if abs(delta_rt) > internal_standard_rt_tolerance:
            entry["warnings"].append(
                f"{record.name} was annotated by the {library_label} {delta_rt:+.2f} min from the library RT "
                f"{record.rt}; the library RT may be stale."
            )
            in_window = [item["spot"]["id"] for item in listed if within(item["spot"], internal_standard_rt_tolerance)]
            if in_window:
                # A stale library RT, or another lipid the LBM library named as the standard: which
                # one, the spots inside the window cannot say on their own.
                entry["needs_confirmation"] = True
                entry["reason"] += (
                    f", {delta_rt:+.2f} min from the library RT, while spot(s) {', '.join(in_window)} lie within "
                    f"{internal_standard_rt_tolerance} min of it"
                )
        elif abs(delta_rt) > internal_standard_rt_tolerance / 2:
            entry["warnings"].append(f"{record.name} lies {delta_rt:+.2f} min from the library RT {record.rt}.")
        entry["zero_height_files"], entry["tolerated_zero_height_files"] = zero_heights(spot)
        entry["valid_alternatives"] = [
            item["alignment_id"] for item in entry["candidates"] if item["valid"] and item["alignment_id"] != spot["id"]
        ]
        if entry["zero_height_files"]:
            entry["status"] = "invalid"
            entry["needs_confirmation"] = False
            entry["reason"] = (
                f"spot {spot['id']} has no height in {', '.join(entry['zero_height_files'])}, and a "
                "concentration divided by nothing is not a concentration"
            )
            if entry["valid_alternatives"]:
                entry["reason"] += (
                    f"; spot(s) {', '.join(entry['valid_alternatives'])} at its m/z and RT have a height in every "
                    "sample and QC, and the person may take one instead"
                )
        entries.append(entry)

    claimed: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if entry["status"] in RESOLVED_STATUSES:
            claimed.setdefault(entry["alignment_id"], []).append(entry)
    for spot_id, owners in claimed.items():
        if len(owners) > 1:
            names = ", ".join(owner["name"] for owner in owners)
            for owner in owners:
                owner["status"] = "conflict"
                owner["needs_confirmation"] = False
                owner["reason"] = f"spot {spot_id} was chosen for more than one standard ({names})"

    present = {record.name for record in records}
    for name in standard_names:
        if name not in present and not any(entry["name"] == name for entry in entries):
            entries.append({
                "name": name, "polarity": polarity, "status": "unresolved_expected", "tier": None,
                "alignment_id": None, "needs_confirmation": False, "candidates": [], "warnings": [],
                "reason": f"the {polarity.lower()} internal-standard library does not list it",
            })
            present.add(name)

    def names_with(*statuses: str) -> list[str]:
        return [entry["name"] for entry in entries if entry["status"] in statuses]

    return {
        "polarity": polarity,
        "standards": entries,
        "peak_ids": {
            entry["name"]: entry["alignment_id"] for entry in entries if entry["status"] in RESOLVED_STATUSES
        },
        "needs_confirmation": [entry["name"] for entry in entries if entry.get("needs_confirmation")],
        "unresolved_expected": names_with("unresolved_expected"),
        "unresolved_missing": names_with("unresolved_missing"),
        "invalid": names_with("invalid", "conflict"),
        "parameters": {
            "annotation_rt_tolerance": annotation_rt_tolerance,
            "internal_standard_rt_tolerance": internal_standard_rt_tolerance,
            "lbm_rt_tolerance": lbm_rt_tolerance,
            "ms1_tolerance": ms1_tolerance, "annotator_id": annotator_id,
            "intensity": "mean exported height over the non-Blank files",
        },
        "inputs": {
            "height_export": export.get("path"), "height_export_sha256": export.get("sha256"),
            "candidate_table": (candidates or {}).get("path"),
            "candidate_table_sha256": (candidates or {}).get("sha256"),
        },
        "warnings": [warning for entry in entries for warning in entry.get("warnings", [])],
    }


def _id_number(value: str) -> tuple[int, str]:
    number = parse_number(value)
    return (int(number), value) if number is not None else (1 << 62, value)


def resolution_digest(resolution: dict[str, Any]) -> str:
    """A digest binding a resolution to the exports it was read from, for the person's confirmation."""
    text = json.dumps(resolution, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


RESOLUTION_COLUMNS = (
    "standard", "polarity", "library_mz", "library_rt", "library_adduct", "tier", "status",
    "alignment_id", "ms_dial_name", "ms_dial_adduct", "mz", "rt", "annotation_method",
    "delta_mz_mda", "delta_mz_ppm", "delta_rt", "intensity", "fill", "zero_height_files",
    "tolerated_zero_height_files", "candidates", "other_observations", "label_disagreement", "reason",
    "warnings", "height_export_sha256", "candidate_table_sha256",
)


def write_resolution_tsv(resolution: dict[str, Any], path: str | Path) -> dict[str, Any]:
    """is_resolution_{pos,neg}.tsv: one row per standard, with every candidate it was chosen from."""
    inputs = resolution.get("inputs", {})

    def text(value: Any) -> str:
        return "" if value is None else str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")

    lines = ["\t".join(RESOLUTION_COLUMNS)]
    for entry in resolution.get("standards", []):
        spot = entry.get("spot") or {}
        disagreement = entry.get("label_disagreement") or {}
        lines.append("\t".join(text(value) for value in (
            entry["name"], entry.get("polarity"), entry.get("text_mz"), entry.get("text_rt"),
            entry.get("text_adduct"), entry.get("tier"), entry["status"], entry.get("alignment_id"),
            spot.get("name"), spot.get("adduct"), spot.get("mz"), spot.get("rt"), spot.get("method"),
            entry.get("delta_mz_mda"), entry.get("delta_mz_ppm"), entry.get("delta_rt"),
            spot.get("intensity"), spot.get("fill"), "; ".join(entry.get("zero_height_files", [])),
            "; ".join(entry.get("tolerated_zero_height_files", [])),
            "; ".join(f"{item['alignment_id']} {item['tier']} {item['name']} {item['adduct']} "
                      f"m/z {item['mz']} RT {item['rt']} I {item['intensity']:.0f}"
                      for item in entry.get("candidates", [])),
            "; ".join(f"{item['alignment_id']} {item['name']} {item['adduct']} m/z {item['mz']} RT {item['rt']}"
                      for item in entry.get("other_observations", [])),
            f"labelled {disagreement['label']}, library {disagreement['library_adduct']}" if disagreement else "",
            entry.get("reason"), "; ".join(entry.get("warnings", [])),
            inputs.get("height_export_sha256"), inputs.get("candidate_table_sha256"),
        )))
    data = ("\r\n".join(lines) + "\r\n").encode("utf-8")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": str(target), "sha256": hashlib.sha256(data).hexdigest(), "rows": len(lines) - 1}


# ---------------------------------------------------------------------------------------------
# The standards table normalize reads
# ---------------------------------------------------------------------------------------------

STANDARDS_COLUMNS = ("StandardName", "TargetClass", "Concentration", "PeakID", "AssignName")


def chosen_candidate(entry: dict[str, Any], alignment_id: Any) -> dict[str, Any]:
    """The candidate a person named for a standard, if the resolver listed it and it can be used.

    Only a spot the resolver listed for that standard qualifies: at the library adduct's m/z and
    inside its RT window, so naming a spot never reaches past what was searched. It must have a
    height in every sample and QC file, as the resolver's own choice must.
    """
    wanted = str(alignment_id).strip()
    name = entry.get("name", "this standard")
    for item in entry.get("candidates", []):
        if str(item["alignment_id"]) == wanted:
            if item.get("zero_height_files"):
                raise LabInputError(
                    f"Spot {wanted} has no height in {', '.join(item['zero_height_files'])}, so it cannot be {name}."
                )
            return item
    listed = ", ".join(str(item["alignment_id"]) for item in entry.get("candidates", [])) or "none"
    raise LabInputError(
        f"Spot {wanted} is not a candidate for {name} at its library m/z and RT (candidates: {listed})."
    )


def standards_rows(
    lookup: dict[str, Any],
    resolution: dict[str, Any],
    accepted: Iterable[str] = (),
    chosen: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Sheet 2 with each standard's chosen alignment ID; -1 where the standard is not to be used.

    A standard the resolver found only by text candidate or plain search (tiers C and D) is used
    only once the person accepted it, and then with AssignName, so MS-DIAL's own export names the
    spot as the standard it is. An LBM annotation the resolver asked the person about is likewise
    used only once accepted, under the name MS-DIAL gave it. A spot the person chose instead
    (chosen, by standard; see chosen_candidate) is used whatever the resolver decided, with
    AssignName when it is a tier C or D spot. Any other standard without a valid spot gets -1,
    which normalize run with --peak-ids-only never replaces by a lookup of its own.
    """
    accepted_names = set(accepted)
    chosen = {str(name): value for name, value in (chosen or {}).items()}
    by_name = {entry["name"]: entry for entry in resolution.get("standards", [])}
    rows = []
    for row in lookup.get("rows", []):
        entry = by_name.get(row["standard"], {})
        status = entry.get("status")
        if row["standard"] in chosen:
            item = chosen_candidate(entry, chosen[row["standard"]])
            peak_id, assign, status = int(item["alignment_id"]), item["tier"] in ("C", "D"), "chosen_by_person"
        else:
            asks = status in NEEDS_CONFIRMATION or bool(entry.get("needs_confirmation"))
            usable = status in RESOLVED_STATUSES and (not asks or row["standard"] in accepted_names)
            peak_id = int(entry["alignment_id"]) if usable else -1
            assign = bool(usable and status in NEEDS_CONFIRMATION)
        rows.append({
            "StandardName": row["standard"], "TargetClass": row["target_class"],
            "Concentration": repr(float(row["amount"])) if row.get("amount") is not None else "",
            "PeakID": peak_id, "AssignName": assign,
            "status": status or "unresolved_expected",
        })
    return rows


def write_standards_tsv(rows: Iterable[dict[str, Any]], path: str | Path) -> dict[str, Any]:
    lines = ["\t".join(STANDARDS_COLUMNS)]
    count = 0
    for row in rows:
        lines.append("\t".join([
            str(row["StandardName"]), str(row["TargetClass"]), str(row["Concentration"]),
            str(row["PeakID"]), "true" if row.get("AssignName") else "false",
        ]))
        count += 1
    data = ("\r\n".join(lines) + "\r\n").encode("ascii")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": str(target), "sha256": hashlib.sha256(data).hexdigest(), "rows": count}


def predict_peak_id_mismatches(
    rows: Iterable[dict[str, Any]], candidates: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """The standards normalize will report as naming a spot annotated as something else.

    normalize compares the standard's name with the spot's stored name, whole and per |-part,
    ignoring case. The stored name is the representative candidate's full name, with a 'low
    score: ' or 'no MS2: ' prefix on its first part when the match was only suggested; the export
    shows neither the full name nor the prefix, so the candidate table is read instead.
    """
    predicted: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        peak_id = int(row["PeakID"])
        if peak_id < 0:
            continue
        representative = representative_candidate(candidates, str(peak_id))
        name = representative["name"] if representative else ""
        if representative and representative["is_reference_matched"]:
            aliases = console_name_aliases(name)
        elif representative and representative["is_annotation_suggested"]:
            parts = [part.strip().casefold() for part in name.split("|") if part.strip()]
            aliases = set(parts[1:])
        else:
            aliases = set()
        if row["StandardName"].casefold() not in aliases:
            predicted[(row["StandardName"], peak_id)] = {
                "standard": row["StandardName"], "alignment_id": peak_id, "ms_dial_name": name,
                "assign_name": bool(row.get("AssignName")),
            }
    return list(predicted.values())


# ---------------------------------------------------------------------------------------------
# normalize: its report and its numbers
# ---------------------------------------------------------------------------------------------

_RESOLVED_OLD = re.compile(r"^\s+(?P<name>.+?) -> (?P<target>.+?) via (?P<how>.+), concentration (?P<amount>\S+)$")
_RESOLVED = re.compile(r"^\s+(?P<name>.+?) via (?P<how>.+), concentration (?P<amount>\S+)$")
_COVERS = re.compile(r"^\s+covers \d+ class\(es\): (?P<classes>.*)$")
_UNRESOLVED = re.compile(r"^\s+UNRESOLVED (?P<name>.+?) -- named for \d+ class\(es\): (?P<classes>.*)$")
_UNRESOLVED_OLD = re.compile(r"^\s+UNRESOLVED (?P<name>.+?) -> (?P<target>.+)$")
_MISMATCHED = re.compile(
    r"^WARNING: \d+ standard\(s\) were taken from an alignment ID that holds a different compound: (?P<items>.*)\.$"
)
_MISMATCH_ITEM = re.compile(r"^(?P<name>.+?) \(alignment ID (?P<id>-?\d+) is annotated '(?P<annotated>.*)'\)$")


def parse_normalize_stdout(text: str) -> dict[str, Any]:
    """What `MSDIALCUI normalize` said it did: each standard's spot, the unresolved, the mismatched."""
    result: dict[str, Any] = {
        "resolved": {}, "unresolved": {}, "ambiguous": [], "mismatched": [], "warnings": [],
        "unit": None, "dilution_factor_applied": None, "outputs": [], "redacted_rows": 0, "other_lines": [],
    }
    last = None
    for raw in str(text or "").lstrip("﻿").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        match = _RESOLVED_OLD.match(line) or _RESOLVED.match(line)
        if match and " UNRESOLVED " not in f" {line.strip()} ":
            name = match.group("name").strip()
            how = match.group("how")
            found = re.search(r"alignment ID (-?\d+)", how)
            entry = result["resolved"].setdefault(name, {
                "alignment_id": int(found.group(1)) if found else None, "how": how,
                "concentration": parse_number(match.group("amount")), "classes": [],
            })
            if "target" in match.groupdict() and match.group("target"):
                entry["classes"].append(match.group("target").strip())
            last = entry
            continue
        match = _COVERS.match(line)
        if match and last is not None:
            last["classes"].extend(item.strip() for item in match.group("classes").split(",") if item.strip())
            continue
        match = _UNRESOLVED.match(line)
        if match:
            result["unresolved"].setdefault(match.group("name").strip(), []).extend(
                item.strip() for item in match.group("classes").split(",") if item.strip()
            )
            continue
        match = _UNRESOLVED_OLD.match(line)
        if match:
            result["unresolved"].setdefault(match.group("name").strip(), []).append(match.group("target").strip())
            continue
        stripped = line.strip()
        if stripped.startswith(("AMBIGUOUS ", "NOTE ")):
            result["ambiguous"].append(stripped.split(" ", 1)[1])
            continue
        match = _MISMATCHED.match(stripped)
        if match:
            for item in match.group("items").split("; "):
                found = _MISMATCH_ITEM.match(item.strip())
                result["mismatched"].append(
                    {"standard": found.group("name"), "alignment_id": int(found.group("id")),
                     "annotated": found.group("annotated")} if found else {"standard": item.strip()}
                )
            continue
        if stripped.startswith("WARNING: "):
            result["warnings"].append(stripped[len("WARNING: "):])
            continue
        match = re.match(r"^(\d+) row\(s\) had no standard of their own class", stripped)
        if match:
            result["redacted_rows"] += int(match.group(1))
            continue
        if stripped.startswith("Normalized unit: "):
            result["unit"] = stripped[len("Normalized unit: "):].strip()
            continue
        if stripped.startswith("Dilution factor applied: "):
            result["dilution_factor_applied"] = stripped.rsplit(" ", 1)[-1].lower() == "true"
            continue
        if stripped.endswith(("_Height.txt", "_NormalizedHeight.txt")):
            result["outputs"].append(stripped)
            continue
        result["other_lines"].append(stripped)
    return result


def compare_normalize_stdout(parsed: dict[str, Any], rows: Iterable[dict[str, Any]]) -> list[str]:
    """Where normalize's own account differs from the table it was given."""
    expected: dict[str, int] = {}
    unresolved: set[str] = set()
    for row in rows:
        if int(row["PeakID"]) >= 0:
            expected[row["StandardName"]] = int(row["PeakID"])
        else:
            unresolved.add(row["StandardName"])
    problems = []
    for name, peak_id in sorted(expected.items()):
        got = parsed["resolved"].get(name)
        if got is None:
            problems.append(f"{name} was given alignment ID {peak_id} but normalize did not report it resolved.")
        elif got.get("alignment_id") != peak_id:
            problems.append(f"{name} was given alignment ID {peak_id} but normalize used {got.get('alignment_id')}.")
    for name in sorted(set(parsed["resolved"]) - set(expected)):
        problems.append(f"normalize resolved {name}, which the table left unresolved.")
    reported = set(parsed["unresolved"])
    for name in sorted(unresolved - reported - set(parsed["resolved"])):
        problems.append(f"{name} was left unresolved but normalize did not report it.")
    return problems


def annotated_lipid_class(representative: dict[str, Any] | None, vocabulary: LipidClassVocabulary) -> str:
    """The class normalize quantifies a spot as (NormalizationTargetSpot.GetAnnotatedLipidClass).

    Not reference matched: Unknown, whatever the Ontology column says. Otherwise the reference's
    CompoundClass, or its Ontology when CompoundClass is empty and the Ontology is a VS2 name,
    converted through the enum to the VS2 spelling. The candidate table merges the two fields
    into one column; a text-library reference has no CompoundClass, so its column is the Ontology.
    """
    if not representative or not representative["is_reference_matched"]:
        return UNKNOWN_CLASS
    text = str(representative.get("ontology") or "").strip()
    if str(representative.get("source") or "").strip() == "TextDB" and not vocabulary.is_vs2(text):
        text = ""
    if not text:
        return UNKNOWN_CLASS
    return vocabulary.canonical_vs2(text)


def verify_normalized(
    height_path: str | Path,
    normalized_path: str | Path,
    rows: Iterable[dict[str, Any]],
    candidates: dict[str, Any],
    vocabulary: LipidClassVocabulary | None = None,
    *,
    unit: str,
    dilution_factors: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Recompute every quantified cell from the Height matrix the same normalize call wrote.

    Each value must be amount x height / standard height (x the file's dilution factor, which
    MS-DIAL multiplies by). The Height export is rounded to whole numbers while normalize divides
    the unrounded heights, so each cell is checked against the interval the rounding allows
    rather than to a fixed precision. A class whose standard did not resolve must be emptied with
    a NOT QUANTIFIED comment; a file whose standard has no height must be empty for that class;
    no row may be exported as raw Intensity. A row that fails is reported, never repaired.
    """
    vocabulary = vocabulary or load_lipid_class_vocabulary()
    if candidates is None:
        raise LabInputError(
            "The candidate table is needed: it says which spots normalize treats as reference matched."
        )
    height = read_alignment_export(height_path)
    normalized = read_alignment_export(normalized_path)
    problems: list[str] = []
    if height["sample_names"] != normalized["sample_names"]:
        problems.append("The Height and NormalizedHeight exports list different samples.")
    table = list(rows)
    own: dict[str, dict[str, Any]] = {}
    for row in table:
        own.setdefault(row["TargetClass"], row)
    fallback = own.get(ANY_OTHERS)
    standard_amounts: dict[str, float] = {}
    for row in table:
        if int(row["PeakID"]) >= 0:
            key = str(int(row["PeakID"]))
            amount = float(row["Concentration"])
            if key in standard_amounts and not math.isclose(standard_amounts[key], amount, rel_tol=1e-12):
                problems.append(f"Alignment ID {key} is given two different amounts.")
            standard_amounts[key] = amount
    heights = {spot["id"]: spot for spot in height["spots"]}
    factors = [float((dilution_factors or {}).get(name, 1.0)) for name in normalized["sample_names"]]
    file_types = normalized["file_types"]
    sample_names = normalized["sample_names"]

    failed: list[dict[str, Any]] = []
    emptied: list[dict[str, Any]] = []
    counts = Counter()

    def fail(spot: dict[str, Any], reason: str, **extra: Any) -> None:
        failed.append({"alignment_id": spot["id"], "name": spot["name"], "ontology": spot["ontology"],
                       "reason": reason, **extra})

    for spot in normalized["spots"]:
        counts["rows"] += 1
        source = heights.get(spot["id"])
        if source is None:
            fail(spot, "the Height export has no row with this alignment ID")
            continue
        comment_unit = normalized_unit(spot["comment"])
        if comment_unit == "Intensity":
            fail(spot, "exported as raw Intensity: no standard quantified it")
            continue
        if spot["id"] in standard_amounts:
            counts["internal_standard_rows"] += 1
            amount = standard_amounts[spot["id"]]
            bad = []
            for cell, height_cell, factor, name in zip(spot["cells"], source["cells"], factors, sample_names):
                value = parse_number(cell)
                if value is None and (cell == "" and _is_zero_height(height_cell)):
                    continue
                if value is None or not math.isclose(value, amount * factor, rel_tol=1e-9, abs_tol=1e-12):
                    bad.append(name)
            if bad:
                fail(spot, f"an internal standard's value is not its amount {amount:g}", files=bad)
            continue
        klass = annotated_lipid_class(representative_candidate(candidates, spot["id"]), vocabulary)
        standard = own.get(klass) if klass in own and klass != ANY_OTHERS else fallback
        if standard is None or int(standard["PeakID"]) < 0:
            counts["not_quantified_rows"] += 1
            values_present = any(cell.strip() for cell in spot["cells"])
            if values_present or not spot["comment"].startswith("NOT QUANTIFIED"):
                designated = standard["StandardName"] if standard else ANY_OTHERS
                fail(spot, f"its class {klass} has no resolved standard ({designated}) but the row was not "
                           "emptied with a NOT QUANTIFIED comment", class_=klass)
            continue
        counts["quantified_rows"] += 1
        if comment_unit != unit:
            fail(spot, f"its unit is '{comment_unit}', not {unit}", class_=klass)
            continue
        standard_spot = heights.get(str(int(standard["PeakID"])))
        if standard_spot is None:
            fail(spot, f"the standard's alignment ID {standard['PeakID']} is not in the Height export")
            continue
        amount = float(standard["Concentration"])
        # 'Replace true zero values' writes a zero as 0.1 x the row's smallest true value, matrix by
        # matrix (LegacyQuantValueAccessor): here the smallest normalized value of a file with a
        # height, never a replaced cell, which is smaller still.
        true_values = [
            parse_number(cell) for cell, height_cell in zip(spot["cells"], source["cells"])
            if not _is_zero_height(height_cell)
        ]
        positive = [value for value in true_values if value is not None and value > 0]
        # With no true value at all MS-DIAL takes 1 as the minimum.
        replacement = 0.1 * (min(positive) if positive else 1.0)
        bad = []
        for cell, height_cell, standard_cell, factor, kind, name in zip(
            spot["cells"], source["cells"], standard_spot["cells"], factors, file_types, sample_names
        ):
            if _is_zero_height(standard_cell):
                if cell.strip():
                    bad.append(f"{name} (the standard has no height in this file, yet the cell has a value)")
                else:
                    emptied.append({"alignment_id": spot["id"], "file": name, "file_type": kind,
                                    "standard": standard["StandardName"]})
                continue
            value = parse_number(cell)
            if value is None:
                bad.append(f"{name} (empty)")
                continue
            standard_height = parse_number(standard_cell)
            target = parse_number(height_cell)
            if target is None or not float(target).is_integer():
                # A zero, or MS-DIAL's replacement for one, in this file: the value is zero or the
                # normalized matrix's own replacement.
                target = 0.0
                if math.isclose(value, replacement, rel_tol=1e-6):
                    continue
            low = amount * max(0.0, target - 0.5) / (standard_height + 0.5) * factor
            high = amount * (target + 0.5) / (standard_height - 0.5) * factor
            if not (low * (1 - 1e-9) - 1e-12 <= value <= high * (1 + 1e-9) + 1e-12):
                bad.append(f"{name} ({value:g}; amount x height / standard height gives {low:g} to {high:g})")
        if bad:
            fail(spot, f"normalized by {standard['StandardName']} the values do not follow from the heights",
                 class_=klass, files=bad)
    for item in failed:
        if "class_" in item:
            item["class"] = item.pop("class_")
    return {
        "passed": not problems and not failed,
        "unit": unit,
        "problems": problems,
        "rows_checked": counts["rows"],
        "quantified_rows": counts["quantified_rows"],
        "not_quantified_rows": counts["not_quantified_rows"],
        "internal_standard_rows": counts["internal_standard_rows"],
        "failed_rows": failed,
        "failed_ids": sorted({item["alignment_id"] for item in failed}, key=_id_number),
        "emptied_for_missing_standard_height": emptied,
        "inputs": {"height": height["path"], "height_sha256": height["sha256"],
                   "normalized": normalized["path"], "normalized_sha256": normalized["sha256"],
                   "candidate_table_sha256": candidates.get("sha256")},
    }


# ---------------------------------------------------------------------------------------------
# Sample tables
# ---------------------------------------------------------------------------------------------

SAMPLE_METADATA_COLUMNS = {
    "file_path": ("filepath", "path"),
    "file_name": ("filename", "name"),
    "file_type": ("type", "filetype"),
    "class_id": ("classid", "class"),
    # "Acquisiotion type" is how MS-DIAL's own sample table template spells it.
    "acquisition_type": ("acquisitiontype", "acquisiotiontype"),
    "batch_order": ("batch", "batchorder", "batchid"),
    "analytical_order": ("analyticalorder", "injectionorder"),
    "factor": ("dilutionfactor", "factor"),
    "included": ("included",),
}


def read_sample_metadata(path: str | Path) -> dict[str, Any]:
    """A sample table with MS-DIAL's GUI headers, from .xlsx (first sheet), .tsv/.txt or .csv."""
    source = Path(path)
    warnings: list[str] = []
    blockers: list[str] = []
    if source.suffix.lower() == ".xlsx":
        workbook = read_xlsx(source)
        warnings.extend(workbook.warnings)
        sheet = workbook.sheets[0]
        width = 1 + max((column for _, column in sheet.cells), default=0)
        table = [
            [sheet.text(f"{column_letters(column)}{row}") for column in range(width)]
            for row in range(1, sheet.max_row + 1)
        ]
    else:
        text = source.read_text(encoding="utf-8-sig")
        delimiter = "," if source.suffix.lower() == ".csv" else "\t"
        table = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    table = [row for row in table if any(str(cell).strip() for cell in row)]
    if not table:
        return {"path": str(source), "rows": [], "blockers": [f"{source.name} is empty."], "warnings": warnings}
    header = [_header_key(cell) for cell in table[0]]
    columns: dict[str, int] = {}
    for key, aliases in SAMPLE_METADATA_COLUMNS.items():
        for alias in aliases:
            if alias in header:
                columns[key] = header.index(alias)
                break
    if "acquisiotiontype" in header:
        warnings.append(f"{source.name} spells its header 'Acquisiotion type'; it is read as Acquisition type.")
    if "file_name" not in columns and "file_path" not in columns:
        blockers.append(f"{source.name} has neither a File name nor a File path column.")
        return {"path": str(source), "rows": [], "blockers": blockers, "warnings": warnings}
    rows = []
    for number, row in enumerate(table[1:], start=2):
        def cell(key: str) -> str:
            index = columns.get(key, -1)
            return str(row[index]).strip() if 0 <= index < len(row) else ""

        file_path = cell("file_path")
        file_name = cell("file_name") or (Path(file_path.replace("\\", "/")).stem if file_path else "")
        if not file_name:
            continue
        item: dict[str, Any] = {
            "line": number, "file_path": file_path, "file_name": file_name,
            "stem": strip_polarity(file_name), "file_type": cell("file_type"), "class_id": cell("class_id"),
            "acquisition_type": cell("acquisition_type"),
        }
        for key in ("batch_order", "analytical_order"):
            value = parse_number(cell(key)) if cell(key) else None
            if cell(key) and (value is None or not value.is_integer()):
                blockers.append(
                    f"{source.name} line {number}: {key.replace('_', ' ')} '{cell(key)}' is not a whole number."
                )
            item[key] = int(value) if value is not None and value.is_integer() else None
        factor = parse_number(cell("factor")) if cell("factor") else 1.0
        if factor is None or factor <= 0:
            blockers.append(
                f"{source.name} line {number}: dilution factor '{cell('factor')}' is not a positive number."
            )
        item["factor"] = factor
        included = _flag(cell("included")) if cell("included") else True
        if included is None:
            blockers.append(f"{source.name} line {number}: Included '{cell('included')}' is not TRUE or FALSE.")
        item["included"] = bool(included) if included is not None else True
        rows.append(item)
    stems = Counter(item["stem"].casefold() for item in rows)
    for stem, count in stems.items():
        if count > 1:
            blockers.append(f"{source.name} lists the sample {stem} {count} times.")
    factors = sorted({item["factor"] for item in rows if item["factor"]})
    if any(value != 1.0 for value in factors):
        warnings.append(
            f"{source.name} gives dilution factors {', '.join(f'{value:g}' for value in factors)}; "
            "MS-DIAL multiplies each file's concentrations by its factor when they are applied."
        )
    return {"path": str(source.resolve()), "rows": rows, "blockers": blockers, "warnings": warnings}


def join_sample_metadata(metadata: dict[str, Any], files: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Each proposed file's metadata row, by exact name or by its stem without the polarity.

    A table written for one polarity, or with paths from another computer, still describes these
    samples; the path is never used to match.
    """
    exact = {row["file_name"].casefold(): row for row in metadata.get("rows", [])}
    by_stem = {row["stem"].casefold(): row for row in metadata.get("rows", [])}
    joined = []
    disagreements = []
    for item in files:
        name = str(item.get("file_name", ""))
        row = exact.get(name.casefold())
        basis = "file name" if row else ""
        if row is None:
            row = by_stem.get(strip_polarity(name).casefold())
            basis = "stem" if row else ""
        joined.append({"file_name": name, "metadata": row, "matched_by": basis})
        if row is None:
            continue
        for key in ("file_type", "class_id", "analytical_order", "batch_order"):
            proposed = item.get(key)
            stated = row.get(key)
            if stated in (None, "") or proposed in (None, ""):
                continue
            if str(proposed).strip().casefold() != str(stated).strip().casefold():
                disagreements.append({"file_name": name, "field": key, "proposed": proposed, "metadata": stated})
    unmatched = [entry["file_name"] for entry in joined if entry["metadata"] is None]
    return {"files": joined, "unmatched": unmatched, "disagreements": disagreements}


def pair_sample_tables(positive: Iterable[dict[str, Any]], negative: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """One table for both polarities, paired by stem; the merge pairs columns the same way."""
    left = {strip_polarity(str(item["file_name"])).casefold(): item for item in positive}
    right = {strip_polarity(str(item["file_name"])).casefold(): item for item in negative}
    rows = []
    problems = []
    for stem in sorted(set(left) | set(right), key=lambda key: (
        (left.get(key) or right.get(key) or {}).get("analytical_order") or 0, key)):
        pos, neg = left.get(stem), right.get(stem)
        if pos is None or neg is None:
            missing = "negative" if neg is None else "positive"
            problems.append(f"{(pos or neg)['file_name']} has no {missing} counterpart.")
        source = pos or neg
        row = {
            "stem": strip_polarity(str(source["file_name"])),
            "positive_file": pos["file_name"] if pos else None,
            "negative_file": neg["file_name"] if neg else None,
        }
        for key in ("file_type", "class_id", "analytical_order", "batch_order", "factor"):
            row[key] = source.get(key)
            if pos and neg and pos.get(key) != neg.get(key):
                problems.append(
                    f"{row['stem']}: {key} is {pos.get(key)!r} in positive and {neg.get(key)!r} in negative."
                )
        rows.append(row)
    return {"rows": rows, "problems": problems, "matched": not problems}


# ---------------------------------------------------------------------------------------------
# Where the analysis is written
# ---------------------------------------------------------------------------------------------


def folder_layout_problems(positive_folder: str | Path, negative_folder: str | Path) -> list[str]:
    """The two raw folders must be different and neither inside the other.

    Each polarity's run writes its method, alignment and project into its own output folder;
    one folder holding both would mix two projects, and Interactive does not check it elsewhere.
    """
    left = Path(positive_folder).expanduser().resolve()
    right = Path(negative_folder).expanduser().resolve()
    if left == right:
        return [f"The positive and negative folders are the same folder ({left})."]
    if left in right.parents or right in left.parents:
        return [f"One polarity's folder lies inside the other's ({left}, {right})."]
    return []


def _folder_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def raw_folder_overlaps(
    root: str | Path, subfolders: dict[str, str | Path], raw_folders: dict[str, str | Path | None]
) -> list[str]:
    """Where an analysis would write into the raw data: the root inside a raw folder, or one of its
    subfolders (the runs' output folders, inputs, quant) being, holding or lying inside one.

    A root named as the parent of folders called pos and neg makes each run's output folder the raw
    folder itself: the method, the project, the exports and the library copy would land beside the
    raw files, on top of the intermediates MS-DIAL already writes there. A root that merely holds the
    raw folders elsewhere is allowed.
    """
    problems: list[str] = []
    root_key = _folder_key(root)
    for polarity, raw in raw_folders.items():
        if not raw:
            continue
        raw_key = _folder_key(raw)
        if root_key == raw_key or root_key.startswith(raw_key.rstrip(os.sep) + os.sep):
            problems.append(f"The analysis root {Path(root)} lies inside the {polarity.lower()} raw-data folder.")
            continue
        for label, folder in subfolders.items():
            key = _folder_key(folder)
            if key == raw_key:
                problems.append(f"The {label} {Path(folder)} would be the {polarity.lower()} raw-data folder itself.")
            elif key.startswith(raw_key.rstrip(os.sep) + os.sep):
                problems.append(f"The {label} {Path(folder)} would lie inside the {polarity.lower()} raw-data folder.")
            elif raw_key.startswith(key.rstrip(os.sep) + os.sep):
                problems.append(f"The {label} {Path(folder)} would hold the {polarity.lower()} raw-data folder {Path(raw)}.")
    return problems


def analysis_root_problems(
    root: str | Path, *, home: str | Path | None = None, temporary: Iterable[str | Path] | None = None
) -> dict[str, list[str]]:
    """Where a laboratory analysis may be written: not into a code checkout or a temporary folder.

    A system drive or the user profile is the analyst's call, so it is said rather than refused.
    """
    path = Path(root).expanduser().resolve()
    blockers: list[str] = []
    warnings: list[str] = []
    if temporary is None:
        temporary = {tempfile.gettempdir(), os.environ.get("TEMP", ""), os.environ.get("TMP", "")}
    temporary_roots = [Path(item).expanduser().resolve() for item in temporary if str(item)]
    for folder in temporary_roots:
        if path == folder or folder in path.parents:
            blockers.append(f"{path} is inside the temporary folder {folder}, which is cleared without asking.")
            break
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            blockers.append(f"{path} is inside the code checkout {parent}; analysis outputs do not belong there.")
            break
    home_path = Path(home).expanduser().resolve() if home else Path.home().resolve()
    if path == home_path or home_path in path.parents:
        warnings.append(f"{path} is inside the user profile {home_path}.")
    if path.drive.upper() == "C:":
        warnings.append(f"{path} is on the system drive C:.")
    return {"blockers": blockers, "warnings": warnings}


# ---------------------------------------------------------------------------------------------
# The method a laboratory lipidomics run states rather than asks
# ---------------------------------------------------------------------------------------------


def lab_method_answers() -> dict[str, Any]:
    """The guided answers every laboratory lipidomics run states (S1-S7), with why.

    Written out in full rather than left to Interactive's defaults: those differ (6 anchors, no
    local window), and a value the record does not carry cannot be checked against the run.
    """
    answers = {
        "parameter_strategy": "auto_peak_range",
        "smoothing_method": "TimeBasedLinearWeightedMovingAverage",
        "execute_rt_correction": False,
        "execute_automatic_rt_correction": True,
        "automatic_rt_correction_maximum_anchors": 12,
        # Written explicitly, which also makes Interactive refuse a Console without MsdialWorkbench#826.
        "automatic_rt_correction_local_support_rt_window": 1.5,
        "automatic_rt_correction_interpolate_blanks_by_analytical_order": True,
        "run_qa": True,
        "height_matrix_export": True,
        "export_annotation_candidates": True,
        "stage_inputs": False,
        # Stated so that a plan from the contract leaves no question open; the text is written when
        # the run is completed (msdial_complete_guided_analysis).
        "generate_materials_methods": True,
    }
    basis = {
        "automatic_rt_correction_interpolate_blanks_by_analytical_order": (
            "the analytical order of the confirmed sample table, which the person confirmed for both polarities"
        ),
        "stage_inputs": "MS-DIAL reads the raw folders the person confirmed, and writes its intermediates beside them",
    }
    return {"answers": answers, "basis": basis}

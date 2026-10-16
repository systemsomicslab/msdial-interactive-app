"""The example quant_utility_table.xlsx shipped for the laboratory lipidomics tutorial.

It is the workbook the MS-DIAL laboratory used for the public hands-on demo (Zenodo
10.5281/zenodo.10957708, data2), as saved on 2026-10-14, with two parts rewritten and nothing else:
docProps/core.xml's creator and last-modified-by emptied, and xl/workbook.xml's absPath (the
folder Excel last saved it in) removed. Every other member is the source's, byte for byte, in the
source's order.

SOURCE_CELLS_SHA256 is the digest of what Interactive's own reader (read_xlsx) made of the source
workbook, every sheet and cell; the example must read to exactly that. Set
MSDIAL_QUANT_TABLE_SOURCE to the source workbook to compare the two files directly as well.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from msdial_app.lab_lipidomics import read_quant_workbook, read_xlsx
from msdial_app.lipidome_merge import reconciliation_selection_warnings

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "lab-lipidomics" / "quant_utility_table.xlsx"
SOURCE_CELLS_SHA256 = "fada0d22ec7add0f4ddb21fbbf61af05725e67a03139a20f289eaa85df08db24"
MEMBERS = [
    "[Content_Types].xml",
    "_rels/.rels",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
    "xl/worksheets/sheet1.xml",
    "xl/worksheets/sheet2.xml",
    "xl/worksheets/sheet3.xml",
    "xl/theme/theme1.xml",
    "xl/styles.xml",
    "xl/sharedStrings.xml",
    "xl/worksheets/_rels/sheet1.xml.rels",
    "xl/printerSettings/printerSettings1.bin",
    "xl/calcChain.xml",
    "docProps/core.xml",
    "docProps/app.xml",
]
REWRITTEN = {"docProps/core.xml", "xl/workbook.xml"}
# Anything that would name a person or a computer: an e-mail address, a drive-letter or home path.
PERSONAL = [
    re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
    re.compile(rb"\b[A-Za-z]:\\"),
    re.compile(rb"\\Users\\|/Users/|/home/", re.IGNORECASE),
    re.compile(rb"absPath"),
]
CORE = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
}


def cells_digest(path: Path) -> str:
    book = read_xlsx(path)
    view = [
        {
            "name": sheet.name, "part": sheet.part, "max_row": sheet.max_row, "merged": sheet.merged,
            "cells": [
                [row, column, cell.ref, cell.kind, repr(cell.value), cell.raw, cell.formula]
                for (row, column), cell in sorted(sheet.cells.items())
            ],
        }
        for sheet in book.sheets
    ] + [{"full_calc_on_load": book.full_calc_on_load, "warnings": book.warnings}]
    return hashlib.sha256(json.dumps(view, sort_keys=True).encode("utf-8")).hexdigest()


def parsed(path: Path, column: str) -> str:
    result = read_quant_workbook(path, column)
    result = {key: value for key, value in result.items() if key not in {"path", "sha256"}}
    result["quant_info"] = {key: value for key, value in result["quant_info"].items() if key != "path"}
    return json.dumps(result, sort_keys=True, default=lambda value: sorted(value) if isinstance(value, set) else str(value))


class ExampleWorkbookTests(unittest.TestCase):
    def test_it_reads_exactly_as_the_source_workbook_did(self) -> None:
        self.assertEqual(SOURCE_CELLS_SHA256, cells_digest(EXAMPLE))

    def test_its_members_are_the_source_members_in_the_source_order(self) -> None:
        with zipfile.ZipFile(EXAMPLE) as archive:
            self.assertEqual(MEMBERS, archive.namelist())
            self.assertIsNone(archive.testzip())
            for name in archive.namelist():
                if name.endswith((".xml", ".rels")):
                    ET.fromstring(archive.read(name))  # every XML part still parses

    def test_no_author_and_no_local_path_remain(self) -> None:
        with zipfile.ZipFile(EXAMPLE) as archive:
            core = ET.fromstring(archive.read("docProps/core.xml"))
            for tag in ("dc:creator", "cp:lastModifiedBy"):
                element = core.find(tag, CORE)
                self.assertIsNotNone(element, tag)
                self.assertFalse((element.text or "").strip(), f"{tag} is not empty")
            self.assertNotIn(b"x15ac:absPath", archive.read("xl/workbook.xml"))
            for name in archive.namelist():
                data = archive.read(name)
                for view in (data, data.decode("utf-16-le", "ignore").encode("utf-8", "ignore")):
                    for pattern in PERSONAL:
                        self.assertIsNone(pattern.search(view), f"{name} matches {pattern.pattern!r}")
        whole = EXAMPLE.read_bytes()
        for pattern in PERSONAL:
            self.assertIsNone(pattern.search(whole), f"the file matches {pattern.pattern!r}")

    def test_the_agent_reads_the_tutorial_choices_from_it(self) -> None:
        workbook = read_quant_workbook(EXAMPLE, "H")
        self.assertEqual([], workbook["blockers"])
        self.assertEqual(["1. IS preparation", "2. Lookup table", "3. Quant info"], workbook["sheet_names"])
        self.assertEqual("pmol_per_microL_plasma", workbook["unit"])
        preparation = workbook["preparation"]
        self.assertEqual("int (pmol/ul plasma)", preparation["amount_header"])
        self.assertEqual(("H4", 10.0), (preparation["sample_parameter"]["value_ref"], preparation["sample_parameter"]["value"]))
        self.assertEqual(15, len(preparation["standards"]))
        self.assertEqual(["FA 16:0(d3)"], workbook["lookup"]["unused_standards"])
        rows = {(row["class"], row["adduct"]): row["selected"] for row in workbook["quant_info"]["rows"]}
        self.assertTrue(rows[("Hex3Cer", "[M+H]+")])
        self.assertFalse(rows[("Hex3Cer", "[M+CH3COO]-")])
        self.assertFalse(rows[("EtherPE", "[M+H]+")])
        self.assertTrue(rows[("EtherPE", "[M-H]-")])
        self.assertTrue(rows[("BMP", "[M-H]-")])
        self.assertFalse(rows[("BMP", "[M+NH4]+")])
        # Sheet 3 already agrees with the merge's positive/negative reconciliation: no question is asked.
        self.assertEqual([], reconciliation_selection_warnings(workbook["quant_info"]["selected"]))

    def test_every_amount_column_reads(self) -> None:
        units = {column: read_quant_workbook(EXAMPLE, column)["unit"] for column in "FGH"}
        self.assertEqual(
            {"F": "pmol_per_mg_tissue", "G": "pmol_per_10E6_cells", "H": "pmol_per_microL_plasma"}, units
        )

    @unittest.skipUnless(os.environ.get("MSDIAL_QUANT_TABLE_SOURCE"), "MSDIAL_QUANT_TABLE_SOURCE names no source workbook")
    def test_against_the_source_workbook(self) -> None:
        source = Path(os.environ["MSDIAL_QUANT_TABLE_SOURCE"])
        self.assertEqual(cells_digest(source), cells_digest(EXAMPLE))
        for column in "FGH":
            self.assertEqual(parsed(source, column), parsed(EXAMPLE, column))
        with zipfile.ZipFile(source) as left, zipfile.ZipFile(EXAMPLE) as right:
            self.assertEqual(left.namelist(), right.namelist())
            changed = {name for name in left.namelist() if left.read(name) != right.read(name)}
            self.assertEqual(REWRITTEN, changed)
            for name in left.namelist():
                a, b = left.getinfo(name), right.getinfo(name)
                self.assertEqual(
                    (a.date_time, a.compress_type, a.external_attr, a.flag_bits),
                    (b.date_time, b.compress_type, b.external_attr, b.flag_bits),
                    name,
                )


if __name__ == "__main__":
    unittest.main()

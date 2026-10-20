"""Regenerate the format-1 defaults and examples from the laboratory example in examples/lab-lipidomics.

    python scripts/generate-lab-quant-input-defaults.py

Writes:
  resources/lab_quant_input_defaults.json   the standards (no amounts, no RTs), class standards and selection
                                            that msdial_lipidomics_quant_input_template prefills
  examples/lab-lipidomics/quant_input.xlsx  the example in format 1, one workbook
  examples/lab-lipidomics/quant_input_tsv/  the same, as four text files

Everything is converted from quant_utility_table.xlsx (column H) and the two EquiSPLASH texts, so the
defaults never drift from the example. tests/test_lab_quant_input.py checks that they have not.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from msdial_app import lab_quant_input  # noqa: E402

EXAMPLE = ROOT / "examples" / "lab-lipidomics"


def converted() -> dict:
    tables, problems, _, _ = lab_quant_input.converted_tables(
        EXAMPLE / "quant_utility_table.xlsx", "H", EXAMPLE / "EquiSPLASH_Pos.txt", EXAMPLE / "EquiSPLASH_Neg.txt"
    )
    if problems:
        raise SystemExit("The example does not convert: " + "; ".join(problems))
    return tables


def defaults(tables: dict) -> dict:
    columns = lab_quant_input.HEADERS["standards"]
    standards = []
    for row in tables["standards"]:
        item = dict(zip(columns, row))
        standards.append({
            "name": item["Name"], "class": item["Class"], "formula": item["Formula"], "inchikey": item["InChIKey"],
            "smiles": item["SMILES"], "pos_adduct": item["Pos adduct"], "neg_adduct": item["Neg adduct"],
        })
    return {
        "source": "examples/lab-lipidomics: quant_utility_table.xlsx (column H), EquiSPLASH_Pos.txt, EquiSPLASH_Neg.txt",
        "salt": "CH3COONH4",
        "standards": standards,
        "class_standards": tables["class_standards"],
        "selection": tables["selection"],
    }


def main() -> None:
    tables = converted()
    lab_quant_input.DEFAULTS_PATH.write_text(json.dumps(defaults(tables), indent=1) + "\n", encoding="utf-8")
    vocabulary = lab_quant_input.load_lipid_class_vocabulary()
    workbook = EXAMPLE / "quant_input.xlsx"
    folder = EXAMPLE / "quant_input_tsv"
    workbook.unlink(missing_ok=True)
    shutil.rmtree(folder, ignore_errors=True)
    lab_quant_input.write_input(workbook, "xlsx", tables, vocabulary)
    lab_quant_input.write_input(folder, "tsv", tables, vocabulary)
    for path in (workbook, folder):
        read = lab_quant_input.read_quant_input(path, vocabulary)
        if read["blockers"]:
            raise SystemExit(f"{path} does not read back: " + "; ".join(read["blockers"]))
    print(f"wrote {lab_quant_input.DEFAULTS_PATH}, {workbook} and {folder}")


if __name__ == "__main__":
    main()

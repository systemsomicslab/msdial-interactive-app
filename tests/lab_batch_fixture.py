"""A synthetic laboratory lipidomics batch: two raw folders, the libraries, the workbook, a Console
stand-in, and the files a production run and normalize leave behind. Nothing here runs MS-DIAL."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from lab_export_fixture import LBM, candidate, spot, write_candidates, write_export
from xlsx_fixture import NEGATIVE_LIBRARY, POSITIVE_LIBRARY, quant_workbook

from msdial_app import lab_lipidomics, lab_lipidomics_agent
from msdial_app.workflow import (
    ANNOTATION_CANDIDATES_CAPABILITY,
    AUTOMATIC_ALIGNMENT_RT_CORRECTION_CAPABILITY,
    AUTOMATIC_RT_LOCAL_SUPPORT_CAPABILITY,
    LCMS_QA_CAPABILITY,
    NORMALIZE_PEAK_IDS_ONLY_CAPABILITY,
)

STEMS = [("S1", "Sample"), ("S2", "Sample"), ("Blank", "Blank")]
EVERY_CAPABILITY = {
    "capability_probe": "test",
    "capabilities": sorted({
        ANNOTATION_CANDIDATES_CAPABILITY, AUTOMATIC_ALIGNMENT_RT_CORRECTION_CAPABILITY,
        AUTOMATIC_RT_LOCAL_SUPPORT_CAPABILITY, LCMS_QA_CAPABILITY, NORMALIZE_PEAK_IDS_ONLY_CAPABILITY,
    }),
}
UNIT = "pmol_per_microL_plasma"
ALIGNMENT = "AlignResult_2026_10_14_12_00_00"
PROJECT = "Project-2610141200"


def capabilities_patches(found: dict[str, Any] | None = None) -> ExitStack:
    """Every Console capability the laboratory profile asks for, wherever it is asked."""
    stack = ExitStack()
    value = found if found is not None else EVERY_CAPABILITY
    stack.enter_context(patch("msdial_app.workflow.console_capabilities", return_value=value))
    stack.enter_context(patch.object(lab_lipidomics_agent, "console_capabilities", return_value=value))
    # The batch lives in a temporary folder, which a real analysis root may not.
    real = lab_lipidomics.analysis_root_problems
    stack.enter_context(patch.object(
        lab_lipidomics_agent, "analysis_root_problems", side_effect=lambda root: real(root, temporary=[])
    ))
    return stack


class Batch:
    def __init__(self, root: Path) -> None:
        self.root = root
        data = root / "data"
        self.pos = data / "pos"
        self.neg = data / "neg"
        for folder, label in ((self.pos, "Pos"), (self.neg, "Neg")):
            folder.mkdir(parents=True)
            for stem, _ in STEMS:
                (folder / f"{stem}_{label}.mzML").write_text("", encoding="ascii")
        self.is_pos = data / "20200121_MsdialTxtDB_Pos_EquiSPLASH.txt"
        self.is_neg = data / "20200121_MsdialTxtDB_Neg_EquiSPLASH.txt"
        self.is_pos.write_bytes(POSITIVE_LIBRARY.encode("ascii"))
        self.is_neg.write_bytes(NEGATIVE_LIBRARY.encode("ascii"))
        self.workbook = quant_workbook(data / "quant_utility_table.xlsx")
        self.lbm = root / "lib" / "Msp_lab_conventional.lbm2"
        self.lbm.parent.mkdir()
        self.lbm.write_bytes(b"a laboratory LBM2 stand-in")
        self.console = root / "console" / "MSDIALCUI.exe"
        self.console.parent.mkdir()
        self.console.write_bytes(b"not a console")
        self.analysis = root / "analysis"

    def request(self, **extra: Any) -> dict[str, Any]:
        request = {
            "pos_path": str(self.pos), "neg_path": str(self.neg), "lbm_path": str(self.lbm),
            "solvent_type": "酢酸アンモニウム", "retention_time_tolerance": 1.5,
            "is_text_pos": str(self.is_pos), "is_text_neg": str(self.is_neg),
            "quant_table_path": str(self.workbook), "amount_column": "H",
            "analysis_root": str(self.analysis), "analysis_purpose": "SPF versus GF plasma lipidome",
            "console_path": str(self.console), "raw_location": "copy",
        }
        request.update(extra)
        return request

    def contract(self, **extra: Any) -> str:
        """Check, confirm the proposed sample table with the classes corrected, and write the contract."""
        check = lab_lipidomics_agent.check_inputs(self.request(**extra))
        assert check["ready_for_contract"], (check["questions"], check["blockers"])
        written = lab_lipidomics_agent.write_contract({
            **self.request(**extra), "check_digest": check["check_digest"], "sample_table_confirmed": True,
            "sample_table": [
                {"stem": "S1", "class_id": "SPF"}, {"stem": "S2", "class_id": "GF"},
                {"stem": "Blank", "class_id": "Blank", "file_type": "Blank"},
            ],
            "confirmed_by": "the analyst", "confirmed": True,
        })
        assert written["written"], written
        return written["contract_path"]


# The positive run: PC d7 by the LBM library (tier A), LPC d7 by its text row (B), Cer d7 only by
# m/z and RT (D), CE d7 by the LBM library; then sample lipids. Heights are whole numbers, as the
# export writes them.
POSITIVE_SPOTS = [
    dict(alignment_id=1, mz=753.6134, rt=9.52, name="PC 15:0_18:1(d7)", adduct="[M+H]+", ontology="PC",
         method=LBM, cells=[30000, 28000, 29000]),
    dict(alignment_id=2, mz=529.3993, rt=6.30, name="LPC 18:1(d7)", adduct="[M+H]+", ontology="LPC",
         method="internal_standards_pos", cells=[20000, 21000, 19000]),
    dict(alignment_id=3, mz=531.5477, rt=9.36, name="Unknown", adduct="[M+H]+", cells=[9000, 8000, 7000]),
    dict(alignment_id=4, mz=675.6779, rt=14.90, name="CE 18:1(d7)", adduct="[M+NH4]+", ontology="CE",
         method=LBM, cells=[5000, 6000, 4000]),
    dict(alignment_id=10, mz=496.3398, rt=5.10, name="LPC 16:0", adduct="[M+H]+", ontology="LPC",
         method=LBM, cells=[40000, 10000, 0]),
    dict(alignment_id=11, mz=700.5511, rt=11.0, name="HexCer 18:1;O2/16:0", adduct="[M+H]+",
         ontology="HexCer_NS", method=LBM, cells=[3000, 2500, 0]),
    dict(alignment_id=12, mz=666.6200, rt=15.2, name="CE 18:2", adduct="[M+NH4]+", ontology="CE",
         method=LBM, cells=[60000, 50000, 100]),
    dict(alignment_id=13, mz=800.6000, rt=12.0, name="PC 38:4", adduct="[M+H]+", ontology="PC",
         method=LBM, cells=[1000, 1200, 0]),
]
NEGATIVE_SPOTS = [
    dict(alignment_id=101, mz=811.6199, rt=9.48, name="PC 15:0_18:1(d7)", adduct="[M+CH3COO]-", ontology="PC",
         method="internal_standards_neg", cells=[31000, 29000, 30000]),
    dict(alignment_id=102, mz=587.4059, rt=6.35, name="LPC 18:1(d7)", adduct="[M+CH3COO]-", ontology="LPC",
         method="internal_standards_neg", cells=[22000, 20000, 21000]),
    dict(alignment_id=103, mz=589.5542, rt=9.30, name="Cer 18:1;O2/15:0(d7)", adduct="[M+CH3COO]-",
         ontology="Cer_NS", method="internal_standards_neg", cells=[8000, 9000, 8500]),
    dict(alignment_id=104, mz=286.2831, rt=6.95, name="FA 18:0(d3)", adduct="[M-H]-", ontology="FA",
         method="internal_standards_neg", cells=[12000, 11000, 0]),
    dict(alignment_id=110, mz=818.6000, rt=10.2, name="PC 16:0_18:1", adduct="[M+CH3COO]-", ontology="PC",
         method=LBM, cells=[90000, 80000, 50]),
    dict(alignment_id=111, mz=596.5000, rt=9.9, name="Cer 18:1;O2/16:0", adduct="[M+CH3COO]-",
         ontology="Cer_NS", method=LBM, cells=[7000, 6500, 0]),
    dict(alignment_id=112, mz=283.2643, rt=6.80, name="FA 18:1", adduct="[M-H]-", ontology="FA",
         method=LBM, cells=[15000, 14000, 0]),
    dict(alignment_id=113, mz=700.0000, rt=13.0, name="AHexCS 18:1", adduct="[M+CH3COO]-", ontology="AHexCS",
         method=LBM, cells=[2000, 2100, 0]),
]
VS2 = {"PC": "PC", "LPC": "LPC", "CE": "CE", "HexCer_NS": "HexCer-NS", "Cer_NS": "Cer-NS", "FA": "FA",
       "AHexCS": "AHexCS"}


def _samples(label: str) -> list[tuple[str, str, str]]:
    classes = {"S1": "SPF", "S2": "GF", "Blank": "Blank"}
    return [(f"{stem}_{label}", classes[stem], kind) for stem, kind in STEMS]


def guided_answers(contract_path: str, polarity: str) -> str:
    """guided-answers.json as the backend writes it beside a run planned from the contract."""
    return json.dumps({
        "answers": {"lab_lipidomics_contract_path": contract_path},
        "lab_lipidomics": {"contract_path": contract_path, "polarity": polarity,
                           "contract_sha256": lab_lipidomics.file_sha256(contract_path)},
    })


def run_spots(polarity: str, extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return (POSITIVE_SPOTS if polarity == "Positive" else NEGATIVE_SPOTS) + list(extra or [])


def write_run(
    batch: Batch, contract_path: str, polarity: str, *, salt: str = "CH3COONH4",
    extra_spots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """What a production run of the contract leaves in its output folder, and the job that made it."""
    label = "Pos" if polarity == "Positive" else "Neg"
    output = batch.analysis / label.lower()
    output.mkdir(parents=True, exist_ok=True)
    spots = run_spots(polarity, extra_spots)
    height = write_export(
        output / f"{ALIGNMENT}_Height.txt",
        [spot(**{**item, "unit": "Height"}) for item in spots], _samples(label),
    )
    rows = []
    for item in spots:
        if item["name"] == "Unknown" or not item.get("method"):
            continue
        source = "TextDB" if item["method"].startswith("internal_standards") else "MspDB"
        rows.append(candidate(item["alignment_id"], item["name"], annotator=item["method"], source=source,
                              ontology=item.get("ontology", "")))
    # Spot 3 has no candidate at all: the positive Cer d7 is found by m/z and RT alone (tier D).
    candidates = write_candidates(output / f"{ALIGNMENT}.mdcandidate.tsv", rows)
    project = output / f"{PROJECT}.mddata"
    project.write_bytes(b"project")
    (output / f"{PROJECT}_Loaded.msp2.dbs").write_bytes(b"library copy")
    (output / "method.txt").write_text(f"Ion mode: {polarity}\nSolvent type: {salt}\n", encoding="utf-8")
    (output / "guided-answers.json").write_text(guided_answers(contract_path, polarity), encoding="utf-8")
    return {
        "id": f"run-{label.lower()}", "kind": "run", "status": "completed", "logs": [],
        "preparation": {"run_directory": str(output), "export_folder_path": str(output)},
        "artifacts": {"msdial": [], "other": [str(height), str(candidates), str(project)], "qa": [], "mztab": []},
    }


def normalize_outputs(
    plan: dict[str, Any], polarity: str, *, wrong_cell: int | None = None, stored_names: dict[int, str] | None = None,
    extra_spots: list[dict[str, Any]] | None = None, factors: dict[str, float] | None = None,
) -> tuple[str, str, str]:
    """What MSDIALCUI normalize writes for this plan's table, and the report it prints.

    The values follow MS-DIAL's rule (amount x height / standard height, the class's own standard or
    Any others, NOT QUANTIFIED where the standard did not resolve); wrong_cell perturbs one spot.
    factors (by sample name) multiply each file's values, standards' included, as -d does.
    The report's mismatches come from the stand-in's own comparison of each row's standard with the
    spot's stored name, as the Console makes it (NormalizationProcess.NameAliases), never from the
    plan's prediction; stored_names gives a spot another stored name than its export name.
    """
    label = "Pos" if polarity == "Positive" else "Neg"
    spots = run_spots(polarity, extra_spots)
    multipliers = [float((factors or {}).get(name, 1.0)) for name, _, _ in _samples(label)]
    rows = plan["standards_table"]
    own = {}
    for row in rows:
        own.setdefault(row["TargetClass"], row)
    standards = {row["PeakID"]: row for row in rows if row["PeakID"] >= 0}
    by_id = {item["alignment_id"]: item for item in spots}
    normalized = []
    assigned = {row["PeakID"]: row["StandardName"] for row in rows if row["AssignName"] and row["PeakID"] >= 0}
    for item in spots:
        entry = dict(item)
        if item["alignment_id"] in assigned:
            entry["name"] = assigned[item["alignment_id"]]
            entry["method"] = "internal standard assignment"
        if item["alignment_id"] in standards:
            amount = float(standards[item["alignment_id"]]["Concentration"])
            normalized.append(spot(**{**entry, "unit": UNIT, "cells": [repr(amount * f) for f in multipliers]}))
            continue
        klass = VS2.get(item.get("ontology", ""), "Unknown") if item["name"] != "Unknown" else "Unknown"
        standard = own.get(klass) if klass in own else own["Any others"]
        if standard["PeakID"] < 0:
            comment = (f"NOT QUANTIFIED: the {standard['TargetClass']} standard {standard['StandardName']} did not "
                       "resolve in this alignment")
            normalized.append(spot(**{**entry, "comment": comment, "cells": ["", "", ""]}))
            continue
        base = by_id[standard["PeakID"]]["cells"]
        amount = float(standard["Concentration"])
        cells = ["" if b <= 0 else repr(amount * h / b * f) for h, b, f in zip(item["cells"], base, multipliers)]
        if wrong_cell == item["alignment_id"]:
            cells[0] = repr(float(cells[0]) * 2)
        normalized.append(spot(**{**entry, "unit": UNIT, "cells": cells}))
    report = []
    for row in rows:
        if row["PeakID"] >= 0 and row["StandardName"] not in {line.split(" via ")[0].strip() for line in report}:
            covers = [item["TargetClass"] for item in rows if item["StandardName"] == row["StandardName"]]
            report.append(f"  {row['StandardName']} via alignment ID {row['PeakID']}, concentration {row['Concentration']}")
            report.append(f"      covers {len(covers)} class(es): {', '.join(covers)}")
    unresolved: dict[str, list[str]] = {}
    for row in rows:
        if row["PeakID"] < 0:
            unresolved.setdefault(row["StandardName"], []).append(row["TargetClass"])
    for name, classes in unresolved.items():
        report.append(f"  UNRESOLVED {name} -- named for {len(classes)} class(es): {', '.join(classes)}")
    mismatched = []
    for row in rows:
        if row["PeakID"] < 0:
            continue
        stored = (stored_names or {}).get(row["PeakID"], by_id[row["PeakID"]]["name"])
        stored = "" if stored == "Unknown" else stored
        # One item per table row, as the Console adds them.
        if row["StandardName"].casefold() not in lab_lipidomics.console_name_aliases(stored):
            mismatched.append(f"{row['StandardName']} (alignment ID {row['PeakID']} is annotated '{stored}')")
    if mismatched:
        report.append(
            f"WARNING: {len(mismatched)} standard(s) were taken from an alignment ID that holds a different compound: "
            + "; ".join(mismatched) + "."
        )
    report.append(f"Normalized unit: {UNIT}")
    report.append(f"Dilution factor applied: {'-d' in plan['command']}")
    folder = Path(plan["paths"]["output_folder"])
    height_path = write_export(folder / f"{ALIGNMENT}_Height.txt",
                               [spot(**{**item, "unit": "Height"}) for item in spots], _samples(label))
    normalized_path = write_export(folder / f"{ALIGNMENT}_NormalizedHeight.txt", normalized, _samples(label))
    report.extend([str(height_path), str(normalized_path)])
    return str(height_path), str(normalized_path), "\n".join(report)

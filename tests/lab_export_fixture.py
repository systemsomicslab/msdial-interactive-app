"""Synthetic MS-DIAL alignment exports and candidate tables for the laboratory lipidomics tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

# LcmsMetadataAccessor's 40 columns, as the current Console writes them.
METADATA_COLUMNS = [
    "Alignment ID", "Average Rt(min)", "Average Mz", "Metabolite name", "Adduct type",
    "Post curation result", "Fill %", "MS/MS assigned", "Reference RT", "Reference m/z", "Formula",
    "Ontology", "INCHIKEY", "SMILES", "Annotation tag (VS1.0)", "RT matched", "m/z matched",
    "MS/MS matched", "Comment", "Manually modified for quantification",
    "Manually modified for annotation", "Isotope tracking parent ID", "Isotope tracking weight number",
    "RT similarity", "m/z similarity", "Simple dot product", "Weighted dot product",
    "Reverse dot product", "Matched peaks count", "Matched peaks percentage", "Total score",
    "S/N average", "Spectrum reference file name", "MS1 isotopic spectrum", "MS/MS spectrum",
    "Measured terms", "Evidence source", "Candidates found", "Candidates above threshold",
    "Candidates reference matched",
]
CANDIDATE_COLUMNS = [
    "alignment_master_id", "alignment_local_id", "parent_alignment_id", "candidate_rank",
    "candidate_count", "is_representative", "annotator_id", "database_id", "source", "priority",
    "library_id", "name", "formula", "ontology", "inchikey", "smiles", "reference_mz",
    "reference_rt_min", "reference_adduct", "annotation_tag_vs1", "is_reference_matched",
    "is_annotation_suggested", "is_precursor_mz_match", "is_spectrum_match",
    "is_spectrum_comparison_performed", "total_score", "mz_similarity", "rt_similarity",
    "ri_similarity", "ccs_similarity", "isotope_similarity", "simple_dot_product",
    "weighted_dot_product", "reverse_dot_product", "matched_peaks_count", "matched_peaks_percentage",
]
SAMPLES = [("S1_Pos", "SPF", "Sample"), ("S2_Pos", "GF", "Sample"), ("Blank_Pos", "Blank", "Blank")]
LBM = "LbmDB: synthetic-lipids"


def spot(
    alignment_id: int, mz: float, rt: float, name: str = "Unknown", *, adduct: str = "[M+H]+",
    ontology: str = "null", method: str = "", unit: str = "Intensity", cells: list[Any] | None = None,
    fill: float = 1.0, isotope: int = 0, formula: str = "null", comment: str | None = None,
) -> dict[str, Any]:
    if comment is None:
        comment = f"Normalized unit: {unit}" + (f"; Annotation method: {method}" if method else "")
    return {
        "Alignment ID": str(alignment_id), "Average Rt(min)": f"{rt:.3f}", "Average Mz": f"{mz:.5f}",
        "Metabolite name": name, "Adduct type": adduct, "Fill %": f"{fill:.2f}", "Formula": formula,
        "Ontology": ontology, "Comment": comment, "Isotope tracking weight number": str(isotope),
        "cells": [str(value) for value in (cells or [1000, 1000, 1000])],
    }


def export_text(spots: list[dict[str, Any]], samples=SAMPLES) -> str:
    width = len(METADATA_COLUMNS)

    def label_row(label: str, values: list[str]) -> list[str]:
        return [""] * (width - 1) + [label] + values + ["NA"] * 4

    lines = [
        label_row("Class", [group for _, group, _ in samples]),
        label_row("File type", [kind for _, _, kind in samples]),
        label_row("Injection order", [str(index + 1) for index in range(len(samples))]),
        [""] * (width - 1) + ["Batch ID"] + ["1"] * len(samples) + ["Average", "Average", "Stdev", "Stdev"],
        METADATA_COLUMNS + [name for name, _, _ in samples] + ["A", "B", "A", "B"],
    ]
    for item in spots:
        row = [item.get(column, "") for column in METADATA_COLUMNS]
        lines.append(row + item["cells"] + ["0"] * 4)
    return "\r\n".join("\t".join(cells) for cells in lines) + "\r\n"


def write_export(path: Path, spots: list[dict[str, Any]], samples=SAMPLES) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(export_text(spots, samples), encoding="ascii", newline="")
    return path


def candidate(
    alignment_id: int, name: str, *, annotator: str = LBM, representative: bool = True,
    matched: bool = True, suggested: bool = False, source: str = "MspDB", ontology: str = "",
) -> dict[str, str]:
    return {
        "alignment_master_id": str(alignment_id), "is_representative": "true" if representative else "false",
        "annotator_id": annotator, "source": source, "name": name, "ontology": ontology,
        "is_reference_matched": "true" if matched else "false",
        "is_annotation_suggested": "true" if suggested else "false",
    }


def write_candidates(path: Path, rows: list[dict[str, str]]) -> Path:
    lines = ["\t".join(CANDIDATE_COLUMNS)]
    for row in rows:
        lines.append("\t".join(row.get(column, "") for column in CANDIDATE_COLUMNS))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    return path

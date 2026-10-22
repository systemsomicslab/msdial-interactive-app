"""Laboratory LC-MS metabolomics: the annotation libraries, normalization by one internal standard, and
the merge of the polarities by InChIKey.

A hydrophilic metabolomics batch is annotated by an ordered list of libraries, each with its own
retention-time use and score thresholds (presets below). Each feature is then divided by one internal
standard's height in its injection and multiplied by that standard's mean height over the Sample and QC
injections. Last, the rows of one or both polarities become one metabolome, with one row per compound
(the InChIKey's first block). lab_lcms_metabolomics_agent walks a person through these steps; this
module reads, computes and writes, and decides nothing on its own.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable

from .lab_lipidomics import (
    NEGATIVE,
    POLARITIES,
    POSITIVE,
    LabInputError,
    _is_zero_height,
    annotation_method,
    file_sha256,
    parse_adduct,
    parse_number,
    read_candidate_table,
    representative_candidate,
)
from .lipidome_merge import MergeRefused, compare_sample_order, describe_export, net_double_tostring, strip_polarity
from .workflow import file_identity

# ---------------------------------------------------------------------------------------------
# Annotation: an ordered list of libraries
# ---------------------------------------------------------------------------------------------

LIBRARY_KINDS = ("msp", "lbm")
LIBRARY_SUFFIXES = {"msp": (".msp",), "lbm": (".lbm2",)}
RETENTION_TIME_USES = ("none", "use")
THRESHOLD_KEYS = (
    "ms2_tolerance", "weighted_dot_product_cutoff", "simple_dot_product_cutoff", "reverse_dot_product_cutoff",
    "matched_peaks_percentage_cutoff", "minimum_spectrum_match",
)
# standard is a guided run's own MSP row (agent_workflow._msp_row), at the method's MS/MS tolerance;
# strict and mild are the repository campaign's two MSP tiers (annotation_pipeline.tiered_lcms_annotation).
THRESHOLDS = {
    "standard": {
        "weighted_dot_product_cutoff": 0.6, "simple_dot_product_cutoff": 0.6, "reverse_dot_product_cutoff": 0.8,
        "matched_peaks_percentage_cutoff": 0.1, "minimum_spectrum_match": 3,
    },
    "strict": {
        "ms2_tolerance": 0.05, "weighted_dot_product_cutoff": 0.6, "simple_dot_product_cutoff": 0.6,
        "reverse_dot_product_cutoff": 0.8, "matched_peaks_percentage_cutoff": 0.0, "minimum_spectrum_match": 3,
    },
    "mild": {
        "ms2_tolerance": 0.25, "weighted_dot_product_cutoff": 0.5, "simple_dot_product_cutoff": 0.5,
        "reverse_dot_product_cutoff": 0.5, "matched_peaks_percentage_cutoff": 0.0, "minimum_spectrum_match": 1,
    },
}
# Highest priority first. A slot names the file the person gives; one file may fill two entries (the
# campaign searches its MSP twice, strictly and mildly).
PRESETS: dict[str, list[dict[str, Any]]] = {
    "single_msp": [
        {"id": "msp", "slot": "msp", "kind": "msp", "thresholds": "standard", "retention_time": "none",
         "label": "MSP"},
    ],
    "campaign": [
        {"id": "lbm", "slot": "lbm", "kind": "lbm", "retention_time": "none", "label": "LBM2 lipids"},
        {"id": "msp_strict", "slot": "msp", "kind": "msp", "thresholds": "strict", "retention_time": "none",
         "label": "MSP strict"},
        {"id": "msp_mild", "slot": "msp", "kind": "msp", "thresholds": "mild", "retention_time": "none",
         "label": "MSP mild"},
        {"id": "in_silico", "slot": "in_silico", "kind": "msp", "thresholds": "mild", "retention_time": "none",
         "label": "in-silico MSP", "optional": True},
    ],
    "lab_rt_tiers": [
        {"id": "in_house", "slot": "in_house", "kind": "msp", "thresholds": "standard", "retention_time": "use",
         "rt_tolerance": 0.5, "label": "in-house RT and MS/MS"},
        {"id": "predicted_rt", "slot": "predicted_rt", "kind": "msp", "thresholds": "standard",
         "retention_time": "use", "rt_tolerance": 2.0, "label": "predicted RT"},
        {"id": "no_rt", "slot": "no_rt", "kind": "msp", "thresholds": "standard", "retention_time": "none",
         "label": "MS/MS without RT"},
    ],
}
PRESET_PURPOSES = {
    "single_msp": "one MSP library, no retention time: what most users and the reanalysis agent run",
    "campaign": "the reanalysis project's tiers: the LBM2 for the few lipids, one MSP searched strictly and "
                "mildly, and optionally an in-silico MSP, all without retention time",
    "lab_rt_tiers": "the laboratory's own tiers: in-house standards (RT 0.5 min), predicted RTs (RT 2.0 min), "
                    "and MS/MS without RT",
}
# The internal-standard text row is searched at priority 1, below every library.
INTERNAL_STANDARD_PRIORITY = 1
_LABEL = re.compile(r"^[ -~]{1,60}$")
_IDENTIFIER = re.compile(r"[^a-z0-9]+")
_POLARITY_KEYS = {"positive": POSITIVE, "pos": POSITIVE, "negative": NEGATIVE, "neg": NEGATIVE}


def library_identity(path: str | Path) -> dict[str, Any]:
    """A library by name, size and sha256; a large one through Interactive's cached digest."""
    target = Path(path).expanduser().resolve()
    size = target.stat().st_size
    sha256 = file_identity(target).get("sha256", "") if size >= (64 << 20) else file_sha256(target)
    return {"path": str(target), "name": target.name, "size_bytes": size, "sha256": sha256}


def _paths_by_polarity(value: Any) -> dict[str, str] | None:
    """A library's file: one path for every polarity, or a path per polarity."""
    if isinstance(value, str):
        text = value.strip()
        return {POSITIVE: text, NEGATIVE: text} if text else {}
    if isinstance(value, dict):
        paths: dict[str, str] = {}
        for key, item in value.items():
            polarity = _POLARITY_KEYS.get(str(key).strip().casefold())
            if polarity is None or not isinstance(item, str):
                return None
            if item.strip():
                paths[polarity] = item.strip()
        return paths
    return None if value not in (None, "") else {}


def read_annotation(spec: Any, polarities: Iterable[str]) -> dict[str, Any]:
    """The annotation libraries a person gave, checked, highest priority first.

    spec is {"preset": name, "libraries": {slot: path}} or {"libraries": [entry, ...]}, an entry being
    {kind, path, thresholds, retention_time, rt_tolerance, label}. A path is one file for both
    polarities or {"Positive": ..., "Negative": ...}. Nothing is hashed here (library_identity).
    """
    wanted = [polarity for polarity in POLARITIES if polarity in set(polarities)]
    blockers: list[str] = []
    warnings: list[str] = []
    missing: list[str] = []
    result: dict[str, Any] = {"preset": None, "entries": [], "blockers": blockers, "warnings": warnings,
                              "missing": missing}
    if not isinstance(spec, dict) or not spec:
        missing.append("annotation")
        return result
    preset = str(spec.get("preset") or "").strip()
    libraries = spec.get("libraries")
    entries: list[dict[str, Any]] = []
    if preset:
        if preset not in PRESETS:
            blockers.append(f"annotation preset '{preset}' is none of {', '.join(PRESETS)}.")
            return result
        result["preset"] = preset
        if libraries in (None, ""):
            libraries = {}
        if not isinstance(libraries, dict):
            blockers.append(f"annotation.libraries for the preset {preset} maps each slot to its file.")
            return result
        slots = {item["slot"] for item in PRESETS[preset]}
        for key in sorted(set(libraries) - slots):
            blockers.append(f"annotation.libraries.{key} is no slot of the preset {preset} ({', '.join(sorted(slots))}).")
        for template in PRESETS[preset]:
            paths = _paths_by_polarity(libraries.get(template["slot"]))
            if paths is None:
                blockers.append(f"annotation.libraries.{template['slot']} is a path, or a path per polarity.")
                continue
            if not paths and template.get("optional"):
                continue
            entries.append({**{key: value for key, value in template.items() if key != "optional"}, "paths": paths})
    else:
        if not isinstance(libraries, list) or not libraries:
            blockers.append("annotation names no preset and lists no libraries.")
            return result
        for number, item in enumerate(libraries, start=1):
            if not isinstance(item, dict):
                blockers.append(f"annotation.libraries[{number}] is not an object.")
                continue
            kind = str(item.get("kind") or "").strip().casefold()
            paths = _paths_by_polarity(item.get("path", item.get("paths")))
            if paths is None:
                blockers.append(f"annotation.libraries[{number}].path is a path, or a path per polarity.")
                continue
            entries.append({
                "id": f"{kind}_{str(item.get('thresholds') or 'standard').strip().casefold()}" if kind != "lbm" else "lbm",
                "slot": f"libraries[{number}]", "kind": kind,
                "thresholds": str(item.get("thresholds") or ("standard" if kind == "msp" else "")).strip().casefold(),
                "retention_time": str(item.get("retention_time") or "").strip().casefold(),
                "rt_tolerance": item.get("rt_tolerance"),
                "label": str(item.get("label") or "").strip() or f"{kind.upper()} {number}",
                "paths": paths,
            })

    count = len(entries)
    for index, entry in enumerate(entries, start=1):
        where = f"annotation library {index} ({entry.get('label') or entry['slot']})"
        if entry["kind"] not in LIBRARY_KINDS:
            blockers.append(f"{where}: kind '{entry['kind']}' is neither msp nor lbm.")
            continue
        if entry["kind"] == "msp" and entry.get("thresholds") not in THRESHOLDS:
            blockers.append(f"{where}: thresholds '{entry.get('thresholds')}' is none of {', '.join(THRESHOLDS)}.")
        if entry["retention_time"] not in RETENTION_TIME_USES:
            blockers.append(f"{where}: retention_time is 'none' (neither filtering nor scoring) or 'use'.")
        tolerance = parse_number(entry.get("rt_tolerance")) if entry.get("rt_tolerance") not in (None, "") else None
        if entry["retention_time"] == "use":
            if tolerance is None or tolerance <= 0 or not math.isfinite(tolerance):
                blockers.append(f"{where}: retention_time 'use' needs rt_tolerance, a positive number of minutes.")
                tolerance = None
        else:
            tolerance = None
        entry["rt_tolerance"] = tolerance
        if not _LABEL.match(str(entry.get("label") or "")) or "\t" in str(entry.get("label")):
            # The label reaches msp_annotator_settings.tsv, which the Console reads as ASCII.
            blockers.append(f"{where}: label is printable ASCII of at most 60 characters.")
        entry["priority"] = count - index + 1 + INTERNAL_STANDARD_PRIORITY
        entry["annotator_id"] = f"lib{index}_" + _IDENTIFIER.sub("_", str(entry["id"]).casefold()).strip("_")
        for polarity in wanted:
            path = entry["paths"].get(polarity)
            if not path:
                missing.append(f"{entry['slot']} ({polarity.lower()})")
                continue
            target = Path(path).expanduser()
            if not target.is_file():
                blockers.append(f"{where}: {target} is not a file.")
            elif target.suffix.casefold() not in LIBRARY_SUFFIXES[entry["kind"]]:
                blockers.append(f"{where}: {target.name} is not an {' or '.join(LIBRARY_SUFFIXES[entry['kind']])} library.")
            elif target.stat().st_size == 0:
                # The Console skips an empty library without a word.
                blockers.append(f"{where}: {target.name} is empty.")
        entry["paths"] = {polarity: entry["paths"][polarity] for polarity in wanted if entry["paths"].get(polarity)}
    lbm = [entry for entry in entries if entry["kind"] == "lbm"]
    if len(lbm) > 1:
        blockers.append("The Console reads one LBM2 per run; the annotation lists more than one.")
    for polarity in wanted:
        by_path: dict[str, list[str]] = {}
        for entry in entries:
            path = entry["paths"].get(polarity)
            if path:
                by_path.setdefault(str(Path(path).expanduser().resolve()).casefold(), []).append(entry)
        for same in by_path.values():
            settings = {(entry.get("thresholds"), entry["retention_time"], entry["rt_tolerance"]) for entry in same}
            if len(same) > 1 and len(settings) < len(same):
                warnings.append(
                    f"{polarity}: {', '.join(entry['label'] for entry in same)} search one file with the same settings."
                )
    result["entries"] = entries
    return result


def annotation_summary(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The libraries as the person is shown them: no path, the settings each is searched with."""
    return [
        {
            "priority": entry["priority"], "label": entry["label"], "kind": entry["kind"],
            "annotator_id": entry["annotator_id"], "thresholds": entry.get("thresholds") or None,
            "threshold_values": THRESHOLDS.get(entry.get("thresholds") or "", {}) if entry["kind"] == "msp" else None,
            "retention_time": entry["retention_time"], "rt_tolerance": entry["rt_tolerance"],
            "files": {polarity: Path(path).name for polarity, path in entry["paths"].items()},
        }
        for entry in entries
    ]


def annotator_settings(entries: Iterable[dict[str, Any]], polarity: str) -> dict[str, Any]:
    """One polarity's annotators as the guided plan takes them (libraries.annotators).

    Every MSP row is rt_fixed: its retention-time use was decided for that library, so the run-wide
    answer leaves it as it is (agent_workflow._apply_retention_time_use).
    """
    rows: list[dict[str, Any]] = []
    for entry in entries:
        path = entry["paths"].get(polarity)
        if not path:
            continue
        rows.append({
            "kind": entry["kind"], "annotator_id": entry["annotator_id"], "label": entry["label"],
            "path": str(Path(path).expanduser().resolve()), "priority": entry["priority"],
            "thresholds": entry.get("thresholds") or "", "retention_time": entry["retention_time"],
            "rt_tolerance": entry["rt_tolerance"],
        })
    return {"annotators": rows}


def apply_annotators(state: dict[str, Any], annotators: Iterable[dict[str, Any]]) -> None:
    """Write libraries.annotators into the workflow state: MSP rows, the LBM2 and their provenance."""
    state["msp_annotators"] = []
    provenance: list[dict[str, Any]] = []
    for item in annotators:
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        use_rt = str(item.get("retention_time") or "none") == "use"
        if item.get("kind") == "lbm":
            state["lbm_path"] = path
            state["lbm_annotator"] = {**dict(state.get("lbm_annotator") or {}), "lbm_file_path": path,
                                      "priority": int(item.get("priority") or 1)}
        else:
            row: dict[str, Any] = {
                "annotator_id": str(item.get("annotator_id") or f"msp_annotator_{len(state['msp_annotators']) + 1}"),
                "msp_file_path": path,
                "priority": int(item.get("priority") or 1),
                "target_omics": "Metabolomics",
                "evidence_tier": str(item.get("label") or ""),
                "use_rt_scoring": use_rt,
                "use_rt_filtering": use_rt,
                # Decided for this library: the run-wide retention-time answer does not reach it.
                "rt_fixed": True,
                **THRESHOLDS.get(str(item.get("thresholds") or "standard"), THRESHOLDS["standard"]),
            }
            if use_rt and item.get("rt_tolerance") not in (None, ""):
                row["rt_tolerance"] = float(item["rt_tolerance"])
            state["msp_annotators"].append(row)
        provenance.append({"path": path, "version": "", "source": "", "doi": "",
                           "license": "institutional/private", "label": str(item.get("label") or "")})
    state["library_provenance"] = provenance


def lbm_retention_time(entries: Iterable[dict[str, Any]]) -> tuple[bool, float | None]:
    """The run-wide retention-time answer: the LBM2's own, the only library it still reaches."""
    for entry in entries:
        if entry["kind"] == "lbm":
            return entry["retention_time"] == "use", entry["rt_tolerance"]
    return False, None


# ---------------------------------------------------------------------------------------------
# Preferred adducts
# ---------------------------------------------------------------------------------------------

# The negative adduct each salt forms besides [M-H]-; the bicarbonate buffer forms none worth preferring.
SALT_NEGATIVE_ADDUCT = {"HCOONH4": "[M+HCOO]-", "CH3COONH4": "[M+CH3COO]-", "NH4HCO3": None}


def preferred_adducts(salt: str | None) -> list[str]:
    adducts = ["[M+H]+", "[M-H]-"]
    extra = SALT_NEGATIVE_ADDUCT.get(str(salt or ""))
    if extra:
        adducts.append(extra)
    return adducts


def adduct_identities(names: Iterable[str]) -> set[tuple[Any, ...]]:
    found = set()
    for name in names:
        try:
            found.add(parse_adduct(name).identity)
        except ValueError:
            continue
    return found


def is_preferred(adduct: str, identities: set[tuple[Any, ...]]) -> bool:
    """[M+FA-H]- is [M+HCOO]-, [M+Hac-H]- is [M+CH3COO]-: an adduct is compared as the ion it names."""
    try:
        return parse_adduct(adduct).identity in identities
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------
# Normalization by one internal standard
# ---------------------------------------------------------------------------------------------

NORMALIZATION_FILE_TYPES = ("sample", "qc")


def _rows_with_cells(path: str | Path) -> dict[str, Any]:
    try:
        export = describe_export(path)
    except MergeRefused as error:
        raise LabInputError(str(error)) from None
    names = export["header"][4]
    try:
        id_column = names.index("Alignment ID")
    except ValueError:
        raise LabInputError(f"{path} has no Alignment ID column.") from None
    export["id_column"] = id_column
    export["count"] = len(export["sample_names"])
    return export


def normalize_heights(
    height_export: str | Path, standard_id: str, factors: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Every feature's height divided by the standard's in its injection, times the standard's mean.

    normalized[s][i] = height[s][i] / height[IS][i] x mean(height[IS] over the Sample and QC injections),
    times the injection's dilution factor. A Sample or QC whose standard has no height blocks; a Blank or
    Standard injection without it is left as its raw height and reported. Nothing is written.
    """
    export = _rows_with_cells(height_export)
    start = export["first_sample_column"]
    count = export["count"]
    names = export["sample_names"]
    kinds = [kind.casefold() for kind in export["file_types"]]
    standard_row = next(
        (row for row in export["rows"] if len(row) > export["id_column"] and row[export["id_column"]].strip() == str(standard_id)),
        None,
    )
    problems: list[str] = []
    if standard_row is None:
        raise LabInputError(f"Alignment ID {standard_id} is not in {Path(height_export).name}.")
    cells = [standard_row[start + offset].strip() if start + offset < len(standard_row) else "" for offset in range(count)]
    standard = [0.0 if _is_zero_height(cell) else float(parse_number(cell)) for cell in cells]
    counted = [index for index, kind in enumerate(kinds) if kind in NORMALIZATION_FILE_TYPES]
    if not counted:
        problems.append("The export lists no Sample or QC injection, so the standard has no mean to scale by.")
    zero_required = [names[index] for index in counted if standard[index] <= 0]
    if zero_required:
        problems.append(
            f"The standard (alignment ID {standard_id}) has no height in {', '.join(zero_required)}; a Sample or "
            "QC divided by nothing is not normalized."
        )
    raw_kept = [names[index] for index, kind in enumerate(kinds) if kind not in NORMALIZATION_FILE_TYPES and standard[index] <= 0]
    mean = sum(standard[index] for index in counted) / len(counted) if counted else 0.0
    multipliers = [float((factors or {}).get(name, 1.0) or 1.0) for name in names]
    values: dict[str, list[float | None]] = {}
    if not problems:
        for row in export["rows"]:
            if len(row) <= export["id_column"] or not row[export["id_column"]].strip():
                continue
            line: list[float | None] = []
            for offset in range(count):
                text = row[start + offset].strip() if start + offset < len(row) else ""
                height = parse_number(text)
                if height is None:
                    line.append(None)
                elif standard[offset] <= 0:
                    line.append(height)
                else:
                    line.append(height / standard[offset] * mean * multipliers[offset])
            values[row[export["id_column"]].strip()] = line
    return {
        "height_export": str(Path(height_export).resolve()),
        "height_export_sha256": file_sha256(height_export),
        "standard_id": str(standard_id),
        "standard_heights": dict(zip(names, standard)),
        "standard_mean": mean,
        "mean_over": [names[index] for index in counted],
        "raw_height_kept": raw_kept,
        "dilution_factors": {name: factor for name, factor in zip(names, multipliers) if factor != 1.0},
        "problems": problems,
        "values": values,
        "export": export,
    }


def normalized_text(normalized: dict[str, Any]) -> str:
    """The normalized matrix in MS-DIAL's export layout: the header lines and metadata as they were.

    Only the sample cells change. MS-DIAL's per-class Average and Stdev columns were computed from
    heights, so they are left empty rather than left wrong.
    """
    export = normalized["export"]
    start = export["first_sample_column"]
    count = export["count"]
    lines = ["\t".join(row) for row in export["header"]]
    for row in export["rows"]:
        identifier = row[export["id_column"]].strip() if len(row) > export["id_column"] else ""
        if identifier not in normalized["values"]:
            lines.append("\t".join(row))
            continue
        out = list(row)
        for offset, value in enumerate(normalized["values"][identifier]):
            if start + offset < len(out):
                out[start + offset] = "" if value is None else net_double_tostring(value)
        for index in range(start + count, len(out)):
            out[index] = ""
        lines.append("\t".join(out))
    return "\r\n".join(lines) + "\r\n"


def verify_normalized(path: str | Path, normalized: dict[str, Any]) -> dict[str, Any]:
    """Every cell of the written matrix recomputed from the Height export, and compared."""
    written = _rows_with_cells(path)
    start = written["first_sample_column"]
    failed: list[dict[str, Any]] = []
    checked = 0
    if written["sample_names"] != normalized["export"]["sample_names"]:
        return {"passed": False, "checked": 0, "failed": [],
                "problems": ["The written matrix lists other samples than the Height export."]}
    for row in written["rows"]:
        identifier = row[written["id_column"]].strip() if len(row) > written["id_column"] else ""
        expected = normalized["values"].get(identifier)
        if expected is None:
            continue
        for offset, value in enumerate(expected):
            text = row[start + offset].strip() if start + offset < len(row) else ""
            got = parse_number(text)
            checked += 1
            if value is None and text == "":
                continue
            if value is None or got is None or not math.isclose(got, value, rel_tol=1e-12, abs_tol=1e-9):
                failed.append({"alignment_id": identifier, "sample": written["sample_names"][offset],
                               "written": text, "expected": value})
    missing = sorted(set(normalized["values"]) - {
        row[written["id_column"]].strip() for row in written["rows"] if len(row) > written["id_column"]
    })
    problems = []
    if missing:
        problems.append(f"{len(missing)} feature(s) of the Height export are missing from the written matrix.")
    if failed:
        problems.append(f"{len(failed)} cell(s) do not follow the formula.")
    return {"passed": not problems, "checked": checked, "failed": failed[:50], "problems": problems}


# ---------------------------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------------------------

_INCHIKEY = re.compile(r"^([A-Z]{14})(?:-[A-Z]{10}-[A-Z])?$")
UNANNOTATED_NAMES = frozenset({"", "unknown", "null"})
MANUAL_EXCLUSION_COMMENT_PREFIX = "x"
MERGE_FILE_TYPES = ("Sample", "QC")
METABOLOME_NAME = "metabolome.csv"
REMOVED_NAME = "metabolome_removed.tsv"
SAMPLES_NAME = "metabolome_samples.csv"
MERGE_REPORT_NAME = "merge-report.json"
DEFAULT_MERGE_OPTIONS = {"reference_matched_only": True, "preferred_adducts_only": False}
REMOVED_COLUMNS = (
    "Polarity", "Alignment ID", "Metabolite name", "Adduct type", "InChIKey", "Reason",
    "Kept polarity", "Kept alignment ID", "Kept name", "Kept adduct",
)


def inchikey_first_block(value: str) -> str:
    """The skeleton block of an InChIKey, or "" when the cell holds no InChIKey."""
    match = _INCHIKEY.match(str(value or "").strip())
    return match.group(1) if match else ""


def _column(names: list[str], title: str) -> int:
    try:
        return names.index(title)
    except ValueError:
        return -1


def _read_polarity(item: dict[str, Any]) -> dict[str, Any]:
    export = _rows_with_cells(item["normalized"])
    names = export["header"][4]
    columns = {key: _column(names, title) for key, title in (
        ("name", "Metabolite name"), ("adduct", "Adduct type"), ("inchikey", "INCHIKEY"),
        ("comment", "Comment"), ("total_score", "Total score"),
    )}
    missing = [title for key, title in (("name", "Metabolite name"), ("adduct", "Adduct type"),
                                         ("inchikey", "INCHIKEY"), ("comment", "Comment")) if columns[key] < 0]
    if missing:
        raise LabInputError(f"{item['normalized']} has no {', '.join(missing)} column(s).")
    candidates = read_candidate_table(item["candidate_table"])
    start = export["first_sample_column"]
    counted = [index for index, kind in enumerate(export["file_types"])
               if kind.strip().casefold() in {value.casefold() for value in MERGE_FILE_TYPES}]
    rows = []
    for raw in export["rows"]:
        identifier = raw[export["id_column"]].strip() if len(raw) > export["id_column"] else ""
        if not identifier:
            continue

        def cell(key: str) -> str:
            index = columns[key]
            return raw[index].strip() if 0 <= index < len(raw) else ""

        sample_cells = [raw[start + offset].strip() if start + offset < len(raw) else "" for offset in range(export["count"])]
        values = [parse_number(sample_cells[index]) or 0.0 for index in counted]
        representative = representative_candidate(candidates, identifier) or {}
        rows.append({
            "polarity": item["polarity"], "id": identifier, "raw": raw, "name": cell("name"), "adduct": cell("adduct"),
            "inchikey": cell("inchikey"), "comment": raw[columns["comment"]] if columns["comment"] < len(raw) else "",
            "method": annotation_method(cell("comment")),
            "total_score": parse_number(cell("total_score")) or 0.0,
            "matched": bool(representative.get("is_reference_matched")),
            "priority": int(parse_number(representative.get("priority", "")) or 0),
            "mean": sum(values) / len(values) if values else 0.0,
            "samples": sample_cells,
        })
    return {"export": export, "rows": rows, "candidates": candidates}


def _id_key(value: str) -> tuple[int, str]:
    number = parse_number(value)
    return (int(number), value) if number is not None else (1 << 62, value)


def merge_metabolome(
    polarities: list[dict[str, Any]],
    folder: str | Path,
    *,
    salt: str | None,
    reference_matched_only: bool = True,
    preferred_adducts_only: bool = False,
    labels: dict[str, str] | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """One metabolome from the normalized matrices of one or both polarities.

    polarities: [{polarity, normalized, candidate_table, internal_standard_ids, internal_standard_annotator}].
    Rows dropped first: unannotated, an internal standard, a Comment that starts with x; then, as the
    options say, rows not reference matched and adducts not preferred. Rows sharing an InChIKey's first
    block are one compound, within and across polarities; one is kept, by: a preferred adduct, matched on
    MS/MS, the larger mean normalized height over Sample and QC, then the higher library priority, the higher
    total score, positive before negative, and the lower alignment ID. A row without an InChIKey is kept as
    its own. labels maps an annotator ID to the library label the person knows.
    """
    folder = Path(folder)
    identities = adduct_identities(preferred_adducts(salt))
    read = {item["polarity"]: {**_read_polarity(item), "input": item} for item in polarities}
    order = [polarity for polarity in POLARITIES if polarity in read]
    if not order:
        raise LabInputError("No polarity to merge.")
    if len(order) == 2:
        compared = compare_sample_order(read[POSITIVE]["export"], read[NEGATIVE]["export"])
        if compared.get("problems"):
            raise LabInputError("The two polarities do not describe the same samples: " + " ".join(compared["problems"]))
    first = read[order[0]]["export"]
    sample_names = [strip_polarity(name) if len(order) == 2 else name for name in first["sample_names"]]
    metadata_titles = first["header"][4][: first["first_sample_column"]]
    removed: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    counts: dict[str, dict[str, int]] = {polarity: {} for polarity in order}

    def drop(row: dict[str, Any], reason: str, kept: dict[str, Any] | None = None) -> None:
        removed.append({"row": row, "reason": reason, "kept": kept})
        counts[row["polarity"]][reason] = counts[row["polarity"]].get(reason, 0) + 1

    for polarity in order:
        item = read[polarity]
        standards = {str(value) for value in item["input"].get("internal_standard_ids", [])}
        annotator = str(item["input"].get("internal_standard_annotator") or "")
        counts[polarity]["rows"] = len(item["rows"])
        for row in item["rows"]:
            if row["name"].casefold() in UNANNOTATED_NAMES:
                drop(row, "unannotated")
            elif row["id"] in standards or (annotator and row["method"] == annotator):
                drop(row, "internal standard")
            elif row["comment"].startswith(MANUAL_EXCLUSION_COMMENT_PREFIX):
                drop(row, "marked removable (Comment starts with x)")
            elif reference_matched_only and not row["matched"]:
                drop(row, "not reference matched")
            elif preferred_adducts_only and not is_preferred(row["adduct"], identities):
                drop(row, "not a preferred adduct")
            else:
                row["preferred"] = is_preferred(row["adduct"], identities)
                row["block"] = inchikey_first_block(row["inchikey"])
                candidates.append(row)

    def rank(row: dict[str, Any]) -> tuple[Any, ...]:
        return (not row["preferred"], not row["matched"], -row["mean"], -row["priority"], -row["total_score"],
                order.index(row["polarity"]), _id_key(row["id"]))

    groups: dict[str, list[dict[str, Any]]] = {}
    kept: list[dict[str, Any]] = []
    for row in candidates:
        if row["block"]:
            groups.setdefault(row["block"], []).append(row)
        else:
            kept.append(row)
    duplicates = {"within_polarity": 0, "across_polarities": 0}
    for block, rows in groups.items():
        ranked = sorted(rows, key=rank)
        winner = ranked[0]
        kept.append(winner)
        for other in ranked[1:]:
            duplicates["across_polarities" if other["polarity"] != winner["polarity"] else "within_polarity"] += 1
            drop(other, "duplicate InChIKey first block", winner)
    kept.sort(key=lambda row: (order.index(row["polarity"]), _id_key(row["id"])))

    label_of = dict(labels or {})
    header = ["Polarity", "Alignment ID", "InChIKey first block", "Library", "Reference matched", "Preferred adduct",
              *[title for title in metadata_titles if title != "Alignment ID"], *sample_names]
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(header)
    for row in kept:
        source = read[row["polarity"]]["export"]
        titles = source["header"][4]
        by_title = {title: (row["raw"][index] if index < len(row["raw"]) else "") for index, title in enumerate(titles)}
        writer.writerow([
            row["polarity"], row["id"], row["block"], label_of.get(row["method"], row["method"]),
            "TRUE" if row["matched"] else "FALSE", "TRUE" if row["preferred"] else "FALSE",
            *[by_title.get(title, "") for title in metadata_titles if title != "Alignment ID"],
            *row["samples"],
        ])
    metabolome = buffer.getvalue().encode("utf-8")

    removed_lines = ["\t".join(REMOVED_COLUMNS)]

    def text(value: Any) -> str:
        return "" if value is None else str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")

    for item in removed:
        row, winner = item["row"], item["kept"] or {}
        removed_lines.append("\t".join(text(value) for value in (
            row["polarity"], row["id"], row["name"], row["adduct"], row["inchikey"], item["reason"],
            winner.get("polarity"), winner.get("id"), winner.get("name"), winner.get("adduct"),
        )))
    removed_data = ("\r\n".join(removed_lines) + "\r\n").encode("utf-8")

    samples = io.StringIO()
    sample_writer = csv.writer(samples, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    sample_writer.writerow(["Sample", "Class", "File type", "Injection order", "Batch ID"])
    injection = first["header"][2][first["first_sample_column"]:]
    batch = first["header"][3][first["first_sample_column"]:]
    for offset, name in enumerate(sample_names):
        sample_writer.writerow([name, first["sample_classes"][offset], first["file_types"][offset],
                                injection[offset] if offset < len(injection) else "",
                                batch[offset] if offset < len(batch) else ""])
    samples_data = samples.getvalue().encode("utf-8")

    outputs = {}
    for key, name, data in (("metabolome", METABOLOME_NAME, metabolome), ("removed", REMOVED_NAME, removed_data),
                            ("samples", SAMPLES_NAME, samples_data)):
        path = folder / name
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        outputs[key] = {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    kept_counts = {polarity: sum(1 for row in kept if row["polarity"] == polarity) for polarity in order}
    report = {
        "polarities": order,
        "inputs": {
            polarity: {
                "normalized": str(Path(read[polarity]["input"]["normalized"]).resolve()),
                "normalized_sha256": file_sha256(read[polarity]["input"]["normalized"]),
                "candidate_table": read[polarity]["candidates"]["path"],
                "candidate_table_sha256": read[polarity]["candidates"]["sha256"],
                "internal_standard_ids": sorted(str(value) for value in read[polarity]["input"].get("internal_standard_ids", [])),
            }
            for polarity in order
        },
        "options": {"reference_matched_only": reference_matched_only, "preferred_adducts_only": preferred_adducts_only},
        "preferred_adducts": preferred_adducts(salt),
        "salt": salt,
        "ranking": [
            "a preferred adduct", "matched on MS/MS (reference matched)",
            f"the larger mean normalized height over the {' and '.join(MERGE_FILE_TYPES)} injections",
            "the higher library priority", "the higher total score", "positive before negative", "the lower alignment ID",
        ],
        "counts": {
            "by_polarity": counts,
            "duplicates_removed": duplicates,
            "kept": len(kept),
            "kept_by_polarity": kept_counts,
            "kept_without_inchikey": sum(1 for row in kept if not row["block"]),
            "removed": len(removed),
        },
        "samples": sample_names,
        "outputs": outputs,
    }
    if write:
        target = folder / MERGE_REPORT_NAME
        target.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        report["report"] = str(target.resolve())
    return report

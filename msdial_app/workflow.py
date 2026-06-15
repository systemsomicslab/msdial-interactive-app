from __future__ import annotations

import csv
import copy
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable


SUPPORTED_SUFFIXES = {
    ".abf",
    ".cdf",
    ".ibf",
    ".mzml",
    ".mzxml",
    ".raw",
    ".wiff",
    ".wiff2",
}
VC2013_DOWNLOAD_URL = (
    "https://support.microsoft.com/en-us/topic/"
    "update-for-visual-c-2013-and-visual-c-redistributable-package-"
    "5b2ac5ab-4139-8acc-08e2-9578ec9b2cf1"
)


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_SUFFIXES or (
        path.is_dir() and path.name.lower().endswith((".d", ".raw"))
    )


def detect_raw_format(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    suffix = target.suffix.lower()
    if target.is_file() and suffix in {".wiff", ".wiff2"}:
        return {
            "vendor": "SCIEX",
            "format": "SCIEX WIFF" if suffix == ".wiff" else "SCIEX WIFF2",
            "instrument_family": "QTOF",
            "minimum_peak_height": 100,
            "mass_slice_width": 0.1,
            "sidecar_available": (
                suffix != ".wiff" or Path(str(target) + ".scan").is_file()
            ),
        }
    if target.is_dir() and suffix == ".raw":
        return {
            "vendor": "Waters",
            "format": "Waters .raw folder",
            "instrument_family": "QTOF",
            "minimum_peak_height": 100,
            "mass_slice_width": 0.1,
        }
    if target.is_file() and suffix == ".raw":
        return {
            "vendor": "Thermo",
            "format": "Thermo .raw file",
            "instrument_family": "Fourier-transform MS",
            "minimum_peak_height": 10000,
            "mass_slice_width": 0.05,
        }
    if target.is_dir() and suffix == ".d":
        if (target / "AcqData").is_dir():
            vendor, label = "Agilent", "Agilent .d folder"
        elif any((target / name).is_file() for name in ("analysis.tdf", "analysis.tsf")):
            vendor, label = "Bruker", "Bruker TDF/TSF .d folder"
        elif (target / "analysis.baf").is_file():
            vendor, label = "Bruker", "Bruker BAF .d folder"
        else:
            vendor, label = "Unknown", "Unrecognized .d folder"
        return {
            "vendor": vendor,
            "format": label,
            "instrument_family": "QTOF",
            "minimum_peak_height": 100,
            "mass_slice_width": 0.1,
        }
    return {
        "vendor": "Open format" if suffix in {".mzml", ".mzxml", ".cdf"} else "Other",
        "format": suffix.lstrip(".").upper() or "Unknown",
        "instrument_family": "QTOF",
        "minimum_peak_height": 100,
        "mass_slice_width": 0.1,
    }


def recommended_peak_parameters(files: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(files)
    if not rows:
        return {"minimum_peak_height": 100, "mass_slice_width": 0.1}
    high_resolution = any(
        item.get("instrument_family") in {"Fourier-transform MS", "FT-ICR"}
        or item.get("vendor") == "Thermo"
        for item in rows
    )
    return {
        "minimum_peak_height": 10000 if high_resolution else 100,
        "mass_slice_width": 0.05 if high_resolution else 0.1,
    }


def expand_paths(paths: Iterable[str]) -> list[dict[str, Any]]:
    return expand_paths_report(paths)["files"]


def expand_paths_report(paths: Iterable[str]) -> dict[str, Any]:
    expanded: list[Path] = []
    rejected: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_file() and is_supported(path):
            expanded.append(path)
        elif path.is_dir() and is_supported(path):
            expanded.append(path)
        elif path.is_dir():
            expanded.extend(
                child
                for child in path.iterdir()
                if is_supported(child)
            )
        elif path.exists():
            rejected.append(str(path))
        else:
            rejected.append(f"{path} (not found)")
    unique = sorted(set(expanded), key=lambda item: str(item).lower())
    result = []
    for index, path in enumerate(unique):
        format_info = detect_raw_format(path)
        name = path.stem
        lower = name.lower()
        is_blank = "blank" in lower
        class_id = (
            "Blank"
            if is_blank
            else "Feces"
            if "feces" in lower
            else "Plasma"
            if "plasma" in lower
            else "Sample"
        )
        result.append(
            {
                "file_path": str(path),
                "file_name": name,
                "file_type": "Blank" if is_blank else "Sample",
                "class_id": class_id,
                "acquisition_type": "DDA",
                "batch_order": 1,
                "analytical_order": index + 1,
                "factor": 1,
                **format_info,
            }
        )
    warnings = _sciex_pair_warnings(unique)
    return {"files": result, "warnings": warnings, "rejected": rejected}


def _sciex_pair_warnings(paths: Iterable[Path]) -> list[str]:
    samples: dict[tuple[str, str], set[str]] = {}
    for path in paths:
        suffix = path.suffix.lower()
        if suffix not in {".wiff", ".wiff2"}:
            continue
        key = (str(path.parent).lower(), path.stem.lower())
        samples.setdefault(key, set()).add(suffix)
    return [
        (
            f"Both .wiff and .wiff2 were found for sample '{sample}'. "
            "Choose exactly one SCIEX primary data file."
        )
        for (_, sample), suffixes in samples.items()
        if suffixes == {".wiff", ".wiff2"}
    ]


def read_lipid_queries(path: str | Path) -> list[dict[str, Any]]:
    query_path = Path(path)
    if not query_path.exists():
        return []
    rows = []
    with query_path.open(encoding="utf-8-sig", errors="replace") as handle:
        for raw in handle:
            if not raw.strip() or raw.startswith("#"):
                continue
            columns = raw.rstrip("\r\n").split("\t")
            if len(columns) < 4 or columns[0].lower() == "class":
                continue
            rows.append(
                {
                    "lipid_class": columns[0].strip(),
                    "adduct": columns[1].strip(),
                    "ion_mode": columns[2].strip(),
                    "selected": columns[3].strip().lower() == "true",
                }
            )
    return rows


def read_adducts(path: str | Path, ion_mode: str) -> list[dict[str, Any]]:
    resource = Path(path)
    rows: list[dict[str, Any]] = []
    with resource.open(encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            adduct = str(row.get("Adduct", "")).strip()
            if not adduct:
                continue
            rows.append(
                {
                    "adduct": adduct,
                    "charge": int(row.get("Charge", 0)),
                    "accurate_mass": float(row.get("Accurate mass", 0)),
                    "ion_mode": ion_mode,
                    "selected": True,
                }
            )
    return rows


def parse_method(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    method_path = Path(path)
    if not method_path.exists():
        return values
    for line in method_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().lower()] = value.strip()
    return values


def validate_workflow(state: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    files = state.get("files", [])
    project_type = str(state.get("project_type", "lcms")).lower()
    if project_type != "lcms":
        issues.append(
            {
                "level": "error",
                "message": (
                    f"{project_type.upper()} parameter UI is scaffolded, but this version "
                    "executes LC-MS workflows only. A mode-specific parameter template "
                    "and backend are required before this project type is runnable."
                ),
            }
        )
    required_paths = [
        ("console_path", "MS-DIAL Console executable"),
        ("template_path", "parameter template"),
        ("output_root", "output root"),
    ]
    for key, label in required_paths:
        value = str(state.get(key, "")).strip()
        if not value:
            issues.append({"level": "error", "message": f"Missing {label}."})
        elif key != "output_root" and not Path(value).exists():
            issues.append({"level": "error", "message": f"Not found: {value}"})
    if not files:
        issues.append({"level": "error", "message": "No analysis files were added."})
    names = set()
    acquisition_types = set()
    sciex_samples: dict[tuple[str, str], set[str]] = {}
    for item in files:
        path = Path(item.get("file_path", ""))
        name = str(item.get("file_name", ""))
        if not path.exists():
            issues.append({"level": "error", "message": f"Input not found: {path}"})
        if (
            path.is_file()
            and path.suffix.lower() == ".wiff"
            and not Path(str(path) + ".scan").is_file()
        ):
            issues.append(
                {
                    "level": "error",
                    "message": (
                        f"WIFF.SCAN is not accessible next to {path}. "
                        "A browser cannot upload an unselected sibling file; drop both "
                        "files together or reference the original file/folder directly."
                    ),
                }
            )
        if path.is_dir() and path.suffix.lower() == ".d" and item.get("vendor") == "Unknown":
            issues.append(
                {
                    "level": "error",
                    "message": f"Unrecognized .d folder (no AcqData/analysis.tdf/analysis.tsf/analysis.baf): {path}",
                }
            )
        if "," in str(path) or "," in name or "," in str(item.get("class_id", "")):
            issues.append(
                {
                    "level": "error",
                    "message": f"Console CSV cannot quote commas: {name}",
                }
            )
        if name.lower() in names:
            issues.append({"level": "error", "message": f"Duplicate file_name: {name}"})
        names.add(name.lower())
        acquisition_types.add(item.get("acquisition_type", "DDA"))
        if path.suffix.lower() in {".wiff", ".wiff2"}:
            key = (str(path.parent).lower(), path.stem.lower())
            sciex_samples.setdefault(key, set()).add(path.suffix.lower())
    for (_, sample), suffixes in sciex_samples.items():
        if suffixes == {".wiff", ".wiff2"}:
            issues.append(
                {
                    "level": "error",
                    "message": (
                        f"Both .wiff and .wiff2 are selected for sample '{sample}'. "
                        "Keep exactly one SCIEX primary data file."
                    ),
                }
            )
    if len(acquisition_types) > 1:
        issues.append(
            {
                "level": "warning",
                "message": (
                    "Multiple acquisition types require the per-file acquisition_type fix "
                    "(fix/console-per-file-acquisition-type) or a release containing it."
                ),
            }
        )
    if any(item.get("vendor") == "Agilent" for item in files):
        issues.append(
            {
                "level": "warning",
                "message": (
                    "Agilent .d reading on Windows may require Microsoft Visual C++ "
                    f"2013 Redistributable Package x64: {VC2013_DOWNLOAD_URL}"
                ),
            }
        )
        if platform.system() != "Windows":
            issues.append(
                {
                    "level": "warning",
                    "message": (
                        "Agilent .d uses a vendor reader whose OS support depends on the "
                        "selected MS-DIAL Console package. Convert to mzML when the reader "
                        "is unavailable on this OS."
                    ),
                }
            )
        console_value = str(state.get("console_path", "")).strip()
        console_path = Path(console_value) if console_value else None
        if console_path and console_path.exists():
            console_directory = console_path.parent
            root_reader = console_directory / "BaseDataAccess.dll"
            packaged_reader = console_directory / "lib" / "Agilent" / "BaseDataAccess.dll"
            if not root_reader.exists() and not packaged_reader.exists():
                issues.append(
                    {
                        "level": "warning",
                        "message": (
                            "Agilent reader dependency BaseDataAccess.dll was not found "
                            f"beside the Console or under lib/Agilent: {console_directory}"
                        ),
                    }
                )
            elif not root_reader.exists() and packaged_reader.exists():
                issues.append(
                    {
                        "level": "warning",
                        "message": (
                            "BaseDataAccess.dll exists only under lib/Agilent. If the run "
                            "reports a BaseDataAccess load error, use an official packaged "
                            "Console build or deploy the Agilent reader so the runtime can "
                            "resolve it."
                        ),
                    }
                )
    for key, label in (
        ("msp_path", "MSP library"),
        ("lbm_path", "LBM library"),
        ("text_db_path", "Text DB"),
    ):
        value = str(state.get(key, "")).strip()
        if value and not Path(value).exists():
            issues.append({"level": "error", "message": f"{label} not found: {value}"})
    selected = state.get("selected_lipids", [])
    if project_type != "gcms" and state.get("target_omics") == "Lipidomics" and not selected:
        issues.append({"level": "error", "message": "No lipid annotation query is selected."})
    if (
        project_type != "gcms"
        and "selected_adducts" in state
        and not state.get("selected_adducts")
    ):
        issues.append({"level": "error", "message": "No adduct ion is selected."})
    return issues


def prepare_run(
    state: dict[str, Any],
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    issues = validate_workflow(state)
    errors = [issue["message"] for issue in issues if issue["level"] == "error"]
    if errors:
        raise ValueError("\n".join(errors))
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    project_type = str(state.get("project_type", "lcms")).lower()
    run_directory = Path(state["output_root"]).expanduser().resolve() / f"{timestamp}-{project_type}"
    run_directory.mkdir(parents=True, exist_ok=False)
    stage_inputs = bool(state.get("stage_inputs", True))
    input_directory = run_directory / "input"
    if stage_inputs:
        input_directory.mkdir()

    effective_files: list[Path] = []
    files = state["files"]
    for index, item in enumerate(files):
        source = Path(item["file_path"]).resolve()
        if progress:
            progress(f"Staging input {index + 1}/{len(files)}: {source.name}")
        if stage_inputs:
            target = input_directory / source.name
            _stage_path(source, target)
            effective_files.append(target)
        else:
            effective_files.append(source)

    csv_path = run_directory / "analysis_files.csv"
    _write_analysis_csv(csv_path, files, effective_files)
    method_path = run_directory / "method.txt"
    _write_method(method_path, state)
    manifest_path = run_directory / "run-manifest.json"
    manifest = {
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "platform": platform.platform(),
        "analysis_type": project_type,
        "project_file_requested": True,
        "stage_inputs": stage_inputs,
        "input_csv": str(csv_path),
        "method_file": str(method_path),
        "output_folder": str(run_directory),
        "source_files": [item["file_path"] for item in files],
        "effective_files": [str(path) for path in effective_files],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    command = build_console_command(
        state["console_path"],
        csv_path,
        run_directory,
        method_path,
    )
    return {
        "run_directory": str(run_directory),
        "input_csv": str(csv_path),
        "method_file": str(method_path),
        "manifest": str(manifest_path),
        "command": command,
        "warnings": [issue for issue in issues if issue["level"] == "warning"],
    }


def prepare_tuning_run(
    state: dict[str, Any],
    file_path: str,
    output_root: str | Path,
) -> dict[str, Any]:
    tuning = copy.deepcopy(state)
    selected = next(
        (item for item in tuning.get("files", []) if item.get("file_path") == file_path),
        None,
    )
    if selected is None:
        raise ValueError("Select one analysis file for parameter tuning.")
    tuning["files"] = [selected]
    tuning["output_root"] = str(output_root)
    tuning["stage_inputs"] = False
    tuning["minimum_peak_height"] = 0
    tuning["together_with_alignment"] = False
    tuning["msp_weighted_dot_product"] = 0
    tuning["msp_simple_dot_product"] = 0
    tuning["msp_reverse_dot_product"] = 0
    tuning["msp_matched_peaks_percentage"] = 0
    tuning["msp_minimum_spectrum_match"] = 0
    return prepare_run(tuning)


def parse_mdpeak(path: str | Path) -> dict[str, Any]:
    mdpeak = Path(path)
    heights: list[float] = []
    scores: list[dict[str, float]] = []
    scored_count = 0
    with mdpeak.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"Height", "Simple dot product", "Weighted dot product", "Reverse dot product"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"Unsupported mdpeak header: {mdpeak}")
        for row in reader:
            height = _nullable_float(row.get("Height"))
            if height is not None:
                heights.append(height)
            weighted = _nullable_float(row.get("Weighted dot product"))
            simple = _nullable_float(row.get("Simple dot product"))
            reverse = _nullable_float(row.get("Reverse dot product"))
            matched_percentage = _nullable_float(row.get("Matched peaks percentage"))
            matched_count = _nullable_float(row.get("Matched peaks count"))
            if all(
                value is not None
                for value in (weighted, simple, reverse, matched_percentage, matched_count)
            ):
                if all(
                    value >= 0
                    for value in (
                        weighted,
                        simple,
                        reverse,
                        matched_percentage,
                        matched_count,
                    )
                ):
                    scored_count += 1
                scores.append(
                    {
                        "weighted": weighted,
                        "simple": simple,
                        "reverse": reverse,
                        "matched_percentage": matched_percentage,
                        "matched_count": matched_count,
                    }
                )
    heights.sort()
    return {
        "mdpeak": str(mdpeak),
        "peak_count": len(heights),
        "heights": heights,
        "msp_candidate_count": len(scores),
        "msp_scored_count": scored_count,
        "msp_scores": scores,
    }


def find_mdpeak(run_directory: str | Path) -> Path:
    files = sorted(Path(run_directory).glob("*.mdpeak"))
    if not files:
        raise FileNotFoundError(f"No mdpeak was generated in {run_directory}")
    return files[0]


def build_console_command(
    console_path: str,
    csv_path: str | Path,
    output_path: str | Path,
    method_path: str | Path,
) -> list[str]:
    executable = Path(console_path).expanduser().resolve()
    prefix = ["dotnet", str(executable)] if executable.suffix.lower() == ".dll" else [str(executable)]
    return prefix + [
        "lcms",
        "-i",
        str(csv_path),
        "-o",
        str(output_path),
        "-m",
        str(method_path),
        "-p",
    ]


def run_console(
    preparation: dict[str, Any],
    on_line: Callable[[str], None],
) -> int:
    process = subprocess.Popen(
        preparation["command"],
        cwd=str(Path(preparation["command"][0]).parent)
        if Path(preparation["command"][0]).is_absolute()
        else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    scan_file_errors = 0
    for line in process.stdout:
        message = line.rstrip()
        on_line(message)
        lower = message.lower()
        if "required 'scan' file missing" in lower or "required 'scan' file is missing" in lower:
            scan_file_errors += 1
            if scan_file_errors >= 3:
                on_line(
                    "Stopping diagnostic after repeated SCIEX scan-sidecar read failures."
                )
                process.terminate()
                process.wait(timeout=10)
                return -2
    return process.wait()


def _write_analysis_csv(
    path: Path,
    rows: list[dict[str, Any]],
    effective_paths: list[Path],
) -> None:
    headers = [
        "file_path",
        "file_name",
        "file_type",
        "class_id",
        "acquisition_type",
        "batch_order",
        "analytical_order",
        "factor",
    ]
    with path.open("w", encoding="ascii", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, lineterminator="\n")
        writer.writeheader()
        for row, effective in zip(rows, effective_paths, strict=True):
            data = {key: row.get(key, "") for key in headers}
            data["file_path"] = str(effective)
            writer.writerow(data)


def _write_method(path: Path, state: dict[str, Any]) -> None:
    template_path = Path(state["template_path"])
    lines = template_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    replacements = {
        "msp file path": state.get("msp_path", ""),
        "lbm file path": state.get("lbm_path", ""),
        "text db file path": state.get("text_db_path", ""),
        "searched adduct ions": ",".join(state.get("selected_adducts", [])),
        "ion mode": state.get("ion_mode", "Negative"),
        "target omics": state.get("target_omics", "Lipidomics"),
        "ms1 data type": state.get("ms1_data_type", "Centroid"),
        "ms2 data type": state.get("ms2_data_type", "Centroid"),
        "number of threads": state.get("number_of_threads", 4),
        "minimum peak height": state.get("minimum_peak_height", 1000),
        "mass slice width": state.get("mass_slice_width", 0.1),
        "minimum peak width": state.get("minimum_peak_width", 5),
        "retention time begin": state.get("retention_time_begin", 0),
        "retention time end": state.get("retention_time_end", 100),
        "ms1 tolerance for centroid": state.get("ms1_tolerance", 0.01),
        "ms2 tolerance for centroid": state.get("ms2_tolerance", 0.025),
        "retention time tolerance for alignment": state.get(
            "alignment_rt_tolerance", 0.1
        ),
        "ms1 tolerance for alignment": state.get("alignment_ms1_tolerance", 0.015),
        "weighted dot product cutoff for msp-based annotation": state.get(
            "msp_weighted_dot_product", 0.6
        ),
        "simple dot product cutoff for msp-based annotation": state.get(
            "msp_simple_dot_product", 0.6
        ),
        "reverse dot product cutoff for msp-based annotation": state.get(
            "msp_reverse_dot_product", 0.8
        ),
        "matched peaks percentage cutoff for msp-based annotation": state.get(
            "msp_matched_peaks_percentage", 0.1
        ),
        "minimum spectrum match for msp-based annotation": state.get(
            "msp_minimum_spectrum_match", 3
        ),
        "together with alignment": state.get("together_with_alignment", True),
        "export as mztabm format": "True",
    }
    selected = state.get("selected_lipids", [])
    searched = ";".join(
        f"{item['lipid_class']} {item['adduct']}"
        for item in selected
        if item.get("ion_mode") == state.get("ion_mode")
    )
    output: list[str] = []
    found: set[str] = set()
    annotation_inserted = False
    for line in lines:
        stripped = line.lstrip()
        lower = stripped.lower()
        if lower.startswith(("solvent type:", "searched lipid class:")):
            continue
        if lower.startswith("adduct list:"):
            output.append(
                f"Searched adduct ions: {replacements['searched adduct ions']}"
            )
            found.add("searched adduct ions")
            continue
        matched = next(
            (key for key in replacements if lower.startswith(key + ":")),
            None,
        )
        if matched:
            output.append(f"{_title_for_key(matched)}: {replacements[matched]}")
            found.add(matched)
            continue
        output.append(line)
        if stripped.lower() == "# annotation parameter":
            output.append(f"Searched lipid class: {searched}")
            output.append(f"Solvent type: {state.get('solvent', 'CH3COONH4')}")
            annotation_inserted = True
    for key, value in replacements.items():
        if key not in found:
            output.insert(0, f"{_title_for_key(key)}: {value}")
    if not annotation_inserted:
        output.extend(
            [
                "",
                "# Annotation parameter",
                f"Searched lipid class: {searched}",
                f"Solvent type: {state.get('solvent', 'CH3COONH4')}",
            ]
        )
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _title_for_key(key: str) -> str:
    names = {
        "msp file path": "Msp file path",
        "lbm file path": "Lbm file path",
        "text db file path": "Text DB file path",
        "searched adduct ions": "Searched adduct ions",
        "ion mode": "Ion mode",
        "target omics": "Target omics",
        "ms1 data type": "MS1 data type",
        "ms2 data type": "MS2 data type",
        "number of threads": "Number of threads",
        "minimum peak height": "Minimum peak height",
        "mass slice width": "Mass slice width",
        "minimum peak width": "Minimum peak width",
        "retention time begin": "Retention time begin",
        "retention time end": "Retention time end",
        "ms1 tolerance for centroid": "MS1 tolerance for centroid",
        "ms2 tolerance for centroid": "MS2 tolerance for centroid",
        "retention time tolerance for alignment": "Retention time tolerance for alignment",
        "ms1 tolerance for alignment": "MS1 tolerance for alignment",
        "weighted dot product cutoff for msp-based annotation": "Weighted dot product cutoff for MSP-based annotation",
        "simple dot product cutoff for msp-based annotation": "Simple dot product cutoff for MSP-based annotation",
        "reverse dot product cutoff for msp-based annotation": "Reverse dot product cutoff for MSP-based annotation",
        "matched peaks percentage cutoff for msp-based annotation": "Matched peaks percentage cutoff for MSP-based annotation",
        "minimum spectrum match for msp-based annotation": "Minimum spectrum match for MSP-based annotation",
        "together with alignment": "Together with alignment",
        "export as mztabm format": "Export as mztabM format",
    }
    return names[key]


def _stage_path(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    if source.suffix.lower() == ".wiff":
        sidecar = Path(str(source) + ".scan")
        if sidecar.exists():
            sidecar_target = Path(str(target) + ".scan")
            try:
                os.link(sidecar, sidecar_target)
            except OSError:
                shutil.copy2(sidecar, sidecar_target)


def _nullable_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text.lower() == "null":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def console_version(console_path: str) -> str:
    path = Path(console_path)
    if not path.exists():
        return ""
    command = ["dotnet", str(path)] if path.suffix.lower() == ".dll" else [str(path)]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = re.search(r"Version\s+([0-9.]+)", result.stdout + result.stderr, re.I)
    return match.group(1) if match else ""

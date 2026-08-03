from __future__ import annotations

import csv
import copy
import datetime as dt
import json
import os
import platform
import re
import shlex
import subprocess
import zipfile
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
SMOOTHING_METHODS = [
    "SimpleMovingAverage",
    "LinearWeightedMovingAverage",
    "SavitzkyGolayFilter",
    "BinomialFilter",
    "LowessFilter",
    "LoessFilter",
    "TimeBasedLinearWeightedMovingAverage",
]


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
    if project_type not in {"lcms", "gcms"}:
        issues.append(
            {
                "level": "error",
                "message": (
                    f"{project_type.upper()} parameter UI is scaffolded, but this version "
                    "does not execute this project type yet. A mode-specific parameter "
                    "template and backend are required before it is runnable."
                ),
            }
        )
    if state.get("alignment_light_mode") and project_type != "lcms":
        issues.append(
            {
                "level": "error",
                "message": "Alignment light mode is currently available for LC-MS Console runs only.",
            }
        )
    if state.get("alignment_light_mode") and not state.get("together_with_alignment", True):
        issues.append(
            {
                "level": "error",
                "message": "Alignment light mode requires Together with alignment to be enabled.",
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
                        "Use Add original files, Add original folder, or Add path so the "
                        "original WIFF directory remains directly accessible."
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
    has_folder_type_input = any(Path(str(item.get("file_path", ""))).is_dir() for item in files)
    if has_folder_type_input and not _console_supports_folder_type_csv(
        str(state.get("console_path", ""))
    ):
        issues.append(
            {
                "level": "error",
                "message": (
                    "The selected MS-DIAL Console does not support folder-type raw-data "
                    "paths in analysis_files.csv. Use the patched source build: "
                    f"{_patched_console_path_hint()}"
                ),
            }
        )
    elif has_folder_type_input:
        issues.append(
            {
                "level": "warning",
                "message": (
                    "Folder-type raw data (.d/.raw) will be passed through analysis_files.csv. "
                    "This requires the patched MS-DIAL Console source build so per-file "
                    "metadata can be honored."
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
    for index, annotator in enumerate(state.get("msp_annotators", []), start=1):
        value = str(annotator.get("msp_file_path", "")).strip()
        if value and not Path(value).exists():
            label = str(annotator.get("annotator_id", "")).strip() or f"MSP annotator row {index}"
            issues.append({"level": "error", "message": f"{label} MSP file not found: {value}"})
    for index, annotator in enumerate(state.get("text_annotators", []), start=1):
        value = str(annotator.get("text_db_file_path", "")).strip()
        if value and not Path(value).exists():
            label = str(annotator.get("annotator_id", "")).strip() or f"Text annotator row {index}"
            issues.append({"level": "error", "message": f"{label} Text library file not found: {value}"})
    selected = state.get("selected_lipids", [])
    if project_type != "gcms" and state.get("target_omics") == "Lipidomics" and not selected:
        issues.append({"level": "error", "message": "No lipid annotation query is selected."})
    if (
        project_type != "gcms"
        and "selected_adducts" in state
        and not state.get("selected_adducts")
    ):
        issues.append({"level": "error", "message": "No adduct ion is selected."})
    if state.get("smoothing_method", "LinearWeightedMovingAverage") not in SMOOTHING_METHODS:
        issues.append(
            {
                "level": "error",
                "message": f"Unsupported smoothing method: {state.get('smoothing_method')}",
            }
        )
    if project_type == "gcms":
        uses_ri = (
            str(state.get("gcms_retention_type", "RT")).upper() == "RI"
            or str(state.get("gcms_alignment_index_type", "RT")).upper() == "RI"
        )
        if uses_ri:
            source = str(state.get("gcms_ri_source", "single"))
            if source == "dictionary":
                dictionary = str(state.get("gcms_ri_dictionary_path", "")).strip()
                if not dictionary:
                    issues.append({"level": "error", "message": "Set the GC-MS RI dictionary path."})
                elif not Path(dictionary).exists():
                    issues.append({"level": "error", "message": f"RI dictionary not found: {dictionary}"})
            elif source == "perFile":
                mapping = {
                    str(item.get("file_path", "")): str(item.get("ri_path", "")).strip()
                    for item in state.get("gcms_ri_file_map", [])
                }
                for file in files:
                    raw_path = str(file.get("file_path", ""))
                    ri_path = mapping.get(raw_path, "")
                    if not ri_path:
                        issues.append(
                            {
                                "level": "error",
                                "message": f"Missing RI carbon-RT file for {file.get('file_name', raw_path)}.",
                            }
                        )
                    elif not Path(ri_path).exists():
                        issues.append({"level": "error", "message": f"RI carbon-RT file not found: {ri_path}"})
            else:
                standard = str(state.get("gcms_ri_standard_path", "")).strip()
                if not standard:
                    issues.append({"level": "error", "message": "Set the alkane/FAME carbon-RT file for RI calculation."})
                elif not Path(standard).exists():
                    issues.append({"level": "error", "message": f"RI carbon-RT file not found: {standard}"})
    return issues


def prepare_run(
    state: dict[str, Any],
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    issues = validate_workflow(state)
    errors = [issue["message"] for issue in issues if issue["level"] == "error"]
    if errors:
        raise ValueError("\n".join(errors))
    project_type = str(state.get("project_type", "lcms")).lower()
    run_directory = Path(state["output_root"]).expanduser().resolve()
    run_directory.mkdir(parents=True, exist_ok=True)
    effective_files: list[Path] = []
    files = state["files"]
    for index, item in enumerate(files):
        source = Path(item["file_path"]).resolve()
        if progress:
            progress(f"Using original input {index + 1}/{len(files)}: {source}")
        effective_files.append(source)

    csv_path = run_directory / "analysis_files.csv"
    _write_analysis_csv(csv_path, files, effective_files)
    method_state = dict(state)
    ri_dictionary = _prepare_gcms_ri_dictionary(
        run_directory,
        method_state,
        files,
        effective_files,
    )
    method_path = run_directory / "method.txt"
    _write_method(method_path, method_state)
    command = build_console_command(
        state["console_path"],
        csv_path,
        run_directory,
        method_path,
        project_type,
        bool(state.get("project_store", True)),
    )
    project_file_requested = bool(state.get("project_store", True))
    analysis_extension = ".mdscan" if project_type == "gcms" else ".mdpeak"
    expected_analysis_exports = [
        str(run_directory / f"{item['file_name']}{analysis_extension}")
        for item in files
    ]
    manifest_path = run_directory / "run-manifest.json"
    manifest = {
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "platform": platform.platform(),
        "analysis_type": project_type,
        "project_file_requested": project_file_requested,
        "stage_inputs": False,
        "input_csv": str(csv_path),
        "console_input": str(csv_path),
        "temporary_input_folder": "",
        "method_file": str(method_path),
        "output_folder": str(run_directory),
        "source_files": [item["file_path"] for item in files],
        "effective_files": [str(path) for path in effective_files],
        "ri_dictionary_file": str(ri_dictionary) if ri_dictionary is not None else "",
        "msp_annotator_settings_file": str(method_state.get("msp_annotator_settings_file_path", "")),
        "text_annotator_settings_file": str(method_state.get("text_annotator_settings_file_path", "")),
        "command": command,
        "expected_analysis_exports": expected_analysis_exports,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    reproduction = _write_reproduction_files(
        run_directory,
        method_state,
        command,
    )
    return {
        "run_directory": str(run_directory),
        "analysis_type": project_type,
        "expected_analysis_exports": expected_analysis_exports,
        "diagnostic_result_file": expected_analysis_exports[0] if len(files) == 1 else "",
        "input_csv": str(csv_path),
        "console_input": str(csv_path),
        "temporary_input_folder": "",
        "preserve_temporary_input_folder": False,
        "project_file_requested": project_file_requested,
        "method_file": str(method_path),
        "manifest": str(manifest_path),
        "command": command,
        **reproduction,
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
    tuning["project_store"] = False
    tuning["together_with_alignment"] = False
    if str(tuning.get("project_type", "lcms")).lower() == "gcms":
        tuning["minimum_peak_height"] = state.get("minimum_peak_height", 1000)
    else:
        tuning["minimum_peak_height"] = 0
    tuning["msp_weighted_dot_product"] = 0
    tuning["msp_simple_dot_product"] = 0
    tuning["msp_reverse_dot_product"] = 0
    tuning["msp_matched_peaks_percentage"] = 0
    tuning["msp_minimum_spectrum_match"] = 0
    for annotator in tuning.get("msp_annotators", []):
        annotator["weighted_dot_product_cutoff"] = 0
        annotator["simple_dot_product_cutoff"] = 0
        annotator["reverse_dot_product_cutoff"] = 0
        annotator["matched_peaks_percentage_cutoff"] = 0
        annotator["minimum_spectrum_match"] = 0
    prepared = prepare_run(tuning)
    if prepared.get("temporary_input_folder"):
        prepared["diagnostic_input_folder"] = prepared["temporary_input_folder"]
        prepared.setdefault("warnings", []).append(
            {
                "level": "warning",
                "message": (
                    "Folder-type input uses a temporary directory link so older "
                    "MS-DIAL Console builds can read .d/.raw data without CSV folder-path support."
                ),
            }
        )
    return prepared


def _folder_type_inputs(paths: list[Path]) -> list[Path]:
    return [
        path
        for path in paths
        if path.is_dir() and path.name.lower().endswith((".d", ".raw"))
    ]


def _patched_console_path_hint() -> str:
    return str(
        Path(__file__).resolve().parent.parent.parent
        / "MsdialWorkbench"
        / "tests"
        / "MSDIAL5"
        / "MsdialCoreTestApp"
        / "bin"
        / "Release"
        / "net48"
        / "MSDIALCUI.exe"
    )


def _console_supports_folder_type_csv(console_path: str) -> bool:
    if os.environ.get("MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED") == "1":
        return True
    path_text = str(console_path or "")
    lowered = path_text.replace("/", "\\").lower()
    return (
        "\\msdialworkbench\\tests\\msdial5\\msdialcoretestapp\\bin\\" in lowered
        and path_text.lower().endswith(("msdialcui.exe", "msdialcui.dll"))
    )


def _prepare_temporary_console_input_folder(sources: list[Path], purpose: str) -> Path:
    staging_base = Path(__file__).resolve().parent.parent / "work" / "console_inputs"
    staging_base.mkdir(parents=True, exist_ok=True)
    staging_root = staging_base / (
        f".msdial_interactive_input_{purpose}_"
        + dt.datetime.now().strftime("%Y%m%d%H%M%S%f")
    )
    staging_root.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()
    for source in sources:
        if source.name.lower() in seen:
            raise ValueError(f"Duplicate folder-type raw-data name cannot be staged: {source.name}")
        seen.add(source.name.lower())
        _link_directory(source, staging_root / source.name)
    return staging_root


def _link_directory(source: Path, link: Path) -> None:
    try:
        os.symlink(source, link, target_is_directory=True)
    except OSError as symlink_error:
        if os.name != "nt":
            raise RuntimeError(
                f"Could not create a directory symlink for {source}: {symlink_error}"
            ) from symlink_error
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(source)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Could not create a directory junction for folder-type raw data: "
                + (completed.stderr or completed.stdout or str(symlink_error)).strip()
            ) from symlink_error


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
        "source_file": str(mdpeak),
        "peak_count": len(heights),
        "heights": heights,
        "msp_candidate_count": len(scores),
        "msp_scored_count": scored_count,
        "msp_scores": scores,
    }


def parse_mdscan(path: str | Path) -> dict[str, Any]:
    mdscan = Path(path)
    heights: list[float] = []
    scores: list[dict[str, float]] = []
    scored_count = 0
    with mdscan.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {
            "Integrated height",
            "Simple dot product",
            "Weighted dot product",
            "Reverse dot product",
            "Fragment presence %",
        }
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"Unsupported mdscan header: {mdscan}")
        for row in reader:
            height = _nullable_float(row.get("Integrated height"))
            if height is not None:
                heights.append(height)
            weighted = _nullable_float(row.get("Weighted dot product"))
            simple = _nullable_float(row.get("Simple dot product"))
            reverse = _nullable_float(row.get("Reverse dot product"))
            matched_percentage = _nullable_float(row.get("Fragment presence %"))
            matched_count = _nullable_float(row.get("Matched peaks count"))
            if matched_count is None:
                matched_count = _count_spectrum_peaks(row.get("Spectrum"))
            if all(
                value is not None
                for value in (weighted, simple, reverse, matched_percentage)
            ):
                if (
                    weighted >= 0
                    and simple >= 0
                    and reverse >= 0
                    and matched_percentage >= 0
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
        "mdscan": str(mdscan),
        "source_file": str(mdscan),
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


def find_mdscan(run_directory: str | Path) -> Path:
    files = sorted(Path(run_directory).glob("*.mdscan"))
    if not files:
        raise FileNotFoundError(f"No mdscan was generated in {run_directory}")
    return files[0]


def _count_spectrum_peaks(value: str | None) -> float:
    if not value:
        return 0
    return float(len([item for item in value.split() if ":" in item]))


def build_console_command(
    console_path: str,
    csv_path: str | Path,
    output_path: str | Path,
    method_path: str | Path,
    analysis_type: str = "lcms",
    project_store: bool = True,
) -> list[str]:
    executable = Path(console_path).expanduser().resolve()
    prefix = ["dotnet", str(executable)] if executable.suffix.lower() == ".dll" else [str(executable)]
    command = prefix + [
        analysis_type,
        "-i",
        str(csv_path),
        "-o",
        str(output_path),
        "-m",
        str(method_path),
    ]
    if project_store:
        command.append("-p")
    return command


def _write_reproduction_files(
    run_directory: Path,
    state: dict[str, Any],
    command: list[str],
) -> dict[str, str]:
    settings_path = run_directory / "workflow-settings.json"
    settings = {
        key: value
        for key, value in state.items()
        if key not in {"files"}
    }
    settings["files"] = [
        {
            key: item.get(key)
            for key in (
                "file_path",
                "file_name",
                "file_type",
                "class_id",
                "acquisition_type",
                "batch_order",
                "analytical_order",
                "factor",
            )
        }
        for item in state.get("files", [])
    ]
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    command_path = run_directory / "command.txt"
    command_path.write_text(
        subprocess.list2cmdline(command) + "\n",
        encoding="utf-8",
    )
    default_console = str(state["console_path"])
    analysis_type = str(state.get("project_type", "lcms")).lower()
    powershell_path = run_directory / "run-msdial.ps1"
    powershell_path.write_text(
        _powershell_script(default_console, analysis_type),
        encoding="utf-8",
    )
    shell_path = run_directory / "run-msdial.sh"
    shell_path.write_text(
        _shell_script(default_console, analysis_type),
        encoding="utf-8",
        newline="\n",
    )
    readme_path = run_directory / "REPRODUCE.txt"
    readme_path.write_text(
        (
            "MS-DIAL reproducible Console workflow\n\n"
            "Files:\n"
            "- analysis_files.csv: original raw-data paths and sample metadata\n"
            "- method.txt: final parameter file, including Tune parameters values\n"
            "- msp_annotator_settings.tsv: optional per-MSP LC-MS annotation settings\n"
            "- text_annotator_settings.tsv: optional per-Text-library LC-MS annotation settings\n"
            "- workflow-settings.json: UI settings used to generate the workflow\n"
            "- command.txt: exact command generated on the original machine\n"
            "- run-msdial.ps1 / run-msdial.sh: portable launch scripts\n\n"
            "Edit parameters:\n"
            "  vim method.txt\n\n"
            "Windows PowerShell:\n"
            "  .\\run-msdial.ps1\n"
            "  .\\run-msdial.ps1 'C:\\path\\to\\MSDIALCUI.exe'\n\n"
            "Bash:\n"
            "  bash run-msdial.sh\n"
            "  bash run-msdial.sh /path/to/MSDIALCUI.dll\n\n"
            "The CSV contains absolute raw-data paths. Update them if the data move.\n"
        ),
        encoding="utf-8",
    )
    bundle_path = run_directory / "msdial-workflow-bundle.zip"
    members = [
        run_directory / "analysis_files.csv",
        run_directory / "method.txt",
        run_directory / "run-manifest.json",
        settings_path,
        command_path,
        powershell_path,
        shell_path,
        readme_path,
    ]
    ri_dictionary = state.get("ri_dictionary_file_path")
    if ri_dictionary and Path(ri_dictionary).is_file():
        members.append(Path(ri_dictionary))
    msp_annotator_settings = state.get("msp_annotator_settings_file_path")
    if msp_annotator_settings and Path(msp_annotator_settings).is_file():
        members.append(Path(msp_annotator_settings))
    text_annotator_settings = state.get("text_annotator_settings_file_path")
    if text_annotator_settings and Path(text_annotator_settings).is_file():
        members.append(Path(text_annotator_settings))
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            archive.write(member, member.name)
    return {
        "settings_file": str(settings_path),
        "command_file": str(command_path),
        "powershell_script": str(powershell_path),
        "shell_script": str(shell_path),
        "reproduce_readme": str(readme_path),
        "bundle": str(bundle_path),
    }


def _powershell_script(default_console: str, analysis_type: str) -> str:
    quoted = default_console.replace("'", "''")
    return (
        "param([string]$Console = '" + quoted + "')\n"
        "$Here = Split-Path -Parent $MyInvocation.MyCommand.Path\n"
        "$Output = Join-Path $Here 'reproduced-results'\n"
        "New-Item -ItemType Directory -Force -Path $Output | Out-Null\n"
        f"$Arguments = @('{analysis_type}', '-i', (Join-Path $Here 'analysis_files.csv'), "
        "'-o', $Output, '-m', (Join-Path $Here 'method.txt'), '-p')\n"
        "if ($Console.ToLowerInvariant().EndsWith('.dll')) {\n"
        "  & dotnet $Console @Arguments\n"
        "} else {\n"
        "  & $Console @Arguments\n"
        "}\n"
        "exit $LASTEXITCODE\n"
    )


def _shell_script(default_console: str, analysis_type: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"CONSOLE=${{1:-{shlex.quote(default_console)}}}\n"
        'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        'OUTPUT="$HERE/reproduced-results"\n'
        'mkdir -p "$OUTPUT"\n'
        'if [[ "${CONSOLE,,}" == *.dll ]]; then\n'
        f'  dotnet "$CONSOLE" {analysis_type} -i "$HERE/analysis_files.csv" '
        '-o "$OUTPUT" -m "$HERE/method.txt" -p\n'
        "else\n"
        f'  "$CONSOLE" {analysis_type} -i "$HERE/analysis_files.csv" '
        '-o "$OUTPUT" -m "$HERE/method.txt" -p\n'
        "fi\n"
    )


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


def _prepare_gcms_ri_dictionary(
    run_directory: Path,
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    effective_paths: list[Path],
) -> Path | None:
    if str(state.get("project_type", "lcms")).lower() != "gcms":
        return None
    uses_ri = (
        str(state.get("gcms_retention_type", "RT")).upper() == "RI"
        or str(state.get("gcms_alignment_index_type", "RT")).upper() == "RI"
    )
    if not uses_ri:
        state["ri_dictionary_file_path"] = ""
        return None
    source = str(state.get("gcms_ri_source", "single"))
    if source == "dictionary":
        dictionary = Path(str(state.get("gcms_ri_dictionary_path", ""))).expanduser().resolve()
        state["ri_dictionary_file_path"] = str(dictionary)
        return None
    dictionary = run_directory / "ri_dictionary_paths.txt"
    with dictionary.open("w", encoding="ascii", newline="") as handle:
        if source == "perFile":
            mapping = {
                str(item.get("file_path", "")): str(item.get("ri_path", "")).strip()
                for item in state.get("gcms_ri_file_map", [])
            }
            for original, effective in zip(rows, effective_paths, strict=True):
                standard = Path(mapping[str(original["file_path"])]).expanduser().resolve()
                handle.write(f"{effective}\t{standard}\n")
        else:
            standard = Path(str(state.get("gcms_ri_standard_path", ""))).expanduser().resolve()
            for path in effective_paths:
                handle.write(f"{path}\t{standard}\n")
    state["ri_dictionary_file_path"] = str(dictionary)
    return dictionary


def _write_method(path: Path, state: dict[str, Any]) -> None:
    template_path = Path(state["template_path"])
    lines = template_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    project_type = str(state.get("project_type", "lcms")).lower()
    msp_annotator_settings_path = _write_msp_annotator_settings(path.parent, state)
    text_annotator_settings_path = _write_text_annotator_settings(path.parent, state)
    first_msp_path = next(
        (
            str(row.get("msp_file_path", "")).strip()
            for row in state.get("msp_annotators", [])
            if str(row.get("msp_file_path", "")).strip()
        ),
        "",
    )
    replacements = {
        "msp file path": "" if msp_annotator_settings_path else (state.get("msp_path", "") or first_msp_path),
        "lbm file path": state.get("lbm_path", ""),
        "text db file path": "" if text_annotator_settings_path else state.get("text_db_path", ""),
        "searched adduct ions": ",".join(state.get("selected_adducts", [])),
        "ion mode": state.get("ion_mode", "Negative"),
        "target omics": state.get("target_omics", "Lipidomics"),
        "ms1 data type": state.get("ms1_data_type", "Centroid"),
        "ms2 data type": state.get("ms2_data_type", "Centroid"),
        "number of threads": state.get("number_of_threads", 4),
        "smoothing method": state.get("smoothing_method", "LinearWeightedMovingAverage"),
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
        "rt tolerance for lbm-based annotation": state.get("lbm_rt_tolerance", 100),
        "ms1 tolerance for lbm-based annotation": state.get("lbm_ms1_tolerance", 0.01),
        "ms2 tolerance for lbm-based annotation": state.get("lbm_ms2_tolerance", 0.025),
        "weighted dot product cutoff for lbm-based annotation": state.get("lbm_weighted_dot_product", 0.15),
        "simple dot product cutoff for lbm-based annotation": state.get("lbm_simple_dot_product", 0.15),
        "reverse dot product cutoff for lbm-based annotation": state.get("lbm_reverse_dot_product", 0.3),
        "matched peaks percentage cutoff for lbm-based annotation": state.get("lbm_matched_peaks_percentage", 0),
        "minimum spectrum match for lbm-based annotation": state.get("lbm_minimum_spectrum_match", 1),
        "use retention information for lbm-based annotation scoring": state.get("lbm_use_rt_scoring", False),
        "use retention information for lbm-based annotation filtering": state.get("lbm_use_rt_filtering", False),
        "together with alignment": state.get("together_with_alignment", True),
        "export as mztabm format": "True",
    }
    if project_type == "lcms":
        replacements["alignment light mode"] = bool(state.get("alignment_light_mode", False))
    if msp_annotator_settings_path is not None:
        replacements["msp annotator settings file path"] = str(msp_annotator_settings_path)
    if text_annotator_settings_path is not None:
        replacements["text annotator settings file path"] = str(text_annotator_settings_path)
    if project_type == "gcms":
        replacements.update(
            {
                "ionization": "EI",
                "machine category": "GCMS",
                "accuracy type": state.get("gcms_accuracy_type", "IsNominal"),
                "ri index file pathes": state.get("ri_dictionary_file_path", ""),
                "ri compound": state.get("gcms_ri_compound_type", "Alkanes"),
                "ri compound type": state.get("gcms_ri_compound_type", "Alkanes"),
                "retention type": state.get("gcms_retention_type", "RT"),
                "alignment index type": state.get("gcms_alignment_index_type", "RT"),
                "retention index alignment tolerance": state.get(
                    "gcms_ri_alignment_tolerance", 10
                ),
                "weighted dot product cutoff": state.get(
                    "msp_weighted_dot_product", 0.5
                ),
                "simple dot product cutoff": state.get(
                    "msp_simple_dot_product", 0.5
                ),
                "reverse dot product cutoff": state.get(
                    "msp_reverse_dot_product", 0.5
                ),
                "matched peaks percentage cutoff": state.get(
                    "msp_matched_peaks_percentage", 0.5
                ),
                "minimum spectrum match": state.get(
                    "msp_minimum_spectrum_match", 3
                ),
            }
        )
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
        if project_type != "gcms" and stripped.lower() == "# annotation parameter":
            output.append(f"Searched lipid class: {searched}")
            output.append(f"Solvent type: {state.get('solvent', 'CH3COONH4')}")
            annotation_inserted = True
    gcms_no_auto_insert = {
        "searched adduct ions",
        "lbm file path",
        "text db file path",
        "export as mztabm format",
        "weighted dot product cutoff",
        "simple dot product cutoff",
        "reverse dot product cutoff",
        "matched peaks percentage cutoff",
        "minimum spectrum match",
        "rt tolerance for lbm-based annotation",
        "ms1 tolerance for lbm-based annotation",
        "ms2 tolerance for lbm-based annotation",
        "weighted dot product cutoff for lbm-based annotation",
        "simple dot product cutoff for lbm-based annotation",
        "reverse dot product cutoff for lbm-based annotation",
        "matched peaks percentage cutoff for lbm-based annotation",
        "minimum spectrum match for lbm-based annotation",
        "use retention information for lbm-based annotation scoring",
        "use retention information for lbm-based annotation filtering",
    }
    for key, value in replacements.items():
        if key not in found:
            if project_type == "gcms" and key in gcms_no_auto_insert:
                continue
            output.insert(0, f"{_title_for_key(key)}: {value}")
    if project_type == "gcms":
        if "ri compound" not in found:
            output.append(f"RI compound: {state.get('gcms_ri_compound_type', 'Alkanes')}")
    elif not annotation_inserted:
        output.extend(
            [
                "",
                "# Annotation parameter",
                f"Searched lipid class: {searched}",
                f"Solvent type: {state.get('solvent', 'CH3COONH4')}",
            ]
        )
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _write_msp_annotator_settings(run_directory: Path, state: dict[str, Any]) -> Path | None:
    if str(state.get("project_type", "lcms")).lower() != "lcms":
        state["msp_annotator_settings_file_path"] = ""
        return None
    rows = [
        row
        for row in state.get("msp_annotators", [])
        if str(row.get("msp_file_path", "")).strip()
    ]
    if not rows:
        state["msp_annotator_settings_file_path"] = ""
        return None

    settings_path = run_directory / "msp_annotator_settings.tsv"
    header = [
        "annotator_id",
        "msp_file_path",
        "priority",
        "rt_tolerance",
        "ms1_tolerance",
        "ms2_tolerance",
        "weighted_dot_product_cutoff",
        "simple_dot_product_cutoff",
        "reverse_dot_product_cutoff",
        "matched_peaks_percentage_cutoff",
        "minimum_spectrum_match",
        "use_retention_information_for_scoring",
        "use_retention_information_for_filtering",
    ]
    with settings_path.open("w", encoding="ascii", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow(
                {
                    "annotator_id": str(row.get("annotator_id", "")).strip() or f"msp_annotator_{index}",
                    "msp_file_path": str(Path(str(row["msp_file_path"])).expanduser().resolve()),
                    "priority": int(row.get("priority") or index),
                    "rt_tolerance": row.get("rt_tolerance", state.get("msp_rt_tolerance", 100)),
                    "ms1_tolerance": row.get("ms1_tolerance", state.get("ms1_tolerance", 0.01)),
                    "ms2_tolerance": row.get("ms2_tolerance", state.get("ms2_tolerance", 0.025)),
                    "weighted_dot_product_cutoff": row.get("weighted_dot_product_cutoff", state.get("msp_weighted_dot_product", 0.6)),
                    "simple_dot_product_cutoff": row.get("simple_dot_product_cutoff", state.get("msp_simple_dot_product", 0.6)),
                    "reverse_dot_product_cutoff": row.get("reverse_dot_product_cutoff", state.get("msp_reverse_dot_product", 0.8)),
                    "matched_peaks_percentage_cutoff": row.get("matched_peaks_percentage_cutoff", state.get("msp_matched_peaks_percentage", 0.1)),
                    "minimum_spectrum_match": row.get("minimum_spectrum_match", state.get("msp_minimum_spectrum_match", 3)),
                    "use_retention_information_for_scoring": str(bool(row.get("use_rt_scoring", False))),
                    "use_retention_information_for_filtering": str(bool(row.get("use_rt_filtering", False))),
                }
            )
    state["msp_annotator_settings_file_path"] = str(settings_path)
    return settings_path


def _write_text_annotator_settings(run_directory: Path, state: dict[str, Any]) -> Path | None:
    if str(state.get("project_type", "lcms")).lower() != "lcms":
        state["text_annotator_settings_file_path"] = ""
        return None
    rows = [
        row
        for row in state.get("text_annotators", [])
        if str(row.get("text_db_file_path", "")).strip()
    ]
    if not rows:
        state["text_annotator_settings_file_path"] = ""
        return None

    settings_path = run_directory / "text_annotator_settings.tsv"
    header = [
        "annotator_id",
        "text_db_file_path",
        "priority",
        "rt_tolerance",
        "ms1_tolerance",
        "total_score_cutoff",
        "use_retention_information_for_scoring",
        "use_retention_information_for_filtering",
    ]
    with settings_path.open("w", encoding="ascii", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow(
                {
                    "annotator_id": str(row.get("annotator_id", "")).strip() or f"text_annotator_{index}",
                    "text_db_file_path": str(Path(str(row["text_db_file_path"])).expanduser().resolve()),
                    "priority": int(row.get("priority") or index),
                    "rt_tolerance": row.get("rt_tolerance", state.get("text_rt_tolerance", 0.5)),
                    "ms1_tolerance": row.get("ms1_tolerance", state.get("ms1_tolerance", 0.01)),
                    "total_score_cutoff": row.get("total_score_cutoff", 0.8),
                    "use_retention_information_for_scoring": str(bool(row.get("use_rt_scoring", False))),
                    "use_retention_information_for_filtering": str(bool(row.get("use_rt_filtering", False))),
                }
            )
    state["text_annotator_settings_file_path"] = str(settings_path)
    return settings_path


def _title_for_key(key: str) -> str:
    names = {
        "msp file path": "Msp file path",
        "msp annotator settings file path": "MSP annotator settings file path",
        "lbm file path": "Lbm file path",
        "text db file path": "Text DB file path",
        "text annotator settings file path": "Text annotator settings file path",
        "searched adduct ions": "Searched adduct ions",
        "ion mode": "Ion mode",
        "target omics": "Target omics",
        "ms1 data type": "MS1 data type",
        "ms2 data type": "MS2 data type",
        "number of threads": "Number of threads",
        "smoothing method": "Smoothing method",
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
        "rt tolerance for lbm-based annotation": "RT tolerance for LBM-based annotation",
        "ms1 tolerance for lbm-based annotation": "MS1 tolerance for LBM-based annotation",
        "ms2 tolerance for lbm-based annotation": "MS2 tolerance for LBM-based annotation",
        "weighted dot product cutoff for lbm-based annotation": "Weighted dot product cutoff for LBM-based annotation",
        "simple dot product cutoff for lbm-based annotation": "Simple dot product cutoff for LBM-based annotation",
        "reverse dot product cutoff for lbm-based annotation": "Reverse dot product cutoff for LBM-based annotation",
        "matched peaks percentage cutoff for lbm-based annotation": "Matched peaks percentage cutoff for LBM-based annotation",
        "minimum spectrum match for lbm-based annotation": "Minimum spectrum match for LBM-based annotation",
        "use retention information for lbm-based annotation scoring": "Use retention information for LBM-based annotation scoring",
        "use retention information for lbm-based annotation filtering": "Use retention information for LBM-based annotation filtering",
        "together with alignment": "Together with alignment",
        "alignment light mode": "Alignment light mode",
        "export as mztabm format": "Export as mztabM format",
        "ionization": "Ionization",
        "machine category": "Machine category",
        "accuracy type": "Accuracy type",
        "ri index file pathes": "RI index file pathes",
        "ri compound": "RI compound",
        "ri compound type": "RI compound type",
        "retention type": "Retention type",
        "alignment index type": "Alignment index type",
        "retention index alignment tolerance": "Retention index alignment tolerance",
        "weighted dot product cutoff": "Weighted dot product cutoff",
        "simple dot product cutoff": "Simple dot product cutoff",
        "reverse dot product cutoff": "Reverse dot product cutoff",
        "matched peaks percentage cutoff": "Matched peaks percentage cutoff",
        "minimum spectrum match": "Minimum spectrum match",
    }
    return names[key]


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

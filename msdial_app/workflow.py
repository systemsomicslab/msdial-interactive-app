from __future__ import annotations

import csv
import copy
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__
from .user_settings import load_user_settings


SUPPORTED_SUFFIXES = {
    ".abf",
    ".cdf",
    ".ibf",
    ".lcd",
    ".mzml",
    ".mzxml",
    ".qgd",
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
LCMS_QA_CAPABILITY = "lcms_alignment_qa_matrix"
RT_CORRECTION_REVIEW_CAPABILITY = "rt_correction_review"
CONSOLE_BUILD_PROVENANCE = "msdial-console-build-provenance.json"


def _git_output(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def find_console_source_root(console_path: str | Path) -> Path | None:
    path = Path(console_path).expanduser().resolve()
    for parent in path.parents:
        project = parent / "tests" / "MSDIAL5" / "MsdialCoreTestApp" / "MsdialCoreTestApp.csproj"
        if project.is_file() and (parent / ".git").exists():
            return parent
    return None


def console_git_state(source_root: str | Path) -> dict[str, Any]:
    root = Path(source_root).expanduser().resolve()
    head = _git_output(root, "rev-parse", "HEAD")
    if not head:
        return {"source_root": str(root), "available": False}
    status = _git_output(root, "status", "--porcelain", "--untracked-files=normal")
    diff = _git_output(root, "diff", "--binary", "HEAD")
    working_tree_state = status + "\n" + diff
    origin_master = _git_output(root, "rev-parse", "--verify", "refs/remotes/origin/master")
    ahead = behind = None
    if origin_master:
        counts = _git_output(root, "rev-list", "--left-right", "--count", "HEAD...origin/master")
        try:
            ahead_text, behind_text = counts.split()
            ahead, behind = int(ahead_text), int(behind_text)
        except (ValueError, TypeError):
            pass
    return {
        "source_root": str(root),
        "available": True,
        "branch": _git_output(root, "branch", "--show-current"),
        "head": head,
        "short_head": head[:12],
        "head_time": _git_output(root, "show", "-s", "--format=%cI", "HEAD"),
        "origin_master_head": origin_master,
        "origin_master_short_head": origin_master[:12],
        "ahead_of_origin_master": ahead,
        "behind_origin_master": behind,
        "remote_note": "origin/master is a local tracking reference; use Fetch and compare source to refresh it.",
        "dirty": bool(status),
        "changed_files": len(status.splitlines()) if status else 0,
        "working_tree_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest()
        if diff
        else "",
        "working_tree_state_sha256": hashlib.sha256(
            working_tree_state.encode("utf-8")
        ).hexdigest()
        if working_tree_state.strip()
        else "",
    }


def inspect_console_path(console_path: str | Path, source: str = "") -> dict[str, Any]:
    path = Path(console_path).expanduser().resolve()
    if not path.is_file():
        return {"path": str(path), "exists": False, "source": source or "custom"}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    source_root = find_console_source_root(path)
    folder_text = str(path.parent).casefold()
    if source_root:
        source_kind = "local_source_build"
    elif "msdial.console" in folder_text:
        source_kind = "official_distribution"
    else:
        source_kind = "custom"
    provenance_path = path.parent / CONSOLE_BUILD_PROVENANCE
    provenance: dict[str, Any] = {}
    if provenance_path.is_file():
        try:
            loaded = json.loads(provenance_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict) and loaded.get("binary_sha256") == digest.hexdigest():
                provenance = loaded
        except (OSError, json.JSONDecodeError):
            pass
    result = {
        "path": str(path),
        "exists": True,
        "source": source or source_kind.replace("_", " "),
        "source_kind": source_kind,
        "version": console_version(str(path)),
        "binary_sha256": digest.hexdigest(),
        "binary_size": stat.st_size,
        "binary_modified_at": dt.datetime.fromtimestamp(
            stat.st_mtime, tz=dt.timezone.utc
        ).astimezone().isoformat(),
        "provenance_verified": bool(provenance),
        "provenance": provenance,
        **console_capabilities(str(path)),
    }
    if source_root:
        result["git"] = console_git_state(source_root)
        if provenance:
            result["matches_recorded_git_head"] = (
                provenance.get("git_head") == result["git"].get("head")
                and provenance.get("working_tree_state_sha256", "")
                == result["git"].get("working_tree_state_sha256", "")
            )
    return result


def discover_console_paths(search_roots: Iterable[str | Path] | None = None) -> dict[str, Any]:
    saved = str(load_user_settings().get("console_path", "")).strip()
    environment = str(os.environ.get("MSDIAL_CONSOLE_PATH", "")).strip()
    path_match = shutil.which("MSDIALCUI.exe") or shutil.which("MSDIALCUI")
    candidates: list[tuple[str, Path | None]] = [
        ("saved setting", Path(saved) if saved else None),
        ("MSDIAL_CONSOLE_PATH", Path(environment) if environment else None),
        ("PATH", Path(path_match) if path_match else None),
    ]
    bases = [Path.cwd(), Path(sys.executable).resolve().parent, Path(__file__).resolve().parent.parent]
    for base in list(bases):
        bases.extend(list(base.parents)[:2])
    for base in dict.fromkeys(bases):
        candidates.extend(
            [
                ("near application", base / "MSDIALCUI.exe"),
                ("near application", base / "MSDIALCUI.dll"),
                ("source build", base / "MsdialWorkbench" / "tests" / "MSDIAL5" / "MsdialCoreTestApp" / "bin" / "Release" / "net48" / "MSDIALCUI.exe"),
                ("source build", base / "MsdialWorkbench" / "tests" / "MSDIAL5" / "MsdialCoreTestApp" / "bin" / "Release" / "net8" / "MSDIALCUI.dll"),
            ]
        )
        if base.is_dir():
            candidates.extend(
                ("console distribution", path)
                for pattern in ("MSDIAL.console*/MSDIALCUI.exe", "MSDIAL.console*/MSDIALCUI.dll")
                for path in base.glob(pattern)
            )
    for raw_root in search_roots or []:
        root = Path(raw_root).expanduser()
        if root.is_file():
            candidates.append(("requested path", root))
        elif root.is_dir():
            candidates.extend(
                ("requested search root", path)
                for name in ("MSDIALCUI.exe", "MSDIALCUI.dll")
                for path in root.rglob(name)
            )
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, candidate in candidates:
        if not candidate or not candidate.is_file():
            continue
        resolved = str(candidate.resolve())
        if resolved.casefold() in seen:
            continue
        seen.add(resolved.casefold())
        found.append(inspect_console_path(resolved, source))
    return {
        "configured_path": saved,
        "environment_path": environment,
        "candidates": found,
        "selected_path": found[0]["path"] if found else "",
        "expected_names": ["MSDIALCUI.exe", "MSDIALCUI.dll"],
    }


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
    if target.is_file() and suffix in {".lcd", ".qgd"}:
        return {
            "vendor": "Shimadzu",
            "format": "Shimadzu LCD" if suffix == ".lcd" else "Shimadzu QGD",
            "instrument_family": "QTOF" if suffix == ".lcd" else "GC-MS",
            "minimum_peak_height": 100,
            "mass_slice_width": 0.1,
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


def format_based_peak_parameters(files: Iterable[dict[str, Any]]) -> dict[str, Any]:
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


def read_analysis_csv(path: str | Path) -> dict[str, Any]:
    csv_path = Path(path).expanduser().resolve()
    if not csv_path.is_file():
        raise FileNotFoundError(f"Analysis metadata CSV was not found: {csv_path}")

    files: list[dict[str, Any]] = []
    rejected: list[str] = []
    warnings: list[str] = []
    with csv_path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("Analysis metadata CSV has no header row.")
        header_map = {str(name).strip().casefold(): name for name in reader.fieldnames}
        if "file_path" not in header_map:
            raise ValueError("Analysis metadata CSV requires a file_path column.")

        for line_number, source in enumerate(reader, start=2):
            row = {
                key: source.get(original, "")
                for key, original in header_map.items()
            }
            raw_path = str(row.get("file_path", "")).strip()
            if not raw_path:
                rejected.append(f"line {line_number}: empty file_path")
                continue
            analysis_path = Path(raw_path).expanduser()
            if not analysis_path.is_absolute():
                analysis_path = csv_path.parent / analysis_path
            analysis_path = analysis_path.resolve()
            if not analysis_path.exists():
                rejected.append(f"{analysis_path} (not found; line {line_number})")
                continue
            if not is_supported(analysis_path):
                rejected.append(f"{analysis_path} (unsupported; line {line_number})")
                continue

            format_info = detect_raw_format(analysis_path)
            files.append(
                {
                    "file_path": str(analysis_path),
                    "file_name": str(row.get("file_name", "")).strip() or analysis_path.stem,
                    "file_type": str(row.get("file_type", "")).strip() or "Sample",
                    "class_id": str(row.get("class_id", "")).strip() or "Sample",
                    "acquisition_type": str(row.get("acquisition_type", "")).strip() or "DDA",
                    "batch_order": _csv_number(row.get("batch_order"), 1, int),
                    "analytical_order": _csv_number(
                        row.get("analytical_order"), len(files) + 1, int
                    ),
                    "factor": _csv_number(row.get("factor"), 1, float),
                    **format_info,
                }
            )

    if not files:
        detail = f" Rejected rows: {'; '.join(rejected[:5])}" if rejected else ""
        raise ValueError(f"Analysis metadata CSV contained no usable analysis files.{detail}")
    duplicate_paths = len(files) - len({item["file_path"].casefold() for item in files})
    if duplicate_paths:
        warnings.append(f"The CSV contains {duplicate_paths} duplicate file path(s).")
    return {
        "files": files,
        "warnings": warnings,
        "rejected": rejected,
        "source_csv": str(csv_path),
    }


def _csv_number(value: Any, default: int | float, converter: Any) -> int | float:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return converter(float(text)) if converter is int else converter(text)
    except (TypeError, ValueError):
        return default


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


def read_rt_correction_anchors(path: str | Path) -> list[dict[str, Any]]:
    anchor_path = Path(path).expanduser().resolve()
    if not anchor_path.is_file():
        raise FileNotFoundError(f"RT correction anchor library was not found: {anchor_path}")
    rows: list[dict[str, Any]] = []
    with anchor_path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        next(reader, None)
        for line_number, columns in enumerate(reader, start=2):
            if not columns or not any(str(value).strip() for value in columns):
                continue
            if len(columns) < 7:
                raise ValueError(
                    f"RT correction anchor line {line_number} requires 7 tab-delimited columns."
                )
            try:
                rows.append(
                    {
                        "name": columns[0].strip(),
                        "rt": float(columns[1]),
                        "rt_tolerance": float(columns[2]),
                        "mz": float(columns[3]),
                        "mz_tolerance": float(columns[4]),
                        "minimum_height": float(columns[5]),
                        "include": columns[6].strip().casefold() == "true",
                    }
                )
            except ValueError as error:
                raise ValueError(
                    f"RT correction anchor line {line_number} contains a non-numeric value."
                ) from error
    if not rows:
        raise ValueError("RT correction anchor library contains no entries.")
    return rows


def save_rt_correction_anchors(
    state: dict[str, Any], rows: list[dict[str, Any]]
) -> str:
    output_root = Path(str(state.get("output_root", ""))).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    source_value = str(
        state.get("rt_correction_anchor_source_path")
        or state.get("rt_correction_anchor_path")
        or "rt_correction_anchors"
    ).strip()
    source_stem = Path(source_value).stem or "rt_correction_anchors"
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = output_root / f"{source_stem}_{timestamp}.txt"
    suffix = 2
    while path.exists():
        path = output_root / f"{source_stem}_{timestamp}_{suffix}.txt"
        suffix += 1
    with path.open("w", encoding="ascii", errors="replace", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(
            ["Name", "RT(min)", "RT tol.(min)", "m/z", "m/z tol.", "Minimum height", "T/F"]
        )
        for index, row in enumerate(rows, start=1):
            name = str(row.get("name", "")).strip()
            if not name:
                raise ValueError(f"RT correction anchor row {index} requires a name.")
            rt = float(row.get("rt", 0))
            rt_tolerance = float(row.get("rt_tolerance", 0))
            mz = float(row.get("mz", 0))
            mz_tolerance = float(row.get("mz_tolerance", 0))
            minimum_height = float(row.get("minimum_height", 0))
            if rt < 0 or rt_tolerance <= 0 or mz <= 0 or mz_tolerance <= 0 or minimum_height < 0:
                raise ValueError(
                    f"RT correction anchor row {index} has an invalid RT, tolerance, m/z, or height."
                )
            writer.writerow(
                [
                    name,
                    format(rt, ".15g"),
                    format(rt_tolerance, ".15g"),
                    format(mz, ".15g"),
                    format(mz_tolerance, ".15g"),
                    format(minimum_height, ".15g"),
                    "TRUE" if bool(row.get("include", False)) else "FALSE",
                ]
            )
    return str(path)


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


def load_parameter_template(
    path: str | Path,
    lipid_queries_path: str | Path | None = None,
) -> dict[str, Any]:
    template_path = Path(path).expanduser().resolve()
    if not template_path.is_file():
        raise FileNotFoundError(f"Parameter template not found: {template_path}")
    values = parse_method(template_path)

    def value(*keys: str, default: str = "") -> str:
        return next((values[key] for key in keys if key in values), default)

    def number(*keys: str, default: float = 0.0) -> float:
        try:
            return float(value(*keys, default=str(default)))
        except ValueError:
            return default

    def boolean(*keys: str, default: bool = False) -> bool:
        text = value(*keys, default=str(default)).strip().casefold()
        return text in {"true", "1", "yes", "on"}

    def library_path(*keys: str) -> str:
        text = value(*keys).strip()
        if not text:
            return ""
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = template_path.parent / candidate
        return str(candidate.resolve())

    def annotator_rows(setting_key: str, path_key: str, defaults: dict[str, Any]) -> list[dict[str, Any]]:
        settings_file = library_path(setting_key)
        if not settings_file:
            return []
        settings_path = Path(settings_file)
        if not settings_path.is_file():
            raise FileNotFoundError(f"Annotator settings file not found: {settings_path}")
        rows: list[dict[str, Any]] = []
        with settings_path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
            for index, source in enumerate(csv.DictReader(handle, delimiter="\t"), start=1):
                library_text = str(source.get(path_key, "")).strip()
                library = Path(library_text).expanduser()
                if library_text and not library.is_absolute():
                    library = settings_path.parent / library
                row = dict(defaults)
                row.update(
                    {
                        "annotator_id": str(source.get("annotator_id", "")).strip()
                        or f"{defaults['annotator_id'].rsplit('_', 1)[0]}_{index}",
                        path_key: str(library.resolve()) if library_text else "",
                        "priority": int(float(source.get("priority") or index)),
                        "target_omics": str(source.get("target_omics", "")).strip()
                        or row.get("target_omics", ""),
                        "evidence_tier": str(source.get("evidence_tier", "")).strip()
                        or row.get("evidence_tier", ""),
                    }
                )
                for key in (
                    "rt_tolerance",
                    "ms1_tolerance",
                    "ms2_tolerance",
                    "weighted_dot_product_cutoff",
                    "simple_dot_product_cutoff",
                    "reverse_dot_product_cutoff",
                    "matched_peaks_percentage_cutoff",
                    "minimum_spectrum_match",
                    "total_score_cutoff",
                ):
                    if source.get(key, "").strip():
                        row[key] = float(source[key])
                if "minimum_spectrum_match" in row:
                    row["minimum_spectrum_match"] = int(row["minimum_spectrum_match"])
                row["use_rt_scoring"] = str(
                    source.get("use_retention_information_for_scoring", row.get("use_rt_scoring", False))
                ).casefold() in {"true", "1", "yes", "on"}
                row["use_rt_filtering"] = str(
                    source.get("use_retention_information_for_filtering", row.get("use_rt_filtering", False))
                ).casefold() in {"true", "1", "yes", "on"}
                rows.append(row)
        return rows

    machine = value("machine category", "ionization").casefold()
    project_type = "gcms" if "gc" in machine or value("ionization").casefold() == "ei" else "lcms"
    target_omics = value("target omics", default="Metabolomics")
    workflow_values: dict[str, Any] = {
        "project_type": project_type,
        "ion_mode": value("ion mode", default="Positive"),
        "target_omics": target_omics,
        "ms1_data_type": value("ms1 data type", default="Centroid"),
        "ms2_data_type": value("ms2 data type", default="Centroid"),
        "number_of_threads": int(number("number of threads", default=4)),
        "smoothing_method": value("smoothing method", default="LinearWeightedMovingAverage"),
        "minimum_peak_height": number("minimum peak height", default=300),
        "mass_slice_width": number("mass slice width", default=0.1),
        "minimum_peak_width": int(number("minimum peak width", default=5)),
        "retention_time_begin": number("retention time begin", default=0),
        "retention_time_end": number("retention time end", default=100),
        "ms1_tolerance": number("ms1 tolerance for centroid", default=0.01),
        "ms2_tolerance": number("ms2 tolerance for centroid", default=0.025),
        "alignment_rt_tolerance": number("retention time tolerance for alignment", default=0.1),
        "alignment_ms1_tolerance": number("ms1 tolerance for alignment", default=0.015),
        "alignment_light_mode": boolean("alignment light mode"),
        "export_folder_path": library_path("export folder path"),
        "height_matrix_export": boolean("height matrix export"),
        "solvent": value("solvent type", default="CH3COONH4"),
        "gcms_accuracy_type": value("accuracy type", default="IsNominal"),
        "gcms_ri_compound_type": value("ri compound type", "ri compound", default="Alkanes"),
        "gcms_retention_type": value("retention type", default="RT"),
        "gcms_alignment_index_type": value("alignment index type", default="RT"),
        "gcms_ri_alignment_tolerance": number("retention index alignment tolerance", default=10),
        "gcms_ri_dictionary_path": library_path("ri index file pathes"),
    }

    msp_path = library_path("msp file path")
    msp = {
        "annotator_id": "msp_annotator_1",
        "msp_file_path": msp_path,
        "priority": 1,
        "rt_tolerance": number("rt tolerance for msp-based annotation", default=0.5),
        "use_rt_scoring": boolean("use retention information for msp-based annotation scoring"),
        "use_rt_filtering": boolean("use retention information for msp-based annotation filtering"),
        "weighted_dot_product_cutoff": number("weighted dot product cutoff for msp-based annotation", default=0.6),
        "simple_dot_product_cutoff": number("simple dot product cutoff for msp-based annotation", default=0.6),
        "reverse_dot_product_cutoff": number("reverse dot product cutoff for msp-based annotation", default=0.8),
        "matched_peaks_percentage_cutoff": number("matched peaks percentage cutoff for msp-based annotation", default=0.1),
        "minimum_spectrum_match": int(number("minimum spectrum match for msp-based annotation", default=3)),
    }
    text_path = library_path("text db file path")
    text = {
        "annotator_id": "text_annotator_1",
        "text_db_file_path": text_path,
        "priority": 1,
        "rt_tolerance": number("rt tolerance for text-based annotation", default=0.5),
        "ms1_tolerance": number("accurate ms1 tolerance for text-based annotation", default=0.01),
        "total_score_cutoff": number("total score cutoff for text-based annotation", default=0.8),
        "use_rt_scoring": boolean("use retention information for text-based annotation scoring"),
        "use_rt_filtering": boolean("use retention information for text-based annotation filtering"),
    }
    lbm = {
        "lbm_file_path": library_path("lbm file path"),
        "priority": int(number("lbm annotator priority", "lbm annotation priority", default=1)),
        "rt_tolerance": number("rt tolerance for lbm-based annotation", default=100),
        "ms1_tolerance": number("ms1 tolerance for lbm-based annotation", default=0.01),
        "ms2_tolerance": number("ms2 tolerance for lbm-based annotation", default=0.025),
        "weighted_dot_product_cutoff": number("weighted dot product cutoff for lbm-based annotation", default=0.15),
        "simple_dot_product_cutoff": number("simple dot product cutoff for lbm-based annotation", default=0.15),
        "reverse_dot_product_cutoff": number("reverse dot product cutoff for lbm-based annotation", default=0.3),
        "matched_peaks_percentage_cutoff": number("matched peaks percentage cutoff for lbm-based annotation", default=0),
        "minimum_spectrum_match": int(number("minimum spectrum match for lbm-based annotation", default=1)),
        "use_rt_scoring": boolean("use retention information for lbm-based annotation scoring"),
        "use_rt_filtering": boolean("use retention information for lbm-based annotation filtering"),
    }
    msp_rows = annotator_rows(
        "msp annotator settings file path", "msp_file_path", msp
    )
    text_rows = annotator_rows(
        "text annotator settings file path", "text_db_file_path", text
    )

    searched_lipids = [
        item.strip()
        for item in value("searched lipid class").split(";")
        if item.strip()
    ]
    searched_adducts = [
        item.strip()
        for item in value("searched adduct ions", "adduct list").split(",")
        if item.strip()
    ]
    lipid_queries = read_lipid_queries(lipid_queries_path) if lipid_queries_path else []
    selected_set = set(searched_lipids)
    if target_omics.casefold() == "lipidomics" and not selected_set:
        selected_set = {
            f"{item['lipid_class']} {item['adduct']}" for item in lipid_queries
        }
    for item in lipid_queries:
        item["selected"] = f"{item['lipid_class']} {item['adduct']}" in selected_set

    return {
        "path": str(template_path),
        "workflow": workflow_values,
        "msp_annotators": msp_rows or [msp],
        "text_annotators": text_rows or [text],
        "lbm_annotator": lbm,
        "selected_lipids": searched_lipids,
        "selected_adducts": searched_adducts,
        "lipid_queries": lipid_queries,
    }


def validate_workflow(state: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    files = state.get("files", [])
    project_type = str(state.get("project_type", "lcms")).lower()
    if state.get("run_qa") and project_type == "lcms":
        if not state.get("height_matrix_export"):
            issues.append(
                {
                    "level": "error",
                    "message": "LC-MS QA requires Height matrix export to be enabled.",
                }
            )
        if not str(state.get("export_folder_path", "")).strip():
            issues.append(
                {
                    "level": "warning",
                    "message": (
                        "LC-MS QA Export folder path is blank; Output root will be "
                        "written into the generated method before execution."
                    ),
                }
            )
        console_path = str(state.get("console_path", "")).strip()
        if console_path and LCMS_QA_CAPABILITY not in console_capabilities(console_path)["capabilities"]:
            issues.append(
                {
                    "level": "error",
                    "message": (
                        "The selected MS-DIAL Console does not support LC-MS QA matrix export. "
                        "Choose a Console build containing the LC-MS QA matrix exporter, "
                        "or disable QA matrix export for this run."
                    ),
                }
            )
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
    if state.get("execute_rt_correction"):
        if project_type != "lcms":
            issues.append(
                {
                    "level": "error",
                    "message": "Retention-time correction is currently available only for LC-MS.",
                }
            )
        anchor_path = str(state.get("rt_correction_anchor_path", "")).strip()
        if not anchor_path:
            issues.append(
                {
                    "level": "error",
                    "message": "Set the RT correction anchor library path.",
                }
            )
        elif not Path(anchor_path).exists():
            issues.append(
                {
                    "level": "error",
                    "message": f"RT correction anchor library not found: {anchor_path}",
                }
            )
        selection_path = str(state.get("rt_correction_selection_path", "")).strip()
        if selection_path and not Path(selection_path).exists():
            issues.append(
                {
                    "level": "error",
                    "message": f"RT correction peak selection file not found: {selection_path}",
                }
            )
        selection_mode = str(
            state.get("rt_correction_peak_selection_mode", "HighestIntensity")
        )
        if selection_mode not in {
            "HighestIntensity",
            "ClosestToReferenceRt",
            "Weighted",
        }:
            issues.append(
                {
                    "level": "error",
                    "message": f"Unknown RT correction peak selection mode: {selection_mode}",
                }
            )
        rt_weight = float(state.get("rt_correction_peak_selection_rt_weight", 0.5))
        if not 0 <= rt_weight <= 1:
            issues.append(
                {
                    "level": "error",
                    "message": "RT correction peak selection RT weight must be between 0 and 1.",
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
    if method_state.get("height_matrix_export") and not str(
        method_state.get("export_folder_path", "")
    ).strip():
        method_state["export_folder_path"] = str(run_directory)
    method_state["msdial_console_version"] = console_version(state["console_path"]) or "not recorded"
    method_state["msdial_interactive_version"] = __version__
    repository_metadata_files: dict[str, str] = {}
    if isinstance(state.get("repository_metadata"), dict):
        from .repository_metadata import save_metadata_review

        repository_metadata_files = save_metadata_review(
            state["repository_metadata"], run_directory
        )
        method_state["repository_metadata_files"] = repository_metadata_files
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
        "msdial_console_version": method_state["msdial_console_version"],
        "msdial_interactive_version": method_state["msdial_interactive_version"],
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
        "rt_correction_anchor_file": str(method_state.get("rt_correction_anchor_path", "")),
        "rt_correction_selection_file": str(method_state.get("rt_correction_selection_path", "")),
        "repository_metadata_files": repository_metadata_files,
        "command": command,
        "expected_analysis_exports": expected_analysis_exports,
        "export_folder_path": str(method_state.get("export_folder_path", "")),
        "qa_matrix_expected": bool(
            project_type == "lcms" and method_state.get("height_matrix_export")
        ),
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
        "export_folder_path": str(method_state.get("export_folder_path", "")),
        "qa_matrix_expected": bool(
            project_type == "lcms" and method_state.get("height_matrix_export")
        ),
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
    tuning["execute_rt_correction"] = False
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


def prepare_rt_correction_run(state: dict[str, Any]) -> dict[str, Any]:
    if str(state.get("project_type", "lcms")).lower() != "lcms":
        raise ValueError("Retention-time correction preview is currently available only for LC-MS.")
    files = state.get("files", [])
    if not files:
        raise ValueError("Add at least one LC-MS analysis file.")
    console_path = str(state.get("console_path", "")).strip()
    if not console_path:
        raise ValueError("Set the MS-DIAL Console path.")
    executable = Path(console_path).expanduser().resolve()
    if not executable.is_file():
        raise ValueError(f"MS-DIAL Console not found: {executable}")
    anchor_path = Path(str(state.get("rt_correction_anchor_path", ""))).expanduser().resolve()
    if not anchor_path.is_file():
        raise ValueError(f"RT correction anchor library not found: {anchor_path}")
    output_root = Path(str(state.get("output_root", ""))).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    template_path = Path(str(state.get("template_path", ""))).expanduser().resolve()
    if not template_path.is_file():
        raise ValueError(f"Parameter template not found: {template_path}")

    acquisition_types = {
        str(item.get("acquisition_type", "DDA") or "DDA")
        for item in files
    }
    if len(acquisition_types) != 1:
        raise ValueError("RT correction preview currently requires one common acquisition type.")
    acquisition_type = next(iter(acquisition_types))
    prefix = ["dotnet", str(executable)] if executable.suffix.lower() == ".dll" else [str(executable)]
    eic_path = output_root / "rt_correction_eics.csv"
    features = console_capabilities(str(executable))["capabilities"]
    rt_command = (
        ["rtcorrection"]
        if RT_CORRECTION_REVIEW_CAPABILITY in features
        else ["eic", "rtcorrection"]
    )
    command = prefix + rt_command
    for item in files:
        command.extend(["-i", str(Path(str(item["file_path"])).expanduser().resolve())])
    command.extend(
        [
            "--library",
            str(anchor_path),
            "-o",
            str(eic_path),
            "-m",
            str(template_path),
            "--ionmode",
            str(state.get("ion_mode", "Negative")),
            "--acquisitiontype",
            acquisition_type,
        ]
    )
    selection_input = str(state.get("rt_correction_selection_path", "")).strip()
    if selection_input:
        selection_path = Path(selection_input).expanduser().resolve()
        if not selection_path.is_file():
            raise ValueError(f"RT correction peak selection file not found: {selection_path}")
        command.extend(["--selection", str(selection_path)])
        result_selection = output_root / "rt_correction_peak_selections_applied.tsv"
    else:
        result_selection = output_root / "rt_correction_peak_selections.tsv"
    return {
        "run_directory": str(output_root),
        "analysis_type": "lcms",
        "kind": "rt_correction",
        "command": command,
        "eic_file": str(eic_path),
        "selection_file": str(result_selection),
    }


def parse_rt_correction_result(preparation: dict[str, Any]) -> dict[str, Any]:
    selection_path = Path(preparation["selection_file"])
    eic_path = Path(preparation["eic_file"])
    if not selection_path.is_file():
        raise FileNotFoundError(f"RT correction peak selection file was not generated: {selection_path}")
    if not eic_path.is_file():
        raise FileNotFoundError(f"RT correction EIC file was not generated: {eic_path}")
    run_started_ns = int(preparation.get("run_started_ns", 0))
    if run_started_ns and (
        selection_path.stat().st_mtime_ns < run_started_ns
        or eic_path.stat().st_mtime_ns < run_started_ns
    ):
        raise RuntimeError(
            "The selected MS-DIAL Console did not generate fresh RT correction outputs. "
            "Use a CUI build containing the current rtcorrection implementation: "
            f"{preparation['command'][0]}"
        )

    rows: list[dict[str, Any]] = []
    with selection_path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            rows.append(
                {
                    "file_path": row.get("File path", ""),
                    "file_name": row.get("File name", ""),
                    "standard_id": int(row.get("Standard ID", "0")),
                    "standard_name": row.get("Standard name", ""),
                    "reference_rt": float(row.get("Reference RT (min)", "0")),
                    "detected_rt": float(row.get("Detected RT (min)", "0")),
                    "selected_rt": float(row.get("Selected RT (min)", "0")),
                    "use": str(row.get("Use", "False")).lower() == "true",
                    "peak_height": float(row.get("Peak height", "0")),
                }
            )

    series_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    with eic_path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        required_columns = {"CorrectedRT", "SmoothedIntensity", "RTTolerance"}
        missing_columns = required_columns.difference(reader.fieldnames or [])
        if missing_columns:
            raise RuntimeError(
                "The selected MS-DIAL Console returned the legacy RT correction EIC format "
                f"(missing: {', '.join(sorted(missing_columns))}). "
                "This output cannot display corrected chromatograms. Rebuild or select the "
                f"current CUI: {preparation['command'][0]}"
            )
        for row in reader:
            key = (row.get("FilePath", ""), int(row.get("StandardId", "0")))
            series = series_by_key.setdefault(
                key,
                {
                    "file_path": row.get("FilePath", ""),
                    "file_name": row.get("FileName", ""),
                    "standard_id": key[1],
                    "standard_name": row.get("StandardName", ""),
                    "target_mz": float(row.get("TargetMz", "0")),
                    "reference_rt": float(row.get("ReferenceRT", "0")),
                    "rt_tolerance": float(row.get("RTTolerance", "0")),
                    "rt": [],
                    "corrected_rt": [],
                    "intensity": [],
                    "smoothed_intensity": [],
                },
            )
            series["rt"].append(float(row.get("RT", "0")))
            series["corrected_rt"].append(float(row.get("CorrectedRT", "0")))
            series["intensity"].append(float(row.get("Intensity", "0")))
            series["smoothed_intensity"].append(float(row.get("SmoothedIntensity", "0")))
    return {
        "selection_file": str(selection_path),
        "eic_file": str(eic_path),
        "rows": rows,
        "series": list(series_by_key.values()),
    }


def save_rt_correction_selections(state: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    output_root = Path(str(state.get("output_root", ""))).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    allowed_files = {
        str(Path(str(item["file_path"])).expanduser().resolve()).casefold()
        for item in state.get("files", [])
    }
    path = output_root / "rt_correction_peak_selections_edited.tsv"
    fields = [
        "File path",
        "File name",
        "Standard ID",
        "Standard name",
        "Reference RT (min)",
        "Detected RT (min)",
        "Selected RT (min)",
        "Use",
        "Peak height",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            file_path = str(Path(str(row.get("file_path", ""))).expanduser().resolve())
            if file_path.casefold() not in allowed_files:
                raise ValueError(f"Unexpected analysis file in RT correction selections: {file_path}")
            selected_rt = float(row.get("selected_rt", 0))
            use = bool(row.get("use", False))
            writer.writerow(
                {
                    "File path": file_path,
                    "File name": row.get("file_name", ""),
                    "Standard ID": int(row.get("standard_id", 0)),
                    "Standard name": row.get("standard_name", ""),
                    "Reference RT (min)": float(row.get("reference_rt", 0)),
                    "Detected RT (min)": float(row.get("detected_rt", 0)),
                    "Selected RT (min)": selected_rt if use else 0,
                    "Use": str(use),
                    "Peak height": float(row.get("peak_height", 0)),
                }
            )
    return str(path)


def _write_reproduction_files(
    run_directory: Path,
    state: dict[str, Any],
    command: list[str],
) -> dict[str, str]:
    settings_path = run_directory / "workflow-settings.json"
    settings = {
        key: value
        for key, value in state.items()
        if key not in {"files", "repository_metadata"}
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
            "- RT correction anchor/selection files: included when RT correction is enabled\n"
            "- workflow-settings.json: UI settings used to generate the workflow\n"
            "- *_repository_metadata_reviewed.json / *_sample_metadata_reviewed.tsv: reviewed repository metadata and Class hierarchy\n"
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
            "RT correction paths in method.txt are also absolute; update them after moving the bundle.\n"
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
    if state.get("execute_rt_correction"):
        for key in ("rt_correction_anchor_path", "rt_correction_selection_path"):
            value = str(state.get(key, "")).strip()
            if value and Path(value).is_file():
                members.append(Path(value))
    for value in (state.get("repository_metadata_files") or {}).values():
        path = Path(str(value))
        if path.is_file():
            members.append(path)
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
        "lbm annotator priority": int(state.get("lbm_annotator", {}).get("priority", state.get("lbm_priority", 1))),
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
        "export folder path": state.get("export_folder_path", ""),
        "height matrix export": bool(state.get("height_matrix_export", False)),
        "compounds library file path for rt correction": state.get("rt_correction_anchor_path", ""),
        "rt correction peak selection file path": state.get("rt_correction_selection_path", ""),
        "execute rt correction": bool(state.get("execute_rt_correction", False)),
        "rt correction with smoothing for rt diff": bool(state.get("rt_correction_smooth_rt_diff", False)),
        "user setting intercept": state.get("rt_correction_intercept", 0),
        "rt diff calc method": state.get("rt_correction_diff_method", "SampleMinusSampleAverage"),
        "interpolation method": "Linear",
        "extrapolation method (begin)": state.get("rt_correction_extrapolation_begin", "UserSetting"),
        "extrapolation method (end)": state.get("rt_correction_extrapolation_end", "LastPoint"),
        "rt correction peak selection mode": state.get(
            "rt_correction_peak_selection_mode", "HighestIntensity"
        ),
        "rt correction peak selection rt weight": state.get(
            "rt_correction_peak_selection_rt_weight", 0.5
        ),
    }
    if project_type == "lcms":
        replacements["alignment light mode"] = bool(state.get("alignment_light_mode", False))
        if state.get("annotation_pipeline_profile"):
            replacements["annotation pipeline profile"] = state["annotation_pipeline_profile"]
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
    output_aliases = (
        {
            "retention index alignment tolerance": "retention index tolerance for alignment",
            "weighted dot product cutoff": "square root of weighted dot product cutoff for msp-based annotation",
            "simple dot product cutoff": "square root of simple dot product cutoff for msp-based annotation",
            "reverse dot product cutoff": "square root of reverse dot product cutoff for msp-based annotation",
            "matched peaks percentage cutoff": "matched peaks percentage cutoff for msp-based annotation",
            "minimum spectrum match": "minimum spectrum match for msp-based annotation",
        }
        if project_type == "gcms"
        else {}
    )
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
            output_key = output_aliases.get(matched, matched)
            output.append(f"{_title_for_key(output_key)}: {replacements[matched]}")
            found.add(matched)
            found.add(output_key)
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
        "compounds library file path for rt correction",
        "rt correction peak selection file path",
        "execute rt correction",
        "rt correction with smoothing for rt diff",
        "user setting intercept",
        "rt diff calc method",
        "interpolation method",
        "extrapolation method (begin)",
        "extrapolation method (end)",
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
        "target_omics",
        "evidence_tier",
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
                    "target_omics": str(row.get("target_omics", "")).strip(),
                    "evidence_tier": str(row.get("evidence_tier", "")).strip(),
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
        "annotation pipeline profile": "Annotation pipeline profile",
        "lbm annotator priority": "LBM annotator priority",
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
        "export folder path": "Export folder path",
        "height matrix export": "Height matrix export",
        "compounds library file path for rt correction": "Compounds library file path for RT correction",
        "rt correction peak selection file path": "RT correction peak selection file path",
        "execute rt correction": "Execute RT correction",
        "rt correction with smoothing for rt diff": "RT correction with smoothing for RT diff",
        "user setting intercept": "User setting intercept",
        "rt diff calc method": "RT diff calc method",
        "interpolation method": "Interpolation method",
        "extrapolation method (begin)": "Extrapolation method (begin)",
        "extrapolation method (end)": "Extrapolation method (end)",
        "rt correction peak selection mode": "RT correction peak selection mode",
        "rt correction peak selection rt weight": "RT correction peak selection RT weight",
        "ionization": "Ionization",
        "machine category": "Machine category",
        "accuracy type": "Accuracy type",
        "ri index file pathes": "RI index file pathes",
        "ri compound": "RI compound",
        "ri compound type": "RI compound type",
        "retention type": "Retention type",
        "alignment index type": "Alignment index type",
        "retention index alignment tolerance": "Retention index alignment tolerance",
        "retention index tolerance for alignment": "Retention index tolerance for alignment",
        "weighted dot product cutoff": "Weighted dot product cutoff",
        "simple dot product cutoff": "Simple dot product cutoff",
        "reverse dot product cutoff": "Reverse dot product cutoff",
        "matched peaks percentage cutoff": "Matched peaks percentage cutoff",
        "minimum spectrum match": "Minimum spectrum match",
        "square root of weighted dot product cutoff for msp-based annotation": "Square root of weighted dot product cutoff for MSP-based annotation",
        "square root of simple dot product cutoff for msp-based annotation": "Square root of simple dot product cutoff for MSP-based annotation",
        "square root of reverse dot product cutoff for msp-based annotation": "Square root of reverse dot product cutoff for MSP-based annotation",
        "matched peaks percentage cutoff for msp-based annotation": "Matched peaks percentage cutoff for MSP-based annotation",
        "minimum spectrum match for msp-based annotation": "Minimum spectrum match for MSP-based annotation",
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
            command + ["--version"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (result.stdout + result.stderr).strip()
    match = re.search(r"(?:^|\s)(\d+\.\d+(?:\.\d+)+)(?:\s|$)", text)
    if match:
        return match.group(1)
    try:
        fallback = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = re.search(
        r"(?:Version|Application)\s+([0-9.]+)",
        fallback.stdout + fallback.stderr,
        re.I,
    )
    return match.group(1) if match else ""


def console_capabilities(console_path: str) -> dict[str, Any]:
    path = Path(console_path)
    if not path.is_file():
        return {"capability_probe": "missing", "capabilities": []}
    command = ["dotnet", str(path)] if path.suffix.lower() == ".dll" else [str(path)]
    capabilities: set[str] = set()
    probes: list[str] = []

    # Command availability is already represented by System.CommandLine help;
    # do not require MS-DIAL Console to maintain a separate partial inventory.
    try:
        result = subprocess.run(
            command + ["rtcorrection", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        help_text = result.stdout + result.stderr
        if "--library" in help_text and "--selection" in help_text:
            capabilities.add(RT_CORRECTION_REVIEW_CAPABILITY)
            probes.append("rtcorrection help")

    # QA export is not a standalone command, so recognize the exact exporter
    # message embedded in compatible builds and verify the artifact after a run.
    try:
        binary = path.read_bytes()
    except OSError:
        binary = b""
    marker = "LC-MS quality-assurance matrix:"
    if marker.encode("utf-8") in binary or marker.encode("utf-16-le") in binary:
        capabilities.add(LCMS_QA_CAPABILITY)
        probes.append("QA exporter marker")
    return {
        "capability_probe": " + ".join(probes) if probes else "unsupported",
        "capabilities": sorted(capabilities),
    }

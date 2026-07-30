from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

from .mztab_validation import find_mztab_files, validate_mztab_file


SECTION_HEADERS = {"SMH": "SML", "SFH": "SMF", "SEH": "SME"}
PREVIEW_SECTIONS = ("SML", "SMF", "SME")
NUMERIC_SCAN_LIMIT = 5000
ROW_PREVIEW_LIMIT = 8


def preview_mztab_outputs(run_directory: str | Path, file_path: str | Path | None = None) -> dict[str, Any]:
    target = Path(file_path).expanduser() if file_path else _select_preview_file(run_directory)
    if not target:
        return {
            "run_directory": str(Path(run_directory).expanduser()),
            "status": "warning",
            "message": "No mzTab-M file was found.",
            "file": "",
            "files": [],
            "validation": None,
            "metadata": {},
            "sections": {},
        }
    preview = preview_mztab_file(target)
    preview["run_directory"] = str(Path(run_directory).expanduser())
    preview["files"] = [str(path) for path in find_mztab_files(run_directory)]
    return preview


def preview_mztab_file(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise FileNotFoundError(f"mzTab-M file not found: {target}")

    metadata: dict[str, str] = {}
    headers: dict[str, list[str]] = {}
    sections: dict[str, dict[str, Any]] = {
        name: _empty_section(name) for name in PREVIEW_SECTIONS
    }
    section_stats: dict[str, dict[str, dict[str, Any]]] = {
        name: {} for name in PREVIEW_SECTIONS
    }
    counts: dict[str, int] = {}
    unknown_prefixes: dict[str, int] = {}

    with target.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for line_number, row in enumerate(reader, start=1):
            if not row:
                continue
            prefix = row[0]
            counts[prefix] = counts.get(prefix, 0) + 1
            if prefix == "MTD":
                if len(row) >= 3 and len(metadata) < 80:
                    metadata[row[1]] = row[2]
                continue
            if prefix in SECTION_HEADERS:
                section = SECTION_HEADERS[prefix]
                headers[section] = row[1:]
                sections[section]["columns"] = row[1:]
                continue
            if prefix in sections:
                section = sections[prefix]
                values = row[1:]
                section["row_count"] += 1
                if len(section["rows"]) < ROW_PREVIEW_LIMIT:
                    section["rows"].append(_row_preview(headers.get(prefix, []), values))
                if section["row_count"] <= NUMERIC_SCAN_LIMIT:
                    _update_section_stats(section_stats[prefix], headers.get(prefix, []), values)
                continue
            if prefix not in {"COM"}:
                unknown_prefixes[prefix] = unknown_prefixes.get(prefix, 0) + 1

    for section_name, section in sections.items():
        stats = section_stats[section_name]
        section["numeric_columns"] = _summarize_numeric_columns(stats)
        section["suggested_columns"] = _suggest_columns(section.get("columns", []))
        section["preview_limited_to_rows"] = NUMERIC_SCAN_LIMIT

    validation = validate_mztab_file(target)
    status = validation.get("status", "unknown")
    return {
        "status": status,
        "file": str(target),
        "file_name": target.name,
        "file_size_bytes": target.stat().st_size,
        "validation": validation,
        "metadata": metadata,
        "counts": dict(sorted(counts.items())),
        "unknown_prefixes": dict(sorted(unknown_prefixes.items())),
        "sections": sections,
    }


def _select_preview_file(run_directory: str | Path) -> Path | None:
    files = find_mztab_files(run_directory)
    existing = [path for path in files if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def _empty_section(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "row_count": 0,
        "columns": [],
        "rows": [],
        "numeric_columns": [],
        "suggested_columns": {},
    }


def _row_preview(columns: list[str], values: list[str]) -> dict[str, str]:
    if columns:
        pairs = zip(columns, values)
        return {key: value for key, value in pairs}
    return {f"column_{index + 1}": value for index, value in enumerate(values)}


def _update_section_stats(
    stats: dict[str, dict[str, Any]],
    columns: list[str],
    values: list[str],
) -> None:
    for index, value in enumerate(values):
        column = columns[index] if index < len(columns) else f"column_{index + 1}"
        item = stats.setdefault(
            column,
            {
                "seen": 0,
                "numeric": 0,
                "missing": 0,
                "min": None,
                "max": None,
                "sum": 0.0,
            },
        )
        item["seen"] += 1
        text = value.strip()
        if not text or text.lower() in {"null", "na", "nan"}:
            item["missing"] += 1
            continue
        try:
            number = float(text)
        except ValueError:
            continue
        if not math.isfinite(number):
            item["missing"] += 1
            continue
        item["numeric"] += 1
        item["sum"] += number
        item["min"] = number if item["min"] is None else min(item["min"], number)
        item["max"] = number if item["max"] is None else max(item["max"], number)


def _summarize_numeric_columns(stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    for name, item in stats.items():
        seen = item["seen"] or 1
        if item["numeric"] == 0:
            continue
        columns.append(
            {
                "name": name,
                "numeric_count": item["numeric"],
                "missing_count": item["missing"],
                "missing_rate": round(item["missing"] / seen, 4),
                "min": item["min"],
                "max": item["max"],
                "mean": round(item["sum"] / item["numeric"], 6),
            }
        )
    columns.sort(
        key=lambda item: (
            _column_priority(item["name"]),
            -item["numeric_count"],
            item["name"],
        )
    )
    return columns[:30]


def _suggest_columns(columns: list[str]) -> dict[str, list[str]]:
    groups = {
        "abundance": ["abundance", "intensity", "height", "area"],
        "retention": ["retention_time", "retention", "rt_", "_rt", "ri_", "_ri"],
        "mass": ["mass_to_charge", "mz", "m/z", "mass"],
        "annotation": ["chemical", "identifier", "database", "smiles", "inchi", "formula"],
        "quality": ["opt_", "score", "reliability", "best_id"],
    }
    result: dict[str, list[str]] = {}
    lowered = [(column, column.lower()) for column in columns]
    for group, needles in groups.items():
        result[group] = [
            column
            for column, lower in lowered
            if any(needle in lower for needle in needles)
        ][:20]
    return result


def _column_priority(name: str) -> int:
    lower = name.lower()
    if "abundance" in lower or "height" in lower or "area" in lower:
        return 0
    if "score" in lower or "best_id" in lower:
        return 1
    if "retention" in lower or lower in {"rt", "ri"}:
        return 2
    if "mz" in lower or "mass" in lower:
        return 3
    return 9

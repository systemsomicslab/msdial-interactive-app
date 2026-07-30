from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


MZTAB_SUFFIXES = {".mztab", ".mztabm"}
KNOWN_PREFIXES = {"MTD", "SMH", "SML", "SFH", "SMF", "SEH", "SME", "COM"}
HEADER_TO_DATA = {
    "SMH": "SML",
    "SFH": "SMF",
    "SEH": "SME",
}


def find_mztab_files(run_directory: str | Path, limit: int = 200) -> list[Path]:
    if not str(run_directory).strip():
        return []
    root = Path(run_directory).expanduser().resolve()
    if root.is_file():
        return [root] if _looks_like_mztab(root) else []
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and _looks_like_mztab(path):
            files.append(path)
            if len(files) >= limit:
                break
    return sorted(files)


def list_mztab_outputs(run_directory: str | Path) -> dict[str, Any]:
    files = find_mztab_files(run_directory)
    items = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "file": str(path),
                "file_name": path.name,
                "modified_time": stat.st_mtime,
                "modified_time_iso": _modified_time_iso(stat.st_mtime),
                "size_bytes": stat.st_size,
            }
        )
    items.sort(key=lambda item: item["modified_time"], reverse=True)
    if items:
        items[0]["is_default"] = True
    for item in items[1:]:
        item["is_default"] = False
    return {
        "run_directory": str(Path(run_directory).expanduser()),
        "files": items,
        "default_file": items[0]["file"] if items else "",
    }


def validate_mztab_outputs(run_directory: str | Path) -> dict[str, Any]:
    files = [validate_mztab_file(path) for path in find_mztab_files(run_directory)]
    summary = {
        "status": "passed",
        "passed": sum(1 for item in files if item["status"] == "passed"),
        "warnings": sum(1 for item in files if item["status"] == "warning"),
        "failed": sum(1 for item in files if item["status"] == "failed"),
        "file_count": len(files),
    }
    if not files:
        summary.update({"status": "warning", "warnings": 1})
    elif summary["failed"]:
        summary["status"] = "failed"
    elif summary["warnings"]:
        summary["status"] = "warning"

    external = _external_validator_status()
    return {
        "run_directory": str(Path(run_directory).expanduser()),
        "summary": summary,
        "status": summary["status"],
        "files": files,
        "external_validator": external,
    }


def validate_mztab_file(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    counts: dict[str, int] = {}
    header_widths: dict[str, int] = {}
    unknown_prefixes: dict[str, int] = {}
    column_mismatch_examples: dict[tuple[str, int, int], list[int]] = {}

    if not target.is_file():
        return _result(target, "failed", ["File does not exist."], [], counts)
    if target.stat().st_size == 0:
        return _result(target, "failed", ["File is empty."], [], counts)

    try:
        with target.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                parts = line.split("\t")
                prefix = parts[0]
                counts[prefix] = counts.get(prefix, 0) + 1
                if prefix in HEADER_TO_DATA:
                    header_widths[prefix] = len(parts)
                elif prefix in HEADER_TO_DATA.values():
                    header_prefix = next(
                        key for key, value in HEADER_TO_DATA.items() if value == prefix
                    )
                    expected = header_widths.get(header_prefix)
                    if expected is not None and len(parts) != expected:
                        key = (prefix, len(parts), expected)
                        column_mismatch_examples.setdefault(key, []).append(line_number)
                elif prefix in KNOWN_PREFIXES:
                    if len(parts) == 1 and prefix != "COM":
                        errors.append(f"Line {line_number}: {prefix} line has no tab-separated fields.")
                else:
                    unknown_prefixes[prefix] = unknown_prefixes.get(prefix, 0) + 1
    except OSError as error:
        return _result(target, "failed", [str(error)], warnings, counts)

    if counts.get("MTD", 0) == 0:
        errors.append("No MTD metadata section was found.")
    if not _has_metadata_key(target, "mzTab-version"):
        errors.append("MTD mzTab-version is missing.")
    if counts.get("SMH", 0) == 0:
        warnings.append("No SMH small molecule header was found.")
    if counts.get("SML", 0) == 0:
        warnings.append("No SML small molecule data rows were found.")
    for (prefix, observed, expected), lines in sorted(column_mismatch_examples.items()):
        examples = ", ".join(str(line) for line in lines[:5])
        suffix = "" if len(lines) <= 5 else f", ... ({len(lines)} lines total)"
        warnings.append(
            f"{prefix} column count {observed} differs from its header count {expected}; "
            f"example line(s): {examples}{suffix}."
        )
    if unknown_prefixes:
        examples = ", ".join(
            f"{key} ({value})" for key, value in sorted(unknown_prefixes.items())[:5]
        )
        warnings.append(f"Unknown mzTab line prefixes were found: {examples}.")

    status = "failed" if errors else "warning" if warnings else "passed"
    return _result(target, status, errors, warnings, counts)


def _looks_like_mztab(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix.lower() in MZTAB_SUFFIXES or name.endswith(".mztab.txt") or "mztab" in name


def _modified_time_iso(timestamp: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(timestamp).astimezone().isoformat()


def _has_metadata_key(path: Path, key: str) -> bool:
    prefix = f"MTD\t{key}"
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return any(line.startswith(prefix) for line in handle)


def _external_validator_status() -> dict[str, Any]:
    available = importlib.util.find_spec("mztab_m_io") is not None
    if available:
        message = "pymzTab-m parser package is installed; built-in structural check was used for this run."
    else:
        message = "Not installed; built-in structural check was used. No extra environment setup is required."
    return {
        "mode": "builtin",
        "pymztab_m_available": available,
        "message": message,
    }


def _result(
    path: Path,
    status: str,
    errors: list[str],
    warnings: list[str],
    counts: dict[str, int],
) -> dict[str, Any]:
    return {
        "file": str(path),
        "file_name": path.name,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "counts": dict(sorted(counts.items())),
    }

"""Read the Console's alignment-only RT correction audit without changing its results."""

import csv
import hashlib
import json
import math
import subprocess
import tempfile
from collections import Counter
from pathlib import Path


SUMMARY = "automatic_alignment_rt_correction_summary.tsv"
ANCHORS = "automatic_alignment_rt_correction_anchors.tsv"
SUMMARY_COLUMNS = {"File ID", "File name", "File type", "Analytical order", "Used anchors", "Model source"}
ANCHOR_COLUMNS = {"File ID", "Anchor ID", "m/z", "Reference RT (min)", "Original RT (min)", "Quality score", "Used", "Status"}
MAX_AUDIT_BYTES = 32 * 1024 * 1024
MAX_ROWS = 200_000
SELECTION_FIELDS = (
    ("Automatic RT correction intensity quantile", "Intensity quantile"),
    ("Automatic RT correction maximum peak width quantile", "Maximum peak-width quantile"),
    ("Automatic RT correction minimum signal to noise", "Minimum S/N"),
    ("Automatic RT correction minimum Gaussian similarity", "Minimum Gaussian similarity"),
    ("Automatic RT correction minimum ideal slope", "Minimum ideal slope"),
    ("Automatic RT correction match RT tolerance", "Cross-file RT tolerance (min)"),
    ("Automatic RT correction minimum sample coverage", "Minimum non-Blank coverage"),
)


def _method_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().casefold()] = value.strip()
    return values


def _smooth_eic(points: list[dict], method: str, level: int) -> list[float]:
    """Preview the CommonStandard moving averages on the full, original RT axis."""
    if method not in {"LinearWeightedMovingAverage", "TimeBasedLinearWeightedMovingAverage", "SimpleMovingAverage"}:
        raise ValueError(f"Smoothing preview is not implemented for {method or 'an unrecorded method'}; raw EIC is retained.")
    if not 0 <= level <= 100:
        raise ValueError("Smoothing preview requires an integer level between 0 and 100.")
    if not points:
        return []
    intensities = [point["intensity"] for point in points]
    if method != "TimeBasedLinearWeightedMovingAverage":
        weights = [level + 1 - abs(offset) if method == "LinearWeightedMovingAverage" else 1
                   for offset in range(-level, level + 1)]
        denominator = sum(weights)
        return [sum(weight * (intensities[i + offset] if 0 <= i + offset < len(points) else intensities[i])
                    for offset, weight in zip(range(-level, level + 1), weights)) / denominator
                for i in range(len(points))]
    if len(points) == 1:
        return intensities
    times = [point["original_rt"] for point in points]
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError("Time-based smoothing preview requires strictly increasing MS1 retention times.")
    interval = (times[-1] - times[0]) / (len(times) - 1)
    distance = level + 1
    result, start = [], 0
    for time in times:
        lo, hi = time - interval * distance, time + interval * distance
        while start < len(times) and times[start] <= lo:
            start += 1
        numerator = denominator = 0.0
        for index in range(start, len(times)):
            if times[index] >= hi:
                break
            weight = distance - abs(time - times[index]) / interval
            numerator += intensities[index] * weight
            denominator += weight
        result.append(numerator / denominator)
    return result


def _table(path: Path, required: set[str]) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"RT correction audit not found: {path.name}. Run LC-MS with automatic alignment RT correction enabled.")
    if path.stat().st_size > MAX_AUDIT_BYTES:
        raise ValueError(f"RT correction audit {path.name} exceeds the review limit.")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name} is missing columns: {', '.join(sorted(missing))}")
        rows = []
        for row in reader:
            if len(rows) >= MAX_ROWS:
                raise ValueError(f"{path.name} has too many rows for interactive review.")
            rows.append(row)
    return rows


def _number(row: dict[str, str], column: str) -> float | None:
    try:
        value = float(row.get(column, ""))
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def read_automatic_rt_review(directory: str | Path) -> dict:
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Output directory not found: {root}")
    summary_path, anchors_path = root / SUMMARY, root / ANCHORS
    summary_rows = _table(summary_path, SUMMARY_COLUMNS)
    anchor_rows = _table(anchors_path, ANCHOR_COLUMNS)
    if not summary_rows:
        raise ValueError("RT correction summary contains no analysis files.")

    files = []
    file_ids = set()
    for row in summary_rows:
        file_id = row["File ID"].strip()
        if not file_id or file_id in file_ids:
            raise ValueError("RT correction summary contains an empty or duplicate File ID.")
        file_ids.add(file_id)
        files.append({
            "file_id": file_id,
            "name": row["File name"],
            "type": row["File type"],
            "order": _number(row, "Analytical order"),
            "candidate_count": _number(row, "Candidate count"),
            "matched_anchors": _number(row, "Matched anchors"),
            "used_anchors": _number(row, "Used anchors"),
            "model_source": row["Model source"],
            "reference_score": _number(row, "Reference score"),
            "median_absolute_offset": _number(row, "Median absolute offset (min)"),
            "maximum_absolute_offset": _number(row, "Maximum absolute offset (min)"),
            "note": row.get("Note", ""),
        })

    anchors = []
    anchors_by_file = {file_id: [] for file_id in file_ids}
    for row in anchor_rows:
        file_id = row["File ID"].strip()
        if file_id not in file_ids:
            raise ValueError(f"RT correction anchor refers to unknown file ID {file_id}.")
        original = _number(row, "Original RT (min)")
        reference = _number(row, "Reference RT (min)")
        offset = reference - original if original is not None and reference is not None else None
        if offset is not None and not math.isfinite(offset):
            offset = None
        anchor = {
            "file_id": file_id,
            "anchor_id": row["Anchor ID"].strip(),
            "mz": _number(row, "m/z"),
            "reference_rt": reference,
            "original_rt": original,
            "offset": offset,
            "rt_available": original is not None and reference is not None and offset is not None,
            "quality_score": _number(row, "Quality score"),
            "coverage": _number(row, "Non-Blank sample coverage"),
            "used": row["Used"].strip().casefold() == "true",
            "status": row["Status"],
        }
        anchors.append(anchor)
        anchors_by_file[file_id].append(anchor)

    for file in files:
        used = [item for item in anchors_by_file[file["file_id"]] if item["used"]]
        file["model_reconstructable"] = (
            file["model_source"] in {"Reference", "DetectedAnchors"}
            and all(item["rt_available"] for item in used)
            and len({item["original_rt"] for item in used}) >= 2
        )

    model_counts = dict(Counter(item["model_source"] for item in files))
    reference = next((item for item in files if item["model_source"] == "Reference"), None)
    rejected = Counter(item["status"] for item in anchors if not item["used"])
    warnings = []
    if not reference:
        warnings.append("No reference file is recorded. Do not interpret the shift curves as validated correction.")
    if model_counts.get("Uncorrected"):
        warnings.append(f"{model_counts['Uncorrected']} file(s) remained on their original RT axis.")
    if rejected:
        warnings.append("Rejected anchors require review: " + ", ".join(f"{key} ({value})" for key, value in sorted(rejected.items())))
    missing_rt = [item for item in anchors if not item["rt_available"]]
    if missing_rt:
        warnings.append(f"{len(missing_rt)} anchor record(s) have unavailable RT values. They remain in the table as N/A and are excluded from numeric plots; no RT values are imputed.")
    invalid_used_files = sorted({item["file_id"] for item in missing_rt if item["used"]})
    if invalid_used_files:
        warnings.append("Anchors marked Used have unavailable RT values in File ID(s) " + ", ".join(invalid_used_files) + ". Their correction models cannot be reconstructed safely.")
    if any(item["model_source"] in {"InterpolatedBlank", "NearestBlank"} for item in files):
        warnings.append("Blank models are derived from neighboring samples; their control points are not recorded in the anchor TSV, so the viewer does not reconstruct their shift curves.")

    method_audit = {"status": "missing", "applied_count": 0, "unrecognised": [], "unusable": [], "blank": []}
    keys_path, method_path = root / "method.keys.json", root / "method.txt"
    if keys_path.is_file() and method_path.is_file():
        try:
            record = json.loads(keys_path.read_text(encoding="utf-8-sig"))
            digest = hashlib.sha256(method_path.read_bytes()).hexdigest()
            status = "verified" if record.get("method_file_sha256", "").casefold() == digest else "method_mismatch"
            if status == "verified" and min(summary_path.stat().st_mtime, anchors_path.stat().st_mtime) < keys_path.stat().st_mtime:
                status = "stale_rt_audit"
            method_audit = {
                "status": status,
                "applied_count": len(record.get("applied") or []),
                "unrecognised": list(record.get("unrecognised") or []),
                "unusable": list(record.get("unusable") or []),
                "blank": list(record.get("blank") or []),
                "automatic_rt_key_applied": "Execute automatic RT correction for alignment" in (record.get("applied") or []),
            }
        except (OSError, ValueError, AttributeError):
            method_audit["status"] = "unreadable"
    if method_audit["status"] != "verified":
        warnings.append(f"Run provenance is not verified ({method_audit['status']}); audit files may belong to an earlier run.")
    elif not method_audit["automatic_rt_key_applied"]:
        warnings.append("The Console did not record the automatic RT correction method key as applied.")
    if method_audit["unusable"]:
        warnings.append(f"{len(method_audit['unusable'])} method parameter value(s) were rejected by the Console.")

    method_values = _method_values(method_path)
    return {
        "run_directory": str(root),
        "summary_file": str(summary_path),
        "anchors_file": str(anchors_path),
        "files": files,
        "anchors": anchors,
        "reference": reference,
        "model_counts": model_counts,
        "rejected_status_counts": dict(rejected),
        "method_audit": method_audit,
        "selection_settings": [{"label": label, "value": method_values.get(key.casefold()) or None}
                               for key, label in SELECTION_FIELDS],
        "smoothing_settings": {
            "method": method_values.get("smoothing method") or None,
            "level": method_values.get("smoothing level") or None,
            "ms1_tolerance": method_values.get("ms1 tolerance for centroid") or None,
        },
        "warnings": warnings,
    }


def _correct_rt(rt: float, anchors: list[dict]) -> float | None:
    if not math.isfinite(rt) or any(item["used"] and not item["rt_available"] for item in anchors):
        return None
    by_rt: dict[float, dict] = {}
    for anchor in anchors:
        if not anchor["used"]:
            continue
        original = anchor["original_rt"]
        if original not in by_rt or (anchor["quality_score"] or 0) > (by_rt[original]["quality_score"] or 0):
            by_rt[original] = anchor
    points = sorted(by_rt.values(), key=lambda item: item["original_rt"])
    if len(points) < 2:
        return None
    if rt <= points[0]["original_rt"]:
        return rt + points[0]["offset"]
    if rt >= points[-1]["original_rt"]:
        return rt + points[-1]["offset"]
    for left, right in zip(points, points[1:]):
        if rt <= right["original_rt"]:
            fraction = (rt - left["original_rt"]) / (right["original_rt"] - left["original_rt"])
            return left["reference_rt"] + fraction * (right["reference_rt"] - left["reference_rt"])
    return None


def extract_anchor_eic(directory: str | Path, file_id: str, anchor_id: str, tolerance: float) -> dict:
    """Extract one chromatogram on demand; never copy or retain the vendor raw file."""
    if not math.isfinite(tolerance) or tolerance <= 0 or tolerance > 1:
        raise ValueError("EIC m/z tolerance must be greater than 0 and at most 1 Da.")
    review = read_automatic_rt_review(directory)
    root = Path(review["run_directory"])
    file = next((item for item in review["files"] if item["file_id"] == str(file_id)), None)
    anchor = next((item for item in review["anchors"] if item["file_id"] == str(file_id) and item["anchor_id"] == str(anchor_id)), None)
    if not file or not anchor or anchor["mz"] is None:
        raise ValueError("Select a file and an anchor recorded in this run's RT audit.")
    if not anchor["rt_available"]:
        raise ValueError(f"Anchor {anchor_id} has no finite original/reference RT ({anchor['status']}); no apex is available for this EIC review.")
    if file["model_source"] not in {"Reference", "DetectedAnchors"}:
        raise ValueError("The Console did not retain a reconstructable RT model for this file.")
    if not file["model_reconstructable"]:
        raise ValueError("The accepted anchors do not provide a complete, reconstructable RT model for this file.")
    if review["method_audit"]["status"] != "verified":
        raise ValueError("Run provenance must be verified before extracting an EIC against its RT model.")

    input_csv = root / "analysis_files.csv"
    with input_csv.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    try:
        raw_row = rows[int(file_id)]
    except (IndexError, ValueError):
        raise ValueError("File ID is absent from this run's analysis_files.csv.") from None
    if raw_row.get("file_name", "").strip() != file["name"]:
        raise ValueError("The input CSV no longer matches the RT audit's file ordering.")
    raw_path = Path(raw_row["file_path"]).expanduser().resolve()
    if not raw_path.exists():
        raise FileNotFoundError(f"The original raw file is unavailable: {raw_path}")
    manifest = json.loads((root / "run-manifest.json").read_text(encoding="utf-8-sig"))
    console = Path(str((manifest.get("console") or {}).get("path") or "")).expanduser().resolve()
    if not console.is_file() or console.name.casefold() not in {"msdialcui.exe", "msdialcui.dll"}:
        raise FileNotFoundError("The Console used by this run is unavailable; select a valid run manifest.")
    acquisition = raw_row.get("acquisition_type", "DDA").strip()
    if acquisition not in {"DDA", "SWATH", "AIF", "None"}:
        acquisition = "DDA"

    with tempfile.TemporaryDirectory(prefix=".rt-eic-", dir=root) as temporary:
        output = Path(temporary) / "anchor_eic.csv"
        command = (["dotnet", str(console)] if console.suffix.lower() == ".dll" else [str(console)]) + [
            "eic", "raw", "--input", str(raw_path), "--output", str(output),
            "-target", str(anchor["mz"]), "-target", str(tolerance), "--acquisitiontype", acquisition,
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=300, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("EIC extraction exceeded five minutes. Try another representative raw file.") from None
        if completed.returncode != 0 or not output.is_file():
            detail = (completed.stderr or completed.stdout or "No EIC was produced.").strip()[-1200:]
            raise RuntimeError(f"MS-DIAL EIC extraction failed: {detail}")
        full_points = []
        model_anchors = [item for item in review["anchors"] if item["file_id"] == file_id]
        window = max(0.25, min(1.0, abs(anchor["offset"]) * 5 + 0.25))
        with output.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    rt, intensity = float(row["RT"]), float(row["Intensity"])
                except (KeyError, ValueError):
                    continue
                if not (math.isfinite(rt) and math.isfinite(intensity)):
                    continue
                full_points.append({"original_rt": rt, "intensity": intensity})
                if len(full_points) > 1_000_000:
                    raise ValueError("EIC contains more than 1,000,000 points; interactive smoothing review is bounded.")
        smoothing = {**review["smoothing_settings"], "available": False, "note": ""}
        smoothed = None
        try:
            level = int(smoothing["level"])
            # Match the recorded method only; never substitute another smoother silently.
            invalid = {str(key).casefold() for key in review["method_audit"]["unusable"]}
            if invalid & {"smoothing method", "smoothing level"}:
                raise ValueError("The Console rejected a smoothing setting; no smoothed preview is inferred.")
            smoothed = _smooth_eic(full_points, smoothing["method"], level)
            smoothing["available"] = True
            smoothing["level"] = level
            smoothing["note"] = "Reconstructed for this review EIC using the recorded method, before cropping and RT projection. This does not replace the stored detection metrics."
        except (TypeError, ValueError) as error:
            smoothing["note"] = str(error) if smoothing["level"] is not None else "No smoothing level was recorded; only the raw EIC is shown."
        points = []
        for index, point in enumerate(full_points):
            rt = point["original_rt"]
            if abs(rt - anchor["original_rt"]) > window:
                continue
            points.append({**point, "corrected_rt": _correct_rt(rt, model_anchors),
                           "smoothed_intensity": smoothed[index] if smoothed is not None else None})
            if len(points) > 10_000:
                raise ValueError("EIC review window contains more than 10,000 points.")
    return {
        "file_id": file_id,
        "file_name": file["name"],
        "anchor_id": anchor_id,
        "mz": anchor["mz"],
        "tolerance": tolerance,
        "original_rt": anchor["original_rt"],
        "reference_rt": anchor["reference_rt"],
        "raw_file": str(raw_path),
        "smoothing": smoothing,
        "quality_score": anchor["quality_score"],
        "coverage": anchor["coverage"],
        "points": points,
    }

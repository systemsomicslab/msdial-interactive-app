from __future__ import annotations

import csv
import heapq
import math
import random
from pathlib import Path
from statistics import median
from typing import Any, Iterable
from .diagnostic_paths import is_diagnostic_artifact


QA_SUFFIX = ".qa.tsv"
PCA_FEATURE_LIMIT = 1000
PCA_SAMPLE_LIMIT = 500
RESERVOIR_LIMIT = 5000


def find_qa_files(
    path: str | Path, limit: int = 200, *, recursive: bool = False
) -> list[Path]:
    root = Path(path).expanduser()
    if root.is_file():
        return [root.resolve()] if root.name.lower().endswith(QA_SUFFIX) else []
    if not root.is_dir():
        return []
    patterns = [f"*{QA_SUFFIX}"]
    if recursive:
        # QA exports are written either in the run root or one generated QA
        # directory below it. Avoid traversing vendor RAW directory contents.
        patterns.append(f"*/*{QA_SUFFIX}")
    files = {
        item.resolve()
        for pattern in patterns
        for item in root.glob(pattern)
        if item.is_file() and not is_diagnostic_artifact(item)
    }
    files = list(files)
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return files[:limit]


def build_lcms_qa_report(
    path: str | Path,
    internal_standards: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    qa_files = find_qa_files(path)
    if not qa_files:
        raise FileNotFoundError(f"No LC-MS QA matrix (*{QA_SUFFIX}) was found in {Path(path).expanduser()}.")
    target = qa_files[0]
    standards = [_normalize_standard(item, index) for index, item in enumerate(internal_standards or [])]
    standards = [item for item in standards if item]

    samples: dict[str, dict[str, Any]] = {}
    sample_order: list[str] = []
    sample_reservoirs: dict[str, _Reservoir] = {}
    sn_reservoirs: dict[str, _Reservoir] = {}
    type_reservoirs: dict[str, _Reservoir] = {}
    qc_rsds = _Reservoir()
    qc_detection_rates = _Reservoir()
    blank_ratios = _Reservoir()
    pca_heap: list[tuple[float, int, list[float]]] = []
    standard_matches: list[dict[str, Any] | None] = [None] * len(standards)
    spot_count = 0
    pca_indices: list[int] = []
    row_counter = 0

    with target.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        _validate_headers(reader.fieldnames or [])
        msms_available = "MSMS" in (reader.fieldnames or [])
        current_id: str | None = None
        spot_rows: list[dict[str, Any]] = []
        for raw in reader:
            spot_id = str(raw.get("ID", "")).strip()
            if current_id is not None and spot_id != current_id:
                if not sample_order:
                    sample_order = [str(row["file"]) for row in spot_rows]
                    pca_indices = _select_pca_indices(sample_order, samples)
                row_counter += 1
                _consume_spot(
                    current_id,
                    spot_rows,
                    sample_order,
                    pca_indices,
                    samples,
                    sample_reservoirs,
                    sn_reservoirs,
                    type_reservoirs,
                    qc_rsds,
                    qc_detection_rates,
                    blank_ratios,
                    pca_heap,
                    row_counter,
                    standards,
                    standard_matches,
                )
                spot_count += 1
                spot_rows = []
            row = _parse_row(raw)
            _register_sample(row, samples, sample_reservoirs, sn_reservoirs, type_reservoirs)
            spot_rows.append(row)
            current_id = spot_id
        if spot_rows:
            if not sample_order:
                sample_order = [str(row["file"]) for row in spot_rows]
                pca_indices = _select_pca_indices(sample_order, samples)
            row_counter += 1
            _consume_spot(
                current_id or "",
                spot_rows,
                sample_order,
                pca_indices,
                samples,
                sample_reservoirs,
                sn_reservoirs,
                type_reservoirs,
                qc_rsds,
                qc_detection_rates,
                blank_ratios,
                pca_heap,
                row_counter,
                standards,
                standard_matches,
            )
            spot_count += 1

    sample_summaries = _summarize_samples(samples, sample_reservoirs, sn_reservoirs, msms_available)
    pca_files = [sample_order[index] for index in pca_indices]
    pca = _compute_pca([item[2] for item in pca_heap], pca_files, samples)
    summary = _summarize_report(
        sample_summaries,
        spot_count,
        qc_rsds.values,
        qc_detection_rates.values,
        blank_ratios.values,
    )
    summary.update(_pca_qc_metrics(pca))
    return {
        "status": "ok",
        "file": str(target),
        "file_name": target.name,
        "summary": summary,
        "samples": sample_summaries,
        "intensity_distributions": _histograms(type_reservoirs),
        "pca": pca,
        "internal_standards": _summarize_internal_standards(standards, standard_matches),
        "warnings": _warnings(sample_summaries, pca),
        "method": {
            "intensity_transform": "log10(height + 1)",
            "pca": "mean-centered log10 height; up to 1000 highest-variance features and 500 samples",
            "qc_precision": "feature-wise RSD among positive QC detections, reported with QC detection rate",
            "blank_separation": "sample median height / blank median height for each feature",
            "carryover": "blank total height / immediately preceding injection total height",
            "qc_topology": "median pairwise PCA distance among QCs / median pairwise distance among all displayed samples",
            "msms_acquisition": "features with an assigned MS/MS spectrum / detected features in each sample",
            "sn_distribution": "median and interquartile range of positive raw (not log-transformed) feature S/N values in each sample",
        },
    }


def build_lcms_qa_report_from_file(
    path: str | Path,
    internal_standards: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if not target.is_file() or not target.name.lower().endswith(QA_SUFFIX):
        raise FileNotFoundError(f"LC-MS QA matrix not found: {target}")
    return build_lcms_qa_report(target, internal_standards)


class _Reservoir:
    def __init__(self, limit: int = RESERVOIR_LIMIT, seed: int = 17) -> None:
        self.limit = limit
        self.values: list[float] = []
        self.seen = 0
        self.random = random.Random(seed)

    def add(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.seen += 1
        if len(self.values) < self.limit:
            self.values.append(value)
            return
        index = self.random.randrange(self.seen)
        if index < self.limit:
            self.values[index] = value


def _validate_headers(headers: list[str]) -> None:
    required = {"ID", "File", "Class", "File type", "Injection order", "Batch ID", "Height", "RT", "MZ", "Reference matched"}
    missing = sorted(required.difference(headers))
    if missing:
        raise ValueError(f"LC-MS QA matrix is missing columns: {', '.join(missing)}")


def _parse_row(raw: dict[str, str]) -> dict[str, Any]:
    return {
        "id": str(raw.get("ID", "")).strip(),
        "file": str(raw.get("File", "")).strip(),
        "class": str(raw.get("Class", "")).strip(),
        "file_type": str(raw.get("File type", "Sample")).strip() or "Sample",
        "order": _integer(raw.get("Injection order")),
        "batch": _integer(raw.get("Batch ID"), 1),
        "height": max(0.0, _number(raw.get("Height"))),
        "rt": _number(raw.get("RT")),
        "mz": _number(raw.get("MZ")),
        "sn": _number(raw.get("SN")),
        "msms": str(raw.get("MSMS", "")).strip().lower() == "true",
        "reference_matched": str(raw.get("Reference matched", "")).strip().lower() == "true",
    }


def _register_sample(
    row: dict[str, Any],
    samples: dict[str, dict[str, Any]],
    sample_reservoirs: dict[str, _Reservoir],
    sn_reservoirs: dict[str, _Reservoir],
    type_reservoirs: dict[str, _Reservoir],
) -> None:
    name = row["file"]
    if name in samples:
        return
    category = _sample_category(row["file_type"], row["class"])
    samples[name] = {
        "file": name,
        "class": row["class"],
        "file_type": row["file_type"],
        "category": category,
        "order": row["order"],
        "batch": row["batch"],
        "detected_features": 0,
        "msms_acquired_count": 0,
        "reference_matched_count": 0,
        "total_height": 0.0,
    }
    sample_reservoirs[name] = _Reservoir(seed=23 + len(samples))
    sn_reservoirs[name] = _Reservoir(seed=29 + len(samples))
    type_reservoirs.setdefault(category, _Reservoir(seed=31 + len(type_reservoirs)))


def _consume_spot(
    spot_id: str,
    rows: list[dict[str, Any]],
    sample_order: list[str],
    pca_indices: list[int],
    samples: dict[str, dict[str, Any]],
    sample_reservoirs: dict[str, _Reservoir],
    sn_reservoirs: dict[str, _Reservoir],
    type_reservoirs: dict[str, _Reservoir],
    qc_rsds: _Reservoir,
    qc_detection_rates: _Reservoir,
    blank_ratios: _Reservoir,
    pca_heap: list[tuple[float, int, list[float]]],
    sequence: int,
    standards: list[dict[str, Any]],
    standard_matches: list[dict[str, Any] | None],
) -> None:
    by_file = {row["file"]: row for row in rows}
    for row in rows:
        height = row["height"]
        if height <= 0:
            continue
        sample = samples[row["file"]]
        sample["detected_features"] += 1
        sample["total_height"] += height
        if row["msms"]:
            sample["msms_acquired_count"] += 1
        if row["reference_matched"]:
            sample["reference_matched_count"] += 1
        if row["sn"] > 0:
            sn_reservoirs[row["file"]].add(row["sn"])
        transformed = math.log10(height + 1.0)
        sample_reservoirs[row["file"]].add(transformed)
        type_reservoirs[sample["category"]].add(transformed)

    qc_values = [row["height"] for row in rows if samples[row["file"]]["category"] == "QC"]
    if qc_values:
        positive = [value for value in qc_values if value > 0]
        qc_detection_rates.add(len(positive) / len(qc_values))
        if len(positive) >= 2:
            average = sum(positive) / len(positive)
            if average > 0:
                variance = sum((value - average) ** 2 for value in positive) / (len(positive) - 1)
                qc_rsds.add(math.sqrt(variance) / average * 100.0)

    blank_values = [row["height"] for row in rows if samples[row["file"]]["category"] == "Blank" and row["height"] > 0]
    study_values = [row["height"] for row in rows if samples[row["file"]]["category"] == "Sample" and row["height"] > 0]
    if blank_values and study_values:
        blank_median = median(blank_values)
        if blank_median > 0:
            blank_ratios.add(median(study_values) / blank_median)

    vector = [math.log10(by_file.get(sample_order[index], {}).get("height", 0.0) + 1.0) for index in pca_indices]
    if len(vector) >= 2:
        average = sum(vector) / len(vector)
        variance = sum((value - average) ** 2 for value in vector) / len(vector)
        item = (variance, sequence, vector)
        if len(pca_heap) < PCA_FEATURE_LIMIT:
            heapq.heappush(pca_heap, item)
        elif variance > pca_heap[0][0]:
            heapq.heapreplace(pca_heap, item)

    detected = [row for row in rows if row["height"] > 0 and row["mz"] > 0 and row["rt"] >= 0]
    if not detected:
        return
    spot_mz = median([row["mz"] for row in detected])
    spot_rt = median([row["rt"] for row in detected])
    for index, standard in enumerate(standards):
        mz_delta = abs(spot_mz - standard["mz"])
        use_rt = standard["rt"] is not None
        rt_delta = abs(spot_rt - standard["rt"]) if use_rt else 0.0
        if mz_delta > standard["mz_tolerance"] or (
            use_rt and rt_delta > standard["rt_tolerance"]
        ):
            continue
        score = (
            math.hypot(
                mz_delta / standard["mz_tolerance"],
                rt_delta / standard["rt_tolerance"],
            )
            if use_rt
            else mz_delta / standard["mz_tolerance"]
        )
        if standard_matches[index] is None or score < standard_matches[index]["score"]:
            standard_matches[index] = {
                "score": score,
                "spot_id": spot_id,
                "median_mz": spot_mz,
                "median_rt": spot_rt,
                "rows": [dict(row) for row in rows],
            }


def _select_pca_indices(sample_order: list[str], samples: dict[str, dict[str, Any]]) -> list[int]:
    if len(sample_order) <= PCA_SAMPLE_LIMIT:
        return list(range(len(sample_order)))
    qc = [index for index, name in enumerate(sample_order) if samples[name]["category"] == "QC"]
    selected = set(qc[: min(len(qc), PCA_SAMPLE_LIMIT // 2)])
    remaining = PCA_SAMPLE_LIMIT - len(selected)
    if remaining > 0:
        step = max(1, len(sample_order) / remaining)
        for number in range(remaining):
            selected.add(min(len(sample_order) - 1, int(number * step)))
    return sorted(selected)[:PCA_SAMPLE_LIMIT]


def _compute_pca(features: list[list[float]], files: list[str], samples: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if len(files) < 3 or len(features) < 2:
        return {"status": "unavailable", "points": [], "message": "PCA requires at least 3 samples and 2 variable features."}
    centered: list[list[float]] = []
    total_variance = 0.0
    for feature in features:
        average = sum(feature) / len(feature)
        values = [value - average for value in feature]
        variance = sum(value * value for value in values)
        if variance <= 1e-12:
            continue
        centered.append(values)
        total_variance += variance
    if len(centered) < 2:
        return {"status": "unavailable", "points": [], "message": "No variable features were available for PCA."}
    pc1, eigen1 = _power_component(centered, [])
    pc2, eigen2 = _power_component(centered, [pc1])
    scale1 = math.sqrt(max(eigen1, 0.0))
    scale2 = math.sqrt(max(eigen2, 0.0))
    points = []
    for index, name in enumerate(files):
        sample = samples[name]
        points.append({
            "file": name,
            "class": sample["class"],
            "category": sample["category"],
            "order": sample["order"],
            "batch": sample["batch"],
            "pc1": pc1[index] * scale1,
            "pc2": pc2[index] * scale2,
        })
    return {
        "status": "ok",
        "points": points,
        "feature_count": len(centered),
        "sample_count": len(files),
        "sample_limited": len(samples) > len(files),
        "explained_variance": [
            eigen1 / total_variance if total_variance else 0.0,
            eigen2 / total_variance if total_variance else 0.0,
        ],
    }


def _power_component(matrix: list[list[float]], orthogonal_to: list[list[float]]) -> tuple[list[float], float]:
    size = len(matrix[0])
    vector = [math.sin(index + 1.0) for index in range(size)]
    vector = _normalize(vector)
    for _ in range(40):
        result = [0.0] * size
        for row in matrix:
            projection = sum(value * weight for value, weight in zip(row, vector))
            for index, value in enumerate(row):
                result[index] += value * projection
        for prior in orthogonal_to:
            projection = sum(value * weight for value, weight in zip(result, prior))
            result = [value - projection * weight for value, weight in zip(result, prior)]
        updated = _normalize(result)
        if sum((left - right) ** 2 for left, right in zip(updated, vector)) < 1e-14:
            vector = updated
            break
        vector = updated
    multiplied = [0.0] * size
    for row in matrix:
        projection = sum(value * weight for value, weight in zip(row, vector))
        for index, value in enumerate(row):
            multiplied[index] += value * projection
    eigenvalue = sum(value * weight for value, weight in zip(vector, multiplied))
    return vector, eigenvalue


def _normalize(values: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in values))
    if length <= 1e-15:
        return [0.0] * len(values)
    return [value / length for value in values]


def _summarize_samples(
    samples: dict[str, dict[str, Any]],
    reservoirs: dict[str, _Reservoir],
    sn_reservoirs: dict[str, _Reservoir],
    msms_available: bool,
) -> list[dict[str, Any]]:
    result = []
    for name, item in samples.items():
        values = sorted(reservoirs[name].values)
        sn_values = sorted(sn_reservoirs[name].values)
        result.append({
            **item,
            "median_log_intensity": _quantile(values, 0.5),
            "q25_log_intensity": _quantile(values, 0.25),
            "q75_log_intensity": _quantile(values, 0.75),
            "log_total_height": math.log10(item["total_height"] + 1.0),
            "msms_acquisition_rate": (
                item["msms_acquired_count"] / item["detected_features"]
                if msms_available and item["detected_features"] > 0
                else None
            ),
            "median_sn": _quantile(sn_values, 0.5),
            "q25_sn": _quantile(sn_values, 0.25),
            "q75_sn": _quantile(sn_values, 0.75),
        })
    result.sort(key=lambda item: (item["batch"], item["order"], item["file"]))
    for index, item in enumerate(result):
        item["carryover_ratio_to_previous_injection"] = None
        if item["category"] != "Blank" or index == 0:
            continue
        previous = result[index - 1]
        if previous["batch"] == item["batch"] and previous["total_height"] > 0:
            item["carryover_ratio_to_previous_injection"] = item["total_height"] / previous["total_height"]
    return result


def _summarize_report(
    samples: list[dict[str, Any]],
    spot_count: int,
    qc_rsds: list[float],
    qc_detection_rates: list[float],
    blank_ratios: list[float],
) -> dict[str, Any]:
    category_counts = {name: sum(item["category"] == name for item in samples) for name in ("Sample", "QC", "Blank")}
    intensity_corr = _pearson(
        [float(item["order"]) for item in samples],
        [float(item["median_log_intensity"] or 0.0) for item in samples],
    )
    ref_corr = _pearson(
        [float(item["order"]) for item in samples],
        [float(item["reference_matched_count"]) for item in samples],
    )
    msms_rates = [float(item["msms_acquisition_rate"]) for item in samples if item["msms_acquisition_rate"] is not None]
    median_sns = [float(item["median_sn"]) for item in samples if item["median_sn"] is not None]
    return {
        "sample_count": len(samples),
        "alignment_spot_count": spot_count,
        "category_counts": category_counts,
        "median_qc_rsd_percent": _safe_median(qc_rsds),
        "qc_features_rsd_le_30_percent": _fraction(qc_rsds, lambda value: value <= 30.0),
        "median_qc_detection_rate": _safe_median(qc_detection_rates),
        "sample_blank_ratio_ge_3": _fraction(blank_ratios, lambda value: value >= 3.0),
        "run_order_intensity_correlation": intensity_corr,
        "run_order_reference_match_correlation": ref_corr,
        "median_msms_acquisition_rate": _safe_median(msms_rates),
        "median_sample_sn": _safe_median(median_sns),
        "median_blank_carryover_ratio": _safe_median([
            float(item["carryover_ratio_to_previous_injection"])
            for item in samples
            if item["carryover_ratio_to_previous_injection"] is not None
        ]),
    }


def _pca_qc_metrics(pca: dict[str, Any]) -> dict[str, Any]:
    points = pca.get("points", []) if pca.get("status") == "ok" else []
    all_distances = _pairwise_distances(points)
    qc_distances = _pairwise_distances([point for point in points if point.get("category") == "QC"])
    all_median = _safe_median(all_distances)
    qc_median = _safe_median(qc_distances)
    return {
        "qc_pca_median_pairwise_distance": qc_median,
        "qc_pca_relative_dispersion": (
            qc_median / all_median
            if qc_median is not None and all_median is not None and all_median > 0
            else None
        ),
    }


def _pairwise_distances(points: list[dict[str, Any]]) -> list[float]:
    distances = []
    for left_index, left in enumerate(points):
        for right in points[left_index + 1:]:
            distances.append(math.hypot(float(left["pc1"]) - float(right["pc1"]), float(left["pc2"]) - float(right["pc2"])))
    return distances


def _histograms(reservoirs: dict[str, _Reservoir], bins: int = 30) -> list[dict[str, Any]]:
    all_values = [value for reservoir in reservoirs.values() for value in reservoir.values]
    if not all_values:
        return []
    lower, upper = min(all_values), max(all_values)
    width = max((upper - lower) / bins, 1e-9)
    result = []
    for category in ("Blank", "QC", "Sample"):
        reservoir = reservoirs.get(category)
        if not reservoir or not reservoir.values:
            continue
        counts = [0] * bins
        for value in reservoir.values:
            index = min(bins - 1, max(0, int((value - lower) / width)))
            counts[index] += 1
        total = sum(counts) or 1
        result.append({
            "category": category,
            "bin_centers": [lower + (index + 0.5) * width for index in range(bins)],
            "density": [count / total for count in counts],
            "observed_values": reservoir.seen,
            "sampled_values": len(reservoir.values),
        })
    return result


def _normalize_standard(item: dict[str, Any], index: int) -> dict[str, Any] | None:
    mz = _number(item.get("mz"))
    raw_rt = item.get("rt")
    rt = None if raw_rt is None or str(raw_rt).strip() == "" else _number(raw_rt)
    if mz <= 0 or (rt is not None and rt < 0):
        return None
    return {
        "name": str(item.get("name", "")).strip() or f"Internal standard {index + 1}",
        "adduct": str(item.get("adduct", "")).strip(),
        "mz": mz,
        "rt": rt,
        "mz_tolerance": max(_number(item.get("mz_tolerance"), 0.01), 1e-9),
        "rt_tolerance": max(_number(item.get("rt_tolerance"), 0.5), 1e-9),
    }


def _summarize_internal_standards(
    standards: list[dict[str, Any]],
    matches: list[dict[str, Any] | None],
) -> list[dict[str, Any]]:
    result = []
    for standard, match in zip(standards, matches):
        if match is None:
            result.append({**standard, "status": "not_found", "values": []})
            continue
        values = []
        for row in sorted(match["rows"], key=lambda item: (item["batch"], item["order"], item["file"])):
            values.append({
                "file": row["file"],
                "class": row["class"],
                "category": _sample_category(row["file_type"], row["class"]),
                "order": row["order"],
                "batch": row["batch"],
                "height": row["height"],
                "log_height": math.log10(row["height"] + 1.0),
                "mz": row["mz"],
                "ppm_error": (row["mz"] - standard["mz"]) / standard["mz"] * 1e6 if row["mz"] > 0 else None,
                "rt": row["rt"],
                "rt_delta": (
                    row["rt"] - standard["rt"]
                    if row["rt"] >= 0 and standard["rt"] is not None
                    else None
                ),
                "reference_matched": row["reference_matched"],
            })
        result.append({
            **standard,
            "status": "matched",
            "alignment_id": match["spot_id"],
            "median_mz": match["median_mz"],
            "median_rt": match["median_rt"],
            "selection_score": match["score"],
            "values": values,
        })
    return result


def _warnings(samples: list[dict[str, Any]], pca: dict[str, Any]) -> list[str]:
    warnings = []
    if not any(item["category"] == "Blank" for item in samples):
        warnings.append("No Blank files were identified; blank separation and carryover cannot be assessed.")
    if sum(item["category"] == "QC" for item in samples) < 3:
        warnings.append("At least three QC injections are needed to evaluate QC precision and topology.")
    if pca.get("sample_limited"):
        warnings.append(f"PCA was limited to {pca.get('sample_count', PCA_SAMPLE_LIMIT)} representative samples.")
    return warnings


def _sample_category(file_type: str, class_name: str) -> str:
    combined = f"{file_type} {class_name}".lower()
    if "blank" in combined:
        return "Blank"
    if "qc" in combined or "quality control" in combined:
        return "QC"
    return "Sample"


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def _safe_median(values: list[float]) -> float | None:
    return median(values) if values else None


def _fraction(values: list[float], predicate: Any) -> float | None:
    return sum(1 for value in values if predicate(value)) / len(values) if values else None


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    x_sum = sum((x - x_mean) ** 2 for x in xs)
    y_sum = sum((y - y_mean) ** 2 for y in ys)
    denominator = math.sqrt(x_sum * y_sum)
    return numerator / denominator if denominator > 0 else None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default

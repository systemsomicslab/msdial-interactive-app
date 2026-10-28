from __future__ import annotations

import csv
import heapq
import math
import random
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable
from .diagnostic_paths import is_diagnostic_artifact


QA_SUFFIX = ".qa.tsv"
PCA_FEATURE_LIMIT = 1000
PCA_SAMPLE_LIMIT = 500
RESERVOIR_LIMIT = 5000
# The prespecified QA criteria, by what they are computed from. A QC-based criterion needs at least
# MINIMUM_QC_INJECTIONS QC injections: an RSD, a detection rate or a dispersion from one or two QC
# says nothing about precision.
QC_CRITERION_METRICS = (
    "median_qc_rsd_percent",
    "qc_features_rsd_le_30_percent",
    "median_qc_detection_rate",
    "qc_pca_relative_dispersion",
)
BLANK_CRITERION_METRICS = ("sample_blank_ratio_ge_3", "median_blank_carryover_ratio")
CRITERION_METRICS = QC_CRITERION_METRICS + BLANK_CRITERION_METRICS + ("run_order_intensity_correlation",)
MINIMUM_QC_INJECTIONS = 3
MINIMUM_INJECTIONS = 3
# Every reason not_assessed_reasons can give; {n} is a count. The public reanalysis gate reads these
# phrases, so a change here is a change there.
NOT_ASSESSED_REASON_PHRASES = (
    "the alignment has no features",
    "the run had {n} QC injection(s), and at least three are needed",
    "no feature was detected in two or more QC injections",
    "the QC dispersion could not be computed from the PCA",
    "the run had no Blank files",
    "no feature was detected in both a Blank file and a study sample",
    "no Blank file followed an injection with detected features in its batch",
    "the run had {n} injection(s), and at least three are needed",
    "run order or median intensity did not vary across injections",
    "the injection order was not recorded for every file",
    "the QA matrix gives no value for it",
)
# The analytical-order sources a run-order criterion is not assessed against (decided 2026-09-29):
# neither the file listing nor a sequence number read out of the file names is a recorded injection
# order, and a drift computed against either describes the names.
UNRECORDED_ORDER_SOURCES = frozenset({"listing", "embedded"})
UNRECORDED_ORDER_REASON = "the injection order was not recorded for every file"
# The QA counts a feature-injection cell "Reference matched" only where its accepted annotation is no in-silico
# match: an MS/MS match against a predicted spectrum is never an identification (the MS-DIAL author's decision of
# 2026-10-28). A Console with that change (MsdialWorkbench #853) writes the in-silico cells in their own column and
# leaves them out of "Reference matched"; a Console before it (MS-DIAL 103c3b535 and earlier) marks them "Reference
# matched", and they are found from the run's per-file peak tables (<file>.mdpeak, their Evidence source and
# annotator) through its peak-ID matrix (<alignment>.mdpeakid.tsv), both written beside the QA matrix.
IN_SILICO_MATCHED_COLUMN = "In-silico matched"
PEAK_ID_MATRIX_SUFFIX = ".mdpeakid.tsv"
PEAK_TABLE_SUFFIX = ".mdpeak"
MSP_ANNOTATOR_SETTINGS_NAME = "msp_annotator_settings.tsv"
REFERENCE_MATCH_SOURCES = {
    "qa_matrix": "the QA matrix's own In-silico matched column (a Console that leaves in-silico matches out of "
                 "Reference matched)",
    "peak_tables": "the run's per-file peak tables (.mdpeak: Evidence source InSilico, or an annotator that searches "
                   "predicted spectra) through its peak-ID matrix (.mdpeakid.tsv)",
    "not_established": "nothing beside the QA matrix says which cells are in-silico matches",
}


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
    spot_observer: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    """The QA report of one matrix, read once.

    spot_observer, when given, is called with each spot's parsed rows as the matrix is read, so that
    another consumer (the QA figures) needs no second read. The report does not depend on it.
    """
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
        matching = ReferenceMatchReading.for_matrix(target, reader.fieldnames or [])
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
                if spot_observer is not None:
                    spot_observer(spot_rows)
                spot_count += 1
                spot_rows = []
            row = _parse_row(raw, matching)
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
            if spot_observer is not None:
                spot_observer(spot_rows)
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
    summary = with_qc_minimum(summary)
    return {
        "status": "ok",
        "file": str(target),
        "file_name": target.name,
        "summary": summary,
        "samples": sample_summaries,
        "intensity_distributions": _histograms(type_reservoirs),
        "pca": pca,
        "internal_standards": _summarize_internal_standards(standards, standard_matches),
        "reference_matching": matching.record(),
        "warnings": _warnings(sample_summaries, pca) + matching.warnings(),
        "method": {
            "reference_match": (
                "a feature counts as reference matched in an injection where MS-DIAL accepted an annotation of "
                "that injection's peak that is no in-silico (predicted-spectrum) match; in-silico matches are "
                "counted apart (in_silico_matched_count) and never as reference matches"
            ),
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
    spot_observer: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if not target.is_file() or not target.name.lower().endswith(QA_SUFFIX):
        raise FileNotFoundError(f"LC-MS QA matrix not found: {target}")
    return build_lcms_qa_report(target, internal_standards, spot_observer)


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


def _flag(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() == "true"


class ReferenceMatchReading:
    """Which cells of one QA matrix count as Reference matched, with in-silico matches left out.

    A Console with the in-silico QA change writes "In-silico matched" beside "Reference matched" and marks such
    a cell only there. A Console before it marks it "Reference matched"; then the in-silico cells are the
    (alignment ID, file) pairs whose per-file peak, by the run's peak-ID matrix, an in-silico match annotates in
    that file's .mdpeak: its Evidence source InSilico (or, where the table records none, code 450), or its
    Comment's annotator an MSP row of library kind predicted in msp_annotator_settings.tsv. This is the test the
    Console applies per cell, the cell's own peak, so both Consoles give the same counts. Where neither is beside
    the matrix the cells are read as the file states them, and the reading says so (warnings, record).
    """

    def __init__(self, source: str, cells: frozenset[tuple[str, str]] = frozenset(), detail: str = "") -> None:
        self.source = source
        self.cells = cells
        self.detail = detail
        self.in_silico_cells = 0
        self.excluded_cells = 0

    @classmethod
    def for_matrix(cls, matrix: str | Path, headers: Iterable[str]) -> "ReferenceMatchReading":
        names = {str(name).strip() for name in headers}
        if IN_SILICO_MATCHED_COLUMN in names:
            return cls("qa_matrix")
        try:
            cells, detail = in_silico_cells_from_peak_tables(Path(matrix))
        except InSilicoCellsNotEstablished as error:
            return cls("not_established", detail=str(error))
        return cls("peak_tables", cells, detail)

    @property
    def established(self) -> bool:
        return self.source != "not_established"

    def classify(self, spot_id: str, file: str, reference_text: Any, in_silico_text: Any = None) -> tuple[bool, bool]:
        """(reference matched, in-silico matched) for one cell, from its two columns as the matrix writes them."""
        marked = _flag(reference_text)
        in_silico = _flag(in_silico_text) or (marked and (spot_id, file) in self.cells)
        if in_silico:
            self.in_silico_cells += 1
            if marked:
                self.excluded_cells += 1
        return marked and not in_silico, in_silico

    def record(self) -> dict[str, Any]:
        """What the report keeps of this reading: where the in-silico cells were read from, and how many."""
        return {
            "in_silico_excluded": self.established,
            "source": self.source,
            "source_description": REFERENCE_MATCH_SOURCES[self.source],
            "detail": self.detail,
            # Cells read as in-silico matched, at any height; of them, those the matrix itself marks Reference
            # matched (a Console before the change) and the QA does not count as such.
            "in_silico_matched_cells": self.in_silico_cells,
            "excluded_from_reference_matched_cells": self.excluded_cells,
        }

    def warnings(self) -> list[str]:
        if self.established:
            return []
        return [
            "Whether a Reference matched cell of the QA matrix is an in-silico match could not be established ("
            f"{self.detail}); reference-match counts are as the matrix states them and may include in-silico "
            "matches, which are not identifications."
        ]


class InSilicoCellsNotEstablished(Exception):
    """Why the peak tables beside a QA matrix cannot say which of its cells are in-silico matches."""


def _predicted_annotators(folder: Path) -> set[str] | None:
    """The annotator ids of the MSP rows that search predicted spectra, from the run's MSP annotator settings;
    None where the run has no such table or it cannot be read."""
    from .workflow import is_predicted_library_kind

    table = folder / MSP_ANNOTATOR_SETTINGS_NAME
    if not table.is_file():
        return None
    try:
        with table.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
    except (OSError, csv.Error):
        return None
    return {
        str(row.get("annotator_id") or "").strip()
        for row in rows
        if is_predicted_library_kind(row.get("library_kind")) and str(row.get("annotator_id") or "").strip()
    }


def _peak_key(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    try:
        return str(int(float(text)))
    except (TypeError, ValueError, OverflowError):
        return text


def _in_silico_peak_ids(table: Path, predicted: set[str] | None) -> tuple[set[str], bool]:
    """The Peak IDs of one .mdpeak whose accepted or named annotation is an in-silico match, and whether the
    table records an evidence source."""
    from .annotation_codes import is_in_silico_match
    from .workflow import _comment_annotator

    found: set[str] = set()
    try:
        with table.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            headers = reader.fieldnames or []
            if "Peak ID" not in headers:
                raise InSilicoCellsNotEstablished(f"the peak table {table.name} has no Peak ID column")
            with_evidence = "Evidence source" in headers
            code_column = next((name for name in headers if name.startswith("Annotation tag")), "")
            for row in reader:
                name = str(row.get("Name") or "").strip()
                if not name or name.casefold() == "unknown":
                    continue
                evidence = row.get("Evidence source") if with_evidence else None
                code = row.get(code_column) if code_column else None
                if (with_evidence or code_column) and is_in_silico_match(code, evidence) or (
                    predicted and _comment_annotator(row.get("Comment")) in predicted
                ):
                    found.add(_peak_key(row.get("Peak ID")))
    except (OSError, csv.Error) as error:
        raise InSilicoCellsNotEstablished(f"the peak table {table.name} could not be read ({type(error).__name__})")
    return found, with_evidence


def in_silico_cells_from_peak_tables(matrix: Path) -> tuple[frozenset[tuple[str, str]], str]:
    """The (alignment ID, file) cells of a QA matrix whose per-file peak an in-silico match annotates, from the
    peak-ID matrix and the .mdpeak tables beside it, and a sentence on what was read (files named, no paths).

    Raises InSilicoCellsNotEstablished where they are not there or cannot say it: a table that records no
    evidence source can say it only through the run's MSP annotator settings.
    """
    folder = matrix.parent
    name = matrix.name
    stem = name[: -len(QA_SUFFIX)] if name.lower().endswith(QA_SUFFIX) else matrix.stem
    id_matrix = folder / f"{stem}{PEAK_ID_MATRIX_SUFFIX}"
    if not id_matrix.is_file():
        raise InSilicoCellsNotEstablished(f"no peak-ID matrix {id_matrix.name} is beside the QA matrix")
    predicted = _predicted_annotators(folder)
    try:
        with id_matrix.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            rows = csv.reader(handle, delimiter="\t")
            header = [item.strip() for item in next(rows, [])]
            if not header or header[0] != "alignment_master_id":
                raise InSilicoCellsNotEstablished(f"{id_matrix.name} is not an MS-DIAL peak-ID matrix")
            files = header[1:]
            flagged: dict[int, set[str]] = {}
            for index, file_name in enumerate(files):
                table = folder / f"{file_name}{PEAK_TABLE_SUFFIX}"
                if not table.is_file():
                    raise InSilicoCellsNotEstablished(f"the peak table {table.name} is not beside the QA matrix")
                peaks, with_evidence = _in_silico_peak_ids(table, predicted)
                if not with_evidence and predicted is None:
                    raise InSilicoCellsNotEstablished(
                        f"the peak table {table.name} records no evidence source, and no {MSP_ANNOTATOR_SETTINGS_NAME} "
                        "says which annotator searches predicted spectra"
                    )
                if peaks:
                    flagged[index] = peaks
            cells: set[tuple[str, str]] = set()
            if flagged:
                for row in rows:
                    if not row:
                        continue
                    spot = row[0].strip()
                    for index, peaks in flagged.items():
                        if index + 1 < len(row) and _peak_key(row[index + 1]) in peaks:
                            cells.add((spot, files[index]))
    except (OSError, csv.Error) as error:
        raise InSilicoCellsNotEstablished(f"{id_matrix.name} could not be read ({type(error).__name__})")
    detail = (f"{id_matrix.name} and {len(files)} peak table(s)"
              + (f", with {MSP_ANNOTATOR_SETTINGS_NAME}" if predicted is not None else ""))
    return frozenset(cells), detail


def _parse_row(raw: dict[str, str], matching: ReferenceMatchReading | None = None) -> dict[str, Any]:
    height = _number(raw.get("Height"), math.nan)
    spot_id = str(raw.get("ID", "")).strip()
    file = str(raw.get("File", "")).strip()
    reading = matching if matching is not None else ReferenceMatchReading("not_established")
    reference_matched, in_silico_matched = reading.classify(
        spot_id, file, raw.get("Reference matched"), raw.get(IN_SILICO_MATCHED_COLUMN)
    )
    return {
        "id": spot_id,
        "file": file,
        "class": str(raw.get("Class", "")).strip(),
        "file_type": str(raw.get("File type", "Sample")).strip() or "Sample",
        "order": _integer(raw.get("Injection order")),
        "batch": _integer(raw.get("Batch ID"), 1),
        # A height that is empty or not a finite number counts as 0, as before; the QA figures say
        # how many there were.
        "height": max(0.0, height) if height == height else 0.0,
        "height_numeric": height == height,
        "rt": _number(raw.get("RT")),
        "mz": _number(raw.get("MZ")),
        "sn": _number(raw.get("SN")),
        "msms": str(raw.get("MSMS", "")).strip().lower() == "true",
        # Never an in-silico match (ReferenceMatchReading), whatever the Console wrote in this column.
        "reference_matched": reference_matched,
        "in_silico_matched": in_silico_matched,
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
        "in_silico_matched_count": 0,
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
        if row["in_silico_matched"]:
            sample["in_silico_matched_count"] += 1
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


def with_qc_minimum(summary: dict[str, Any]) -> dict[str, Any]:
    """The summary with the QC criteria withheld below MINIMUM_QC_INJECTIONS, and why each is missing.

    A summary written before 0.5.3 carried an RSD from two QC and no reasons; read through this, it
    says what 0.5.3 would have. The summary passed in is not changed.
    """
    result = dict(summary)
    counts = result.get("category_counts") or {}
    qc = _known_count(counts, "QC")
    if qc is not None and qc < MINIMUM_QC_INJECTIONS:
        for metric in QC_CRITERION_METRICS:
            result[metric] = None
    reasons = result.get("not_assessed_reasons")
    if not isinstance(reasons, dict) or any(
            metric not in reasons for metric in CRITERION_METRICS
            if result.get(metric) is None or not _is_finite(result.get(metric))):
        # Fill the missing reasons; keep a reason already given unless the QC minimum now decides it,
        # so that a reason another rule gave (the unrecorded order) survives in any order of calls.
        computed = not_assessed_reasons(result)
        given = reasons if isinstance(reasons, dict) else {}
        below_minimum = qc is not None and qc < MINIMUM_QC_INJECTIONS
        result["not_assessed_reasons"] = {
            metric: (given[metric] if metric in given and not (below_minimum and metric in QC_CRITERION_METRICS)
                     else reason)
            for metric, reason in computed.items()
        }
    return result


def with_recorded_order(summary: dict[str, Any], order_source: str | None) -> dict[str, Any]:
    """The summary with the run-order criterion withheld when the run's order was read from the names.

    The correlation is computed against whatever analytical_order the CSV carries. Where the unit
    manifest records that order as the file listing or a number in the file names, it says nothing
    about drift, and for a study
    with no QC it was the one criterion left to report as met. A value already withheld keeps the
    reason it had. The summary passed in is not changed.
    """
    metric = "run_order_intensity_correlation"
    value = summary.get(metric)
    if order_source not in UNRECORDED_ORDER_SOURCES or value is None or not _is_finite(value):
        return summary
    result = dict(summary)
    result[metric] = None
    # Computed against the same order, so it describes the names too. It is no criterion.
    if "run_order_reference_match_correlation" in result:
        result["run_order_reference_match_correlation"] = None
    reasons = dict(result.get("not_assessed_reasons") or {})
    injections = _known_count(result, "sample_count")
    features = _known_count(result, "alignment_spot_count")
    if features == 0 or (injections is not None and injections < MINIMUM_INJECTIONS):
        # The counts decide first, as they do for a value that was never computed.
        reasons[metric] = _not_assessed_reason(metric, None, None, injections, features, None)
    else:
        reasons[metric] = UNRECORDED_ORDER_REASON
    result["not_assessed_reasons"] = reasons
    return result


def not_assessed_reasons(summary: dict[str, Any], pca: dict[str, Any] | None = None) -> dict[str, str]:
    """Why each prespecified criterion has no value, in words the publication report prints.

    The reason is the condition the value was computed under, so a report can say which criterion
    fell to which: the QC criteria to too few QC injections, the blank ones to no Blank file, and so
    on. The wording is fixed; the public reanalysis gate compares it with the counts.
    """
    counts = summary.get("category_counts") or {}
    # A count the summary does not carry is unknown, and a reason resting on it is not given.
    qc = _known_count(counts, "QC")
    blank = _known_count(counts, "Blank")
    injections = _known_count(summary, "sample_count")
    features = _known_count(summary, "alignment_spot_count")
    reasons: dict[str, str] = {}
    for metric in CRITERION_METRICS:
        value = summary.get(metric)
        if value is not None and _is_finite(value):
            continue
        reasons[metric] = _not_assessed_reason(metric, qc, blank, injections, features, pca)
    return reasons


def _not_assessed_reason(
    metric: str, qc: int | None, blank: int | None, injections: int | None, features: int | None,
    pca: dict[str, Any] | None,
) -> str:
    if features == 0:
        return "the alignment has no features"
    if metric in QC_CRITERION_METRICS:
        if qc is None:
            return "the QA matrix gives no value for it"
        if qc < MINIMUM_QC_INJECTIONS:
            return f"the run had {qc} QC injection(s), and at least three are needed"
        if metric in ("median_qc_rsd_percent", "qc_features_rsd_le_30_percent"):
            return "no feature was detected in two or more QC injections"
        if metric == "qc_pca_relative_dispersion":
            return "the QC dispersion could not be computed from the PCA"
        return "the QA matrix gives no value for it"
    if metric in BLANK_CRITERION_METRICS:
        if blank is None:
            return "the QA matrix gives no value for it"
        if blank == 0:
            return "the run had no Blank files"
        if metric == "sample_blank_ratio_ge_3":
            return "no feature was detected in both a Blank file and a study sample"
        # The Blank opened its batch, followed another batch, or followed an injection that detected
        # nothing: in each case no Blank has a preceding injection to compare with.
        return "no Blank file followed an injection with detected features in its batch"
    if metric == "run_order_intensity_correlation":
        if injections is None:
            return "the QA matrix gives no value for it"
        if injections < MINIMUM_INJECTIONS:
            return f"the run had {injections} injection(s), and at least three are needed"
        return "run order or median intensity did not vary across injections"
    return "the QA matrix gives no value for it"


def _known_count(mapping: Any, key: str) -> int | None:
    if not isinstance(mapping, dict) or mapping.get(key) is None:
        return None
    return _count(mapping.get(key))


def _count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _warnings(samples: list[dict[str, Any]], pca: dict[str, Any]) -> list[str]:
    warnings = []
    if not any(item["category"] == "Blank" for item in samples):
        warnings.append("No Blank files were identified; blank separation and carryover cannot be assessed.")
    if sum(item["category"] == "QC" for item in samples) < MINIMUM_QC_INJECTIONS:
        warnings.append("At least three QC injections are needed to evaluate QC precision, detection rate and topology.")
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

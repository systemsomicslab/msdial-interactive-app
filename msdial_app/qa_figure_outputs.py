"""Read what the QA figures are drawn from, and write them where the person will look.

Three places write figures, each after a step that already writes:

- a run's QA (msdial_generate_lcms_qa, msdial_complete_guided_analysis): figures 01, 02, 03,
  05, 06 and 07 and a page that shows them, in <run directory>/qa_figures;
- a confirmed laboratory normalize: figure 04 for its polarity, in <quant>/qa_figures;
- a confirmed laboratory merge: both polarities' run figures side by side, 04 for each
  polarity, 08 from the merged lipidome, and the batch's page, in <quant>/qa_figures.

A figure that cannot be drawn is skipped with its reason, and so is one whose input cannot be
read: the other figures are still drawn and the page still written. A failure here never
changes the QA, the normalize or the merge it accompanies: the callers turn it into a warning.
What is written carries no time and no absolute path, so the same inputs give the same
bytes, and figures.json records the inputs by name and sha256 and the version that drew them.
A run's QA reads its matrix once: QaMatrixReader takes the rows build_lcms_qa_report parses.
"""

from __future__ import annotations

import csv
import hashlib
import heapq
import json
import math
import os
import threading
import urllib.parse
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import __version__
from .qa_figures import (
    FIGURES,
    RATIO_THRESHOLD,
    RUN_FIGURE_KEYS,
    Anchor,
    ClassTotals,
    FigureOutput,
    Injection,
    RunQa,
    StandardResponse,
    group_colors,
    height_bin,
    internal_standard_figure,
    lipid_class_totals_figure,
    log_ratio,
    report_html,
    run_figures,
    safely,
    short_labels,
)
from .quality_assurance import _sample_category as sample_category

QA_FIGURES_FOLDER = "qa_figures"
REPORT_NAME = "qa_report.html"
MANIFEST_NAME = "figures.json"
MANIFEST_SCHEMA = "msdial-interactive.qa-figures.v1"
ANCHORS_NAME = "automatic_alignment_rt_correction_anchors.tsv"
SUMMARY_NAME = "automatic_alignment_rt_correction_summary.tsv"
# The PCA figure's bounds: beyond them it shows the most variable reference-matched features and
# study injections spread evenly over the run order, and says so.
PCA_SAMPLE_LIMIT = 200
PCA_FEATURE_LIMIT = 2000
NOT_WRITTEN_FOR_PATH = (
    "QA figures are written for a job's run (job_id or manifest_path); a QA matrix named by file_path "
    "alone has no run folder of its own to write them into"
)


# ---------------------------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------------------------


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _height(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return max(value, 0.0)


def _integer(text: str, default: int = 0) -> int:
    try:
        return int(float(text))
    except (TypeError, ValueError, OverflowError):
        return default


def _error_text(error: BaseException) -> str:
    """An error as a page may say it: an OSError without its file name, which can be a path of this machine
    and the page can travel in a publication bundle."""
    if isinstance(error, OSError) and error.strerror:
        return f"{type(error).__name__}: {error.strerror}"
    return f"{type(error).__name__}: {error}"


def _run_order_key(item: Injection) -> tuple[int, int, str]:
    return (item.batch, item.order, item.file)


def _evenly(indices: list[int], limit: int) -> list[int]:
    if len(indices) <= limit:
        return list(indices)
    chosen = sorted({round(position * (len(indices) - 1) / (limit - 1)) for position in range(limit)})
    return [indices[position] for position in chosen]


class QaMatrixReader:
    """What the figures need from an MS-DIAL QA matrix (*.qa.tsv), gathered spot by spot.

    Fed either by read_qa_matrix, which reads the file itself, or by build_lcms_qa_report through
    its spot_observer (add_qa_spot), so that a run's QA reads its matrix once. A height that is
    empty or not a number counts as not detected, and is counted. A feature counts as reference
    matched when the peak of at least one study (non-Blank) injection in it is, since the matrix
    marks the match per injection; its every study height then enters the PCA, and each feature
    counts once in the sample-to-Blank ratio.

    add_qa_spot never raises: the QA it observes must not change because a figure input failed.
    The first error is kept in ``error`` and the reader ignores the rest of the matrix.
    """

    def __init__(self) -> None:
        self.injections: list[Injection] = []
        self.files: dict[str, int] = {}
        self.detected: list[int] = []
        self.summed: list[float] = []
        self.bins: list[dict[int, int]] = []
        self.ratios: list[tuple[float, bool]] = []
        self.criterion = [0, 0]  # features at >= 3x, features counted, as the QA report computes them
        self.heap: list[tuple[float, int, list[float]]] = []
        self.spots = 0
        self.non_numeric = 0
        self.matched_features = 0
        self.error: BaseException | None = None
        self._known = -1
        self._samples: list[int] = []
        self._blanks: list[int] = []
        self._study: list[int] = []
        self._pca: list[int] | None = None

    def injection(self, name: str, class_name: str, file_type: str, order: int, batch: int) -> int:
        index = self.files.get(name)
        if index is None:
            file_type = file_type or "Sample"
            index = len(self.injections)
            self.files[name] = index
            self.injections.append(Injection(
                file=name, class_name=class_name, file_type=file_type,
                category=sample_category(file_type, class_name), order=order, batch=batch,
            ))
            self.detected.append(0)
            self.summed.append(0.0)
            self.bins.append({})
        return index

    def _categories(self) -> None:
        injections = self.injections
        self._samples = [index for index, item in enumerate(injections) if item.category == "Sample"]
        self._blanks = [index for index, item in enumerate(injections) if item.category == "Blank"]
        self._study = [index for index, item in enumerate(injections) if item.category != "Blank"]
        self._known = len(injections)
        if self._pca is None:
            study = sorted(self._study, key=lambda index: _run_order_key(injections[index]))
            self._pca = _evenly(study, PCA_SAMPLE_LIMIT)

    def add_spot(self, values: list[float], matched: set[int]) -> None:
        """One aligned feature: its height in each injection (by index; 0 when absent) and the
        injections whose own peak is reference matched."""
        if self._known != len(self.injections):
            self._categories()
        self.spots += 1
        detected, summed, bins = self.detected, self.summed, self.bins
        for index, value in enumerate(values):
            if value > 0:
                detected[index] += 1
                summed[index] += value
                key = height_bin(value)
                counts = bins[index]
                counts[key] = counts.get(key, 0) + 1
        is_matched = any(index in matched for index in self._study)
        if self._samples and self._blanks:
            sample_mean = sum(values[index] for index in self._samples) / len(self._samples)
            blank_mean = sum(values[index] for index in self._blanks) / len(self._blanks)
            self.ratios.append((log_ratio(sample_mean, blank_mean), is_matched))
            # The QA report's sample_blank_ratio_ge_3: median of the detected Sample heights over the
            # median of the detected Blank heights, for a feature detected in both.
            blank_detected = [values[index] for index in self._blanks if values[index] > 0]
            if blank_detected:
                sample_detected = [values[index] for index in self._samples if values[index] > 0]
                blank_median = _median(blank_detected)
                if sample_detected and blank_median > 0:
                    self.criterion[1] += 1
                    if _median(sample_detected) / blank_median >= RATIO_THRESHOLD:
                        self.criterion[0] += 1
        if is_matched:
            self.matched_features += 1
            vector = [math.log10(values[index] + 1.0) for index in self._pca or []]
            if len(vector) >= 2:
                mean = sum(vector) / len(vector)
                variance = sum((value - mean) ** 2 for value in vector)
                item = (variance, -self.spots, vector)
                if len(self.heap) < PCA_FEATURE_LIMIT:
                    heapq.heappush(self.heap, item)
                elif item[:2] > self.heap[0][:2]:
                    heapq.heapreplace(self.heap, item)

    def add_qa_spot(self, rows: Sequence[dict[str, Any]]) -> None:
        """One spot as build_lcms_qa_report parsed its rows (file, class, file_type, order, batch, height,
        height_numeric, reference_matched)."""
        if self.error is not None:
            return
        try:
            positions = [self.injection(str(row["file"]), str(row["class"]), str(row["file_type"]),
                                        int(row["order"]), int(row["batch"])) for row in rows]
            values = [0.0] * len(self.injections)
            matched: set[int] = set()
            for index, row in zip(positions, rows):
                if not row.get("height_numeric", True):
                    self.non_numeric += 1
                values[index] = float(row["height"])
                if row["reference_matched"]:
                    matched.add(index)
            self.add_spot(values, matched)
        except Exception as error:  # the QA it observes goes on unchanged
            self.error = error

    def finish(self, label: str) -> RunQa:
        if self.error is not None:
            raise self.error
        injections = self.injections
        order = sorted(range(len(injections)), key=lambda index: _run_order_key(injections[index]))
        position = {old: new for new, old in enumerate(order)}
        ordered = [injections[index] for index in order]
        for item, text in zip(ordered, short_labels(ordered)):
            item.label = text
        selected = sorted(self.heap, key=lambda item: -item[1])
        pca = self._pca or []
        return RunQa(
            label=label,
            injections=ordered,
            feature_count=self.spots,
            detected=[self.detected[index] for index in order],
            summed=[self.summed[index] for index in order],
            height_bins=[self.bins[index] for index in order],
            blank_ratios=self.ratios,
            blank_criterion=(self.criterion[0], self.criterion[1]) if self._samples and self._blanks else None,
            pca_matrix=[item[2] for item in selected],
            pca_injections=[position[index] for index in pca],
            pca_reference_matched=self.matched_features,
            pca_feature_limited=self.matched_features > PCA_FEATURE_LIMIT,
            pca_sample_limited=len(pca) < len(self._study),
            non_numeric_cells=self.non_numeric,
        )


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _matrix_label(path: Path) -> str:
    return path.name[: -len(".qa.tsv")] if path.name.casefold().endswith(".qa.tsv") else path.stem


def read_qa_matrix(path: str | Path, label: str = "") -> RunQa:
    """One pass over an MS-DIAL QA matrix (*.qa.tsv), kept as the figures need it (QaMatrixReader)."""
    target = Path(path)
    reader = QaMatrixReader()
    with target.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        rows = csv.reader(handle, delimiter="\t")
        header = [name.strip() for name in next(rows, [])]
        columns = {name: position for position, name in enumerate(header)}
        missing = [name for name in ("ID", "File", "Height") if name not in columns]
        if missing:
            raise ValueError(f"{target.name} is not an MS-DIAL QA matrix; it has no {', '.join(missing)} column(s).")

        def cell(row: list[str], name: str, default: str = "") -> str:
            position = columns.get(name, -1)
            return row[position].strip() if 0 <= position < len(row) else default

        current: str | None = None
        heights: dict[int, float] = {}
        matched: set[int] = set()

        def flush() -> None:
            reader.add_spot([heights.get(index, 0.0) for index in range(len(reader.injections))], matched)

        for row in rows:
            if not row or not any(item.strip() for item in row):
                continue
            spot = cell(row, "ID")
            if current is not None and spot != current:
                flush()
                heights, matched = {}, set()
            current = spot
            index = reader.injection(
                cell(row, "File"), cell(row, "Class"), cell(row, "File type", "Sample") or "Sample",
                _integer(cell(row, "Injection order"), 0), _integer(cell(row, "Batch ID"), 1),
            )
            value = _height(cell(row, "Height"))
            if value is None:
                reader.non_numeric += 1
                value = 0.0
            heights[index] = value
            if cell(row, "Reference matched").casefold() == "true":
                matched.add(index)
        if current is not None:
            flush()
    return reader.finish(label or _matrix_label(target))


def run_label(run_directory: str | Path) -> str:
    """The run's ion mode as its method file states it ("Positive", "Negative"), or ""."""
    try:
        text = (Path(run_directory) / "method.txt").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().casefold() == "ion mode":
            return value.strip()
    return ""


def attach_rt_anchors(run: RunQa, run_directory: str | Path) -> dict[str, Any] | None:
    """Read the run's RT-correction audit into ``run``; returns the anchor file's identity."""
    root = Path(run_directory)
    anchors_path = root / ANCHORS_NAME
    if not anchors_path.is_file():
        run.anchors = None
        run.anchor_missing_reason = (
            f"the run has no automatic RT correction audit ({ANCHORS_NAME}): the correction was not requested, "
            "or the Console predates it"
        )
        return None
    known = {item.file for item in run.injections}
    anchors: dict[str, list[Anchor]] = {}
    with anchors_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            name = str(row.get("File name") or "").strip()
            try:
                reference = float(str(row.get("Reference RT (min)") or ""))
                offset = float(str(row.get("Offset (min)") or ""))
            except ValueError:
                continue
            if not (math.isfinite(reference) and math.isfinite(offset)):
                continue
            used = str(row.get("Used") or "").strip().casefold() == "true"
            anchors.setdefault(name, []).append(Anchor(reference, offset * 60.0, used))
    identity = anchors_identity(root)
    if not anchors:
        run.anchors = None
        run.anchor_missing_reason = f"{ANCHORS_NAME} lists no anchor"
        return identity
    if not set(anchors) & known:
        run.anchors = None
        run.anchor_missing_reason = (
            f"{ANCHORS_NAME} names none of this alignment's files; an earlier run in this folder left it"
        )
        return identity
    run.anchors = anchors
    models: dict[str, str] = {}
    summary_path = root / SUMMARY_NAME
    if summary_path.is_file():
        with summary_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                models[str(row.get("File name") or "").strip()] = str(row.get("Model source") or "").strip()
    run.anchor_models = models
    from .automatic_rt_evidence import automatic_rt_correction_proof, proof_reason_phrase

    proof = automatic_rt_correction_proof(root)
    if not proof.get("performed"):
        run.anchor_note = (
            "the retained records do not prove that this run's alignment was corrected: "
            + proof_reason_phrase(proof)
        )
    return identity


def attach_rt_anchors_safely(run: RunQa, run_directory: str | Path) -> dict[str, Any] | None:
    """attach_rt_anchors, with an audit that cannot be read turned into figure 07's reason, not an error:
    the other figures do not depend on it."""
    try:
        return attach_rt_anchors(run, run_directory)
    except Exception as error:
        run.anchors = None
        run.anchor_models = {}
        run.anchor_note = ""
        run.anchor_missing_reason = f"the RT correction audit ({ANCHORS_NAME}) could not be read ({_error_text(error)})"
        return {"file": ANCHORS_NAME, "unreadable": type(error).__name__}


def read_height_export(path: str | Path) -> tuple[list[Injection], dict[str, list[float | None]], int]:
    """An alignment Height export's injections (in run order) and each spot's heights by alignment ID."""
    from .lipidome_merge import describe_export

    export = describe_export(path)
    start = int(export["first_sample_column"])
    names = list(export["sample_names"])
    header = export["header"]
    label_column = start - 1

    def header_row(title: str) -> list[str]:
        for row in header:
            if 0 <= label_column < len(row) and row[label_column].strip() == title:
                return row
        return []

    orders = header_row("Injection order")
    batches = header_row("Batch ID")
    injections = []
    for offset, (name, class_name, file_type) in enumerate(zip(names, export["sample_classes"], export["file_types"])):
        column = start + offset
        injections.append(Injection(
            file=name, class_name=class_name, file_type=file_type or "Sample",
            category=sample_category(file_type or "Sample", class_name),
            order=_integer(orders[column] if column < len(orders) else "", offset + 1),
            batch=_integer(batches[column] if column < len(batches) else "", 1),
        ))
    id_column = int(export["columns"]["alignment_id"])
    heights: dict[str, list[float | None]] = {}
    non_numeric = 0
    for row in export["rows"]:
        if id_column >= len(row) or not row[id_column].strip():
            continue
        values: list[float | None] = []
        for offset in range(len(names)):
            column = start + offset
            text = row[column].strip() if column < len(row) else ""
            value = _height(text)
            if value is None:
                non_numeric += 1
            values.append(value)
        heights[row[id_column].strip()] = values
    order = sorted(range(len(injections)), key=lambda index: _run_order_key(injections[index]))
    ordered = [injections[index] for index in order]
    for item, text in zip(ordered, short_labels(ordered)):
        item.label = text
    return ordered, {key: [values[index] for index in order] for key, values in heights.items()}, non_numeric


def standards_in_use(rows: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    """(name, alignment ID) of each standard a standards table resolves, once each, in table order."""
    seen: dict[str, str] = {}
    for row in rows:
        peak = _peak_id(row)
        name = str(row.get("StandardName") or "").strip()
        if peak is not None and peak >= 0 and name and name not in seen:
            seen[name] = str(peak)
    return list(seen.items())


def _peak_id(row: dict[str, Any]) -> int | None:
    try:
        return int(float(str(row.get("PeakID", "-1")).strip()))
    except ValueError:
        return None


def unresolved_standards(rows: Sequence[dict[str, Any]]) -> list[str]:
    """The standards a standards table names without a spot (PeakID < 0), once each, in table order."""
    resolved = {name for name, _ in standards_in_use(rows)}
    names: list[str] = []
    for row in rows:
        peak = _peak_id(row)
        name = str(row.get("StandardName") or "").strip()
        if peak is not None and peak < 0 and name and name not in resolved and name not in names:
            names.append(name)
    return names


def resolved_but_unused(resolution_rows: Iterable[dict[str, Any]], standards_rows: Sequence[dict[str, Any]]) -> list[str]:
    """The standards the resolution found that the standards table does not name, so normalize never used.

    resolution_rows are is_resolution_<polarity>.tsv's rows (standard, status) or the resolution's own
    entries (name, status).
    """
    from .lab_lipidomics import RESOLVED_STATUSES

    listed = {str(row.get("StandardName") or "").strip() for row in standards_rows}
    names: list[str] = []
    for row in resolution_rows:
        name = str(row.get("standard") or row.get("name") or "").strip()
        status = str(row.get("status") or "").strip()
        if name and status in RESOLVED_STATUSES and name not in listed and name not in names:
            names.append(name)
    return names


def standard_notes(standards_rows: Sequence[dict[str, Any]], resolution_rows: Iterable[dict[str, Any]] = ()) -> list[str]:
    """What figure 04 says about the standards it does not draw."""
    notes = []
    unresolved = unresolved_standards(standards_rows)
    if unresolved:
        notes.append("Not resolved in this polarity, so the classes they serve are not quantified from it: "
                     + ", ".join(unresolved))
    unused = resolved_but_unused(resolution_rows, standards_rows)
    if unused:
        notes.append("Resolved but not used by normalize (the quantification rules do not name them): "
                     + ", ".join(unused))
    return notes


def read_standards_table(path: str | Path) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def standard_responses(
    injections: Sequence[Injection], heights: dict[str, list[float | None]], standards: Sequence[tuple[str, str]],
) -> tuple[list[StandardResponse], list[str]]:
    responses: list[StandardResponse] = []
    missing: list[str] = []
    for name, alignment_id in standards:
        values = heights.get(alignment_id)
        if values is None:
            missing.append(f"{name} (alignment ID {alignment_id} is not in the Height export)")
            continue
        responses.append(StandardResponse(name=name, alignment_id=alignment_id, heights=list(values)))
    return responses, missing


def read_class_summary(path: str | Path) -> ClassTotals:
    """lipid_metabolome4classsummary.csv: Metabolite, Class, then one column per lipid class."""
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        rows = [row for row in csv.reader(handle) if row and any(cell.strip() for cell in row)]
    if not rows or len(rows[0]) < 3:
        raise ValueError(f"{Path(path).name} has no lipid class columns.")
    classes = [name.strip() for name in rows[0][2:]]
    samples, groups, values = [], [], []
    for row in rows[1:]:
        samples.append(row[0].strip())
        groups.append(row[1].strip() if len(row) > 1 else "")
        parsed: list[float | None] = []
        for position in range(len(classes)):
            text = row[position + 2].strip() if position + 2 < len(row) else ""
            try:
                value = float(text)
                parsed.append(value if math.isfinite(value) else None)
            except ValueError:
                parsed.append(None)
        values.append(parsed)
    return ClassTotals(samples=samples, groups=groups, classes=classes, values=values)


# ---------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------

_FOLDER_LOCKS: dict[str, threading.RLock] = {}
_FOLDER_LOCKS_GUARD = threading.Lock()


@contextmanager
def folder_lock(folder: str | Path) -> Iterator[None]:
    """One writer at a time per figures folder, so that a page, its figures and figures.json come from one
    draw: the backend serves requests on threads, and a QA, a publication, a normalize and a merge write."""
    key = os.path.normcase(os.path.abspath(str(folder)))
    with _FOLDER_LOCKS_GUARD:
        lock = _FOLDER_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def _write_text(path: Path, text: str) -> None:
    """The text, in place at once: written to a temporary file of its own beside the target, then renamed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.writing")
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _skipped_entry(figure: FigureOutput) -> dict[str, str]:
    return {"file": figure.file_name, "title": figure.title, "reason": str(figure.skipped)}


def _write_figures(folder: Path, figures: Sequence[FigureOutput]) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    """Each drawn figure written; a skipped one's earlier file removed, so no stale figure stays."""
    written: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    warnings: list[str] = []
    for figure in figures:
        target = folder / figure.file_name
        if figure.svg is not None:
            _write_text(target, figure.svg)
            written.append({"file": str(target), "title": figure.title})
        else:
            if target.is_file():
                target.unlink()
            skipped.append(_skipped_entry(figure))
            if str(figure.skipped).startswith("the figure could not be drawn") or " could not be read (" in str(figure.skipped):
                warnings.append(f"{figure.file_name}: {figure.skipped}.")
    return written, skipped, warnings


def _manifest(figures: Sequence[FigureOutput], sources: dict[str, Any]) -> str:
    record = {
        "schema": MANIFEST_SCHEMA,
        "msdial_interactive_version": __version__,
        "sources": sources,
        "figures": [
            {"file": figure.file_name, "title": figure.title, "caption": figure.caption}
            for figure in figures if figure.svg is not None
        ],
        "skipped": [_skipped_entry(figure) for figure in figures if figure.svg is None],
    }
    return json.dumps(record, indent=2, ensure_ascii=False) + "\n"


def _result(folder: Path, html_path: Path | None, written: list[dict[str, str]], skipped: list[dict[str, str]],
            warnings: list[str]) -> dict[str, Any]:
    return {
        "status": "written" if written else "nothing_drawn",
        "folder": str(folder),
        "html": str(html_path) if html_path else "",
        "figures": written,
        "skipped": skipped,
        "warnings": warnings,
    }


def failed_result(step: str, error: BaseException) -> dict[str, Any]:
    """What a caller reports when the figures could not be written at all."""
    return {
        "status": "failed",
        "folder": "",
        "html": "",
        "figures": [],
        "skipped": [],
        "warnings": [f"The QA figures for {step} could not be written ({type(error).__name__}: {error}); "
                     "the step itself is unaffected."],
    }


def _lede(run: RunQa, source: str) -> str:
    text = (f"Drawn from {source}: {len(run.injections)} injection(s) and {run.feature_count:,} aligned feature(s). "
            "Each figure is also saved as an SVG file beside this page.")
    if run.non_numeric_cells:
        text += f" {run.non_numeric_cells:,} cell(s) had no numeric height and count as not detected."
    return text


def _read_manifest(folder: Path) -> dict[str, Any] | None:
    try:
        record = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def anchors_identity(run_directory: str | Path) -> dict[str, Any] | None:
    """The RT-correction audit as figures.json records it: its name and sha256, or None when there is none."""
    path = Path(run_directory) / ANCHORS_NAME
    if not path.is_file():
        return None
    try:
        return {"file": path.name, "sha256": file_sha256(path)}
    except OSError as error:
        return {"file": path.name, "unreadable": type(error).__name__}


def _run_sources(qa: Path, root: Path) -> dict[str, Any]:
    return {"qa_matrix": {"file": qa.name, "sha256": file_sha256(qa)}, "rt_correction_anchors": anchors_identity(root)}


def _record_is_current(record: dict[str, Any] | None, folder: Path, sources: dict[str, Any]) -> bool:
    """Figures drawn by this version from these very inputs, every one still on disk with its page."""
    if not record or record.get("msdial_interactive_version") != __version__ or record.get("sources") != sources:
        return False
    listed = [item for item in record.get("figures") or [] if isinstance(item, dict)]
    return (folder / REPORT_NAME).is_file() and all((folder / str(item.get("file") or "")).is_file() for item in listed)


def _paths(qa_path: str | Path, run_directory: str | Path | None) -> tuple[Path, Path]:
    qa = Path(qa_path).expanduser().resolve()
    root = Path(run_directory).expanduser().resolve() if run_directory else qa.parent
    return qa, root


def figures_are_current(qa_path: str | Path, run_directory: str | Path | None = None) -> bool:
    """Whether <run directory>/qa_figures holds what write_run_qa_figures would write now (_record_is_current)."""
    try:
        qa, root = _paths(qa_path, run_directory)
        folder = root / QA_FIGURES_FOLDER
        return _record_is_current(_read_manifest(folder), folder, _run_sources(qa, root))
    except Exception:
        return False


def run_page(run: RunQa, source: str) -> tuple[list[FigureOutput], str]:
    """One run's figures and the page that shows them."""
    figures = run_figures([run])
    return figures, report_html(f"QA figures: {run.label} run", _lede(run, source), [("", figures)])


def write_run_qa_figures(qa_path: str | Path, run_directory: str | Path | None = None,
                         reader: QaMatrixReader | None = None) -> dict[str, Any]:
    """Figures 01, 02, 03, 05, 06 and 07 and their page, in <run directory>/qa_figures.

    reader is a QaMatrixReader that build_lcms_qa_report fed while it read this matrix; without one, or
    when it stopped on an error, the matrix is read here.
    """
    qa, root = _paths(qa_path, run_directory)
    label = run_label(root) or _matrix_label(qa)
    folder = root / QA_FIGURES_FOLDER
    with folder_lock(folder):
        if reader is not None and reader.error is None and reader.spots:
            run = reader.finish(label)
        else:
            run = read_qa_matrix(qa, label)
        anchors = attach_rt_anchors_safely(run, root)
        figures, page = run_page(run, qa.name)
        written, skipped, warnings = _write_figures(folder, figures)
        html_path = folder / REPORT_NAME
        _write_text(html_path, page)
        sources = {"qa_matrix": {"file": qa.name, "sha256": file_sha256(qa)}, "rt_correction_anchors": anchors}
        _write_text(folder / MANIFEST_NAME, _manifest(figures, sources))
    return _result(folder, html_path, written, skipped, warnings)


def current_run_figures(qa_path: str | Path, run_directory: str | Path | None = None,
                        reader: QaMatrixReader | None = None) -> tuple[dict[str, Any], list[Path]]:
    """The run's figures as this version draws them from this QA matrix and anchor audit, drawn first
    when the ones there are not (another matrix or audit, another version, or a file missing).

    Returns the compact result (status "current" when the figures there were kept) and the files a
    publication bundle may carry: the SVG files that exist, the page and figures.json.
    """
    qa, root = _paths(qa_path, run_directory)
    folder = root / QA_FIGURES_FOLDER
    with folder_lock(folder):
        result = None
        record = _read_manifest(folder)
        if not _record_is_current(record, folder, _run_sources(qa, root)):
            result = write_run_qa_figures(qa, root, reader=reader)
            record = _read_manifest(folder)
        if record is None:
            return result or _result(folder, None, [], [], [f"{MANIFEST_NAME} could not be read."]), []
        listed = [item for item in record.get("figures") or [] if isinstance(item, dict)]
        present = [item for item in listed if (folder / str(item.get("file") or "")).is_file()]
        files = [folder / str(item.get("file")) for item in present]
        files += [path for path in (folder / REPORT_NAME, folder / MANIFEST_NAME) if path.is_file()]
        if result is None:
            result = _result(
                folder, folder / REPORT_NAME,
                [{"file": str(folder / str(item.get("file"))), "title": str(item.get("title") or "")} for item in present],
                [item for item in record.get("skipped") or [] if isinstance(item, dict)], [],
            )
            result["status"] = "current"
    return result, files


def write_internal_standard_figure(
    polarity: str, height_export: str | Path, standards_rows: Sequence[dict[str, Any]], folder: str | Path,
    resolution_rows: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Figure 04 for one polarity, from the run's Height export and the standards normalize is given.

    resolution_rows (the resolution's entries) let the figure name the standards found but not used.
    """
    target = Path(folder)
    with folder_lock(target):
        injections, heights, _ = read_height_export(height_export)
        responses, missing = standard_responses(injections, heights, standards_in_use(standards_rows))
        notes = standard_notes(standards_rows, resolution_rows)
        slug = polarity.casefold()
        figure = safely("04", lambda: internal_standard_figure(polarity, injections, responses, notes=notes),
                        polarity=slug, label=polarity)
        written, skipped, warnings = _write_figures(target, [figure])
    warnings.extend(f"Not drawn in figure 04: {item}." for item in missing)
    return _result(target, None, written, skipped, warnings)


# By polarity: the injections, each standard's heights, the standards not in the Height matrix, and
# what figure 04 says about the standards it does not draw (standard_notes).
StandardsInput = tuple[list[Injection], list[StandardResponse], list[str], list[str]]


def lipidomics_page(
    runs: Sequence[RunQa], standards: dict[str, StandardsInput], totals: ClassTotals | None, unit: str,
    notes: Sequence[str] = (), links: Sequence[tuple[str, str]] = (), unreadable: dict[str, str] | None = None,
) -> tuple[list[FigureOutput], str, list[str]]:
    """The batch's figures, its page, and what the page says was left out.

    unreadable gives, by figure key (04_positive, 04_negative, 08), why an input of that figure could not be
    read; that figure is skipped with the reason and every other one is drawn.
    """
    from .lab_lipidomics import NEGATIVE, POSITIVE

    notes = list(notes)
    unreadable = dict(unreadable or {})
    colors = group_colors(
        [item.group for run in runs for item in run.injections]
        + [item.group for value in standards.values() for item in value[0]]
        + list(totals.groups if totals else [])
    )
    figures = run_figures(runs, colors) if runs else []
    for polarity in (POSITIVE, NEGATIVE):
        slug = polarity.casefold()
        reason = unreadable.get(f"04_{slug}")
        if reason is None and polarity in standards:
            injections, responses, missing, *rest = standards[polarity]
            figure_notes = list(rest[0]) if rest else []
            figures.append(safely(
                "04", lambda: internal_standard_figure(polarity, injections, responses, colors, notes=figure_notes),
                polarity=slug, label=polarity))
            notes.extend(f"Not drawn in the {polarity.lower()} figure 04: {item}." for item in missing)
        else:
            figures.append(FigureOutput(
                key=f"04_{slug}", file_name=FIGURES["04"]["file"].format(polarity=slug),
                title=FIGURES["04"]["title"].format(label=polarity), caption=FIGURES["04"]["caption"],
                how_to_read=FIGURES["04"]["how"],
                skipped=reason or f"the {polarity.lower()} standards table or Height matrix normalize wrote is missing",
            ))
    if totals is not None and "08" not in unreadable:
        figures.append(safely("08", lambda: lipid_class_totals_figure(totals, unit, colors)))
    else:
        figures.append(FigureOutput(
            key="08", file_name=FIGURES["08"]["file"], title=FIGURES["08"]["title"],
            caption=FIGURES["08"]["caption"], how_to_read=FIGURES["08"]["how"],
            skipped=unreadable.get("08") or "the merge wrote no class summary (lipid_metabolome4classsummary.csv)",
        ))
    by_key = {figure.key: figure for figure in figures}
    sections = [
        ("Both polarities, run by run", [by_key[key] for key in RUN_FIGURE_KEYS if key in by_key]),
        ("Internal standards", [by_key[key] for key in ("04_positive", "04_negative") if key in by_key]),
        ("The merged lipidome", [by_key["08"]] if "08" in by_key else []),
    ]
    lede = ("Quality-assurance figures for this laboratory lipidomics batch: the two production runs side by side, "
            "the internal standards normalize used, and the merged lipidome's class totals. Each figure is also "
            "saved as an SVG file beside this page.")
    if notes:
        lede += " " + " ".join(notes)
    return figures, report_html("QA figures: laboratory lipidomics batch", lede, sections, links), notes


def relative_link(target: str | Path, folder: str | Path) -> str | None:
    """A page's href from a page in ``folder``: relative, and percent-encoded, since a folder name may hold
    '#', '?', '%' or a space; None when no relative path exists (another drive)."""
    try:
        relative = Path(os.path.relpath(target, folder)).as_posix()
    except ValueError:
        return None
    return urllib.parse.quote(relative, safe="/")


def write_lipidomics_qa_figures(contract_path: str, merge_report: dict[str, Any]) -> dict[str, Any]:
    """The batch's page after a confirmed merge: both runs side by side, 04 for each polarity, 08.

    Each input is read on its own: one that cannot be read leaves out the figures it feeds, with the
    reason on the page and in the warnings, and every other figure is still drawn.
    """
    from .lab_lipidomics import POLARITIES
    from .lab_lipidomics_agent import FOLDERS, LABELS, load_contract

    contract = load_contract(contract_path, verify=False)
    quant = Path(contract["quant_folder"])
    folder = quant / QA_FIGURES_FOLDER
    runs: list[RunQa] = []
    sources: dict[str, Any] = {}
    notes: list[str] = []
    standards: dict[str, StandardsInput] = {}
    unreadable: dict[str, str] = {}
    with folder_lock(folder):
        for polarity in POLARITIES:
            lower = polarity.lower()
            slug = FOLDERS[polarity]
            root = Path(contract["output_roots"][polarity])
            record: dict[str, Any] = {}
            record_path = quant / f"normalize_{slug}.local.json"
            try:
                loaded = json.loads(record_path.read_text(encoding="utf-8"))
                record = loaded if isinstance(loaded, dict) else {}
            except Exception as error:
                notes.append(f"The {lower} normalize record {record_path.name} could not be read ({_error_text(error)}).")
            candidate_table = Path(str(record.get("candidate_table") or ""))
            stem = candidate_table.name[: -len(".mdcandidate.tsv")] if candidate_table.name.endswith(".mdcandidate.tsv") else ""
            qa = candidate_table.with_name(stem + ".qa.tsv") if stem else None
            if qa is not None and qa.is_file():
                try:
                    run = read_qa_matrix(qa, polarity)
                    sources[f"{slug}_qa_matrix"] = {"file": qa.name, "sha256": file_sha256(qa)}
                except Exception as error:
                    notes.append(f"The {lower} run's QA matrix ({qa.name}) could not be read ({_error_text(error)}), "
                                 "so its run figures are not drawn.")
                else:
                    sources[f"{slug}_rt_correction_anchors"] = attach_rt_anchors_safely(run, root)
                    runs.append(run)
            else:
                notes.append(f"The {lower} run's QA matrix ({stem or 'its alignment'}.qa.tsv) was not found "
                             "beside its candidate table, so its run figures are not drawn.")
            table = quant / f"standards_{slug}.tsv"
            height_copy = Path(str((record.get("outputs") or {}).get("height_copy") or quant / f"Height_{LABELS[polarity]}.txt"))
            if table.is_file() and height_copy.is_file():
                try:
                    rows = read_standards_table(table)
                    injections, heights, _ = read_height_export(height_copy)
                    responses, missing = standard_responses(injections, heights, standards_in_use(rows))
                    sources[f"{slug}_standards"] = {"file": table.name, "sha256": file_sha256(table)}
                    sources[f"{slug}_heights"] = {"file": height_copy.name, "sha256": file_sha256(height_copy)}
                except Exception as error:
                    unreadable[f"04_{polarity.casefold()}"] = (
                        f"the {lower} standards table or Height matrix could not be read ({_error_text(error)})")
                else:
                    resolution_rows: list[dict[str, str]] = []
                    resolution = quant / f"is_resolution_{slug}.tsv"
                    if resolution.is_file():
                        try:
                            resolution_rows = read_standards_table(resolution)
                        except Exception as error:
                            notes.append(f"The {lower} standards resolution {resolution.name} could not be read "
                                         f"({_error_text(error)}); figure 04 names no standard resolved but unused.")
                    standards[polarity] = (injections, responses, missing, standard_notes(rows, resolution_rows))
        summary_path = Path(str((((merge_report.get("outputs") or {}).get("class_summary") or {}).get("path")) or ""))
        totals = None
        if summary_path.is_file():
            try:
                totals = read_class_summary(summary_path)
                sources["class_summary"] = {"file": summary_path.name, "sha256": file_sha256(summary_path)}
            except Exception as error:
                totals = None
                unreadable["08"] = f"the class summary {summary_path.name} could not be read ({_error_text(error)})"
        links = []
        for polarity in POLARITIES:
            page = Path(contract["output_roots"][polarity]) / QA_FIGURES_FOLDER / REPORT_NAME
            href = relative_link(page, folder) if page.is_file() else None
            if href:
                links.append((f"The {polarity.lower()} run's own QA figures", href))
        figures, page, notes = lipidomics_page(runs, standards, totals, str(contract.get("unit") or ""), notes, links,
                                               unreadable)
        written, skipped, warnings = _write_figures(folder, figures)
        warnings.extend(notes)
        html_path = folder / REPORT_NAME
        _write_text(html_path, page)
        _write_text(folder / MANIFEST_NAME, _manifest(figures, sources))
    return _result(folder, html_path, written, skipped, warnings)

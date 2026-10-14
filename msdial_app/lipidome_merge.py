"""Merge a positive-mode and a negative-mode MS-DIAL alignment into one lipidome.

A lipid class is quantified in whichever polarity and adduct measures it best, and
that choice is a per-class decision recorded in a rule table rather than anything the
data can decide for itself. Merging is therefore: keep the rows whose ontology and
adduct the table selects, from both polarities, and concatenate them.

The two exports must describe the same samples in the same order, because the merged
table keeps one set of sample columns and silently pairs them by position. Two runs
that were aligned separately can disagree -- a file excluded in one polarity, a
different injection order -- and nothing downstream would notice. That is checked
here and refused, rather than left to be discovered in the statistics.

The output reproduces the laboratory's own merge program (metaboservice): comma
separated when the file is a .csv, CRLF, and rows ordered by ontology and then name
the way .NET compares strings, which is not Python's ordinal order. On the 2024
hands-on exports and the rule table that produced them, the result is that file byte
for byte.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Collection, Iterable

HEADER_ROW_COUNT = 5
CLASS_ROW = 0
FILE_TYPE_ROW = 1
INJECTION_ORDER_ROW = 2
COLUMN_NAME_ROW = 4

ALIGNMENT_ID_COLUMN = 0
METABOLITE_NAME_COLUMN = 3
ADDUCT_COLUMN = 4
FORMULA_COLUMN = 10
ONTOLOGY_COLUMN = 11
COMMENT_COLUMN = 18

# A row the merged lipidome never carries, whatever the rule table says.
INTERNAL_STANDARD_NAME_FRAGMENT = "(d"
EXCLUDED_NAME_FRAGMENTS = ("w/o", "RIKEN")
# Exports before MS-DIAL 2026-09-15 named suggestions this way. Current exports strip the
# prefix, so this filter no longer sees them; the candidate table does (reference_matched_ids).
EXCLUDED_NAME_PREFIXES = ("low score: ", "no MS2: ")
MANUAL_EXCLUSION_COMMENT_PREFIX = "x"
# Rows the rule table selects but that carry no valid concentration. They go to their own
# file rather than vanishing into a count, because a selected class with no numbers is a
# finding about the standards, not about the lipidome.
NOT_QUANTIFIED_REASON = "not quantified"
NOT_QUANTIFIED_COMMENT_PREFIX = "NOT QUANTIFIED"


class MergeRefused(ValueError):
    """The two exports cannot be merged as they stand."""


def read_adduct_rules(path: str | Path) -> dict[str, Any]:
    """Read the class/adduct table that says which measurement of a class to keep.

    Tab-separated, one row per class and adduct, with a column saying whether that
    combination is the selected one. Only selected rows are returned. A quant_utility_table
    workbook (.xlsx) is read from its third sheet, which holds the same four columns.
    """
    if Path(path).suffix.lower() == ".xlsx":
        return _rules_from_workbook(path)
    rows = _read_rows(path)
    if not rows:
        raise MergeRefused(f"The adduct rule table is empty: {path}")
    header = [cell.strip().lower() for cell in rows[0]]

    def column(*names: str, default: int = -1) -> int:
        for name in names:
            if name in header:
                return header.index(name)
        return default

    class_column = column("class", "ontology", default=0)
    mode_column = column("ion mode", "ionmode", "polarity", default=2)
    adduct_column = column("adduct", "adduct type", default=1)
    selected_column = column("isselected", "selected", default=3)

    selected: set[tuple[str, str]] = set()
    considered = 0
    for row in rows[1:]:
        if len(row) <= max(class_column, adduct_column, selected_column):
            continue
        considered += 1
        flag = row[selected_column].strip().lower()
        if flag not in {"true", "1", "yes"}:
            continue
        selected.add((row[class_column].strip(), row[adduct_column].strip()))
    if not selected:
        raise MergeRefused(
            f"No class/adduct combination is selected in {path}; "
            f"{considered} row(s) were read and every one is unselected."
        )
    by_mode: dict[str, int] = {}
    for row in rows[1:]:
        if len(row) <= max(class_column, adduct_column, selected_column):
            continue
        if row[selected_column].strip().lower() not in {"true", "1", "yes"}:
            continue
        mode = row[mode_column].strip() if 0 <= mode_column < len(row) else ""
        by_mode[mode or "unspecified"] = by_mode.get(mode or "unspecified", 0) + 1
    return {
        "path": str(Path(path).resolve()),
        "selected": selected,
        "rule_count": considered,
        # An annotation search list and a quantification selection list have the same four
        # columns, and passing one for the other is accepted in silence -- it just keeps
        # far more rows. These counts make the substitution visible before the numbers are
        # read: a selection list keeps roughly one adduct per class per polarity.
        "selected_by_ion_mode": dict(sorted(by_mode.items())),
        "selected_classes": len({item[0] for item in selected}),
    }


def _rules_from_workbook(path: str | Path) -> dict[str, Any]:
    from .lab_lipidomics import LabInputError, parse_quant_info, read_xlsx

    try:
        workbook = read_xlsx(path)
    except LabInputError as error:
        raise MergeRefused(str(error)) from None
    if len(workbook.sheets) < 3:
        raise MergeRefused(f"{path} has no third sheet to read the class/adduct selection from.")
    rules = parse_quant_info(workbook.sheets[2], source=workbook.path)
    if rules["blockers"]:
        raise MergeRefused(f"The selection in {path} cannot be used: " + " ".join(rules["blockers"]))
    return rules


def describe_export(path: str | Path) -> dict[str, Any]:
    """The samples an MS-DIAL alignment export describes, in the order it lists them."""
    rows = _read_rows(path)
    if len(rows) <= HEADER_ROW_COUNT:
        raise MergeRefused(f"{path} has no data rows below its {HEADER_ROW_COUNT} header lines.")
    class_row = rows[CLASS_ROW]
    try:
        label_index = class_row.index("Class")
    except ValueError:
        raise MergeRefused(
            f"{path} does not look like an MS-DIAL alignment export: "
            "its first line names no 'Class' column."
        ) from None

    start = label_index + 1
    names: list[str] = []
    classes: list[str] = []
    column_names = rows[COLUMN_NAME_ROW]
    file_types = rows[FILE_TYPE_ROW]
    for index in range(start, len(class_row)):
        label = class_row[index].strip()
        # The statistics columns that follow the samples are labelled NA here.
        if label.upper() == "NA" or not label:
            break
        names.append(column_names[index].strip() if index < len(column_names) else "")
        classes.append(label)
    if not names:
        raise MergeRefused(f"{path} lists no sample columns.")
    def column(name: str, fallback: int) -> int:
        # The constants describe the format as it stands; the header is what this file
        # actually says. Reading the name first means a changed export shifts nothing.
        try:
            return column_names.index(name)
        except ValueError:
            return fallback

    return {
        "path": str(Path(path).resolve()),
        "first_sample_column": start,
        "columns": {
            "alignment_id": column("Alignment ID", ALIGNMENT_ID_COLUMN),
            "metabolite_name": column("Metabolite name", METABOLITE_NAME_COLUMN),
            "adduct": column("Adduct type", ADDUCT_COLUMN),
            "formula": column("Formula", FORMULA_COLUMN),
            "ontology": column("Ontology", ONTOLOGY_COLUMN),
            "comment": column("Comment", COMMENT_COLUMN),
        },
        "sample_names": names,
        "sample_classes": classes,
        "file_types": [
            file_types[start + offset].strip() if start + offset < len(file_types) else ""
            for offset in range(len(names))
        ],
        "header": rows[:HEADER_ROW_COUNT],
        "rows": rows[HEADER_ROW_COUNT:],
    }


def compare_sample_order(positive: dict[str, Any], negative: dict[str, Any]) -> dict[str, Any]:
    """Whether the two exports describe the same samples in the same order.

    Sample names differ by the polarity they were acquired in, so they are compared
    with that suffix removed; everything else must match position by position.
    """
    left = [strip_polarity(name) for name in positive["sample_names"]]
    right = [strip_polarity(name) for name in negative["sample_names"]]
    problems: list[str] = []
    if len(left) != len(right):
        problems.append(
            f"positive lists {len(left)} sample(s) and negative lists {len(right)}"
        )
    for index in range(min(len(left), len(right))):
        if left[index] != right[index]:
            problems.append(
                f"position {index + 1}: positive '{positive['sample_names'][index]}' "
                f"and negative '{negative['sample_names'][index]}' are different samples"
            )
    if positive["first_sample_column"] != negative["first_sample_column"]:
        # Rows from both exports are concatenated and then read positionally, so two
        # exports whose metadata blocks are different widths would put every negative
        # value under the wrong sample -- quietly, and in every row.
        problems.append(
            f"the sample columns begin at index {positive['first_sample_column']} in positive "
            f"and {negative['first_sample_column']} in negative, so the two tables are not "
            "the same shape"
        )
    if positive.get("columns") != negative.get("columns"):
        problems.append(
            "the two exports place their metadata columns differently: "
            f"{positive.get('columns')} against {negative.get('columns')}"
        )
    for index in range(min(len(left), len(right))):
        if positive["sample_classes"][index] != negative["sample_classes"][index]:
            problems.append(
                f"position {index + 1}: class '{positive['sample_classes'][index]}' "
                f"in positive and '{negative['sample_classes'][index]}' in negative"
            )
    return {
        "matched": not problems,
        "problems": problems,
        "positive_sample_count": len(left),
        "negative_sample_count": len(right),
        "paired_samples": [
            {
                "position": index + 1,
                "positive": positive["sample_names"][index],
                "negative": negative["sample_names"][index],
                "class": positive["sample_classes"][index],
            }
            for index in range(min(len(left), len(right)))
        ],
    }


def should_keep(
    row: list[str],
    rules: dict[str, Any],
    columns: dict[str, int] | None = None,
    *,
    internal_standard_ids: Collection[str] | None = None,
    reference_matched_ids: Collection[str] | None = None,
    expected_unit: str | None = None,
    unquantified_ids: Collection[str] | None = None,
) -> tuple[bool, str]:
    """Whether one aligned row belongs in the merged lipidome, and why not if it does not.

    The first reason that applies is the one given. A selected row without a valid
    concentration is refused with a reason starting "not quantified", which the merge
    writes to its own file instead of dropping.
    """
    columns = columns or {
        "alignment_id": ALIGNMENT_ID_COLUMN,
        "metabolite_name": METABOLITE_NAME_COLUMN,
        "adduct": ADDUCT_COLUMN,
        "ontology": ONTOLOGY_COLUMN,
        "comment": COMMENT_COLUMN,
    }
    if len(row) <= columns["ontology"]:
        return False, "row is too short to carry an ontology"
    alignment_id = row[columns.get("alignment_id", ALIGNMENT_ID_COLUMN)].strip()
    name = row[columns["metabolite_name"]].strip()
    adduct = row[columns["adduct"]].strip()
    ontology = row[columns["ontology"]].strip()
    comment = row[columns["comment"]].strip() if len(row) > columns["comment"] else ""

    if not name or name == "Unknown":
        return False, "unannotated"
    if (internal_standard_ids is not None and alignment_id in internal_standard_ids) or (
        INTERNAL_STANDARD_NAME_FRAGMENT in name
    ):
        # The standards quantify the sample; they are not part of it. The resolver's IDs
        # catch a standard named without its label; "(d" catches a deuterated name.
        return False, "internal standard"
    if any(fragment in name for fragment in EXCLUDED_NAME_FRAGMENTS):
        # "w/o" an unresolved position, "RIKEN" an in-house identifier that names no compound.
        return False, "excluded compound name"
    if any(name.startswith(prefix) for prefix in EXCLUDED_NAME_PREFIXES):
        return False, "annotation below the reporting bar"
    if reference_matched_ids is not None and alignment_id not in reference_matched_ids:
        return False, "not reference-matched"
    if comment.lower().startswith(MANUAL_EXCLUSION_COMMENT_PREFIX):
        return False, "manually excluded in the comment column"
    if (ontology, adduct) not in rules["selected"]:
        return False, f"{ontology or '(no ontology)'} is not quantified as {adduct or '(no adduct)'}"
    if comment.startswith(NOT_QUANTIFIED_COMMENT_PREFIX):
        return False, f"{NOT_QUANTIFIED_REASON}: its standard did not resolve"
    unit = _comment_unit(comment)
    if unit == "Intensity":
        return False, f"{NOT_QUANTIFIED_REASON}: exported as raw intensity"
    if expected_unit and unit and unit != expected_unit:
        return False, f"{NOT_QUANTIFIED_REASON}: its unit is {unit}, not {expected_unit}"
    if unquantified_ids is not None and alignment_id in unquantified_ids:
        return False, f"{NOT_QUANTIFIED_REASON}: its values failed verification"
    return True, ""


def collation_key(text: str) -> tuple[tuple[int, str], ...]:
    """Order strings as .NET's culture comparison does for lipid names, without the process locale.

    Symbols sort before digits and digits before letters, and letters compare without case:
    'BileAcid' before 'BRSE', where an ordinal sort puts every capital first. Setting a locale
    instead would change it for every thread of the server.
    """
    return tuple(
        (2, char.casefold()) if char.isalpha() else (1, char) if char.isdigit() else (0, char)
        for char in text
    )


def merge_pos_neg(
    positive_path: str | Path,
    negative_path: str | Path,
    rules_path: str | Path | None,
    output_path: str | Path,
    *,
    rules: dict[str, Any] | None = None,
    internal_standard_ids: dict[str, Iterable[str]] | None = None,
    reference_matched_ids: dict[str, Iterable[str] | None] | None = None,
    expected_unit: str | None = None,
    unquantified_ids: dict[str, Iterable[str]] | None = None,
    not_quantified_path: str | Path | None = None,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Merge the two polarities into one table, refusing when the samples disagree.

    The per-polarity filters are keyed "positive" and "negative": the alignment IDs of the
    resolved internal standards, the IDs whose representative annotation is reference
    matched (from the candidate table; None leaves that filter off, as for an old export),
    and the IDs whose normalized values failed verification.
    """
    rules = rules if rules is not None else read_adduct_rules(rules_path)
    positive = describe_export(positive_path)
    negative = describe_export(negative_path)
    order = compare_sample_order(positive, negative)
    if not order["matched"]:
        raise MergeRefused(
            "The positive and negative exports do not describe the same samples in the "
            "same order, and the merged table pairs them by position: "
            + "; ".join(order["problems"])
        )

    kept: list[tuple[str, list[str]]] = []
    not_quantified: list[tuple[str, list[str], str]] = []
    dropped: dict[str, int] = {}
    dropped_by_polarity: dict[str, dict[str, int]] = {"positive": {}, "negative": {}}
    counts = {"positive": 0, "negative": 0}
    for label, export in (("positive", positive), ("negative", negative)):
        filters = {
            "internal_standard_ids": (internal_standard_ids or {}).get(label),
            "reference_matched_ids": (reference_matched_ids or {}).get(label),
            "expected_unit": expected_unit,
            "unquantified_ids": (unquantified_ids or {}).get(label),
        }
        filters = {key: set(value) if isinstance(value, (list, tuple, set, frozenset)) else value
                   for key, value in filters.items()}
        for row in export["rows"]:
            if not any(cell.strip() for cell in row):
                continue
            keep, reason = should_keep(row, rules, export["columns"], **filters)
            if keep:
                kept.append((label, row))
                counts[label] += 1
            elif reason.startswith(NOT_QUANTIFIED_REASON):
                not_quantified.append((label, row, reason))
            else:
                dropped[reason] = dropped.get(reason, 0) + 1
                dropped_by_polarity[label][reason] = dropped_by_polarity[label].get(reason, 0) + 1

    # Ontology then compound name, so a class reads as one block; stable, so equal keys keep
    # positive before negative, as the laboratory's program wrote them.
    columns = positive["columns"]
    kept.sort(key=lambda item: (
        collation_key(item[1][columns["ontology"]]), collation_key(item[1][columns["metabolite_name"]])
    ))

    output_path = Path(output_path)
    written = _write_table(output_path, positive["header"], [row for _, row in kept])
    side_file = None
    if not_quantified_path is not None:
        side_file = _write_table(
            Path(not_quantified_path), positive["header"], [row for _, row, _ in not_quantified]
        )
    elif not_quantified:
        for label, _, reason in not_quantified:
            dropped[reason] = dropped.get(reason, 0) + 1
            dropped_by_polarity[label][reason] = dropped_by_polarity[label].get(reason, 0) + 1

    def cell(row: list[str], key: str) -> str:
        index = columns.get(key, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    kept_pairs = {(cell(row, "ontology"), cell(row, "adduct")) for _, row in kept}
    not_quantified_pairs = {(cell(row, "ontology"), cell(row, "adduct")) for _, row, _ in not_quantified}
    ontologies_by_polarity = {
        label: {cell(row, "ontology") for polarity, row in kept if polarity == label}
        for label in ("positive", "negative")
    }
    report = {
        "output": str(output_path.resolve()),
        "format": {"delimiter": written["delimiter"], "newline": "CRLF", "encoding": written["encoding"]},
        "rules": {
            "path": rules.get("path", ""),
            "selected_combinations": len(rules["selected"]),
            "selected_classes": rules.get("selected_classes"),
            "selected_by_ion_mode": rules.get("selected_by_ion_mode"),
            "rows_considered": rules.get("rule_count"),
            "multi_adduct_classes": rules.get("multi_adduct_classes", []),
        },
        "sample_pairing": order,
        "kept_rows": len(kept),
        "kept_from_positive": counts["positive"],
        "kept_from_negative": counts["negative"],
        "dropped_rows": sum(dropped.values()),
        "dropped_reasons": dict(sorted(dropped.items(), key=lambda item: -item[1])),
        "dropped_by_polarity": {
            label: dict(sorted(reasons.items(), key=lambda item: -item[1]))
            for label, reasons in dropped_by_polarity.items()
        },
        "ontologies": sorted({cell(row, "ontology") for _, row in kept}),
        "ontologies_from_both_polarities": sorted(
            ontologies_by_polarity["positive"] & ontologies_by_polarity["negative"]
        ),
        "selected_pairs_without_rows": [
            {"class": klass, "adduct": adduct}
            for klass, adduct in sorted(rules["selected"] - kept_pairs - not_quantified_pairs)
        ],
        "possible_double_counts": _same_formula_groups(kept, cell),
        "not_quantified": {
            "output": side_file["path"] if side_file else None,
            "rows": len(not_quantified),
            "by_class": _count_by(not_quantified, lambda item: cell(item[1], "ontology")),
            "items": [
                {"polarity": label, "alignment_id": cell(row, "alignment_id"), "name": cell(row, "metabolite_name"),
                 "ontology": cell(row, "ontology"), "adduct": cell(row, "adduct"), "reason": reason}
                for label, row, reason in not_quantified
            ],
        },
        "warnings": written["warnings"] + (side_file["warnings"] if side_file else []),
        "inputs": {
            "positive": positive["path"], "positive_sha256": _sha256(positive_path),
            "negative": negative["path"], "negative_sha256": _sha256(negative_path),
            "rules_sha256": _sha256(rules["path"]) if rules.get("path") and Path(rules["path"]).is_file() else None,
        },
        "filters": {
            "internal_standard_ids": {
                label: len(set(values)) for label, values in (internal_standard_ids or {}).items()
            },
            "reference_matched_filter": {
                label: values is not None for label, values in (reference_matched_ids or {}).items()
            },
            "expected_unit": expected_unit,
        },
    }
    if report_path is not None:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        report["report"] = str(target.resolve())
    return report


def reference_matched_ids(candidate_path: str | Path) -> set[str]:
    """Alignment IDs whose representative annotation is a reference match, from .mdcandidate.tsv.

    A suggestion is the representative only when nothing matched, and the export no longer
    prefixes its name, so the candidate table is the only place this is still written.
    """
    from .lab_lipidomics import read_candidate_table

    table = read_candidate_table(candidate_path)
    return {
        spot_id for spot_id, rows in table["by_spot"].items()
        if any(row["is_representative"] and row["is_reference_matched"] for row in rows)
    }


def strip_polarity(name: str) -> str:
    value = name.strip()
    for suffix in ("_pos", "_neg", "_positive", "_negative"):
        if value.lower().endswith(suffix):
            return value[: -len(suffix)]
    return value


_strip_polarity = strip_polarity


def _comment_unit(comment: str) -> str:
    for part in comment.split("; "):
        if part.startswith("Normalized unit: "):
            return part[len("Normalized unit: "):].strip()
    return ""


def _same_formula_groups(kept: list[tuple[str, list[str]]], cell: Any) -> list[dict[str, Any]]:
    # The same molecule measured twice -- PE P-36:2 [M+H]+ and PE O-36:3 [M-H]- share a
    # formula, as does cholesterol as [M+NH4]+ and [M+H-H2O]+ -- is counted twice by any
    # sum over the table. Within one ontology, because isomers of two classes (PC 34:1 and
    # PE 37:1) share formulas too and are different lipids. Reported for the person to
    # decide; nothing is removed here.
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for label, row in kept:
        formula = cell(row, "formula")
        if not formula or formula.lower() == "null":
            continue
        groups.setdefault((cell(row, "ontology"), formula), []).append({
            "polarity": label, "alignment_id": cell(row, "alignment_id"), "name": cell(row, "metabolite_name"),
            "ontology": cell(row, "ontology"), "adduct": cell(row, "adduct"),
        })
    return [
        {"ontology": ontology, "formula": formula, "rows": rows}
        for (ontology, formula), rows in sorted(groups.items())
        if len({(item["polarity"], item["adduct"]) for item in rows}) > 1
    ]


def _count_by(items: Iterable[Any], key: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = key(item)
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _write_table(path: Path, header: list[list[str]], rows: list[list[str]]) -> dict[str, Any]:
    # Comma for a .csv, as the laboratory's program wrote it; tab otherwise. Quoting only where
    # a cell holds the delimiter, so a table without one is the plain join the program produced.
    delimiter = "," if path.suffix.lower() == ".csv" else "\t"
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerows(header)
    writer.writerows(rows)
    text = buffer.getvalue()
    warnings = []
    try:
        data = text.encode("ascii")
        encoding = "ascii"
    except UnicodeEncodeError:
        # A raw file name or a comment outside ASCII is the analyst's text, not an error; the
        # laboratory's program wrote ASCII, so the difference is said rather than refused.
        data = text.encode("utf-8")
        encoding = "utf-8"
        warnings.append(f"{path.name} holds non-ASCII text and is written as UTF-8.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": str(path.resolve()), "delimiter": delimiter, "encoding": encoding, "warnings": warnings}


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_rows(path: str | Path) -> list[list[str]]:
    with io.open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return [line.rstrip("\r\n").split("\t") for line in handle]

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
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Iterable

HEADER_ROW_COUNT = 5
CLASS_ROW = 0
FILE_TYPE_ROW = 1
INJECTION_ORDER_ROW = 2
COLUMN_NAME_ROW = 4

ALIGNMENT_ID_COLUMN = 0
METABOLITE_NAME_COLUMN = 3
ADDUCT_COLUMN = 4
ONTOLOGY_COLUMN = 11
COMMENT_COLUMN = 18

# A row the merged lipidome never carries, whatever the rule table says.
EXCLUDED_NAME_FRAGMENTS = ("(d", "w/o", "RIKEN")
EXCLUDED_NAME_PREFIXES = ("low score: ", "no MS2: ")
MANUAL_EXCLUSION_COMMENT_PREFIX = "x"


class MergeRefused(ValueError):
    """The two exports cannot be merged as they stand."""


def read_adduct_rules(path: str | Path) -> dict[str, Any]:
    """Read the class/adduct table that says which measurement of a class to keep.

    Tab-separated, one row per class and adduct, with a column saying whether that
    combination is the selected one. Only selected rows are returned.
    """
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
            "metabolite_name": column("Metabolite name", METABOLITE_NAME_COLUMN),
            "adduct": column("Adduct type", ADDUCT_COLUMN),
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
    left = [_strip_polarity(name) for name in positive["sample_names"]]
    right = [_strip_polarity(name) for name in negative["sample_names"]]
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
    row: list[str], rules: dict[str, Any], columns: dict[str, int] | None = None
) -> tuple[bool, str]:
    """Whether one aligned row belongs in the merged lipidome, and why not if it does not."""
    columns = columns or {
        "metabolite_name": METABOLITE_NAME_COLUMN,
        "adduct": ADDUCT_COLUMN,
        "ontology": ONTOLOGY_COLUMN,
        "comment": COMMENT_COLUMN,
    }
    if len(row) <= columns["ontology"]:
        return False, "row is too short to carry an ontology"
    name = row[columns["metabolite_name"]].strip()
    adduct = row[columns["adduct"]].strip()
    ontology = row[columns["ontology"]].strip()
    comment = row[columns["comment"]].strip() if len(row) > columns["comment"] else ""

    if not name or name == "Unknown":
        return False, "unannotated"
    if any(fragment in name for fragment in EXCLUDED_NAME_FRAGMENTS):
        # "(d" is a deuterated standard, "w/o" an unresolved position, "RIKEN" an
        # in-house identifier that names no compound.
        return False, "excluded compound name"
    if any(name.startswith(prefix) for prefix in EXCLUDED_NAME_PREFIXES):
        return False, "annotation below the reporting bar"
    if comment.lower().startswith(MANUAL_EXCLUSION_COMMENT_PREFIX):
        return False, "manually excluded in the comment column"
    if (ontology, adduct) not in rules["selected"]:
        return False, f"{ontology or '(no ontology)'} is not quantified as {adduct or '(no adduct)'}"
    return True, ""


def merge_pos_neg(
    positive_path: str | Path,
    negative_path: str | Path,
    rules_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Merge the two polarities into one table, refusing when the samples disagree."""
    rules = read_adduct_rules(rules_path)
    positive = describe_export(positive_path)
    negative = describe_export(negative_path)
    order = compare_sample_order(positive, negative)
    if not order["matched"]:
        raise MergeRefused(
            "The positive and negative exports do not describe the same samples in the "
            "same order, and the merged table pairs them by position: "
            + "; ".join(order["problems"])
        )

    kept: list[list[str]] = []
    dropped: dict[str, int] = {}
    counts = {"positive": 0, "negative": 0}
    for label, export in (("positive", positive), ("negative", negative)):
        for row in export["rows"]:
            if not any(cell.strip() for cell in row):
                continue
            keep, reason = should_keep(row, rules, export["columns"])
            if keep:
                kept.append(row)
                counts[label] += 1
            else:
                dropped[reason] = dropped.get(reason, 0) + 1

    # Ontology then compound name, so a class reads as one block.
    sort_columns = positive["columns"]
    kept.sort(
        key=lambda row: (row[sort_columns["ontology"]], row[sort_columns["metabolite_name"]])
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with io.open(output_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        for header_row in positive["header"]:
            writer.writerow(header_row)
        writer.writerows(kept)

    return {
        "output": str(output_path.resolve()),
        "rules": {
            "path": rules["path"],
            "selected_combinations": len(rules["selected"]),
            "selected_classes": rules["selected_classes"],
            "selected_by_ion_mode": rules["selected_by_ion_mode"],
            "rows_considered": rules["rule_count"],
        },
        "sample_pairing": order,
        "kept_rows": len(kept),
        "kept_from_positive": counts["positive"],
        "kept_from_negative": counts["negative"],
        "dropped_rows": sum(dropped.values()),
        "dropped_reasons": dict(sorted(dropped.items(), key=lambda item: -item[1])),
        "ontologies": sorted({row[sort_columns["ontology"]] for row in kept}),
    }


def _strip_polarity(name: str) -> str:
    value = name.strip()
    for suffix in ("_pos", "_neg", "_positive", "_negative"):
        if value.lower().endswith(suffix):
            return value[: -len(suffix)]
    return value


def _read_rows(path: str | Path) -> list[list[str]]:
    with io.open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return [line.rstrip("\r\n").split("\t") for line in handle]

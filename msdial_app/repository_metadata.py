from __future__ import annotations

import csv
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "msdial-repository-metadata.v1"
IDENTITY_FIELDS = {"sample_id", "raw_file", "source_name"}
ANALYSIS_FIELDS = [
    "file_path",
    "file_name",
    "file_type",
    "class_id",
    "acquisition_type",
    "batch_order",
    "analytical_order",
    "factor",
]


def metadata_workspace(project: dict[str, Any]) -> dict[str, Any]:
    rows = normalize_sample_rows(project.get("sample_metadata", []))
    return {
        "schema": SCHEMA,
        "repository": str(project.get("repository", "")),
        "accession": str(project.get("accession", "")),
        "title": str(project.get("title", "")),
        "public_url": str(project.get("public_url", "")),
        "metadata_url": str(project.get("metadata_url", "")),
        "publications": list(project.get("publications", [])),
        "metadata_sources": list(project.get("metadata_sources", [])),
        "repository_record": dict(project.get("repository_metadata", {})),
        "fields": describe_fields(rows),
        "rows": rows,
        "hierarchy": [],
        "separator": "_",
        "missing_value": "NA",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def metadata_workspace_from_file(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Repository metadata file was not found: {source}")
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if payload.get("schema") == SCHEMA:
        workspace = dict(payload)
        workspace["rows"] = normalize_sample_rows(workspace.get("rows", []))
        workspace["fields"] = describe_fields(workspace["rows"])
        workspace["source_file"] = str(source)
        return workspace
    linked = str(payload.get("sample_metadata_file") or "").strip()
    if linked:
        linked_path = Path(linked).expanduser()
        if not linked_path.is_absolute():
            linked_path = source.parent / linked_path
        if linked_path.resolve() != source and linked_path.is_file():
            workspace = metadata_workspace_from_file(linked_path)
            workspace["manifest_file"] = str(source)
            return workspace
    project = payload.get("project", payload)
    if not isinstance(project, dict):
        raise ValueError("The JSON does not contain repository project metadata.")
    workspace = metadata_workspace(project)
    workspace["source_file"] = str(source)
    return workspace


def normalize_sample_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, source in enumerate(rows, start=1):
        values = source.get("values", {})
        if not isinstance(values, dict):
            values = {}
        values = {
            clean_field_name(key): scalar_text(value)
            for key, value in values.items()
            if clean_field_name(key)
        }
        for key, value in source.items():
            name = clean_field_name(key)
            if name and name not in IDENTITY_FIELDS | {"values", "class_id"}:
                values.setdefault(name, scalar_text(value))
        sample_id = scalar_text(source.get("sample_id")) or f"sample_{index}"
        result.append(
            {
                "sample_id": sample_id,
                "source_name": scalar_text(source.get("source_name")) or sample_id,
                "raw_file": scalar_text(source.get("raw_file")),
                "values": values,
                "class_id": scalar_text(source.get("class_id")),
            }
        )
    return result


def describe_fields(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = list(rows)
    names = sorted(
        {name for row in rows for name in row.get("values", {})},
        key=str.casefold,
    )
    fields = []
    for name in names:
        values = [scalar_text(row.get("values", {}).get(name)) for row in rows]
        populated = [value for value in values if value]
        distinct = list(dict.fromkeys(populated))
        fields.append(
            {
                "name": name,
                "non_missing": len(populated),
                "missing": len(values) - len(populated),
                "unique_count": len(distinct),
                "examples": [_short_example(value) for value in distinct[:5]],
            }
        )
    return fields


def project_class_hierarchy(
    workspace: dict[str, Any],
    hierarchy: Iterable[str],
    separator: str = "_",
    missing_value: str = "NA",
) -> dict[str, Any]:
    selected = [clean_field_name(item) for item in hierarchy if clean_field_name(item)]
    known = {item["name"] for item in describe_fields(workspace.get("rows", []))}
    unknown = [item for item in selected if item not in known]
    if unknown:
        raise ValueError(f"Unknown metadata field(s): {', '.join(unknown)}")
    if not separator or any(char.isspace() for char in separator):
        raise ValueError("Class separator must be a non-whitespace character.")
    result = dict(workspace)
    rows = normalize_sample_rows(workspace.get("rows", []))
    missing_token = class_token(missing_value) or "NA"
    for row in rows:
        tokens = [
            class_token(row["values"].get(field_name, "")) or missing_token
            for field_name in selected
        ]
        row["class_id"] = separator.join(tokens) if tokens else "Sample"
    result.update(
        {
            "schema": SCHEMA,
            "rows": rows,
            "fields": describe_fields(rows),
            "hierarchy": selected,
            "separator": separator,
            "missing_value": missing_token,
            "projected_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return result


def apply_classes_to_analysis_files(
    workspace: dict[str, Any], analysis_files: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    rows = normalize_sample_rows(workspace.get("rows", []))
    indexes: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for key in metadata_match_keys(row.get("raw_file", ""), row.get("sample_id", "")):
            indexes.setdefault(key, []).append(row)
    files = [dict(item) for item in analysis_files]
    matches = []
    unmatched = []
    ambiguous = []
    for item in files:
        candidates: list[dict[str, Any]] = []
        seen: set[int] = set()
        for key in metadata_match_keys(item.get("file_path", ""), item.get("file_name", "")):
            for row in indexes.get(key, []):
                marker = id(row)
                if marker not in seen:
                    seen.add(marker)
                    candidates.append(row)
        if len(candidates) == 1:
            item["class_id"] = candidates[0].get("class_id") or "Sample"
            inferred_type = infer_analysis_file_type(candidates[0])
            if inferred_type != "Sample" or not str(item.get("file_type", "")).strip():
                item["file_type"] = inferred_type
            analytical_order = metadata_integer(
                candidates[0],
                ("analytical order", "injection order", "run order", "acquisition order"),
            )
            batch_order = metadata_integer(
                candidates[0], ("batch order", "batch number", "batch id")
            )
            if analytical_order is not None:
                item["analytical_order"] = analytical_order
            if batch_order is not None:
                item["batch_order"] = batch_order
            matches.append(
                {
                    "file_path": str(item.get("file_path", "")),
                    "sample_id": candidates[0]["sample_id"],
                    "class_id": item["class_id"],
                }
            )
        elif len(candidates) > 1:
            ambiguous.append(str(item.get("file_path", item.get("file_name", ""))))
        else:
            unmatched.append(str(item.get("file_path", item.get("file_name", ""))))
    return {
        "files": files,
        "matches": matches,
        "matched_count": len(matches),
        "unmatched": unmatched,
        "ambiguous": ambiguous,
    }


def infer_analysis_file_type(row: dict[str, Any]) -> str:
    values = row.get("values", {})
    focused = " ".join(
        scalar_text(value)
        for key, value in values.items()
        if any(token in key.casefold() for token in ("type", "class", "group", "sample"))
    )
    text = " ".join(
        (focused, scalar_text(row.get("sample_id")), scalar_text(row.get("raw_file")))
    ).casefold()
    if re.search(r"\b(blank|solvent blank|extraction blank|process blank)\b", text):
        return "Blank"
    if re.search(r"\b(qc|quality control|pooled qc|pool qc)\b", text):
        return "QC"
    if re.search(r"\b(standard|calibration|calibrant)\b", text):
        return "Standard"
    return "Sample"


def metadata_integer(row: dict[str, Any], field_names: Iterable[str]) -> int | None:
    names = {name.casefold() for name in field_names}
    for key, value in row.get("values", {}).items():
        normalized = re.sub(r"[_-]+", " ", str(key)).strip().casefold()
        if normalized not in names:
            continue
        try:
            number = int(float(scalar_text(value)))
        except ValueError:
            continue
        if number >= 0:
            return number
    return None


def save_metadata_review(
    workspace: dict[str, Any],
    destination: str | Path,
    analysis_files: Iterable[dict[str, Any]] = (),
) -> dict[str, str]:
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    accession = class_token(workspace.get("accession", "")) or "repository"
    json_path = destination / f"{accession}_repository_metadata_reviewed.json"
    tsv_path = destination / f"{accession}_sample_metadata_reviewed.tsv"
    analysis_path = destination / "analysis_files.csv"
    payload = dict(workspace)
    payload["schema"] = SCHEMA
    payload["saved_at"] = datetime.now(timezone.utc).isoformat()
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_metadata_tsv(tsv_path, payload)
    result = {"metadata_json": str(json_path), "metadata_tsv": str(tsv_path)}
    files = list(analysis_files)
    if files:
        applied = apply_classes_to_analysis_files(payload, files)
        _write_analysis_csv(analysis_path, applied["files"])
        result["analysis_files_csv"] = str(analysis_path)
    return result


def clean_field_name(value: Any) -> str:
    return re.sub(r"\s+", " ", scalar_text(value)).strip()


def scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, list):
        return "; ".join(item for item in (scalar_text(item) for item in value) if item)
    if isinstance(value, dict):
        if "value" in value:
            return scalar_text(value["value"])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def class_token(value: Any) -> str:
    text = unicodedata.normalize("NFKC", scalar_text(value))
    text = re.sub(r"[\s_]+", "-", text.strip())
    text = "".join(character if character.isalnum() or character in ".+-" else "-" for character in text)
    return re.sub(r"-+", "-", text).strip("-")


def _short_example(value: str, limit: int = 160) -> str:
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def metadata_match_keys(*values: Any) -> set[str]:
    keys: set[str] = set()
    for value in values:
        text = scalar_text(value).replace("\\", "/").rstrip("/")
        if not text:
            continue
        name = text.rsplit("/", 1)[-1].casefold()
        keys.add(name)
        lower = name
        for suffix in (".wiff2", ".wiff", ".mzml", ".mzxml", ".raw", ".cdf", ".lcd", ".qgd", ".abf", ".d"):
            if lower.endswith(suffix):
                keys.add(lower[: -len(suffix)])
                break
    return keys


def _write_metadata_tsv(path: Path, workspace: dict[str, Any]) -> None:
    fields = [item["name"] for item in workspace.get("fields", [])]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["sample_id", "source_name", "raw_file", "class_id", *fields],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in workspace.get("rows", []):
            writer.writerow(
                {
                    "sample_id": row.get("sample_id", ""),
                    "source_name": row.get("source_name", ""),
                    "raw_file": row.get("raw_file", ""),
                    "class_id": row.get("class_id", ""),
                    **{name: row.get("values", {}).get(name, "") for name in fields},
                }
            )


def _write_analysis_csv(path: Path, files: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANALYSIS_FIELDS, lineterminator="\n")
        writer.writeheader()
        for item in files:
            writer.writerow({name: item.get(name, "") for name in ANALYSIS_FIELDS})

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
    fields = describe_fields(rows)
    hierarchy = default_class_hierarchy(fields, len(rows))
    workspace = {
        "schema": SCHEMA,
        "repository": str(project.get("repository", "")),
        "accession": str(project.get("accession", "")),
        "title": str(project.get("title", "")),
        "public_url": str(project.get("public_url", "")),
        "metadata_url": str(project.get("metadata_url", "")),
        "publications": list(project.get("publications", [])),
        "metadata_sources": list(project.get("metadata_sources", [])),
        "repository_record": dict(project.get("repository_metadata", {})),
        "separation": str(project.get("separation", "Unknown")),
        "acquisition_mode": str(project.get("acquisition_mode", "Unknown")),
        "ion_mode": str(project.get("ion_mode", "Unknown")),
        "target_omics": _infer_target_omics(project, rows),
        "fields": fields,
        "rows": rows,
        "hierarchy": hierarchy,
        "separator": "_",
        "missing_value": "NA",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return project_class_hierarchy(workspace, hierarchy) if hierarchy else workspace


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


class ClassProposalMismatch(ValueError):
    """The approved proposal and the unit's samples do not describe the same study."""


def apply_class_proposal(workspace: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    """Project the Catalog's approved per-sample Class assignments onto the workspace rows.

    In a repository reanalysis the scientific decision belongs to the Catalog and the execution belongs
    here. The Catalog records a purpose, the fields it selected, a rationale, a contrast definition and
    one Class label per sample, and saving that proposal is a confirmation the user gives. Interactive
    then re-derived Class from a `hierarchy` argument and never read the assignments, so the approved
    artifact had no causal connection to what ran: a proposal could be approved and a different
    grouping executed, with every downstream report internally consistent about the grouping nobody
    approved.

    The assignments are the source of truth rather than the fields, because they are not always
    recoverable from the fields. A proposal may merge, rename or hand-correct labels for reasons that
    live in its rationale, and recombining `selected_fields` reproduces only the mechanical case.

    A sample the proposal does not assign, assigns twice, or assigns without the unit having it, is
    refused rather than defaulted: each of those is a different study from the approved one.
    """
    assignments = proposal.get("assignments") or []
    if not assignments:
        raise ClassProposalMismatch(
            "The approved Class proposal carries no per-sample assignments, so it cannot be applied."
        )
    rows = normalize_sample_rows(workspace.get("rows", []))
    labels: dict[str, str] = {}
    duplicated: list[str] = []
    for item in assignments:
        sample_id = str((item or {}).get("sample_id") or "").strip()
        if not sample_id:
            raise ClassProposalMismatch("An assignment in the approved proposal names no sample.")
        if sample_id in labels:
            duplicated.append(sample_id)
        labels[sample_id] = str((item or {}).get("class_label") or "").strip()

    unit_ids = [str(row.get("sample_id") or "").strip() for row in rows]
    unassigned = sorted({item for item in unit_ids if item and item not in labels})
    extra = sorted({item for item in labels if item not in set(unit_ids)})
    unlabelled = sorted({key for key, value in labels.items() if not value})
    problems = []
    if duplicated:
        problems.append(
            f"assigns {len(set(duplicated))} samples more than once: {sorted(set(duplicated))[:5]}"
        )
    if unassigned:
        problems.append(
            f"leaves {len(unassigned)} of the unit's samples unassigned: {unassigned[:5]}"
        )
    if extra:
        problems.append(f"assigns {len(extra)} samples the unit does not contain: {extra[:5]}")
    if unlabelled:
        problems.append(f"gives {len(unlabelled)} samples an empty Class label: {unlabelled[:5]}")
    if problems:
        raise ClassProposalMismatch(
            "The approved Class proposal and this analysis unit do not describe the same study. "
            "It " + "; it ".join(problems) + "."
        )

    for row in rows:
        label = labels[str(row.get("sample_id") or "").strip()]
        # Normalised the same way the hierarchy path normalises its tokens: MS-DIAL's Class column has
        # to survive the same characters whichever route produced the label.
        row["class_id"] = class_token(label) or "Sample"

    result = dict(workspace)
    result.update(
        {
            "schema": SCHEMA,
            "rows": rows,
            "fields": describe_fields(rows),
            "hierarchy": [
                clean_field_name(item) for item in proposal.get("selected_fields") or []
            ],
            "class_source": "catalog_class_proposal",
            "class_proposal_provenance": {
                "proposal_id": str(proposal.get("proposal_id") or ""),
                "unit_id": str(proposal.get("unit_id") or ""),
                "purpose": str(proposal.get("purpose") or ""),
                "selected_fields": list(proposal.get("selected_fields") or []),
                "rationale": str(proposal.get("rationale") or ""),
                "contrast_definition": dict(proposal.get("contrast_definition") or {}),
                "model": str(proposal.get("model") or ""),
                "prompt_hash": str(proposal.get("prompt_hash") or ""),
                "status": str(proposal.get("status") or ""),
                "assignment_count": len(labels),
                "warnings": list(proposal.get("warnings") or []),
            },
            "projected_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return result


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
            candidate = candidates[0]
            item["class_id"] = candidate.get("class_id") or "Sample"
            inferred_type = infer_analysis_file_type(candidate)
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
            acquisition = infer_acquisition_type(
                candidate, str(workspace.get("acquisition_mode", ""))
            )
            if acquisition:
                item["acquisition_type"] = acquisition
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


def default_class_hierarchy(
    fields: Iterable[dict[str, Any]], row_count: int
) -> list[str]:
    priorities = (
        ("group", "class", "factor value", "treatment", "condition", "genotype", "phenotype"),
        ("cell line", "strain", "region", "tissue", "organ", "sex", "age"),
        ("sample type", "sample source", "species"),
    )
    candidates = []
    for field in fields:
        field_name = str(field.get("name", ""))
        namespace = re.sub(r"\s+", "", field_name).casefold()
        if namespace.startswith(("analyticalcondition/", "softwaresetting/")):
            continue
        unique_count = int(field.get("unique_count") or 0)
        if unique_count <= 1 or (row_count > 1 and unique_count >= row_count):
            continue
        normalized = re.sub(r"[_/-]+", " ", field_name).casefold()
        rank = next(
            (
                group_index
                for group_index, tokens in enumerate(priorities)
                if any(token in normalized for token in tokens)
            ),
            None,
        )
        if rank is not None:
            candidates.append((rank, unique_count, field_name))
    candidates.sort(key=lambda item: (item[0], item[1], item[2].casefold()))
    return [item[2] for item in candidates[:3]]


def infer_acquisition_type(row: dict[str, Any], fallback: str = "") -> str:
    values = row.get("values", {})
    focused = " ".join(
        scalar_text(value)
        for key, value in values.items()
        if any(token in key.casefold() for token in ("instrument mode", "acquisition", "scan mode"))
    )
    text = f"{focused} {fallback}".casefold()
    if re.search(r"\b(aif|all[- ]?ions?)\b", text):
        return "AIF"
    if re.search(r"\b(dia|swath|data[- ]independent)\b", text):
        return "SWATH"
    if re.search(r"\b(dda|data[- ]dependent|auto\s*ms/?ms)\b", text):
        return "DDA"
    return ""


def _infer_target_omics(project: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    text = " ".join(
        [
            scalar_text(project.get("title")),
            scalar_text(project.get("description")),
            *(
                scalar_text(value)
                for row in rows[:10]
                for value in row.get("values", {}).values()
            ),
        ]
    ).casefold()
    return "Lipidomics" if re.search(r"\b(lipidome|lipidomics|lipidomic)\b", text) else "Metabolomics"


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

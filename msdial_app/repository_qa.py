from __future__ import annotations

import json
import re
from typing import Any

from .llm import chat_completion, resolve_llm_config


STANDARD_FIELD_PATTERN = re.compile(
    r"\b(internal standard|internal standards|is mixture|spike(?:d| in)? standard|reference standard)\b",
    re.IGNORECASE,
)


def repository_internal_standard_evidence(
    workspace: dict[str, Any], limit: int = 20
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in workspace.get("rows", []) or []:
        sample_id = str(row.get("sample_id") or "")
        for field, raw_value in (row.get("values", {}) or {}).items():
            field_text = str(field or "").strip()
            value = str(raw_value or "").strip()
            if not value or not STANDARD_FIELD_PATTERN.search(field_text):
                continue
            key = (field_text.casefold(), value.casefold())
            item = grouped.setdefault(
                key,
                {"field": field_text, "value": value, "sample_count": 0, "examples": []},
            )
            item["sample_count"] += 1
            if sample_id and len(item["examples"]) < 5:
                item["examples"].append(sample_id)
    return list(grouped.values())[:limit]


def propose_repository_qa_targets(
    workspace: dict[str, Any],
    workflow: dict[str, Any],
    llm_config: dict[str, Any],
) -> dict[str, Any]:
    evidence = repository_internal_standard_evidence(workspace)
    if not evidence:
        return {
            "mode": "no-metadata",
            "evidence": [],
            "candidates": [],
            "lines": [],
            "warnings": ["No internal-standard metadata field was found in this repository record."],
        }
    if resolve_llm_config(llm_config) is None:
        raise ValueError(
            "Internal-standard metadata was found, but exact QA targets require an Azure OpenAI "
            "API or local model connection in LLM & agent settings."
        )
    messages = [
        {
            "role": "system",
            "content": (
                "You draft LC-MS internal-standard QA targets from repository metadata. "
                "Return JSON only with a top-level candidates array. Each candidate may contain "
                "name, adduct, mz, rt, mz_tolerance, rt_tolerance, confidence, and rationale. "
                "Use monoisotopic precursor m/z values and respect ion polarity. Do not invent a "
                "retention time: set rt to null unless the supplied metadata explicitly states it. "
                "Limit the result to 8 diagnostically useful compounds. If a commercial mixture is "
                "named but an exact component or ion is uncertain, omit it. These are reviewable "
                "drafts, not authoritative identifications."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "repository": workspace.get("repository"),
                    "accession": workspace.get("accession"),
                    "ion_mode": workflow.get("ion_mode") or workspace.get("ion_mode"),
                    "target_omics": workflow.get("target_omics") or workspace.get("target_omics"),
                    "metadata_evidence": evidence,
                },
                ensure_ascii=False,
            ),
        },
    ]
    response = chat_completion(messages, llm_config, temperature=0.0)
    if not response:
        raise RuntimeError("The configured LLM did not return an internal-standard target draft.")
    payload = _json_object(response)
    candidates = []
    warnings = []
    for index, item in enumerate(payload.get("candidates", []) if isinstance(payload, dict) else []):
        candidate = _normalize_candidate(item, index)
        if candidate is None:
            warnings.append(f"Ignored incomplete or invalid LLM candidate at row {index + 1}.")
            continue
        candidates.append(candidate)
    if not candidates:
        warnings.append("The LLM returned no complete m/z candidates. Repository evidence remains available for manual review.")
    return {
        "mode": "repository-metadata-llm-draft",
        "evidence": evidence,
        "candidates": candidates,
        "lines": [_candidate_line(item) for item in candidates],
        "warnings": warnings,
        "review_required": True,
    }


def _json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("The LLM response was not valid JSON.") from error
    if not isinstance(payload, dict):
        raise ValueError("The LLM response must be a JSON object.")
    return payload


def _normalize_candidate(item: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    try:
        mz = float(item.get("mz"))
    except (TypeError, ValueError):
        return None
    if not 10 < mz < 5000:
        return None
    raw_rt = item.get("rt")
    try:
        rt = None if raw_rt is None or str(raw_rt).strip() == "" else float(raw_rt)
    except (TypeError, ValueError):
        rt = None
    if rt is not None and rt < 0:
        rt = None
    return {
        "name": str(item.get("name") or f"Repository IS candidate {index + 1}").strip(),
        "adduct": str(item.get("adduct") or "").strip(),
        "mz": mz,
        "rt": rt,
        "mz_tolerance": max(_positive_number(item.get("mz_tolerance"), 0.01), 1e-6),
        "rt_tolerance": max(_positive_number(item.get("rt_tolerance"), 0.5), 1e-6),
        "confidence": str(item.get("confidence") or "unrated").strip(),
        "rationale": str(item.get("rationale") or "").strip(),
    }


def _positive_number(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _candidate_line(item: dict[str, Any]) -> str:
    rt = "" if item["rt"] is None else f"{item['rt']:.6g}"
    return ",".join(
        (
            item["name"],
            item["adduct"],
            f"{item['mz']:.8g}",
            rt,
            f"{item['mz_tolerance']:.6g}",
            f"{item['rt_tolerance']:.6g}",
        )
    )

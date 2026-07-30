from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from typing import Any

from .llm import chat_completion, resolve_llm_config


PARAMETER_TERMS = (
    "minimum peak height",
    "mass slice width",
    "mass tolerance",
    "dot product",
    "matched peaks",
    "spectrum match",
    "retention time tolerance",
)


def build_search_query(workflow: dict[str, Any]) -> str:
    files = workflow.get("files", [])
    vendors = sorted({str(item.get("vendor", "")) for item in files if item.get("vendor")})
    instruments = sorted(
        {
            str(item.get("instrument_family", ""))
            for item in files
            if item.get("instrument_family")
        }
    )
    terms = [
        '"MS-DIAL"',
        str(workflow.get("target_omics", "")),
        str(workflow.get("ion_mode", "")),
        *vendors[:2],
        *instruments[:2],
    ]
    return " ".join(term for term in terms if term).strip()


def recommend_from_literature(
    workflow: dict[str, Any],
    llm_config: dict[str, Any],
    language: str = "ja",
) -> dict[str, Any]:
    if resolve_llm_config(llm_config) is None:
        raise ValueError(
            "Configure an Azure OpenAI or OpenAI-compatible API key before literature search."
        )
    query = build_search_query(workflow)
    works = search_crossref_open_access(query)
    if not works:
        message = (
            "条件に合う、明示的なオープンアクセスライセンス付き文献は見つかりませんでした。"
            "現在の装置形式別デフォルト値を開始点として使用してください。"
            if language == "ja"
            else
            "No explicitly licensed open-access studies matched this workflow. "
            "Use the current format-based defaults as the starting point."
        )
        return {"query": query, "summary": message, "works": [], "mode": "no-evidence"}

    evidence = "\n\n".join(
        (
            f"[{index}] {work['title']} ({work['year']}). "
            f"Citations: {work['citations']}. Confidence: {work['confidence']}.\n"
            f"DOI/URL: {work['url']}\nAbstract metadata: {work['abstract'] or 'Not available'}"
        )
        for index, work in enumerate(works, 1)
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are an evidence-constrained MS-DIAL parameter assistant. "
                "Use only the supplied open-access Crossref metadata. Never invent a numeric "
                "parameter. Recommend a numeric value only when it is explicitly present in "
                "the supplied title or abstract. Citation count measures influence, not "
                "parameter validity. If no explicit parameters are present, recommend using "
                "the application's format-based defaults and say that evidence was insufficient. "
                "Cite sources as [1], [2], etc."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Respond in {'Japanese' if language == 'ja' else 'English'}.\n"
                f"Workflow:\n{json.dumps(workflow, ensure_ascii=False)}\n\n"
                f"Open-access evidence:\n{evidence}"
            ),
        },
    ]
    summary = chat_completion(messages, llm_config, temperature=0.1)
    if not summary:
        summary = (
            "文献候補は取得できましたが、LLMによる根拠評価を完了できませんでした。"
            if language == "ja"
            else "Literature candidates were retrieved, but LLM evidence assessment failed."
        )
    return {
        "query": query,
        "summary": summary,
        "works": works,
        "mode": "crossref-oa-llm",
    }


def search_crossref_open_access(query: str, rows: int = 15) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "query.bibliographic": query,
            "rows": rows,
            "select": (
                "DOI,title,author,published,is-referenced-by-count,URL,"
                "abstract,license,link,container-title"
            ),
        }
    )
    request = urllib.request.Request(
        f"https://api.crossref.org/works?{params}",
        headers={
            "User-Agent": (
                "MSDIAL-Interactive/0.1 "
                "(https://github.com/systemsomicslab/MsdialWorkbench)"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    return parse_crossref_works(payload.get("message", {}).get("items", []))


def parse_crossref_works(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    works: list[dict[str, Any]] = []
    for item in items:
        licenses = [
            str(entry.get("URL", ""))
            for entry in item.get("license", [])
            if entry.get("URL")
        ]
        if not any("creativecommons.org" in license.lower() for license in licenses):
            continue
        title = " ".join(item.get("title", [])).strip()
        abstract = _strip_markup(str(item.get("abstract", "")))
        searchable = f"{title} {abstract}".lower()
        if "ms-dial" not in searchable and "msdial" not in searchable:
            continue
        citations = int(item.get("is-referenced-by-count", 0) or 0)
        direct_terms = [term for term in PARAMETER_TERMS if term in searchable]
        year = _published_year(item)
        confidence = confidence_label(citations, bool(abstract), len(direct_terms))
        doi = str(item.get("DOI", "")).strip()
        url = f"https://doi.org/{doi}" if doi else str(item.get("URL", ""))
        works.append(
            {
                "title": title or doi or "Untitled work",
                "year": year,
                "citations": citations,
                "url": url,
                "license": licenses[0],
                "abstract": abstract[:4000],
                "confidence": confidence,
                "direct_parameter_terms": direct_terms,
            }
        )
    works.sort(
        key=lambda work: (
            {"high": 3, "medium": 2, "low": 1}[work["confidence"]],
            work["citations"],
        ),
        reverse=True,
    )
    return works[:8]


def confidence_label(citations: int, has_abstract: bool, direct_terms: int) -> str:
    score = min(3.0, math.log10(citations + 1))
    score += 1.5 if has_abstract else 0
    score += min(3, direct_terms) * 1.5
    if direct_terms >= 2 and score >= 5:
        return "high"
    if direct_terms >= 1 or score >= 3:
        return "medium"
    return "low"


def _published_year(item: dict[str, Any]) -> int | None:
    parts = item.get("published", {}).get("date-parts", [])
    try:
        return int(parts[0][0])
    except (IndexError, TypeError, ValueError):
        return None


def _strip_markup(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()

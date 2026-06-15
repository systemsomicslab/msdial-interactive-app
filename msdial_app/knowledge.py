from __future__ import annotations

import json
import math
import re
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", " ", text)).strip()


def _tokens(value: str) -> set[str]:
    text = _normalize(value)
    words = {word for word in text.split() if len(word) >= 2}
    cjk = re.findall(r"[\u3040-\u30ff\u3400-\u9fff]{2,}", text)
    grams = {
        block[index : index + 2]
        for block in cjk
        for index in range(max(0, len(block) - 1))
    }
    return words | grams


class KnowledgeBase:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.cards: dict[str, list[dict[str, Any]]] = {"ja": [], "en": []}
        self.document_frequency: dict[str, Counter[str]] = {
            "ja": Counter(),
            "en": Counter(),
        }
        self._load()

    def _load(self) -> None:
        for language in ("ja", "en"):
            path = self.directory / f"qa_cards_{language}.jsonl"
            if not path.exists():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                card = json.loads(raw)
                search_text = "\n".join(
                    [
                        str(card.get("question", "")),
                        str(card.get("answer", "")),
                        str(card.get("feature", "")),
                        str(card.get("msdial_version", "")),
                        " ".join(card.get("platforms", [])),
                        " ".join(card.get("keywords", [])),
                    ]
                )
                card["_normalized"] = _normalize(search_text)
                card["_tokens"] = _tokens(search_text)
                self.cards[language].append(card)
                self.document_frequency[language].update(card["_tokens"])

    def count(self, language: str) -> int:
        return len(self.cards.get(language, []))

    def search(self, query: str, language: str = "ja", limit: int = 6) -> list[dict[str, Any]]:
        language = language if language in self.cards else "ja"
        cards = self.cards[language]
        query_normalized = _normalize(query)
        query_tokens = _tokens(query)
        scored: list[tuple[float, dict[str, Any]]] = []
        for card in cards:
            score = 0.0
            for token in query_tokens:
                if token in card["_normalized"]:
                    frequency = self.document_frequency[language][token]
                    score += math.log((len(cards) + 1) / (frequency + 1)) + 1
                if token in _normalize(card.get("question", "")):
                    score += 2.5
                if any(token in _normalize(keyword) for keyword in card.get("keywords", [])):
                    score += 1.5
            if query_normalized and query_normalized in card["_normalized"]:
                score += 8
            if score > 0:
                scored.append((score, card))
        scored.sort(
            key=lambda item: (item[0], str(item[1].get("date_last", ""))),
            reverse=True,
        )
        return [
            {
                key: value
                for key, value in card.items()
                if not key.startswith("_")
            }
            | {"score": round(score, 3)}
            for score, card in scored[:limit]
        ]

    def answer(
        self,
        query: str,
        language: str,
        workflow: dict[str, Any],
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cards = self.search(query, language, 6)
        generated = self._llm_answer(
            query,
            language,
            workflow,
            cards,
            llm_config or {},
        )
        if generated:
            return {"answer": generated, "cards": cards, "mode": "llm-grounded"}

        if not cards:
            answer = (
                "関連する公開Q&Aカードは見つかりませんでした。"
                if language == "ja"
                else "No related public Q&A cards were found."
            )
        else:
            heading = "関連する過去Q&Aからの回答候補:" if language == "ja" else "Relevant answers from prior Q&A:"
            answer = heading + "\n\n" + "\n\n".join(
                f"{index + 1}. {card.get('answer', '')}"
                for index, card in enumerate(cards[:3])
            )
        question = next_parameter_question(workflow, language)
        if question:
            answer += "\n\n" + question["prompt"]
        return {"answer": answer, "cards": cards, "mode": "local-retrieval"}

    def _llm_answer(
        self,
        query: str,
        language: str,
        workflow: dict[str, Any],
        cards: list[dict[str, Any]],
        config: dict[str, Any],
    ) -> str | None:
        import os

        provider = str(config.get("provider", "")).lower()
        endpoint = str(config.get("endpoint", "")).strip().rstrip("/")
        key = str(config.get("api_key", "")).strip()
        deployment = str(config.get("deployment", "")).strip()
        api_version = str(config.get("api_version", "2024-10-21")).strip()
        if not provider:
            provider = "azure" if os.environ.get("AZURE_OPENAI_ENDPOINT") else "local"
        if provider == "azure":
            endpoint = endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
            key = key or os.environ.get("AZURE_OPENAI_API_KEY", "")
            deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
            api_version = api_version or os.environ.get(
                "AZURE_OPENAI_API_VERSION",
                "2024-10-21",
            )
        if not endpoint or not key or not deployment or not cards:
            return None
        context = "\n\n".join(
            f"Q: {card.get('question', '')}\nA: {card.get('answer', '')}"
            for card in cards
        )
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an MS-DIAL workflow assistant. Answer only from the supplied "
                        "public Q&A cards and workflow state. Clearly label uncertainty. "
                        f"Respond in {'Japanese' if language == 'ja' else 'English'}."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Workflow state:\n{json.dumps(workflow, ensure_ascii=False)}\n\n"
                        f"Public Q&A cards:\n{context}\n\nQuestion:\n{query}"
                    ),
                },
            ],
            "temperature": 0.2,
        }
        if provider == "azure":
            url = (
                f"{endpoint}/openai/deployments/{deployment}/chat/completions"
                f"?api-version={api_version}"
            )
            headers = {"Content-Type": "application/json", "api-key": key}
        elif provider == "openai-compatible":
            url = (
                endpoint
                if endpoint.endswith("/chat/completions")
                else f"{endpoint}/chat/completions"
            )
            payload["model"] = deployment
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            }
        else:
            return None
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)
            return result["choices"][0]["message"]["content"]
        except (urllib.error.URLError, KeyError, IndexError, TimeoutError):
            return None


def next_parameter_question(
    workflow: dict[str, Any],
    language: str = "ja",
) -> dict[str, Any] | None:
    questions = [
        (
            not workflow.get("files"),
            "files",
            "解析する生データを追加してください。SCIEXは.wiffまたは.wiff2を解析ファイルとして受け付けます。",
            "Add the raw data to analyze. SCIEX .wiff and .wiff2 are accepted as primary files.",
        ),
        (
            not workflow.get("ion_mode"),
            "ion_mode",
            "イオンモードはPositiveですか、Negativeですか？",
            "Is the ion mode Positive or Negative?",
        ),
        (
            not workflow.get("target_omics"),
            "target_omics",
            "解析対象はMetabolomicsですか、Lipidomicsですか？",
            "Is the target omics Metabolomics or Lipidomics?",
        ),
        (
            not workflow.get("console_path"),
            "console_path",
            "このOSで使用するMS-DIAL Console実行ファイルを指定してください。",
            "Choose the MS-DIAL Console executable for this operating system.",
        ),
        (
            not workflow.get("template_path"),
            "template_path",
            "基準にするMS-DIALパラメーターファイルを指定してください。",
            "Choose the MS-DIAL parameter file to use as the template.",
        ),
        (
            not workflow.get("output_root"),
            "output_root",
            "解析結果を保存する出力フォルダーを指定してください。",
            "Choose the output folder for the analysis.",
        ),
        (
            workflow.get("target_omics") == "Lipidomics"
            and not workflow.get("lbm_path"),
            "lbm_path",
            "Lipidomics用のLBMライブラリーを指定してください。",
            "Choose the LBM library for the lipidomics workflow.",
        ),
    ]
    for condition, key, ja, en in questions:
        if condition:
            return {"key": key, "prompt": ja if language == "ja" else en}
    return None

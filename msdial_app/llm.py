from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def resolve_llm_config(config: dict[str, Any]) -> dict[str, str] | None:
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
    if provider not in {"azure", "openai-compatible"}:
        return None
    if not endpoint or not key or not deployment:
        return None
    return {
        "provider": provider,
        "endpoint": endpoint,
        "api_key": key,
        "deployment": deployment,
        "api_version": api_version,
    }


def chat_completion(
    messages: list[dict[str, str]],
    config: dict[str, Any],
    temperature: float = 0.2,
) -> str | None:
    resolved = resolve_llm_config(config)
    if resolved is None:
        return None
    payload: dict[str, Any] = {
        "messages": messages,
        "temperature": temperature,
    }
    if resolved["provider"] == "azure":
        url = (
            f"{resolved['endpoint']}/openai/deployments/{resolved['deployment']}"
            f"/chat/completions?api-version={resolved['api_version']}"
        )
        headers = {
            "Content-Type": "application/json",
            "api-key": resolved["api_key"],
        }
    else:
        url = (
            resolved["endpoint"]
            if resolved["endpoint"].endswith("/chat/completions")
            else f"{resolved['endpoint']}/chat/completions"
        )
        payload["model"] = resolved["deployment"]
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {resolved['api_key']}",
        }
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

"""Minimal client for OpenAI-compatible chat completion APIs.

DeepSeek, OpenAI, OpenRouter, and most other hosted providers (plus local
servers like Ollama) all accept the same request shape at a
`/chat/completions` endpoint, so one small client works for all of them.
Switching providers later means changing `llm_base_url` / `llm_model` in
config.yaml and which secret `LLM_API_KEY` is mapped to in
.github/workflows/explain-alerts.yml - not this file.
"""
from __future__ import annotations

import requests


class LLMError(RuntimeError):
    pass


def chat_completion(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float = 0.3,
    timeout: int = 60,
) -> str:
    if not api_key:
        raise LLMError("No LLM API key configured (LLM_API_KEY)")

    url = base_url.rstrip("/") + "/chat/completions"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages, "temperature": temperature},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise LLMError(f"LLM API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError) as exc:
        raise LLMError(f"Unexpected LLM API response shape: {str(data)[:300]}") from exc

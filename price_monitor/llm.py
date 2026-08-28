"""Minimal client for OpenAI-compatible chat completion APIs.

DeepSeek, OpenAI, OpenRouter, and most other hosted providers (plus local
servers like Ollama) all accept the same request shape at a
`/chat/completions` endpoint, so one small client works for all of them.
Switching providers later means changing `llm_base_url` / `llm_model_peak` /
`llm_model_offpeak` in config.yaml and which secret `LLM_API_KEY` is mapped
to in .github/workflows/explain-alerts.yml - not this file.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests


class LLMError(RuntimeError):
    pass


# DeepSeek charges half price outside these hours - see
# https://api-docs.deepseek.com/quick_start/pricing/ ("Peak hours are
# 01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through Friday; off-peak rates
# are half of the peak rates"). select_model() uses this to pick a cheaper/
# faster model during peak and a stronger one off-peak. If your provider has
# no such split, set llm_model_peak and llm_model_offpeak to the same value
# in config.yaml and select_model() always returns that value.
_PEAK_WINDOWS_UTC = [(1, 4), (6, 10)]  # (start_hour, end_hour), end exclusive


def is_peak_hour(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:  # Saturday, Sunday
        return False
    return any(start <= now.hour < end for start, end in _PEAK_WINDOWS_UTC)


def select_model(peak_model: str, offpeak_model: str, now: datetime | None = None) -> str:
    return peak_model if is_peak_hour(now) else offpeak_model


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

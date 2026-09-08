"""Log of sent alerts awaiting an AI-generated explanation.

Every alert the monitor sends gets appended here with everything a later,
separate step needs to explain it and edit the original Telegram message:
which message to edit (chat_id + message_id), the numbers that were in the
alert, and when it fired. That later step (not built yet) will fetch news for
the pending entries, ask an LLM to explain each move, edit the Telegram
messages via editMessageText, and mark the entries as explained.

The log is a flat JSON list, committed back to the repo by the workflow the
same way state.json is. The monitor only ever appends to it; nothing here
mutates or prunes existing entries yet.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def load_alerts_log(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def save_alerts_log(path: str, entries: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, sort_keys=True)
        f.write("\n")


def record_sent_alert(
    entries: list[dict],
    *,
    chat_id: str,
    message_id: int,
    symbol: str,
    message_text: str,
    last_close: float,
    last_return_pct: float,
    ewma_z: float,
    robust_z: float,
    volume_z: float,
    signal_type: str = "hourly",
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    entries.append({
        "chat_id": chat_id,
        "message_id": message_id,
        "symbol": symbol,
        "message_text": message_text,
        "sent_at": now.isoformat(),
        "last_close": last_close,
        "last_return_pct": last_return_pct,
        "ewma_z": ewma_z,
        "robust_z": robust_z,
        "volume_z": volume_z,
        "signal_type": signal_type,
        "explained": False,
    })


def pending_entries(entries: list[dict]) -> list[dict]:
    return [e for e in entries if not e.get("explained")]

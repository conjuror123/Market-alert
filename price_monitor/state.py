"""Persistent per-symbol state (last alert time) so alerts respect a cooldown window.

The state file is a small JSON blob that the GitHub Actions workflow commits back to
the repository after each run, so it survives between (stateless) workflow runs.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def is_in_cooldown(state: dict, symbol: str, cooldown_minutes: int, now: datetime | None = None) -> bool:
    entry = state.get(symbol)
    if not entry or "last_alert_at" not in entry:
        return False
    now = now or datetime.now(timezone.utc)
    last_alert = datetime.fromisoformat(entry["last_alert_at"])
    elapsed_minutes = (now - last_alert).total_seconds() / 60
    return elapsed_minutes < cooldown_minutes


def record_alert(state: dict, symbol: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    state.setdefault(symbol, {})["last_alert_at"] = now.isoformat()

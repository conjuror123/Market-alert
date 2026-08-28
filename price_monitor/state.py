"""Persistent per-symbol alert state, so repeat alerts respect an escalation-aware
cooldown instead of a flat "no more than one per N minutes" rule.

A flat cooldown has a real failure mode: two genuinely separate big events in the
same window would mean only the first gets reported. Instead, a new alert during the
cooldown window is only suppressed if it isn't meaningfully bigger than the one that
already fired - so routine chatter stays quiet, but an escalating situation still
gets through. `severity` is the caller's own scale (this app uses
max(|ewma_z|, |robust_z|, volume_z)) - `should_notify` only ever compares two
severities from the same caller, so the scale just has to be internally consistent.

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


def should_notify(
    state: dict,
    symbol: str,
    severity: float,
    cooldown_minutes: int,
    escalation_factor: float,
    override_severity: float | None = None,
    now: datetime | None = None,
) -> bool:
    """Whether a new alert for `symbol` should actually be sent.

    True if `severity` alone is already extreme enough to bypass cooldown
    entirely (`override_severity`, e.g. `price_zscore_override` - a genuinely
    extreme reading is never worth delaying just because something else fired
    recently); otherwise true if the cooldown window has elapsed since the last
    alert, or - even while still cooling down - if this event is at least
    `escalation_factor` times more severe than the one that triggered the last
    alert.
    """
    if override_severity is not None and severity >= override_severity:
        return True
    entry = state.get(symbol)
    if not entry or "last_alert_at" not in entry:
        return True
    now = now or datetime.now(timezone.utc)
    last_alert = datetime.fromisoformat(entry["last_alert_at"])
    elapsed_minutes = (now - last_alert).total_seconds() / 60
    if elapsed_minutes >= cooldown_minutes:
        return True
    last_severity = entry.get("last_severity", 0.0)
    return severity >= last_severity * escalation_factor


def record_alert(state: dict, symbol: str, severity: float, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    state.setdefault(symbol, {})
    state[symbol]["last_alert_at"] = now.isoformat()
    state[symbol]["last_severity"] = severity

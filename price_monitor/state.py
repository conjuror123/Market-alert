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


class CorruptState(ValueError):
    """state.json exists but is not JSON.

    Loading it as {} would forget the sent map and re-deliver every push still
    inside the 48-hour window. A missing file is a cold start; a truncated one
    is a failed write, and the run must stop rather than guess.
    """


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:
            raise CorruptState(f"{path} is not valid JSON") from exc


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)

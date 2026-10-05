"""The run's memory: data/state.json - the week's messages on the channel, the
open note, the calendar's last sends and the health streak.

A small JSON blob that the GitHub Actions workflow commits back to the
repository after each run, so it survives between (stateless) workflow runs.
"""
from __future__ import annotations

import json
import os


class CorruptState(ValueError):
    """state.json exists but is not JSON.

    Loading it as {} would forget the week's messages and send again every
    move still inside its 24 hours. A missing file is a cold start; a truncated one
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

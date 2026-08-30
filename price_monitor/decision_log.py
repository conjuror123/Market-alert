"""Append-only log of every anomaly-detection computation the monitor makes -
not just alerts that were actually sent, but every z-score computed for every
asset on every run, for both the hourly and the daily signal.

This exists separately from alerts_log.py (which only records alerts that
were actually sent to Telegram): the goal here is being able to answer, after
the fact, "what did the detector actually see and decide at time X" even when
nothing was sent - was a signal computed at all, what were the numbers, did
cooldown suppress it. It's also what a future re-tuning of thresholds can
check against real, already-observed values instead of only what a separate
backtest recomputes from scratch.

Same NDJSON-per-asset, append-only shape as candle_store.py, for the same
reason (small git diffs, kept forever).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from price_monitor.analysis import Signal
from price_monitor.candle_store import safe_asset_filename


def log_path(base_dir: str, source: str, symbol: str) -> str:
    return os.path.join(base_dir, f"{safe_asset_filename(source, symbol)}.ndjson")


def append_decision(
    path: str,
    signal: Signal,
    *,
    signal_type: str,
    notified: bool,
    price_zscore_threshold: float,
    price_zscore_override: float,
    volume_zscore_threshold: float,
    volume_zscore_override: float,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    row = {
        "run_at": now.isoformat(),
        "signal_type": signal_type,
        "symbol": signal.symbol,
        "last_close": signal.last_close,
        "last_return_pct": signal.last_return_pct,
        "ewma_z": signal.ewma_z,
        "robust_z": signal.robust_z,
        "volume_z": signal.volume_z,
        "price_alert": signal.price_alert,
        "volume_alert": signal.volume_alert,
        "notified": notified,
        "price_zscore_threshold": price_zscore_threshold,
        "price_zscore_override": price_zscore_override,
        "volume_zscore_threshold": volume_zscore_threshold,
        "volume_zscore_override": volume_zscore_override,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True, ensure_ascii=False))
        f.write("\n")

"""Configuration: YAML file defaults, overridable by environment variable.

This used to be a large file. It carried a list of sixteen assets and, for each
of them, its own dial on every threshold the old per-asset detector had - one
global compromise being no use when an equity-index future's volume seasonality
and a currency pair's stale-quote noise are not the same problem with the same
fix. That detector is gone (see price_monitor/__main__.py) and Tremor keeps its
own basket in config/basket.yaml, per instrument, in a form that does not need a
threshold typed in by hand at all.

What is left is what the delivery pass needs: who to message, where the state
and the event tables live, and the two mutes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "config.yaml")


@dataclass
class Config:
    # Silence for the Tremor side: the pushes and the Tuesday/Friday digest.
    # DEFAULT ON - that is, silent - because wiring the delivery is not the same
    # act as deciding to be interrupted by it, and running the pipeline with
    # nothing going out is a state worth being able to hold on purpose.
    #
    # It lives in config.yaml rather than on the external scheduler's side, for
    # a reason worth keeping: "we are deliberately silent" is a state of the
    # project and must be visible where the code is. A cron job disabled on
    # someone else's website looks like a breakage a month later, and there is
    # nobody left to work out which it was.
    tremor_alerts_muted: bool = True
    health_alert_after_failures: int = 3
    health_reminder_every_failures: int = 24
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    state_path: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(__file__), "..", "data", "state.json"))
    # Local store for economic_calendar.py / weekly_digest.py - see README.
    calendar_dir: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(__file__), "..", "data", "economic_calendar"))
    # Routed Tremor events, written by python -m tremor.saed and
    # python -m tremor.market and read by tremor_delivery.py. Both carry the
    # channel and digest slot the detector decided on; the delivery layer
    # decides nothing except what it has already sent.
    tremor_events_path: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(__file__), "..", "data", "tremor", "saed_events.parquet"))
    tremor_market_events_path: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(__file__), "..", "data", "tremor", "market_events.parquet"))
    # Kept because tremor/backfill.py fetches through these clients.
    coinbase_base_url: str = "https://api.exchange.coinbase.com"
    twelvedata_base_url: str = "https://api.twelvedata.com"
    # Free key from twelvedata.com. Never read from config.yaml; it comes from
    # the TWELVEDATA_API_KEY secret only, so that it cannot be committed.
    twelvedata_api_key: str = ""


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Config:
    raw: dict = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    def env_int(name: str, default: int) -> int:
        value = os.environ.get(name)
        return int(value) if value not in (None, "") else default

    def env_bool(name: str, default: bool) -> bool:
        value = os.environ.get(name)
        if value in (None, ""):
            return bool(default)
        return value.strip().lower() in ("1", "true", "yes", "on")

    return Config(
        tremor_alerts_muted=env_bool("TREMOR_ALERTS_MUTED",
                                     raw.get("tremor_alerts_muted", True)),
        health_alert_after_failures=env_int(
            "HEALTH_ALERT_AFTER_FAILURES", raw.get("health_alert_after_failures", 3)),
        health_reminder_every_failures=env_int(
            "HEALTH_REMINDER_EVERY_FAILURES", raw.get("health_reminder_every_failures", 24)),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        twelvedata_api_key=os.environ.get("TWELVEDATA_API_KEY", ""),
    )

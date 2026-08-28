"""Configuration loading: YAML file defaults, overridable via environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "config.yaml")


@dataclass
class Config:
    symbols: list[str]
    interval: str = "15m"
    lookback: int = 500
    mad_window: int = 288
    ewma_lambda: float = 0.94
    price_zscore_threshold: float = 3.0
    volume_zscore_threshold: float = 4.0
    volume_min_price_move_z: float = 1.5
    cooldown_minutes: int = 120
    min_history: int = 60
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    state_path: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(__file__), "..", "data", "state.json"))
    exchange_base_url: str = "https://api.exchange.coinbase.com"


def _split_env_list(value: str) -> list[str]:
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Config:
    raw: dict = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    symbols_env = os.environ.get("SYMBOLS")
    symbols = _split_env_list(symbols_env) if symbols_env else raw.get("symbols", [])
    if not symbols:
        raise ValueError("No symbols configured. Set SYMBOLS env var or config/config.yaml symbols list.")

    def env_float(name: str, default: float) -> float:
        v = os.environ.get(name)
        return float(v) if v not in (None, "") else default

    def env_int(name: str, default: int) -> int:
        v = os.environ.get(name)
        return int(v) if v not in (None, "") else default

    cfg = Config(
        symbols=symbols,
        interval=os.environ.get("INTERVAL", raw.get("interval", "15m")),
        lookback=env_int("LOOKBACK", raw.get("lookback", 500)),
        mad_window=env_int("MAD_WINDOW", raw.get("mad_window", 288)),
        ewma_lambda=env_float("EWMA_LAMBDA", raw.get("ewma_lambda", 0.94)),
        price_zscore_threshold=env_float(
            "PRICE_ZSCORE_THRESHOLD", raw.get("price_zscore_threshold", 3.0)),
        volume_zscore_threshold=env_float(
            "VOLUME_ZSCORE_THRESHOLD", raw.get("volume_zscore_threshold", 4.0)),
        volume_min_price_move_z=env_float(
            "VOLUME_MIN_PRICE_MOVE_Z", raw.get("volume_min_price_move_z", 1.5)),
        cooldown_minutes=env_int("COOLDOWN_MINUTES", raw.get("cooldown_minutes", 120)),
        min_history=env_int("MIN_HISTORY", raw.get("min_history", 60)),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
    state_path_override = os.environ.get("STATE_PATH")
    if state_path_override:
        cfg.state_path = state_path_override
    return cfg

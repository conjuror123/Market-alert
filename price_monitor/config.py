"""Configuration loading: YAML file defaults, overridable via environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "config.yaml")

VALID_SOURCES = {"coinbase", "yahoo"}


@dataclass
class AssetConfig:
    symbol: str
    source: str
    label: str


@dataclass
class Config:
    assets: list[AssetConfig]
    interval: str = "1h"
    lookback: int = 300
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
    coinbase_base_url: str = "https://api.exchange.coinbase.com"
    yahoo_base_url: str = "https://query1.finance.yahoo.com"


def _parse_assets(raw_assets: list) -> list[AssetConfig]:
    assets = []
    for i, item in enumerate(raw_assets):
        if not isinstance(item, dict) or "symbol" not in item or "source" not in item:
            raise ValueError(
                f"config.yaml assets[{i}] must be a mapping with at least "
                f"'symbol' and 'source' keys, got: {item!r}"
            )
        source = item["source"]
        if source not in VALID_SOURCES:
            raise ValueError(
                f"config.yaml assets[{i}] has unknown source '{source}'. "
                f"Supported: {sorted(VALID_SOURCES)}"
            )
        assets.append(AssetConfig(
            symbol=item["symbol"],
            source=source,
            label=item.get("label", item["symbol"]),
        ))
    return assets


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Config:
    raw: dict = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    assets = _parse_assets(raw.get("assets", []))
    if not assets:
        raise ValueError("No assets configured. Add an 'assets' list to config/config.yaml.")

    def env_float(name: str, default: float) -> float:
        v = os.environ.get(name)
        return float(v) if v not in (None, "") else default

    def env_int(name: str, default: int) -> int:
        v = os.environ.get(name)
        return int(v) if v not in (None, "") else default

    cfg = Config(
        assets=assets,
        interval=os.environ.get("INTERVAL", raw.get("interval", "1h")),
        lookback=env_int("LOOKBACK", raw.get("lookback", 300)),
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

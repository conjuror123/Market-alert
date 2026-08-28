"""Configuration loading: YAML file defaults, overridable via environment variables.

Each asset in config.yaml can also override any detection parameter for itself
(interval, lookback, thresholds, EWMA lambda, MAD window, min history, cooldown) -
see `Config.params_for`. With at most ~100 assets, a per-asset knob for every
parameter is affordable and worth it: the backtest showed real assets need real
per-asset tuning (an equity-index future's volume seasonality vs. a currency
pair's stale-quote noise are not the same problem with the same fix), so this
config is deliberately "one asset, one dial per parameter" rather than a single
global compromise.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "config.yaml")

VALID_SOURCES = {"coinbase", "yahoo"}

# Per-asset override keys, mapped to the (type, Config attribute) they fall back to.
_OVERRIDABLE = {
    "interval": (str, "interval"),
    "lookback": (int, "lookback"),
    "mad_window": (int, "mad_window"),
    "ewma_lambda": (float, "ewma_lambda"),
    "price_zscore_threshold": (float, "price_zscore_threshold"),
    "price_zscore_override": (float, "price_zscore_override"),
    "volume_zscore_threshold": (float, "volume_zscore_threshold"),
    "volume_zscore_override": (float, "volume_zscore_override"),
    "volume_min_price_move_z": (float, "volume_min_price_move_z"),
    "cooldown_minutes": (int, "cooldown_minutes"),
    "escalation_factor": (float, "escalation_factor"),
    "min_history": (int, "min_history"),
}


@dataclass
class AssetConfig:
    symbol: str
    source: str
    label: str
    overrides: dict = field(default_factory=dict)


@dataclass
class EffectiveParams:
    """Fully resolved detection parameters for one asset - global defaults with
    that asset's overrides applied."""
    interval: str
    lookback: int
    mad_window: int
    ewma_lambda: float
    price_zscore_threshold: float
    price_zscore_override: float
    volume_zscore_threshold: float
    volume_zscore_override: float
    volume_min_price_move_z: float
    cooldown_minutes: int
    escalation_factor: float
    min_history: int


@dataclass
class Config:
    assets: list[AssetConfig]
    interval: str = "1h"
    lookback: int = 300
    mad_window: int = 288
    ewma_lambda: float = 0.94
    price_zscore_threshold: float = 3.0
    # An overwhelming reading on EWMA or robust z alone (>= this) bypasses the dual
    # confirmation requirement - see the module docstring in analysis.py for why.
    price_zscore_override: float = 6.0
    volume_zscore_threshold: float = 4.0
    volume_zscore_override: float = 8.0
    volume_min_price_move_z: float = 1.5
    cooldown_minutes: int = 2880
    # A repeat alert during cooldown only sends if its severity is at least this many
    # times the severity that triggered the last alert - lets a genuinely escalating
    # situation through without flat-cooldown chatter for routine repeats.
    escalation_factor: float = 1.3
    min_history: int = 60
    health_alert_after_failures: int = 3
    health_reminder_every_failures: int = 24
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    state_path: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(__file__), "..", "data", "state.json"))
    coinbase_base_url: str = "https://api.exchange.coinbase.com"
    yahoo_base_url: str = "https://query1.finance.yahoo.com"

    def params_for(self, asset: AssetConfig) -> EffectiveParams:
        values = {}
        for key, (_, attr) in _OVERRIDABLE.items():
            values[attr] = asset.overrides.get(key, getattr(self, attr))
        return EffectiveParams(**values)


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
        overrides = {}
        for key, (type_, _) in _OVERRIDABLE.items():
            if key in item:
                try:
                    overrides[key] = type_(item[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"config.yaml assets[{i}] ('{item['symbol']}') has invalid "
                        f"{key}={item[key]!r}, expected {type_.__name__}"
                    ) from exc
        assets.append(AssetConfig(
            symbol=item["symbol"],
            source=source,
            label=item.get("label", item["symbol"]),
            overrides=overrides,
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
        price_zscore_override=env_float(
            "PRICE_ZSCORE_OVERRIDE", raw.get("price_zscore_override", 6.0)),
        volume_zscore_threshold=env_float(
            "VOLUME_ZSCORE_THRESHOLD", raw.get("volume_zscore_threshold", 4.0)),
        volume_zscore_override=env_float(
            "VOLUME_ZSCORE_OVERRIDE", raw.get("volume_zscore_override", 8.0)),
        volume_min_price_move_z=env_float(
            "VOLUME_MIN_PRICE_MOVE_Z", raw.get("volume_min_price_move_z", 1.5)),
        cooldown_minutes=env_int("COOLDOWN_MINUTES", raw.get("cooldown_minutes", 2880)),
        escalation_factor=env_float(
            "ESCALATION_FACTOR", raw.get("escalation_factor", 1.3)),
        min_history=env_int("MIN_HISTORY", raw.get("min_history", 60)),
        health_alert_after_failures=env_int(
            "HEALTH_ALERT_AFTER_FAILURES", raw.get("health_alert_after_failures", 3)),
        health_reminder_every_failures=env_int(
            "HEALTH_REMINDER_EVERY_FAILURES", raw.get("health_reminder_every_failures", 24)),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
    state_path_override = os.environ.get("STATE_PATH")
    if state_path_override:
        cfg.state_path = state_path_override
    return cfg

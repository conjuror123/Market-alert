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

VALID_SOURCES = {"coinbase", "yahoo", "twelvedata"}

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
    "daily_mad_window": (int, "daily_mad_window"),
    "daily_ewma_lambda": (float, "daily_ewma_lambda"),
    "daily_price_zscore_threshold": (float, "daily_price_zscore_threshold"),
    "daily_price_zscore_override": (float, "daily_price_zscore_override"),
    "daily_min_history": (int, "daily_min_history"),
    "daily_cooldown_minutes": (int, "daily_cooldown_minutes"),
    "daily_escalation_factor": (float, "daily_escalation_factor"),
}


@dataclass
class AssetConfig:
    symbol: str
    source: str
    label: str
    news_query: str = ""
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
    # Daily signal (see __main__.py / README "Дневной сигнал") - independent
    # detector on top of daily-resampled candles from the local candle store,
    # meant to catch a slow multi-hour grind that no single hourly return is
    # extreme enough to flag. Defaults below are placeholders pending backtest
    # calibration (see config.yaml).
    daily_mad_window: int = 300
    daily_ewma_lambda: float = 0.94
    daily_price_zscore_threshold: float = 4.0
    daily_price_zscore_override: float = 8.0
    daily_min_history: int = 30
    # 43200 = 30 days - deliberately much longer than the hourly cooldown
    # (itself 14 days): this signal only ever evaluates one already-closed day
    # at a time (see candle_store.daily_closes), so the same value recurs
    # unchanged for a full day regardless of how often the hourly workflow
    # re-checks it - a short cooldown here would do nothing useful.
    daily_cooldown_minutes: int = 43200
    daily_escalation_factor: float = 1.5


@dataclass
class Config:
    assets: list[AssetConfig]
    interval: str = "1h"
    lookback: int = 300
    mad_window: int = 288
    ewma_lambda: float = 0.94
    price_zscore_threshold: float = 7.0
    # An overwhelming reading on EWMA or robust z alone (>= this) bypasses the dual
    # confirmation requirement - see the module docstring in analysis.py for why.
    price_zscore_override: float = 22.0
    volume_zscore_threshold: float = 22.0
    volume_zscore_override: float = 22.0
    volume_min_price_move_z: float = 1.5
    cooldown_minutes: int = 2880
    # A repeat alert during cooldown only sends if its severity is at least this many
    # times the severity that triggered the last alert - lets a genuinely escalating
    # situation through without flat-cooldown chatter for routine repeats.
    escalation_factor: float = 1.3
    min_history: int = 60
    daily_mad_window: int = 300
    daily_ewma_lambda: float = 0.94
    daily_price_zscore_threshold: float = 4.0
    daily_price_zscore_override: float = 8.0
    daily_min_history: int = 30
    daily_cooldown_minutes: int = 43200
    daily_escalation_factor: float = 1.5
    health_alert_after_failures: int = 3
    health_reminder_every_failures: int = 24
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    state_path: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(__file__), "..", "data", "state.json"))
    alerts_log_path: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(__file__), "..", "data", "alerts_log.json"))
    # Permanent, append-only local price history (price_monitor/candle_store.py)
    # and per-run detector decisions (price_monitor/decision_log.py) - one file
    # per asset in each directory, kept forever. See README.
    candle_history_dir: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(__file__), "..", "data", "candle_history"))
    decision_log_dir: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(__file__), "..", "data", "decision_log"))
    coinbase_base_url: str = "https://api.exchange.coinbase.com"
    yahoo_base_url: str = "https://query1.finance.yahoo.com"
    twelvedata_base_url: str = "https://api.twelvedata.com"
    # Free API key from twelvedata.com - required for any asset with
    # source: twelvedata (currently the forex pairs). Not read from
    # config.yaml; comes from the TWELVEDATA_API_KEY secret only.
    twelvedata_api_key: str = ""
    # LLM used by the (separate, manually-triggered) "explain alerts" step - see
    # price_monitor/explain.py and price_monitor/llm.py. Any provider with an
    # OpenAI-compatible /chat/completions endpoint works here; switching providers
    # later is just these values plus which secret LLM_API_KEY is mapped to in
    # .github/workflows/explain-alerts.yml, no code changes.
    llm_base_url: str = "https://api.deepseek.com"
    # Model used during DeepSeek's peak-price hours (see llm.py) - cheaper/faster.
    llm_model_peak: str = "deepseek-v4-flash"
    # Model used off-peak, when DeepSeek charges half price - stronger model.
    llm_model_offpeak: str = "deepseek-v4-pro"
    llm_api_key: str = ""
    # Minimum age (hours) an alert must reach before "Explain Alerts" will
    # process it - the news search window is [alert+6h, alert+12h], so
    # running before the full 12h have passed would search a window that
    # hasn't fully happened yet. Should match NEWS_WINDOW_END_HOURS in
    # explain.py; see explain.py's _is_old_enough.
    explain_min_age_hours: float = 12.0

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
        label = item.get("label", item["symbol"])
        assets.append(AssetConfig(
            symbol=item["symbol"],
            source=source,
            label=label,
            news_query=item.get("news_query", label),
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
            "PRICE_ZSCORE_THRESHOLD", raw.get("price_zscore_threshold", 7.0)),
        price_zscore_override=env_float(
            "PRICE_ZSCORE_OVERRIDE", raw.get("price_zscore_override", 22.0)),
        volume_zscore_threshold=env_float(
            "VOLUME_ZSCORE_THRESHOLD", raw.get("volume_zscore_threshold", 22.0)),
        volume_zscore_override=env_float(
            "VOLUME_ZSCORE_OVERRIDE", raw.get("volume_zscore_override", 22.0)),
        volume_min_price_move_z=env_float(
            "VOLUME_MIN_PRICE_MOVE_Z", raw.get("volume_min_price_move_z", 1.5)),
        cooldown_minutes=env_int("COOLDOWN_MINUTES", raw.get("cooldown_minutes", 2880)),
        escalation_factor=env_float(
            "ESCALATION_FACTOR", raw.get("escalation_factor", 1.3)),
        min_history=env_int("MIN_HISTORY", raw.get("min_history", 60)),
        daily_mad_window=env_int("DAILY_MAD_WINDOW", raw.get("daily_mad_window", 300)),
        daily_ewma_lambda=env_float("DAILY_EWMA_LAMBDA", raw.get("daily_ewma_lambda", 0.94)),
        daily_price_zscore_threshold=env_float(
            "DAILY_PRICE_ZSCORE_THRESHOLD", raw.get("daily_price_zscore_threshold", 4.0)),
        daily_price_zscore_override=env_float(
            "DAILY_PRICE_ZSCORE_OVERRIDE", raw.get("daily_price_zscore_override", 8.0)),
        daily_min_history=env_int("DAILY_MIN_HISTORY", raw.get("daily_min_history", 30)),
        daily_cooldown_minutes=env_int(
            "DAILY_COOLDOWN_MINUTES", raw.get("daily_cooldown_minutes", 43200)),
        daily_escalation_factor=env_float(
            "DAILY_ESCALATION_FACTOR", raw.get("daily_escalation_factor", 1.5)),
        health_alert_after_failures=env_int(
            "HEALTH_ALERT_AFTER_FAILURES", raw.get("health_alert_after_failures", 3)),
        health_reminder_every_failures=env_int(
            "HEALTH_REMINDER_EVERY_FAILURES", raw.get("health_reminder_every_failures", 24)),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        llm_base_url=os.environ.get("LLM_BASE_URL", raw.get("llm_base_url", "https://api.deepseek.com")),
        llm_model_peak=os.environ.get(
            "LLM_MODEL_PEAK", raw.get("llm_model_peak", "deepseek-v4-flash")),
        llm_model_offpeak=os.environ.get(
            "LLM_MODEL_OFFPEAK", raw.get("llm_model_offpeak", "deepseek-v4-pro")),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        twelvedata_api_key=os.environ.get("TWELVEDATA_API_KEY", ""),
        explain_min_age_hours=env_float(
            "EXPLAIN_MIN_AGE_HOURS", raw.get("explain_min_age_hours", 12.0)),
    )
    state_path_override = os.environ.get("STATE_PATH")
    if state_path_override:
        cfg.state_path = state_path_override
    alerts_log_path_override = os.environ.get("ALERTS_LOG_PATH")
    if alerts_log_path_override:
        cfg.alerts_log_path = alerts_log_path_override
    candle_history_dir_override = os.environ.get("CANDLE_HISTORY_DIR")
    if candle_history_dir_override:
        cfg.candle_history_dir = candle_history_dir_override
    decision_log_dir_override = os.environ.get("DECISION_LOG_DIR")
    if decision_log_dir_override:
        cfg.decision_log_dir = decision_log_dir_override
    return cfg

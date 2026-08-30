"""Walk-forward backtest of the anomaly-detection thresholds against real history.

Simulates running the exact same `analyze()` logic the live monitor uses, one hour
at a time, over up to a year of real historical candles per asset - at each step it
only ever sees data up to that point (no lookahead), exactly like the hourly
GitHub Actions run does. Crucially, it also simulates *notification delivery*
(cooldown + escalation), reusing `price_monitor.state.should_notify`/`record_alert`
directly with historical timestamps: a "signal fired" and "a notification was
actually sent" are different things once cooldown is in the picture, and only the
second one is what the user actually experiences. Reports: how many notifications
you'd actually receive per week, whether the biggest historical moves would actually
have reached you (not just crossed a threshold internally), how much requiring both
signals (EWMA + robust z) buys over using either alone, and a naive fixed-percentage
comparison.

Usage:
    python -m price_monitor.backtest [--days 365] [--out data/backtest_results.json]

The output JSON is meant to be consumed by a small report/dashboard; a human-readable
summary is also printed to stdout.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

import requests

from price_monitor import candle_store, coinbase, twelvedata, yahoo
from price_monitor.analysis import ewma_volatility, log_returns, robust_z_score
from price_monitor.config import AssetConfig, Config, EffectiveParams, load_config
from price_monitor.state import record_alert, should_notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.backtest")

# Fixed hourly-move thresholds used for the "naive % rule" comparison, applied
# uniformly across every asset regardless of its typical volatility - this is
# exactly the kind of rule the smart z-score approach is meant to replace.
NAIVE_PCT_THRESHOLDS = [1.0, 2.0, 3.0]

# price_zscore_threshold values to sweep for the sensitivity report.
THRESHOLD_SWEEP = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5]

# Disables the volume channel for the daily report the same way config.yaml
# disables it for ES=F - the daily signal is price-only (see __main__.py).
_DAILY_VOLUME_DISABLED = 1e9

# Seconds per candle, by interval - used only to convert a series' raw period
# count into "days covered" for the report (build_report reuses the exact
# same code for both the hourly series and the daily-resampled one, which
# have very different periods-per-day).
_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}


def _periods_per_day(interval: str) -> float:
    return 86400 / _INTERVAL_SECONDS[interval]


def daily_params_view(params: EffectiveParams) -> EffectiveParams:
    """Maps this asset's daily_* fields onto the plain fields build_report()
    and simulate_notifications() already read, so the entire report-building
    pipeline can be reused unchanged for the daily signal - see README."""
    return replace(
        params,
        interval="1d",
        price_zscore_threshold=params.daily_price_zscore_threshold,
        price_zscore_override=params.daily_price_zscore_override,
        ewma_lambda=params.daily_ewma_lambda,
        mad_window=params.daily_mad_window,
        min_history=params.daily_min_history,
        cooldown_minutes=params.daily_cooldown_minutes,
        escalation_factor=params.daily_escalation_factor,
        volume_zscore_threshold=_DAILY_VOLUME_DISABLED,
        volume_zscore_override=_DAILY_VOLUME_DISABLED,
    )


def fetch_backtest_history(
    asset: AssetConfig, params: EffectiveParams, cfg: Config, days: float, session: requests.Session
):
    if asset.source == "coinbase":
        return coinbase.fetch_full_history(
            asset.symbol, params.interval, days, cfg.coinbase_base_url, session=session)
    if asset.source == "yahoo":
        return yahoo.fetch_klines(
            asset.symbol, params.interval, limit=1_000_000, base_url=cfg.yahoo_base_url,
            session=session, range_=f"{int(days) + 5}d")
    if asset.source == "twelvedata":
        return twelvedata.fetch_full_history(
            asset.symbol, params.interval, days, cfg.twelvedata_base_url,
            api_key=cfg.twelvedata_api_key, session=session)
    raise ValueError(f"Unknown source '{asset.source}'")


def compute_zscore_series(candles, ewma_lambda: float, mad_window: int, min_history: int) -> list[dict]:
    """One pass over the full history, computing exactly the signals `analyze()`
    would have seen at each hour - reused from price_monitor.analysis so this can
    never silently drift from the production formulas."""
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    returns = log_returns(closes)
    n = len(candles)

    # Mirrors analyze()'s stale-candle handling: a return of exactly 0.0 means a
    # repeated/stale quote, not genuine calm, so it's dropped from the baseline
    # window. Built up incrementally (not re-filtered from scratch each step) to
    # keep this an O(n) pass instead of O(n * mad_window).
    nonzero_returns: list[float] = [r for r in returns[:min_history - 1] if r != 0.0]

    series = []
    for i in range(min_history, n):
        if i - 1 < 0:
            continue
        last_return = returns[i - 1]
        history_returns = nonzero_returns[-mad_window:]

        ewma_sigma = ewma_volatility(history_returns, ewma_lambda)
        ewma_z = last_return / ewma_sigma if ewma_sigma > 0 else 0.0
        r_z = robust_z_score(last_return, history_returns)

        history_volumes = volumes[max(0, i - mad_window):i]
        v_z = robust_z_score(volumes[i], history_volumes) if len(history_volumes) >= 2 else 0.0

        series.append({
            "open_time": candles[i].open_time,
            "close": candles[i].close,
            "return_pct": (math.exp(last_return) - 1) * 100,
            "ewma_z": ewma_z,
            "robust_z": r_z,
            "volume_z": v_z,
        })

        if last_return != 0.0:
            nonzero_returns.append(last_return)
    return series


def _price_dual_fires(step: dict, threshold: float) -> bool:
    """Threshold-only dual confirmation, no override, no cooldown - the diagnostic
    used to show how much noise the "both signals must agree" rule cuts on its own."""
    return abs(step["ewma_z"]) >= threshold and abs(step["robust_z"]) >= threshold


def _price_alert(step: dict, params: EffectiveParams) -> bool:
    return (
        _price_dual_fires(step, params.price_zscore_threshold)
        or abs(step["ewma_z"]) >= params.price_zscore_override
        or abs(step["robust_z"]) >= params.price_zscore_override
    )


def _volume_confirmed_fires(step: dict, volume_threshold: float, min_price_move_z: float) -> bool:
    return (
        step["volume_z"] >= volume_threshold
        and max(abs(step["ewma_z"]), abs(step["robust_z"])) >= min_price_move_z
    )


def _volume_alert(step: dict, params: EffectiveParams) -> bool:
    return (
        _volume_confirmed_fires(step, params.volume_zscore_threshold, params.volume_min_price_move_z)
        or step["volume_z"] >= params.volume_zscore_override
    )


def _severity(step: dict) -> float:
    return max(abs(step["ewma_z"]), abs(step["robust_z"]), step["volume_z"])


def simulate_notifications(series: list[dict], params: EffectiveParams) -> set[int]:
    """Walks the series in order applying the exact same cooldown/escalation gate
    the live monitor uses (`price_monitor.state`), with each step's own historical
    timestamp standing in for "now". Returns the open_times that would actually have
    produced a Telegram notification - the real, user-facing alert rate."""
    state: dict = {}
    key = "asset"
    notified = set()
    for step in series:
        if not (_price_alert(step, params) or _volume_alert(step, params)):
            continue
        severity = _severity(step)
        now = datetime.fromtimestamp(step["open_time"], tz=timezone.utc)
        if should_notify(
            state, key, severity, params.cooldown_minutes, params.escalation_factor,
            override_severity=params.price_zscore_override, now=now,
        ):
            record_alert(state, key, severity, now=now)
            notified.add(step["open_time"])
    return notified


@dataclass
class AssetReport:
    label: str
    symbol: str
    source: str
    periods: int
    days_covered: float
    params: dict
    price_alerts_dual: int
    price_alerts_ewma_only: int
    price_alerts_robust_only: int
    volume_alerts_raw: int
    notifications_sent: int
    notifications_per_week: float
    biggest_moves: list[dict]
    naive_pct_alert_counts: dict[str, int]
    threshold_sweep: dict[str, float]


def build_report(asset: AssetConfig, series: list[dict], params: EffectiveParams) -> AssetReport:
    periods = len(series)
    days_covered = periods / _periods_per_day(params.interval) if periods else 0.0
    weeks = days_covered / 7 if days_covered else 1e-9

    # Diagnostic-only counts: raw signal crossings, no override, no cooldown - these
    # exist purely to show how much noise the dual-confirmation rule cuts.
    dual = [s for s in series if _price_dual_fires(s, params.price_zscore_threshold)]
    ewma_only = [s for s in series if abs(s["ewma_z"]) >= params.price_zscore_threshold]
    robust_only = [s for s in series if abs(s["robust_z"]) >= params.price_zscore_threshold]
    volume_raw = [
        s for s in series
        if _volume_confirmed_fires(s, params.volume_zscore_threshold, params.volume_min_price_move_z)
    ]

    notified = simulate_notifications(series, params)

    biggest = sorted(series, key=lambda s: abs(s["return_pct"]), reverse=True)[:5]
    biggest_moves = [
        {
            "open_time": s["open_time"],
            "return_pct": round(s["return_pct"], 3),
            "ewma_z": round(s["ewma_z"], 2),
            "robust_z": round(s["robust_z"], 2),
            "notified": s["open_time"] in notified,
        }
        for s in biggest
    ]

    naive_counts = {
        f"{pct:g}%": sum(1 for s in series if abs(s["return_pct"]) >= pct)
        for pct in NAIVE_PCT_THRESHOLDS
    }

    # Always include the asset's own current threshold in the swept set, even if it
    # isn't one of the round default steps, so its "current" point is exact. Each
    # swept value re-simulates the full notification pipeline (cooldown + escalation
    # included) at that price_zscore_threshold, keeping every other param fixed - so
    # this is the real per-week rate a given threshold would produce, not a raw
    # signal-crossing count.
    sweep_thresholds = sorted(set(THRESHOLD_SWEEP) | {params.price_zscore_threshold})
    sweep = {}
    for t in sweep_thresholds:
        trial_params = replace(params, price_zscore_threshold=t)
        trial_notified = simulate_notifications(series, trial_params)
        sweep[f"{t:g}"] = round(len(trial_notified) / weeks, 2)

    return AssetReport(
        label=asset.label,
        symbol=asset.symbol,
        source=asset.source,
        periods=periods,
        days_covered=round(days_covered, 1),
        params={
            "interval": params.interval,
            "price_zscore_threshold": params.price_zscore_threshold,
            "price_zscore_override": params.price_zscore_override,
            "volume_zscore_threshold": params.volume_zscore_threshold,
            "volume_zscore_override": params.volume_zscore_override,
            "volume_min_price_move_z": params.volume_min_price_move_z,
            "ewma_lambda": params.ewma_lambda,
            "mad_window": params.mad_window,
            "min_history": params.min_history,
            "cooldown_minutes": params.cooldown_minutes,
            "escalation_factor": params.escalation_factor,
        },
        price_alerts_dual=len(dual),
        price_alerts_ewma_only=len(ewma_only),
        price_alerts_robust_only=len(robust_only),
        volume_alerts_raw=len(volume_raw),
        notifications_sent=len(notified),
        notifications_per_week=round(len(notified) / weeks, 2),
        biggest_moves=biggest_moves,
        naive_pct_alert_counts=naive_counts,
        threshold_sweep=sweep,
    )


def print_summary(reports: list[AssetReport]) -> None:
    header = f"{'Актив':<32}{'Дней':>7}{'Увед.':>7}{'/неделю':>9}{'Recall':>8}"
    print(header)
    print("-" * len(header))
    total_caught = total_moves = 0
    for r in reports:
        caught = sum(1 for m in r.biggest_moves if m["notified"])
        total_caught += caught
        total_moves += len(r.biggest_moves)
        print(
            f"{r.label:<32}{r.days_covered:>7.0f}{r.notifications_sent:>7}{r.notifications_per_week:>9.2f}"
            f"{caught:>5}/{len(r.biggest_moves)}"
        )
    print()
    print(f"Итоговый recall на топ-5 крупнейших движений по каждому активу: {total_caught}/{total_moves}")
    print()
    print("Наивный % порог (одинаковый для всех активов) vs адаптивный z-score (сырые срабатывания, без cooldown):")
    for r in reports:
        naive_str = ", ".join(f"{k}: {v}" for k, v in r.naive_pct_alert_counts.items())
        print(f"  {r.label:<32} наивный[{naive_str}]  z-score(dual): {r.price_alerts_dual}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=365, help="History window in days (default: 365)")
    parser.add_argument("--out", default="data/backtest_results.json", help="Where to write the JSON report")
    parser.add_argument("--config", default=None, help="Path to config.yaml (default: config/config.yaml)")
    args = parser.parse_args()

    cfg = load_config(args.config) if args.config else load_config()
    session = requests.Session()

    reports = []
    daily_reports = []
    for asset in cfg.assets:
        params = cfg.params_for(asset)
        overridden = sorted(asset.overrides)
        log.info(
            "Fetching %.0f days of history for %s (%s)%s...",
            args.days, asset.label, asset.symbol,
            f" [overrides: {', '.join(overridden)}]" if overridden else "",
        )
        t0 = time.monotonic()
        candles = fetch_backtest_history(asset, params, cfg, args.days, session)
        log.info("  %d candles fetched in %.1fs", len(candles), time.monotonic() - t0)

        # Piggyback on this fetch to seed/extend the permanent local candle
        # history (see candle_store.py) - the daily signal in __main__.py
        # depends on this store having real day-scale history, and this is
        # the cheapest way to get there (already-fetched data, no extra
        # requests). Safe to call every time the backtest is re-run.
        history_path = candle_store.store_path(cfg.candle_history_dir, asset.source, asset.symbol)
        added = candle_store.merge_history(history_path, candles)
        if added:
            log.info("  %d new candles merged into local history (%s)", added, history_path)

        series = compute_zscore_series(candles, params.ewma_lambda, params.mad_window, params.min_history)
        report = build_report(asset, series, params)
        reports.append(report)
        caught = sum(1 for m in report.biggest_moves if m["notified"])
        log.info(
            "  %s: %d notifications over %.0f days (%.2f/week), recall %d/%d",
            asset.label, report.notifications_sent, report.days_covered,
            report.notifications_per_week, caught, len(report.biggest_moves),
        )

        daily_params = daily_params_view(params)
        daily_candles = candle_store.daily_closes(candles)
        daily_report = None
        if len(daily_candles) >= daily_params.min_history + 1:
            daily_series = compute_zscore_series(
                daily_candles, daily_params.ewma_lambda, daily_params.mad_window, daily_params.min_history)
            daily_report = build_report(asset, daily_series, daily_params)
            daily_reports.append(daily_report)
            d_caught = sum(1 for m in daily_report.biggest_moves if m["notified"])
            log.info(
                "  %s (daily): %d notifications over %.0f days (%.2f/week), recall %d/%d",
                asset.label, daily_report.notifications_sent, daily_report.days_covered,
                daily_report.notifications_per_week, d_caught, len(daily_report.biggest_moves),
            )
        else:
            log.info(
                "  %s (daily): only %d daily candles so far, need %d - skipping daily report",
                asset.label, len(daily_candles), daily_params.min_history + 1,
            )

    print()
    print_summary(reports)
    if daily_reports:
        print()
        print("Дневной сигнал (см. README):")
        print_summary(daily_reports)

    out = {
        "global_defaults": {
            "price_zscore_threshold": cfg.price_zscore_threshold,
            "price_zscore_override": cfg.price_zscore_override,
            "volume_zscore_threshold": cfg.volume_zscore_threshold,
            "volume_zscore_override": cfg.volume_zscore_override,
            "volume_min_price_move_z": cfg.volume_min_price_move_z,
            "ewma_lambda": cfg.ewma_lambda,
            "mad_window": cfg.mad_window,
            "min_history": cfg.min_history,
            "interval": cfg.interval,
            "cooldown_minutes": cfg.cooldown_minutes,
            "escalation_factor": cfg.escalation_factor,
            "daily_price_zscore_threshold": cfg.daily_price_zscore_threshold,
            "daily_price_zscore_override": cfg.daily_price_zscore_override,
            "daily_ewma_lambda": cfg.daily_ewma_lambda,
            "daily_mad_window": cfg.daily_mad_window,
            "daily_min_history": cfg.daily_min_history,
            "daily_cooldown_minutes": cfg.daily_cooldown_minutes,
            "daily_escalation_factor": cfg.daily_escalation_factor,
        },
        "assets": [asdict(r) for r in reports],
        "daily_assets": [asdict(r) for r in daily_reports],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    log.info("Wrote report to %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

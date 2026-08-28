"""Walk-forward backtest of the anomaly-detection thresholds against real history.

Simulates running the exact same `analyze()` logic the live monitor uses, one hour
at a time, over up to a year of real historical candles per asset - at each step it
only ever sees data up to that point (no lookahead), exactly like the hourly
GitHub Actions run does. This answers the question the config thresholds can't
answer on their own: how often would this actually have fired, would it have caught
the real big moves, and how much does requiring *both* signals (EWMA + robust z)
actually buy over using either alone or over a naive fixed-percentage rule.

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
from dataclasses import asdict, dataclass

import requests

from price_monitor import coinbase, yahoo
from price_monitor.analysis import ewma_volatility, log_returns, robust_z_score
from price_monitor.config import AssetConfig, Config, EffectiveParams, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.backtest")

# Fixed hourly-move thresholds used for the "naive % rule" comparison, applied
# uniformly across every asset regardless of its typical volatility - this is
# exactly the kind of rule the smart z-score approach is meant to replace.
NAIVE_PCT_THRESHOLDS = [1.0, 2.0, 3.0]

# price_zscore_threshold values to sweep for the sensitivity report.
THRESHOLD_SWEEP = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5]


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
    raise ValueError(f"Unknown source '{asset.source}'")


def compute_zscore_series(candles, ewma_lambda: float, mad_window: int, min_history: int) -> list[dict]:
    """One pass over the full history, computing exactly the signals `analyze()`
    would have seen at each hour - reused from price_monitor.analysis so this can
    never silently drift from the production formulas."""
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    returns = log_returns(closes)
    n = len(candles)

    series = []
    for i in range(min_history, n):
        if i - 1 < 0:
            continue
        last_return = returns[i - 1]
        history_returns = returns[max(0, i - 1 - mad_window):i - 1]

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
    return series


def _price_fires(step: dict, threshold: float) -> bool:
    return abs(step["ewma_z"]) >= threshold and abs(step["robust_z"]) >= threshold


def _volume_fires(step: dict, volume_threshold: float, min_price_move_z: float) -> bool:
    return (
        step["volume_z"] >= volume_threshold
        and max(abs(step["ewma_z"]), abs(step["robust_z"])) >= min_price_move_z
    )


@dataclass
class AssetReport:
    label: str
    symbol: str
    source: str
    hours: int
    days_covered: float
    params: dict
    price_alerts_dual: int
    price_alerts_ewma_only: int
    price_alerts_robust_only: int
    volume_alerts: int
    combined_alerts: int
    alerts_per_week: float
    biggest_moves: list[dict]
    naive_pct_alert_counts: dict[str, int]
    threshold_sweep: dict[str, int]


def build_report(asset: AssetConfig, series: list[dict], params: EffectiveParams) -> AssetReport:
    hours = len(series)
    days_covered = hours / 24 if hours else 0.0
    weeks = days_covered / 7 if days_covered else 1e-9

    dual = [s for s in series if _price_fires(s, params.price_zscore_threshold)]
    ewma_only = [s for s in series if abs(s["ewma_z"]) >= params.price_zscore_threshold]
    robust_only = [s for s in series if abs(s["robust_z"]) >= params.price_zscore_threshold]
    volume = [
        s for s in series
        if _volume_fires(s, params.volume_zscore_threshold, params.volume_min_price_move_z)
    ]

    dual_times = {s["open_time"] for s in dual}
    volume_times = {s["open_time"] for s in volume}
    combined_count = len(dual_times | volume_times)

    biggest = sorted(series, key=lambda s: abs(s["return_pct"]), reverse=True)[:5]
    biggest_moves = [
        {
            "open_time": s["open_time"],
            "return_pct": round(s["return_pct"], 3),
            "ewma_z": round(s["ewma_z"], 2),
            "robust_z": round(s["robust_z"], 2),
            "caught_by_dual_signal": _price_fires(s, params.price_zscore_threshold),
        }
        for s in biggest
    ]

    naive_counts = {
        f"{pct:g}%": sum(1 for s in series if abs(s["return_pct"]) >= pct)
        for pct in NAIVE_PCT_THRESHOLDS
    }

    # Always include the asset's own current threshold in the swept set, even if it
    # isn't one of the round default steps, so its "current" point is exact.
    sweep_thresholds = sorted(set(THRESHOLD_SWEEP) | {params.price_zscore_threshold})
    sweep = {
        f"{t:g}": sum(1 for s in series if _price_fires(s, t))
        for t in sweep_thresholds
    }

    return AssetReport(
        label=asset.label,
        symbol=asset.symbol,
        source=asset.source,
        hours=hours,
        days_covered=round(days_covered, 1),
        params={
            "interval": params.interval,
            "price_zscore_threshold": params.price_zscore_threshold,
            "volume_zscore_threshold": params.volume_zscore_threshold,
            "volume_min_price_move_z": params.volume_min_price_move_z,
            "ewma_lambda": params.ewma_lambda,
            "mad_window": params.mad_window,
            "min_history": params.min_history,
            "cooldown_minutes": params.cooldown_minutes,
        },
        price_alerts_dual=len(dual),
        price_alerts_ewma_only=len(ewma_only),
        price_alerts_robust_only=len(robust_only),
        volume_alerts=len(volume),
        combined_alerts=combined_count,
        alerts_per_week=round(combined_count / weeks, 2),
        biggest_moves=biggest_moves,
        naive_pct_alert_counts=naive_counts,
        threshold_sweep=sweep,
    )


def print_summary(reports: list[AssetReport]) -> None:
    header = f"{'Актив':<32}{'Дней':>7}{'Алертов':>9}{'/неделю':>9}{'EWMA-only':>11}{'Robust-only':>12}"
    print(header)
    print("-" * len(header))
    for r in reports:
        print(
            f"{r.label:<32}{r.days_covered:>7.0f}{r.combined_alerts:>9}{r.alerts_per_week:>9.2f}"
            f"{r.price_alerts_ewma_only:>11}{r.price_alerts_robust_only:>12}"
        )
    print()
    print("Наивный % порог (одинаковый для всех активов) vs адаптивный z-score:")
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

        series = compute_zscore_series(candles, params.ewma_lambda, params.mad_window, params.min_history)
        report = build_report(asset, series, params)
        reports.append(report)
        log.info(
            "  %s: %d alerts over %.0f days (%.2f/week)",
            asset.label, report.combined_alerts, report.days_covered, report.alerts_per_week,
        )

    print()
    print_summary(reports)

    out = {
        "global_defaults": {
            "price_zscore_threshold": cfg.price_zscore_threshold,
            "volume_zscore_threshold": cfg.volume_zscore_threshold,
            "volume_min_price_move_z": cfg.volume_min_price_move_z,
            "ewma_lambda": cfg.ewma_lambda,
            "mad_window": cfg.mad_window,
            "min_history": cfg.min_history,
            "interval": cfg.interval,
        },
        "assets": [asdict(r) for r in reports],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    log.info("Wrote report to %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

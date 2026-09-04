"""The volatility-forecast detector, end to end.

features -> HAR forecast -> adaptive threshold -> cooldown -> alerts.

Replaces the trigger sum of §4.2 and the fixed gate of §4.1. What it keeps from
v1 is everything below the score: the bars, the reference calendar, the quality
gate, the per-asset metrics, the basket aggregates, and the cooldown that turns a
firing into an alert a person can read.
"""
from __future__ import annotations

import logging
import os

import pandas as pd

from meals import features, forecast, threshold, windows

log = logging.getLogger("meals.detector")

DEFAULT_SCORES_PATH = os.path.join("data", "meals", "detector_scores.parquet")
DEFAULT_ALERTS_PATH = os.path.join("data", "meals", "detector_alerts.parquet")


def run(basket_frame: pd.DataFrame, quantile: float = threshold.QUANTILE,
        cooldown_bars: int = windows.CLUSTER_COOLDOWN) -> pd.DataFrame:
    """Scores every hour and returns the frame with the score, threshold and alerts."""
    matrix = features.build(basket_frame)
    columns = features.model_columns(matrix)
    y = features.target(basket_frame["m_weighted_median"])

    score = forecast.fit_predict(matrix, y, columns)
    limit, fires = threshold.adaptive(score, quantile)
    alerts = threshold.cooldown(fires, cooldown_bars)

    return matrix.assign(target=y, forecast=score, threshold=limit,
                         fires=fires, alert=alerts)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from meals import cross_section

    parser = argparse.ArgumentParser(description="Volatility-forecast detector")
    parser.add_argument("--basket-metrics", default=cross_section.DEFAULT_BASKET_METRICS_PATH)
    parser.add_argument("--scores-out", default=DEFAULT_SCORES_PATH)
    parser.add_argument("--alerts-out", default=DEFAULT_ALERTS_PATH)
    parser.add_argument("--quantile", type=float, default=threshold.QUANTILE)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    basket_frame = pd.read_parquet(args.basket_metrics).set_index("hour_utc").sort_index()
    scored = run(basket_frame, args.quantile)

    skill = forecast.skill(scored["forecast"], scored["target"])
    # The benchmark is the same machinery given only the trailing 24-hour
    # volatility - "tomorrow looks like today", fitted the same way, so the
    # comparison isolates what the extra horizons and signals contribute rather
    # than measuring a level offset.
    matrix = features.build(basket_frame)
    benchmark = forecast.skill(
        forecast.fit_predict(matrix, features.target(basket_frame["m_weighted_median"]),
                             ["rv_h24"]), scored["target"])
    log.info("forecast skill on %d hours: R2 %.4f, correlation %.4f",
             skill["n"], skill["r2"], skill["correlation"])
    log.info("  benchmark, trailing 24h volatility fitted alone: R2 %.4f, correlation %.4f",
             benchmark["r2"], benchmark["correlation"])
    log.info("alerts %d over %d hours (%.2f a week)",
             int(scored["alert"].sum()), len(scored),
             scored["alert"].sum() / (len(scored) / (24 * 7)))

    for path, frame in ((args.scores_out, scored),
                        (args.alerts_out, scored[scored["alert"]])):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        frame.reset_index().to_parquet(path, index=False, compression="zstd")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

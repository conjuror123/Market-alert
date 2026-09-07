"""The market-wide channel: routing what the volatility detector already finds.

The single-asset detector cannot see a macro day, and not by oversight. It
fires on moves the market does not explain, so on a day when everything moves
together the common factor absorbs the move and the residual is small BY
DEFINITION. Measured: on the SVB collapse, the August 2024 yen carry unwind and
the 2024 US election it pushed nothing at all, and on the yen unwind that was
individually correct - SPY fell 1.01% and QQQ rose 1.82% that hour, ordinary
moves for them. Thirteen instruments across all five blocks fired that day, and
the event was that breadth.

The repository already has the detector for this. tremor.detector forecasts
basket volatility and marks its own alerts; what it did not have was any way to
reach a person. Its alerts carried no severity, so nothing could decide whether
one was worth an interruption, and they went nowhere. This module supplies that
missing half, and nothing else - it does not re-score, re-threshold or re-fit
anything the detector does.

The tier comes from the same ladder as everything else, so that a market alert
is directly comparable to an instrument one - "the market's most disorderly hour
in two years" sits on the same scale as "gold's biggest move in two years",
which is the whole reason for routing them together rather than inventing a
second, parallel notion of importance.

WHAT THE LADDER IS ASKED OF, and why it is not the forecast itself. The
forecast is not stationary: its LEVEL depends on which instruments are in the
basket, and this basket grew. Fitted on an expanding window the ladder therefore
never fires again - measured, the routine level settled at -5.21 during the
crypto-heavy 2021-2022 warm-up and the forecast never once reached it
afterwards, topping out between -5.46 and -5.74 in every half-year since. Zero
market events, silently, forever.

So the ladder is asked of the EXCEEDANCE, forecast minus the detector's own
adaptive threshold. That threshold is a rolling quantile and already tracks the
level, so the difference is stationary by construction and "how far past its own
bar did the market go" means the same thing in 2021 and in 2026. This is the
general lesson and it is worth stating once: an expanding-window return period
belongs on a stationary quantity, and a level that moves with basket
composition is not one.

ONE-SIDED. The forecast is a log volatility, a level rather than a deviation,
and only high is an event. That is passed explicitly (see severity.magnitudes)
because the default is wrong here and wrong silently: the forecast runs from
-8.26 to -4.98, so a ladder built on its magnitude ranks the calmest hours on
record as the rarest, and every market alert it produced would name a dead
market.

NO RETENTION GATE. The other channels wait to see whether a move held, because
price impact splits into a permanent part and a transitory one. A volatility
regime is not a price move and does not split that way: a spike that subsides
within the day was still a real spike, and the market really was disorderly
while it lasted. There is nothing here for a retention test to reject, so the
tiers are routed on severity alone.
"""
from __future__ import annotations

import logging
import os

import numpy as np
import pandas as pd

from tremor import routing, severity, windows

log = logging.getLogger("tremor.market")

# The detector's score and its own adaptive bar, both log volatilities. The
# ladder reads their difference; see the module docstring for why not the level.
FORECAST_COLUMN = "forecast"
THRESHOLD_COLUMN = "threshold"
EXCESS_COLUMN = "excess"
LEVEL_PREFIX = "mkt_level"
DEFAULT_EVENTS_PATH = os.path.join("data", "tremor", "market_events.parquet")


def tiers(scores: pd.DataFrame) -> pd.DataFrame:
    """Puts the detector's exceedance on the same severity ladder as everything else."""
    frame = scores if "hour_utc" in scores.columns else scores.reset_index()
    frame = frame[["hour_utc", FORECAST_COLUMN, THRESHOLD_COLUMN]].dropna()
    frame = frame.sort_values("hour_utc").reset_index(drop=True)
    frame[EXCESS_COLUMN] = frame[FORECAST_COLUMN] - frame[THRESHOLD_COLUMN]
    return severity.annotate(frame, column=EXCESS_COLUMN, prefix=LEVEL_PREFIX,
                             tier_column="tier", fallback=None, two_sided=False)


def events(scored: pd.DataFrame,
           cooldown_bars: int = windows.CLUSTER_COOLDOWN) -> pd.DataFrame:
    """Collapses consecutive scored hours into market events.

    The cooldown is the cluster one, not the single-asset one: a market that is
    disorderly stays disorderly for hours, and reporting each of those hours
    would be reporting one event many times. As with the single-asset automaton
    the pause does not cancel anything, it merges - and the event keeps the
    worst tier it reached, because a stretch that starts merely unusual and
    becomes a once-a-year hour is a once-a-year event.
    """
    fired = scored["tier"].notna().to_numpy(dtype=bool)
    if not fired.any():
        return pd.DataFrame({c: pd.Series(dtype=t) for c, t in (
            ("event_id", "object"), ("hour_utc", "int64"), ("tier", "string"),
            ("excess", "float64"), ("repeat_count", "int64"))})

    hours = scored["hour_utc"].to_numpy()
    tier = scored["tier"].to_numpy(dtype=object)
    score = scored[EXCESS_COLUMN].to_numpy()
    rank = {name: i for i, name in enumerate(severity.TIERS)}

    rows: list[dict] = []
    open_at: int | None = None
    for i in np.flatnonzero(fired):
        if open_at is not None and i - open_at < cooldown_bars:
            rows[-1]["repeat_count"] += 1
            if rank.get(tier[i], -1) > rank.get(rows[-1]["tier"], -1):
                rows[-1]["tier"] = tier[i]
                rows[-1]["excess"] = float(score[i])
            continue
        open_at = i
        rows.append({"event_id": f"market:{int(hours[i])}", "hour_utc": int(hours[i]),
                     "tier": str(tier[i]), "excess": float(score[i]),
                     "repeat_count": 0})

    out = pd.DataFrame(rows)
    out["tier"] = out["tier"].astype("string")
    return out


def build(scores: pd.DataFrame) -> pd.DataFrame:
    """Market events, tiered and routed, ready to be delivered alongside the rest."""
    routed = routing.route(events(tiers(scores)), require_retention=False)
    return routed.assign(basis=pd.Series("market", index=routed.index,
                                         dtype="string"))


def main(argv: list[str] | None = None) -> int:
    import argparse

    from tremor import detector, versioning

    parser = argparse.ArgumentParser(description="Market-wide events (routed)")
    parser.add_argument("--scores", default=detector.DEFAULT_SCORES_PATH)
    parser.add_argument("--events-out", default=DEFAULT_EVENTS_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not os.path.exists(args.scores):
        log.error("No detector scores - run python -m tremor.detector first")
        return 2

    out = build(pd.read_parquet(args.scores))
    config, run_id = versioning.versions_for()
    os.makedirs(os.path.dirname(args.events_out) or ".", exist_ok=True)
    versioning.stamp(out, config, run_id).to_parquet(
        args.events_out, index=False, compression="zstd")

    log.info("market events %d", len(out))
    if not out.empty:
        span = (out["hour_utc"].max() - out["hour_utc"].min()) / (3600 * 24 * 365.25)
        log.info("by tier: %s", out["tier"].value_counts().to_dict())
        log.info("by channel: %s", out["channel"].value_counts().to_dict())
        if span > 0:
            log.info("%.1f market events a year", len(out) / span)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

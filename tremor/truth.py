"""Truth labelling and the baseline for the backtest (spec §7).

This is the yardstick, not part of the detector. §7 defines a significant hour
without reference to anything the system computes: over the following 24
reference-calendar hours at least one block of the basket accumulates a move
beyond the Q99 of that block's own historical distribution of such 24-hour moves,
with the Q99 taken on train. The SI-Index, the triggers and the thresholds play no
part in it - which is the point, because a yardstick derived from the thing being
measured cannot measure it.

For the same reason this module is deliberately NOT in versioning.CONFIG_INPUTS.
If it were, adjusting the evaluation would move config_version and invalidate a
calibration frozen under the old one. The labels are stamped with the raw-data
fingerprint instead, and with no config_version at all: they are a function of the
market data and of this file, and of nothing the detector does. A config_version
on them would suggest they move when a threshold moves, and they do not.

Two readings of §7 that the text leaves open are settled here, both recorded in
docs/tremor-deviations.md: the accumulated move is compared in ABSOLUTE value, and
the 2% of the baseline is read on the log scale like every other return in the
system.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from tremor.basket import Basket

log = logging.getLogger("tremor.truth")

# §7 sets train at 2021-01-01 .. 2023-12-31 and test at 2024-01-01 onwards, and
# says in the same breath that recalibrating after a test run requires "a new
# version and a NEW TEST PERIOD". The first test has been run and reported, so
# this is that new period: train now ends 2025-01-01 and test is 2025 onwards.
#
# It is the largest clean split still available, and it is not a comfortable one -
# 33 episodes against the first test's 42. Holding out 2026 alone would have left
# 16, at which point recall moves in steps of six percentage points and any F1 is
# barely distinguishable from noise. Read the second test as weaker evidence than
# the first: its window lay inside the range whose aggregate result has already
# been seen once, even though nothing was tuned against it. The genuinely
# untouched period is the data that accumulates from 2026-09 onwards.
TRAIN_END = datetime(2025, 1, 1, tzinfo=timezone.utc)

# §7: the horizon of both the labelling protocol and the baseline, in
# reference-calendar hours.
HORIZON = 24

# §7: the baseline detector - not a definition of truth, a yardstick to beat.
BASELINE_ASSET = "twelvedata:SPY"
# "|the move accumulated over the following 24 RCH| > 2%". Everything in this
# system accumulates LOG returns, because they add up and simple returns do not,
# so a 2% move is ln(1.02) on that scale rather than 0.02. The difference is a
# fifth of a percent of the threshold - immaterial to the result, but stating it
# is cheaper than leaving a reader to guess which of the two was meant.
BASELINE_MOVE = math.log(1.02)

# A move that reaches this fraction of its block's Q99 counts as a NEAR miss.
# §7 labels an hour by a hard cut at the 99th percentile, and against that cut a
# detector firing before a move that reaches 0.99 of it is recorded as entirely
# wrong. Measured on train: of 37 alerts scored as failures, not one landed on a
# quiet market, the median reached 0.69 of the threshold and 14 came within 25%
# of it. The strict label stays exactly as §7 defines it and remains what is
# optimised and reported; this second one exists so the size of that cliff is
# visible next to it.
NEAR_FRACTION = 0.75

DEFAULT_LABELS_PATH = os.path.join("data", "tremor", "truth_labels.parquet")
DEFAULT_THRESHOLDS_PATH = os.path.join("data", "tremor", "truth_thresholds.parquet")


def block_returns(metrics: dict[str, pd.DataFrame], basket: Basket,
                  hours: pd.Index) -> pd.DataFrame:
    """M_block,t for every block, on the reference-hour grid (§2.3).

    The weighted median of the returns of the assets of the block that are in
    session and have a valid bar. The median is taken plain rather than weighted,
    and that is not a shortcut: under the equal-weight rule of §2.3 every asset
    within a block carries the same weight, so the two coincide.

    Only basket assets count. The instruments outside the basket have a block
    (§8.1) but it serves SAED alone and takes no part in basket aggregates.

    An hour where a block has no valid bar at all yields NaN, not zero - the block
    is not known to have stood still, it is simply unobserved.
    """
    out = {}
    for block, assets in basket.by_block().items():
        columns = [a.asset_id for a in assets if a.asset_id in metrics]
        if not columns:
            continue
        panel = pd.DataFrame({
            aid: metrics[aid].set_index("hour_utc")["r"] for aid in columns})
        out[block] = panel.reindex(hours).median(axis=1, skipna=True)
    return pd.DataFrame(out, index=hours).sort_index(axis=1)


def forward_sum(values: np.ndarray, horizon: int = HORIZON) -> tuple[np.ndarray, np.ndarray]:
    """Accumulates the NEXT `horizon` positions: sum of values[i+1 .. i+horizon].

    Positions, not clock time. The array is the reference-calendar grid, so a step
    is one reference hour and the weekend is simply not in it - which is what "24
    RCH" means and what makes a Friday-evening window end on Monday rather than in
    an empty Saturday.

    An hour whose forward window runs off the end of the history gets NaN. That is
    not the same as zero and must not be labelled False later: the window is
    missing, not calm - the same distinction the export draws with truncated_right.

    A gap inside the window contributes nothing to the sum, and the count of hours
    actually observed is returned alongside so a caller can see how much of the
    window was real.
    """
    present = np.isfinite(values)
    cumulative = np.concatenate([[0.0], np.cumsum(np.where(present, values, 0.0))])
    counted = np.concatenate([[0], np.cumsum(present.astype(np.int64))])

    n = len(values)
    index = np.arange(n)
    complete = index + horizon < n
    low = np.minimum(index + 1, n)
    high = np.minimum(index + horizon + 1, n)

    accumulated = np.where(complete, cumulative[high] - cumulative[low], np.nan)
    observed = np.where(complete, counted[high] - counted[low], 0)
    return accumulated, observed


def trailing_sum(values: np.ndarray, horizon: int = HORIZON) -> tuple[np.ndarray, np.ndarray]:
    """Accumulates the PRECEDING `horizon` positions: sum of values[i-horizon .. i-1].

    The mirror of forward_sum, and the reason it exists is §7's baseline. The text
    defines that baseline on "the move accumulated over the FOLLOWING 24 RCH" -
    the same window the truth label is built from. Read literally it is not a
    detector at all but a second look at the answer: at hour t it reads hours
    after t, which nothing running live can do, and it scores near the label by
    construction because SPY sits inside the equity block the label measures.

    Scoring the SI-Index against that is scoring a forecast against a recording.
    So both are computed: baseline_spy exactly as §7 writes it, and
    baseline_spy_trailing on the hours BEFORE t - "SPY has just moved 2%, expect
    more", which is a detector one could actually run and therefore the one worth
    beating. Which of the two §7 intends is recorded in docs/tremor-deviations.md.
    """
    present = np.isfinite(values)
    cumulative = np.concatenate([[0.0], np.cumsum(np.where(present, values, 0.0))])
    counted = np.concatenate([[0], np.cumsum(present.astype(np.int64))])

    n = len(values)
    index = np.arange(n)
    complete = index >= horizon
    low = np.maximum(index - horizon, 0)

    accumulated = np.where(complete, cumulative[index] - cumulative[low], np.nan)
    observed = np.where(complete, counted[index] - counted[low], 0)
    return accumulated, observed


def train_thresholds(accumulated: pd.DataFrame, train_end: datetime = TRAIN_END
                     ) -> pd.DataFrame:
    """Q99 of |accumulated move| per block, on TRAIN ONLY (§7).

    Computed on train and then applied to the whole history, test included. That
    asymmetry is the entire discipline of §7: a threshold fitted on the period it
    is later scored against would score the fit, not the detector.
    """
    boundary = int(train_end.timestamp())
    train = accumulated[accumulated.index < boundary]
    rows = []
    for block in accumulated.columns:
        moves = train[block].abs().dropna()
        rows.append({"block": block,
                     "q99_abs_move": float(moves.quantile(0.99)) if len(moves) else np.nan,
                     "n_train_windows": int(len(moves))})
    return pd.DataFrame(rows)


def label(metrics: dict[str, pd.DataFrame], basket: Basket, hours: pd.Index,
          train_end: datetime = TRAIN_END, horizon: int = HORIZON
          ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The §7 labels and the per-block thresholds they rest on."""
    blocks = block_returns(metrics, basket, hours)

    accumulated, observed = {}, {}
    for block in blocks.columns:
        acc, seen = forward_sum(blocks[block].to_numpy(dtype="float64"), horizon)
        accumulated[block], observed[block] = acc, seen
    accumulated = pd.DataFrame(accumulated, index=hours)
    observed = pd.DataFrame(observed, index=hours)

    thresholds = train_thresholds(accumulated, train_end)
    limits = thresholds.set_index("block")["q99_abs_move"]

    # How far past its own threshold each block went. The ratio, not the raw move:
    # blocks have wildly different volatility, and only the ratio makes "which
    # block drove this hour" a comparable question.
    ratio = accumulated.abs().div(limits.reindex(accumulated.columns), axis=1)
    # forward_sum leaves NaN exactly where the forward window runs off the end,
    # and it does so for every block at once - they share one grid.
    complete = accumulated.notna().all(axis=1)

    frame = pd.DataFrame({"hour_utc": hours}, index=hours)
    for block in accumulated.columns:
        frame[f"acc_{block}"] = accumulated[block]
        frame[f"observed_{block}"] = observed[block]

    significant = (ratio > 1).any(axis=1)
    frame["significant"] = pd.array(np.where(complete, significant, pd.NA),
                                    dtype="boolean")
    # idxmax refuses an all-NaN row, and the incomplete tail is exactly that.
    # Ratios are non-negative, so -1 loses to every real one.
    leader = ratio.fillna(-1.0).idxmax(axis=1)
    frame["driving_block"] = leader.where(complete & significant)
    frame["max_ratio"] = ratio.max(axis=1).where(complete)
    near = (ratio > NEAR_FRACTION).any(axis=1)
    frame["significant_near"] = pd.array(np.where(complete, near, pd.NA),
                                         dtype="boolean")

    baseline = metrics.get(BASELINE_ASSET)
    if baseline is None:
        log.warning("%s absent - the §7 baseline cannot be computed", BASELINE_ASSET)
        frame["acc_baseline"] = np.nan
        frame["acc_baseline_trailing"] = np.nan
        for column in ("baseline_spy", "baseline_spy_trailing"):
            frame[column] = pd.array([pd.NA] * len(frame), dtype="boolean")
    else:
        series = baseline.set_index("hour_utc")["r"].reindex(hours)
        values = series.to_numpy(dtype="float64")

        acc, seen = forward_sum(values, horizon)
        frame["acc_baseline"] = acc
        frame["observed_baseline"] = seen
        frame["baseline_spy"] = pd.array(
            np.where(np.isfinite(acc), np.abs(acc) > BASELINE_MOVE, pd.NA),
            dtype="boolean")

        before, seen_before = trailing_sum(values, horizon)
        frame["acc_baseline_trailing"] = before
        frame["baseline_spy_trailing"] = pd.array(
            np.where(np.isfinite(before), np.abs(before) > BASELINE_MOVE, pd.NA),
            dtype="boolean")
        del seen_before

    return frame.reset_index(drop=True), thresholds


def main(argv: list[str] | None = None) -> int:
    import argparse

    from tremor import cross_section, pipeline, sessions, versioning
    from tremor.basket import load_basket

    parser = argparse.ArgumentParser(description="Truth labels and the baseline (§7)")
    parser.add_argument("--metrics-dir", default=pipeline.DEFAULT_METRICS_DIR)
    parser.add_argument("--labels-out", default=DEFAULT_LABELS_PATH)
    parser.add_argument("--thresholds-out", default=DEFAULT_THRESHOLDS_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    basket = load_basket()
    metrics = pipeline.load_all(basket, args.metrics_dir)
    if not metrics:
        log.error("No per-asset metrics - run python -m tremor.pipeline first")
        return 2

    panel_hours = cross_section.build_panel(metrics, "r").index
    hours = pd.Index([h for h in panel_hours
                      if sessions.is_reference_hour(int(h), basket.anchor_exchange_tz)],
                     name="hour_utc")

    frame, thresholds = label(metrics, basket, hours)

    # No config_version: see the module docstring. The labels are a function of
    # the raw data and of this file, and the detector's configuration is not among
    # their inputs.
    fingerprint = versioning.data_fingerprint()
    frame = frame.assign(data_fingerprint=fingerprint)

    for path, data in ((args.labels_out, frame),
                       (args.thresholds_out, thresholds.assign(
                           data_fingerprint=fingerprint))):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data.to_parquet(path, index=False, compression="zstd")

    labelled = frame["significant"].notna()
    train = frame["hour_utc"] < int(TRAIN_END.timestamp())
    log.info("hours %d, labelled %d (the last %d have no forward window)",
             len(frame), int(labelled.sum()), int((~labelled).sum()))
    for name, mask in (("train", train & labelled), ("test", ~train & labelled)):
        part = frame[mask]
        if part.empty:
            continue
        log.info("  %-5s hours %6d | significant %5d (%.1f%%) | near %5d (%.1f%%) "
                 "| baseline trailing %5d (%.1f%%)",
                 name, len(part), int(part["significant"].sum()),
                 100 * part["significant"].mean(),
                 int(part["significant_near"].sum()),
                 100 * part["significant_near"].mean(),
                 int(part["baseline_spy_trailing"].fillna(False).sum()),
                 100 * part["baseline_spy_trailing"].fillna(False).mean())
    log.info("thresholds (Q99 of |24h move| on train): %s",
             {r.block: round(r.q99_abs_move, 5) for r in thresholds.itertuples()})
    driving = frame.loc[frame["significant"].fillna(False), "driving_block"]
    log.info("driving block: %s", driving.value_counts().to_dict())

    # How much of a 24-hour window each block actually observes. The blocks are
    # not comparable on this - an ETF block trades seven hours a day and crypto
    # around the clock - and they do not need to be: every block is measured
    # against its own train distribution, so a thin window is thin on both sides
    # of the comparison.
    coverage = {c[len("observed_"):]: round(float(frame.loc[labelled, c].mean()), 1)
                for c in frame.columns if c.startswith("observed_")}
    log.info("hours observed out of %d in a window: %s", HORIZON, coverage)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

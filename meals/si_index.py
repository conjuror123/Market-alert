"""Sensation Index (spec §4).

    SI_total = (sum of base points) * M_calendar * M_VIX

Four triggers answer four different questions, and the points add up precisely
because the questions differ: was the move extreme (price shock), is it backed
by order flow (volume), did it touch DIFFERENT parts of the market (cluster
shift), and is everything happening explained by a single cause (single-factor).
An event that scores on several axes at once is qualitatively different from one
that scores the same total on a single axis.

Points for a trigger type are awarded ONCE per hour, however many assets satisfy
the condition. Cluster shift and single-factor already aggregate information
across the basket, and multiplying them by the number of assets on top of that
would count the same thing twice.

Breadth is counted by BLOCKS, not by tickers. Six currency pairs jerked by one
move in the dollar are one block, not six pieces of evidence.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from meals.basket import Basket

# Trigger weights (§4.2), all starred in the spec.
POINTS_PRICE_SHOCK = 3
POINTS_VOLUME = 2
POINTS_CLUSTER_SHIFT = 4
POINTS_SINGLE_FACTOR = 3

# Points awarded in proportion to HOW BROAD the shift was, on top of the flat
# award for the cluster-shift trigger itself. Starred: §7 calibrates it.
#
# §4.2 gives four yes/no triggers, and because the gate of §4.1 requires the
# cluster shift, its +4 is always present and volume cannot fire without a price
# shock. That leaves exactly five reachable totals - 4, 7, 9, 10, 12 - with
# THRESHOLD sitting between the first two, so the gate reduces to "cluster shift
# AND price shock" and moving the threshold jumps whole triggers at a time
# instead of tuning. Measured: 67.8% of cluster-shift hours already reach 7 on
# base points alone.
#
# The breadth of a shift is the information already at hand that separates the
# hours the flat award cannot: a shift across two blocks and one across five
# score identically today. Adding it continuously gives §7 a threshold it can
# actually slide. It takes the maximum sum past the 12 of §4.2 - see
# docs/meals-deviations.md §22.
POINTS_BREADTH = 14.0   # (*) calibrated on train, spec has no such term

# Two channels the spec does not have, both matched to §7's 24-hour horizon
# rather than to a single bar. See docs/meals-deviations.md §24.
#
# Trigger 5, the sustained move: the basket's own move accumulated over 24
# reference hours. On train the one-hour form of this quantity reaches 25%
# precision and the accumulated form 80%.
#
# Trigger 6, single-asset breadth: how many SAED events fired in the last 24
# reference hours. §8.6 makes the dependency one-way - SAED consumes the basket
# factor and feeds nothing back - and that decision was costing signal: on train
# the SAED count alone reaches 53% precision at the 98th percentile and 83% at
# the 99th, against the cluster detector's own 37%.
#
# Both starred. Their weights are what the calibration is for; if either turns
# out not to earn its place, the search will take it to zero.
POINTS_SUSTAINED = 6.0      # (*) calibrated on train
POINTS_SAED_BREADTH = 4.0   # (*) calibrated on train
# The 12 of §4.2 - the four flat trigger awards - plus the graded breadth term,
# which the spec does not have. Kept as two names because the first is what §4.2
# states and what the trigger weights must still add up to.
MAX_FLAT_POINTS = (POINTS_PRICE_SHOCK + POINTS_VOLUME + POINTS_CLUSTER_SHIFT
                   + POINTS_SINGLE_FACTOR)
MAX_POINTS = (MAX_FLAT_POINTS + POINTS_BREADTH + POINTS_SUSTAINED
              + POINTS_SAED_BREADTH)

# Share of a block's assets at which the block counts as active (§4.2).
BLOCK_ACTIVE_SHARE = 0.33
BLOCK_ACTIVE_MIN = 2
MIN_ACTIVE_BLOCKS = 2

# Decision thresholds (§4.5), also starting values.
THRESHOLD = 14
ESCALATION_THRESHOLD = 24

# Breadth scale on Q99 (§4.5, §5.2).
BREADTH_SHARE = 0.50
BREADTH_MIN_BLOCKS = 2


def _panel(metrics: dict[str, pd.DataFrame], column: str, hours: pd.Index) -> pd.DataFrame:
    series = {aid: df.set_index("hour_utc")[column] for aid, df in metrics.items()}
    return pd.DataFrame(series).reindex(hours)


def active_blocks(breaches: pd.DataFrame, present: pd.DataFrame,
                  blocks: dict[str, str]) -> pd.DataFrame:
    """How many of a block's assets breached, and how many are in session at all.

    The share is taken over assets IN SESSION, not over the block's roster: at
    night the ETFs are closed, and demanding a third of the equity block's
    members would be an impossible requirement rather than a strict one.
    """
    counts = {}
    for block in sorted(set(blocks.values())):
        columns = [c for c in breaches.columns if blocks.get(c) == block]
        if not columns:
            continue
        hit = breaches[columns].fillna(False).sum(axis=1)
        in_session = present[columns].sum(axis=1)
        counts[block] = (hit >= np.maximum(BLOCK_ACTIVE_SHARE * in_session,
                                           BLOCK_ACTIVE_MIN))
    return pd.DataFrame(counts, index=breaches.index)


def base_points(basket: Basket, metrics: dict[str, pd.DataFrame],
                single_factor: pd.Series, hours: pd.Index,
                volume_threshold: float,
                sustained: pd.Series | None = None,
                saed_breadth: pd.Series | None = None,
                weights: dict | None = None) -> pd.DataFrame:
    """Base points per §4.2 plus the two matched-horizon channels, one column
    per trigger.

    `weights` overrides the module constants and exists for the §7 search, which
    sweeps them; every real run passes None and uses the configuration.
    """
    w = {"price_shock": POINTS_PRICE_SHOCK, "volume": POINTS_VOLUME,
         "cluster_shift": POINTS_CLUSTER_SHIFT, "single_factor": POINTS_SINGLE_FACTOR,
         "breadth": POINTS_BREADTH, "sustained": POINTS_SUSTAINED,
         "saed_breadth": POINTS_SAED_BREADTH} | (weights or {})
    ids = {a.asset_id for a in basket.assets}
    blocks = {a.asset_id: a.block for a in basket.assets}
    tier1 = {a.asset_id for a in basket.assets if a.tier == 1}
    with_volume = {a.asset_id for a in basket.assets if a.has_volume}
    basket_metrics = {k: v for k, v in metrics.items() if k in ids}

    q99 = _panel(basket_metrics, "breach_q99", hours).astype("boolean")
    q95 = _panel(basket_metrics, "breach_q95", hours).astype("boolean")
    v_r = _panel(basket_metrics, "v_r", hours)
    present = _panel(basket_metrics, "r", hours).notna()

    price_shock = q99.fillna(False).any(axis=1)

    # Volume confirms not on its own but on an asset that ALREADY produced a
    # price shock: a volume spike without a price move is a different event.
    volume_columns = [c for c in q99.columns if c in with_volume]
    volume_confirms = (q99[volume_columns].fillna(False)
                       & (v_r[volume_columns] > volume_threshold)).any(axis=1)

    active = active_blocks(q95, present, blocks)
    tier1_columns = [c for c in q95.columns if c in tier1]
    cluster_shift = ((active.sum(axis=1) >= MIN_ACTIVE_BLOCKS)
                     & q95[tier1_columns].fillna(False).any(axis=1))

    active_q99 = active_blocks(q99, present, blocks)
    represented = pd.DataFrame({
        block: present[[c for c in present.columns if blocks.get(c) == block]].any(axis=1)
        for block in sorted(set(blocks.values()))
    }, index=hours).sum(axis=1)
    breadth = ((active_q99.sum(axis=1) >= BREADTH_MIN_BLOCKS)
               & (active_q99.sum(axis=1) >= BREADTH_SHARE * represented))

    single = single_factor.reindex(hours).fillna(False).astype(bool)

    # The share of the blocks present this hour that are active by Q95. Graded
    # rather than counted, so a two-block shift out of two scores like a
    # five-block shift out of five: what matters is how much of the market that
    # was open moved, not how much of it happened to be open.
    breadth_share = (active.sum(axis=1) / represented.replace(0, np.nan)).fillna(0.0)

    def as_flags(series):
        if series is None:
            return pd.Series(False, index=hours)
        return series.reindex(hours).fillna(False).astype(bool)

    sustained_hit = as_flags(sustained)
    saed_hit = as_flags(saed_breadth)

    return pd.DataFrame({
        "trigger_price_shock": price_shock,
        "trigger_volume": volume_confirms,
        "trigger_cluster_shift": cluster_shift,
        "trigger_single_factor": single,
        "n_active_blocks": active.sum(axis=1),
        "n_active_blocks_q99": active_q99.sum(axis=1),
        "breadth_q99": breadth,
        "breadth_share": breadth_share,
        "trigger_sustained": sustained_hit,
        "trigger_saed_breadth": saed_hit,
        "base_points": (price_shock * w["price_shock"]
                        + volume_confirms * w["volume"]
                        + cluster_shift * w["cluster_shift"]
                        + single * w["single_factor"]
                        # Only where the shift actually fired: breadth without a
                        # cluster shift is a handful of assets moving, which the
                        # price-shock trigger already speaks for.
                        + cluster_shift * breadth_share * w["breadth"]
                        + sustained_hit * w["sustained"]
                        + saed_hit * w["saed_breadth"]),
    }, index=hours)


def si_total(points: pd.Series, m_calendar: pd.Series, m_vix: pd.Series) -> pd.Series:
    """The final score after the multipliers - the primary scale on which every
    threshold decision is taken (§4.5)."""
    return points * m_calendar * m_vix

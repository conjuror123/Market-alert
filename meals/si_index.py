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
MAX_POINTS = POINTS_PRICE_SHOCK + POINTS_VOLUME + POINTS_CLUSTER_SHIFT + POINTS_SINGLE_FACTOR

# Share of a block's assets at which the block counts as active (§4.2).
BLOCK_ACTIVE_SHARE = 0.33
BLOCK_ACTIVE_MIN = 2
MIN_ACTIVE_BLOCKS = 2

# Decision thresholds (§4.5), also starting values.
THRESHOLD = 7
ESCALATION_THRESHOLD = 12

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
                volume_threshold: float) -> pd.DataFrame:
    """Base points per §4.2, one column per trigger."""
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

    return pd.DataFrame({
        "trigger_price_shock": price_shock,
        "trigger_volume": volume_confirms,
        "trigger_cluster_shift": cluster_shift,
        "trigger_single_factor": single,
        "n_active_blocks": active.sum(axis=1),
        "n_active_blocks_q99": active_q99.sum(axis=1),
        "breadth_q99": breadth,
        "base_points": (price_shock * POINTS_PRICE_SHOCK
                        + volume_confirms * POINTS_VOLUME
                        + cluster_shift * POINTS_CLUSTER_SHIFT
                        + single * POINTS_SINGLE_FACTOR),
    }, index=hours)


def si_total(points: pd.Series, m_calendar: pd.Series, m_vix: pd.Series) -> pd.Series:
    """The final score after the multipliers - the primary scale on which every
    threshold decision is taken (§4.5)."""
    return points * m_calendar * m_vix

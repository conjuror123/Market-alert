"""Turning a forecast into alerts, at a rate that follows the market.

This is the piece v1 did not have, and the reason it failed out of sample. Its
THRESHOLD was a constant, so the detector fired on a fixed slice of the score's
distribution - and that distribution barely moved between periods whose real
event rates differed threefold. Measured: the market produced 41% as many
significant episodes per hour in test as in train, and the detector fired at 98%
of its former rate.

The standard answer is a time-varying threshold: a quantile of the score's own
recent history rather than a number chosen once. The alert then means "unusual
for conditions" instead of "above 14", and the firing rate follows the market
because the yardstick does.

The quantile is taken over a trailing window and shifted by one bar, so the hour
being judged is never part of the distribution judging it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tremor import windows

# Trailing window for the threshold quantile: ten trading weeks, as W_CS.
WINDOW = windows.W_CS

# How selective the detector is. This is the one dial worth keeping, and it is
# expressed where it means something: 0.99 fires on the top one percent of hours
# FOR THE CONDITIONS, whatever those conditions happen to be.
QUANTILE = 0.99


def adaptive(score: pd.Series, quantile: float = QUANTILE,
             window: int = WINDOW) -> tuple[pd.Series, pd.Series]:
    """The score's own trailing quantile, and the alerts it produces."""
    threshold = (score.shift(1).rolling(window, min_periods=window // 2)
                 .quantile(quantile))
    return threshold, (score > threshold).where(threshold.notna(), False)


def cooldown(fires: pd.Series, bars: int) -> pd.Series:
    """One alert, then silence for `bars` hours - §5.1's rule, kept.

    Nothing about the new score changes why this exists: after a shock the market
    keeps rumbling, and without a pause the same storm is reported every hour.
    """
    flags = fires.fillna(False).to_numpy(dtype=bool)
    out = np.zeros(len(flags), dtype=bool)
    last = -10 ** 9
    for i in np.flatnonzero(flags):
        if i - last >= bars:
            out[i] = True
            last = i
    return pd.Series(out, index=fires.index)


def firing_rate_ratio(alerts: pd.Series, split: int) -> float:
    """How much the firing rate changed across a split, as a ratio.

    The check that v1 failed, and it needs no labels at all: if the detector is
    tracking conditions, this should move with the episode rate rather than
    sitting at one.
    """
    before, after = alerts[alerts.index < split], alerts[alerts.index >= split]
    if not len(before) or not len(after) or not before.mean():
        return float("nan")
    return float(after.mean() / before.mean())

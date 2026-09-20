"""The long-run sigma: an exponentially weighted spread over a bounded window.

WHAT THIS REPLACED. sigma_LT was a box: the plain standard deviation of the
last 5,000 bars, every one of them counting exactly as much as yesterday's,
and the 5,001st counting not at all. Both halves of that are claims about the
market that nobody would make out loud - that a bar from two years ago
describes today's normal as well as this morning does, and that a bar one hour
older than that describes it not at all. The edge is not harmless: it moves
through the data, so a violent week leaves the window on a particular day and
the yardstick steps.

Exponential weights are the standard answer and have been since RiskMetrics
quoted a decay rate rather than a window. Foster and Nelson (1996) and the
rolling-sample literature after it find them dominating flat weights for the
same reason. Measured here, at matched behaviour (see tools/sigma_window.py),
they hold the multiple steadier from era to era than the box does on 82% of
equity settings and 65% of crypto ones.

WHY IT IS STILL CUT OFF SOMEWHERE. A true EWMA depends on every bar ever
recorded. This system's hourly run reproduces a cold pass over the whole
archive precisely BECAUSE every quantity depends on a bounded stretch of the
past (windows.warm_bars); an unbounded one would make the warm run an
approximation, and the tier of a borderline bar would depend on how much
history the runner happened to load. So the weights are cut off at
windows.SIGMA_LT_BARS - six half-lives, past which the measurement stops
improving - and the estimator is normalised by the weights it ACTUALLY USED.
The cut is part of the definition rather than an error in it, and the bar at
the edge carries a sixty-fourth of the newest one's weight rather than all of
it.

WHY SQUARED RETURNS AND NOT DEVIATIONS FROM A MEAN. The mean of an hourly
return is three orders of magnitude below its standard deviation, so estimating
it spends a degree of freedom to subtract nothing, and makes the recursion below
impossible. This is the RiskMetrics convention for the same reason.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def long_run_sigma(values: pd.Series, half_life: int, span: int,
                   min_periods: int) -> pd.Series:
    """Exponentially weighted spread of `values`, over the `span` bars before each.

    CAUSAL: the value at position t is built from positions t-1 and earlier, so
    no bar is described using itself. Callers pass the series unshifted - the
    shift is here, where it cannot be forgotten.

    Missing values are skipped rather than treated as zero returns, which would
    quietly report a gap in the data as a period of calm. They are dropped from
    the weight total too, so a window with holes is still normalised by what it
    actually summed. `min_periods` counts real observations, as it did when this
    was a rolling standard deviation, so an instrument with less history than
    `span` is measured over what it has instead of not at all - which matters:
    XLP holds 7,246 bars against a span of 8,400.

    Exact in one pass. The box window's trick is to add one observation and drop
    one; the same works here with the dropped one discounted by how far it has
    decayed:

        S_t = x_{t-1} + lam * S_{t-1} - lam^span * x_{t-1-span}

    and identically for the weight total, which is what makes the partial
    windows at the start fall out of the same recursion rather than needing a
    branch.
    """
    if half_life <= 0 or span <= 0:
        raise ValueError("half_life and span must be positive")

    raw = values.to_numpy(dtype="float64")
    n = raw.size
    out = np.full(n, np.nan)
    if n == 0:
        return pd.Series(out, index=values.index, dtype="float64")

    good = np.isfinite(raw)
    square = np.where(good, raw * raw, 0.0)
    seen = good.astype("float64")

    lam = 0.5 ** (1.0 / half_life)
    tail = lam ** span
    # Real observations in the window, for the min_periods gate. Kept separate
    # from the weighted total: the gate is a question about how much data there
    # is, not about how much of it is recent.
    count = (pd.Series(good.astype("float64"))
             .rolling(span, min_periods=1).sum().shift(1).to_numpy())

    total = 0.0          # weighted sum of squares
    weight = 0.0         # weighted count of the bars that contributed
    for t in range(1, n):
        drop = t - 1 - span
        total = square[t - 1] + lam * total
        weight = seen[t - 1] + lam * weight
        if drop >= 0:
            total -= tail * square[drop]
            weight -= tail * seen[drop]
        if weight > 0 and count[t] >= min_periods:
            out[t] = total / weight

    return pd.Series(np.sqrt(out), index=values.index, dtype="float64")


def sigma_lt(values: pd.Series) -> pd.Series:
    """long_run_sigma at the settings in tremor.windows - the one callers want."""
    from tremor import windows

    return long_run_sigma(values, windows.SIGMA_LT_HALFLIFE_BARS,
                          windows.SIGMA_LT_BARS, windows.SIGMA_LT_MIN_BARS)

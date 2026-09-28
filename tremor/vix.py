"""The VIX spike test behind the fear-gauge line on the weekly note.

The condition is one-sided, and that is the point: fear and relief are not
symmetric states of the market. A sharp rise in VIX means participants are
paying for protection, that is, they consider the near future dangerous; a fall
of the same size merely means a return to normal. So a falling VIX does not
count as a spike.

The series is daily, because no available source offers intraday VIX, and a
reading counts from the moment the value became KNOWN to the system, not from
the observation date: FRED publishes it on the next business day.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tremor import ewma, windows, zscore


# Absolute-leg threshold for the VIX series: 1.5 * sigma_LT.
ABS_LEG = windows.ABS_LEG_Q95


# Rows per pass of the vectorised MAD. The sliding view itself is free - it is a
# stride trick over the original buffer - but the |x - median| step has to
# materialise, so a whole series at once would allocate n*window doubles. At
# this size that is tens of megabytes rather than gigabytes, and chunking keeps
# it flat regardless of how long the history grows.
_MAD_CHUNK = 100_000


def _rolling_mad(series: pd.Series, window: int) -> pd.Series:
    """Median absolute deviation on a rolling window, EXCLUDING the current bar -
    the window ends on the previous one.

    Vectorised over the whole series rather than called back per window, which
    is not a micro-optimisation: this was 54% of the entire pipeline when the pipeline winsorised every instrument. The window
    is twenty-four bars, and a twenty-four-element double median takes about
    35us of which almost all is call overhead rather than arithmetic - so 1.7
    million of them, one per bar per instrument, cost two minutes of the five a
    full run took, to do a few seconds of actual work.

    NaN handling is inherited rather than coded: np.median returns NaN if any
    element is NaN, exactly as the per-window callback did, so a window
    straddling a gap still yields NaN and the first `window` positions stay NaN
    for want of a full window.
    """
    values = series.shift(1).to_numpy(dtype="float64")
    out = np.full(len(values), np.nan)
    if len(values) < window or window < 1:
        return pd.Series(out, index=series.index)

    view = np.lib.stride_tricks.sliding_window_view(values, window)
    for start in range(0, len(view), _MAD_CHUNK):
        block = view[start:start + _MAD_CHUNK]
        median = np.median(block, axis=1, keepdims=True)
        out[start + window - 1:start + window - 1 + len(block)] = np.median(
            np.abs(block - median), axis=1)
    return pd.Series(out, index=series.index)


def score(series: pd.DataFrame, window: int = windows.SIGMA_LT_MIN_BARS) -> pd.DataFrame:
    """Runs the VIX series through the z-score machinery with its own states.

    The threshold window for a daily series is 720 bars: the rule gives
    max(120 * B_asset, 720), and a daily series has one bar per day, so the
    floor is what applies.
    """
    out = series.copy()
    out["r"] = np.log(out["close"] / out["close"].shift(1))
    out["sigma_lt"] = ewma.sigma_lt(out["r"], windows.DAILY_SERIES)

    mad_24 = _rolling_mad(out["r"], windows.MAD_WINDOW)
    mad_eff = np.maximum(mad_24, 0.2 * out["sigma_lt"])
    limit = 5 * mad_eff
    out["r_w"] = np.where(out["r"].abs() > limit, np.sign(out["r"]) * limit, out["r"])
    out.loc[mad_eff.isna(), "r_w"] = out["r"][mad_eff.isna()]

    z, sigma_eff = zscore.ewma_state(out["r"].to_numpy(dtype="float64"),
                                     out["r_w"].to_numpy(dtype="float64"),
                                     out["sigma_lt"].to_numpy(dtype="float64"))
    out["z"] = z
    out.loc[out["sigma_lt"].isna(), "z"] = np.nan
    q95, _ = zscore.adaptive_thresholds(out["z"].abs(), window)
    out["q95"] = q95

    # All three conditions at once, and Z is taken WITH its sign.
    out["is_spike"] = ((out["z"] > out["q95"])
                       & (out["r"] > 0)
                       & (out["r"].abs() >= ABS_LEG * out["sigma_lt"]))
    return out



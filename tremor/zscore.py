"""The short-memory EWMA state, and the adaptive threshold the VIX test uses.

The order of operations here is not an implementation detail - it is the method.

Z is computed BEFORE the state is updated, against the previous bar's
parameters. Do it the other way round and the current move first enters the
estimate of "normal" and is then compared against that same estimate - and the
larger the event, the more it raises its own denominator. A detector built that
way sees a move less well the bigger it is; this is called look-ahead bias, and
it is exactly what "out-of-sample" guards
against.

The state update is fed the winsorized r_w, while Z is computed from the
raw r. The same quantity in two roles: as the observation to be judged, and as a
contribution to the estimate of normal. Only the second role is capped.

And one more subtlety that is easy to lose: the variance update is fed the
ewma_mean of the PREVIOUS bar, not the one just recomputed. It is worth stating
in its own line, because both forms look equally natural and give different
results.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tremor import windows

# Fraction of the long-term sigma below which Z's denominator never falls.
# Without this floor the EWMA variance collapses in a lull, and Z shoots up not
# because the move is large but because the denominator became tiny.
SIGMA_EFF_FLOOR = 0.2


def ewma_state(returns: np.ndarray, winsorized: np.ndarray, sigma_lt: np.ndarray,
               lam: float = windows.LAMBDA) -> tuple[np.ndarray, np.ndarray]:
    """Runs the z-score automaton over a series and returns (Z, sigma_eff).

    The sequential pass is layer B: a bar's state depends on the
    previous bar's, and vectorising that without losing the meaning is not
    possible.

    The initial state does not matter: at lambda = 2/25 the half-life is about
    eight bars, so by the end of the 500-bar burn-in the weight of the
    starting value is around 1e-18.
    """
    n = len(returns)
    z = np.full(n, np.nan)
    sigma_eff_out = np.full(n, np.nan)
    mean = 0.0
    var = float(returns[np.isfinite(returns)][0] ** 2) if np.isfinite(returns).any() else 0.0

    for i in range(n):
        r = returns[i]
        if not np.isfinite(r):
            continue

        # Step 1: Z against the PREVIOUS bar's parameters.
        floor = SIGMA_EFF_FLOOR * sigma_lt[i] if np.isfinite(sigma_lt[i]) else 0.0
        sigma_eff = max(np.sqrt(var), floor)
        if sigma_eff > 0:
            z[i] = (r - mean) / sigma_eff
            sigma_eff_out[i] = sigma_eff

        # Step 2: update the state, on the winsorized return and with the OLD
        # mean in the variance formula.
        r_w = winsorized[i] if np.isfinite(winsorized[i]) else r
        previous_mean = mean
        mean = lam * r_w + (1 - lam) * mean
        var = lam * (r_w - previous_mean) ** 2 + (1 - lam) * var

    return z, sigma_eff_out


def adaptive_thresholds(abs_z: pd.Series, window: int,
                        lam_q: float = windows.LAMBDA_Q) -> tuple[pd.Series, pd.Series]:
    """Smoothed Q95 and Q99 thresholds of |Z| - read only by the VIX spike test.

    The percentiles are computed on a rolling window of |Z| EXCLUDING the current
    bar: a threshold that contains the observation being judged adjusts towards
    it and thereby understates its own breach. The window is counted over
    DEFINED values of Z, not over rows. Smoothing is mandatory: without it the
    threshold jerks with whatever happened to enter and leave the window.
    """
    defined = abs_z.dropna()
    raw = defined.shift(1).rolling(window, min_periods=window)
    # ewm with adjust=False is exactly the recurrence
    # Q_t = lambda_q * Q_raw_t + (1 - lambda_q) * Q_{t-1}, computed over
    # consecutive defined bars.
    q95 = raw.quantile(0.95).ewm(alpha=lam_q, adjust=False).mean()
    q99 = raw.quantile(0.99).ewm(alpha=lam_q, adjust=False).mean()
    # Where Z is undefined no threshold is needed: the breach is not assessed anyway.
    return q95.reindex(abs_z.index), q99.reindex(abs_z.index)


def compute(frame: pd.DataFrame) -> pd.DataFrame:
    """The short-memory scale of one price series: `sigma_eff`.

    The input is a frame after winsorize, with columns r, r_w and sigma_lt. The
    blocks read sigma_eff - a block's move is the median of its members' moves
    each over its own sigma_eff (tremor.cross_section) - and nothing else reads
    this state for a price series. Its Z-score, and the Q95/Q99 thresholds and
    breach flags built on it, fed no decision and were removed.
    """
    out = frame.copy()
    if out.empty:
        return out.assign(sigma_eff=pd.Series(dtype="float64"))

    _, sigma_eff = ewma_state(out["r"].to_numpy(dtype="float64"),
                              out["r_w"].to_numpy(dtype="float64"),
                              out["sigma_lt"].to_numpy(dtype="float64"))
    out["sigma_eff"] = sigma_eff
    # Until sigma_LT has filled, the denominator has no floor - the very floor
    # that keeps the EWMA variance from collapsing - so the state is not given
    # out there at all.
    out.loc[out["sigma_lt"].isna(), "sigma_eff"] = np.nan
    return out

"""Adaptive EWMA Z-score and adaptive thresholds (spec §3.1).

The order of operations here is not an implementation detail - it is the method.

Z is computed BEFORE the state is updated, against the previous bar's
parameters. Do it the other way round and the current move first enters the
estimate of "normal" and is then compared against that same estimate - and the
larger the event, the more it raises its own denominator. A detector built that
way sees a move less well the bigger it is; this is called look-ahead bias, and
it is exactly what the phrase "out-of-sample" in the heading of §3.1 guards
against.

The state update is fed the winsorized r_w (§2.5), while Z is computed from the
raw r. The same quantity in two roles: as the observation to be judged, and as a
contribution to the estimate of normal. Only the second role is capped.

And one more subtlety that is easy to lose: the variance update is fed the
ewma_mean of the PREVIOUS bar, not the one just recomputed. The spec spells this
out in its own line, because both forms look equally natural and give different
results.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tremor import windows

# Fraction of the long-term sigma below which Z's denominator never falls (§3.1).
# Without this floor the EWMA variance collapses in a lull, and Z shoots up not
# because the move is large but because the denominator became tiny.
SIGMA_EFF_FLOOR = 0.2


def ewma_state(returns: np.ndarray, winsorized: np.ndarray, sigma_lt: np.ndarray,
               lam: float = windows.LAMBDA) -> tuple[np.ndarray, np.ndarray]:
    """Runs the §3.1 automaton over a series and returns (Z, sigma_eff).

    The sequential pass is layer B from §6.1: a bar's state depends on the
    previous bar's, and vectorising that without losing the meaning is not
    possible.

    The initial state does not matter: at lambda = 2/25 the half-life is about
    eight bars, so by the end of the 500-bar burn-in (§6.6) the weight of the
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
    """Smoothed Q95 and Q99 thresholds (§3.1).

    The percentiles are computed on a rolling window of |Z| EXCLUDING the current
    bar: a threshold that contains the observation being judged adjusts towards
    it and thereby understates its own breach.

    The spec makes smoothing mandatory. Without it the threshold jerks around
    depending on which values happened to enter and leave the window, and the
    same move can be significant or not purely because of something that happened
    840 bars ago.

    The window is counted over DEFINED values of Z, not over rows. §3.1 literally
    says "a rolling window W_asset of absolute values of Z", and the difference
    shows wherever the Z series has holes beyond the burn-in. For the residual
    series that happens every week: crypto trades at weekends, but the basket
    factor does not exist in those hours because the reference calendar excludes
    them. A window of 2880 consecutive rows would never accumulate 2880 values -
    Bitcoin's residual thresholds would never be computed at all, and the SAED
    module would stay silent across the whole crypto block.
    """
    defined = abs_z.dropna()
    raw = defined.shift(1).rolling(window, min_periods=window)
    # ewm with adjust=False is exactly the spec's recurrence
    # Q_t = lambda_q * Q_raw_t + (1 - lambda_q) * Q_{t-1}, computed over
    # consecutive defined bars.
    q95 = raw.quantile(0.95).ewm(alpha=lam_q, adjust=False).mean()
    q99 = raw.quantile(0.99).ewm(alpha=lam_q, adjust=False).mean()
    # Where Z is undefined no threshold is needed: the breach is not assessed anyway.
    return q95.reindex(abs_z.index), q99.reindex(abs_z.index)


def breaches(abs_z: pd.Series, abs_r: pd.Series, sigma_lt: pd.Series,
             q95: pd.Series, q99: pd.Series,
             abs_leg_q95: float | None = None,
             abs_leg_q99: float | None = None) -> pd.DataFrame:
    """The hybrid significance condition of §3.1: the relative AND the absolute
    leg at once.

    Returns NULL (pd.NA) rather than False wherever the condition was not
    assessed - the thresholds have not filled yet, or there is no long-term
    sigma. Per §1.2 these are different things: "did not clear the threshold" and
    "the threshold does not exist yet".
    """
    # The legs are arguments so that §7 can search them without recomputing the
    # Z-scores: only the comparison moves, and the whole series above it stays.
    leg95 = windows.ABS_LEG_Q95 if abs_leg_q95 is None else abs_leg_q95
    leg99 = windows.ABS_LEG_Q99 if abs_leg_q99 is None else abs_leg_q99

    known = q95.notna() & q99.notna() & sigma_lt.notna() & abs_z.notna()
    q99_hit = (abs_z > q99) & (abs_r >= leg99 * sigma_lt)
    q95_hit = (abs_z > q95) & (abs_r >= leg95 * sigma_lt)
    return pd.DataFrame({
        "breach_q99": q99_hit.where(known, pd.NA).astype("boolean"),
        "breach_q95": q95_hit.where(known, pd.NA).astype("boolean"),
    })


def compute(frame: pd.DataFrame, w_asset: int) -> pd.DataFrame:
    """Assembles Z, the thresholds and the breach flags for one series.

    The input is a frame after winsorize: with columns r, r_w and sigma_lt. The
    same machinery is applied to the SAED residual series (§3.6) and to the VIX
    series (§4.4) - they have their own states and their own thresholds, but the
    formulas are identical, so this function does not know whose series it is
    processing.
    """
    out = frame.copy()
    if out.empty:
        return out.assign(z=pd.Series(dtype="float64"), q95=pd.Series(dtype="float64"),
                          q99=pd.Series(dtype="float64"),
                          breach_q95=pd.Series(dtype="boolean"),
                          breach_q99=pd.Series(dtype="boolean"))

    z, sigma_eff = ewma_state(out["r"].to_numpy(dtype="float64"),
                              out["r_w"].to_numpy(dtype="float64"),
                              out["sigma_lt"].to_numpy(dtype="float64"))
    out["z"] = z
    out["sigma_eff"] = sigma_eff

    # Until sigma_LT has filled, the denominator has no floor - the very floor
    # that keeps the EWMA variance from collapsing. On frozen quotes this yields
    # nonsense: on EUR/USD, 1 January 2021, after four hours of a standing price,
    # one ordinary half-percent move produced a Z of 224. That is harmless in
    # itself - breaches there are NULL anyway - but such Z values entered the
    # percentile window and shifted the thresholds thousands of bars forward.
    # Per §6.6 an hour before first_valid_hour takes no part in the statistics at
    # all, so Z during the burn-in is not merely unused, it does not exist.
    out.loc[out["sigma_lt"].isna(), ["z", "sigma_eff"]] = np.nan

    abs_z = out["z"].abs()
    q95, q99 = adaptive_thresholds(abs_z, w_asset)
    out["q95"], out["q99"] = q95, q99
    return out.join(breaches(abs_z, out["r"].abs(), out["sigma_lt"], q95, q99))

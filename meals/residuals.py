"""Idiosyncratic residual: the input of the SAED module (spec §3.6).

The whole construction answers one question: is this asset's move its own, or is
it simply drifting along with the market? A raw return cannot answer that. On a
day when everything falls, every asset shows a large move and a large Z, and a
detector built on raw returns fires twenty identical alerts about one and the
same event - which is exactly how the current bot's hourly signal behaves.

So the common basket factor is subtracted from the return:
r = alpha + beta * F + e, and only the residual e goes forward. Beta is estimated
on a rolling window and strictly on data BEFORE the current bar - otherwise the
very move we are trying to detect would adjust the coefficient and partly
subtract itself from itself.

The residual is processed by the same §3.1 machinery as the price, but with
entirely ITS OWN states: its own EWMA, its own long-term sigma, its own smoothed
thresholds. Mixing them with the price ones is not allowed - the residual has a
different scale and a different distribution.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from meals import windows
from meals.basket import Asset


def rolling_beta(returns: pd.Series, factor: pd.Series,
                 window: int = windows.REGRESSION_WINDOW,
                 minimum: int = windows.REGRESSION_MIN) -> pd.DataFrame:
    """Rolling regression of r on F over bars where BOTH are valid (§2.7).

    The window is measured in bars where both quantities are defined, not in
    calendar hours: for an ETF the basket factor exists only during the US
    session, and a window of five hundred calendar hours would give it four times
    fewer observations than a currency pair.

    The coefficients are shifted forward by one bar - the estimate available AT
    moment t is built on data up to and including the previous bar.
    """
    joint = pd.DataFrame({"r": returns, "f": factor}).dropna()
    if len(joint) < minimum:
        return pd.DataFrame({"alpha": np.nan, "beta": np.nan}, index=returns.index)

    rolling = joint.rolling(window, min_periods=minimum)
    mean_r = rolling["r"].mean()
    mean_f = rolling["f"].mean()
    var_f = rolling["f"].var(ddof=1)
    cov = rolling.cov().unstack()[("r", "f")]

    beta = (cov / var_f).where(var_f > 0)
    alpha = mean_r - beta * mean_f
    # Shift by one bar: at bar t the estimate computed on data before t is used.
    estimates = pd.DataFrame({"alpha": alpha.shift(1), "beta": beta.shift(1)})
    return estimates.reindex(returns.index)


def rolling_two_factor(returns: pd.Series, factor: pd.Series, block_factor: pd.Series,
                       window: int = windows.REGRESSION_WINDOW,
                       minimum: int = windows.REGRESSION_MIN) -> pd.DataFrame:
    """Regression on two factors: the basket and the asset's own block.

    Solved through the normal equations rather than by fitting in a loop: for two
    regressors the 2x2 system can be written out explicitly in terms of rolling
    variances and covariances, and the whole calculation stays vectorised
    (layer A of §6.1).

    The degenerate case is handled explicitly. If the two factors are nearly
    collinear within the window, the determinant tends to zero and the
    coefficients fly off to arbitrary values with opposite signs - formally there
    is a solution, in substance it is noise. In such a window the regression falls
    back to the basket factor alone, that is, to the behaviour of §3.6.
    """
    joint = pd.DataFrame({"y": returns, "x1": factor, "x2": block_factor}).dropna()
    if len(joint) < minimum:
        return pd.DataFrame({"alpha": np.nan, "beta": np.nan, "beta_block": np.nan},
                            index=returns.index)

    rolling = joint.rolling(window, min_periods=minimum)
    mean_y, mean_1, mean_2 = rolling["y"].mean(), rolling["x1"].mean(), rolling["x2"].mean()
    var_1, var_2 = rolling["x1"].var(ddof=1), rolling["x2"].var(ddof=1)
    covariances = rolling.cov().unstack()
    cov_12 = covariances[("x1", "x2")]
    cov_1y = covariances[("x1", "y")]
    cov_2y = covariances[("x2", "y")]

    determinant = var_1 * var_2 - cov_12 ** 2
    # The determinant is compared not against zero but against the product of the
    # variances: on its own it is small simply because returns are small, and an
    # absolute threshold would declare every window degenerate.
    degenerate = (determinant / (var_1 * var_2)).abs() < 1e-8

    beta = ((var_2 * cov_1y - cov_12 * cov_2y) / determinant).mask(degenerate)
    beta_block = ((var_1 * cov_2y - cov_12 * cov_1y) / determinant).mask(degenerate)

    single = (cov_1y / var_1).where(var_1 > 0)
    beta = beta.fillna(single)
    beta_block = beta_block.fillna(0.0)

    alpha = mean_y - beta * mean_1 - beta_block * mean_2
    estimates = pd.DataFrame({"alpha": alpha.shift(1), "beta": beta.shift(1),
                              "beta_block": beta_block.shift(1)})
    return estimates.reindex(returns.index)


def residuals(asset: Asset, frame: pd.DataFrame, factor: pd.Series,
              block_factor: pd.Series | None = None) -> pd.DataFrame:
    """The residual e and everything the §3.1 machinery needs to process it."""
    out = frame.copy()
    if out.empty:
        return out.assign(alpha=pd.Series(dtype="float64"),
                          beta=pd.Series(dtype="float64"),
                          beta_block=pd.Series(dtype="float64"),
                          e_resid=pd.Series(dtype="float64"),
                          e_resid_w=pd.Series(dtype="float64"),
                          sigma_lt_resid=pd.Series(dtype="float64"))

    factor_series = pd.Series(factor.reindex(out["hour_utc"]).to_numpy(), index=out.index)

    if block_factor is None:
        estimates = rolling_beta(out["r"], factor_series)
        estimates["beta_block"] = 0.0
        block_series = pd.Series(0.0, index=out.index)
    else:
        block_series = pd.Series(block_factor.reindex(out["hour_utc"]).to_numpy(),
                                 index=out.index)
        estimates = rolling_two_factor(out["r"], factor_series, block_series)

    out["alpha"] = estimates["alpha"].to_numpy()
    out["beta"] = estimates["beta"].to_numpy()
    out["beta_block"] = estimates["beta_block"].to_numpy()
    out["e_resid"] = out["r"] - (out["alpha"] + out["beta"] * factor_series
                                 + out["beta_block"] * block_series)

    # The residual's own long-term sigma, on data strictly before the current bar.
    out["sigma_lt_resid"] = (out["e_resid"].shift(1)
                             .rolling(windows.SIGMA_LT_BARS,
                                      min_periods=windows.SIGMA_LT_MIN_BARS)
                             .std(ddof=1))

    # Winsorization of the residual per §2.5 - with its own MAD and its own floor.
    # The floor takes the same half-tick return: a residual is never finer than
    # the price step anyway.
    from meals.returns import _rolling_mad

    mad_24 = _rolling_mad(out["e_resid"], windows.MAD_WINDOW)
    half_tick = np.log1p(asset.tick_size / 2 / out["close"])
    eps = np.fmax(0.2 * out["sigma_lt_resid"], half_tick)
    mad_eff = np.maximum(mad_24, eps)
    limit = 5 * mad_eff
    out["e_resid_w"] = np.where(out["e_resid"].abs() > limit,
                                np.sign(out["e_resid"]) * limit, out["e_resid"])
    out.loc[mad_eff.isna(), "e_resid_w"] = out["e_resid"][mad_eff.isna()]
    return out


def score_residuals(frame: pd.DataFrame, w_asset: int) -> pd.DataFrame:
    """Runs the residual series through the §3.1 machinery with its own states.

    Per §3.6, Q95_resid is computed, stored and exported PURELY for diagnostics:
    it takes part in no condition anywhere in the document. Only Q99_resid works
    in the event-generation condition (§8.2).
    """
    from meals import zscore

    out = frame.copy()
    if out.empty:
        return out.assign(z_resid=pd.Series(dtype="float64"),
                          q95_resid=pd.Series(dtype="float64"),
                          q99_resid=pd.Series(dtype="float64"))

    z, sigma_eff = zscore.ewma_state(
        out["e_resid"].to_numpy(dtype="float64"),
        out["e_resid_w"].to_numpy(dtype="float64"),
        out["sigma_lt_resid"].to_numpy(dtype="float64"))
    out["z_resid"] = z
    out["sigma_eff_resid"] = sigma_eff
    # Same reason as for the price series: without the floor, which does not
    # exist until sigma_LT appears, Z during the burn-in is meaningless and
    # spoils the percentiles.
    out.loc[out["sigma_lt_resid"].isna(), ["z_resid", "sigma_eff_resid"]] = np.nan

    q95, q99 = zscore.adaptive_thresholds(out["z_resid"].abs(), w_asset)
    out["q95_resid"], out["q99_resid"] = q95, q99
    return out


# Minimum number of assets in session before a cross-sectional spread means
# anything. Below this the standardisation is not attempted and the hour's value
# is NULL - per §1.2 that is "not assessed", not "not an event".
BMP_MIN_ASSETS = 5


def cross_sectional_scale(panel: pd.DataFrame,
                          minimum: int = BMP_MIN_ASSETS) -> pd.DataFrame:
    """Per hour, the spread of the OTHER assets' Z-scores, one column per asset.

    The denominator of the BMP test (Boehmer, Musumeci and Poulsen), which exists
    to solve exactly the failure this detector has. Measured before the change:
    bucketing hours by how wide the cross-section is, 97.1% of all single-asset
    breaches fell in the widest fifth and the calmest 40% of hours produced none
    at all. A detector firing only when everything moves is not finding
    idiosyncratic moves - it is finding market-wide volatility and naming
    whichever asset moved most.

    Dividing by this spread makes the question the right one: not "did this asset
    move a lot" but "did it move a lot compared with what every other asset is
    doing this very hour".

    LEAVE-ONE-OUT, which the classic formulation does not do. In an ordinary
    event study every firm shares one event date and the statistic is about their
    average, so including a firm in its own denominator is harmless. Here each
    asset is tested individually against its peers, and a genuine single-asset
    move would otherwise inflate the very spread it is measured against - the
    asset would raise its own bar and hide itself. Excluding it costs one line
    and removes that.
    """
    values = panel.to_numpy(dtype="float64")
    present = np.isfinite(values)
    filled = np.where(present, values, 0.0)

    count = present.sum(axis=1, keepdims=True)
    total = filled.sum(axis=1, keepdims=True)
    total_sq = (filled ** 2).sum(axis=1, keepdims=True)

    # Leave-one-out moments: subtract this asset's own contribution.
    n = count - present
    mean = np.divide(total - filled, n, out=np.full_like(filled, np.nan), where=n > 1)
    sum_sq = total_sq - filled ** 2
    variance = np.divide(sum_sq - n * mean ** 2, n - 1,
                         out=np.full_like(filled, np.nan), where=n > 1)

    scale = np.sqrt(np.maximum(variance, 0.0))
    enough = (count >= minimum) & present & (n > 1)
    scale = np.where(enough & (scale > 0), scale, np.nan)
    return pd.DataFrame(scale, index=panel.index, columns=panel.columns)


def standardise_cross_section(scored: dict[str, pd.DataFrame],
                              minimum: int = BMP_MIN_ASSETS) -> dict[str, pd.DataFrame]:
    """Adds z_resid_bmp to every asset's frame: its Z divided by its peers' spread.

    Returns new frames rather than mutating, and leaves z_resid untouched beside
    it - the raw score stays exported and logged, because a change of this size
    should be arguable against what it replaced.
    """
    series = {aid: frame.set_index("hour_utc")["z_resid"]
              for aid, frame in scored.items() if "z_resid" in frame}
    if not series:
        return scored

    panel = pd.DataFrame(series).sort_index()
    scale = cross_sectional_scale(panel, minimum)

    out = {}
    for aid, frame in scored.items():
        if aid not in panel:
            out[aid] = frame.assign(bmp_scale=np.nan, z_resid_bmp=np.nan)
            continue
        own = scale[aid].reindex(frame["hour_utc"]).to_numpy()
        out[aid] = frame.assign(
            bmp_scale=own,
            z_resid_bmp=frame["z_resid"].to_numpy() / own)
    return out


def rescore_thresholds(frame: pd.DataFrame, w_asset: int,
                       column: str = "z_resid_bmp") -> pd.DataFrame:
    """Recomputes the adaptive Q95/Q99 on whichever score the trigger will use.

    The thresholds of §3.1 are percentiles of the series' own recent history, so
    changing the series means the thresholds have to follow it. Keeping the old
    ones would compare a standardised score against an unstandardised yardstick.
    """
    from meals import zscore

    if frame.empty or column not in frame:
        return frame
    q95, q99 = zscore.adaptive_thresholds(frame[column].abs(), w_asset)
    return frame.assign(q95_resid=q95, q99_resid=q99)

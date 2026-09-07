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
    # The estimation-window moments travel with the coefficients: Patell's
    # inflation needs the window's size and the factor's spread within it, and
    # they must come from the SAME window the coefficients did.
    count = rolling["f"].count()
    gap = windows.REGRESSION_GAP_BARS
    estimates = pd.DataFrame({
        "alpha": alpha.shift(gap), "beta": beta.shift(gap),
        "n_est": count.shift(gap),
        "f_mean": mean_f.shift(gap), "f_var": var_f.shift(gap),
    })
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
    gap = windows.REGRESSION_GAP_BARS
    estimates = pd.DataFrame({
        "alpha": alpha.shift(gap), "beta": beta.shift(gap),
        "beta_block": beta_block.shift(gap),
        # Carried for Patell's inflation, from the same window and with the
        # same gap as the coefficients themselves.
        "n_est": rolling["x1"].count().shift(gap),
        "f_mean": mean_1.shift(gap), "b_mean": mean_2.shift(gap),
        "f_var": var_1.shift(gap), "b_var": var_2.shift(gap),
        "fb_cov": cov_12.shift(gap),
    })
    return estimates.reindex(returns.index)


def patell_scale(estimates: pd.DataFrame, factor: pd.Series,
                 block: pd.Series | None = None) -> pd.Series:
    """How much wider a forecast error is than an in-sample residual.

    The residual here is an OUT-OF-SAMPLE forecast error: the coefficients come
    from a window that ends before the bar being judged, so the error carries
    the estimation error of those coefficients on top of the noise. Dividing it
    by an in-sample sigma therefore understates the scale and overstates the
    score. Patell's correction is the ratio of the two:

        Var(forecast error) = s^2 * [ 1 + 1/L + h_t ]

    with L the estimation window's size and h_t the LEVERAGE of the current
    factor values - how far they sit from the window's own centre, in units of
    the window's spread. The leverage term is the one that matters here, and it
    matters most exactly when the system is most likely to fire: on a violent
    hour the factor is far from its average, the fitted beta is extrapolating,
    and the residual is genuinely noisier than usual. Not correcting for it
    inflates the score precisely on the days the detector is asked about.

    For the two-factor model the leverage is the full quadratic form, cross term
    included, rather than the sum of two one-factor terms: the basket factor and
    a block factor are not orthogonal - a block is part of the basket - and
    pretending they were would understate the leverage whenever they move
    together, which is the case of interest.
    """
    n = estimates.get("n_est")
    if n is None or "f_var" not in estimates:
        return pd.Series(1.0, index=factor.index)

    df = factor - estimates["f_mean"]
    var_f = estimates["f_var"]
    if block is None or "b_var" not in estimates:
        leverage = df ** 2 / ((n - 1) * var_f)
    else:
        db = block - estimates["b_mean"]
        var_b, cov = estimates["b_var"], estimates["fb_cov"]
        det = var_f * var_b - cov ** 2
        quad = df ** 2 * var_b - 2 * df * db * cov + db ** 2 * var_f
        leverage = quad / ((n - 1) * det)
        # A degenerate window - the two factors collinear inside it - falls back
        # to the basket factor alone, exactly as the regression itself does.
        single = df ** 2 / ((n - 1) * var_f)
        leverage = leverage.where((det / (var_f * var_b)).abs() >= 1e-8, single)

    inflation = 1.0 + 1.0 / n + leverage
    # Never smaller than one: the forecast error cannot be tighter than the
    # in-sample residual, and a negative leverage means the window was
    # degenerate rather than that the bar was easy to predict.
    return np.sqrt(inflation.clip(lower=1.0)).fillna(1.0)


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

    # Patell's inflation, carried as a column rather than folded into e_resid:
    # the raw residual is what the absolute channel, the retention check and
    # the event export all read, and it should stay the size the price actually
    # moved. Only the STANDARDISATION divides by it.
    est = estimates.set_axis(out.index)
    out["patell_scale"] = patell_scale(
        est, factor_series, None if block_factor is None else block_series).to_numpy()

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

    # The score is Patell's standardised residual: the forecast error divided by
    # the scale a forecast error actually has, not by the scale an in-sample one
    # would have had. Both the value and the winsorized value are divided, so
    # the winsorization still bites on the same relative sizes.
    scale = (out["patell_scale"].to_numpy(dtype="float64")
             if "patell_scale" in out else np.ones(len(out)))
    z, sigma_eff = zscore.ewma_state(
        out["e_resid"].to_numpy(dtype="float64") / scale,
        out["e_resid_w"].to_numpy(dtype="float64") / scale,
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
    dof = np.where(enough, np.broadcast_to(n, scale.shape) - 1.0, np.nan)
    return (pd.DataFrame(scale, index=panel.index, columns=panel.columns),
            pd.DataFrame(dof, index=panel.index, columns=panel.columns))


def normalise_t(ratio: np.ndarray, dof: np.ndarray) -> np.ndarray:
    """Puts a t-statistic on the standard normal scale, given its own dof.

    WHY THIS IS NEEDED AT ALL. `z_resid / bmp_scale` is not a Z-score. The
    denominator is a sample standard deviation of n peers, and leave-one-out
    makes it independent of the numerator, so the ratio is
    N(0,1) / sqrt(chi2_{n-1}/(n-1)) - a t-statistic with n-1 degrees of freedom,
    exactly. Its heavy tail is not a defect to be clipped away; it is the
    correct sampling distribution of a ratio whose denominator was estimated.

    THE DEFECT IS THAT n IS NOT CONSTANT. Between five instruments in session
    overnight and twenty-three during the US day, the same number means
    entirely different things:

        a score of 11.5 with  6 peers - one hour in 11 thousand
        a score of 11.5 with 23 peers - one hour in 11 billion

    and both were handed to one severity ladder. Measured on the store, the
    p99.9 of |score| runs 11.77 for hours with 5-8 peers against 5.80 for
    13-17 - a factor of two purely from how many instruments happened to be
    open. The ladder read that as the overnight hours being the violent ones.

    THE TRANSFORM. Wallace's (1959) normalising approximation for Student's t,
    which maps a t with `dof` degrees of freedom onto the standard normal
    scale. Chosen over the exact probability integral transform after measuring
    both: on this store the exact transform leaves the widest and narrowest
    peer-count buckets differing by 1.09x and Wallace by 1.08x - no better,
    because what remains is the residuals not being exactly normal rather than
    any inaccuracy in the mapping. Wallace needs a logarithm and a square root
    where the exact transform needs an incomplete beta function, a dependency
    this project does not carry and would not earn its place here.

    A FLOOR ON THE DENOMINATOR WOULD NOT HAVE FIXED THIS. It would have capped
    the score in thin hours while leaving the calibration wrong in every other
    hour, and the value of the floor would have been a number chosen by taste.
    """
    out = np.full_like(np.asarray(ratio, dtype="float64"), np.nan)
    good = np.isfinite(ratio) & np.isfinite(dof) & (dof >= 1)
    t = np.asarray(ratio, dtype="float64")[good]
    v = np.asarray(dof, dtype="float64")[good]
    out[good] = (np.sign(t) * np.sqrt(v * np.log1p(t * t / v))
                 * (8.0 * v + 3.0) / (8.0 * v + 1.0))
    return out


def standardise_cross_section(scored: dict[str, pd.DataFrame],
                              minimum: int = BMP_MIN_ASSETS) -> dict[str, pd.DataFrame]:
    """Adds z_resid_bmp to every asset's frame: its Z against its peers' spread,
    put on the standard normal scale.

    The division by the peers' spread produces a t-statistic, not a Z-score, and
    its degrees of freedom change every hour with how many instruments are in
    session - see normalise_t, which is what makes the result comparable between
    a five-instrument night and a twenty-three-instrument afternoon.

    Returns new frames rather than mutating, and leaves z_resid untouched beside
    it - the raw score stays exported and logged, because a change of this size
    should be arguable against what it replaced.
    """
    series = {aid: frame.set_index("hour_utc")["z_resid"]
              for aid, frame in scored.items() if "z_resid" in frame}
    if not series:
        return scored

    panel = pd.DataFrame(series).sort_index()
    scale, dof = cross_sectional_scale(panel, minimum)

    out = {}
    for aid, frame in scored.items():
        if aid not in panel:
            out[aid] = frame.assign(bmp_scale=np.nan, bmp_dof=np.nan,
                                    t_resid=np.nan, z_resid_bmp=np.nan)
            continue
        own = scale[aid].reindex(frame["hour_utc"]).to_numpy()
        own_dof = dof[aid].reindex(frame["hour_utc"]).to_numpy()
        ratio = frame["z_resid"].to_numpy() / own
        out[aid] = frame.assign(
            bmp_scale=own,
            bmp_dof=own_dof,
            # The raw ratio stays beside the transformed score for the same
            # reason z_resid does: a change of this size should be arguable
            # against what it replaced.
            t_resid=ratio,
            z_resid_bmp=normalise_t(ratio, own_dof))
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

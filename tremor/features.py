"""Feature matrix for the volatility-forecast detector.

The v1 detector scored an hour by summing binary triggers built from
cross-sectional structure - breadth, single-factorness, correlation. It reached
F1 23% against a trailing-SPY rule's 25%, and its out-of-sample F1 sat near 9%.
The literature says why, and says it plainly: average pairwise correlation and
breadth are weak standalone warning scores, while volatility, VIX, downside
semivolatility and cross-sectional dispersion are the strong ones. v1 computed
the strong signals and used them nowhere - CSV only inside a condition that fired
zero times, VIX only as a multiplier active in 3% of hours.

So the strong signals become the inputs here, and the cross-sectional ones stay
as complements, which is what they are good for.

The other correction is the horizon. Every v1 trigger was a one-hour statistic
answering a question about the next twenty-four. Volatility has long memory that
no single horizon captures, and the standard response - the HAR model - simply
uses several at once: an hour, a day, a week, a month. That is most of what this
file does.

Everything here is strictly trailing. A feature at hour t is computed from bars
that closed at or before t, so the matrix can be handed to a forecaster without
any further care about leakage.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tremor import windows

# HAR horizons in reference-calendar hours: an hour, a day, a trading week, a
# trading month. The point of the ladder is that volatility is persistent at
# every scale and a single window has to choose one.
HORIZONS = {"h1": 1, "h24": 24, "h120": 120, "h480": 480}

# Window over which a feature is standardised against its own past, so that a
# value means the same thing in a calm year as in a violent one. Ten trading
# weeks, matching W_CS.
SCALE_WINDOW = windows.W_CS

# Forward horizon the forecaster is asked to predict (§7's own horizon).
TARGET_HORIZON = 24


# Floor under a volatility before its logarithm is taken. An hour in which the
# basket median does not move at all would otherwise give minus infinity.
LOG_FLOOR = 1e-8


def realized_volatility(returns: pd.Series, horizon: int) -> pd.Series:
    """Root mean square of the trailing `horizon` returns.

    Not the standard deviation: the mean of a return series over a day is
    indistinguishable from zero and subtracting it only adds noise. This is the
    realized-volatility convention.
    """
    minimum = max(horizon // 2, 1)
    return np.sqrt((returns ** 2).rolling(horizon, min_periods=minimum).mean())


def log_volatility(returns: pd.Series, horizon: int) -> pd.Series:
    """Realized volatility on the log scale, which is where HAR lives.

    Volatility is strongly right-skewed, so a least-squares fit on the raw
    quantity is dominated by a handful of violent days and fits the ordinary ones
    badly - measured here as a NEGATIVE out-of-sample R-squared for both the
    model and the trailing benchmark, on a quantity that is genuinely persistent.
    Log volatility is close to Gaussian, which is why the HAR literature works in
    it almost without exception.
    """
    return np.log(realized_volatility(returns, horizon).clip(lower=LOG_FLOOR))


def downside_volatility(returns: pd.Series, horizon: int) -> pd.Series:
    """Realized volatility of the negative returns alone.

    Named in the literature as one of the stronger warning signals, and it is a
    different quantity from total volatility: a market grinding upward and a
    market falling apart can share a variance.
    """
    negative = returns.where(returns < 0, 0.0)
    minimum = max(horizon // 2, 1)
    return np.sqrt((negative ** 2).rolling(horizon, min_periods=minimum).mean())


def standardise(series: pd.Series, window: int = SCALE_WINDOW) -> pd.Series:
    """Express a feature in units of its own trailing distribution.

    The regime problem v1 died of: a fixed threshold on a raw feature means
    something different in 2022 than in 2025. Centring and scaling on a trailing
    window - shifted by one bar, so the current value is never part of its own
    yardstick - makes the feature comparable across regimes by construction.
    """
    past = series.shift(1)
    centre = past.rolling(window, min_periods=window // 4).median()
    spread = past.rolling(window, min_periods=window // 4).std(ddof=1)
    return ((series - centre) / spread).replace([np.inf, -np.inf], np.nan)


def target(m: pd.Series, horizon: int = TARGET_HORIZON) -> pd.Series:
    """What the forecaster predicts: LOG realized volatility of the basket over
    the NEXT `horizon` hours.

    Shifted so that the value at t describes hours t+1 .. t+horizon, and left
    NaN where the window runs off the end of the history - missing, not calm.
    On the log scale for the same reason the features are: least squares on raw
    volatility chases the tail and fits the body badly.
    """
    forward = np.sqrt((m ** 2).rolling(horizon, min_periods=horizon).mean())
    return np.log(forward.clip(lower=LOG_FLOOR)).shift(-horizon)


def build(basket_frame: pd.DataFrame) -> pd.DataFrame:
    """The full matrix, indexed by reference hour.

    Takes metrics_basket_hour, which already carries M_t, csv_norm, pc1_ratio,
    mean_pairwise_corr and the VIX multiplier - v1 computed all of them and then
    used most of them for nothing.
    """
    m = basket_frame["m_weighted_median"]
    out = pd.DataFrame(index=basket_frame.index)

    # --- the HAR ladder, the core of the forecast -------------------------
    for name, horizon in HORIZONS.items():
        out[f"rv_{name}"] = log_volatility(m, horizon)
    out["rv_down_24"] = np.log(downside_volatility(m, 24).clip(lower=LOG_FLOOR))
    out["rv_down_120"] = np.log(downside_volatility(m, 120).clip(lower=LOG_FLOOR))

    # --- the other signals the field calls strong -------------------------
    if "csv_norm" in basket_frame:
        out["csv_level"] = basket_frame["csv_norm"]
        out["csv_level_24"] = basket_frame["csv_norm"].rolling(24, min_periods=12).mean()
    if "m_vix" in basket_frame:
        # The stored column is the multiplier; what matters here is the level of
        # stress it stands for, which is what its excess over one encodes.
        out["vix_stress"] = basket_frame["m_vix"].fillna(1.0) - 1.0

    # --- the cross-sectional complements ----------------------------------
    for column, name in (("pc1_ratio", "pc1"), ("mean_pairwise_corr", "corr"),
                         ("breadth_share", "breadth"), ("saed_count_24h", "saed"),
                         ("m_calendar", "calendar")):
        if column in basket_frame:
            out[name] = basket_frame[column]

    # Everything expressed against its own trailing distribution, so the
    # forecaster sees comparable numbers in every regime.
    scaled = pd.DataFrame({f"z_{c}": standardise(out[c]) for c in out.columns},
                          index=out.index)
    return pd.concat([out, scaled], axis=1)


def scaled_columns(frame: pd.DataFrame) -> list[str]:
    """The columns expressed against their own trailing distribution."""
    return [c for c in frame.columns if c.startswith("z_")]


def level_columns(frame: pd.DataFrame) -> list[str]:
    """The columns as levels, un-standardised."""
    return [c for c in frame.columns if not c.startswith("z_")]


def model_columns(frame: pd.DataFrame) -> list[str]:
    """Everything the forecaster is fed: levels AND their standardised forms.

    Both, and the distinction is not cosmetic. Standardising alone was tried and
    is much worse than the trailing benchmark - R-squared -0.11 against 0.10 -
    because it removes exactly what predicts: forward volatility is mostly
    explained by the LEVEL of recent volatility, and asking "is this unusual for
    conditions" throws that away. Levels alone reach 0.16. Together they reach
    0.20, because the two answer different questions and the forecast wants both.
    """
    return level_columns(frame) + scaled_columns(frame)

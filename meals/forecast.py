"""HAR forecast of the basket's forward volatility.

The detector's score. v1 summed binary triggers; this predicts a number - the
realized volatility of the basket over the next twenty-four reference hours - and
the alert is then a question about that prediction rather than about a committee
of yes/no votes.

Linear, and deliberately so. HAR is a linear regression on a few volatility
horizons, it is the field's own baseline, and it beats far more elaborate models
often enough that starting anywhere else would be a choice to defend rather than
a default. A handful of coefficients can also be printed and argued with, which
matters more here than the last decimal of fit.

Fitted on an EXPANDING window, refit periodically, and only ever on rows whose
target is already known at the moment of fitting. At hour t the model has seen
targets up to t - horizon, because the target at t - horizon is the volatility of
hours t-horizon+1 .. t, which have closed. Anything more recent is the future.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from meals import features

log = logging.getLogger("meals.forecast")

# How often the coefficients are refit, in reference hours. Weekly: the
# coefficients of a HAR model move slowly, and refitting every bar would spend a
# great deal of arithmetic to chase noise.
REFIT_EVERY = 120

# Rows required before the first fit.
MIN_TRAIN = 2000


def _design(frame: pd.DataFrame, columns: list[str]) -> np.ndarray:
    """Feature matrix with an intercept, NaN filled with the column's own zero -
    which, on standardised features, is the neutral value."""
    values = frame[columns].to_numpy(dtype="float64")
    values = np.where(np.isfinite(values), values, 0.0)
    return np.column_stack([np.ones(len(values)), values])


def fit_predict(frame: pd.DataFrame, y: pd.Series, columns: list[str],
                refit_every: int = REFIT_EVERY, min_train: int = MIN_TRAIN,
                horizon: int = features.TARGET_HORIZON) -> pd.Series:
    """Walk-forward prediction over the whole index.

    Returns a prediction for every hour from the first fit onward, each made by a
    model that saw only rows whose targets had already resolved. Least squares
    through the normal equations with a small ridge term - the HAR horizons are
    nested and therefore correlated, and without it the 480-hour column can hand
    the 120-hour one an arbitrarily large offsetting coefficient.
    """
    x = _design(frame, columns)
    target = y.to_numpy(dtype="float64")
    n = len(frame)
    out = np.full(n, np.nan)

    beta = None
    for i in range(n):
        # The most recent row whose target has resolved by hour i.
        usable = i - horizon
        if usable >= min_train and (beta is None or i % refit_every == 0):
            past_x, past_y = x[:usable], target[:usable]
            ok = np.isfinite(past_y)
            if ok.sum() >= min_train:
                a, b = past_x[ok], past_y[ok]
                gram = a.T @ a + 1e-6 * np.eye(a.shape[1])
                beta = np.linalg.solve(gram, a.T @ b)
        if beta is not None:
            out[i] = x[i] @ beta

    return pd.Series(out, index=frame.index, name="forecast")


def skill(prediction: pd.Series, actual: pd.Series) -> dict:
    """Out-of-sample R^2 against the obvious benchmark.

    The benchmark is not zero but the trailing realized volatility itself -
    "tomorrow looks like today", which in volatility is a genuinely strong rule.
    A forecast that cannot beat it is not adding anything.
    """
    both = pd.DataFrame({"p": prediction, "a": actual}).dropna()
    if len(both) < 100:
        return {"n": len(both), "r2": np.nan, "correlation": np.nan}
    residual = ((both["a"] - both["p"]) ** 2).sum()
    total = ((both["a"] - both["a"].mean()) ** 2).sum()
    return {"n": len(both),
            "r2": float(1 - residual / total) if total else np.nan,
            "correlation": float(both["p"].corr(both["a"]))}

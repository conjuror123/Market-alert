import math

import numpy as np
import pandas as pd
import pytest

from tremor import windows, zscore

LAM = windows.LAMBDA  # 2/25 = 0.08


def test_z_is_measured_against_the_previous_bar_state():
    # A reference computed by hand. Initial state: mean = 0,
    # var = r[0]^2 = 1e-4, so sigma_eff = 0.01 and Z[0] = 0.01/0.01 = 1.
    r = np.array([0.01, 0.02])
    sigma_lt = np.array([0.001, 0.001])  # floor 2e-4, does not touch the denominator
    z, _ = zscore.ewma_state(r, r, sigma_lt)

    assert z[0] == pytest.approx(1.0)

    # After the first bar: mean = 0.08*0.01 = 8e-4,
    # var = 0.08*(0.01-0)^2 + 0.92*1e-4 = 1e-4, sigma_eff = 0.01.
    assert z[1] == pytest.approx((0.02 - 8e-4) / 0.01)


def test_variance_update_uses_the_previous_mean():
    # The variance is fed the ewma_mean
    # of the PREVIOUS bar. Both forms look natural but give different numbers, and
    # diverging here means quietly computing the wrong thing.
    r = np.array([0.01, 0.02, -0.03])
    sigma_lt = np.full(3, 0.001)
    z, _ = zscore.ewma_state(r, r, sigma_lt)

    mean1 = LAM * 0.01
    var1 = LAM * (0.01 - 0.0) ** 2 + (1 - LAM) * 1e-4
    mean2 = LAM * 0.02 + (1 - LAM) * mean1
    var2 = LAM * (0.02 - mean1) ** 2 + (1 - LAM) * var1  # mean1, not mean2

    assert z[2] == pytest.approx((-0.03 - mean2) / math.sqrt(var2))

    # For contrast: with the new mean in the variance the answer would differ.
    wrong_var2 = LAM * (0.02 - mean2) ** 2 + (1 - LAM) * var1
    assert z[2] != pytest.approx((-0.03 - mean2) / math.sqrt(wrong_var2))


def test_state_update_takes_the_winsorised_return():
    # A spike must not inflate the norm: the state gets r_w, Z gets r.
    r = np.array([0.01, 0.50, 0.01])
    r_w = np.array([0.01, 0.02, 0.01])   # the spike is clipped for the state
    sigma_lt = np.full(3, 0.001)

    z_clipped, _ = zscore.ewma_state(r, r_w, sigma_lt)
    z_raw, _ = zscore.ewma_state(r, r, sigma_lt)

    # The spike's own Z is the same - it is measured against the PREVIOUS state.
    assert z_clipped[1] == pytest.approx(z_raw[1])
    # The next bar does differ: the unclipped state inflated.
    assert abs(z_clipped[2]) > abs(z_raw[2])


def test_sigma_eff_never_falls_below_the_floor():
    # A frozen quote collapses the EWMA variance. Without the floor any
    # subsequent move would produce an astronomical Z.
    r = np.array([0.0] * 30 + [0.001])
    sigma_lt = np.full(31, 0.01)  # floor 0.002
    z, sigma_eff = zscore.ewma_state(r, r, sigma_lt)

    assert sigma_eff[-1] == pytest.approx(0.2 * 0.01)
    assert abs(z[-1]) < 1.0


def test_thresholds_exclude_the_current_bar():
    # A threshold that contains the observation being judged adjusts towards it
    # and understates its own firing.
    abs_z = pd.Series([1.0] * 10 + [100.0])
    q95, q99 = zscore.adaptive_thresholds(abs_z, window=10)

    # On the last bar the window is the first ten ones; the spike did not enter it.
    assert q99.iloc[-1] == pytest.approx(1.0)


def test_thresholds_are_smoothed_by_the_specified_recurrence():
    abs_z = pd.Series([1.0] * 10 + [5.0] * 10)
    q95, _ = zscore.adaptive_thresholds(abs_z, window=10)

    raw = abs_z.shift(1).rolling(10, min_periods=10).quantile(0.95)
    expected = raw.ewm(alpha=windows.LAMBDA_Q, adjust=False).mean()
    pd.testing.assert_series_equal(q95, expected, check_names=False)

    # Smoothing slows the threshold: it does not jump along with the window.
    assert q95.iloc[-1] < raw.iloc[-1]

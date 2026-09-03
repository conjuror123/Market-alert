import math

import numpy as np
import pandas as pd
import pytest

from meals import windows, zscore

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
    # The spec spells this out in its own line: the variance is fed the ewma_mean
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


def test_breach_needs_both_legs():
    abs_z = pd.Series([5.0, 5.0, 1.0])
    abs_r = pd.Series([0.10, 0.001, 0.10])
    sigma_lt = pd.Series([0.01, 0.01, 0.01])
    q95 = pd.Series([2.0, 2.0, 2.0])
    q99 = pd.Series([3.0, 3.0, 3.0])

    out = zscore.breaches(abs_z, abs_r, sigma_lt, q95, q99)

    # Both legs: |Z| > Q99 and |r| >= 3 * sigma_LT.
    assert bool(out["breach_q99"].iloc[0])
    # The relative leg passed, the absolute one did not - the move is negligible
    # by the standards of the whole history, however rare it is for the current
    # lull.
    assert not bool(out["breach_q99"].iloc[1])
    # The absolute leg passed, the relative one did not.
    assert not bool(out["breach_q99"].iloc[2])


def test_q95_leg_is_looser_than_q99():
    abs_z = pd.Series([2.5])
    abs_r = pd.Series([0.02])
    sigma_lt = pd.Series([0.01])
    out = zscore.breaches(abs_z, abs_r, sigma_lt, pd.Series([2.0]), pd.Series([3.0]))

    assert bool(out["breach_q95"].iloc[0])
    assert not bool(out["breach_q99"].iloc[0])


def test_unevaluated_breach_is_null_not_false():
    # §1.2: NULL and False are different things. "The threshold does not exist
    # yet" cannot be written down as "the threshold was not exceeded".
    abs_z = pd.Series([5.0, 5.0])
    abs_r = pd.Series([0.1, 0.1])
    sigma_lt = pd.Series([np.nan, 0.01])
    out = zscore.breaches(abs_z, abs_r, sigma_lt, pd.Series([2.0, 2.0]),
                          pd.Series([3.0, 3.0]))

    assert pd.isna(out["breach_q99"].iloc[0])
    assert out["breach_q99"].iloc[1] is np.True_ or bool(out["breach_q99"].iloc[1])


def test_compute_drops_z_while_sigma_lt_is_unknown():
    # During the burn-in the denominator has no floor, and on frozen quotes Z
    # comes out meaningless. Such values must not enter the percentile window -
    # per §6.6 an hour before first_valid_hour takes no part in the statistics.
    frame = pd.DataFrame({
        "r": [0.0, 0.0, 0.0, 0.01, 0.01],
        "r_w": [0.0, 0.0, 0.0, 0.01, 0.01],
        "sigma_lt": [np.nan, np.nan, np.nan, 0.01, 0.01],
    })
    out = zscore.compute(frame, w_asset=2)

    assert out["z"].iloc[:3].isna().all()
    assert out["z"].iloc[3:].notna().all()


def test_compute_on_empty_input_keeps_columns():
    out = zscore.compute(pd.DataFrame({"r": [], "r_w": [], "sigma_lt": []}), w_asset=10)
    assert out.empty
    assert {"z", "q95", "q99", "breach_q95", "breach_q99"} <= set(out.columns)

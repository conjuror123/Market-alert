import numpy as np
import pandas as pd
import pytest

from tremor import ewma, windows


def brute(x, half_life, span, t):
    """The definition, written out, with no recursion in it."""
    lam = 0.5 ** (1.0 / half_life)
    used = x[max(0, t - span):t][::-1]
    w = lam ** np.arange(len(used))
    keep = np.isfinite(used)
    return float(np.sqrt(np.dot(w[keep], used[keep] ** 2) / w[keep].sum()))


def noise(n=3000, seed=1):
    return pd.Series(np.random.default_rng(seed).standard_normal(n) * 1e-3)


def test_it_is_the_weighted_spread_it_claims_to_be():
    # Against a direct sum rather than a second recursion: the one-pass form is
    # the thing under test, so a test that re-implements it could not fail.
    x = noise()
    got = ewma.long_run_sigma(x, 137, 500, 1)
    for t in (600, 1500, 2999):
        assert got.iloc[t] == pytest.approx(brute(x.to_numpy(), 137, 500, t), rel=1e-12)


def test_a_short_history_is_measured_over_what_it_has():
    # XLP holds 7,246 bars against a span of 8,400. Demanding a full window
    # would leave it with no sigma at all, and the instrument would go silent.
    x = noise(n=400)
    got = ewma.long_run_sigma(x, 137, 500, 1)
    assert np.isfinite(got.iloc[399])
    assert got.iloc[399] == pytest.approx(brute(x.to_numpy(), 137, 500, 399), rel=1e-12)


def test_no_bar_is_described_using_itself():
    # One enormous bar at the end. If it entered its own estimate the sigma on
    # that bar would jump, and the move would be measured against itself.
    x = noise().copy()
    before = ewma.long_run_sigma(x, 137, 500, 1).iloc[-1]
    x.iloc[-1] = 1.0
    assert ewma.long_run_sigma(x, 137, 500, 1).iloc[-1] == pytest.approx(before)


def test_recent_bars_weigh_more_than_old_ones():
    # The whole point. A burst just before the bar must move sigma further than
    # the same burst at the far end of the window - which under the box window
    # this replaced it did not, by construction.
    x = noise()
    quiet = ewma.long_run_sigma(x, 137, 500, 1).iloc[-1]
    near, far = x.copy(), x.copy()
    near.iloc[-20:-10] *= 50
    far.iloc[-480:-470] *= 50
    lifted_near = ewma.long_run_sigma(near, 137, 500, 1).iloc[-1]
    lifted_far = ewma.long_run_sigma(far, 137, 500, 1).iloc[-1]
    assert lifted_near > lifted_far > quiet


def test_a_bar_leaving_the_window_barely_moves_the_yardstick():
    # The edge this estimator exists to soften. Under a box window one violent
    # bar counts in full, then not at all, and sigma steps down on the day it
    # falls out - for no reason the market would recognise. It cannot be removed
    # entirely while the window is bounded, only reduced to the weight the
    # oldest bar carries, which at six half-lives is a sixty-fourth.
    #
    # Measured at production proportions on a twenty-sigma bar: the box steps
    # 16.8%, this steps 1.5%.
    half_life = 137
    span = windows.SIGMA_LT_SPAN_HALFLIVES * half_life
    x = noise(n=4000)
    x.iloc[900] = 20e-3
    exp = ewma.long_run_sigma(x, half_life, span, 1)
    box = x.shift(1).rolling(span, min_periods=1).std(ddof=1)
    leaves = 900 + span + 1
    step = lambda s: abs(s.iloc[leaves] / s.iloc[leaves - 1] - 1)
    assert step(box) > 0.15
    assert step(exp) < step(box) / 8


def test_a_gap_in_the_data_is_not_a_period_of_calm():
    # NaN dropped from the weight total as well as the sum. Treated as a zero
    # return it would report missing data as quiet and inflate every multiple
    # measured against it.
    x = noise()
    holed = x.copy()
    holed.iloc[1000:1200] = np.nan
    full = ewma.long_run_sigma(x, 137, 500, 1)
    with_hole = ewma.long_run_sigma(holed, 137, 500, 1)
    assert with_hole.iloc[1300] == pytest.approx(full.iloc[1300], rel=0.25)
    assert with_hole.iloc[1300] > 0


def test_nothing_is_said_before_there_is_enough_to_say_it_with():
    # min_periods counts real observations inside the span, so it is only
    # reachable when the span is at least that long - as it is in production,
    # where 8,400 bars of span sit over a 720-bar floor.
    got = ewma.long_run_sigma(noise(), 137, 1000, 720)
    assert got.iloc[:720].isna().all()
    assert np.isfinite(got.iloc[720])


def test_an_empty_or_one_bar_series_answers_without_raising():
    assert len(ewma.long_run_sigma(pd.Series([], dtype="float64"), 10, 20, 1)) == 0
    assert ewma.long_run_sigma(pd.Series([0.01]), 10, 20, 1).isna().all()


def test_the_settings_are_the_ones_the_registry_holds():
    x = noise(n=12000)
    assert ewma.sigma_lt(x).equals(ewma.long_run_sigma(
        x, windows.SIGMA_LT_HALFLIFE_BARS, windows.SIGMA_LT_BARS,
        windows.SIGMA_LT_MIN_BARS))


def test_the_span_reaches_six_half_lives():
    # Not a style preference: at four half-lives the measured gain is a third of
    # what it could be, and past six it stops improving (tools/sigma_window.py).
    assert windows.SIGMA_LT_BARS == 6 * windows.SIGMA_LT_HALFLIFE_BARS
    assert windows.warm_bars(840) > windows.SIGMA_LT_BARS

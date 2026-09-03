import pytest

from meals import windows


def test_w_asset_scales_with_the_session_length():
    # A US ETF: 7 bars in a session -> 840. A currency pair: 24 -> 2880. The same
    # number of bars covers a different stretch of history for each, which is why
    # the window is derived from B_asset rather than set as a constant.
    assert windows.w_asset(7) == 840
    assert windows.w_asset(24) == 2880


def test_w_asset_has_a_floor():
    # On an instrument with a very short session the window must not collapse.
    assert windows.w_asset(1) == 720
    assert windows.w_asset(6) == 720


def test_w_asset_rejects_a_nonpositive_session():
    with pytest.raises(ValueError):
        windows.w_asset(0)


def test_sigma_lt_takes_all_history_until_the_cap():
    assert windows.sigma_lt_bars(1000) == 1000
    assert windows.sigma_lt_bars(9000) == windows.SIGMA_LT_BARS


def test_sigma_lt_is_undefined_below_the_minimum():
    # "no fewer than 720 bars" is not a suggestion: on a shorter series the
    # long-term sigma is undefined, and substituting something silently is not on.
    with pytest.raises(ValueError, match="720"):
        windows.sigma_lt_bars(719)


def test_ewma_periods_match_the_specification():
    assert windows.LAMBDA == pytest.approx(2 / 25)
    assert windows.LAMBDA_Q == pytest.approx(2 / 121)

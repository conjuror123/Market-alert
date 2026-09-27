import pytest

from tremor import windows


def test_sigma_lt_takes_all_history_until_the_cap():
    assert windows.sigma_lt_bars(1000) == 1000
    assert windows.sigma_lt_bars(9000, "us_equity") == windows.sigma_lt_span("us_equity")


def test_sigma_lt_is_undefined_below_the_minimum():
    # "no fewer than 720 bars" is not a suggestion: on a shorter series the
    # long-term sigma is undefined, and substituting something silently is not on.
    with pytest.raises(ValueError, match="720"):
        windows.sigma_lt_bars(719)


def test_the_ewma_periods_are_the_periods_they_are_named_for():
    # lambda = 2 / (period + 1), so a period of 24 bars and one of 120. Written
    # as the arithmetic rather than as 0.08 and 0.0165, which say nothing.
    assert windows.LAMBDA == pytest.approx(2 / 25)
    assert windows.LAMBDA_Q == pytest.approx(2 / 121)


def test_the_warm_lead_is_the_chain_the_remaining_stages_need():
    # The long-run sigma's span, the block regression behind each residual, and
    # the short-memory burn-in after it - and nothing for the Q95/Q99
    # thresholds, which were removed with the four windows they added.
    for template in ("us_equity", "fx_continuous", "crypto_24_7"):
        assert windows.warm_bars(template) == (
            windows.sigma_lt_span(template) + windows.REGRESSION_WINDOW
            + windows.REGRESSION_GAP_BARS + windows.EWMA_BURN_IN_BARS)
    assert not hasattr(windows, "w_asset")

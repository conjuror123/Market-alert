import pytest

from jump import windows


def test_the_ewma_periods_are_the_periods_they_are_named_for():
    # lambda = 2 / (period + 1), so a period of 24 bars and one of 120. Written
    # as the arithmetic rather than as 0.08 and 0.0165, which say nothing.
    assert windows.LAMBDA == pytest.approx(2 / 25)
    assert windows.LAMBDA_Q == pytest.approx(2 / 121)


def test_the_warm_lead_covers_more_than_a_long_weekend_for_every_calendar():
    # The move needs the bar before it and the gap the last close before the
    # session: a few sessions at most. Two weeks of any calendar is ample.
    for template, bars_a_day in (("us_equity", 7), ("fx_continuous", 24),
                                 ("crypto_24_7", 24)):
        assert windows.warm_bars(template) >= 14 * bars_a_day

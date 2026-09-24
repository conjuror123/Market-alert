import math
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from tremor import bars, returns, windows
from tremor.basket import Asset

HOUR = 3600


def asset(**over):
    base = dict(ticker="SPY", source="twelvedata", tier=1, block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="30min", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def frame(rows):
    return pd.DataFrame(rows, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)


def et(y, m, d, h):
    return int(datetime(y, m, d, h, tzinfo=ZoneInfo("America/New_York")).timestamp())


def two_days():
    # Two trading days of two hours: the first closes at 101, the second opens at 105.
    return frame([
        (et(2021, 3, 1, 10), 100.0, 101.0, 99.0, 100.5, 1.0, 2),
        (et(2021, 3, 1, 11), 100.5, 102.0, 100.0, 101.0, 1.0, 2),
        (et(2021, 3, 2, 10), 105.0, 106.0, 104.0, 106.0, 1.0, 2),
        (et(2021, 3, 2, 11), 106.0, 107.0, 105.0, 106.5, 1.0, 2),
    ])


def test_the_overnight_jump_never_reaches_the_return():
    # Between sessions the price moved from 101 to 105. Had that jump landed in
    # r, every morning would look like an anomaly. The first bar of a session is
    # measured from its OWN open, so the jump is not a return at all.
    out = returns.split_channels(asset(), two_days())
    opening = out.iloc[2]

    assert opening["is_session_open"]
    assert opening["r"] == pytest.approx(math.log(106.0 / 105.0))
    # And emphatically not the close-to-close figure, which is what an ordinary
    # return would have given and is four per cent of nothing.
    assert opening["r"] != pytest.approx(math.log(106.0 / 101.0))


def test_an_ordinary_bar_is_close_to_close():
    out = returns.split_channels(asset(), two_days())
    ordinary = out.iloc[1]

    assert not ordinary["is_session_open"]
    assert ordinary["r"] == pytest.approx(math.log(101.0 / 100.5))


def test_the_very_first_bar_of_history_has_no_return():
    # Its own open is the start of the record rather than a continuation of
    # anything: undefined, not zero.
    out = returns.split_channels(asset(), two_days())
    assert np.isnan(out.iloc[0]["r"])


def test_an_ex_dividend_drop_cannot_reach_the_return():
    # The price drop on an ex-date is mechanical, not a market move, and it
    # happens between sessions. It used to be excluded by flagging the ex-dates
    # from the corporate-actions table; it is now excluded by construction,
    # because nothing that happens between sessions is a return here. The test
    # is the same either way: whatever the price did overnight, r is measured
    # from the session's own open.
    day_two_opens_far_below = frame([
        (et(2021, 3, 1, 10), 100.0, 101.0, 99.5, 100.5, 1.0, 2),
        (et(2021, 3, 1, 11), 100.5, 101.5, 100.0, 101.0, 1.0, 2),
        (et(2021, 3, 2, 10), 90.0, 91.0, 89.0, 90.9, 1.0, 2),
    ])
    opening = returns.split_channels(asset(), day_two_opens_far_below).iloc[2]

    assert opening["is_session_open"]
    assert opening["r"] == pytest.approx(math.log(90.9 / 90.0))   # +1%, not -10%
    assert opening["r"] > 0


def test_crypto_has_no_session_boundaries():
    crypto = asset(ticker="BTC-USD", source="coinbase", block="crypto",
                   session_template="crypto_24_7", fetch_interval="1h")
    out = returns.split_channels(crypto, two_days())

    # Only the very first bar of history opens a session; after that the series is continuous.
    assert int(out["is_session_open"].sum()) == 1


def test_forex_week_is_one_session():
    fx = asset(ticker="EUR/USD", block="FX", has_volume=False, tick_size=0.00001,
               session_template="fx_continuous", fetch_interval="1h")
    # Thursday and Friday of one week, then Monday of the next.
    data = frame([
        (int(datetime(2026, 8, 27, 12, tzinfo=timezone.utc).timestamp()),
         1.0, 1.1, 0.9, 1.05, 0.0, 1),
        (int(datetime(2026, 8, 28, 12, tzinfo=timezone.utc).timestamp()),
         1.05, 1.1, 1.0, 1.06, 0.0, 1),
        (int(datetime(2026, 8, 31, 12, tzinfo=timezone.utc).timestamp()),
         1.08, 1.1, 1.0, 1.09, 0.0, 1),
    ])
    out = returns.split_channels(fx, data)

    # There is no break inside the week, and Monday opens a new one - so Monday
    # is measured from its own open, not across the weekend.
    assert list(out["is_session_open"]) == [True, False, True]
    assert out.iloc[2]["r"] == pytest.approx(math.log(1.09 / 1.08))


def test_winsorization_clips_only_the_state_input():
    # r_w goes into the EWMA update, r stays untouched: clipping the very thing
    # we want to detect is pointless.
    calm = [(i * HOUR, 100.0, 100.1, 99.9, 100.0 + (i % 2) * 0.01, 1.0, 2)
            for i in range(1, 40)]
    spike = [(40 * HOUR, 100.0, 130.0, 99.9, 130.0, 1.0, 2)]
    out = returns.winsorize(asset(), returns.split_channels(
        asset(ticker="BTC-USD", source="coinbase", block="crypto",
              session_template="crypto_24_7", fetch_interval="1h"),
        frame(calm + spike)))

    last = out.iloc[-1]
    assert last["r"] > last["r_w"]           # the raw return exceeds the clipped one
    assert last["r_w"] == pytest.approx(5 * last["mad_eff"])


def test_winsorization_floor_saves_a_stuck_quote():
    # The quote stands still: MAD_24 is zero, and without the floor any move at
    # all would come out "larger than five MADs".
    flat = [(i * HOUR, 100.0, 100.0, 100.0, 100.0, 1.0, 2) for i in range(1, 40)]
    move = [(40 * HOUR, 100.0, 100.2, 100.0, 100.2, 1.0, 2)]
    crypto = asset(ticker="BTC-USD", source="coinbase", block="crypto",
                   session_template="crypto_24_7", fetch_interval="1h")
    out = returns.winsorize(crypto, returns.split_channels(crypto, frame(flat + move)))

    assert out.iloc[-1]["mad_eff"] > 0
    assert np.isfinite(out.iloc[-1]["r_w"])


def test_winsorization_leaves_returns_alone_before_the_window_fills():
    short = frame([(i * HOUR, 100.0, 101.0, 99.0, 100.0 + i, 1.0, 2) for i in range(1, 5)])
    crypto = asset(ticker="BTC-USD", source="coinbase", block="crypto",
                   session_template="crypto_24_7", fetch_interval="1h")
    out = returns.winsorize(crypto, returns.split_channels(crypto, short))

    filled = out["r"].notna()
    assert (out.loc[filled, "r_w"] == out.loc[filled, "r"]).all()


def test_empty_input_keeps_the_columns():
    out = returns.winsorize(asset(), returns.split_channels(asset(), bars.empty_frame()))
    assert out.empty
    assert "r_w" in out.columns


def test_the_vectorised_mad_matches_a_per_window_median_exactly():
    # _rolling_mad was 54% of the whole pipeline: 1.7 million callbacks, one per
    # bar per instrument, over a twenty-four-bar window where the call overhead
    # dwarfs the arithmetic. Vectorising it has to be exact, not close - the
    # levels the ladder fits are downstream of this - so it is checked against
    # the per-window form it replaced, NaNs and short series included.
    def per_window(series, window):
        def mad(values):
            median = np.median(values)
            return float(np.median(np.abs(values - median)))
        return series.shift(1).rolling(window).apply(mad, raw=True)

    rng = np.random.default_rng(0)
    for n, holes in ((5000, 0), (5000, 200), (50, 0), (24, 0), (20, 0)):
        values = rng.normal(size=n)
        if holes:
            values[rng.choice(n, holes, replace=False)] = np.nan
        series = pd.Series(values)
        expected = per_window(series, windows.MAD_WINDOW)
        actual = returns._rolling_mad(series, windows.MAD_WINDOW)
        # NaN in the same places, and bit-identical where both are finite.
        assert expected.isna().equals(actual.isna())
        assert np.array_equal(expected.dropna().to_numpy(), actual.dropna().to_numpy())


def test_the_vectorised_mad_spans_more_than_one_chunk():
    # The |x - median| step materialises, so it runs in chunks; the seam between
    # two chunks must not drop or duplicate a window.
    rng = np.random.default_rng(1)
    series = pd.Series(rng.normal(size=3000))
    whole = returns._rolling_mad(series, windows.MAD_WINDOW)
    original = returns._MAD_CHUNK
    try:
        returns._MAD_CHUNK = 500
        chunked = returns._rolling_mad(series, windows.MAD_WINDOW)
    finally:
        returns._MAD_CHUNK = original
    assert whole.equals(chunked)


# --- the overnight gap, kept beside r ----------------------------------------

from tremor.corporate_actions import Dividends
from tremor.sessions import Session

# two_days() ends its first session on the 11:00 bar, so that day closes at
# 12:00 here - the calendar has to agree that the stored bar reached the close.
TABLE = {
    date(2021, 2, 26): Session(date(2021, 2, 26), "09:30", "16:00", False),
    date(2021, 3, 1): Session(date(2021, 3, 1), "09:30", "12:00", True),
    date(2021, 3, 2): Session(date(2021, 3, 2), "09:30", "16:00", False),
    date(2021, 3, 3): Session(date(2021, 3, 3), "09:30", "16:00", False),
}


def dividends(steps=None, splits=None, through="2021-12-31", ticker="SPY"):
    return Dividends(steps={ticker: steps or {}},
                     splits={ticker: frozenset(splits or ())},
                     checked_through={ticker: through} if through else {})


def test_the_gap_is_open_over_previous_close_on_the_first_bar_only():
    out = returns.split_channels(asset(), two_days(), session_table=TABLE, dividends=dividends())

    assert out.iloc[2]["gap"] == pytest.approx(math.log(105.0 / 101.0))
    # Nowhere else: not on an ordinary bar, and not on the first bar of the
    # record, which has no previous close.
    assert out["gap"].notna().sum() == 1
    # And r is exactly what it was without the gap - the first bar undisturbed.
    assert out.iloc[2]["r"] == pytest.approx(math.log(106.0 / 105.0))


def test_the_payout_comes_out_of_the_gap():
    # Day two is an ex-date paying 1% of the previous close. The table stores
    # d/(1-d), and ln(1 + step) is -ln(1 - d): the drop the payout causes.
    d = 0.01
    out = returns.split_channels(
        asset(), two_days(),
        session_table=TABLE, dividends=dividends(steps={"2021-03-02": d / (1 - d)}))

    assert out.iloc[2]["gap"] == pytest.approx(math.log(105.0 / 101.0) - math.log(1 - d))


def test_a_date_past_the_checked_through_date_is_not_scored():
    # A payout the table has not heard of yet reads as a gap the size of the
    # dividend. Not knowing is not the same as knowing there was none.
    out = returns.split_channels(asset(), two_days(),
                                 session_table=TABLE, dividends=dividends(through="2021-03-01"))
    assert out["gap"].isna().all()

    never = returns.split_channels(asset(), two_days(), session_table=TABLE, dividends=dividends(through=None))
    assert never["gap"].isna().all()


def test_a_split_is_not_a_gap():
    split_overnight = frame([
        (et(2021, 3, 1, 10), 100.0, 101.0, 99.5, 100.5, 1.0, 2),
        (et(2021, 3, 1, 11), 100.5, 101.5, 100.0, 101.0, 1.0, 2),
        (et(2021, 3, 2, 10), 50.6, 51.0, 50.0, 50.8, 1.0, 2),
    ])
    # Undeclared: a -69% print half the previous close is a provider that has
    # not adjusted yet, not the crash of the century.
    out = returns.split_channels(asset(), split_overnight, session_table=TABLE, dividends=dividends())
    assert out["gap"].isna().all()

    # Declared in the table: refused on that date whatever its size.
    declared = returns.split_channels(asset(), two_days(),
                                      session_table=TABLE, dividends=dividends(splits={"2021-03-02"}))
    assert declared["gap"].isna().all()


def test_no_dividend_table_means_no_gap_at_all():
    # The default for every caller that does not pass one - an unscored gap is
    # today's behaviour, not a wrong answer.
    out = returns.split_channels(asset(), two_days())
    assert "gap" in out and out["gap"].isna().all()


def test_a_missing_day_is_not_an_overnight_gap():
    # The store lost 2 March. The 3 March open against the 1 March close is two
    # days of market, not a night - the calendar says the previous session was
    # the 2nd, and the previous stored bar is not in it.
    hole = frame([
        (et(2021, 3, 1, 10), 100.0, 101.0, 99.0, 100.5, 1.0, 2),
        (et(2021, 3, 1, 11), 100.5, 102.0, 100.0, 101.0, 1.0, 2),
        (et(2021, 3, 3, 10), 108.0, 109.0, 107.0, 108.5, 1.0, 2),
    ])
    out = returns.split_channels(asset(), hole, session_table=TABLE,
                                 dividends=dividends())
    assert out["gap"].isna().all()


def test_a_session_that_lost_its_closing_bar_has_no_gap_after_it():
    # 2 March closes at 16:00 and the store stops at the 11:00 bar.
    lost_afternoon = frame([
        (et(2021, 3, 2, 10), 100.0, 101.0, 99.0, 100.5, 1.0, 2),
        (et(2021, 3, 2, 11), 100.5, 102.0, 100.0, 101.0, 1.0, 2),
        (et(2021, 3, 3, 10), 105.0, 106.0, 104.0, 106.0, 1.0, 2),
    ])
    out = returns.split_channels(asset(), lost_afternoon, session_table=TABLE,
                                 dividends=dividends())
    assert out["gap"].isna().all()


def test_no_calendar_means_no_fund_gap():
    out = returns.split_channels(asset(), two_days(), dividends=dividends())
    assert out["gap"].isna().all()


def fx_pair():
    return asset(ticker="EUR/USD", source="twelvedata", block="FX",
                 session_template="fx_continuous", fetch_interval="1h")


def weekend(friday_last_hour):
    # Friday afternoon, then the Sunday 17:00 New York open.
    return frame([
        (et(2021, 3, 5, 14), 1.2000, 1.2010, 1.1990, 1.2005, 0.0, 2),
        (et(2021, 3, 5, friday_last_hour), 1.2005, 1.2010, 1.1990, 1.2000, 0.0, 2),
        (et(2021, 3, 7, 17), 1.2120, 1.2130, 1.2110, 1.2125, 0.0, 2),
    ])


def test_a_currency_pair_has_a_weekend_gap_and_no_dividend_rule():
    # No corporate actions exist for a currency, so nothing needs checking.
    out = returns.split_channels(fx_pair(), weekend(16),
                                 dividends=dividends(ticker="SPY"))
    assert out.iloc[2]["is_session_open"]
    assert out.iloc[2]["gap"] == pytest.approx(math.log(1.2120 / 1.2000))
    assert out["gap"].notna().sum() == 1


def test_a_weekend_missing_its_friday_close_is_not_scored():
    # The store's last Friday bar is 14:00 - the week's close is not in it,
    # so this is not the weekend's gap. 2015-01-02 was a whole missing day.
    out = returns.split_channels(fx_pair(), frame([
        (et(2021, 3, 5, 13), 1.2000, 1.2010, 1.1990, 1.2000, 0.0, 2),
        (et(2021, 3, 7, 17), 1.2120, 1.2130, 1.2110, 1.2125, 0.0, 2),
    ]), dividends=dividends(ticker="SPY"))
    assert out["gap"].isna().all()


def test_crypto_has_no_gap():
    coin = asset(ticker="BTC-USD", source="coinbase", block="crypto",
                 session_template="crypto_24_7", fetch_interval="1h")
    out = returns.split_channels(coin, two_days(),
                                 dividends=dividends(ticker="BTC-USD"))
    assert out["gap"].isna().all()

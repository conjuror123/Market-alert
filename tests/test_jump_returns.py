import math
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from jump import bars, returns
from jump.basket import Asset

HOUR = 3600


def asset(**over):
    base = dict(ticker="SPY", source="twelvedata", block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="30min", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def frame(rows):
    return pd.DataFrame(rows, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)


def et(y, m, d, h):
    return int(datetime(y, m, d, h, tzinfo=ZoneInfo("America/New_York")).timestamp())


def two_days():
    # Two trading days of two hours: the first closes at 101, the second opens
    # at 105 on its first hour (the 09:30 open's).
    return frame([
        (et(2021, 3, 1, 10), 100.0, 101.0, 99.0, 100.5, 1.0, 2),
        (et(2021, 3, 1, 11), 100.5, 102.0, 100.0, 101.0, 1.0, 2),
        (et(2021, 3, 2, 9), 105.0, 106.0, 104.0, 106.0, 1.0, 2),
        (et(2021, 3, 2, 10), 106.0, 107.0, 105.0, 106.5, 1.0, 2),
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
    # happens between sessions, so it is excluded by construction: whatever the
    # price did overnight, r is measured from the session's own open.
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


from jump.corporate_actions import Dividends
from jump.sessions import Session

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


def test_a_pairs_weekend_opening_exactly_at_fridays_close_is_not_scored():
    # A stitched open (the majors' 2012, USD/INR's and USD/KRW's zeros): not a
    # gap of 0 but no measurement - scored, a run of them collapsed the
    # yardstick and the next real weekend read as hundreds of sigma.
    out = returns.split_channels(fx_pair(), frame([
        (et(2021, 3, 5, 14), 1.2000, 1.2010, 1.1990, 1.2005, 0.0, 2),
        (et(2021, 3, 5, 16), 1.2005, 1.2010, 1.1990, 1.2000, 0.0, 2),
        (et(2021, 3, 7, 17), 1.2000, 1.2130, 1.1990, 1.2125, 0.0, 2),
    ]), dividends=dividends(ticker="SPY"))
    assert out["gap"].isna().all()


def test_crypto_has_no_gap():
    coin = asset(ticker="BTC-USD", source="coinbase", block="crypto",
                 session_template="crypto_24_7", fetch_interval="1h")
    out = returns.split_channels(coin, two_days(),
                                 dividends=dividends(ticker="BTC-USD"))
    assert out["gap"].isna().all()


# --- missing hours ---------------------------------------------------------------

def test_a_missing_hour_is_skipped_as_if_it_were_never_there():
    # 11:00 and 12:00 are missing. The 13:00 bar is its own hour, open to close;
    # the move across the hole, 101 to 104, is kept aside and never scored.
    data = frame([
        (et(2021, 3, 2, 10), 100.0, 101.0, 99.0, 101.0, 1.0, 2),
        (et(2021, 3, 2, 13), 104.0, 105.0, 103.0, 105.0, 1.0, 2),
        (et(2021, 3, 2, 14), 105.0, 106.0, 104.0, 106.0, 1.0, 2),
    ])
    out = returns.split_channels(asset(), data)

    assert not out.iloc[1]["is_session_open"]
    assert out.iloc[1]["r"] == pytest.approx(math.log(105.0 / 104.0))
    assert out.iloc[1]["hole"] == pytest.approx(math.log(104.0 / 101.0))
    # The hour after it is close to close as usual, and has no hole.
    assert out.iloc[2]["r"] == pytest.approx(math.log(106.0 / 105.0))
    assert np.isnan(out.iloc[2]["hole"])


def test_a_night_whose_first_hours_are_missing_is_not_scored():
    # F2. Day two's 09:00 and 10:00 bars are missing: from the close to the
    # 11:00 bar's open is the night and two hours of trading, not a gap (LE=F
    # 2025-10-29 was flagged so). The hour itself is still its own move.
    data = frame([
        (et(2021, 3, 1, 10), 100.0, 101.0, 99.0, 100.5, 1.0, 2),
        (et(2021, 3, 1, 11), 100.5, 102.0, 100.0, 101.0, 1.0, 2),
        (et(2021, 3, 2, 11), 106.0, 107.0, 105.0, 106.5, 1.0, 2),
    ])
    out = returns.split_channels(asset(), data, session_table=TABLE, dividends=dividends())

    assert out.iloc[2]["is_session_open"]
    assert math.isnan(out.iloc[2]["gap"])
    assert out.iloc[2]["r"] == pytest.approx(math.log(106.5 / 106.0))


def utc(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def test_christmas_is_a_closure_for_a_currency_pair():
    # The store stops on 24 December at 17:00 UTC and resumes on the 26th: the
    # reopening is a gap from the last close, not a missing stretch.
    data = frame([
        (utc(2019, 12, 24, 16), 1.100, 1.11, 1.09, 1.100, 0.0, 1),
        (utc(2019, 12, 24, 17), 1.100, 1.11, 1.09, 1.101, 0.0, 1),
        (utc(2019, 12, 26, 4), 1.110, 1.12, 1.10, 1.112, 0.0, 1),
    ])
    out = returns.split_channels(fx_pair(), data, dividends=dividends(ticker="EUR/USD"))

    assert out.iloc[2]["is_session_open"]
    assert out.iloc[2]["gap"] == pytest.approx(math.log(1.110 / 1.101))
    assert out.iloc[2]["r"] == pytest.approx(math.log(1.112 / 1.110))
    assert np.isnan(out.iloc[2]["hole"])


def test_an_ordinary_midweek_hole_is_not_a_closure_for_a_currency_pair():
    data = frame([
        (utc(2019, 12, 10, 16), 1.100, 1.11, 1.09, 1.100, 0.0, 1),
        (utc(2019, 12, 10, 19), 1.105, 1.11, 1.10, 1.106, 0.0, 1),
    ])
    out = returns.split_channels(fx_pair(), data, dividends=dividends(ticker="EUR/USD"))

    assert not out.iloc[1]["is_session_open"]
    assert np.isnan(out.iloc[1]["gap"])
    assert out.iloc[1]["hole"] == pytest.approx(math.log(1.105 / 1.100))


# --- nickel: the LME's day ----------------------------------------------------

def nickel():
    return asset(ticker="NID", source="sina", block="industrial_metals",
                 has_volume=False, tick_size=5.0, session_template="lme",
                 fetch_interval="1h")


def ldn(y, m, d, h):
    return int(datetime(y, m, d, h, tzinfo=ZoneInfo("Europe/London")).timestamp())


def test_nickel_has_a_night_from_the_last_lme_hour_to_the_first():
    out = returns.split_channels(nickel(), frame([
        (ldn(2026, 9, 29, 17), 15000, 15020, 14990, 15010, 0.0, 12),
        (ldn(2026, 9, 29, 18), 15010, 15030, 15000, 15020, 0.0, 12),
        (ldn(2026, 9, 30, 1), 15200, 15210, 15190, 15205, 0.0, 12),
        (ldn(2026, 9, 30, 2), 15205, 15215, 15195, 15210, 0.0, 12),
    ]), dividends=dividends(ticker="SPY"))
    assert list(out["is_session_open"]) == [True, False, True, False]
    assert out.iloc[2]["gap"] == pytest.approx(math.log(15200 / 15020))
    assert out.iloc[2]["r"] == pytest.approx(math.log(15205 / 15200))
    assert out["gap"].notna().sum() == 1


def test_a_nickel_night_that_lost_its_evening_is_not_scored():
    out = returns.split_channels(nickel(), frame([
        (ldn(2026, 9, 29, 15), 15000, 15020, 14990, 15010, 0.0, 12),
        (ldn(2026, 9, 30, 1), 15200, 15210, 15190, 15205, 0.0, 12),
    ]), dividends=dividends(ticker="SPY"))
    assert out["gap"].isna().all()


def test_a_nickel_close_longer_than_any_holiday_is_an_outage_not_a_gap():
    # Kitco's quote stood still for eight days from 2023-12-18.
    out = returns.split_channels(nickel(), frame([
        (ldn(2023, 12, 15, 18), 15000, 15020, 14990, 15010, 0.0, 12),
        (ldn(2023, 12, 27, 1), 16200, 16210, 16190, 16205, 0.0, 12),
    ]), dividends=dividends(ticker="SPY"))
    assert out["gap"].isna().all()


def test_lme_hours_are_one_to_nineteen_london_on_weekdays():
    from jump import quality
    hours = pd.Series([ldn(2026, 9, 30, 0), ldn(2026, 9, 30, 1), ldn(2026, 9, 30, 18),
                       ldn(2026, 9, 30, 19), ldn(2026, 10, 3, 12)])
    assert list(quality.in_session(nickel(), hours)) == [False, True, True, False, False]


# --- the official open (jump.opens) ------------------------------------------

def stale_morning():
    # Day two's first print is the previous close, 101, and the bar then trades
    # at 98.5-99.5: a stale print. The market opened 1.8% lower.
    return frame([
        (et(2021, 3, 1, 10), 100.0, 101.0, 99.0, 100.5, 1.0, 2),
        (et(2021, 3, 1, 11), 100.5, 102.0, 100.0, 101.0, 1.0, 2),
        (et(2021, 3, 2, 9), 101.0, 101.0, 98.5, 99.5, 1.0, 2),
        (et(2021, 3, 2, 10), 99.5, 100.0, 99.0, 99.8, 1.0, 2),
    ])


def with_opens(opens, **kw):
    d = dividends(**kw)
    return Dividends(steps=d.steps, splits=d.splits, checked_through=d.checked_through,
                     opens={"SPY": opens})


def test_the_night_ends_at_the_official_open_not_at_a_stale_first_print():
    official = 0.982                         # official open over the official close
    out = returns.split_channels(asset(), stale_morning(), session_table=TABLE,
                                 dividends=with_opens({"2021-03-02": official}))
    opening = out.iloc[2]
    assert opening["official_open"]
    assert opening["gap"] == pytest.approx(math.log(official))
    assert opening["r"] == pytest.approx(math.log(99.5 / (101.0 * official)))
    # The close-to-close move is the store's, only split differently.
    assert opening["gap"] + opening["r"] == pytest.approx(math.log(99.5 / 101.0))
    # Untouched elsewhere.
    assert out.iloc[3]["r"] == pytest.approx(math.log(99.8 / 99.5))


def test_a_first_print_off_the_official_open_is_replaced_too():
    # An IEX first print 1% above where the market opened (+2.97%, not +3.96%).
    official = 104.0 / 101.0
    out = returns.split_channels(asset(), two_days(), session_table=TABLE,
                                 dividends=with_opens({"2021-03-02": official}))
    assert out.iloc[2]["gap"] == pytest.approx(math.log(104.0 / 101.0))
    assert out.iloc[2]["r"] == pytest.approx(math.log(106.0 / 104.0))


def test_no_official_open_no_night():
    # The fund has official opens, but not for this day: its first print may
    # be a stale one, so the gap is not scored. The hour keeps its stored open.
    out = returns.split_channels(asset(), stale_morning(), session_table=TABLE,
                                 dividends=with_opens({"2021-03-03": 1.0}))
    assert out["gap"].isna().all()
    assert not out.iloc[2]["official_open"]
    assert out.iloc[2]["r"] == pytest.approx(math.log(99.5 / 101.0))


def test_an_unadjusted_split_is_not_spliced_into_the_first_hour():
    # The provider has not adjusted a 2:1 split: the store's close-to-close is
    # the split ratio. Spliced, the official (adjusted) open would put -69%
    # into the first hour; left alone, the gap is refused as a split, as before.
    split_overnight = frame([
        (et(2021, 3, 1, 10), 100.0, 101.0, 99.5, 100.5, 1.0, 2),
        (et(2021, 3, 1, 11), 100.5, 101.5, 100.0, 101.0, 1.0, 2),
        (et(2021, 3, 2, 9), 50.6, 51.0, 50.0, 50.8, 1.0, 2),
    ])
    out = returns.split_channels(asset(), split_overnight, session_table=TABLE,
                                 dividends=with_opens({"2021-03-02": 1.002}))
    assert not out.iloc[2]["official_open"]
    assert out.iloc[2]["r"] == pytest.approx(math.log(50.8 / 50.6))
    assert out["gap"].isna().all()

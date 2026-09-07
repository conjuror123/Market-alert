from datetime import date, datetime, timezone

import pandas as pd
import pytest

from tremor import bars, quality, sessions
from tremor.basket import Asset

HOUR = 3600


def asset(**over):
    base = dict(ticker="SPY", source="twelvedata", tier=1, block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="30min", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def frame(rows):
    return pd.DataFrame(rows, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)


def hour_at(y, m, d, h, tz="UTC"):
    from zoneinfo import ZoneInfo
    return int(datetime(y, m, d, h, tzinfo=ZoneInfo(tz)).timestamp())


TABLE = {
    date(2021, 11, 26): sessions.Session(date(2021, 11, 26), "09:30", "13:00", True),
    date(2021, 11, 29): sessions.Session(date(2021, 11, 29), "09:30", "16:00", False),
}


def test_post_close_hours_of_a_half_session_are_not_in_session():
    # On 26 November 2021 the exchange closed at 13:00, yet the source served bars
    # for 13:00, 14:00 and 15:00. Without the filter they would enter the EWMA and
    # the volume profile.
    hours = pd.Series([hour_at(2021, 11, 26, h, "America/New_York")
                       for h in (11, 12, 13, 14, 15)])
    flags = list(quality.in_session(asset(), hours, TABLE))
    assert flags == [True, True, False, False, False]


def test_the_hour_containing_the_close_is_kept():
    # The 12:00-13:00 hour ends exactly at the close: the closing auction prints
    # inside it, and losing that is not an option.
    hours = pd.Series([hour_at(2021, 11, 26, 12, "America/New_York")])
    assert bool(quality.in_session(asset(), hours, TABLE).iloc[0])


def test_a_full_session_day_has_seven_hourly_bars():
    hours = pd.Series([hour_at(2021, 11, 29, h, "America/New_York") for h in range(8, 18)])
    assert int(quality.in_session(asset(), hours, TABLE).sum()) == 7


def test_a_holiday_has_no_session_hours():
    hours = pd.Series([hour_at(2021, 11, 25, h, "America/New_York") for h in range(9, 16)])
    assert not quality.in_session(asset(), hours, TABLE).any()


def test_crypto_is_always_in_session():
    hours = pd.Series([hour_at(2026, 8, 30, h) for h in range(0, 24, 6)])  # Sunday
    crypto = asset(ticker="BTC-USD", source="coinbase", block="crypto",
                   session_template="crypto_24_7", fetch_interval="1h")
    assert quality.in_session(crypto, hours).all()


def test_forex_stops_for_the_weekend():
    fx = asset(ticker="EUR/USD", block="FX", has_volume=False, tick_size=0.00001,
               session_template="fx_continuous", fetch_interval="1h")
    weekday = pd.Series([hour_at(2026, 8, 26, 12)])
    saturday = pd.Series([hour_at(2026, 8, 29, 12)])

    assert bool(quality.in_session(fx, weekday).iloc[0])
    assert not bool(quality.in_session(fx, saturday).iloc[0])


def test_unknown_session_template_is_rejected():
    with pytest.raises(ValueError, match="session template"):
        quality.in_session(asset(session_template="made-up"), pd.Series([0]))


def test_invalid_reasons_name_the_defect():
    bad = frame([
        (HOUR, 1.0, 2.0, 0.5, 1.5, 10.0, 2),      # sound
        (2 * HOUR, 0.0, 2.0, 0.5, 1.5, 10.0, 2),  # price not positive
        (3 * HOUR, 1.0, 2.0, 0.5, 9.9, 10.0, 2),  # close above high
        (4 * HOUR, 1.0, 2.0, 0.5, 1.5, -1.0, 2),  # volume negative
    ])
    reasons = list(quality.invalid_reasons(asset(), bad))
    assert reasons[0] == ""
    assert "positive" in reasons[1]
    assert "OHLC" in reasons[2]
    assert "volume" in reasons[3]


def test_ohlc_tolerance_matches_the_audit():
    # The same half-tick tolerance as in the coverage table: a discrepancy under a
    # tick is vendor rounding, not a broken bar.
    almost = frame([(HOUR, 92.15, 92.415, 92.14, 92.42, 1.0, 2)])
    assert quality.invalid_reasons(asset(), almost).iloc[0] == ""


def test_gate_marks_usable_bars_only():
    hours = [hour_at(2021, 11, 26, h, "America/New_York") for h in (11, 14)]
    data = frame([(hours[0], 1.0, 2.0, 0.5, 1.5, 10.0, 2),
                  (hours[1], 1.0, 2.0, 0.5, 1.5, 0.0, 2)])
    gated = quality.apply_gate(asset(), data, TABLE)

    assert list(gated["is_usable"]) == [True, False]


def test_expected_hours_are_the_session_hours():
    first = hour_at(2021, 11, 26, 0, "America/New_York")
    last = hour_at(2021, 11, 26, 23, "America/New_York")
    assert len(quality.expected_hours(asset(), first, last, TABLE)) == 4

from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from meals import bars, volume
from meals.basket import Asset

ET = "America/New_York"


def asset(**over):
    base = dict(ticker="SPY", source="twelvedata", tier=1, block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="30min", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def bar(day: date, hour: int, vol: float):
    moment = datetime(day.year, day.month, day.day, hour, tzinfo=ZoneInfo(ET))
    return (int(moment.timestamp()), 1.0, 1.0, 1.0, 1.0, vol, 2)


def frame(rows):
    return pd.DataFrame(rows, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)


def days(n, start=date(2021, 3, 1)):
    from datetime import timedelta
    return [start + timedelta(days=i) for i in range(n)]


def test_forex_gets_null_not_zero():
    # §3.5: for an instrument without volume, volume confirmation is NOT
    # ASSESSED. A zero would mean "volume is ordinary", an assertion about data
    # that does not exist.
    fx = asset(ticker="EUR/USD", block="FX", has_volume=False, tick_size=0.00001,
               session_template="fx_continuous", fetch_interval="1h")
    data = frame([bar(d, 10, 0.0) for d in days(30)])

    assert volume.robust_volume_z(fx, data, ET).isna().all()


def test_seasonal_hour_is_not_an_anomaly_by_itself():
    # The open is several times busier than midday. Compared against one global
    # norm, every morning would be a spike. The norm is taken per hour.
    rows = []
    for d in days(40):
        rows.append(bar(d, 10, 10_000_000.0))  # the noisy opening hour
        rows.append(bar(d, 13, 1_000_000.0))   # quiet midday
    data = frame(rows)

    result = volume.robust_volume_z(asset(), data, ET)
    measured = result.dropna()

    assert len(measured) > 0
    # Both hours are ordinary for themselves, so both come out near zero.
    assert measured.abs().max() < 1.0


def test_a_genuine_surge_is_flagged():
    rows = [bar(d, 10, 1_000_000.0 * (1 + 0.01 * i))
            for i, d in enumerate(days(30))]
    rows.append(bar(date(2021, 4, 15), 10, 20_000_000.0))
    result = volume.robust_volume_z(asset(), frame(rows), ET)

    assert result.iloc[-1] > 2.5


def test_profile_excludes_the_current_day():
    # A norm that includes the observation being judged adjusts towards it and
    # understates its own firing.
    rows = [bar(d, 10, 1_000_000.0) for d in days(25)]
    rows.append(bar(date(2021, 4, 15), 10, 50_000_000.0))
    result = volume.robust_volume_z(asset(), frame(rows), ET)

    # The profile is degenerate (volume never moved), so §3.5 gives V_R = 0
    # rather than infinity - but the point is that the spike did not enter its own
    # profile.
    assert result.iloc[-1] == 0.0


def test_degenerate_profile_gives_zero():
    # This hour's volume has not moved for 20 days: MAD is zero, division is impossible.
    rows = [bar(d, 10, 1_000_000.0) for d in days(30)]
    result = volume.robust_volume_z(asset(), frame(rows), ET).dropna()

    assert (result == 0.0).all()


def test_half_sessions_are_kept_out_of_the_profile():
    # On a shortened day volume is lower by construction, and keeping such days
    # in the norm depresses it for every full one.
    #
    # The effect has to be built deliberately large, and that says something
    # about the estimator itself: the median and MAD are so robust that one short
    # day in twenty does not move them at all. For a difference to show, the band
    # of shortened days must occupy half the window.
    all_days = days(40)
    short_days = set(all_days[20:30])
    rows = [bar(d, 10, 200_000.0 if d in short_days else 1_000_000.0 + 10_000 * i)
            for i, d in enumerate(all_days)]
    data = frame(rows)

    with_filter = volume.robust_volume_z(asset(), data, ET, set(all_days) - short_days)
    without_filter = volume.robust_volume_z(asset(), data, ET, None)

    # Shortened days in the profile spoil the norm not by shifting its centre but
    # by inflating its spread: the distribution becomes bimodal - around 200
    # thousand and around a million - and MAD stretches across the whole gap
    # between the humps. The denominator inflates, and a genuine spike stops
    # standing out. So the filter guards not against false firings but against
    # blindness.
    assert with_filter.iloc[-1] > 1.5 * without_filter.iloc[-1]


def test_short_history_leaves_the_value_undefined():
    rows = [bar(d, 10, 1_000_000.0) for d in days(5)]
    assert volume.robust_volume_z(asset(), frame(rows), ET).isna().all()


def test_local_hour_not_utc_hour():
    # The seasonality is tied to the trading schedule, and that lives in local
    # time and shifts relative to UTC with daylight saving. The same exchange hour
    # in winter and in summer is a different UTC hour.
    winter = datetime(2021, 1, 15, 10, tzinfo=ZoneInfo(ET))
    summer = datetime(2021, 7, 15, 10, tzinfo=ZoneInfo(ET))
    assert winter.astimezone(ZoneInfo("UTC")).hour != summer.astimezone(ZoneInfo("UTC")).hour

    rows = ([bar(d, 10, 1_000_000.0 + 1000 * i) for i, d in enumerate(days(25, date(2021, 1, 4)))]
            + [bar(d, 10, 1_000_000.0 + 1000 * i) for i, d in enumerate(days(25, date(2021, 7, 5)))])
    result = volume.robust_volume_z(asset(), frame(rows), ET).dropna()

    # Every bar landed in the same hour-10 profile despite the UTC hour changing.
    assert len(result) >= 25


def test_empty_input():
    assert volume.robust_volume_z(asset(), bars.empty_frame(), ET).empty


def test_full_session_days_drops_early_closes():
    from meals import sessions

    table = {
        date(2021, 11, 26): sessions.Session(date(2021, 11, 26), "09:30", "13:00", True),
        date(2021, 11, 29): sessions.Session(date(2021, 11, 29), "09:30", "16:00", False),
    }
    assert volume.full_session_days(table) == {date(2021, 11, 29)}

from datetime import date, datetime, timezone

import pytest

from meals import sessions

ANCHOR = "America/New_York"


def utc(y, m, d, h=0):
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp())


def test_loads_the_generated_table_without_the_calendar_library():
    # The hourly run reads the CSV and must not depend on exchange_calendars.
    table = sessions.load_sessions()
    assert len(table) > 1900
    assert table[date(2021, 1, 4)].local_open == "09:30"
    assert table[date(2021, 1, 4)].local_close == "16:00"


def test_missing_table_says_how_to_build_it(tmp_path):
    with pytest.raises(FileNotFoundError, match="python -m meals.sessions"):
        sessions.load_sessions(str(tmp_path / "missing.csv"))


def test_half_sessions_are_marked():
    table = sessions.load_sessions()
    # The Friday after Thanksgiving - an early close at 13:00.
    day = table[date(2021, 11, 26)]
    assert day.is_early_close
    assert day.local_close == "13:00"
    assert len(sessions.half_sessions(table)) == 15


def test_holiday_is_a_weekday_absent_from_the_table():
    table = sessions.load_sessions()
    # Christmas 2023 falls on a Monday, the exchange is closed.
    assert sessions.is_holiday(date(2023, 12, 25), table)
    # An ordinary Tuesday is not a holiday.
    assert not sessions.is_holiday(date(2023, 12, 26), table)
    # Weekends do not count as holidays: that is the ordinary close of the week.
    assert not sessions.is_holiday(date(2023, 12, 23), table)


def test_reference_week_is_exactly_120_hours():
    # §2.2: the reference-calendar week is exactly 120 hours long, and holidays
    # are not subtracted from it.
    opened, closed = sessions.reference_week_bounds(
        datetime(2026, 8, 26, 12, tzinfo=timezone.utc), ANCHOR)
    assert (closed - opened) / 3600 == sessions.REFERENCE_WEEK_HOURS


def test_reference_week_bounds_shift_with_daylight_saving():
    # The bounds are given in the exchange's local time, so in UTC they differ
    # between summer and winter: 21:00 and 22:00. §2.2 forbids storing them as UTC
    # - daylight saving would otherwise shift the week relative to the market.
    summer, _ = sessions.reference_week_bounds(
        datetime(2026, 7, 15, 12, tzinfo=timezone.utc), ANCHOR)
    winter, _ = sessions.reference_week_bounds(
        datetime(2026, 1, 15, 12, tzinfo=timezone.utc), ANCHOR)

    assert datetime.fromtimestamp(summer, tz=timezone.utc).hour == 21
    assert datetime.fromtimestamp(winter, tz=timezone.utc).hour == 22


def test_hours_inside_and_outside_the_reference_week():
    # Midday Wednesday is inside; Saturday is outside.
    assert sessions.is_reference_hour(utc(2026, 8, 26, 12), ANCHOR)
    assert not sessions.is_reference_hour(utc(2026, 8, 29, 12), ANCHOR)


def test_moment_before_sunday_open_belongs_to_the_previous_week():
    # Sunday 20:00 UTC in summer is 16:00 in New York, an hour before the week
    # opens. That hour must belong to the previous week, not the coming one.
    sunday_before_open = utc(2026, 8, 30, 20)
    opened, closed = sessions.reference_week_bounds(
        datetime.fromtimestamp(sunday_before_open, tz=timezone.utc), ANCHOR)

    assert opened < sunday_before_open
    assert closed < sunday_before_open + 3600 * 24
    assert not sessions.is_reference_hour(sunday_before_open, ANCHOR)


def test_reference_week_covers_the_holiday_hours_too():
    # 4 July 2026 is a Saturday, so take Christmas 2026 (a Friday, exchange
    # closed). Holiday hours still belong to the reference week.
    assert sessions.is_reference_hour(utc(2026, 12, 25, 15), ANCHOR)


def test_table_matches_the_days_the_data_actually_has():
    # The check that confirmed the choice of library: the schedule must match the
    # actual bars day for day. A discrepancy means either an error in the calendar
    # or a hole in the data - both need noticing before quorum and the
    # cross-section start being computed on top of it.
    import pandas as pd

    from meals import bars

    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, "twelvedata_SPY"))
    observed = set(pd.to_datetime(frame["hour_utc"], unit="s", utc=True).dt.date)
    table = sessions.load_sessions()
    scheduled = {d for d in table if min(observed) <= d <= max(observed)}

    assert observed - scheduled == set()
    assert scheduled - observed == set()

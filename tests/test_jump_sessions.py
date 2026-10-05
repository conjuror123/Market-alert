from datetime import date, datetime, timedelta, timezone

import pytest

from jump import sessions

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
    with pytest.raises(FileNotFoundError, match="python -m jump.sessions"):
        sessions.load_sessions(str(tmp_path / "missing.csv"))


def test_half_sessions_are_marked():
    table = sessions.load_sessions()
    # The Friday after Thanksgiving - an early close at 13:00.
    day = table[date(2021, 11, 26)]
    assert day.is_early_close
    assert day.local_close == "13:00"
    # Counted rather than fixed: the table's span is a configuration choice - it
    # now reaches back to 2015 - and a literal here would fail every time the
    # history is deepened without anything being wrong.
    early = [s for s in table.values() if s.is_early_close]
    assert all(s.is_early_close and s.local_close == "13:00" for s in early)
    # Roughly two or three a year over the table's span, never none.
    years = len({day.year for day in table})
    assert years <= len(early) <= 4 * years


def test_a_holiday_is_a_weekday_absent_from_the_table():
    table = sessions.load_sessions()
    # Christmas 2023 falls on a Monday, the exchange is closed.
    assert date(2023, 12, 25) not in table
    assert date(2023, 12, 26) in table


def test_reference_week_is_exactly_120_hours():
    # The reference-calendar week is exactly 120 hours long, and holidays
    # are not subtracted from it.
    opened, closed = sessions.reference_week_bounds(
        datetime(2026, 8, 26, 12, tzinfo=timezone.utc), ANCHOR)
    assert (closed - opened) / 3600 == 120


def test_reference_week_bounds_shift_with_daylight_saving():
    # The bounds are given in the exchange's local time, so in UTC they differ
    # between summer and winter: 21:00 and 22:00. Storing them as UTC is forbidden
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


# The US-equity instruments, whose sessions the NYSE table describes. The FX
# pairs and crypto trade on their own calendars and are not judged against it.
CALENDAR_INSTRUMENTS = ("twelvedata_SPY", "twelvedata_QQQ", "twelvedata_IWM",
                        "twelvedata_XLF", "twelvedata_TLT", "twelvedata_IEF",
                        "twelvedata_SHY", "twelvedata_HYG", "twelvedata_GLD",
                        "twelvedata_SLV", "twelvedata_USO", "twelvedata_DBC")

# How much of an instrument's history may be missing before it stops being a
# handful of provider holes and starts being a broken archive. The worst
# instrument sits at 3 sessions in about 1640 - 0.18% - so this leaves room for
# a few more to turn up without leaving room for a real failure to hide.
MAX_MISSING_FRACTION = 0.005

# Nothing recent may be missing, whatever the archive looks like further back.
# A hole in the last quarter is not a provider's old gap, it is the live
# collection failing now, and that has to fail loudly.
RECENT_DAYS = 90


def _missing_sessions(stem):
    import pandas as pd

    from jump import bars

    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, stem))
    observed = set(pd.to_datetime(frame["hour_utc"], unit="s", utc=True).dt.date)
    table = sessions.load_sessions()
    scheduled = {d for d in table if min(observed) <= d <= max(observed)}
    return observed, scheduled


def test_the_data_never_has_a_day_the_calendar_does_not():
    # This direction stays absolute. A bar on a day the exchange was shut means
    # the calendar is wrong or the bars are misdated, and either would poison
    # every gap and every skip that reads the calendar.
    for stem in CALENDAR_INSTRUMENTS:
        observed, scheduled = _missing_sessions(stem)
        assert observed - scheduled == set(), f"{stem} has bars outside the calendar"


def test_the_missing_sessions_stay_a_handful_and_none_are_recent():
    # Not absolute: 2020-02-18 is absent from all twelve instruments, plus a
    # few scattered days per symbol.
    # Every missing day was re-fetched
    # individually from Twelve Data (jump.backfill --fill-gaps, run 33994110137)
    # and the answer was 0 recovered, 21 confirmed missing at the source. So the
    # holes are the provider's and no amount of asking will close them.
    #
    # What is still worth failing on is a hole that is NOT one of those: too
    # many, which means the archive is broken rather than pitted, or a recent
    # one, which means the live collection is failing now.
    import datetime as dt

    today = dt.date.today()
    for stem in CALENDAR_INSTRUMENTS:
        observed, scheduled = _missing_sessions(stem)
        missing = scheduled - observed
        fraction = len(missing) / max(len(scheduled), 1)
        assert fraction <= MAX_MISSING_FRACTION, (
            f"{stem}: {len(missing)} of {len(scheduled)} sessions missing "
            f"({fraction:.2%}) - too many to be provider holes: "
            f"{sorted(str(d) for d in missing)[:10]}")

        recent = [d for d in missing if (today - d).days <= RECENT_DAYS]
        assert not recent, (
            f"{stem} is missing recent sessions {sorted(str(d) for d in recent)} - "
            f"that is the live collection failing, not an old provider hole")


def test_an_ordinary_session_is_seven_hourly_bars():
    from jump.sessions import Session, session_hours
    import pandas as pd

    s = Session(day=date(2019, 6, 10), local_open="09:30",
                local_close="16:00", is_early_close=False)
    hours = session_hours(s)
    local = pd.to_datetime(hours, unit="s", utc=True).tz_convert(
        "America/New_York").strftime("%H:%M").tolist()
    assert local == ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00",
                     "15:00"]


def test_the_close_hour_gets_no_bar_of_its_own():
    # The 15:30 half-hourly bar covers 15:30-16:00 and folds into 15:00, so
    # demanding a 16:00 bar would mark every ordinary day incomplete.
    from jump.sessions import Session, session_hours
    import pandas as pd

    s = Session(day=date(2019, 6, 10), local_open="09:30",
                local_close="16:00", is_early_close=False)
    local = pd.to_datetime(session_hours(s), unit="s", utc=True).tz_convert(
        "America/New_York").hour.tolist()
    assert 16 not in local


def test_a_half_session_is_four():
    from jump.sessions import Session, session_hours

    s = Session(day=date(2020, 11, 27), local_open="09:30",
                local_close="13:00", is_early_close=True)
    assert len(session_hours(s)) == 4


def test_the_stamps_follow_daylight_saving_not_a_fixed_offset():
    from jump.sessions import Session, session_hours

    winter = session_hours(Session(day=date(2021, 1, 4), local_open="09:30",
                                   local_close="16:00", is_early_close=False))
    summer = session_hours(Session(day=date(2021, 7, 6), local_open="09:30",
                                   local_close="16:00", is_early_close=False))
    assert (winter[0] % 86400) // 3600 == 14   # 09:00 EST
    assert (summer[0] % 86400) // 3600 == 13   # 09:00 EDT


def test_expected_hours_is_bounded_by_the_days_asked_for():
    from jump.sessions import Session, expected_hours

    table = {d: Session(day=d, local_open="09:30", local_close="16:00",
                        is_early_close=False)
             for d in (date(2021, 1, 4), date(2021, 1, 5), date(2021, 1, 6))}
    assert len(expected_hours(table, date(2021, 1, 5), date(2021, 1, 6))) == 14


# --- walking forward in the instrument's own bars ---------------------------

def _table(days):
    from jump.sessions import Session

    return {d: Session(day=d, local_open="09:30", local_close="16:00",
                       is_early_close=False) for d in days}


# --- the table extends itself, append-only ------------------------------------

def _table_until(path, last):
    from jump.sessions import Session, write_sessions
    days = [last - timedelta(days=k) for k in range(10) if (last - timedelta(days=k)).weekday() < 5]
    # A past row the library would describe differently today: it must survive.
    odd = Session(day=days[-1], local_open="09:30", local_close="13:00", is_early_close=True)
    rows = [Session(day=d, local_open="09:30", local_close="16:00", is_early_close=False)
            for d in days[:-1]] + [odd]
    write_sessions(str(path), rows)
    return odd


def _fake_generate(start, end):
    from jump.sessions import Session
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(Session(day=d, local_open="09:30", local_close="16:00",
                               is_early_close=False))
        d += timedelta(days=1)
    return out


def test_a_short_table_is_extended_three_years_and_its_rows_kept(tmp_path):
    from jump.sessions import extend_sessions, load_sessions
    path = tmp_path / "nyse.csv"
    odd = _table_until(path, date(2027, 12, 31))
    before = load_sessions(str(path))
    added = extend_sessions(str(path), today=date(2026, 10, 4), generate=_fake_generate)
    after = load_sessions(str(path))
    assert added > 0 and max(after) == date(2029, 12, 31)
    assert all(after[d] == s for d, s in before.items())    # nothing already there moved
    assert after[odd.day].is_early_close


def test_a_table_that_reaches_far_enough_is_left_alone(tmp_path):
    from jump.sessions import extend_sessions, needs_extension
    path = tmp_path / "nyse.csv"
    _table_until(path, date(2029, 6, 30))
    assert needs_extension(str(path), today=date(2026, 10, 4)) is False
    def never(*a):
        raise AssertionError("the calendar library was asked for nothing")
    assert extend_sessions(str(path), today=date(2026, 10, 4), generate=never) == 0


def test_the_extension_with_the_real_calendar_library(tmp_path):
    pytest.importorskip("exchange_calendars")
    from jump.sessions import extend_sessions, load_sessions
    path = tmp_path / "nyse.csv"
    _table_until(path, date(2028, 12, 29))
    extend_sessions(str(path), today=date(2027, 6, 1))
    table = load_sessions(str(path))
    assert date(2029, 1, 1) not in table                  # New Year's Day
    assert date(2029, 1, 2) in table and max(table) == date(2030, 12, 31)
    assert table[date(2029, 11, 23)].is_early_close        # the day after Thanksgiving

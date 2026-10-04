"""Session calendar and the basket's reference calendar.

The NYSE schedule comes from the exchange_calendars library, but NOT on every
run: the library acts as a generator, and the result of its work lives in the
repository as a table committed alongside the code. There are three reasons.

1. Reproducibility. A test must yield an identical set of events on a
   repeat run of the same period under the same config_version. If the schedule
   is computed by the library at launch time, an update to it can move historical
   sessions - and a past backtest stops reproducing, although not a single
   configuration parameter was touched.
2. The export schema explicitly requires holidays and half_sessions tables - that
   is, data, not a function call.
3. The hourly run then needs no calendar library at all, only a ready CSV. Fewer
   dependencies in the hourly run, faster installs.

There are deliberately no separate holidays and half_sessions tables in the
store: both are derived from the session table without loss - a business day that
is absent is a holiday, and a row with is_early_close is a half session. Storing
one and the same fact twice means getting two diverging answers sooner or later.

Verified against data: over 2021-01-04 .. 2026-08-28 the schedule matches SPY's
actual bars day for day - 1420 trading days on both sides, no discrepancies in
either direction.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

HOUR = 3600
DEFAULT_SESSIONS_PATH = os.path.join("data", "tremor", "sessions", "nyse.csv")

REFERENCE_OPEN_HOUR = 17   # Sunday, anchor exchange local time
REFERENCE_CLOSE_HOUR = 17  # Friday


@dataclass(frozen=True)
class Session:
    day: date
    local_open: str    # "HH:MM" in the exchange's timezone
    local_close: str
    is_early_close: bool


def generate_nyse_sessions(start: date, end: date) -> list[Session]:
    """Builds the NYSE schedule with the exchange_calendars library.

    Called by hand only, when the table is refreshed (see main). It is not used in
    the hourly run, so exchange_calendars stays a development dependency rather
    than a runtime one.
    """
    import exchange_calendars as xcals  # local import: generation only

    calendar = xcals.get_calendar("XNYS", start=str(start), end=str(end))
    schedule = calendar.schedule
    tz = "America/New_York"
    sessions = []
    for day, row in schedule.iterrows():
        opened = row["open"].tz_convert(tz)
        closed = row["close"].tz_convert(tz)
        sessions.append(Session(
            day=day.date(),
            local_open=opened.strftime("%H:%M"),
            local_close=closed.strftime("%H:%M"),
            is_early_close=(closed.hour, closed.minute) < (16, 0),
        ))
    return sessions


def write_sessions(path: str, sessions: list[Session]) -> None:
    from tremor import atomic

    def _write(tmp: str) -> None:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            # \n rather than the default \r\n: the file lives in the repository,
            # and a carriage return on every line would clutter diffs.
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(["date", "local_open", "local_close", "is_early_close"])
            for s in sorted(sessions, key=lambda s: s.day):
                writer.writerow([s.day.isoformat(), s.local_open, s.local_close,
                                 "1" if s.is_early_close else "0"])

    atomic.write_replacing(path, _write)


# THE TABLE EXTENDS ITSELF, APPEND-ONLY. Past its last row every weekday would
# read as a holiday: no fund gap scored, no fund ever skipped. So the hourly run
# checks how far it reaches (needs_extension, no calendar library needed) and,
# once under EXTEND_WHEN_YEARS_LEFT years remain, the workflow installs
# exchange_calendars and appends the years up to EXTEND_YEARS ahead - about
# once a year. Only days AFTER the last row are written: the rows already there
# are the schedule every stored reading was judged against, and a newer library
# that revised a past session must not move them (reason 1 above).
EXTEND_WHEN_YEARS_LEFT = 2
EXTEND_YEARS = 3


def needs_extension(path: str = DEFAULT_SESSIONS_PATH, today: "date | None" = None) -> bool:
    """Whether the table ends within EXTEND_WHEN_YEARS_LEFT years of today."""
    today = today or date.today()
    last = max(load_sessions(path))
    return last < today + timedelta(days=round(365.25 * EXTEND_WHEN_YEARS_LEFT))


def extend_sessions(path: str = DEFAULT_SESSIONS_PATH, today: "date | None" = None,
                    generate=None) -> int:
    """Appends the sessions after the table's last row through the end of the
    year EXTEND_YEARS from now, if it is short. Returns how many days were added;
    the rows already in the table are left exactly as they are."""
    today = today or date.today()
    if not needs_extension(path, today):
        return 0
    table = load_sessions(path)
    last = max(table)
    until = date(today.year + EXTEND_YEARS, 12, 31)
    new = [s for s in (generate or generate_nyse_sessions)(last + timedelta(days=1), until)
           if s.day > last]
    if new:
        write_sessions(path, list(table.values()) + new)
    return len(new)


def load_sessions(path: str = DEFAULT_SESSIONS_PATH) -> dict[date, Session]:
    """Reads the session table. The calendar library is not needed for this."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No session table at {path}. Generate it: python -m tremor.sessions")
    sessions = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            day = date.fromisoformat(row["date"])
            sessions[day] = Session(
                day=day, local_open=row["local_open"], local_close=row["local_close"],
                is_early_close=row["is_early_close"] == "1",
            )
    return sessions


EXCHANGE_TZ = "America/New_York"


def session_hours(session: Session, tz_name: str = EXCHANGE_TZ) -> list[int]:
    """The hour_utc stamps a COMPLETE session should occupy.

    From the hour containing the open to the hour containing the last full
    half-hour bar. A 09:30-16:00 session is seven bars (09:00 .. 15:00): the
    15:30 bar covers 15:30-16:00 and folds into 15:00, so there is no 16:00 bar
    and demanding one would mark every ordinary day incomplete.

    An early close is one bar shorter than the arithmetic suggests for the same
    reason, and the store in fact carries one MORE - the 13:00:00 closing print
    lands in a bar of its own. Expecting the smaller set is the safe direction:
    a bar that exists and was not demanded is not a hole.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    open_h, open_m = (int(x) for x in session.local_open.split(":"))
    close_h, close_m = (int(x) for x in session.local_close.split(":"))
    last = close_h if close_m else close_h - 1
    stamps = []
    for hour in range(open_h, last + 1):
        moment = datetime.combine(session.day, time(hour), tzinfo=tz)
        stamps.append(int(moment.timestamp()))
    return stamps


def expected_hours(sessions: dict[date, Session], first: date, last: date,
                   tz_name: str = EXCHANGE_TZ) -> set[int]:
    """Every hour_utc the calendar claims between two days, inclusive."""
    return {stamp
            for day, session in sessions.items()
            if first <= day <= last
            for stamp in session_hours(session, tz_name)}


def cached_sessions(path: str = DEFAULT_SESSIONS_PATH) -> dict[date, Session]:
    """load_sessions, read once per process.

    The delivery layer asks the same question of the same table once per event
    per horizon, and the table is a static file of several thousand rows that
    only ever changes when someone regenerates it by hand.
    """
    return _load_sessions_cached(os.path.abspath(path))


@lru_cache(maxsize=4)
def _load_sessions_cached(path: str) -> dict[date, Session]:
    return load_sessions(path)


def instrument_day_hours(day: date, template: str,
                         table: "dict[date, Session] | None" = None,
                         tz_name: str = EXCHANGE_TZ) -> list[int]:
    """Every hour_utc the instrument trades on that day, in order.

    Empty for a day it does not trade - a weekend, a holiday, a Saturday in a
    currency pair - which is what makes the walk below skip such days without
    knowing anything about why they are closed.
    """
    if template == "us_equity":
        session = (table or {}).get(day)
        return session_hours(session, tz_name) if session else []

    midnight = int(datetime.combine(day, time(0), tzinfo=timezone.utc).timestamp())
    hours = [midnight + i * HOUR for i in range(24)]
    if template == "crypto_24_7":
        return hours
    if template == "fx_continuous":
        return [h for h in hours if is_reference_hour(h, tz_name)]
    if template in DAILY_SESSIONS:
        return [h for h in hours if session_key(h, template) is not None]
    raise ValueError(f"unknown session template '{template}'")


# The reference week runs Sunday 17:00 to Friday 17:00 local: five days on.
REFERENCE_CLOSE_DAYS = 5


def reference_week_bounds(any_moment: datetime, anchor_tz: str) -> tuple[int, int]:
    """Bounds of the reference-calendar week containing `any_moment`:
    (open, close) in epoch UTC.

    The bounds are given in the anchor exchange's local time and converted to UTC
    on the fly - storing them as UTC is forbidden, because a change to daylight
    saving would shift them relative to the market.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(anchor_tz)
    local = any_moment.astimezone(tz)
    # Sunday 17:00 of the week the moment belongs to. weekday(): Mon=0, Sun=6.
    # A moment before the Sunday open belongs to the previous week.
    days_since_sunday = (local.weekday() + 1) % 7
    sunday = (local - timedelta(days=days_since_sunday)).date()
    opened = datetime.combine(sunday, time(REFERENCE_OPEN_HOUR), tzinfo=tz)
    if local < opened:
        opened = datetime.combine(sunday - timedelta(days=7), time(REFERENCE_OPEN_HOUR),
                                  tzinfo=tz)
    closed = datetime.combine(opened.date() + timedelta(days=REFERENCE_CLOSE_DAYS),
                              time(REFERENCE_CLOSE_HOUR), tzinfo=tz)
    return int(opened.timestamp()), int(closed.timestamp())


def reference_week_opens(moments: "pd.Series", anchor_tz: str) -> "pd.Series":
    """reference_week_bounds' opening moment, for a whole series at once.

    The scalar version is Python datetime arithmetic and two timezone
    conversions per call, and the callers ask it per BAR: at 145,000 bars it was
    292,165 calls and the single largest cost in the metrics stage. Nothing
    about the answer is per-bar - it is a property of the week - so it
    vectorises completely.

    DST is the reason this is not simply "add seventeen hours". The bounds are
    defined in the anchor exchange's local time, so the arithmetic is done on
    NAIVE local timestamps and localised afterwards; adding a seventeen-hour
    offset to a tz-aware timestamp would work in absolute time and drift by an
    hour across each transition, which is exactly what storing the bounds as UTC
    would have done and what the design forbids.
    """
    import pandas as pd

    local = pd.to_datetime(moments, utc=True).dt.tz_convert(anchor_tz)
    naive = local.dt.tz_localize(None)
    days_since_sunday = (naive.dt.weekday + 1) % 7
    sunday = naive.dt.normalize() - pd.to_timedelta(days_since_sunday, unit="D")
    opened = sunday + pd.Timedelta(hours=REFERENCE_OPEN_HOUR)
    # A moment before its own Sunday open belongs to the previous week.
    opened = opened.where(naive >= opened, opened - pd.Timedelta(days=7))
    return opened.dt.tz_localize(anchor_tz, nonexistent="shift_forward",
                                 ambiguous=True)


def reference_hours_mask(hours_utc, anchor_tz: str) -> "pd.Series":
    """Which of these hours fall inside the reference calendar, vectorised.

    Same answer as is_reference_hour one at a time, and the same caveat: the
    week is exactly 120 hours long and holidays are not subtracted from it. The
    caller's index is preserved, because every caller is masking a frame with it.
    """
    import pandas as pd

    hours = hours_utc if isinstance(hours_utc, pd.Series) else pd.Series(
        list(hours_utc), dtype="int64")
    moments = pd.to_datetime(hours.astype("int64"), unit="s", utc=True)
    opened = reference_week_opens(moments, anchor_tz)
    closed = (opened.dt.tz_localize(None)
              + pd.Timedelta(days=REFERENCE_CLOSE_DAYS)
              + pd.Timedelta(hours=REFERENCE_CLOSE_HOUR - REFERENCE_OPEN_HOUR)
              ).dt.tz_localize(anchor_tz, nonexistent="shift_forward", ambiguous=True)
    return (moments >= opened) & (moments < closed)


def is_reference_hour(hour_utc: int, anchor_tz: str) -> bool:
    """Whether an hour (by the bar's OPENING moment) falls in the reference
    calendar.

    Holidays are not excluded: the week is exactly 120 hours long and
    holidays are not subtracted from it. The reference calendar is the basket's
    hours, not the schedule of any particular exchange.
    """
    moment = datetime.fromtimestamp(hour_utc, tz=timezone.utc)
    opened, closed = reference_week_bounds(moment, anchor_tz)
    return opened <= hour_utc < closed


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate the NYSE session table")
    # The table starts with the store's oldest bars. A narrower --start rewrites
    # it without the years before, which every fund's history was judged against.
    parser.add_argument("--start", default="2002-01-01")
    parser.add_argument("--end", default="2028-12-31")
    parser.add_argument("--out", default=DEFAULT_SESSIONS_PATH)
    parser.add_argument("--extend-if-short", action="store_true",
                        help="append the coming years when under two remain "
                             "(append-only; what the hourly workflow runs)")
    args = parser.parse_args(argv)

    if args.extend_if_short:
        added = extend_sessions(args.out)
        print(f"{args.out}: {added} trading days appended" if added
              else f"{args.out}: reaches far enough, nothing appended")
        return 0

    sessions = generate_nyse_sessions(date.fromisoformat(args.start),
                                      date.fromisoformat(args.end))
    write_sessions(args.out, sessions)
    early = sum(1 for s in sessions if s.is_early_close)
    print(f"{args.out}: trading days {len(sessions)}, half sessions {early}, "
          f"{sessions[0].day} .. {sessions[-1].day}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())


# DAILY SESSIONS: a market that opens and closes once a trading day (Monday to
# Friday) at fixed local times. An hour (by its opening moment) is in the session
# when [h, h+1) overlaps [open, close) - the us_equity rule. A session whose open
# is later than its close starts the evening before: cotton's Monday runs from
# Sunday 21:00 New York. Holidays are not listed: the source has no bars on them,
# so the close before one is simply longer, and its gap is judged with the other
# closes of that length (tremor.jumps.gap_kinds).
#
#   lme          tin, nickel and aluminium on LMEselect, 01:00-19:00 London
#   ice_coffee   ICE arabica, 04:15-13:30 New York
#   ice_cocoa    ICE cocoa, 04:45-13:30 New York
#   ice_cotton   ICE cotton No. 2, 21:00-14:20 New York
#   cme_cattle   CME live cattle, 08:30-13:05 Chicago
#   b3_fx        the Brazilian real, 09:00-18:00 Sao Paulo: on a whole session
#                of hourly bars, 94-98% of the hours from 12:00 to 21:00 UTC move
#                and carry 9-31 bp each, the rest under 3.5 bp - the offshore
#                quote before the onshore market opens
DAILY_SESSIONS: "dict[str, tuple[str, tuple[int, int], tuple[int, int]]]" = {
    "lme": ("Europe/London", (1, 0), (19, 0)),
    "ice_coffee": ("America/New_York", (4, 15), (13, 30)),
    "ice_cocoa": ("America/New_York", (4, 45), (13, 30)),
    "ice_cotton": ("America/New_York", (21, 0), (14, 20)),
    "cme_cattle": ("America/Chicago", (8, 30), (13, 5)),
    "b3_fx": ("America/Sao_Paulo", (9, 0), (18, 0)),
}

# The longest a daily-session market can be shut: Thursday's close to Tuesday's
# open over Easter, or a Christmas on a Thursday, about 102 hours. Longer means
# the source was out, not the market shut, and the move across is not a gap.
DAILY_CLOSED_MAX_SECONDS = 110 * HOUR


def _bounds(day: date, name: str) -> "tuple[datetime, datetime]":
    """The session of trading day `day`: its open and close as aware datetimes."""
    from zoneinfo import ZoneInfo

    zone, (oh, om), (ch, cm) = DAILY_SESSIONS[name]
    tz = ZoneInfo(zone)
    opened_on = day - timedelta(days=1) if (oh, om) > (ch, cm) else day
    return (datetime.combine(opened_on, time(oh, om), tzinfo=tz),
            datetime.combine(day, time(ch, cm), tzinfo=tz))


def daily_session_of(hour_utc: int, name: str) -> "date | None":
    """The trading day an hour belongs to, or None outside every session."""
    from zoneinfo import ZoneInfo

    start = datetime.fromtimestamp(int(hour_utc), ZoneInfo(DAILY_SESSIONS[name][0]))
    end = start + timedelta(hours=1)
    for day in (start.date(), start.date() + timedelta(days=1)):
        if day.weekday() >= 5:
            continue
        opened, closed = _bounds(day, name)
        if start < closed and end > opened:
            return day
    return None


def daily_session_open(day: date, name: str) -> int:
    """Epoch UTC of that trading day's open."""
    return int(_bounds(day, name)[0].timestamp())


def daily_session_close(day: date, name: str) -> int:
    """Epoch UTC of that trading day's close."""
    return int(_bounds(day, name)[1].timestamp())


def daily_bars_per_day(name: str) -> int:
    """How many hourly bars a whole session holds (on a winter Wednesday)."""
    day = date(2026, 1, 7)
    opened, closed = _bounds(day, name)
    first = int(opened.timestamp()) // HOUR * HOUR - HOUR
    return sum(daily_session_of(h, name) == day
               for h in range(first, int(closed.timestamp()) + HOUR, HOUR))


def session_key(hour_utc: int, template: str) -> "str | None":
    """The daily session an hour belongs to, or None."""
    day = daily_session_of(hour_utc, template)
    return str(day) if day else None


def session_key_close(key: str, template: str) -> int:
    return daily_session_close(date.fromisoformat(key), template)


def hours_mask(hours_utc, template: str) -> "pd.Series":
    """Which of these hours are in a daily template's sessions."""
    import pandas as pd

    hours = hours_utc if isinstance(hours_utc, pd.Series) else pd.Series(
        list(hours_utc), dtype="int64")
    return hours.map(lambda h: session_key(int(h), template) is not None).astype(bool)


def is_calendar_template(template: str) -> bool:
    """A template whose sessions are computed here (a daily session)."""
    return template in DAILY_SESSIONS

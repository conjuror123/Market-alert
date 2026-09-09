"""Session calendar and the basket's reference calendar (spec §2.2).

The NYSE schedule comes from the exchange_calendars library, but NOT on every
run: the library acts as a generator, and the result of its work lives in the
repository as a table committed alongside the code. There are three reasons.

1. Reproducibility (§6.2). A test must yield an identical set of events on a
   repeat run of the same period under the same config_version. If the schedule
   is computed by the library at launch time, an update to it can move historical
   sessions - and a past backtest stops reproducing, although not a single
   configuration parameter was touched.
2. The §6.4 schema explicitly requires holidays and half_sessions tables - that
   is, data, not a function call.
3. The hourly run then needs no calendar library at all, only a ready CSV. Fewer
   dependencies in production, faster installs.

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

# The basket's reference calendar (§2.2): the continuous trading week of the
# anchor exchange, from Sun 17:00 to Fri 17:00 of its LOCAL time. Exactly 120
# hours; holidays are not subtracted from the week - §2.2 says so in as many
# words.
REFERENCE_WEEK_HOURS = 120
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
    than a production one.
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
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        # \n rather than the default \r\n: the file lives in the repository, and
        # a carriage return on every line would clutter diffs on each regeneration.
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["date", "local_open", "local_close", "is_early_close"])
        for s in sorted(sessions, key=lambda s: s.day):
            writer.writerow([s.day.isoformat(), s.local_open, s.local_close,
                             "1" if s.is_early_close else "0"])


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


def is_holiday(day: date, sessions: dict[date, Session]) -> bool:
    """A business day absent from the session table. Weekends do not count as
    holidays - that is the ordinary close of the week."""
    return day.weekday() < 5 and day not in sessions


def half_sessions(sessions: dict[date, Session]) -> list[Session]:
    return [s for s in sessions.values() if s.is_early_close]


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


# How far forward the bar walk will look before giving up. Three weeks is far
# past any weekend, holiday or exchange closure the table describes, and the
# ceiling exists only so a template whose days are all empty - a session table
# that has run out of years, say - returns "no answer" instead of looping.
MAX_LOOKAHEAD_DAYS = 21


def instrument_day(hour_utc: int, template: str,
                   tz_name: str = EXCHANGE_TZ) -> date:
    """The day an hour belongs to, in the calendar this instrument's day uses.

    An exchange-listed instrument's day is the exchange's local day; everything
    else has no daily close to speak of, so its day is the UTC one. Same rule as
    persistence.next_close_offsets, and it has to stay the same rule: that is
    where the settled reading is actually measured.
    """
    moment = datetime.fromtimestamp(int(hour_utc), tz=timezone.utc)
    if template == "us_equity":
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo(tz_name)).date()
    return moment.date()


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
    raise ValueError(f"unknown session template '{template}'")


def bars_after(hour_utc: int, count: int, template: str,
               table: "dict[date, Session] | None" = None,
               tz_name: str = EXCHANGE_TZ) -> "int | None":
    """The stamp of the bar `count` bars after the one opening at `hour_utc`.

    Bars, not hours: the answer to "two bars after Friday's last one" is Monday
    morning, and every horizon in this system is counted the same way (see
    tremor.persistence). Returns None when the calendar cannot reach that far -
    the honest answer for an instrument whose session table has run out.
    """
    if count <= 0:
        return int(hour_utc)
    day = instrument_day(hour_utc, template, tz_name)
    seen = 0
    for _ in range(MAX_LOOKAHEAD_DAYS):
        for stamp in instrument_day_hours(day, template, table, tz_name):
            if stamp <= hour_utc:
                continue
            seen += 1
            if seen == count:
                return stamp
        day += timedelta(days=1)
    return None


def today_close_after(hour_utc: int, template: str,
                      table: "dict[date, Session] | None" = None,
                      tz_name: str = EXCHANGE_TZ) -> "int | None":
    """When the instrument's OWN day ends, as an epoch UTC moment.

    The moment the first check-in becomes measurable. Equal to the end of the
    bar itself when the move happened in the closing hour, which is not a
    failure: there is no day left to hold through, and the message says so.
    """
    day = instrument_day(hour_utc, template, tz_name)
    hours = instrument_day_hours(day, template, table, tz_name)
    later = [h for h in hours if h >= int(hour_utc)]
    return (later[-1] + HOUR) if later else None


def next_close_after(hour_utc: int, template: str,
                     table: "dict[date, Session] | None" = None,
                     tz_name: str = EXCHANGE_TZ) -> "int | None":
    """When the instrument's NEXT trading day ends, as an epoch UTC moment.

    The moment the settled reading becomes measurable: the last bar of that day
    has closed. Which day is "next" is read off the trading calendar, so a
    Friday move settles at Monday's close and a move on the eve of a holiday at
    the close after it.
    """
    day = instrument_day(hour_utc, template, tz_name)
    for _ in range(MAX_LOOKAHEAD_DAYS):
        day += timedelta(days=1)
        hours = instrument_day_hours(day, template, table, tz_name)
        if hours:
            return hours[-1] + HOUR
    return None


# The reference week runs Sunday 17:00 to Friday 17:00 local: five days on.
REFERENCE_CLOSE_DAYS = 5


def reference_week_bounds(any_moment: datetime, anchor_tz: str) -> tuple[int, int]:
    """Bounds of the reference-calendar week containing `any_moment`:
    (open, close) in epoch UTC.

    The bounds are given in the anchor exchange's local time and converted to UTC
    on the fly - §2.2 forbids storing them as UTC, because the switch to daylight
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
    """Whether an hour (by the bar's OPENING moment, §1.2) falls in the reference
    calendar.

    Holidays are not excluded: per §2.2 the week is exactly 120 hours long and
    holidays are not subtracted from it. The reference calendar is the basket's
    hours, not the schedule of any particular exchange.
    """
    moment = datetime.fromtimestamp(hour_utc, tz=timezone.utc)
    opened, closed = reference_week_bounds(moment, anchor_tz)
    return opened <= hour_utc < closed


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate the NYSE session table (spec §2.2)")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2028-12-31")
    parser.add_argument("--out", default=DEFAULT_SESSIONS_PATH)
    args = parser.parse_args(argv)

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

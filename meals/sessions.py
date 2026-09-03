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

HOUR = 3600
DEFAULT_SESSIONS_PATH = os.path.join("data", "meals", "sessions", "nyse.csv")

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
            f"No session table at {path}. Generate it: python -m meals.sessions")
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
    closed = datetime.combine(opened.date() + timedelta(days=5),
                              time(REFERENCE_CLOSE_HOUR), tzinfo=tz)
    return int(opened.timestamp()), int(closed.timestamp())


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

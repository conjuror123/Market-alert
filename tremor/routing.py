"""Who gets interrupted, and who goes quietly into the running note.

The tier says how rare a move was. It does not say whether the message should
make a phone buzz, and those are different questions - deliberately so. Rarity
is a property of the instrument and its history, and it means the same thing
whether the system watches five instruments or fifty. How often someone is
willing to be interrupted is a property of the person, and it does not grow
when the watchlist does. Keeping them apart is what stops the alert rate from
tripling the day three stocks are added.

There are two channels, and they differ in how loudly they arrive rather than
in how long they wait. Nothing is held back:

  push      `high`, `major` and `extreme`, sent AT ONCE, as their own
            message - nothing folds into it. A once-a-year move that arrives
            six hours late is a worse product than one that arrives now.
  digest    `noticeable`, written into the weekly note as it is found. That note is OPENED at the start of the period it covers
            and edited in place afterwards, so a digest line appears within the
            hour of the move rather than days later - and the edit is silent, so
            each row also gets a small ping that lives as long as its row
            (see price_monitor.tremor_delivery).

There is deliberately no cap on how many pushes a week may contain. A detector
that counts its own alerts and goes quiet on the third one is answering a
question about the reader's patience with an instrument's price history, and
the two have nothing to do with each other: the week the franc is unpegged is
exactly the week a budget would start silencing things. Volume is controlled
where it is generated - by the jump detector's words (tremor.jumps).
"""
from __future__ import annotations

import bisect
import os
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

PUSH = "push"
DIGEST = "digest"

# The words that interrupt, at once. `noticeable` goes into the weekly note.
PUSH_TIERS = ("high", "major", "extreme")

# ONE NOTE A WEEK, turning at the first run after the WEEK'S LAST FUNDS CLOSE -
# the NYSE close, normally Friday 16:00 New York; Thursday on a Good Friday week,
# 13:00 on a half day - just after the coming week's economic calendar goes out as
# its own message (price_monitor.weekly_digest). Every word from `high` up
# pushes, so the note carries only `noticeable` rows and one a week holds them.
#
# THE FUNDS' CLOSE, because every move is checked at it (tremor.jumps
# held_at_close): with the week turning just after its last close, every check
# lands inside its own week. The run that turns the week fills in the old week's
# checks at that close first; the closing hour itself is found in that run and
# goes into the new note, and is checked at Monday's close.
#
# Five past, the run after the close: the hourly job runs at :05.
TURN_AFTER_CLOSE = 300
EXCHANGE_TZ = ZoneInfo("America/New_York")
SESSIONS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tremor",
                             "sessions", "nyse.csv")


@lru_cache(maxsize=1)
def closes() -> "tuple[int, ...]":
    """Every NYSE close in the session table, as UTC seconds, in order."""
    from tremor.sessions import cached_sessions

    out = []
    for day, session in cached_sessions(SESSIONS_PATH).items():
        hh, mm = (int(x) for x in session.local_close.split(":"))
        moment = datetime(day.year, day.month, day.day, hh, mm, tzinfo=EXCHANGE_TZ)
        out.append(int(moment.timestamp()))
    return tuple(sorted(out))


@lru_cache(maxsize=1)
def _turns() -> "tuple[int, ...]":
    """Every week's turn: its last close, plus the run after it."""
    last: dict = {}
    for close in closes():
        local = datetime.fromtimestamp(close, tz=EXCHANGE_TZ)
        week = local.isocalendar()[:2]
        last[week] = max(last.get(week, close), close)
    return tuple(sorted(c + TURN_AFTER_CLOSE for c in last.values()))


def next_close(moment: int) -> "int | None":
    """The first NYSE close strictly after `moment`, or None past the table."""
    table = closes()
    at = bisect.bisect_right(table, int(moment))
    return table[at] if at < len(table) else None


def digest_slot(hour_utc: int) -> int:
    """WHICH note this moment belongs to: the one opened at or before it."""
    turns = _turns()
    at = bisect.bisect_right(turns, int(hour_utc)) - 1
    if at < 0:
        raise RuntimeError("before the session table begins")
    return turns[at]


def next_digest_slot(hour_utc: int) -> int:
    """The first turn strictly after this moment - when the open note stops
    taking events and the next one opens."""
    turns = _turns()
    at = bisect.bisect_right(turns, int(hour_utc))
    if at >= len(turns):
        raise RuntimeError("past the session table's end")
    return turns[at]



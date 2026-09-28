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

from datetime import datetime, time, timedelta, timezone

PUSH = "push"
DIGEST = "digest"

# The words that interrupt, at once. `noticeable` goes into the weekly note.
PUSH_TIERS = ("high", "major", "extreme")

# datetime.weekday(): Monday=0, Sunday=6. ONE NOTE A WEEK, opening Sunday and
# running to the next Sunday, just after the coming week's economic calendar
# goes out as its own message (price_monitor.weekly_digest). Sunday because a
# forecast wants to arrive before the week it forecasts, and the week's markets
# are all shut by then. With the jump detector every word from `high` up
# pushes, so the note carries only `noticeable` rows and one a week holds them.
#
# UTC AND NOT THE READER'S CLOCK. The ping is what buzzes; the note is a record,
# and a record wants the boundary the market uses. 00:05 UTC sits between the
# American close and the Asian open, the quietest hour there is, and it does not
# drift by an hour twice a year.
#
# Five past rather than on the hour: the hourly job runs at :05, so a note opens
# on the first run of its period instead of waiting fifty-five minutes.
DIGEST_WEEKDAYS = (6,)
DIGEST_HOUR_LOCAL = 0
DIGEST_MINUTE_LOCAL = 5
DIGEST_TZ = timezone.utc


def digest_slot(hour_utc: int) -> int:
    """WHICH digest note this hour belongs to: the one opened at or before it.

    The note is opened at the start of the period it covers and edited as
    events are found, so the slot an event carries names a message that already
    exists rather than a time to wait for: an event joins a live note instead of
    queueing for one.

    Kept as a zone lookup rather than arithmetic on the timestamp even though
    the zone is now UTC: the boundary is a wall-clock rule - Sunday at 00:05
    - and expressing it as a modulus would quietly break the day a
    different zone is wanted again.
    """
    moment = datetime.fromtimestamp(int(hour_utc), tz=timezone.utc).astimezone(DIGEST_TZ)
    for back in range(0, 9):
        day = (moment - timedelta(days=back)).date()
        if day.weekday() not in DIGEST_WEEKDAYS:
            continue
        slot = datetime.combine(day, time(DIGEST_HOUR_LOCAL, DIGEST_MINUTE_LOCAL),
                                tzinfo=DIGEST_TZ)
        if slot <= moment:
            return int(slot.astimezone(timezone.utc).timestamp())
    raise RuntimeError("no digest slot within nine days")


def next_digest_slot(hour_utc: int) -> int:
    """The first slot strictly after this hour - when the open note stops taking
    events and the next one opens."""
    moment = datetime.fromtimestamp(int(hour_utc), tz=timezone.utc).astimezone(DIGEST_TZ)
    for ahead in range(0, 9):
        day = (moment + timedelta(days=ahead)).date()
        if day.weekday() not in DIGEST_WEEKDAYS:
            continue
        slot = datetime.combine(day, time(DIGEST_HOUR_LOCAL, DIGEST_MINUTE_LOCAL),
                                tzinfo=DIGEST_TZ)
        if slot > moment:
            return int(slot.astimezone(timezone.utc).timestamp())
    raise RuntimeError("no digest slot within nine days")


def digest_window(slot_utc: int) -> tuple[int, int]:
    """The period a note covers: from when it opened to when the next one does."""
    return int(slot_utc), next_digest_slot(int(slot_utc))

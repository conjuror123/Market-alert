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

  push      both push tiers, sent AT ONCE, as their own message, and final on
            arrival - nothing folds into it and nothing moves it afterwards. A
            once-a-year move that arrives six hours late is a worse product than
            one that arrives now and is marked "reverted" later.
  digest    everything else, written into the Monday or Saturday note as it is
            found. That note is OPENED at the start of the period it covers
            and edited in place afterwards, so a digest line appears within the
            hour of the move rather than days later - and the edit is silent, so
            each row also gets a throwaway ping that is deleted when the next
            note opens (see price_monitor.tremor_delivery).

Both kinds of message are then corrected in place as the market answers: at the
close of the day the move happened and at the close of the next day the
instrument trades, for a push and for a digest line alike. That is what the
retention check decides - what the sent message SAYS, not whether it is sent.
Gating on it would buy silence for as long as the answer took to arrive; this
way a move that reverted is still shown, with the fact that it reverted written
on it.

There is deliberately no cap on how many pushes a week may contain. A detector
that counts its own alerts and goes quiet on the third one is answering a
question about the reader's patience with an instrument's price history, and
the two have nothing to do with each other: the week the franc is unpegged is
exactly the week a budget would start silencing things. Volume is controlled
where it is actually generated - by the rarity ladder, which asks how unusual
this move is for THIS instrument, and by the size floor beside it, which asks
whether the move was big enough in the instrument's own terms to be worth a
line. Both are statements about the market. Neither needs to know the running
total, and the total is a thing to report afterwards, not a thing to steer by.
Both are turned from config/basket.yaml rather than from here.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pandas as pd

from tremor import persistence, severity

PUSH = "push"
DIGEST = "digest"

# The tiers that interrupt. Both go at once.
PUSH_TIERS = ("major", "extreme")
SETTLED_HORIZON = persistence.SETTLED        # the next trading close

# datetime.weekday(): Monday=0, Saturday=5. Two notes a week, each opening at
# the start of the stretch it covers rather than in the middle of one: the
# workweek note opens Monday and runs to Saturday, the weekend note opens
# Saturday and runs to Monday. So a note is never half trading week and half
# weekend, which is what a Tuesday/Friday pair could not avoid.
#
# UTC AND NOT THE READER'S CLOCK. The ping is what buzzes; the note is a record,
# and a record wants the boundary the market uses. 00:05 UTC sits between the
# American close and the Asian open, the quietest hour there is, and it does not
# drift by an hour twice a year.
#
# Five past rather than on the hour: the hourly job runs at :05, so a note opens
# on the first run of its period instead of waiting fifty-five minutes.
DIGEST_WEEKDAYS = (0, 5)
DIGEST_HOUR_LOCAL = 0
DIGEST_MINUTE_LOCAL = 5
DIGEST_TZ = timezone.utc


def channel(events: pd.DataFrame) -> pd.Series:
    """The delivery channel for each event.

    Tier alone, and deliberately nothing else. Letting retention in here would
    mean a note could only be written once every one of its events had been
    answered - a report three days after the fact. The answer is written onto
    the line instead, and the line goes up straight away.
    """
    if events.empty:
        return pd.Series(dtype="string")

    pushes = events["tier"].isin(PUSH_TIERS).fillna(False).to_numpy(dtype=bool)
    out = pd.Series(DIGEST, index=events.index, dtype="string")
    return out.mask(pushes, PUSH)


# A PUSH IS FINAL WHEN IT ARRIVES, and nothing here moves an event between
# channels after the fact. Folding a second push of the day under the first
# would be an argument about message count, and the count is not what this is
# for: six messages on the day the market breaks is the bot working, where the
# same six spread over six quiet days would not be. It also makes an event's
# channel depend on what else happened that day - a push tier sitting in the
# digest, a digest row turning into a push later, 199 of 9,069 events in a
# channel their tier did not choose.
#
# So: tier decides, once, and that is the whole rule.
def digest_slot(hour_utc: int) -> int:
    """WHICH digest note this hour belongs to: the one opened at or before it.

    The note is opened at the start of the period it covers and edited as
    events are found, so the slot an event carries names a message that already
    exists rather than a time to wait for: an event joins a live note instead of
    queueing for one.

    Kept as a zone lookup rather than arithmetic on the timestamp even though
    the zone is now UTC: the boundary is a wall-clock rule - Monday and Saturday
    at 00:05 - and expressing it as a modulus would quietly break the day a
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


def route(events: pd.DataFrame) -> pd.DataFrame:
    """Adds `channel` and, for the digested ones, the note they belong to.

    One line of logic on purpose. An event's channel is a function of its tier
    and of nothing else - not of what else moved that day, not of how the move
    later held - so it is decided once, when the event is built, and never
    revised.
    """
    if events.empty:
        return events.assign(channel=pd.Series(dtype="string"),
                             digest_slot=pd.Series(dtype="Int64"))

    channels = channel(events)
    digested = channels.eq(DIGEST).fillna(False).to_numpy(dtype=bool)
    slots = pd.Series(pd.NA, index=events.index, dtype="Int64")
    if digested.any():
        slots.loc[digested] = pd.array(
            [digest_slot(h) for h in events.loc[digested, "hour_utc"]], dtype="Int64")
    return events.assign(channel=channels, digest_slot=slots)


def summarise(routed: pd.DataFrame) -> pd.DataFrame:
    """Counts by channel and tier, for reporting what the settings actually do."""
    if routed.empty:
        return pd.DataFrame()
    table = pd.crosstab(routed["tier"], routed["channel"])
    return table.reindex(index=[t for t in severity.TIERS if t in table.index],
                         columns=[c for c in (PUSH, DIGEST) if c in table.columns],
                         fill_value=0)

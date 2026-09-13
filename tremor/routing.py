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

  push      both push tiers, sent AT ONCE, as their own message, and then that
            message collects the rest of the day: another instrument moving
            before midnight joins it rather than buzzing again (see collapse).
            A once-a-year move that arrives six hours late is a worse product
            than one that arrives now and is marked "reverted" later.
  digest    everything else, written into the Tuesday or Friday note as it is
            found. That note is OPENED at the start of the period it covers
            and edited in place afterwards, so a digest line appears within the
            hour of the move rather than up to three days later - and it is a
            silent edit, so the reader is interrupted twice a week and no more
            (see price_monitor.tremor_delivery).

Both kinds of message are then corrected in place as the market answers: at two
bars, at six, and at the close of the next trading day for a push, and at the
next close for a digest line. That is where the retention check went. It used
to decide whether an event was sent at all - a move that gave everything back
took no line - and the price of that was silence for as long as the answer took
to arrive. It now decides what the sent message SAYS, which costs nothing and
hides nothing: a move that reverted is still shown, with the fact that it
reverted written on it.

There is deliberately no cap on how many pushes a week may contain. A detector
that counts its own alerts and goes quiet on the third one is answering a
question about the reader's patience with an instrument's price history, and
the two have nothing to do with each other: the week the franc is unpegged is
exactly the week a budget would start silencing things. Volume is controlled
where it is actually generated - by the rarity ladder, which asks how unusual
this move is for THIS instrument, and by the collapse below, which asks whether
this is a new event or the same one seen again. Both are statements about the
market. Neither needs to know the running total, and the total is a thing to
report afterwards, not a thing to steer by.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from tremor import persistence, severity

PUSH = "push"
DIGEST = "digest"

# The tiers that interrupt. Both go at once.
PUSH_TIERS = ("major", "extreme")
SETTLED_HORIZON = persistence.SETTLED        # the next trading close

# datetime.weekday(): Monday=0. Tuesday covers the weekend and Monday - which
# matters, because crypto trades straight through it and equities gap on the
# Monday open - and Friday closes the trading week before the weekend.
DIGEST_WEEKDAYS = (1, 4)
DIGEST_HOUR_LOCAL = 12
DIGEST_TZ = ZoneInfo("Asia/Jerusalem")


def channel(events: pd.DataFrame) -> pd.Series:
    """The delivery channel for each event, before the collapse.

    Tier alone, and deliberately nothing else. Retention used to enter here -
    an event whose move had fully reverted was dropped rather than digested -
    and the cost was that the digest could only be written once every one of
    its events had been answered, which is what made it a report three days
    after the fact. The answer is now written onto the line instead, and the
    line goes up straight away.
    """
    if events.empty:
        return pd.Series(dtype="string")

    pushes = events["tier"].isin(PUSH_TIERS).fillna(False).to_numpy(dtype=bool)
    out = pd.Series(DIGEST, index=events.index, dtype="string")
    return out.mask(pushes, PUSH)


# A PUSH IS FINAL WHEN IT ARRIVES, and nothing here moves an event between
# channels after the fact. There used to be a collapse: a second push inside the
# same UTC day was folded under the first, on the argument that 2008-11-20's six
# pushes and 2020-03-12's five were each one market event delivered five or six
# times.
#
# The argument was about message count and the count is not what this is for. Six
# messages on the day the market breaks is the bot working; the same six spread
# over six quiet days would not be. Folding also cost the thing a push is for -
# it is an alert, it goes out first and is analysed afterwards - and it produced
# the one behaviour that made the routing hard to reason about: an event whose
# channel depended on what else happened that day, so a push tier could sit in
# the digest and a digest row could turn into a push later. Measured on the
# record, that was 199 of 9,069 events living in a channel their tier did not
# choose.
#
# So: tier decides, once, and that is the whole rule.
def digest_slot(hour_utc: int) -> int:
    """WHICH digest note this hour belongs to: the one opened at or before it.

    The note is opened at the start of the period it covers and edited as
    events are found, so the slot an event carries is a name for a message
    that already exists rather than a time to wait for. Under the old
    end-of-period digest this returned the next slot AFTER the hour, and the
    difference is the whole change: an event now joins a live note instead of
    queueing for one.

    Local time, because the recipient reads it in local time and a note that
    opens at 04:00 twice a week is one nobody opens. Whether that is 12:00 UTC
    or 13:00 depends on daylight saving, and letting the zone decide is why
    this is not arithmetic on the timestamp.
    """
    moment = datetime.fromtimestamp(int(hour_utc), tz=timezone.utc).astimezone(DIGEST_TZ)
    for back in range(0, 9):
        day = (moment - timedelta(days=back)).date()
        if day.weekday() not in DIGEST_WEEKDAYS:
            continue
        slot = datetime.combine(day, time(DIGEST_HOUR_LOCAL), tzinfo=DIGEST_TZ)
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
        slot = datetime.combine(day, time(DIGEST_HOUR_LOCAL), tzinfo=DIGEST_TZ)
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
    revised. See the note above the digest slots for what used to be here.
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

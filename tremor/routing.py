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


# How long one push speaks for: THE REST OF THE DAY IT OPENED IN, by the UTC
# clock. A second instrument moving inside that day is almost always the same
# event seen again rather than news - measured over the whole record,
# 2008-11-20 sent six pushes across two hours (SPY, XLF, USO, then QQQ, IWM,
# TLT) and 2020-03-12 sent five, each of them one market event delivered as five
# or six separate interruptions.
#
# A CALENDAR DAY RATHER THAN A ROLLING TWENTY-FOUR HOURS, and the difference is
# legibility rather than arithmetic. A rolling window means the reader can never
# say when the next interruption becomes possible; a day means they can - the
# collector fills until midnight and the next one opens with the first bar after
# it. Priced at 26.9 pushes a year against 25.0 for the rolling window: the day
# is the shorter window on average, since an episode opening in the evening
# collects only the hours left in it.
#
# UTC, and that is the boundary worth having rather than the recipient's own
# midnight. Their midnight is 21:00 UTC, which lands in the busiest hour of the
# American session and would cut episodes in half; 00:00 UTC falls between the
# American close and the Asian open, which is as quiet as any hour gets.
def same_day(hour: int, other: int) -> bool:
    """Whether two hours fall in the same UTC day. The epoch begins at midnight,
    so the day is the quotient and no calendar is needed."""
    return int(hour) // 86400 == int(other) // 86400


def collapse(events: pd.DataFrame, channels: pd.Series) -> pd.Series:
    """Folds pushes that belong to one episode into the first of them.

    The first push of an episode interrupts immediately - it is news, and
    holding it back to see what else arrives would trade the only thing a push
    is for. Later pushes inside the window go to the digest instead of buzzing
    again.

    UNLESS THE LATER ONE IS RARER. A once-a-year move at ten o'clock must not
    silence a once-in-three-years move at one, or the window would invert the
    ladder, which is the one thing the collapse must never do. A rarer push
    interrupts and becomes the episode's new anchor, which is the same rule
    build_events applies within a single instrument when an event escalates
    inside its cooldown. Note what this rule is NOT: it never asks how many
    pushes have already gone out, only whether this one is the same event as
    the last.

    Chronological and greedy, which is the only honest way: a live system cannot
    hold this morning's alert back on the chance that something bigger arrives
    this afternoon, so neither does this.

    Returns the channels, which instruments each surviving push now speaks for,
    and - the other way round - which push each folded event belongs to. Both
    directions are needed now that the digest note is live: the folded events
    reach it within the hour, right under the push that already named them, and
    a row that cannot say which alert it belongs to reads as the same news
    arriving twice.
    """
    # Chronological, and within an hour the BLOCK first. A block push and the
    # members that made it up land on the same bar, and the block is the more
    # informative of the two - "the whole complex repriced, led by these three"
    # rather than "this one member moved and six others moved with it" - so it
    # takes the anchor and they fold under it. Nothing else about the rule
    # changes: a rarer push later still interrupts, block or not.
    from tremor.blocks import is_block

    order = events.assign(
        _block_first=events["asset_id"].map(is_block).map({True: 0, False: 1})
        if "asset_id" in events else 1
    ).sort_values(["hour_utc", "_block_first"], kind="mergesort").index
    tier = events["tier"]
    rank = {name: i for i, name in enumerate(severity.TIERS)}
    out = channels.copy()
    # WHICH other instruments each surviving push speaks for, not how many.
    # Without this the collapse would understate a crisis rather than merely
    # tidy it: on 2008-11-20 the reader would get one alert about the financial
    # sector and never learn the other six, which is the more important fact of
    # the two. Named rather than counted because "six others moved" tells you
    # something happened and nothing about what - and the whole point of the
    # basket is that WHICH instruments moved together is the diagnosis.
    ids = events["asset_id"] if "asset_id" in events else None
    folded: dict = {}
    belongs_to: dict = {}
    open_at: int | None = None
    open_rank = -1
    anchor = None
    for index in order:
        if out.get(index) != PUSH:
            continue
        hour = int(events.at[index, "hour_utc"])
        here = rank.get(tier.get(index), -1)
        if open_at is not None and same_day(hour, open_at) and here <= open_rank:
            out.at[index] = DIGEST
            # Not the anchor's own instrument. A second event on the SAME
            # instrument inside the window is the same move continuing, and
            # folding its id in made the message name itself: the SNB unpegging
            # read "Dollar / franc - biggest move in about three years ... with
            # Dollar / franc within the day". It is a companion list, and an
            # instrument is not its own companion.
            if anchor is not None and ids is not None:
                belongs_to[index] = str(ids.get(anchor, ""))
                if ids.get(index) != ids.get(anchor):
                    folded.setdefault(anchor, []).append(str(ids.get(index, "")))
            continue
        open_at, open_rank, anchor = hour, here, index

    # A space-separated string rather than a list column: it has to survive a
    # Parquet round trip and be read back by the delivery layer without either
    # side agreeing on a nested type.
    joined = pd.Series("", index=events.index, dtype="object")
    for key, names in folded.items():
        joined.at[key] = " ".join(dict.fromkeys(n for n in names if n))
    anchors = pd.Series("", index=events.index, dtype="object")
    for key, name in belongs_to.items():
        anchors.at[key] = name
    return out, joined, anchors


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
    """Adds `channel` and, for the digested ones, the note they belong to."""
    if events.empty:
        return events.assign(channel=pd.Series(dtype="string"),
                             digest_slot=pd.Series(dtype="Int64"),
                             folded_into=pd.Series(dtype="string"))

    channels, folded, anchors = collapse(events, channel(events))
    digested = channels.eq(DIGEST).fillna(False).to_numpy(dtype=bool)
    slots = pd.Series(pd.NA, index=events.index, dtype="Int64")
    if digested.any():
        slots.loc[digested] = pd.array(
            [digest_slot(h) for h in events.loc[digested, "hour_utc"]], dtype="Int64")
    return events.assign(channel=channels, digest_slot=slots,
                         also_moved=folded.astype("string"),
                         folded_into=anchors.astype("string"))


def summarise(routed: pd.DataFrame) -> pd.DataFrame:
    """Counts by channel and tier, for reporting what the settings actually do."""
    if routed.empty:
        return pd.DataFrame()
    table = pd.crosstab(routed["tier"], routed["channel"])
    return table.reindex(index=[t for t in severity.TIERS if t in table.index],
                         columns=[c for c in (PUSH, DIGEST) if c in table.columns],
                         fill_value=0)

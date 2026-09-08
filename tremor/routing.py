"""Who gets interrupted, who waits for Friday, and who is not worth a line.

The tier says how rare a move was. It does not say whether the message should
make a phone buzz, and those are different questions - deliberately so. Rarity
is a property of the instrument and its history, and it means the same thing
whether the system watches five instruments or fifty. How often someone is
willing to be interrupted is a property of the person, and it does not grow
when the watchlist does. Keeping them apart is what stops the alert rate from
tripling the day three stocks are added.

The routing is the second half of the answer to a fair complaint about the old
detector: not all big movements stay. A move that gives everything back within
a few hours is not news, and there is a cheap way to know which ones did -
wait and look (see tremor.persistence). So the channels differ in urgency, and
the ones that are not urgent spend their delay earning the right to be sent:

  push      both push tiers, sent AT ONCE and then corrected in place. A
            once-a-year move that arrives six hours late is a worse product
            than one that arrives now and is marked "reverted" later, so the
            message is edited at two, six and twenty-four bars with how the
            move actually held (see price_monitor.tremor_delivery). The
            retention check did not go away - it moved from deciding whether
            to send to deciding what the sent message says.
  digest    everything else that held, batched into the next Tuesday or Friday
            note. Nothing here is urgent by construction, so the full
            twenty-four-bar answer is available before it is written.
  dropped   the move reverted. Not a failure of the detector - it correctly
            found an unusual move - but not something to spend a line on.

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
DROPPED = "dropped"

# The tiers that interrupt. Both go at once; neither waits for retention.
PUSH_IMMEDIATE_TIER = "extreme"
PUSH_DELAYED_TIER = "major"          # kept as a name; no longer delayed
PUSH_TIERS = (PUSH_DELAYED_TIER, PUSH_IMMEDIATE_TIER)
DELAY_HORIZON = persistence.BAR_HORIZONS[0]
SETTLED_HORIZON = persistence.SETTLED        # the next trading close

# datetime.weekday(): Monday=0. Tuesday covers the weekend and Monday - which
# matters, because crypto trades straight through it and equities gap on the
# Monday open - and Friday closes the trading week before the weekend.
DIGEST_WEEKDAYS = (1, 4)
DIGEST_HOUR_LOCAL = 12
DIGEST_TZ = ZoneInfo("Asia/Jerusalem")


def channel(events: pd.DataFrame, require_retention: bool = True) -> pd.Series:
    """The delivery channel for each event, before the rate limit.

    Retention that is not yet known is not treated as a reversal. An event
    whose horizon has not elapsed - every event the live system has just
    produced - has not failed the check, it has not taken it, and it waits for
    the next digest rather than being dropped.

    `require_retention` is off for streams the test does not apply to. Price
    impact splits into a permanent part and a transitory one, which is what the
    check measures; a volatility regime is not a price move and does not split
    that way - a spike that subsided within the day was still a real spike, and
    the market really was disorderly while it lasted. Left on, the check would
    have nothing to read, and every market event would silently fall to the
    digest for failing a test it was never given.
    """
    if events.empty:
        return pd.Series(dtype="string")

    tier = events["tier"]
    if not require_retention:
        out = pd.Series(DIGEST, index=events.index, dtype="string")
        pushes = tier.isin([PUSH_IMMEDIATE_TIER, PUSH_DELAYED_TIER])
        return out.mask(pushes.fillna(False).to_numpy(dtype=bool), PUSH)

    # Every condition is reduced to a plain bool before it reaches mask().
    # Series.mask treats a pandas NA in the condition as True, so an unknown
    # retention - which is every event the live system has just produced, since
    # the horizon has not elapsed - was being read as "it reverted" and dropped.
    reverted = persistence.held(events, SETTLED_HORIZON).eq(False).fillna(False)
    pushes = tier.isin(PUSH_TIERS).fillna(False).to_numpy(dtype=bool)

    out = pd.Series(DIGEST, index=events.index, dtype="string")
    # Dropping a reverted move applies to the DIGEST tiers only. Nothing there
    # is urgent, so its retention is known long before it would be written up
    # and a move that gave everything back need never take a line. A push has
    # already gone out by the time that answer exists, and unsending it is not
    # a thing Telegram can do - the follow-up edit says so instead.
    out = out.mask(reverted.to_numpy(dtype=bool) & ~pushes, DROPPED)
    out = out.mask(pushes, PUSH)
    return out


# How long one push speaks for. A second instrument moving inside this window is
# almost always the same event seen again rather than news: measured over the
# whole record, 2008-11-20 sent six pushes across two hours (SPY, XLF, USO, then
# QQQ, IWM, TLT) and 2020-03-12 sent five, each of them one market event
# delivered as five or six separate interruptions. Twenty-four hours because that
# is the span over which a person reads a move as "still the same thing" - the
# per-asset cooldown of §8.3 makes the same judgement one instrument at a time,
# and this is that judgement across the portfolio.
COLLAPSE_HOURS = 24


def collapse(events: pd.DataFrame, channels: pd.Series,
             hours: int = COLLAPSE_HOURS) -> pd.Series:
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

    Returns the channels and, beside them, which instruments each surviving
    push now speaks for.
    """
    order = events["hour_utc"].sort_values().index
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
    open_at: int | None = None
    open_rank = -1
    anchor = None
    for index in order:
        if out.get(index) != PUSH:
            continue
        hour = int(events.at[index, "hour_utc"])
        here = rank.get(tier.get(index), -1)
        if (open_at is not None and hour - open_at < hours * 3600
                and here <= open_rank):
            out.at[index] = DIGEST
            # Not the anchor's own instrument. A second event on the SAME
            # instrument inside the window is the same move continuing, and
            # folding its id in made the message name itself: the SNB unpegging
            # read "Dollar / franc - biggest move in about three years ... with
            # Dollar / franc within the day". It is a companion list, and an
            # instrument is not its own companion.
            if (anchor is not None and ids is not None
                    and ids.get(index) != ids.get(anchor)):
                folded.setdefault(anchor, []).append(str(ids.get(index, "")))
            continue
        open_at, open_rank, anchor = hour, here, index

    # A space-separated string rather than a list column: it has to survive a
    # Parquet round trip and be read back by the delivery layer without either
    # side agreeing on a nested type.
    joined = pd.Series("", index=events.index, dtype="object")
    for key, names in folded.items():
        joined.at[key] = " ".join(dict.fromkeys(n for n in names if n))
    return out, joined


def digest_slot(hour_utc: int) -> int:
    """The send time of the first digest strictly after this hour.

    Local time, because the recipient reads it in local time and a digest that
    lands at 04:00 twice a week is one nobody opens. Whether that is 12:00 UTC
    or 13:00 depends on daylight saving, and letting the zone decide is why
    this is not arithmetic on the timestamp.
    """
    moment = datetime.fromtimestamp(int(hour_utc), tz=timezone.utc).astimezone(DIGEST_TZ)
    for ahead in range(0, 9):
        day = (moment + timedelta(days=ahead)).date()
        if day.weekday() not in DIGEST_WEEKDAYS:
            continue
        slot = datetime.combine(day, time(DIGEST_HOUR_LOCAL), tzinfo=DIGEST_TZ)
        if slot > moment:
            return int(slot.astimezone(timezone.utc).timestamp())
    raise RuntimeError("no digest slot within nine days")


def route(events: pd.DataFrame, require_retention: bool = True) -> pd.DataFrame:
    """Adds `channel` and, for the digested ones, the slot they belong to."""
    if events.empty:
        return events.assign(channel=pd.Series(dtype="string"),
                             digest_slot=pd.Series(dtype="Int64"))

    channels, folded = collapse(events, channel(events, require_retention))
    digested = channels.eq(DIGEST).fillna(False).to_numpy(dtype=bool)
    slots = pd.Series(pd.NA, index=events.index, dtype="Int64")
    if digested.any():
        slots.loc[digested] = pd.array(
            [digest_slot(h) for h in events.loc[digested, "hour_utc"]], dtype="Int64")
    return events.assign(channel=channels, digest_slot=slots,
                         also_moved=folded.astype("string"))


def summarise(routed: pd.DataFrame) -> pd.DataFrame:
    """Counts by channel and tier, for reporting what the settings actually do."""
    if routed.empty:
        return pd.DataFrame()
    table = pd.crosstab(routed["tier"], routed["channel"])
    return table.reindex(index=[t for t in severity.TIERS if t in table.index],
                         columns=[c for c in (PUSH, DIGEST, DROPPED)
                                  if c in table.columns], fill_value=0)

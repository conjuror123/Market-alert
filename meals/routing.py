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
wait and look (see meals.persistence). So the channels differ in urgency, and
the ones that are not urgent spend their delay earning the right to be sent:

  push      the rarest tier, sent at once. It cannot wait for the retention
            check, and should not: a once-in-three-years move in an instrument
            is worth knowing about while it is happening even if it turns out
            to have been liquidity. Roughly seven a year.
  push      a once-a-year move, sent six bars later and only if it is still
  (delayed)  standing. Six bars is hours, not days, so the news is not stale,
            and it is worth the wait: of the events still standing at six
            bars, 72% were still standing at twenty-four, against 32% of those
            that had already given it back.
  digest    everything else that held, batched into the next Tuesday or Friday
            note. Nothing here is urgent by construction, so the full
            twenty-four-bar answer is available before it is written.
  dropped   the move reverted. Not a failure of the detector - it correctly
            found an unusual move - but not something to spend a line on.

The rate limit is a backstop, not the mechanism. It exists because the push
rate is otherwise proportional to the number of instruments watched, and the
recipient's attention is not. It runs chronologically and greedily, which is
the only honest way: a live system deciding whether to send at noon on Tuesday
cannot know whether something rarer is coming on Thursday.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from meals import persistence, severity

PUSH = "push"
DIGEST = "digest"
DROPPED = "dropped"

# Sent at once, without waiting to see whether it held.
PUSH_IMMEDIATE_TIER = "extreme"
# Sent after the short retention check, and only if it passes it.
PUSH_DELAYED_TIER = "major"
DELAY_HORIZON = persistence.HORIZONS[0]      # 6 bars
SETTLED_HORIZON = persistence.HORIZONS[-1]   # 24 bars

# At most this many pushes in any rolling seven days. On the current basket the
# rules above produce about one every three weeks, so this never binds - which
# is the point of a backstop. It binds the day the watchlist doubles.
MAX_PUSHES_PER_WEEK = 2

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
    standing = persistence.held(events, DELAY_HORIZON).eq(True).fillna(False)

    out = pd.Series(DIGEST, index=events.index, dtype="string")
    out = out.mask(reverted.to_numpy(dtype=bool), DROPPED)
    out = out.mask((tier.eq(PUSH_DELAYED_TIER).fillna(False) & standing)
                   .to_numpy(dtype=bool), PUSH)
    out = out.mask(tier.eq(PUSH_IMMEDIATE_TIER).fillna(False).to_numpy(dtype=bool),
                   PUSH)
    return out


def rate_limit(events: pd.DataFrame, channels: pd.Series,
               cap: int = MAX_PUSHES_PER_WEEK) -> pd.Series:
    """Demotes pushes beyond `cap` in any rolling week to the digest.

    Chronological and greedy, because that is what a live system can do. It
    cannot hold Tuesday's alert back on the chance that something rarer arrives
    on Thursday, so neither does this - a backtest that reserved the budget for
    the best event of the week would be measuring a system nobody can build.

    The rarest tier is never demoted, only counted. Greedy and chronological
    means a Monday once-a-year move would otherwise spend the budget that a
    Wednesday once-in-three-years move needed, which inverts the whole ladder
    to save a message: measured, that silenced five of thirty-nine extremes.
    It still consumes budget, because it consumes attention.
    """
    order = events["hour_utc"].sort_values().index
    tier = events["tier"]
    out = channels.copy()
    sent: list[int] = []
    for index in order:
        if out.get(index) != PUSH:
            continue
        hour = int(events.at[index, "hour_utc"])
        window = hour - 7 * 24 * 3600
        sent = [h for h in sent if h > window]
        if len(sent) >= cap and tier.get(index) != PUSH_IMMEDIATE_TIER:
            out.at[index] = DIGEST
        else:
            sent.append(hour)
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
    ladder exactly the way the weekly cap is careful not to. A rarer push
    interrupts and becomes the episode's new anchor, which is the same rule
    build_events applies within a single instrument when an event escalates
    inside its cooldown.

    Chronological and greedy for the reason rate_limit is: a live system cannot
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
            if anchor is not None and ids is not None:
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


def route(events: pd.DataFrame, cap: int = MAX_PUSHES_PER_WEEK,
          require_retention: bool = True) -> pd.DataFrame:
    """Adds `channel` and, for the digested ones, the slot they belong to."""
    if events.empty:
        return events.assign(channel=pd.Series(dtype="string"),
                             digest_slot=pd.Series(dtype="Int64"))

    # Collapse BEFORE the weekly cap: the cap exists to ration attention, and
    # spending it on six views of one event is exactly what it should not do.
    channels, folded = collapse(events, channel(events, require_retention))
    channels = rate_limit(events, channels, cap)
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

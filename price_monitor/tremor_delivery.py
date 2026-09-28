"""Delivers Tremor events to Telegram: the pushes, and the weekly note.

The detector decides everything about WHAT to say - the word, which channel an
event belongs to, when it was found (tremor.jumps, tremor.routing). This module
decides what is on the channel because of it, and keeps that in line.

THE CHANNEL IS PUBLIC and the bot is an administrator of it: it can edit any of
its messages at any age and delete any message there.

TWO KINDS OF MESSAGE, and the difference is how loudly they arrive. A push -
`high` and up - is its own message and rings. A `noticeable` move is a row in
the week's note, which is edited in place and so stays silent; a small ping
beneath it rings instead and points up at it.

ONE NOTE A WEEK, opened Sunday 00:05 UTC right after the economic calendar's own
message (weekly_digest.py), and curated for that week: every run re-reads the
events table and brings every message of the week in line with it - see "the
week" below for exactly what that means. What belongs to an earlier note is
history and is never touched.

A MOVE IS SENT ONLY WITHIN 24 HOURS OF BEING FOUND. After that it may still be
corrected or removed, but nothing new rings for it. This is load-bearing: the
events table holds the whole history, so without it the first run after a mute
would deliver years of alerts at once. An EMPTY events table changes nothing at
all: it cannot tell "nothing happened" from "the pipeline did not run".

THE MUTE (Config.tremor_alerts_muted) lives in config.yaml, so "we are
deliberately silent" is visible where the code is.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pandas as pd

from price_monitor import economic_calendar
from price_monitor.config import Config
from price_monitor.notifier import (TelegramError, edit_telegram_message,
                                    send_telegram_message)
from price_monitor.state import save_state

log = logging.getLogger("price_monitor.tremor_delivery")

# Telegram rejects a message over 4096 characters outright rather than
# truncating it, so the digest is cut into parts. The headroom covers the
# "part N of M" line, which would otherwise have to be counted recursively.
_MESSAGE_LIMIT = 4000

STATE_KEY = "tremor_delivery"

# How rare the move was, as a colour. A SQUARE, against the circles the economic
# calendar uses for a release's impact (economic_calendar.IMPACT_EMOJI): the same
# four hues carry the same "how much should I care", and the shape says which
# kind of thing the line is - something the market did, or something that was on
# the schedule - without the reader having to read the words first.
#
# Ordered like the words themselves, so a note sorted by word is also sorted by
# colour, and a long note can be skimmed down its left edge.
TIER_EMOJI = {"noticeable": "⬜", "high": "🟨", "major": "🟧", "extreme": "🟥"}


# Marks the timestamp footer. The hour is the last thing on the line rather
# than the first because it is what a reader checks last - everything above it
# is what happened, and this is when.
TIME_EMOJI = "\U0001f550 "


def format_day(when: datetime) -> str:
    """A calendar day as 14.09.2026. The same shape on every line that names one."""
    return when.strftime("%d.%m.%Y")


def _overnight(event: dict) -> bool:
    """Whether the overnight gap claimed this event rather than an hour's move.

    Then `r` is the gap - last close to first print - and `sigma_lt` the
    half-year σ of that kind of gap (tremor.jumps).
    """
    flag = event.get("overnight")
    try:
        return bool(flag) and not pd.isna(flag)
    except (TypeError, ValueError):
        return bool(flag)


def _weekly(event: dict) -> bool:
    """A currency pair's gap is the WEEKEND - Friday 17:00 to Sunday 17:00 New
    York - and saying "overnight" or "the open" of it would send the reader to
    the wrong chart."""
    return str(event.get("block") or "") == "FX"


def _gap_kind(event: dict) -> str:
    """What kind of close came before a gap: "night" or "weekend".

    Carried on the row by tremor.jumps, because a gap is judged against the
    earlier gaps of THAT kind - a weekend against weekends. A row without it
    falls back to what it must have been.
    """
    kind = event.get("gap_kind")
    if isinstance(kind, str) and kind:
        return kind
    return "weekend" if _weekly(event) else "night"


def _open_words(event: dict) -> "tuple[str, str]":
    """(when it opened, what kind of gap) - in the reader's words."""
    if _weekly(event):
        return "at the weekly open", "weekend gap"
    kind = _gap_kind(event)
    if kind == "weekend":
        return "at the open after the weekend", "weekend gap"
    return "at the open", "overnight gap"


@lru_cache(maxsize=1)
def _tickers() -> dict:
    """asset_id -> ticker, from the basket definition, read once per process."""
    try:
        from tremor.basket import load_basket

        return {a.asset_id: a.ticker for a in load_basket().instruments}
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("Could not read the basket composition: %s", exc)
        return {}


def _ticker(asset_id: str) -> str:
    return _tickers().get(asset_id) or str(asset_id).split(":")[-1]




def _escape(text: str) -> str:
    """The message goes out with parse_mode=HTML.

    Instrument labels come from config rather than an external feed, so this is
    belt and braces - but one unescaped "&" is enough for Telegram to reject a
    whole message, and the digest would then simply never arrive.
    """
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _labels() -> dict[str, str]:
    """asset_id -> human label, from the basket definition.

    Falls back to the ticker embedded in the id if the basket cannot be read: a
    message naming BTC-USD instead of Bitcoin is worse than one that is never
    sent, but only slightly, and this should not be the thing that stops an
    alert going out.
    """
    try:
        from tremor.basket import load_basket

        return {a.asset_id: a.label for a in load_basket().instruments}
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("Could not read basket labels: %s", exc)
        return {}


def _calendar(cfg: Config) -> "list[dict] | None":
    """The economic calendar archive, or None if it cannot be read.

    A push must not be lost because the calendar is missing: the context is an
    addition to the message, and an alert without it is far better than no
    alert at all.
    """
    try:
        return economic_calendar.load_events(
            economic_calendar.store_path(cfg.calendar_dir))
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("calendar could not be read, sending without context: %s", exc)
        return None


def load_events(cfg: Config) -> "list[dict]":
    """Every routed event as a plain dict.

    Returns an empty list rather than raising when the parquet file is absent.
    It is produced by python -m tremor.jumps, and the hourly monitoring run must
    not fall over because a pipeline step has not been run yet.
    """
    paths = [cfg.tremor_events_path]
    if not any(os.path.exists(p) for p in paths):
        return []

    try:
        import pandas as pd
    except ImportError:                          # pragma: no cover - defensive
        log.warning("pandas is not available; Tremor delivery skipped")
        return []

    rows: list[dict] = []
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:                 # pragma: no cover - defensive
            log.warning("Could not read %s: %s", path, exc)
            continue
        if frame.empty or "channel" not in frame.columns:
            continue
        rows.extend(frame.to_dict("records"))
    return rows


def _clean(value) -> "float | None":
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number   # NaN check without numpy


def _sigma_multiple(event: dict) -> str:
    """A jump's size, " · 11.0×σ": |move| over its half-year σ (tremor.jumps),
    always to one decimal, at the end of the first line. Empty when either is
    missing."""
    move = _clean(event.get("r"))
    usual = _clean(event.get("sigma_lt"))
    if move is None or usual is None or usual <= 0:
        return ""
    return f" · {abs(move) / usual:.1f}×σ"


def describe(event: dict, labels: dict[str, str]) -> str:
    """One instrument's move, as it appears in a push or a note row.

    THE SAME ACCOUNT EVERYWHERE. A push and a row in the weekly note are the
    same kind of thing seen at different volumes: what moved, how far, and how
    big that is against the instrument's own half-year (tremor.jumps). The
    colour of the square is the word - noticeable, high, major, extreme - so
    the word is not written out.

    Only what the detector measures is said. The "biggest since" date, how the
    move held at the next close and the block's share of it come back as the
    stages that measure them do.
    """
    tier = str(event.get("tier") or "noticeable")
    emoji = TIER_EMOJI.get(tier, "⚪")
    when = datetime.fromtimestamp(int(event["hour_utc"]), tz=timezone.utc)

    asset_id = str(event.get("asset_id", ""))
    label = labels.get(asset_id) or asset_id.split(":")[-1]
    move = _clean(event.get("r"))
    # The ticker leads: it is what the reader types into a chart and the only
    # name that is the same everywhere. The move shares that line, because it is
    # the first thing anyone wants, and its size in σ ends it.
    shown = f" {move * 100:+.2f}%" if move is not None else ""
    if shown and _overnight(event):
        # The gap is a price move from the last close to the first print, and
        # it is said as one - never as the hour it was scored alongside.
        shown += f" {_open_words(event)[0]}"
    shown += _sigma_multiple(event)
    return "\n".join([f"{emoji} <b>{_escape(_ticker(asset_id))}</b> · "
                      f"{_escape(label)}{shown}",
                      f"{TIME_EMOJI}<b>{format_day(when)} {when:%H:%M} UTC</b>"])


# --- the regime the move happened in ----------------------------------------
#
# VIX is the one thing in this system that is not about a single instrument. It
# is the price of protection on the S&P 500, so it says what the market as a
# whole expected of the near future - and "SPY fell 1.8%" reads completely
# differently at a VIX of 13 and at a VIX of 38.
#
# It has been computed since the beginning and shown to nobody. The daily FRED
# series feeds a stress multiplier that raises the weight of clustered moves for
# a day after a spike, and that multiplier feeds the SI-Index, which feeds the
# cluster channel, which is not delivered. So the whole of it has been invisible.
# This is where it becomes a line in a message.
#
# WHY THE DAILY INDEX AND NOT AN HOURLY PRODUCT. Twelve Data does not carry the
# VIX index at all, and the tradable futures ETF that tracks it does not stand in
# for it - see config/basket.yaml for the measurements. What a reader wants here
# is the regime, and a regime is slow: an index that updates
# once a day and reaches back to 1990 describes it better than a decaying
# futures product that starts in 2011.
#
# The file is named for FRED because that is where its history came from; it
# holds the union of FRED and CBOE's own daily file (see tremor.cboe). CBOE
# posts the close the same evening, which is what keeps the gauge from sitting
# three calendar days behind across a weekend.
VIX_PATH = os.path.join("data", "tremor", "vix", "fred_VIXCLS.parquet")



@lru_cache(maxsize=1)
def _vix_scored() -> "pd.DataFrame | None":
    """The VIX series with the spike test already applied, read once per process.

    Carries `available_at` - the moment the value became KNOWN, which is the
    evening of the observation for a day CBOE served and one to two business
    days later for a day only FRED had. Every reading below is chosen by that
    column and not by the observation date, so a message about Monday's move
    never quotes a number that did not exist until Wednesday.
    """
    try:
        from tremor import vix as vix_module

        series = pd.read_parquet(VIX_PATH).sort_values("day").reset_index(drop=True)
        return vix_module.score(series)
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("Could not read the VIX series: %s", exc)
        return None


def _stress_open_since(scored: "pd.DataFrame", hour_utc: int) -> "int | None":
    """When the stress episode covering this hour began, if one does.

    The multiplier's window is twenty-four REFERENCE hours long - the hours the
    basket's anchor exchange is open - rather than twenty-four clock hours, so a
    Friday spike is still live on Monday morning. Counted by walking those hours
    forward from the spike, which is at most a few dozen steps, rather than by
    materialising every reference hour since 1990.
    """
    from tremor import sessions, windows as w

    known = scored[scored["is_spike"].fillna(False)
                   & (scored["available_at"] <= hour_utc)]
    if known.empty:
        return None
    opened = int(known["available_at"].iloc[-1])

    counted, hour = 0, opened
    while counted < w.VIX_WINDOW:
        hour += 3600
        if hour > hour_utc:
            return opened                        # still inside the window
        if sessions.is_reference_hour(hour, "America/New_York"):
            counted += 1
    return None


def vix_context(hour_utc: int) -> str:
    """Where fear stood when this happened, as the message says it.

    Silent when there is no reading the system could have had at that hour - a
    young archive, or an unreadable file - because a regime line that guesses is
    worse than no regime line.
    """
    scored = _vix_scored()
    if scored is None or scored.empty:
        return ""
    hour_utc = int(hour_utc)
    known = scored[scored["available_at"] <= hour_utc]
    if known.empty:
        return ""

    latest = known.iloc[-1]
    level, day = float(latest["close"]), int(latest["day"])

    # Where the level sits in its own history, which is the only form of this
    # number a reader can do anything with: 16 and 54 are both just numbers
    # until one of them is "calmer than three days in five" and the other is
    # "higher than all but one day in a hundred". Computed on the readings KNOWN
    # at this hour, like everything else here.
    rank = float((known["close"] <= level).mean()) * 100
    first = _vix_since(known)
    place = (f"the highest it has been since {first}" if rank >= 99.995 else
             f"higher than {rank:.0f}% of days since {first}" if rank >= 50 else
             f"calmer than {100 - rank:.0f}% of days since {first}")
    lines = [f"🌡 <b>Fear gauge VIX</b>: {level:.2f} 1 day ago - {place}"]

    earlier = known.iloc[:-1]
    if not earlier.empty:
        before = float(earlier["close"].iloc[-1])
        lines.append(f"     {before:.2f} 2 days ago")
        week = _vix_close_days_before(known, day, 7)
        if week is not None and int(week["day"]) != int(earlier["day"].iloc[-1]):
            lines.append(f"     {float(week['close']):.2f} 7 days ago")

    since = _stress_open_since(scored, hour_utc)
    if since is not None:
        began = datetime.fromtimestamp(int(since), tz=timezone.utc)
        lines.append("     a jump that large counts as a stress episode - this one "
                     f"has been running since {format_day(began)}")
    return "\n".join(lines)


def _vix_close_days_before(known: "pd.DataFrame", latest_day: int, days: int):
    """The last close whose observation day is at least `days` before `latest_day`."""
    cutoff = int(latest_day) - int(days) * 86400
    older = known[known["day"] <= cutoff]
    if older.empty:
        return None
    return older.iloc[-1]


def _vix_since(known: "pd.DataFrame") -> int:
    """The first year the percentile is taken over, so the claim can be checked."""
    return datetime.fromtimestamp(int(known["day"].iloc[0]), tz=timezone.utc).year


# How far back to look for scheduled news when a push goes out. Three hours
# because that is long enough to cover a release the instrument was still
# digesting and short enough that what it names is plausibly the cause;
# measured over every push in the record, a three-hour window holds a median of
# zero high-impact events and three at the ninetieth percentile, so the line
# stays readable.
CALENDAR_LOOKBACK_HOURS = 2
# And an hour AFTER: a release five minutes after the hour closed is a cause,
# not a coincidence, and a window ending where the move does excludes precisely
# the releases a reader would blame first.
#
# This costs no waiting. The archive is a SCHEDULE, not a log: it carries the
# releases announced ahead of time, currently a few hundred of them reaching
# weeks into the future. So the hour after a move is already known when the
# push is written, and the line is complete in the first message rather than
# arriving with a later edit.
CALENDAR_LOOKAHEAD_HOURS = 1

# High and Medium, the same two the weekly calendar shows, and each carries
# its colour. Low is excluded everywhere for the same reason: it is dominated by
# bank holidays and minor prints, and naming those would turn the most important
# line of the most important message into noise.
#
# Medium earns its place without flooding the line: over the record the
# three-hour window holds a median of two High-or-Medium releases against one
# High alone, six at the ninetieth percentile against four, and fourteen at the
# worst against twelve. 24% of events still have nothing scheduled around them.
#
# All of them are listed rather than capped: "and two more" would hide the tail
# of a busy morning, which on a busy morning is the half worth reading.
CALENDAR_IMPACTS = economic_calendar.SHOWN_IMPACTS


def calendar_context(hour_utc: int, calendar: "list[dict] | None") -> str:
    """What was scheduled around the move - before it and just after.

    Naming the release tells the reader the move has a known cause and they can
    stop looking for one. NOTHING IS PRINTED WHEN NOTHING WAS SCHEDULED. 55% of
    pushes have no Medium or High release in the window, so a "none scheduled"
    line would appear on more than half of all messages - and a line that usually
    says nothing stops being read, taking the half that does say something with
    it. Silence carries the same fact in no space at all.
    """
    # An empty archive is not evidence of a quiet three hours: it cannot tell
    # "nothing was scheduled" from "nothing was loaded", and only one of those
    # is safe to print. None and [] are both treated as "no calendar".
    if not calendar:
        return ""
    moment = datetime.fromtimestamp(int(hour_utc), tz=timezone.utc)
    lower = moment - timedelta(hours=CALENDAR_LOOKBACK_HOURS)
    upper = moment + timedelta(hours=CALENDAR_LOOKAHEAD_HOURS)
    try:
        window = economic_calendar.events_in_window(calendar, lower, upper)
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("calendar context unavailable: %s", exc)
        return ""

    named = [e for e in window if str(e.get("impact")) in CALENDAR_IMPACTS]
    if not named:
        return ""

    # The span as an offset pair rather than a sentence. It is the same fact in
    # a fifth of the width, and the width matters here: this header sits above
    # a list on a phone, where the sentence wrapped onto a second line and the
    # events themselves were pushed down the message.
    header = (f"Nearby economic events "
              f"(-{CALENDAR_LOOKBACK_HOURS}h+{CALENDAR_LOOKAHEAD_HOURS}h):")
    named.sort(key=lambda e: str(e.get("date") or ""))
    lines = [header]
    for e in named:
        colour = economic_calendar.IMPACT_EMOJI.get(str(e.get("impact")), "")
        country = economic_calendar.country_label(e.get("country"))
        title = str(e.get("title") or "").strip()
        lines.append(f"     {colour} {country} {title}".rstrip())
    return "\n".join(lines)


def format_push(event: dict, labels: dict[str, str],
                calendar: "list[dict] | None" = None) -> str:
    """A single interrupting alert.

    Ordered so the reader meets one instrument first and the day second: the
    move written out in full, then the news scheduled around it.

    ONE INSTRUMENT, and only one. A push is final when it arrives - nothing is
    folded into it - so each is its own story and the day assembles itself out
    of however many arrive. The fear gauge lives on the weekly note rather than
    here: a standalone alert is already one instrument's story, and the running
    note carries the regime once for all of them.
    """
    lines = [describe(event, labels)]
    context = calendar_context(int(event["hour_utc"]), calendar)
    if context:
        lines.append("")
        lines.append(_escape(context))
    return "\n".join(lines)




def format_digest(events: "list[dict]", labels: dict[str, str],
                  window: "tuple[int, int]",
                  calendar: "list[dict] | None" = None,
                  now: datetime | None = None) -> "list[str]":
    """One note, whole, split into parts Telegram will accept.

    ORDERED BY TIME, and by rarity only inside an hour. A note is a record, so a
    period read top to bottom runs in the order it happened; leading with the
    rarest row would buy nothing, because the note does not notify - the ping
    does. Two moves in the same hour are the one case time cannot separate, and
    there the rarer goes first.

    The order runs ACROSS the parts, not within each. A long note is cut into
    several messages, and sorting each part on its own would restart the clock
    at every cut - so the rows are ordered once and the cut falls wherever the
    character budget runs out.

    The header states the period the note speaks for, Sunday to Saturday.
    """
    from tremor.jumps import WORDS as TIERS

    now = now or datetime.now(timezone.utc)
    start, end = int(window[0]), int(window[1])
    opened = datetime.fromtimestamp(start, tz=timezone.utc)
    closes = datetime.fromtimestamp(end, tz=timezone.utc)
    live = now.timestamp() < end

    rank = {name: i for i, name in enumerate(TIERS)}
    ordered = sorted(events, key=lambda e: (int(e["hour_utc"]),
                                            -rank.get(str(e.get("tier")), 0)))
    if ordered:
        count = (f"{len(ordered)} event{'s' if len(ordered) != 1 else ''}"
                 + (" so far" if live else ""))
    else:
        count = "Nothing so far" if live else "Nothing in this period"
    # THE LAST DAY THE NOTE CAN HOLD ANYTHING, not the moment it stops. A note
    # runs to the instant the next one opens, Sunday 00:05 - so it technically
    # reaches into the next Sunday by five minutes and would print "Sun 13 to
    # Sun 20", handing that Sunday to a note that carries none of it.
    #
    # An hour back rather than a second, and the hour is the unit that makes it
    # true rather than merely nicer: the note is a list of hourly bars, so a
    # stretch shorter than an hour cannot contain one, and naming that day claims
    # something the note is unable to have.
    last = closes - timedelta(hours=1)
    header = (f"📋 <b>Digest</b> - {format_day(opened)} to {format_day(last)}\n"
              + count + (" - this message is updated as moves are found" if live else ""))
    # The regime the whole period sits in, read at the note's latest edit rather
    # than at its opening: a note is re-rendered every time a row is added, so
    # while it is live this tracks the market, and once the period closes it
    # freezes at the last reading inside it.
    regime = vix_context(int(min(now.timestamp(), end)))
    if regime:
        header += "\n" + regime

    def row(event: dict) -> str:
        line = describe(event, labels)
        context = calendar_context(int(event["hour_utc"]), calendar)
        return f"{line}\n     {_escape(context)}" if context else line

    messages, current = [], header
    for text in [row(e) for e in ordered]:
        candidate = f"{current}\n\n{text}"
        if len(candidate) > _MESSAGE_LIMIT and current != header:
            messages.append(current)
            current = text
        else:
            current = candidate
    messages.append(current)

    if len(messages) > 1:
        # THE PART MARKER NAMES ITS NOTE. Only the first part carries the
        # header, so a later part read on its own would be a wall of rows
        # ending in "part 2 of 3" with nothing saying of what.
        total = len(messages)
        period = f"{format_day(opened)} to {format_day(last)}"
        messages = [f"{m}\n\n<i>part {i} of {total} - {period}</i>"
                    for i, m in enumerate(messages, 1)]
    return messages


def format_ping(event: dict, labels: dict[str, str]) -> str:
    """The throwaway line that says a digest row just appeared.

    A digest row is written the hour its move is found, but the note stays
    silent - Telegram does not notify on an edit - so a reader who wants to know
    NOW has to keep opening it. This is the buzz: ticker, name, size, and a
    pointer at the note. The calendar context stays in the note, one tap away.

    It lives exactly as long as its row: deleted when the row leaves the note,
    when the move becomes a push, and when the next note opens - so what
    remains is a clean run of notes rather than a scroll of pings around them.
    """
    tier = str(event.get("tier") or "noticeable")
    emoji = TIER_EMOJI.get(tier, "⚪")
    asset_id = str(event.get("asset_id", ""))
    move = _clean(event.get("r"))
    shown = f" {move * 100:+.2f}%" if move is not None else ""
    ticker = _ticker(asset_id)
    label = labels.get(asset_id) or ticker
    first = (f"{emoji} <b>{_escape(ticker)}</b> · {_escape(label)}"
             f"{shown}{_sigma_multiple(event)}")
    return f"{first}\nAdded to digest👆🏻👆🏻"

# --- the week ------------------------------------------------------------------
#
# ONE NOTE A WEEK, opened Sunday 00:05 UTC (tremor.routing) just after the
# economic calendar's own message. For that week every message the bot sent is
# kept in line with the events table, every run; anything from before the note
# opened is history and is never touched again.
#
# The week's state is the only thing remembered: which pushes and which note rows
# are on the channel, their pings, and the note's message ids. What each run does
# with them, in order:
#
#   pushes on the channel   event gone -> deleted. A rarer word within 24 hours of
#                           the move being found -> a new push rings and the old
#                           one is deleted. Anything else -> edited in place (a
#                           push whose word fell to `noticeable` shows ⬜).
#   rows in the note        event gone -> the row leaves (an edit) and its ping is
#                           deleted. A push word within 24 hours -> the push rings,
#                           the row and its ping go. Past 24 hours -> the row stays
#                           with its new colour, and so does its ping.
#   new events              within 24 hours of being found only: `high` and up
#                           push, `noticeable` becomes a row with a ping - unless
#                           its day already has a push on the channel.
#   the day                 an instrument-day keeps one push, its rarest: the lower
#                           ones are deleted. A day with a push shows no rows.
#
# A detector update (a new tremor.jumps.detector_version) starts the week over at
# that run: every push and ping of the week is deleted, the note stays and shows
# only what is found from then on.
#
# Deleting is how a message leaves; the bot is an administrator of a public
# channel and may delete any message there. Should Telegram refuse, the message
# is struck through by an edit instead.

WEEK = "week"

# A push goes out only within this long of its move being found. After that the
# move can still be corrected or removed, but nothing new rings for it.
PUSH_WINDOW_HOURS = 24


def _fingerprint(text: str) -> str:
    """What a message said last time, so an unchanged one is not re-sent.

    Telegram rejects an edit whose text matches the message already there, and
    most hours change nothing: without this the run would call editMessageText
    for every live message every hour and collect an error each time.
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _rank(tier) -> int:
    from tremor.jumps import WORDS

    return WORDS.index(str(tier)) if str(tier) in WORDS else -1


def _is_push_word(tier) -> bool:
    from tremor.routing import PUSH_TIERS

    return str(tier) in PUSH_TIERS


def superseder(event: dict) -> "str | None":
    """The event_id of the rarer event that later took this one's day
    (tremor.jumps.for_delivery), or None for the day's rarest event."""
    value = event.get("superseded_by")
    try:
        if value is None or pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value) or None


def _kept(event: dict) -> bool:
    value = event.get("kept", True)
    try:
        return bool(value) and not pd.isna(value)
    except (TypeError, ValueError):
        return bool(value)


def _found(event: dict) -> int:
    value = event.get("found_utc")
    try:
        if value is not None and not pd.isna(value):
            return int(value)
    except (TypeError, ValueError):
        pass
    return int(event["hour_utc"]) + 3600


def _day_key(event: dict) -> "tuple[str, int]":
    day = event.get("day")
    try:
        day = int(day)
    except (TypeError, ValueError):
        day = int(event["hour_utc"]) // 86400
    return str(event.get("asset_id", "")), day


def _send(cfg: Config, text: str) -> "int | None":
    try:
        return int(send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, text))
    except TelegramError as exc:
        log.error("Could not send: %s", exc)
        return None


def _edit(cfg: Config, message_id: int, text: str) -> bool:
    try:
        edit_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                              int(message_id), text)
        return True
    except TelegramError as exc:
        log.error("Could not edit message %s: %s", message_id, exc)
        return False


def _delete(cfg: Config, message_id: "int | None", first_line: str = "") -> bool:
    """Deletes one message. If Telegram refuses, strikes it through instead.
    True once the message no longer stands on the channel as it was."""
    from price_monitor.notifier import delete_telegram_message

    if message_id is None:
        return True
    try:
        if delete_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                   int(message_id)):
            return True
    except TelegramError as exc:
        log.warning("Could not delete message %s: %s", message_id, exc)
        return False
    struck = f"<s>{first_line}</s>" if first_line else "<s>removed</s>"
    try:
        edit_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                              int(message_id), struck)
    except TelegramError as exc:
        if "not found" in str(exc).lower():
            return True                          # somebody deleted it already
        log.error("Could not strike message %s through: %s", message_id, exc)
        return False
    log.info("Message %s struck through: Telegram would not delete it", message_id)
    return True


def _first(text: str) -> str:
    return (text or "").split("\n", 1)[0]


def _old_format(store: dict) -> bool:
    return any(k in store for k in ("digests", "sent", "tracked", "pings"))


def _adopt_old_state(cfg: Config, store: dict, now: datetime) -> "dict | None":
    """The first run of this delivery on a channel the previous one wrote.

    The previous version kept its notes, pushes and pings under other keys. Its
    latest note is adopted as this week's note; every push of that note's
    period and every outstanding ping is deleted, and the week goes on from this
    run - the same as any detector update.
    """
    from tremor import routing

    digests = store.pop("digests", {}) or {}
    sent = store.pop("sent", {}) or {}
    tracked = store.pop("tracked", {}) or {}
    pings = store.pop("pings", {}) or {}
    latest = max((int(k) for k, v in digests.items() if v.get("ids")), default=None)
    start = latest if latest is not None else routing.digest_slot(int(now.timestamp()))
    for event_id, rec in sent.items():
        hour = rec.get("hour") if isinstance(rec, dict) else rec
        mid = rec.get("id") if isinstance(rec, dict) else None
        mid = mid or (tracked.get(event_id) or {}).get("message_id")
        if hour is not None and int(float(hour)) >= start and mid:
            _delete(cfg, mid)
    for value in pings.values():
        _delete(cfg, value.get("id") if isinstance(value, dict) else value)
    if latest is None:
        return None
    record = digests[str(latest)]
    return {"slot": latest, "since": int(now.timestamp()) - 3600 + 1,
            "note": {"ids": list(record.get("ids") or []),
                     "hashes": list(record.get("hashes") or [])},
            "pushes": {}, "rows": {}, "retired": []}


def _new_week(slot: int, version: str) -> dict:
    return {"slot": int(slot), "since": int(slot), "detector": version,
            "note": {"ids": [], "hashes": []}, "pushes": {}, "rows": {}, "retired": []}


def _close_week(cfg: Config, week: dict) -> None:
    """The week becomes history: its pings go, everything else stays as it is."""
    for row in week.get("rows", {}).values():
        _delete(cfg, row.get("ping"), _first(row.get("ping_text", "")))


def _restart_week(cfg: Config, week: dict, version: str, now: datetime) -> None:
    """A detector update: the week's pushes and pings are deleted, the note
    stays, and the week continues with what is found from this run on."""
    for rec in week.get("pushes", {}).values():
        _delete(cfg, rec.get("id"), rec.get("first", ""))
    for row in week.get("rows", {}).values():
        _delete(cfg, row.get("ping"), _first(row.get("ping_text", "")))
    week.update(pushes={}, rows={}, retired=[], detector=version,
                since=int(now.timestamp()) - 3600 + 1)


def _in_week(event: dict, week: dict) -> bool:
    from tremor import routing
    from tremor.jumps import FOUND_TO_RUN

    moment = _found(event) + FOUND_TO_RUN
    end = routing.next_digest_slot(int(week["slot"]))
    return max(int(week["slot"]), int(week["since"])) <= moment < end


def _push(cfg: Config, week: dict, event: dict, labels: dict, calendar) -> bool:
    text = format_push(event, labels, calendar)
    message_id = _send(cfg, text)
    if message_id is None:
        return False
    asset_id, day = _day_key(event)
    week["pushes"][str(event["event_id"])] = {
        "id": message_id, "hash": _fingerprint(text), "first": _first(text),
        "tier": str(event.get("tier")), "rang": str(event.get("tier")),
        "asset_id": asset_id, "day": day}
    return True


def _drop_row(cfg: Config, week: dict, event_id: str) -> None:
    row = week["rows"].pop(event_id, None)
    if row:
        _delete(cfg, row.get("ping"), _first(row.get("ping_text", "")))


def _curate(cfg: Config, week: dict, events: "list[dict]", labels: dict,
            calendar, now: datetime) -> int:
    """One run's pass over the week. Returns how many messages changed."""
    by_id = {str(e.get("event_id")): e for e in events}
    now_ts = int(now.timestamp())
    window = PUSH_WINDOW_HOURS * 3600
    changed = 0

    # Pushes already on the channel.
    for event_id, rec in list(week["pushes"].items()):
        event = by_id.get(event_id)
        if event is None or not _in_week(event, week):
            if _delete(cfg, rec.get("id"), rec.get("first", "")):
                week["pushes"].pop(event_id)
                changed += 1
            continue
        tier = str(event.get("tier"))
        # Rarer than the word it last RANG at, not than its current one: a push
        # that fell to `noticeable` and came back is a flip-flop, and is edited.
        rarer = _is_push_word(tier) and _rank(tier) > _rank(rec.get("rang", rec.get("tier")))
        if rarer and _kept(event) and now_ts - _found(event) <= window:
            old = dict(rec)
            if _push(cfg, week, event, labels, calendar):
                _delete(cfg, old.get("id"), old.get("first", ""))
                changed += 2
            continue
        text = format_push(event, labels, calendar)
        rec["tier"] = tier
        if _fingerprint(text) != rec.get("hash") and _edit(cfg, rec["id"], text):
            rec.update(hash=_fingerprint(text), first=_first(text))
            changed += 1

    # Rows already in the note.
    for event_id, row in list(week["rows"].items()):
        event = by_id.get(event_id)
        if event is None or not _kept(event) or not _in_week(event, week):
            _drop_row(cfg, week, event_id)
            changed += 1
            continue
        if (_is_push_word(event.get("tier")) and _kept(event)
                and now_ts - _found(event) <= window):
            if _push(cfg, week, event, labels, calendar):
                _drop_row(cfg, week, event_id)
                changed += 2
            continue
        if row.get("ping") is not None:
            text = format_ping(event, labels)
            if _fingerprint(text) != row.get("ping_hash") and _edit(cfg, row["ping"], text):
                row.update(ping_hash=_fingerprint(text), ping_text=text)
                changed += 1

    # New events: only within their push window, and only the day's kept ones.
    days_with_push = {(r["asset_id"], int(r["day"])) for r in week["pushes"].values()}
    fresh = sorted((e for e in events
                    if _kept(e) and superseder(e) is None and _in_week(e, week)
                    and 0 <= now_ts - _found(e) <= window
                    and str(e.get("event_id")) not in week["pushes"]
                    and str(e.get("event_id")) not in week["rows"]
                    and str(e.get("event_id")) not in week["retired"]),
                   key=_found)
    for event in [e for e in fresh if _is_push_word(e.get("tier"))]:
        if _push(cfg, week, event, labels, calendar):
            days_with_push.add(_day_key(event))
            changed += 1
    for event in [e for e in fresh if not _is_push_word(e.get("tier"))]:
        if _day_key(event) in days_with_push:
            continue
        text = format_ping(event, labels)
        ping = _send(cfg, text)
        asset_id, day = _day_key(event)
        week["rows"][str(event["event_id"])] = {
            "ping": ping, "ping_hash": _fingerprint(text), "ping_text": text,
            "asset_id": asset_id, "day": day}
        changed += 1

    # One push a day, its rarest; lower ones leave. A day with a push shows no row.
    by_day: dict = {}
    for event_id, rec in week["pushes"].items():
        by_day.setdefault((rec["asset_id"], int(rec["day"])), []).append(event_id)
    for ids in by_day.values():
        if len(ids) < 2:
            continue
        keep = max(ids, key=lambda i: (_rank(week["pushes"][i]["tier"]),
                                       _found(by_id.get(i, {"hour_utc": 0}))))
        for event_id in ids:
            if event_id == keep:
                continue
            rec = week["pushes"][event_id]
            if _delete(cfg, rec.get("id"), rec.get("first", "")):
                week["pushes"].pop(event_id)
                week["retired"].append(event_id)
                changed += 1
    days_with_push = {(r["asset_id"], int(r["day"])) for r in week["pushes"].values()}
    for event_id, row in list(week["rows"].items()):
        if (row.get("asset_id"), int(row.get("day", -1))) in days_with_push:
            _drop_row(cfg, week, event_id)
            changed += 1
    return changed


def _write_note(cfg: Config, week: dict, texts: "list[str]") -> int:
    """Posts, edits and trims the note's parts. A part that is no longer needed
    is deleted, newest first, so the ids stay a prefix of the note."""
    note = week["note"]
    ids, hashes = note["ids"], note["hashes"]
    changed = 0
    for index, text in enumerate(texts):
        mark = _fingerprint(text)
        if index < len(ids):
            if hashes[index] != mark and _edit(cfg, ids[index], text):
                hashes[index] = mark
                changed += 1
            continue
        message_id = _send(cfg, text)
        if message_id is None:
            break
        ids.append(message_id)
        hashes.append(mark)
        changed += 1
    while len(ids) > max(len(texts), 1):
        if not _delete(cfg, ids[-1]):
            break
        ids.pop()
        hashes.pop()
        changed += 1
    return changed


def maybe_deliver(cfg: Config, state: dict, now: datetime | None = None) -> int:
    """One run of the week. Returns how many Telegram messages went out or changed.

    Failures are logged and swallowed: this runs inside the hourly monitoring
    loop, and a Telegram outage must not bring the whole run down. A message is
    remembered only once it is up, so a failure means a retry on the next run.
    """
    from tremor import jumps, routing

    now = now or datetime.now(timezone.utc)
    if cfg.tremor_alerts_muted:
        return 0

    events = load_events(cfg)
    # An empty table is "the pipeline did not run", not "every event vanished"
    # and not "nothing happened": it changes nothing, the note included.
    if not events:
        return 0
    store = state.setdefault(STATE_KEY, {})
    version = jumps.detector_version()
    changed = 0

    if _old_format(store):
        adopted = _adopt_old_state(cfg, store, now)
        if adopted is not None:
            adopted["detector"] = version
            store[WEEK] = adopted
        save_state(cfg.state_path, state)

    week = store.get(WEEK)
    slot = routing.digest_slot(int(now.timestamp()))
    if week is None or slot > int(week["slot"]):
        if week is not None:
            _close_week(cfg, week)
        week = store[WEEK] = _new_week(slot, version)
        save_state(cfg.state_path, state)
    elif week.get("detector") != version:
        _restart_week(cfg, week, version, now)
        log.info("Detector updated: the week restarts from this run")
        save_state(cfg.state_path, state)

    labels = _labels()
    calendar = _calendar(cfg)
    changed += _curate(cfg, week, events, labels, calendar, now)
    save_state(cfg.state_path, state)

    by_id = {str(e.get("event_id")): e for e in events}
    rows = [by_id[i] for i in week["rows"] if i in by_id]
    window = (int(week["slot"]), routing.next_digest_slot(int(week["slot"])))
    changed += _write_note(cfg, week, format_digest(rows, labels, window, calendar, now))
    save_state(cfg.state_path, state)
    return changed

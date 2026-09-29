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

ONE NOTE A WEEK, opened at the first run after the week's last funds close
(tremor.routing) right after the economic calendar's own message
(weekly_digest.py), and curated for that week: every run re-reads the
events table and brings every message of the week in line with it - see "the
week" below for exactly what that means. What belongs to an earlier note is
history and is never touched.

AN EVENT IS 24 HOURS FROM ITS FIRST MOVE BEING FOUND, and it rings only inside
them. After that it may still be corrected or removed, silently. This is load-bearing: the
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

    Only what the detector measures is said: the move, its size in σ, and how
    long it has been since the instrument was at least this rare (rarest_line).
    How the move held at the next close and the block's share of it come back
    as the stages that measure them do.
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
    lines = [f"{emoji} <b>{_escape(_ticker(asset_id))}</b> · {_escape(label)}{shown}"]
    rarest = rarest_line(event)
    if rarest:
        lines.append(rarest)
    lines.append(f"{TIME_EMOJI}<b>{format_day(when)} {when:%H:%M} UTC</b>"
                 f"{check_suffix(event)}")
    return "\n".join(lines)


def check_suffix(event: dict) -> str:
    """ · close in 5h, then · close 80% - STAGE 4 on the time line.

    Every move is checked at the funds' close after it was found
    (tremor.jumps.held_at_close): "close" when that is the close of the day it
    was found on in New York, "next close" when it is a later one - a move in
    the closing hour, after it, or on a weekend. Until then it counts down in
    hours, from the run's own clock (`now_utc`, set by delivery); after, the
    share of the move still there - 100% held exactly, 120% kept going, -20%
    reversed. Nothing without the stage's columns."""
    from tremor.routing import EXCHANGE_TZ

    check, found = _clean(event.get("check_utc")), _clean(event.get("found_utc"))
    if check is None:
        return ""
    if found is None:
        found = (_clean(event.get("hour_utc")) or 0) + 3600
    same_day = (datetime.fromtimestamp(check, tz=EXCHANGE_TZ).date()
                == datetime.fromtimestamp(found, tz=EXCHANGE_TZ).date())
    label = "close" if same_day else "next close"
    held = _clean(event.get("held"))
    if held is not None:
        return f" · {label} {held * 100:.0f}%"
    now = _clean(event.get("now_utc"))
    if now is None:
        return ""
    left = -int(-(check - now) // 3600)               # hours, rounded up
    # Past the close with the bars not there yet (a missed fetch): it waits.
    return f" · {label} in {left}h" if left > 0 else f" · {label} pending"


def _span(seconds: float) -> str:
    """A span the way a person says it, rounded DOWN so the claim stays true:
    "rarest in 2 years" holds for 2.6 of them. Hours under two days, days
    under two months, months under two years, then years."""
    hours = max(1, int(seconds // 3600))
    days = seconds / 86400
    if hours < 48:
        n, unit = hours, "hour"
    elif days < 60:
        n, unit = int(days), "day"
    elif days < 730.5:
        n, unit = int(days // 30.44), "month"
    else:
        n, unit = int(days // 365.25), "year"
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _kind(event: dict) -> str:
    reading = event.get("reading")
    if isinstance(reading, str) and reading:
        return reading
    return _gap_kind(event) if _overnight(event) else "hour"


def rarest_line(event: dict) -> str:
    """📈 Rarest hour in 6 months (then 7.1×σ) - or, with nothing at least as
    rare in the whole record, 📉 Rarest weekend in 6 years of record.

    STAGE 3 (tremor.jumps.rarest_since). How long since the instrument last
    moved at least this rarely: the most recent earlier reading of the SAME
    KIND - an hour, a night, a weekend, each in its own σ - in the same
    direction, at least 95% of this size or bigger. "Then" is that reading's
    size, so a near-match is shown as one. Empty when the event does not carry
    the stage's columns."""
    hour, start = _clean(event.get("hour_utc")), _clean(event.get("record_start"))
    move = _clean(event.get("r"))
    if hour is None or start is None:
        return ""
    arrow = "📉" if move is not None and move < 0 else "📈"
    kind = _kind(event)
    since = _clean(event.get("since_utc"))
    if since is None:
        return f"{arrow} Rarest {kind} in {_span(hour - start)} of record"
    then = _clean(event.get("since_z"))
    size = f" (then {abs(then):.1f}×σ)" if then is not None else ""
    return f"{arrow} Rarest {kind} in {_span(hour - since)}{size}"


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
    if event.get("story"):
        lines.append(event["story"])
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

    The header states the period the note speaks for, from the evening of one
    week's last funds close to the next's.
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
    # runs to the instant the next one opens, five minutes after a close - so a
    # boundary just past midnight UTC would otherwise name a day the note
    # carries none of.
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
        if event.get("story"):
            line += "\n" + event["story"]
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
# ONE NOTE A WEEK, opened at the first run after the week's last funds close
# (tremor.routing) just after the economic calendar's own message. For that week
# every message is kept in line with the events table, every run; anything from
# before the note opened is history and is never touched again. The run that
# turns the week first finishes the old one - its checks at that close are in -
# and only then closes it and opens the new note.
#
# HELD AT THE CLOSE (stage 4). Every message counts down on its time line to
# the funds' close its move is checked at, and then says how much of the move
# was still there (check_suffix). The edits are silent and are not a change to
# the event: they add no story.
#
# AN EVENT is 24 hours of real time from its first move being found
# (tremor.jumps.event_starts). Its word is its rarest reading's and the numbers
# it shows its biggest reading's. What happens to it on the channel:
#
#   within its 24 hours     rarer (any cause but a detector update) -> its
#                           message is deleted - the row and its ping, or the
#                           push - and it goes out again at the new word, and
#                           rings. Milder -> edited in place: a push that falls
#                           to `noticeable` shows ⬜; a `noticeable` that falls
#                           away is deleted, row and ping. Same word, other
#                           numbers -> edited in place.
#   after its 24 hours      complete: a new move starts a new event. It changes
#                           only when a bar is corrected or arrives late, and
#                           never rings: rarer or milder is an edit - a row that
#                           becomes `high` leaves the note and its ping is
#                           edited into the push - and gone is deleted, for good.
#
# A CHANGED EVENT TELLS ITS STORY: one line under the time, every state it has
# been in with why it moved (story_line). A clean event says nothing.
#
# A detector update (a new tremor.jumps.detector_version) starts the week over at
# that run: every push and ping of the week is deleted, the note stays and shows
# only what is found from then on.
#
# Deleting is how a message leaves; the bot is an administrator of a public
# channel and may delete any message there. Should Telegram refuse, the message
# is struck through by an edit instead.

WEEK = "week"

# A message rings only within this long of its event's first move being found.
# After that the event can still be corrected or removed, but nothing rings.
PUSH_WINDOW_HOURS = 24

ROW, PUSH = "row", "push"

# Why an event's message changed, in the jump detector's own terms: its size is
# |move| / σ of its biggest reading, and only these move it.
BIGGER = "bigger jump"            # a new hour in the event jumped further
LATE = "arrived late"             # a bar or gap that was missing came in
PRICE = "price corrected"         # the provider revised the bar
SIGMA = "σ corrected"             # older bars revised, so the half-year yardstick moved
AWAY = "corrected away"           # no longer a jump


def _fingerprint(text: str) -> str:
    """What a message said last time, so an unchanged one is not re-sent.

    Telegram rejects an edit whose text matches the message already there, and
    most hours change nothing: without this the run would call editMessageText
    for every live message every hour and collect an error each time.
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _rank(tier) -> int:
    from tremor.jumps import WORDS

    return WORDS.index(str(tier)) if tier is not None and str(tier) in WORDS else -1


def _is_push_word(tier) -> bool:
    from tremor.routing import PUSH_TIERS

    return str(tier) in PUSH_TIERS


def _found(reading: dict) -> int:
    value = reading.get("found_utc")
    try:
        if value is not None and not pd.isna(value):
            return int(value)
    except (TypeError, ValueError):
        pass
    return int(reading["hour_utc"]) + 3600


def _size(reading: dict) -> float:
    z = _clean(reading.get("z"))
    if z is None:
        move, usual = _clean(reading.get("r")), _clean(reading.get("sigma_lt"))
        z = move / usual if move is not None and usual else 0.0
    return abs(z)


def _hour_of(reading_id: str) -> int:
    return int(str(reading_id).rsplit(":", 1)[1])


def _clock(hour_utc: int, headline: int) -> str:
    """HH:MM, with the day in front when it is not the headline's day."""
    at = datetime.fromtimestamp(int(hour_utc), tz=timezone.utc)
    same = at.date() == datetime.fromtimestamp(int(headline), tz=timezone.utc).date()
    return at.strftime("%H:%M") if same else at.strftime("%d.%m %H:%M")


def story_line(story: "list[list]", headline: int) -> str:
    """✏️ 🟨 6.0×σ 02:00 → 🟧 8.1×σ 03:00 bigger jump → ✖ corrected away ...

    Every state the event has been in, oldest first, and why it moved into
    each. Empty for an event that never changed."""
    if len(story) < 2:
        return ""
    steps = []
    for tier, size, hour, why in story:
        if tier is None:
            steps.append(f"✖ {why}".rstrip())
            continue
        step = f"{TIER_EMOJI.get(tier, '⚪')} {size:.1f}×σ {_clock(hour, headline)}"
        steps.append(f"{step} {why}" if why else step)
    return "✏️ " + " → ".join(_escape(s) for s in steps)


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


def _discard(cfg: Config, week: dict, message_id, first_line: str = "") -> None:
    """Deletes a message, and remembers it to try again should that fail."""
    if message_id is not None and not _delete(cfg, message_id, first_line):
        week.setdefault("orphans", []).append([int(message_id), first_line])


# --- the week's state -----------------------------------------------------------

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
    week = _new_week(routing.digest_slot(int(now.timestamp())), "")
    week["since"] = int(now.timestamp()) - 3600 + 1
    week["note"] = {"ids": list(record.get("ids") or []),
                    "hashes": list(record.get("hashes") or [])}
    return week


def _new_week(slot: int, version: str) -> dict:
    return {"slot": int(slot), "since": int(slot), "detector": version,
            "note": {"ids": [], "hashes": []}, "events": {}, "orphans": []}


def _close_week(cfg: Config, week: dict) -> None:
    """The week becomes history: its pings go, everything else stays as it is."""
    for rec in week.get("events", {}).values():
        if rec.get("form") == ROW:
            _delete(cfg, rec.get("ping"), rec.get("first", ""))
    for message_id, first_line in week.get("orphans", []):
        _delete(cfg, message_id, first_line)


def _restart_week(cfg: Config, week: dict, version: str, now: datetime) -> None:
    """A detector update: the week's pushes and pings are deleted, the note
    stays, and the week continues with what is found from this run on."""
    for rec in week.get("events", {}).values():
        _delete(cfg, rec.get("id") if rec.get("form") == PUSH else rec.get("ping"),
                rec.get("first", ""))
    for message_id, first_line in week.get("orphans", []):
        _delete(cfg, message_id, first_line)
    week.update(events={}, orphans=[], detector=version,
                since=int(now.timestamp()) - 3600 + 1)


def note_due(state: dict, now: datetime) -> bool:
    """Whether this run opens a new note - the run the calendar goes out in,
    just before it (weekly_digest)."""
    from tremor import routing

    store = state.get(STATE_KEY) or {}
    if _old_format(store):
        return False
    week = store.get(WEEK)
    return week is None or routing.digest_slot(int(now.timestamp())) > int(week["slot"])


# --- one event ------------------------------------------------------------------

def _in_week(reading: dict, week: dict) -> bool:
    from tremor import routing
    from tremor.jumps import FOUND_TO_RUN

    moment = _found(reading) + FOUND_TO_RUN
    end = routing.next_digest_slot(int(week["slot"]))
    return max(int(week["slot"]), int(week["since"])) <= moment < end


def _group(readings: "list[dict]", week: dict) -> "dict[str, list[dict]]":
    """The week's readings as events, {key: readings}. Events already on the
    channel hold their 24 hours (tremor.jumps.event_starts' anchors)."""
    from tremor.jumps import event_starts

    tracked = week["events"]
    by_asset: dict = {}
    for reading in readings:
        by_asset.setdefault(str(reading.get("asset_id", "")), []).append(reading)
    out: dict = {key: [] for key in tracked}
    for asset, rows in by_asset.items():
        anchors = [rec["start"] for rec in tracked.values() if rec["asset"] == asset]
        starts = event_starts([_found(r) for r in rows], anchors)
        for reading, start in zip(rows, starts):
            out.setdefault(f"{asset}|{int(start)}", []).append(reading)
    return out


def _moved(before: "list[float]", reading: dict) -> "str | None":
    """Why one reading's numbers differ from what was seen before, if they do."""
    import math

    r, sigma = _clean(reading.get("r")), _clean(reading.get("sigma_lt"))
    if r is None or not math.isclose(before[0], r, rel_tol=1e-9, abs_tol=1e-12):
        return PRICE
    if sigma is None or not math.isclose(before[1], sigma, rel_tol=1e-9, abs_tol=1e-12):
        return SIGMA
    return None


def _why(rec: dict, members: "dict[str, dict]", peak: "dict | None") -> str:
    """Why the event now shows `peak` rather than what it showed."""
    if peak is None:
        return AWAY
    seen, old_peak = rec["members"], rec.get("peak")
    pid = str(peak["reading_id"])
    if old_peak and old_peak not in members:
        return f"{_clock(_hour_of(old_peak), int(peak['hour_utc']))} {AWAY}"
    if old_peak and old_peak in seen:
        moved = _moved(seen[old_peak], members[old_peak])
        if moved:
            return moved
    if pid in seen:
        return _moved(seen[pid], peak) or PRICE
    return BIGGER if _found(peak) > int(rec.get("seen", 0)) else LATE


def _shown(peak: "dict | None") -> list:
    if peak is None:
        return [None, None, None, None]
    return [str(peak.get("tier")), round(_size(peak), 1), int(peak["hour_utc"]),
            round(float(_clean(peak.get("r")) or 0.0) * 100, 2)]


def _render(rec: dict, peak: dict) -> dict:
    return dict(peak, story=story_line(rec["story"], int(peak["hour_utc"])))


def _post(cfg: Config, rec: dict, peak: dict, labels: dict, calendar) -> bool:
    """Puts the event on the channel at its word: a push, or a row and its
    ping. True once it is up; nothing in `rec` changes if it is not."""
    if _is_push_word(peak.get("tier")):
        text = format_push(_render(rec, peak), labels, calendar)
        message_id = _send(cfg, text)
        if message_id is None:
            return False
        rec.update(form=PUSH, id=message_id, ping=None, hash=_fingerprint(text),
                   first=_first(text))
        return True
    text = format_ping(peak, labels)
    ping = _send(cfg, text)
    rec.update(form=ROW, id=None, ping=ping, hash=_fingerprint(text), first=_first(text))
    return True


def _take_down(cfg: Config, week: dict, rec: dict) -> None:
    """Deletes what the event has on the channel. A row leaves the note with
    the next render."""
    if rec.get("form") == PUSH:
        _discard(cfg, week, rec.get("id"), rec.get("first", ""))
    elif rec.get("form") == ROW:
        _discard(cfg, week, rec.get("ping"), rec.get("first", ""))
    rec.update(form=None, id=None, ping=None, hash=None)


def _step(cfg: Config, week: dict, key: str, readings: "list[dict]", labels: dict,
          calendar, now_ts: int) -> int:
    """One event, one run. Returns how many messages went out or went."""
    tracked = week["events"]
    asset, start = key.rsplit("|", 1)
    rec = tracked.get(key)
    open_ = now_ts < int(start) + PUSH_WINDOW_HOURS * 3600
    members = {str(r["reading_id"]): r for r in readings}
    peak = max(readings, key=_size) if readings else None
    # Every reading the event has shown, kept after it vanishes: one that comes
    # back was corrected back, it did not arrive late.
    snapshot = dict(rec["members"]) if rec else {}
    snapshot.update({rid: [_clean(r.get("r")) or 0.0, _clean(r.get("sigma_lt")) or 0.0]
                     for rid, r in members.items()})

    if rec is None:
        # Found now. Only an event still inside its 24 hours goes out at all.
        if peak is None or not open_:
            return 0
        rec = {"asset": asset, "start": int(start), "form": None, "members": {},
               "peak": None, "tier": None, "shown": _shown(None), "story": [],
               "seen": now_ts}
        shown = _shown(peak)
        rec["story"] = [shown[:3] + [""]]
        if not _post(cfg, rec, peak, labels, calendar):
            return 0
        rec.update(members=snapshot, peak=str(peak["reading_id"]), tier=shown[0],
                   shown=shown)
        tracked[key] = rec
        return 1


    shown = _shown(peak)
    if shown == rec["shown"]:
        rec.update(members=snapshot, seen=now_ts)
        return 0

    before = dict(rec, story=list(rec["story"]))
    why = _why(rec, members, peak)
    rec["story"] = rec["story"] + [shown[:3] + [why] if peak is not None
                                   else [None, None, None, why]]
    changed = 0
    promoted = _rank(shown[0]) > _rank(rec.get("tier"))

    if peak is None:
        _take_down(cfg, week, rec)
        changed += 1
    elif promoted and open_:
        # Rarer inside its 24 hours: the old message goes, the new one rings.
        # Only here does anything go out, so an event with nothing on the
        # channel once its 24 hours are over - corrected away - stays gone.
        old = {k: rec.get(k) for k in ("form", "id", "ping", "first")}
        if not _post(cfg, rec, peak, labels, calendar):
            tracked[key] = before
            return 0
        _take_down(cfg, week, dict(old))
        changed += 2
    elif rec.get("form") == ROW and _is_push_word(shown[0]):
        # Rarer after its 24 hours: silent. The row leaves the note and its
        # ping becomes the push, by an edit.
        rec.update(form=PUSH, id=rec.get("ping"), ping=None, hash=None)
        changed += 1
    rec.update(members=snapshot, peak=str(peak["reading_id"]) if peak else None,
               tier=shown[0], shown=shown, seen=now_ts)
    return changed


def _sync(cfg: Config, rec: dict, peak: dict, labels: dict, calendar) -> int:
    """Edits the event's message where its text changed."""
    if rec.get("form") == PUSH:
        text, message_id = format_push(_render(rec, peak), labels, calendar), rec.get("id")
    elif rec.get("form") == ROW and rec.get("ping") is not None:
        text, message_id = format_ping(peak, labels), rec.get("ping")
    else:
        return 0
    if _fingerprint(text) == rec.get("hash") or not _edit(cfg, message_id, text):
        return 0
    rec.update(hash=_fingerprint(text), first=_first(text))
    return 1


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

    readings = load_events(cfg)
    # An empty table is "the pipeline did not run", not "every event vanished"
    # and not "nothing happened": it changes nothing, the note included.
    if not readings:
        return 0
    store = state.setdefault(STATE_KEY, {})
    version = jumps.detector_version()
    now_ts = int(now.timestamp())
    changed = 0

    if _old_format(store):
        adopted = _adopt_old_state(cfg, store, now)
        if adopted is not None:
            adopted["detector"] = version
            store[WEEK] = adopted
        save_state(cfg.state_path, state)

    labels = _labels()
    calendar = _calendar(cfg)
    # Every message says how long until its close from this run's own clock.
    readings = [dict(r, now_utc=now_ts) for r in readings]

    week = store.get(WEEK)
    slot = routing.digest_slot(now_ts)
    if week is None or slot > int(week["slot"]):
        if week is not None:
            # The run after the week's last close: its moves' checks at that
            # close are in, and land on the old week before it closes.
            if week.get("detector") == version:
                changed += _pass(cfg, state, week, readings, labels, calendar, now)
            _close_week(cfg, week)
        week = store[WEEK] = _new_week(slot, version)
        save_state(cfg.state_path, state)
    elif week.get("detector") != version:
        _restart_week(cfg, week, version, now)
        log.info("Detector updated: the week restarts from this run")
        save_state(cfg.state_path, state)
    return changed + _pass(cfg, state, week, readings, labels, calendar, now)


def _pass(cfg: Config, state: dict, week: dict, readings: "list[dict]", labels: dict,
          calendar, now: datetime) -> int:
    """One run over one week's messages: its events, their edits, its note."""
    from tremor import routing

    now_ts = int(now.timestamp())
    window = (int(week["slot"]), routing.next_digest_slot(int(week["slot"])))
    changed = 0
    if not week["note"]["ids"]:
        # A new note goes up before anything it opens with: the calendar, the
        # note, then the moves - the closing hour's among them.
        changed += _write_note(cfg, week, format_digest([], labels, window, calendar, now))
    orphans, week["orphans"] = week.get("orphans", []), []
    for message_id, first_line in orphans:
        _discard(cfg, week, message_id, first_line)

    groups = _group([r for r in readings if _in_week(r, week)], week)
    # Oldest first, so the pushes of one run arrive in the order they happened.
    for key in sorted(groups, key=lambda k: int(k.rsplit("|", 1)[1])):
        changed += _step(cfg, week, key, groups[key], labels, calendar, now_ts)
        save_state(cfg.state_path, state)
    for key, rec in week["events"].items():
        rows = groups.get(key) or []
        if rows and rec.get("form"):
            changed += _sync(cfg, rec, max(rows, key=_size), labels, calendar)
    save_state(cfg.state_path, state)

    note_rows = [_render(rec, max(groups[key], key=_size))
                 for key, rec in week["events"].items()
                 if rec.get("form") == ROW and groups.get(key)]
    changed += _write_note(cfg, week, format_digest(note_rows, labels, window, calendar, now))
    save_state(cfg.state_path, state)
    return changed

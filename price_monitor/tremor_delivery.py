"""Delivers Tremor events to Telegram: the pushes, and the running digest.

The detector decides everything about WHAT to say - the word, which channel an
event belongs to, which note it falls in (see tremor.jumps and tremor.routing).
This module decides nothing. It reads those decisions,
renders them, and keeps the messages up to date.

TWO KINDS OF MESSAGE, and the difference is how loudly they arrive rather than
how long they wait. A push is its own message and goes out the hour the move is
found. A digest row goes into the note for its period - and that note is OPENED
at the start of the period rather than written at the end of it, so a row
appears the same hour and the reader is not made to wait three days for
something that has already happened and will not change. Telegram notifies on a
new message and stays silent on an edit, so the note itself costs one
interruption a week, when it opens; each row adds a small ping of its own.

EVERY MESSAGE IS CORRECTED IN PLACE. A row's numbers can move after it is
sent - the hour is scored a few minutes in and the bar heals on the next fetch -
so a push is re-rendered and edited when its text changes (follow_up.py), and a
note is rendered whole every run (_write_digest here).

NOTHING IS REMEMBERED ABOUT A NOTE EXCEPT ITS MESSAGE IDS. It is rendered whole
from the events table every run and edited only when the text actually changed,
so a late event simply appears, a recomputed-away one simply goes, and a run
that renders twice writes the same thing twice.

It piggybacks on the existing hourly trigger rather than taking a schedule of
its own.

SEPARATE FROM THE SATURDAY CALENDAR DIGEST, on purpose, and not merely to keep
files apart. The two are different tenses: the calendar digest is a forecast of
what is scheduled next week, this one is a report of what actually happened.
Reading them as one message makes both harder to skim. Both go out in the run
that opens the note, the calendar first (see weekly_digest.py).

WHEN A NOTE MAY BE OPENED is a rule of its own. Only in its own hour, or the
three after it, so that a note stays a thing with a date on it: Saturday 00:05
UTC, the quietest hour of the week and the seam where the trading week actually
ends. A note opened whenever the system
happened to next run is not a schedule, it is an arrival time. A period that
misses the window is not lost: the next note covers from where the last one that
actually went out left off, so the boundaries hold AND no move is silently
dropped for want of a scheduler.

WHAT IS NOT SENT. A push older than STALE_AFTER_HOURS. This is load-bearing
rather than a nicety: the events table holds the entire history, so without it
the first run after the mute comes off would deliver five years of alerts at
once - old news is not news, whatever the state file does or does not remember.
An EMPTY events table sends nothing at all, note included: it cannot tell
"nothing happened" from "the pipeline did not run", and only one of those is
safe to print.

MUTED BY DEFAULT (Config.tremor_alerts_muted). The flag lives in config.yaml for
the same reason alerts_muted does: "we are deliberately silent" is a state of the
project and has to be visible where the code is, not on some scheduler's website
where a month later nobody can tell it from a breakage.
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

# How old an event may be and still be worth sending. Two days: long enough that
# a scheduler outage over a weekend does not lose the Friday digest, short enough
# that nothing arrives claiming to be news when it is not.
STALE_AFTER_HOURS = 48

# What the state file remembers. Event ids rather than a high-water mark on the
# hour, because an event can legitimately change channel after the fact: an event
# is open for the rest of its trading day and escalates if the move gets worse,
# so a digest row found at 10:00 can be a push by 15:00. A watermark would have
# stepped over it in between and it would never have been sent at all. The wait
# is bounded by STALE_AFTER_HOURS with room to spare: an event can escalate at
# most 23 hours after it opened, and nothing is dropped before 48.
STATE_KEY = "tremor_delivery"
_SENT = "sent"

# The outstanding throwaway pings, {event_id: message_id}. Kept because a
# message can only be deleted by its id, and the runner that sent it is thrown
# away within the minute - an untracked ping is a ping that stays for ever.
PINGS = "pings"

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


def on_channel_elsewhere(events: "list[dict]", sent: dict) -> "set[str]":
    """Events a note row and a ping must not show: one already sent as a push -
    its word may since have fallen to `noticeable`, and the push, corrected in
    place, stays its one message - and one whose day grew into a rarer event
    that is already on the channel."""
    hidden = {str(k) for k, v in sent.items()
              if not (isinstance(v, dict) and v.get("gone"))}
    for e in events:
        top = superseder(e)
        if top and top in sent:
            hidden.add(str(e.get("event_id")))
    return hidden


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


# --- the running note -------------------------------------------------------
#
# The digest is not a report written at the end of a period. It is OPENED at the
# start of the period it covers and edited in place as events are found, which
# is a different product from the same events: a move that will be in Friday's
# note is worth reading on Wednesday, and there is nothing to gain by holding
# it - it happened and its size is known.
#
# It costs no extra interruption. Telegram notifies on a NEW message and stays
# silent on an edit, so the note buzzes once a week, at the hour it opens, and
# everything after that arrives quietly in a message the reader already has
# (each row's own small ping aside).
#
# WHICH IS WHY THE OPENING HOUR IS THE ONE THING THAT CANNOT SLIP. The whole
# arrangement is worth having because the note's interruption lands at a
# scheduled hour; a note that opened whenever the system happened to next
# run - at 22:16, as it did the first time this shipped - is an ordinary
# unscheduled buzz wearing a schedule's clothes.
DIGEST_STATE = "digests"

# So a note may only be opened in its own hour, or shortly after. Three hours of
# grace, because the trigger is an external service and one failed run must not
# cost the whole note - but 15:00 is still an afternoon and 22:00 is not.
DIGEST_OPEN_WITHIN_HOURS = 4

# And when a note misses that window entirely, its period is not lost: it is
# carried into the next note, which then covers from where the last note that
# actually went out left off. That is the only way both halves can be true at
# once - the buzz is always at noon, and no move is silently dropped for want of
# a scheduler.

# How long after its period ends a note may still POST a part, as opposed to
# edit one. It needs a little: the last hour of a period is scored by the run
# after that period has closed, so a note that froze exactly on its boundary
# would drop its own final hour. It must not have much, because a new message is
# an interruption and the whole point of a note is that it interrupts twice a
# week. The same four hours the opening rule allows, for the same reason - the
# trigger is an external service and one failed run must not cost the tail.
DIGEST_GROW_AFTER_CLOSE_HOURS = DIGEST_OPEN_WITHIN_HOURS

# How long a note stays editable after it opens. Its own window is at most three
# and a half days, and the last event inside it then needs until the close of
# the next trading day to be answered - over a holiday weekend, another four.
# Ten days covers both with room to spare, after which the note is left as it
# stands and forgotten.
DIGEST_TRACK_HOURS = 240


def _fingerprint(text: str) -> str:
    """What the note said last time, so an unchanged note is not re-sent.

    Telegram rejects an edit whose text matches the message already there, and
    most hours change nothing: without this the run would call editMessageText
    for every live note every hour and collect an error each time.
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def note_window(slot: int, record: "dict | None" = None) -> "tuple[int, int]":
    """The hours one note speaks for: from where the last note stopped, to where
    the next one starts.

    Normally that is exactly the note's own period. It is wider only when a
    previous note was never opened, and the record carries the wider bound so
    the answer does not change if the state file is read again later.
    """
    from tremor import routing

    slot = int(slot)
    record = record or {}
    return (int(record.get("from", slot)),
            int(record.get("to") or routing.next_digest_slot(slot)))


def due_to_open(slot: int, now: datetime) -> bool:
    """Whether a note that does not exist yet may be opened right now."""
    return int(slot) <= now.timestamp() < int(slot) + DIGEST_OPEN_WITHIN_HOURS * 3600


def carried_from(records: dict, slot: int) -> int:
    """Where a note opening at `slot` should start covering.

    The end of the last note that ACTUALLY went out - so a period whose note
    was never opened is picked up by the next one instead of vanishing. A
    record with no message behind it does not count as a note: it is a post
    that failed and will be retried, and treating it as covered would lose
    exactly the rows it failed to deliver.
    """
    from tremor import routing

    ends = [note_window(int(key), record)[1]
            for key, record in records.items() if record.get("ids")]
    reached = [end for end in ends if end <= int(slot)]
    # With no note behind it at all - a cold start, or a scheduler that has been
    # down longer than a note is kept - one period back is the honest default.
    # It is what the reader missed, it is bounded at three and a half days, and
    # it arrives silently inside a note rather than as a burst of alerts.
    fallback = routing.digest_slot(int(slot) - 1)
    floor = int(slot) - DIGEST_TRACK_HOURS * 3600
    return max(max(reached) if reached else fallback, floor)


def tidy_windows(records: dict) -> int:
    """Makes the open notes cover one stretch each, end to end, never overlapping.

    THE SCHEDULE CAN MOVE UNDER A LIVE NOTE, and when it does the arithmetic that
    is right the rest of the time goes wrong. A note is opened with a `to` taken
    from the boundaries in force at the time; change those boundaries and the
    next note can open INSIDE a note that is still running. carried_from then
    looks for the last note end that has been reached, finds the one before the
    still-open note rather than the still-open note itself, and starts the new
    note there - so both claim the same hours.

    Seen live, moving the notes from Tuesday/Friday to Monday/Saturday:

        Fri 11 09:00  covers Fri 11 09:00 -> Tue 15 09:00   (still open)
        Mon 14 00:05  covers Fri 11 09:00 -> Sat 19 00:05   (opened inside it)

    The reader gets one note headed "Fri 11 to Sat 19" and another headed
    "Fri 11 to Tue 15", both listing the same move.

    So a note ends where the next one begins, always. Applied on every run rather
    than only when a note is opened, because that also repairs records already
    written this way - there is no migration to run and no state to hand-edit.
    Returns how many records it changed, for the log.
    """
    fixed = 0
    ordered = sorted(int(key) for key in records)
    for earlier, later in zip(ordered, ordered[1:]):
        before, after = records[str(earlier)], records[str(later)]
        end = int(note_window(earlier, before)[1])
        if end > later:
            before["to"] = later
            before.pop("rows", None)
            end = later
            fixed += 1
        if int(after.get("from", later)) < end:
            after["from"] = end
            # The rows marker guards a note against being emptied by a change to
            # what QUALIFIES (see the note in maybe_deliver). A window that has
            # just been straightened is a different thing: the rows it is losing
            # were never its own, they belong to the note beside it, and holding
            # them would leave the same move printed twice.
            after.pop("rows", None)
            fixed += 1
    return fixed


def published_only(record: dict, rows: "list[dict]",
                   window: "tuple[int, int]", now: datetime) -> "list[dict]":
    """A closed note shows what it showed, not what the table says now.

    A note is re-rendered from the events table on every run, which is what lets
    a late row appear while the period is open. Once the period is over that
    stops being a feature and becomes a lie: the table keeps changing - a
    recompute, a retuned threshold, a fixed detector - and the note silently
    rewrites itself into a record of moves it never reported.

    That is not hypothetical. Scoring was fixed so that an hour is judged on its
    whole bar rather than on the first five minutes of it, and fourteen moves it
    had missed appeared at once inside a note for a week that had already ended -
    which then claimed to have reported nineteen moves that week when it had
    reported five. The reader is owed the five.

    So while a note can still grow it records the ids it is showing, and once it
    cannot it shows only those. A note from before this existed has nothing
    recorded and is left alone.
    """
    kept = record.get("events")
    if kept is None:
        return rows
    if now.timestamp() < window[1] + DIGEST_GROW_AFTER_CLOSE_HOURS * 3600:
        return rows
    allowed = set(kept)
    return [e for e in rows if str(e.get("event_id")) in allowed]


def digest_rows(events: "list[dict]", window: "tuple[int, int]",
                now: datetime) -> "list[dict]":
    """Every event one note speaks for. Recomputed from the table each run.

    Nothing is remembered about which rows have already been written: the note
    is rendered whole from the events table every time, so an event that
    arrives late simply appears, and one a recompute no longer produces simply
    goes. That is what makes editing safe to repeat.

    A push word is never here: its channel is decided from its word. A move
    that is on the channel as a push already - its word has since fallen, or
    its day grew into one - is taken out by the caller (on_channel_elsewhere),
    so nothing is ever shown twice.

    An hour that has not happened yet is not written down, the same rule a push
    is held to. It should not arise - a bar has to close before it is scored -
    but a clock skew or a bad bar must not put tomorrow in today's note.
    """
    start, end = window
    return [e for e in events
            if str(e.get("channel") or "") == "digest"
            and start <= float(e.get("hour_utc", 0)) < end
            and float(e.get("hour_utc", 0)) <= now.timestamp()]


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

    The header states the period the note speaks for rather than the day it was
    posted, because those come apart exactly when it matters: a note that had
    to pick up a period whose own note never opened covers six days, and saying
    so is the difference between a complete record and a puzzling one.
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
    # runs to the instant the next one opens, and under the current boundaries
    # that instant is 00:05 - so a note technically reaches into the next
    # Saturday by five minutes and would print "Sat 12 to Sat 19", handing that
    # Saturday to a note that carries none of it. It belongs to the next note.
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
        # header, so a later part read on its own is a wall of event lines
        # ending in "part 2 of 3" with nothing saying of what. That is not
        # hypothetical: when a closed note gained parts they landed BELOW the
        # note that had already replaced it, and the reader got two orphans
        # under the wrong week. The bound in maybe_deliver stops a note growing
        # once it is closed; this makes any part that does get split legible on
        # its own, wherever it ends up in the scroll.
        total = len(messages)
        period = f"{format_day(opened)} to {format_day(last)}"
        messages = [f"{m}\n\n<i>part {i} of {total} - {period}</i>"
                    for i, m in enumerate(messages, 1)]
    return messages


_EMPTIED_PART = "<i>(this part is no longer needed - the note above is complete)</i>"


def _write_digest(cfg: Config, slot: int, record: dict,
                  texts: "list[str]", state: dict | None = None,
                  may_grow: bool = True) -> "tuple[int, int]":
    """Posts a note's parts, or edits the ones already posted.

    Returns (posted, edited). Whether the note may exist at all was decided
    before this was called; here it either has messages behind it or is having
    its first ones sent.

    Parts can only grow - events are added, never removed - so a new part is a
    new message and everything before it is an edit. A failed post stops the
    loop rather than skipping a part, because the parts are numbered and a gap
    would be worse than a retry on the next run.

    may_grow=False renders into the parts the note already has and stops there.
    It is how a closed note stays correctable without becoming loud: an edit is
    silent, a new part is a notification, and a period that ended days ago has
    no business notifying anybody. The caller decides; see maybe_deliver.
    """
    ids, hashes = record["ids"], record["hashes"]
    if len(texts) < len(ids):
        # A note can lose a part: a recompute that no longer produces an event
        # takes its lines with it. The message itself cannot be deleted, so the
        # surplus part is emptied rather than left saying "part 3 of 5" under a
        # note that now has two.
        texts = list(texts) + [_EMPTIED_PART] * (len(ids) - len(texts))

    posted = edited = 0
    for index, text in enumerate(texts):
        mark = _fingerprint(text)
        if index < len(ids):
            if hashes[index] == mark:
                continue
            try:
                edit_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                      int(ids[index]), text)
            except TelegramError as exc:
                log.error("Could not update the digest for %s: %s", slot, exc)
                continue
            hashes[index] = mark
            edited += 1
            if state is not None:
                save_state(cfg.state_path, state)
        else:
            if not may_grow:
                break
            try:
                message_id = send_telegram_message(
                    cfg.telegram_bot_token, cfg.telegram_chat_id, text)
            except TelegramError as exc:
                log.error("Could not post the digest for %s: %s", slot, exc)
                break
            ids.append(int(message_id))
            hashes.append(mark)
            posted += 1
            if state is not None:
                save_state(cfg.state_path, state)
    return posted, edited


def _prune_digests(digests: dict, now: datetime) -> dict:
    cutoff = now.timestamp() - DIGEST_TRACK_HOURS * 3600
    return {k: v for k, v in digests.items() if float(k) >= cutoff}


def _fresh(event: dict, now: datetime, key: str = "hour_utc") -> bool:
    value = event.get(key)
    if value is None or value != value:
        return False
    age = now.timestamp() - float(value)
    return 0 <= age <= STALE_AFTER_HOURS * 3600


def format_ping(event: dict, labels: dict[str, str]) -> str:
    """The throwaway line that says a digest row just appeared.

    A digest row is written the hour its move is found, but the note stays
    silent - Telegram does not notify on an edit - so a reader who wants to know
    NOW has to keep opening it. This is the buzz: ticker, name, size, and a
    pointer at the note. The calendar context stays in the note, one tap away.

    It is deleted when the next note opens, so what remains is a clean run of
    notes rather than a scroll of pings around them.
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


def pending_pings(events: "list[dict]", pinged: dict, now: datetime,
                  window: "tuple[int, int] | None" = None,
                  hidden: "set[str] | frozenset" = frozenset()) -> "list[dict]":
    """Digest rows that have appeared in the OPEN note and not yet been announced.

    Keyed on the TIER and not merely on where the event sits right now. An event
    stays open for the rest of its trading day, so a row found at the noticeable
    level in the morning can be a push by the afternoon - and a channel test
    would buzz for it, then push it, and the reader would be interrupted twice
    for one move. A push tier never pings; it gets the message with the story in
    it, which is the whole distinction between the two.

    The same freshness rule as a push, and for the same reason: without it the
    first run after the mute comes off would buzz once for every row in the
    history rather than for what just happened.

    AND THE SAME WINDOW THE NOTE ITSELF USES, which is what stops the ping and
    the note disagreeing. A ping says "a row just appeared in the note below";
    it is the interim signal that exists only because Telegram does not notify
    on an edit. Once a note's period closes, its rows have been said properly
    and its pings are swept - so an event from a CLOSED period must never buzz
    again. Without this bound it did: `sweep_pings` clears the ledger as the
    next note opens, and `pending_pings` ran straight down the same event table
    and re-announced everything still inside the 48-hour freshness rule. Seen
    live on 19 Sep 2026 - the note for 19-21 Sep opened empty and correct, and
    two rows from the 17th and 18th, already published by the note that had
    just closed, buzzed again beside it.

    No open note means no ping. Nothing is lost: the next note to open carries
    the stretch through `carried_from`, and the rows buzz when it does.

    Nor for a row the note does not show (`hidden`, see on_channel_elsewhere).
    """
    from tremor.routing import PUSH_TIERS

    if window is None:
        return []
    start, end = window
    out = [e for e in events
           if str(e.get("channel") or "") == "digest"
           and str(e.get("tier") or "") not in PUSH_TIERS
           and str(e.get("event_id", ""))
           and str(e.get("event_id", "")) not in pinged
           and str(e.get("event_id", "")) not in hidden
           and start <= float(e.get("hour_utc", 0)) < end
           and _fresh(e, now)]
    out.sort(key=lambda e: int(e["hour_utc"]))
    return out


def _ping_message_id(value) -> int:
    """A ping record is either the Telegram id or `{id, hash}` after restyle."""
    if isinstance(value, dict):
        return int(value["id"])
    return int(value)


def _ping_hash(value) -> str:
    if isinstance(value, dict):
        return str(value.get("hash") or "")
    return ""


def _remove_ping(cfg: Config, event_id: str, value) -> bool:
    """Deletes one ping. The bot is an administrator of a public channel and
    can delete any message there; should Telegram refuse anyway, the ping is
    struck through by an edit instead, so it never goes on claiming a row.
    True if it is deleted or struck."""
    from price_monitor.notifier import delete_telegram_message

    message_id = _ping_message_id(value)
    try:
        if delete_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                   message_id):
            return True
    except TelegramError as exc:
        log.warning("Could not delete ping %s: %s", event_id, exc)
        return False
    first = (value.get("text") if isinstance(value, dict) else "") or ""
    first = first.split("\n", 1)[0]
    try:
        edit_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, message_id,
                              f"<s>{first}</s>" if first else "<s>ping</s>")
    except TelegramError as exc:
        log.warning("Could not strike ping %s through either: %s", event_id, exc)
        return False
    log.info("Ping %s struck through: Telegram would not delete it", event_id)
    return True


def sweep_pings(cfg: Config, store: dict) -> int:
    """Removes every outstanding ping. Called as the next note opens.

    An id is dropped from the state once it is handled; one Telegram would
    neither delete nor edit is dropped too, because retrying it every hour for
    ever would be a leak dressed as diligence.
    """
    outstanding: dict = store.get(PINGS) or {}
    if not outstanding:
        return 0
    gone = sum(_remove_ping(cfg, event_id, value)
               for event_id, value in list(outstanding.items()))
    store[PINGS] = {}
    if gone < len(outstanding):
        log.warning("Cleared %d of %d pings; Telegram refused the rest - check the "
                    "bot's admin rights in the channel", gone, len(outstanding))
    return gone


def _still_a_digest_ping(event: dict | None,
                         hidden: "set[str] | frozenset" = frozenset()) -> bool:
    """True only while this run's table still puts the row in the note."""
    from tremor.routing import PUSH_TIERS

    if event is None:
        return False
    return (str(event.get("channel") or "") == "digest"
            and str(event.get("tier") or "") not in PUSH_TIERS
            and str(event.get("event_id")) not in hidden)


def _drop_ping(cfg: Config, outstanding: dict, event_id: str, value) -> bool:
    """Removes a ping whose row the open note no longer shows. Drops the id."""
    gone = _remove_ping(cfg, event_id, value)
    outstanding.pop(event_id, None)
    return gone


def restyle_pings(cfg: Config, store: dict, events: "list[dict]",
                  labels: dict[str, str],
                  window: "tuple[int, int] | None" = None,
                  hidden: "set[str] | frozenset" = frozenset()) -> int:
    """Re-edits outstanding pings whose rendered text no longer matches, and
    deletes the ones with nothing left to point at.

    A ping is a claim that the row is in the note below it. A recompute, or a
    raised floor, can drop that row while the ping is still on the phone;
    rewriting it as ticker · name with no size, and still saying "Added to
    digest", is a lie. Those pings are deleted instead. An empty table is left
    alone: that is "the pipeline did not run", not "every live row vanished".

    A ping can also be orphaned by TIME rather than by a recompute: its event
    belongs to a period that has closed, so the note now on screen does not
    show it and never will. `sweep_pings` clears the ledger as a note opens,
    which covers the ordinary case - but a ping created after that sweep, in
    the same run, is left pointing at a note that cannot contain it. That is
    how messages 24 and 25 survived the 19 September run. Any ping outside the
    open note's window is deleted here, so the invariant holds in both
    directions: a ping exists only while the note beneath it shows its row.

    And a row the note stops showing because the move is on the channel
    elsewhere (`hidden`: the day grew into a push, or the move was pushed)
    takes its ping with it.
    """
    outstanding: dict = store.get(PINGS) or {}
    if not outstanding:
        return 0
    by_id = {str(e.get("event_id")): e for e in events}
    edited = dropped = 0
    for event_id, value in list(outstanding.items()):
        event = by_id.get(str(event_id))
        if window is not None and event is not None:
            hour = float(event.get("hour_utc", 0))
            if not window[0] <= hour < window[1]:
                _drop_ping(cfg, outstanding, str(event_id), value)
                dropped += 1
                continue
        if not _still_a_digest_ping(event, hidden):
            if not events:
                continue
            _drop_ping(cfg, outstanding, str(event_id), value)
            dropped += 1
            continue
        text = format_ping(event, labels)
        mark = _fingerprint(text)
        if mark == _ping_hash(value):
            continue
        message_id = _ping_message_id(value)
        try:
            edit_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                  message_id, text)
        except TelegramError as exc:
            log.error("Could not restyle ping %s: %s", event_id, exc)
            continue
        outstanding[event_id] = {"id": message_id, "hash": mark, "text": text}
        edited += 1
    if dropped:
        log.info("Pings deleted (no longer in the open note): %d", dropped)
    return edited + dropped


def _sent_hour(value) -> float:
    """A sent record is the hour, or `{hour, id, hash}` after restyle ids landed."""
    if isinstance(value, dict):
        return float(value.get("hour") or 0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sent_message_id(value) -> "int | None":
    if isinstance(value, dict) and value.get("id") is not None:
        return int(value["id"])
    return None


def pending(events: "list[dict]", sent: dict, now: datetime) -> "list[dict]":
    """The pushes that are due and have not gone out.

    Only pushes. A digest row needs no record of having been written: its note
    is rendered whole from the events table every run and edited if it changed,
    so "already sent" is a question the digest side never has to ask.

    A push whose day has already grown into a rarer one is not sent at all: the
    rarer one is, and it would only be deleted beside it (follow_up).
    """
    pushes = [e for e in events
              if str(e.get("channel") or "") == "push"
              and str(e.get("event_id", ""))
              and str(e.get("event_id", "")) not in sent
              and superseder(e) is None
              and _fresh(e, now)]
    pushes.sort(key=lambda e: int(e["hour_utc"]))
    return pushes


def _prune(sent: dict, now: datetime) -> dict:
    """Forgets what is too old to matter, so the state file stays small.

    Kept a little longer than an event can be delivered, so that an id is never
    dropped while its event is still deliverable and re-sent as a result.
    """
    cutoff = now.timestamp() - 4 * STALE_AFTER_HOURS * 3600
    return {k: v for k, v in sent.items() if _sent_hour(v) >= cutoff}


def maybe_deliver(cfg: Config, state: dict, now: datetime | None = None) -> int:
    """Sends whatever is due. Returns how many Telegram messages went out or changed.

    Failures are logged and swallowed: this runs inside the hourly monitoring
    loop, and a Telegram outage must not bring the whole run down. A push is
    marked sent only once its message has actually gone, and a note's part is
    remembered only once it is up, so a failure means a retry on the next run
    rather than a loss.
    """
    now = now or datetime.now(timezone.utc)
    if cfg.tremor_alerts_muted:
        return 0

    events = load_events(cfg)
    # An empty table must not mass-delete pings: that is "the pipeline did not
    # run", not "every live row vanished". A missing row among a live table is
    # deleted in restyle_pings.

    from price_monitor import follow_up
    from tremor import routing

    store = state.setdefault(STATE_KEY, {})
    sent: dict = store.setdefault(_SENT, {})
    pushes = pending(events, sent, now)

    # Open this period's note if its hour has come and it is not open already.
    # Nothing else ever creates one: a period whose hour passed unopened is
    # picked up by the next note instead (see carried_from).
    digests: dict = store.setdefault(DIGEST_STATE, {})
    current = routing.digest_slot(int(now.timestamp()))
    if str(current) not in digests and due_to_open(current, now):
        # Before the note, never after: the pings are the interim signal that a
        # row appeared, and the note they were standing in for is about to say
        # it properly. Clearing them afterwards would leave a window where both
        # are on screen claiming the same moves.
        swept = sweep_pings(cfg, store)
        if swept:
            log.info("Cleared %d ping(s) ahead of the %s note", swept, current)
        digests[str(current)] = {"ids": [], "hashes": [],
                                 "from": carried_from(digests, current),
                                 "to": routing.next_digest_slot(current)}

    # After opening, so a note created this run is tidied with the rest, and on
    # every run, so records written before this existed are repaired in place.
    straightened = tidy_windows(digests)
    if straightened:
        log.info("Straightened %d overlapping note window(s)", straightened)

    # The archive is ninety thousand events, so it is read once for the whole
    # run and only when there is something to render with it.
    calendar = _calendar(cfg) if (
        pushes or digests or store.get(_SENT) or store.get(follow_up.TRACKED)
    ) else None

    labels = _labels()
    pushed = posted = edited = 0

    for event in pushes:
        text = format_push(event, labels, calendar)
        try:
            message_id = send_telegram_message(
                cfg.telegram_bot_token, cfg.telegram_chat_id, text)
        except TelegramError as exc:
            log.error("Failed to send Tremor push %s: %s", event.get("event_id"), exc)
            continue
        mark = _fingerprint(text)
        sent[str(event["event_id"])] = {
            "hour": int(event["hour_utc"]), "id": int(message_id), "hash": mark,
        }
        # Remembered so a later correction edits this very message rather
        # than sending another. Kept for TRACK_HOURS.
        follow_up.track(store, event, message_id, mark, text)
        save_state(cfg.state_path, state)
        pushed += 1

    # What the note and the pings must not show, now that this run's pushes
    # are out: a move already on the channel as a push, and a day's lower
    # rows once the day grew into one.
    hidden = on_channel_elsewhere(events, sent)

    # The buzz for a digest row. Sent after the pushes so that on an hour
    # carrying both, the message with the whole story arrives first and the
    # throwaway line second.
    pings: dict = store.setdefault(PINGS, {})
    buzzed = 0
    # The window of the note that is open right now, so a ping cannot announce
    # a row the note beneath it does not show.
    open_note = digests.get(str(current))
    open_window = note_window(current, open_note) if open_note else None
    for event in pending_pings(events, pings, now, open_window, hidden):
        text = format_ping(event, labels)
        try:
            message_id = send_telegram_message(
                cfg.telegram_bot_token, cfg.telegram_chat_id, text)
        except TelegramError as exc:
            log.error("Failed to send ping %s: %s", event.get("event_id"), exc)
            continue
        pings[str(event["event_id"])] = {"id": int(message_id),
                                         "hash": _fingerprint(text), "text": text}
        save_state(cfg.state_path, state)
        buzzed += 1
    if buzzed:
        log.info("Pings sent: %d", buzzed)
    restyled = restyle_pings(cfg, store, events, labels, open_window, hidden)
    if restyled:
        save_state(cfg.state_path, state)
        log.info("Pings restyled or removed: %d", restyled)

    for slot in sorted(int(key) for key in digests):
        record = digests[str(slot)]
        table_rows = digest_rows(events, note_window(slot, record), now)
        rows = [e for e in table_rows if str(e.get("event_id")) not in hidden]
        # A NOTE NEVER UN-SAYS SOMETHING. It is rendered whole from the events
        # table every run, which is what lets a late event appear and a
        # recomputed-away one go - right for one row among several, wrong for
        # all of them at once. A change to what qualifies (a retuned ladder, a
        # moved floor) can otherwise empty a note the reader has already read
        # and been pinged about, which reads as forgetting rather than
        # correcting.
        #
        # So a note that has had rows keeps them until its period closes. A
        # recompute dropping one row of three still shows; only the
        # all-or-nothing case is held. A row the note stops showing because
        # its move is on the channel as a push is not that case: the table
        # still has it, and the note is edited without it.
        if not table_rows and record.get("rows"):
            log.info("Digest %s: recomputed to nothing, keeping the %d row(s) "
                     "already published", slot, record["rows"])
            continue
        window = note_window(slot, record)
        rows = published_only(record, rows, window, now)
        # A NOTE INTERRUPTS ONLY WHILE ITS PERIOD IS OPEN, and for the short
        # grace that lets its own last hour land. After that it is a record: it
        # is still rendered and still corrected in place, silently, but it never
        # grows a new part again.
        #
        # Every note here is re-rendered from the events table on every run for
        # as long as DIGEST_TRACK_HOURS keeps it, which is ten days. Without
        # this bound, anything that changes the table changes closed notes too,
        # and the rows they gain go out as new messages - a burst of alerts,
        # now, for hours that were scored days ago. That is not a hypothetical:
        # a cold rebuild grew the note for Mon 14 -> Sat 19 from 5 rows to 19
        # seven hours after it had closed, and posted the fourteen it gained as
        # two new messages. The rows were right; the interruption was not.
        #
        # A note that has never posted at all is exempt. It is not a closed note
        # gaining a row, it is a first post that failed and is being retried,
        # and refusing it would lose the only copy of that period.
        may_grow = (not record["ids"]
                    or now.timestamp() < window[1]
                    + DIGEST_GROW_AFTER_CLOSE_HOURS * 3600)
        texts = format_digest(rows, labels, window, calendar, now)
        held = max(0, len(texts) - len(record["ids"])) if not may_grow else 0
        made, changed = _write_digest(cfg, slot, record, texts, state,
                                      may_grow=may_grow)
        posted += made
        edited += changed
        if may_grow:
            # WHAT THIS NOTE HAS ACTUALLY SAID, kept while it can still say more
            # and frozen the moment it cannot. See published_only.
            record["events"] = [str(e.get("event_id")) for e in rows]
        if rows:
            record["rows"] = len(rows)
        if made or changed:
            log.info("Digest %s: %d part(s) posted, %d edited (%d event(s))",
                     slot, made, changed, len(rows))
        if held:
            log.info("Digest %s: closed, so %d late part(s) were not posted",
                     slot, held)

    if pushes:
        log.info("Tremor pushes sent: %d of %d due", pushed, len(pushes))

    # THE PUSHES ALREADY ON THE CHANNEL, after this run's are out: a day's
    # lower push is deleted once the day's rarer one is delivered, and every
    # other change - a healed bar, a move that was not a jump once its hour
    # completed - is corrected in place by an edit. See follow_up.
    corrected = follow_up.apply(cfg, state, events, calendar, now)
    if corrected:
        save_state(cfg.state_path, state)
        log.info("Pushes corrected or removed: %d", corrected)

    store[_SENT] = _prune(sent, now)
    store[DIGEST_STATE] = _prune_digests(digests, now)
    return pushed + posted + edited + corrected + buzzed + restyled

"""Delivers Jump events to Telegram: the pushes, and the weekly note.

The detector decides everything about WHAT to say - the word, which channel an
event belongs to, when it was found (jump.jumps, jump.routing). This module
decides what is on the channel because of it, and keeps that in line.

THE CHANNEL IS PRIVATE and the bot is an administrator of it: it can edit any of
its messages at any age and delete one within Telegram's 48 hours (past them a
message that has to go is struck through instead).

TWO KINDS OF MOVE, and the difference is how loudly they arrive. A push -
`high` and up - goes out at once and rings. A `noticeable` move is a row in
the week's note, which is edited in place and so stays silent; a ping line
rings instead and points up at it. The moves one run finds share messages: its
pushes in one, its pings in one more, biggest first, and only the run's first
message rings (format_message).

ONE NOTE A WEEK, opened at the first run after the week's last funds close
(jump.routing) right after the economic calendar's own message
(weekly_digest.py), and curated for that week: every run re-reads the
events table and brings every message of the week in line with it - see "the
week" below for exactly what that means. What belongs to an earlier note is
history and is never touched.

AN EVENT IS 24 HOURS FROM ITS FIRST MOVE BEING FOUND, and it rings only inside
them. After that it may still be corrected or removed, silently. This is load-bearing: the
events table holds the whole history, so without it the first run after a mute
would deliver years of alerts at once. An EMPTY events table changes nothing at
all: it cannot tell "nothing happened" from "the pipeline did not run".

THE MUTE (Config.jump_alerts_muted) lives in config.yaml, so "we are
deliberately silent" is visible where the code is.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pandas as pd

from price_monitor import economic_calendar
from price_monitor import telegram_ledger as ledger
from price_monitor.config import Config
from price_monitor.notifier import (TelegramError, TelegramUnsure, edit_telegram_message,
                                    send_telegram_message)
from price_monitor.state import save_state

log = logging.getLogger("price_monitor.jump_delivery")

# Telegram rejects a message over 4096 characters outright rather than
# truncating it, so the digest is cut into parts. The headroom covers the
# "part N of M" in each part's header, which would otherwise have to be counted
# recursively.
_MESSAGE_LIMIT = 4000

STATE_KEY = "jump_delivery"

# How rare the move was, as a colour. A SQUARE, against the circles the economic
# calendar uses for a release's impact (economic_calendar.IMPACT_EMOJI): the same
# four hues carry the same "how much should I care", and the shape says which
# kind of thing the line is - something the market did, or something that was on
# the schedule - without the reader having to read the words first.
#
# Ordered like the words themselves, so a note sorted by word is also sorted by
# colour, and a long note can be skimmed down its left edge.
TIER_EMOJI = {"noticeable": "⬜", "high": "🟨", "major": "🟧", "extreme": "🟥"}


# The note's blocks, in the basket's order (jump.basket.BLOCKS), each under a
# name-line: what moved together is read together (the user's, 2026-10-08).
# Not 📈 or 📉, which say a row's direction.
BLOCK_LINES = {"equity": ("🏢", "EQUITY"), "rates": ("🏛", "RATES"),
               "credit": ("💳", "CREDIT"), "energy": ("🛢", "ENERGY"),
               "precious_metals": ("🥇", "PRECIOUS METALS"),
               "industrial_metals": ("⚙️", "INDUSTRIAL METALS"),
               "agriculture": ("🌾", "AGRICULTURE"), "FX": ("💱", "FX"),
               "crypto": ("🪙", "CRYPTO")}


def block_line(block: str, continued: bool = False) -> str:
    """A block's name-line in the note; "continued" at the top of a later part."""
    icon, name = BLOCK_LINES.get(block, ("▪️", str(block).upper()))
    return f"━━━ {icon} <b>{name}</b>{' · continued' if continued else ''} ━━━"


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
    half-year σ of that kind of gap (jump.jumps).
    """
    flag = event.get("overnight")
    try:
        return bool(flag) and not pd.isna(flag)
    except (TypeError, ValueError):
        return bool(flag)


def _weekly(event: dict) -> bool:
    """A round-the-week currency pair's gap is the WEEKEND - Friday 17:00 to
    Sunday 17:00 New York - and saying "overnight" or "the open" of it would
    send the reader to the wrong chart. The Brazilian real is in the FX block
    but keeps a daily session, so its gaps are nights and weekends."""
    template = event.get("template")
    if isinstance(template, str) and template:
        return template == "fx_continuous"
    return str(event.get("block") or "") == "FX"


def _gap_kind(event: dict) -> str:
    """What kind of close came before a gap: "night" or "weekend".

    Carried on the row by jump.jumps, because a gap is judged against the
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
    """asset_id -> the name a message leads with (the ticker, or the instrument's
    own name: jump.basket.Asset.shown_as), from the basket definition, read once
    per process."""
    try:
        from jump.basket import load_basket

        return {a.asset_id: a.shown_as for a in load_basket().instruments}
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("Could not read the basket composition: %s", exc)
        return {}


def _ticker(asset_id: str) -> str:
    return _tickers().get(asset_id) or str(asset_id).split(":")[-1]


@lru_cache(maxsize=1)
def _blocks() -> dict:
    """asset_id -> block, from the basket definition, read once per process."""
    try:
        from jump.basket import load_basket

        return {a.asset_id: a.block for a in load_basket().instruments}
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("Could not read the basket's blocks: %s", exc)
        return {}


def _block(event: dict) -> str:
    """The event's block: as the detector wrote it, else the basket's."""
    block = event.get("block")
    if isinstance(block, str) and block:
        return block
    return _blocks().get(str(event.get("asset_id")), "other")




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
        from jump.basket import load_basket

        return {a.asset_id: a.label for a in load_basket().instruments}
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("Could not read basket labels: %s", exc)
        return {}


def _calendar(cfg: Config) -> "economic_calendar.Timeline | None":
    """The economic calendar archive in time order, or None if it cannot be read.

    Read and sorted once a run; every push and note row then looks up its own
    hours in it (economic_calendar.Timeline). A push must not be lost because
    the calendar is missing: the context is an addition to the message, and an
    alert without it is far better than no alert at all.
    """
    try:
        return economic_calendar.Timeline(economic_calendar.load_events(
            economic_calendar.store_path(cfg.calendar_dir)))
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("calendar could not be read, sending without context: %s", exc)
        return None


def load_events(cfg: Config) -> "list[dict]":
    """Every routed event as a plain dict.

    Returns an empty list rather than raising when the parquet file is absent.
    It is produced by python -m jump.jumps, and the hourly monitoring run must
    not fall over because a pipeline step has not been run yet.
    """
    paths = [cfg.jump_events_path]
    if not any(os.path.exists(p) for p in paths):
        return []

    try:
        import pandas as pd
    except ImportError:                          # pragma: no cover - defensive
        log.warning("pandas is not available; Jump delivery skipped")
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
    """A jump's size, " · 11.0×σ": |move| over its half-year σ (jump.jumps),
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
    big that is against the instrument's own half-year (jump.jumps). The
    colour of the square is the word - noticeable, high, major, extreme - so
    the word is not written out.

    Only what the detector measures is said: the move, its size in σ, and how
    long it has been since the instrument was at least this rare (rarest_line).
    How the move held at the close is on the time line (check_suffix).
    """
    tier = str(event.get("tier") or "noticeable")
    emoji = TIER_EMOJI.get(tier, "⚪")
    when = datetime.fromtimestamp(int(event["hour_utc"]), tz=timezone.utc)

    asset_id = str(event.get("asset_id", ""))
    label = labels.get(asset_id) or asset_id.split(":")[-1]
    move = _clean(event.get("r"))
    # The ticker leads where it is the same everywhere (SPY, EUR/USD): it is what
    # the reader types into a chart. Where it is one vendor's code (Yahoo's ^N225
    # and ZS=F, Sina's CAD for the LME's copper) the instrument's own name leads
    # instead (jump.basket.Asset.display; the user's, 2026-11-14). The move shares
    # that line, because it is the first thing anyone wants, and its size in σ ends it.
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
    """ · close in 5h, then · close 80% - the held check, on the time line.

    Every move is checked at the funds' close after it was found
    (jump.jumps.held_at_close): "close" when that is the close of the day it
    was found on in New York, "next close" when it is a later one - a move in
    the closing hour, after it, or on a weekend. Until then it counts down in
    hours, from the run's own clock (`now_utc`, set by delivery); after, the
    share of the move still there - 100% held exactly, 120% kept going, -20%
    reversed. Nothing without the stage's columns."""
    from jump.routing import EXCHANGE_TZ

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
    # Past the close with no answer yet (the scoring has not run since): it waits.
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

    jump.jumps.rarest_since. How long since the instrument last
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
# WHY THE DAILY INDEX AND NOT AN HOURLY PRODUCT. Twelve Data does not carry the
# VIX index at all, and the tradable futures ETF that tracks it does not stand in
# for it. What a reader wants here
# is the regime, and a regime is slow: an index that updates
# once a day and reaches back to 1990 describes it better than a decaying
# futures product that starts in 2011.
#
# The file is named for FRED because that is where its history came from; it
# holds the union of FRED and CBOE's own daily file (see jump.cboe). CBOE
# posts the close the same evening, which is what keeps the gauge from sitting
# three calendar days behind across a weekend.
VIX_PATH = os.path.join("data", "jump", "vix", "fred_VIXCLS.parquet")



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
        from jump import vix as vix_module

        series = pd.read_parquet(VIX_PATH).sort_values("day").reset_index(drop=True)
        return vix_module.score(series)
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("Could not read the VIX series: %s", exc)
        return None


def _stress_open_since(scored: "pd.DataFrame", hour_utc: int) -> "int | None":
    """When the stress episode covering this hour began, if one does.

    The window is twenty-four REFERENCE hours long - the hours the
    basket's anchor exchange is open - rather than twenty-four clock hours, so a
    Friday spike is still live on Monday morning. Counted by walking those hours
    forward from the spike, which is at most a few dozen steps, rather than by
    materialising every reference hour since 1990.
    """
    from jump import sessions, windows as w

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


# The window for scheduled news on a push, around the moved bar (`hour_utc` is
# its start): from two hours before it to its close, three hours in all - long
# enough to cover a release the instrument was still digesting and short enough
# that what it names is plausibly the cause;
# measured over every push in the record, a three-hour window holds a median of
# zero high-impact events and three at the ninetieth percentile, so the line
# stays readable.
CALENDAR_LOOKBACK_HOURS = 2
# And the bar's own hour after its start: a release at 14:30 is the cause of
# the 14:00 bar's move. One after the bar closed came after the move and cannot
# have caused it, so the window ends at the close.
#
# This costs no waiting. The archive is a SCHEDULE, not a log: it carries the
# releases announced ahead of time, currently a few hundred of them reaching
# weeks into the future. So the bar's hour is already known when the
# push is written, and the line is complete in the first message rather than
# arriving with a later edit.
CALENDAR_LOOKAHEAD_HOURS = 1

# High alone, as the weekly calendar shows (economic_calendar.SHOWN_IMPACTS;
# the user's, 2026-11-13: Medium, shown until then, is left out of both). Low
# is excluded for the reason it always was: it is dominated by bank holidays and
# minor prints, and naming those would turn the most important line of the most
# important message into noise.
#
# All of them are listed rather than capped: "and two more" would hide the tail
# of a busy morning, which on a busy morning is the half worth reading.
CALENDAR_IMPACTS = economic_calendar.SHOWN_IMPACTS


def calendar_context(hour_utc: int,
                     calendar: "economic_calendar.Timeline | list[dict] | None") -> str:
    """What was scheduled around the move - before it and just after.

    Naming the release tells the reader the move has a known cause and they can
    stop looking for one. NOTHING IS PRINTED WHEN NOTHING WAS SCHEDULED. Most
    pushes have no High release in the window (55% had none even with Medium
    counted), so a "none scheduled" line would appear on most messages - and a
    line that usually says nothing stops being read, taking the part that does
    say something with it. Silence carries the same fact in no space at all.
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


def push_parts(event: dict, labels: dict[str, str],
               calendar: "economic_calendar.Timeline | list[dict] | None" = None) -> "tuple[str, str]":
    """(the move, the news around it) - a push's two parts, apart, so that a
    message carrying several pushes can say the news once (format_message)."""
    body = describe(event, labels)
    if event.get("story"):
        body += "\n" + event["story"]
    if event.get("vote"):
        body += "\n" + event["vote"]
    return body, _escape(calendar_context(int(event["hour_utc"]), calendar))


def format_push(event: dict, labels: dict[str, str],
                calendar: "economic_calendar.Timeline | list[dict] | None" = None) -> str:
    """One interrupting alert, as it reads when it is alone in its message.

    Ordered so the reader meets one instrument first and the day second: the
    move written out in full, then the news scheduled around it. The fear gauge
    lives on the weekly note rather than here: the running note carries the
    regime once for all of them.
    """
    body, context = push_parts(event, labels, calendar)
    return format_message([dict(form=PUSH, size=0.0, body=body, context=context,
                                hour=int(event["hour_utc"]))])


# ONE MESSAGE A RUN, NOT ONE PER MOVE. Big news moves dozens of instruments in
# the same hour: the FOMC hour of 2024-12-18 found 91 events at today's 6-sigma
# bottom, 131 at 3.9, and Telegram takes about twenty messages a minute into a
# channel. So the pushes a run finds go out together in one message, and its
# pings in one more - this delivery replayed over the year to 2026-10-01 at a
# 3.9-sigma bottom sends 26.0 messages a week for 69 events, and the FOMC
# hour's run 5 alert messages, against 131 (8.6 a week for 14.5 events at 6
# sigma; bars to 2026-10-08). Each move keeps its
# own life inside the message (the week below): it is edited there, and leaves
# it when it turns rarer and rings again in the run that finds that.
#
# BIGGEST FIRST. Inside a message the moves are ordered by size in σ, largest
# to smallest, pushes before pings. The note is a record and runs by time; a
# message is an alarm and leads with what matters most.
#
# A message is cut at this many characters when it is first sent, rather than at
# Telegram's 4096: its moves stay with it for their lives, and a story line or
# a filled-in close has to fit later without moving anything to another
# message.
MESSAGE_BUDGET = 3000


def format_message(members: "list[dict]") -> str:
    """One alert message from its moves, each {form, size, body, context, hour}.

    Pushes first, then pings, each by size, biggest first. The news scheduled around the
    pushes follows them, each list once: a run's pushes are nearly always the same hour,
    so it is usually one list. When they are not, each list names the hour it is around
    - even when only one of the hours had any."""
    pushes = sorted((m for m in members if m["form"] == PUSH), key=lambda m: -m["size"])
    rows = sorted((m for m in members if m["form"] == ROW), key=lambda m: -m["size"])
    blocks = []
    if pushes:
        blocks.append("\n\n".join(m["body"] for m in pushes))
        news: dict = {}
        for m in sorted(pushes, key=lambda m: int(m.get("hour") or 0)):
            if m["context"]:
                news.setdefault(m["context"], int(m.get("hour") or 0))
        # Named by hour whenever the pushes are of more than one hour: a single
        # list under moves of 09:00 and 10:00 would read as the news of both.
        if len({int(m.get("hour") or 0) for m in pushes}) == 1:
            blocks.extend(news)
        else:
            days = {datetime.fromtimestamp(h, tz=timezone.utc).date() for h in news.values()}
            for context, hour in news.items():
                at = datetime.fromtimestamp(hour, tz=timezone.utc)
                clock = at.strftime("%H:%M" if len(days) == 1 else "%d.%m %H:%M")
                blocks.append(context.replace("Nearby economic events (",
                                              f"Nearby economic events around {clock} UTC (",
                                              1))
    if rows:
        blocks.append("\n".join(m["body"] for m in rows))
    return "\n\n".join(blocks)


def format_digest(events: "list[dict]", labels: dict[str, str],
                  window: "tuple[int, int]",
                  calendar: "economic_calendar.Timeline | list[dict] | None" = None,
                  now: datetime | None = None) -> "list[str]":
    """One note, whole, split into parts Telegram will accept.

    BY BLOCK, THEN BY TIME. The rows stand in their blocks, in the basket's
    order, each block under its name-line (block_line) and left out when it
    has none: a rates week reads as one. Inside a block a period read top to
    bottom runs in the order it happened; leading with the biggest row would
    buy nothing, because the note does not notify - the ping does. Moves in the
    same hour are the one case time cannot separate, and there the biggest in
    σ goes first.

    The order runs ACROSS the parts, not within each. A long note is cut into
    several messages, and sorting each part on its own would restart the clock
    at every cut - so the rows are ordered once. A cut keeps a block whole (the
    user's, 2026-10-08): a block that does not fit what is left of a message
    opens the next one. Only a block longer than a message - the first
    message's room counted after the header - is cut where the budget runs out,
    and the part it goes on in says so with the block's name-line, "continued".

    The header states the period the note speaks for, from the evening of one
    week's last funds close to the next's. A note in several parts repeats the
    period in every part's first line and names the part and the count on the
    second, so a later part reads on its own (the user's, 2026-11-05); the VIX
    line and the "updated as moves are found" stay in the first.
    """
    from jump.jumps import WORDS as TIERS

    now = now or datetime.now(timezone.utc)
    start, end = int(window[0]), int(window[1])
    opened = datetime.fromtimestamp(start, tz=timezone.utc)
    closes = datetime.fromtimestamp(end, tz=timezone.utc)
    live = now.timestamp() < end

    rank = {name: i for i, name in enumerate(TIERS)}
    ordered = sorted(events, key=lambda e: (int(e["hour_utc"]),
                                            -rank.get(str(e.get("tier")), 0), -_size(e)))
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
    lead = f"📋 <b>Digest</b> - {format_day(opened)} to {format_day(last)}"
    header = f"{lead}\n{count}" + (" - this message is updated as moves are found"
                                   if live else "")
    # A later part's header: the part number goes in once the parts are counted.
    follow = f"{lead}\n{count}"
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
        if event.get("vote"):
            line += "\n" + event["vote"]
        context = calendar_context(int(event["hour_utc"]), calendar)
        return f"{line}\n     {_escape(context)}" if context else line

    from jump.basket import BLOCKS

    blocks: "dict[str, list[dict]]" = {}
    for e in ordered:
        blocks.setdefault(_block(e), []).append(e)
    messages, current = [], header
    for block in sorted(blocks, key=lambda b: (BLOCKS.index(b) if b in BLOCKS else len(BLOCKS), b)):
        texts = [row(e) for e in blocks[block]]
        whole = "\n\n".join([block_line(block)] + texts)
        if len(f"{current}\n\n{whole}") <= _MESSAGE_LIMIT:
            current = f"{current}\n\n{whole}"
            continue
        # Whole in the next message. The first message's room is what the
        # header leaves, so a block that does not fit beside the header is cut
        # under it rather than leave the header alone; one longer than any
        # message starts the next too.
        if len(f"{follow}\n\n{whole}") <= _MESSAGE_LIMIT and current != header:
            messages.append(current)
            current = f"{follow}\n\n{whole}"
            continue
        opening = f"{block_line(block)}\n\n{texts[0]}"
        if current != header:
            messages.append(current)
            current = follow
        current = f"{current}\n\n{opening}"
        for text in texts[1:]:
            if len(f"{current}\n\n{text}") > _MESSAGE_LIMIT:
                messages.append(current)
                current = f"{follow}\n\n{block_line(block, continued=True)}\n\n{text}"
            else:
                current = f"{current}\n\n{text}"
    messages.append(current)

    if len(messages) > 1:
        # THE PART IS NAMED ON THE SECOND LINE. The first line is how a message
        # sent by a run whose state was lost is recognised and taken over
        # (telegram_ledger.claim), so it stays the same in every part and every
        # run of the week, while the number of parts changes as the note grows.
        # Parts that share it are taken over in the order they were sent.
        total = len(messages)
        messages = [m.replace("\n", f"\nPart {i} of {total} · ", 1)
                    for i, m in enumerate(messages, 1)]
    return messages


def ping_line(event: dict, labels: dict[str, str]) -> str:
    """One noticeable move in a ping: ticker, name, size."""
    tier = str(event.get("tier") or "noticeable")
    emoji = TIER_EMOJI.get(tier, "⚪")
    asset_id = str(event.get("asset_id", ""))
    move = _clean(event.get("r"))
    shown = f" {move * 100:+.2f}%" if move is not None else ""
    ticker = _ticker(asset_id)
    label = labels.get(asset_id) or ticker
    return (f"{emoji} <b>{_escape(ticker)}</b> · {_escape(label)}"
            f"{shown}{_sigma_multiple(event)}")


def format_ping(event: dict, labels: dict[str, str]) -> str:
    """The throwaway message that says a digest row just appeared, as it reads
    when it is alone.

    A digest row is written the hour its move is found, but the note stays
    silent - Telegram does not notify on an edit - so a reader who wants to know
    NOW has to keep opening it. This is the buzz: ticker, name, size. The
    calendar context stays in the note, one tap away.

    A line lives exactly as long as its row: gone when the row leaves the note,
    when the move becomes a push, and when the next note opens - so what
    remains is a clean run of notes rather than a scroll of pings around them.
    """
    return format_message([dict(form=ROW, size=0.0, body=ping_line(event, labels),
                                context="")])


# --- the week ------------------------------------------------------------------
#
# ONE NOTE A WEEK, opened at the first run after the week's last funds close
# (jump.routing) just after the economic calendar's own message. For that week
# every message is kept in line with the events table, every run; anything from
# before the note opened is history and is never touched again. The run that
# turns the week first finishes the old one - its checks at that close are in -
# and only then closes it and opens the new note.
#
# HELD AT THE CLOSE. Every message counts down on its time line to
# the funds' close its move is checked at, and then says how much of the move
# was still there (check_suffix). The edits are silent and are not a change to
# the event: they add no story.
#
# AN EVENT is 24 hours of real time from its first move being found
# (jump.jumps.event_starts). Its word is its rarest reading's and the numbers
# it shows its biggest reading's. What happens to it on the channel:
#
#   within its 24 hours     rarer (any cause but a detector update) -> it leaves
#                           its message - and the note - and goes out again in
#                           this run's message at the new word, and rings.
#                           Milder -> edited in place: a push that falls to
#                           `noticeable` shows ⬜; a `noticeable` that falls away
#                           is taken out, row and ping line. Same word, other
#                           numbers -> edited in place.
#   after its 24 hours      complete: a new move starts a new event. It changes
#                           only when a bar is corrected or arrives late, and
#                           never rings: rarer or milder is an edit - a row that
#                           becomes `high` leaves the note and its ping line
#                           becomes the push, in the same message - and gone is
#                           taken out, for good.
#
# A MESSAGE carries the moves it was sent with (each event's `message`), is
# edited as they change and deleted once none is left in it (_sync_messages).
#
# A CHANGED EVENT TELLS ITS STORY: one line under the time, every state it has
# been in with why it moved (story_line). A clean event says nothing.
#
# THE SOURCES' VOTE (jump.verify) is said under the move (vote_line), the
# store's provider and every source with its own move, or `(outage)`:
#
#   not real      most sources did not see it: no longer scored. If it is on
#                 the channel, its line stays where it is with
#                 `❌ Binance(+2.03%), Coinbase(-0.10%), Kraken(-0.10%)` under
#                 it, silently; a row leaves the note. Should a reading of it
#                 come back - the bar healed, or the vote turned - or a new
#                 move come inside its 24 hours, it is an event like any other
#                 again.
#   uncertain     a tie: scored as usual, `⚠️ Yahoo(-2.50%), Alpaca(-2.50%),
#                 Sina(outage), MarketWatch(-0.50%)` under it.
#   real          nothing is added, nor for an instrument no other source
#                 carries.
#
# A detector update (a new jump.jumps.detector_version) keeps the week: its
# messages are edited to the updated numbers, silently, as corrections are -
# rarer included - and an event it no longer sees leaves as a corrected one
# does. A move it finds in hours the runs before it had already read is not
# sent at all (`updated`, an hour before the update's run).
#
# Deleting is how a message leaves; the bot is an administrator of the
# channel and deletes its messages within Telegram's 48-hour limit. Should Telegram refuse,
# the message is struck through by an edit instead.

WEEK = "week"

# A message rings only within this long of its event's first move being found.
# After that the event can still be corrected or removed, but nothing rings.
PUSH_WINDOW_HOURS = 24

# A ROW ping is deleted early - well within Telegram's 48-hour bot-deletion
# window - rather than waiting for _close_week (which may come days later).
PING_MAX_AGE_HOURS = 40

ROW, PUSH = "row", "push"

# Why an event's message changed, in the jump detector's own terms: its size is
# |move| / σ of its biggest reading, and only these move it.
BIGGER = "bigger jump"            # a new hour in the event jumped further
LATE = "arrived late"             # a bar or gap that was missing came in
PRICE = "price corrected"         # the provider revised the bar
SIGMA = "σ corrected"             # older bars revised, so the half-year yardstick moved
AWAY = "corrected away"           # no longer a jump
UNSEEN = "not real"               # most sources did not see the move (jump.verify)


def _fingerprint(text: str) -> str:
    """What a message said last time, so an unchanged one is not re-sent.

    Telegram rejects an edit whose text matches the message already there, and
    most hours change nothing: without this the run would call editMessageText
    for every live message every hour and collect an error each time.
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _rank(tier) -> int:
    from jump.jumps import WORDS

    return WORDS.index(str(tier)) if tier is not None and str(tier) in WORDS else -1


def _is_push_word(tier) -> bool:
    from jump.routing import PUSH_TIERS

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


def clean_story(story: "list[list]") -> "list[list]":
    """Compacts an event's story so that repeated adjustments to the same hour
    or back-and-forth oscillations do not accumulate in the message."""
    if not story or len(story) <= 1:
        return [list(s) for s in story]
    out: list[list] = []
    for step in story:
        item = list(step)
        if not out:
            out.append(item)
            continue
        last = out[-1]
        # 1. Skip exact duplicate step
        if (item[0] == last[0] and
                (item[1] == last[1] or (item[1] is not None and last[1] is not None and
                                        round(float(item[1]), 1) == round(float(last[1]), 1))) and
                item[2] == last[2] and item[3] == last[3]):
            continue
        # 2. Consecutive revisions of the same hour and same reason
        # (e.g. repeated price or sigma corrections of that hour)
        if (item[2] is not None and item[2] == last[2] and
                item[3] in (PRICE, SIGMA) and last[3] == item[3]):
            out[-1] = item
            continue
        # 3. Two-step oscillations (A -> B -> A), collapsing back to A
        # unless B was an explicit disappearance (corrected away / not real).
        if (len(out) >= 2 and item[0] == out[-2][0] and
                (item[1] == out[-2][1] or (item[1] is not None and out[-2][1] is not None and
                                           round(float(item[1]), 1) == round(float(out[-2][1]), 1))) and
                item[2] == out[-2][2] and
                last[0] is not None and not str(last[3]).endswith((AWAY, UNSEEN))):
            out.pop()
            continue
        out.append(item)
    return out


def story_line(story: "list[list]", headline: int) -> str:
    """✏️ 🟨 6.0×σ 02:00 → 🟧 8.1×σ 03:00 bigger jump → ✖ corrected away ...

    Every state the event has been in, oldest first, and why it moved into
    each. Empty for an event that never changed."""
    cleaned = clean_story(story)
    if len(cleaned) < 2:
        return ""
    steps = []
    for tier, size, hour, why in cleaned:
        if tier is None:
            steps.append(f"✖ {why}".rstrip())
            continue
        step = f"{TIER_EMOJI.get(tier, '⚪')} {size:.1f}×σ {_clock(hour, headline)}"
        steps.append(f"{step} {why}" if why else step)
    return "✏️ " + " → ".join(_escape(s) for s in steps)


SENT, FAILED, UNSURE, SKIPPED = "sent", "failed", "unsure", "skipped"


def _send(cfg: Config, text: str, silent: bool = False) -> "tuple[int | None, str]":
    """(message id, what became of it): SENT; FAILED - refused, or Telegram never
    reached; UNSURE - it may have taken the message without saying so, which
    the caller writes down (telegram_ledger); SKIPPED - not asked, Telegram
    having left a request unanswered earlier in this run."""
    def request():
        if silent:
            return send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                         text, silent=True)
        return send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, text)
    try:
        message_id = int(ledger.call(request))
    except ledger.Skipped:
        return None, SKIPPED
    except TelegramUnsure as exc:
        log.error("Could not send: %s", exc)
        return None, UNSURE
    except TelegramError as exc:
        log.error("Could not send: %s", exc)
        return None, FAILED
    ledger.saw(message_id)
    return message_id, SENT


def _edit(cfg: Config, message_id: int, text: str) -> bool:
    """An edit that got no answer is repeated for a day (telegram_ledger.recheck):
    it may still land, after a newer one."""
    try:
        ledger.call(lambda: edit_telegram_message(cfg.telegram_bot_token,
                                                  cfg.telegram_chat_id, int(message_id), text))
        return True
    except ledger.Skipped:
        return False
    except TelegramUnsure as exc:
        ledger.recheck(message_id)
        log.error("Could not edit message %s: %s", message_id, exc)
        return False
    except TelegramError as exc:
        if "not found" in str(exc).lower():
            ledger.settled(message_id)
        log.error("Could not edit message %s: %s", message_id, exc)
        return False


def _delete(cfg: Config, message_id: "int | None", first_line: str = "") -> bool:
    """Deletes one message. If Telegram refuses, strikes it through instead.
    True once the message no longer stands on the channel as it was."""
    from price_monitor.notifier import delete_telegram_message

    if message_id is None:
        return True
    try:
        if ledger.call(lambda: delete_telegram_message(
                cfg.telegram_bot_token, cfg.telegram_chat_id, int(message_id))):
            return True
    except TelegramError as exc:
        log.warning("Could not delete message %s: %s", message_id, exc)
        return False
    struck = f"<s>{first_line}</s>" if first_line else "<s>removed</s>"
    try:
        ledger.call(lambda: edit_telegram_message(cfg.telegram_bot_token,
                                                  cfg.telegram_chat_id, int(message_id), struck))
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

def _new_week(slot: int, version: str) -> dict:
    return {"slot": int(slot), "since": int(slot), "detector": version,
            "note": {"ids": [], "hashes": []}, "events": {}, "messages": {},
            "orphans": []}


def _expire_pings(cfg: Config, week: dict, now_ts: int) -> int:
    """Delete ROW ping messages older than PING_MAX_AGE_HOURS.

    Telegram's Bot API refuses to delete messages older than 48 hours.
    By deleting pings early (at 40 hours) the bot stays well inside that
    window and the channel never shows struck-through leftovers.

    The event stays in the week and keeps appearing in the digest note;
    only its standalone ping message is removed.
    """
    cutoff = now_ts - PING_MAX_AGE_HOURS * 3600
    changed = 0
    for rec in week.get("events", {}).values():
        if rec.get("form") != ROW or rec.get("message") is None:
            continue
        # rec["ping_at"] is when the event was first discovered (unix ts).
        # Fall back to rec["start"] (the bar's hour) for older state records.
        sent_ts = int(rec.get("ping_at") or rec.get("start") or 0)
        if sent_ts and sent_ts < cutoff:
            log.info("Ping for %s expired (%.1f h old): clearing message %s",
                     rec.get("asset", "?"),
                     (now_ts - sent_ts) / 3600,
                     rec["message"])
            rec["message"] = None
            changed += 1
    if changed:
        # Clean up messages that now carry nothing.
        changed += _sync_messages(cfg, week)
    return changed


def _close_week(cfg: Config, week: dict) -> list:
    """The week becomes history: its pings go, everything else stays as it is.
    A message that carried pings and pushes keeps its pushes. Returns what
    could not be deleted, for the next week to try again."""
    for rec in week.get("events", {}).values():
        if rec.get("form") == ROW:
            rec["message"] = None
    _sync_messages(cfg, week)
    left = [[message_id, first_line] for message_id, first_line in week.get("orphans", [])
            if not _delete(cfg, message_id, first_line)]
    _settle(week)
    return left


def _week_id(week: dict, kind: str = "alert") -> dict:
    """Which week an unsure send belongs to."""
    if kind == "note":
        return {"slot": int(week["slot"])}
    return {"slot": int(week["slot"]), "since": int(week["since"])}


def _placed_since(tracked: dict, snap: dict, at: int) -> bool:
    """Whether the event was put on the channel since `at`, under any key: a
    repeat that went out, or one already taken in from an earlier late copy."""
    return any(rec.get("asset") == snap.get("asset")
               and abs(int(rec.get("start") or 0) - int(snap.get("start") or 0))
               < PUSH_WINDOW_HOURS * 3600
               and int(rec.get("placed_at") or 0) > int(at)
               for rec in tracked.values())


def _settle(week: dict) -> None:
    """The week closes: what its unsure sends will be if they appear later is
    decided now, while the week can still be read. A ping goes with its week; a
    push stays unless something replaced it; a note part stays unless the note
    has it already."""
    for record in ledger.pending():
        kind = record.get("kind")
        if kind not in ("alert", "note") or "verdict" in record \
                or record.get("week") != _week_id(week, kind):
            continue
        if kind == "note":
            record["verdict"] = ("delete" if len(week["note"]["ids"]) > int(record["index"])
                                 else "keep")
        elif record.get("form") == ROW:
            record["verdict"] = "delete"
        else:
            replaced = all(_placed_since(week["events"], snap, record["at"])
                           for snap in record["events"].values())
            record["verdict"] = "delete" if replaced else "keep"


def _drop_late(cfg: Config, week: "dict | None", message_id: int, record: dict) -> str:
    first = record.get("first", "")
    if not _delete(cfg, int(message_id), record.get("first_html", "")) and week is not None:
        week.setdefault("orphans", []).append([int(message_id), record.get("first_html", "")])
    return f"Deleted late duplicate {message_id} (\"{first}\"): it had gone out again"


def adopt(cfg: Config, state: dict, record: dict, message_id: int) -> str:
    """A send Telegram did not answer for, found on the channel as `message_id`
    (telegram_ledger.reconcile): taken into the week when nothing replaced it -
    the run's edits then bring it up to date and nothing is sent again - and
    deleted when something did. Returns a line for the health chat."""
    week = (state.get(STATE_KEY) or {}).get(WEEK)
    first = record.get("first", "")
    verdict = record.get("verdict")
    if verdict is None and (week is None
                            or record.get("week") != _week_id(week, record["kind"])):
        verdict = "keep"                         # a week it was not settled for
    if verdict == "keep":
        return f"Found late message {message_id} (\"{first}\"): its week is over, left as it is"
    if verdict == "delete":
        return _drop_late(cfg, week, message_id, record)
    taken = f"Found late message {message_id} (\"{first}\"): taken in, nothing sent again"
    if record["kind"] == "note":
        note = week["note"]
        if len(note["ids"]) != int(record["index"]):
            return _drop_late(cfg, week, message_id, record)
        note["ids"].append(int(message_id))
        note["hashes"].append(record["fingerprint"])
        return taken
    tracked, adopted = week["events"], False
    for key, snap in record["events"].items():
        if _placed_since(tracked, snap, record["at"]):
            continue
        if record["form"] == PUSH:
            tracked[key] = dict(copy.deepcopy(snap), form=PUSH, message=int(message_id),
                                placed_at=ledger.clock())
            adopted = True
        else:
            rec = tracked.get(key)
            if rec is not None and rec.get("form") == ROW and rec.get("message") is None:
                rec.update(message=int(message_id), placed_at=ledger.clock())
                rec.pop("ping_due", None)
                adopted = True
    if not adopted:
        return _drop_late(cfg, week, message_id, record)
    week["messages"][str(int(message_id))] = {"hash": record["fingerprint"],
                                              "first": record["first_html"]}
    return taken


def note_due(state: dict, now: datetime) -> bool:
    """Whether this run opens a new note - the run the calendar goes out in,
    just before it (weekly_digest)."""
    from jump import routing

    store = state.get(STATE_KEY) or {}
    week = store.get(WEEK)
    return week is None or routing.digest_slot(int(now.timestamp())) > int(week["slot"])


# --- one event ------------------------------------------------------------------

def _in_week(reading: dict, week: dict) -> bool:
    from jump import routing
    from jump.jumps import FOUND_TO_RUN

    moment = _found(reading) + FOUND_TO_RUN
    end = routing.next_digest_slot(int(week["slot"]))
    return max(int(week["slot"]), int(week["since"])) <= moment < end


def _group(readings: "list[dict]", week: dict) -> "dict[str, list[dict]]":
    """The week's readings as events, {key: readings}. Events already on the
    channel hold their 24 hours (jump.jumps.event_starts' anchors)."""
    from jump.jumps import event_starts

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


def _why(rec: dict, members: "dict[str, dict]", peak: "dict | None",
         doubts: "dict | None" = None) -> str:
    """Why the event now shows `peak` rather than what it showed."""
    if peak is None:
        return AWAY
    seen, old_peak = rec["members"], rec.get("peak")
    pid = str(peak["reading_id"])
    if old_peak and old_peak not in members:
        gone = UNSEEN if _doubt(rec["asset"], [old_peak], doubts) else AWAY
        return f"{_clock(_hour_of(old_peak), int(peak['hour_utc']))} {gone}"
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




def _doubt(asset: str, reading_ids, doubts: "dict | None") -> "dict | None":
    """The sources' vote on one of these readings, if `doubts` holds one: an
    hour's reading by the `close` check, a gap's by the `open`."""
    for reading_id in reading_ids:
        _, _, kind, hour = str(reading_id).rsplit(":", 3)
        row = (doubts or {}).get((asset, int(hour), "close" if kind == "hour" else "open"))
        if row:
            return row
    return None


def vote_line(row: "dict | None") -> str:
    """The sources' vote under a move that is not plainly real: `❌` when
    most did not see it, `⚠️` on a tie, then the store's provider with the
    stored move and every source with its own, or `(outage)`:
    `❌ Binance(+2.03%), Coinbase(-0.10%), Kraken(-0.10%)`. Nothing for a real
    move or one no other source carries."""
    import math

    from jump import verify

    mark = {verify.NOT_REAL: "❌", verify.UNCERTAIN: "⚠️"}.get((row or {}).get("verdict"))
    if not mark:
        return ""

    def label(name: str) -> str:
        return _escape(verify.LABELS.get(name, name.capitalize()))

    def shown(move: str) -> str:
        try:
            return f"{(math.exp(float(move)) - 1) * 100:+.2f}%"
        except (TypeError, ValueError):
            return "outage"

    names = [n for n in str(row.get("verifier") or "").split(",") if n]
    moves = str(row.get("verifier_move") or "").split(",")
    parts = [f"{label(str(row.get('provider') or ''))}({shown(row.get('stored_move'))})"]
    parts += [f"{label(n)}({shown(moves[i] if i < len(moves) else '')})"
              for i, n in enumerate(names)]
    return f"{mark} " + ", ".join(parts)


def _mark(rec: dict, row: dict) -> None:
    """Most sources did not see the move: its line stays in its message with
    the mark under it, and a row leaves the note - until a reading of the
    event is back (_step)."""
    mark = vote_line(row)
    body = rec.get("body", "")
    rec.update(doubt={k: row.get(k) for k in ("verifier", "verifier_move")},
               body=f"{body}\n{mark}" if rec.get("form") == PUSH else f"{body} {mark}",
               context="")


def _leave(rec: dict) -> None:
    """The event comes off the channel: out of its message, and out of the
    note with the next render. Its message is edited without it, or deleted
    once nothing is left in it (_sync_messages)."""
    rec.update(form=None, message=None)


def _step(week: dict, key: str, readings: "list[dict]", now_ts: int,
          fresh: "list[tuple]", doubts: "dict | None" = None) -> int:
    """One event, one run: its state brought in line with the table. What has
    to go out in a new message is appended to `fresh` as (key, rec, before) -
    `before` the record to fall back to should the send fail, None for an
    event not on the channel yet. Returns how many events changed in place."""
    tracked = week["events"]
    asset, start = key.rsplit("|", 1)
    rec = tracked.get(key)
    shown_before = rec.get("tier") if rec is not None else None
    if rec is not None and rec.get("doubt"):
        if not readings:
            return 0                   # marked not real, and still nothing
        # A reading is back - the bar healed and was confirmed - or a new move
        # came inside its 24 hours: an event like any other again. Rarer is
        # measured against the word the channel last showed, so a doubt lifted
        # is a silent edit and only a move rarer than that rings.
        rec.pop("doubt")
        shown_before = next((step[0] for step in reversed(rec["story"]) if step[0]), None)
    open_ = now_ts < int(start) + PUSH_WINDOW_HOURS * 3600
    members = {str(r["reading_id"]): r for r in readings}
    peak = max(readings, key=_size) if readings else None
    # Every reading the event has shown, kept after it vanishes: one that comes
    # back was corrected back, it did not arrive late.
    snapshot = dict(rec["members"]) if rec else {}
    snapshot.update({rid: [_clean(r.get("r")) or 0.0, _clean(r.get("sigma_lt")) or 0.0]
                     for rid, r in members.items()})

    if rec is None:
        # Found now. Only an event still inside its 24 hours goes out at all, and
        # not one a detector update found only in hours read before it.
        if peak is None or not open_ or \
                all(_found(r) < int(week.get("updated", 0)) for r in readings):
            return 0
        shown = _shown(peak)
        rec = {"asset": asset, "start": int(start), "form": None, "message": None,
               "members": snapshot, "peak": str(peak["reading_id"]), "tier": shown[0],
               "shown": shown, "story": [shown[:3] + [""]], "seen": now_ts,
               "ping_at": now_ts}
        fresh.append((key, rec, None))
        return 0

    shown = _shown(peak)
    if shown == rec["shown"]:
        rec.update(members=snapshot, seen=now_ts)
        return 0

    before = dict(rec)
    why = _why(rec, members, peak, doubts)
    new_step = shown[:3] + [why] if peak is not None else [None, None, None, why]
    rec["story"] = clean_story(rec["story"] + [new_step])
    changed = 0
    promoted = _rank(shown[0]) > _rank(shown_before)
    # Moved only by a detector update re-reading hours it had read: news to no one.
    reread = all(_found(r) < int(week.get("updated", 0)) for r in readings)

    doubt = _doubt(asset, rec["members"], doubts) if peak is None and rec.get("form") else None
    if doubt:
        # Silent, inside its 24 hours or after them: an edit never rings.
        rec["story"][-1][3] = UNSEEN
        rec["story"] = clean_story(rec["story"])
        _mark(rec, doubt)
        changed += 1
    elif peak is None:
        _leave(rec)
        changed += 1
    elif promoted and open_ and not reread:
        # Rarer inside its 24 hours: it leaves its message and rings again in
        # this run's. Only here does anything go out, so an event with nothing
        # on the channel once its 24 hours are over - corrected away - stays gone.
        fresh.append((key, rec, before))
    elif rec.get("form") == ROW and _is_push_word(shown[0]):
        # Rarer after its 24 hours, or by a detector update: silent. The row
        # leaves the note and its line in the ping message becomes a push, by
        # an edit.
        rec["form"] = PUSH
        changed += 1
    rec.update(members=snapshot, peak=str(peak["reading_id"]) if peak else None,
               tier=shown[0], shown=shown, seen=now_ts)
    return changed


def _member(rec: dict, peak: dict, form: str, labels: dict, calendar) -> dict:
    """The event as a message shows it, kept on the record so that a message
    can be put back together without the events table (_close_week)."""
    if form == PUSH:
        body, context = push_parts(_render(rec, peak), labels, calendar)
    else:
        body, context = ping_line(peak, labels), ""
    rec.update(body=body, context=context, size=_size(peak), hour=int(peak["hour_utc"]))
    return {"form": form, "size": rec["size"], "body": body, "context": context,
            "hour": rec["hour"]}


def _members_of(week: dict) -> "dict[str, list[dict]]":
    """{message id: what it carries}, from the events' own records."""
    out: dict = {}
    for rec in week["events"].values():
        if rec.get("form") and rec.get("message") is not None:
            out.setdefault(str(rec["message"]), []).append(
                {"form": rec["form"], "size": float(rec.get("size") or 0.0),
                 "body": rec.get("body", ""), "context": rec.get("context", ""),
                 "hour": int(rec.get("hour") or 0)})
    return out


def _cut(members: "list[tuple]") -> "list[list[tuple]]":
    """Moves into messages of at most MESSAGE_BUDGET characters, biggest first.
    `members` are (key, member) pairs."""
    members = sorted(members, key=lambda km: -km[1]["size"])
    messages: list = []
    for item in members:
        if messages and len(format_message([m for _, m in messages[-1] + [item]])) \
                <= MESSAGE_BUDGET:
            messages[-1].append(item)
        else:
            messages.append([item])
    return messages


def _post_new(cfg: Config, state: dict, week: dict, fresh: "list[tuple]",
              groups: dict, labels: dict, calendar, ring: dict, now_ts: int) -> int:
    """This run's new moves, in as few messages as fit: the pushes, then the
    pings. Only the run's first message rings - the reader is on the channel
    after that - and a move is on the record only once its message is up. A
    push that cannot be sent is tried again by the next run; a row is in the
    note whether or not its ping went, and a ping not asked at all (the
    breaker) goes with the next run. A send Telegram did not answer for is
    written down with what it would have recorded (telegram_ledger), to be
    taken in if it appears."""
    tracked = week["events"]
    by_form: dict = {PUSH: [], ROW: []}
    for key, rec, before in fresh:
        peak = max(groups[key], key=_size)
        form = PUSH if _is_push_word(peak.get("tier")) else ROW
        by_form[form].append((key, _member(rec, peak, form, labels, calendar)))
    sent = 0
    states = {key: (rec, before) for key, rec, before in fresh}
    for form in (PUSH, ROW):
        for message in _cut(by_form[form]):
            text = format_message([m for _, m in message])
            claimed = ledger.claim(text)
            if claimed is not None:
                # Left by a run whose state was not saved: taken over, and edited
                # to this text by _sync_messages (no hash yet). It rang already.
                message_id, outcome = claimed, SENT
                week["messages"][str(message_id)] = {"hash": "", "first": _first(text)}
            else:
                message_id, outcome = _send(cfg, text, silent=not ring["left"])
            if message_id is not None and claimed is None:
                ring["left"] = False
                week["messages"][str(message_id)] = {"hash": _fingerprint(text),
                                                     "first": _first(text)}
                sent += 1
            if outcome == UNSURE:
                ledger.note_unsure(
                    "alert", text, week=_week_id(week), form=form,
                    fingerprint=_fingerprint(text), first_html=_first(text),
                    events={key: copy.deepcopy(dict(states[key][0], form=form, message=None))
                            for key, _ in message})
            for key, _ in message:
                rec, before = states[key]
                if message_id is None and form == PUSH:
                    if before is not None:
                        tracked[key] = before
                    continue
                rec.update(form=form, message=message_id)
                if message_id is not None:
                    rec["placed_at"] = now_ts
                if outcome == SKIPPED:
                    rec["ping_due"] = True
                else:
                    rec.pop("ping_due", None)
                tracked[key] = rec
            save_state(cfg.state_path, state)
    return sent


def _sync_messages(cfg: Config, week: dict) -> int:
    """Every alert message of the week brought in line with what it carries:
    edited where its text changed, deleted once nothing is left in it."""
    carried = _members_of(week)
    changed = 0
    for message_id, rec in list(week["messages"].items()):
        members = carried.get(message_id)
        if not members:
            _discard(cfg, week, int(message_id), rec.get("first", ""))
            del week["messages"][message_id]
            changed += 1
            continue
        text = format_message(members)
        mark = _fingerprint(text)
        if (mark != rec.get("hash") or ledger.rechecking(message_id)) \
                and _edit(cfg, int(message_id), text):
            changed += mark != rec.get("hash")
            rec.update(hash=mark, first=_first(text))
    return changed


def _write_note(cfg: Config, week: dict, texts: "list[str]") -> int:
    """Posts, edits and trims the note's parts. A part that is no longer needed
    is deleted, newest first, so the ids stay a prefix of the note. Only the
    note's opening rings: a part added as it grows is the note growing, and
    the pings already rang for what is in it."""
    note = week["note"]
    ids, hashes = note["ids"], note["hashes"]
    changed = 0
    for index, text in enumerate(texts):
        mark = _fingerprint(text)
        if index < len(ids):
            if (hashes[index] != mark or ledger.rechecking(ids[index])) \
                    and _edit(cfg, ids[index], text):
                changed += hashes[index] != mark
                hashes[index] = mark
            continue
        claimed = ledger.claim(text)
        if claimed is not None:
            ids.append(claimed)
            hashes.append(mark if _edit(cfg, claimed, text) else "")
            changed += 1
            continue
        message_id, outcome = _send(cfg, text, silent=index > 0)
        if message_id is None:
            if outcome == UNSURE:
                ledger.note_unsure("note", text, week=_week_id(week, "note"), index=index,
                                   fingerprint=mark, first_html=_first(text))
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
    remembered once it is up; one refused is retried by the next run, and one
    Telegram did not answer for is first looked for on the channel
    (telegram_ledger).
    """
    from jump import jumps, routing

    now = now or datetime.now(timezone.utc)
    if cfg.jump_alerts_muted:
        return 0
    ledger.use(state, int(now.timestamp()))

    readings = load_events(cfg)
    # An empty table is "the pipeline did not run", not "every event vanished"
    # and not "nothing happened": it changes nothing, the note included.
    if not readings:
        return 0
    store = state.setdefault(STATE_KEY, {})
    version = jumps.detector_version()
    now_ts = int(now.timestamp())
    changed = 0
    ring = {"left": True}

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
                changed += _pass(cfg, state, week, readings, labels, calendar, now, ring)
            left = _close_week(cfg, week)
        else:
            left = []
        week = store[WEEK] = _new_week(slot, version)
        week["orphans"] = left
        save_state(cfg.state_path, state)
    elif week.get("detector") != version:
        # A DETECTOR UPDATE KEEPS THE WEEK. What is on the channel stays and is
        # brought to the updated numbers by the ordinary pass, as any correction
        # is - an edit, silent; an event the update no longer sees leaves as a
        # corrected one does. A move it sees in hours the runs before it had
        # already read is not sent late (_step). It used to restart the week,
        # deleting every push and ping of it: on 2026-11-04 a change to
        # sessions.py's table code took eight messages and the note's ten rows.
        week["detector"] = version
        week["updated"] = now_ts - 3600
        log.info("Detector updated: the week's messages are kept, corrected where they moved")
        save_state(cfg.state_path, state)
    changed += _pass(cfg, state, week, readings, labels, calendar, now, ring)
    # Run through: whatever stray it did not claim is not wanted (a run that
    # raised part way leaves them for the next).
    ledger.delivering()
    return changed


def _pass(cfg: Config, state: dict, week: dict, readings: "list[dict]", labels: dict,
          calendar, now: datetime, ring: dict) -> int:
    """One run over one week's messages: its events, their messages, its note."""
    from jump import routing

    now_ts = int(now.timestamp())
    window = (int(week["slot"]), routing.next_digest_slot(int(week["slot"])))
    week.setdefault("messages", {})
    changed = 0
    if not week["note"]["ids"]:
        # A new note goes up before anything it opens with: the calendar, the
        # note, then the moves - the closing hour's among them.
        changed += _write_note(cfg, week, format_digest([], labels, window, calendar, now))

    from jump import verify

    # The sources' vote on each reading, said under it (vote_line).
    record = verify.votes()
    for r in readings:
        r["vote"] = vote_line(_doubt(r["asset_id"], [r["reading_id"]], record))
    groups = _group([r for r in readings if _in_week(r, week)], week)
    doubts = {k: row for k, row in record.items() if row.get("verdict") == verify.NOT_REAL}
    fresh: list = []
    for key in sorted(groups, key=lambda k: int(k.rsplit("|", 1)[1])):
        changed += _step(week, key, groups[key], now_ts, fresh, doubts)
    # A ping the breaker kept from going (Telegram silent earlier in that run)
    # goes now, inside the event's 24 hours.
    queued = {key for key, _, _ in fresh}
    for key, rec in week["events"].items():
        if (rec.get("ping_due") and rec.get("form") == ROW and rec.get("message") is None
                and key not in queued and groups.get(key) and not rec.get("doubt")
                and now_ts < int(rec["start"]) + PUSH_WINDOW_HOURS * 3600):
            fresh.append((key, rec, copy.deepcopy(rec)))
    # What stays where it is shows its moves as they now stand.
    moving = {key for key, _, _ in fresh}
    for key, rec in week["events"].items():
        rows = groups.get(key) or []
        if rows and rec.get("form") and key not in moving and not rec.get("doubt"):
            _member(rec, max(rows, key=_size), rec["form"], labels, calendar)
    changed += _post_new(cfg, state, week, fresh, groups, labels, calendar, ring, now_ts)
    # After the new moves: a delete Telegram does not answer must not hold them.
    orphans, week["orphans"] = week.get("orphans", []), []
    for message_id, first_line in orphans:
        _discard(cfg, week, message_id, first_line)
    changed += _sync_messages(cfg, week)
    changed += _expire_pings(cfg, week, now_ts)
    save_state(cfg.state_path, state)

    note_rows = [_render(rec, max(groups[key], key=_size))
                 for key, rec in week["events"].items()
                 if rec.get("form") == ROW and groups.get(key) and not rec.get("doubt")]
    changed += _write_note(cfg, week, format_digest(note_rows, labels, window, calendar, now))
    save_state(cfg.state_path, state)
    return changed

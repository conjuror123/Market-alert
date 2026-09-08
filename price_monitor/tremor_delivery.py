"""Delivers Tremor events to Telegram: the pushes, and the running digest.

The detector decides everything about WHAT to say - severity as a return period,
which channel an event belongs to, which note it falls in (see tremor.severity
and tremor.routing). This module decides nothing. It reads those decisions,
renders them, and keeps the messages up to date.

TWO KINDS OF MESSAGE, and the difference is how loudly they arrive rather than
how long they wait. A push is its own message and goes out the hour the move is
found. A digest row goes into the note for its period - and that note is OPENED
at the start of the period rather than written at the end of it, so a row
appears the same hour and the reader is not made to wait three days for
something that has already happened and will not change. Telegram notifies on a
new message and stays silent on an edit, so the whole arrangement costs exactly
two interruptions a week: one when each note opens.

EVERY MESSAGE IS CORRECTED IN PLACE. Neither kind waits for the market to
answer, so both say what they are waiting for: a push carries the two-bar,
six-bar and settled check-ins with the moment each is due, a digest row carries
the settled one. When an answer lands the message is edited (see follow_up.py
for pushes, _write_digest here for notes), which is why a move that fully
reverted is no longer hidden - by the time that is known it is already on the
reader's phone, and unsending is not a thing Telegram can do.

NOTHING IS REMEMBERED ABOUT A NOTE EXCEPT ITS MESSAGE IDS. It is rendered whole
from the events table every run and edited only when the text actually changed,
so a late event simply appears, a recomputed-away one simply goes, and a run
that renders twice writes the same thing twice.

Like the calendar digest it piggybacks on the existing hourly trigger rather
than taking a schedule of its own (see weekly_digest.py's module docstring).

SEPARATE FROM THE SATURDAY CALENDAR DIGEST, on purpose, and not merely to keep
files apart. The two are different tenses: the calendar digest is a forecast of
what is scheduled next week, this one is a report of what actually happened.
Reading them as one message makes both harder to skim.

WHAT IS NOT SENT. A push older than STALE_AFTER_HOURS, and a note whose period
closed before this system ever saw it. This is load-bearing rather than a
nicety: the events table holds the entire history, so without it the first run
after the mute comes off would deliver five years of alerts at once. It also
does the right thing on an ordinary cold start, where there is no record of what
was sent - old news is not news, whatever the state file does or does not
remember. An EMPTY events table sends nothing at all, note included: it cannot
tell "nothing happened" from "the pipeline did not run", and only one of those
is safe to print.

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
from math import ceil

import pandas as pd

from price_monitor import economic_calendar
from price_monitor.config import Config
from price_monitor.notifier import (TelegramError, edit_telegram_message,
                                    send_telegram_message)

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
# hour, because an event can legitimately change channel after the fact: a
# once-a-year move is routed to the digest while its retention is unknown and
# becomes a push six bars later, when the answer arrives. A watermark would have
# stepped over it in between and it would never have been sent at all.
STATE_KEY = "tremor_delivery"
_SENT = "sent"

# How rare the move was, as a colour. A SQUARE, against the circles the economic
# calendar uses for a release's impact (economic_calendar.IMPACT_EMOJI): the same
# four hues carry the same "how much should I care", and the shape says which
# kind of thing the line is - something the market did, or something that was on
# the schedule - without the reader having to read the words first.
#
# Ordered like the ladder itself, so a digest sorted by tier is also sorted by
# colour, and a long note can be skimmed down its left edge.
TIER_EMOJI = {"routine": "⬜", "notable": "🟨", "major": "🟧", "extreme": "🟥"}

# The tier names are internal; these are what a person reads. Said as a return
# period, because "the biggest move in about three years" needs no calibration
# intuition where a 1-to-100 score would.
TIER_PERIOD = {
    "routine": "in two weeks",
    "notable": "in about two months",
    "major": "in about a year",
    "extreme": "in about three years",
}

# What was biggest, which is not the same claim for each channel. An abnormal
# event is the biggest move the rest of the market did NOT explain, and calling
# that "the biggest move" overstates it - the instrument may well have had
# larger hours that the market accounted for perfectly. The qualifier carries
# that rather than a different noun: "biggest move in two weeks (not explained
# by the rest of the market)" reads as one claim with a caveat, where "biggest
# unexplained move in a fortnight" made the reader parse an adjective first.
#
# "the rest of the market" is meant literally and is the only accurate phrase
# available: the residual is r minus what the basket factor and the block
# factor predicted for this instrument this hour (see tremor.residuals). The
# economic calendar plays no part in it - it enters only the SI-Index in
# tremor.cluster - so an alert saying the calendar failed to explain a move
# would be claiming a test the system never ran.
BASIS_NOUN = "move"
BASIS_QUALIFIER = {
    "abnormal": " (not explained by the rest of the market)",
    "absolute": "",
    "both": "",
}

# Said only where it adds something the headline does not. For an abnormal
# event the headline already carries it, and repeating it is noise.
BASIS_NOTE = {
    "absolute": "The rest of the market moved with it.",
    "both": "And the rest of the market did not explain it.",
}


def _headline(tier: str, basis: str) -> str:
    period = TIER_PERIOD.get(tier, tier)
    if basis == "market":
        return f"most disorderly hour {period}"
    return f"biggest {BASIS_NOUN} {period}{BASIS_QUALIFIER.get(basis, '')}"


def _retention_note(value: float) -> str:
    """How the move stood once settled, in words rather than a bare ratio.

    Used on a DIGEST line, where every horizon has long since elapsed and one
    settled sentence is the whole answer - a push carries the three-line
    follow-up instead, because for a push the answer is still arriving.

    "By the next close" rather than "a day later": the settled reading is taken
    at the close of the next trading day, which in an instrument that trades six
    and a half hours is not the same thing as twenty-four hours later.

    A ratio above one means the move CONTINUED, and rendering that as a
    percentage still standing produces sentences like "360% of it still
    standing", which reads as an error rather than as the strongest thing the
    system can say about an event.
    """
    if value > 1.15:
        return f"and it kept going - {value:.1f}x the original move by the next close"
    if value >= 0.85:
        return "still there at the next close"
    if value > 0:
        return f"{value * 100:.0f}% of it still there at the next close"
    return "fully reversed before the next close"


# How many companions to name before the line stops being readable. Six is the
# most the record ever produced in one window, so this is a guard rather than a
# limit anyone should meet.
MAX_NAMED_COMPANIONS = 6


def _also_moved(event: dict, labels: dict[str, str]) -> str:
    """The other instruments this push speaks for, named.

    "and six others moved" says something happened and nothing about what,
    and WHICH instruments moved together is the whole diagnosis - equities and
    credit is a different event from equities and the yen.
    """
    raw = event.get("also_moved")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    ids = [a for a in str(raw).split(" ") if a]
    if not ids:
        return ""
    named = [labels.get(a) or a.split(":")[-1] for a in ids]
    shown, extra = named[:MAX_NAMED_COMPANIONS], len(named) - MAX_NAMED_COMPANIONS
    if len(shown) == 1:
        listed = shown[0]
    else:
        listed = ", ".join(shown[:-1]) + " and " + shown[-1]
    if extra > 0:
        listed += f" and {extra} more"
    return f"{listed} within the day"


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
    """Every routed event, instrument and market, as plain dicts.

    Returns an empty list rather than raising when the parquet files are absent.
    They are produced by python -m tremor.saed and python -m tremor.market, and the
    hourly monitoring run must not fall over because a pipeline step has not been
    run yet.
    """
    paths = [cfg.tremor_events_path, cfg.tremor_market_events_path]
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


def _is_market(event: dict) -> bool:
    return str(event.get("basis") or "") == "market"


def _clean(value) -> "float | None":
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number   # NaN check without numpy


# Below this the comparison is not worth a line: a move only twice its
# instrument's usual hour is not what the tier language is describing, and the
# note would be making a small number look smaller.
_SCALE_FLOOR = 3.0


def _scale_note(event: dict) -> str:
    """What the move was big COMPARED WITH, in the instrument's own units.

    "+0.13%, biggest move in about three years" reads as a bug, and 45% of
    pushes carry a number under 1%. It is not a bug - SHY's usual hour is
    0.013%, so that is ten times normal - but nothing in the message said so,
    and a reader has no way to know that a tenth of a percent in short
    Treasuries is an enormous hour while the same number in SOL is nothing.

    The yardstick is sigma_LT, the instrument's own rolling standard deviation
    over the previous five thousand bars, so it is causal like everything else
    and was already being computed. Both numbers are shown rather than just the
    ratio: seeing "0.013%" beside it is what makes the claim checkable instead
    of asking the reader to trust a multiplier.
    """
    move = _clean(event.get("r"))
    usual = _clean(event.get("sigma_lt"))
    if move is None or usual is None or usual <= 0:
        return ""
    ratio = abs(move) / usual
    if ratio < _SCALE_FLOOR:
        return ""
    return (f"that is {ratio:.0f}x its usual hour of {usual * 100:.3f}%"
            if usual * 100 < 0.1 else
            f"that is {ratio:.0f}x its usual hour of {usual * 100:.2f}%")


def _settled_line(event: dict, now: datetime | None = None) -> str:
    """The one-line answer a digest row carries, or when it will have one.

    Read off the series that matches what the event claimed - abnormal for a
    move the market did not explain, raw for one that was simply large - the
    same choice persistence.held and the push follow-up both make.
    """
    raw_basis = str(event.get("basis") or "") == "absolute"
    key = "retention_raw_settled" if raw_basis else "retention_settled"
    value = _clean(event.get(key))
    if value is not None:
        return _retention_note(value)
    return f"how it held - {_due_in(event, 'settled', now)}"


def describe(event: dict, labels: dict[str, str], for_push: bool = False,
             now: datetime | None = None) -> str:
    """One line for one event, as it appears in a push or a digest row.

    `for_push` drops the single retention note, because a push carries the
    fuller follow-up block instead and would otherwise say how the move held
    twice, once vaguely and once by horizon. A digest row keeps the short form
    - one sentence, or one promise. It is written the hour the move is found,
    long before the answer exists, so the row says when the answer is due and
    the note is edited when it lands.
    """
    tier = str(event.get("tier") or "routine")
    emoji = TIER_EMOJI.get(tier, "⚪")
    when = datetime.fromtimestamp(int(event["hour_utc"]), tz=timezone.utc)

    basis = str(event.get("basis") or "")
    headline = _headline(tier, basis)
    if _is_market(event):
        return (f"{emoji} <b>Market-wide</b> - {headline}"
                f"\n     hour to {when:%Y-%m-%d %H:%M} UTC")

    asset_id = str(event.get("asset_id", ""))
    label = labels.get(asset_id) or asset_id.split(":")[-1]
    move = _clean(event.get("r"))
    parts = [f"{emoji} <b>{_escape(label)}</b> - {headline}"]

    detail = [f"hour to {when:%Y-%m-%d %H:%M} UTC"]
    if move is not None:
        detail.insert(0, f"{move * 100:+.2f}%")
    parts.append("     " + ", ".join(detail))

    scale = _scale_note(event)
    if scale:
        parts.append(f"     {scale}")

    companions = _also_moved(event, labels)
    if companions:
        parts.append(f"     with {_escape(companions)}")

    if not for_push:
        parts.append(f"     {_settled_line(event, now)}")
    return "\n".join(parts)


# How far back to look for scheduled news when a push goes out. Three hours
# because that is long enough to cover a release the instrument was still
# digesting and short enough that what it names is plausibly the cause;
# measured over every push in the record, a three-hour window holds a median of
# zero high-impact events and three at the ninetieth percentile, so the line
# stays readable.
CALENDAR_LOOKBACK_HOURS = 2
# And an hour AFTER. A release five minutes after the hour closed is a cause,
# not a coincidence, and the window used to end exactly where the move did,
# which excluded precisely the releases a reader would blame first.
#
# This costs no waiting. The archive is a SCHEDULE, not a log: it carries the
# releases announced ahead of time, currently a few hundred of them reaching
# weeks into the future. So the hour after a move is already known when the
# push is written, and the line is complete in the first message rather than
# arriving with a later edit.
CALENDAR_LOOKAHEAD_HOURS = 1

# High and Medium, the same two the Saturday calendar shows, and each carries
# its colour. Low is excluded everywhere for the same reason: it is dominated by
# bank holidays and minor prints, and naming those would turn the most important
# line of the most important message into noise.
#
# Adding Medium was measured before it was done. Over every event in the record
# that the archive covers, the three-hour window holds a median of two
# High-or-Medium releases against one High, six at the ninetieth percentile
# against four, and fourteen at the very worst against twelve - so the line
# roughly doubles from short to short, and 24% of events still have nothing
# scheduled around them at all, which is the more interesting half of the
# answer.
#
# All of them are listed rather than capped: "and two more" would hide the tail
# of a busy morning, which on a busy morning is the half worth reading.
CALENDAR_IMPACTS = economic_calendar.SHOWN_IMPACTS


def calendar_context(hour_utc: int, calendar: "list[dict] | None") -> str:
    """What was scheduled around the move - before it and just after.

    Both answers are worth printing. Naming the release tells the reader the
    move has a known cause and they can stop looking for one. Saying that
    nothing was scheduled is the more interesting half: 55% of pushes in the
    record have no high-impact event in the previous three hours, and an
    unexplained move with no news behind it is exactly what this system exists
    to find.
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
    header = (f"Economic events, {CALENDAR_LOOKBACK_HOURS}h before to "
              f"{CALENDAR_LOOKAHEAD_HOURS}h after:")
    if not named:
        return f"{header} none scheduled."

    named.sort(key=lambda e: str(e.get("date") or ""))
    lines = [header]
    for e in named:
        colour = economic_calendar.IMPACT_EMOJI.get(str(e.get("impact")), "")
        country = str(e.get("country") or "").strip()
        title = str(e.get("title") or "").strip()
        lines.append(f"     {colour} {country} {title}".rstrip())
    return "\n".join(lines)


# The check-ins a push promises, matching tremor.persistence.HORIZONS. The first
# two are bar counts; the last is the close of the next trading day, which is a
# moment rather than a distance - in an ETF that trades six and a half hours,
# twenty-four bars was nearly four days away and arrived on a Thursday for a
# Monday move.
#
# Every one is listed from the first message onward WITH WHEN IT IS DUE, so a
# line that has not landed yet reads as an appointment rather than an omission.
FOLLOW_UP_HORIZONS = (2, 6, "settled")
_HORIZON_LABEL = {2: "2h", 6: "6h", "settled": "next close"}


def _retention_word(value: float) -> str:
    """How the move stood, in the same words the digest uses."""
    if value > 1.15:
        return f"kept going, {value:.1f}x the original move"
    if value >= 0.85:
        return "still there"
    if value >= 0.5:
        return f"{value * 100:.0f}% of it still there"
    if value > 0:
        return f"mostly given back, {value * 100:.0f}% left"
    return "fully reversed"


def _template(asset_id: str) -> str:
    """Which trading calendar this instrument keeps.

    Falls back to the round-the-clock one, where a bar is an hour and there are
    no closed days, because that is the assumption that degrades gracefully: it
    can make a promise arrive early, never make one that never arrives.
    """
    return _templates().get(asset_id, "crypto_24_7")


@lru_cache(maxsize=1)
def _templates() -> dict[str, str]:
    """asset_id -> session template, from the basket definition."""
    try:
        from tremor.basket import load_basket

        return {a.asset_id: a.session_template for a in load_basket().instruments}
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("Could not read basket session templates: %s", exc)
        return {}


def due_moment(event: dict, horizon) -> "int | None":
    """The earliest epoch second at which this check-in can have an answer.

    Every horizon in this system is measured in the instrument's OWN bars, and a
    closed market has none - so the wait for an answer is a question about the
    trading calendar, not about the clock. Two bars after an ETF's last bar of
    the week is Monday morning; the settled reading is the close of the next day
    the instrument actually trades.

    None where the calendar cannot answer - an unreadable session table, or a
    date past the end of it. The caller then says less rather than saying
    something wrong.
    """
    try:
        from tremor import sessions

        template = _template(str(event.get("asset_id") or ""))
        table = sessions.cached_sessions() if template == "us_equity" else None
        hour = int(event["hour_utc"])
        if horizon == "settled":
            return sessions.next_close_after(hour, template, table)
        stamp = sessions.bars_after(hour, int(horizon), template, table)
        # A bar's answer exists once that bar has CLOSED, which is an hour after
        # the stamp it opens on.
        return None if stamp is None else stamp + sessions.HOUR
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("Could not date the %s check-in: %s", horizon, exc)
        return None


# Past this, counting hours stops being useful. "Coming in 63h" is arithmetic a
# reader has to do something with; "coming Monday at 14:00 UTC" is an
# appointment. Half a day is the crossover: everything inside it is today or
# tonight and reads naturally as a countdown.
_COUNTDOWN_LIMIT_HOURS = 12
# And past a week a weekday name is ambiguous, so the date is named instead.
_WEEKDAY_LIMIT_HOURS = 6 * 24


def _due_in(event: dict, horizon, now: datetime | None = None) -> str:
    """When an unanswered check-in is expected, in the reader's terms.

    A placeholder that says only "not yet" is indistinguishable from a bot that
    has forgotten. Saying when it is due makes the same silence an appointment.

    The wait is computed through the instrument's trading calendar and then
    rendered in ordinary clock time, because those are the two different things
    the writer and the reader each need. Counting the horizon in hours instead -
    which is what this did - told a Friday-afternoon push it was "coming within
    the hour" for the whole weekend, since the hours passed and the bars did not.
    """
    now = now or datetime.now(timezone.utc)
    due = due_moment(event, horizon)
    if due is None:
        return ("coming at the next market close" if horizon == "settled"
                else "coming when trading resumes")

    left = (due - now.timestamp()) / 3600.0
    if left <= 0:
        # The bar has closed and the answer has not appeared: the pipeline runs
        # a few minutes past the hour, and a bar the quality gate threw out
        # never produces one at all.
        return "coming with the next update"
    if left <= 1:
        return "coming within the hour"
    moment = datetime.fromtimestamp(due, tz=timezone.utc)
    if left <= _COUNTDOWN_LIMIT_HOURS:
        return f"coming in {ceil(left)}h"
    if left <= _WEEKDAY_LIMIT_HOURS:
        return f"coming {moment:%A} at {moment:%H:%M} UTC"
    return f"coming {moment:%-d %B} at {moment:%H:%M} UTC"


def follow_up_block(event: dict, horizons=FOLLOW_UP_HORIZONS,
                    now: datetime | None = None) -> str:
    """The running record of how the move held, one line per check-in.

    Every horizon is listed from the first message onward, so the reader can
    see what is still coming rather than wondering whether the bot forgot. A
    horizon whose answer has not arrived yet says so; the message is edited in
    place as each one lands (see follow_up.py).

    Reading the ABNORMAL series or the RAW one is not a detail: an event found
    because the market did not explain the move is tested on whether that
    survived, and one found because the move was simply large is tested on the
    price itself. persistence.held makes the same choice for the same reason.
    """
    raw_basis = str(event.get("basis") or "") == "absolute"
    lines = ["<i>Checking how the move held:</i>"]
    for h in horizons:
        key = f"retention_raw_{h}" if raw_basis else f"retention_{h}"
        value = _clean(event.get(key))
        label = _HORIZON_LABEL.get(h, str(h))
        if value is not None:
            lines.append(f"     {label} - {_retention_word(value)}")
        else:
            lines.append(f"     {label} - {_due_in(event, h, now)}")
    return "\n".join(lines)


def format_push(event: dict, labels: dict[str, str],
                calendar: "list[dict] | None" = None) -> str:
    """A single interrupting alert."""
    lines = [describe(event, labels, for_push=True)]
    note = BASIS_NOTE.get(str(event.get("basis") or ""))
    if note:
        lines.append("")
        lines.append(_escape(note))
    context = calendar_context(int(event["hour_utc"]), calendar)
    if context:
        lines.append("")
        lines.append(_escape(context))
    lines.append("")
    lines.append(follow_up_block(event))
    return "\n".join(lines)


# --- the running note -------------------------------------------------------
#
# The digest is not a report written at the end of a period. It is OPENED at the
# start of the period it covers and edited in place as events are found, which
# is a different product from the same events: a move that will be in Friday's
# note is worth reading on Wednesday, and there is nothing to gain by holding
# it - it happened, its size is known, and the only thing still missing is
# whether it held, which the row says it is waiting for.
#
# It costs no extra interruption. Telegram notifies on a NEW message and stays
# silent on an edit, so the reader is buzzed exactly twice a week, at the hour
# each note opens, and everything after that arrives quietly in a message they
# already have.
DIGEST_STATE = "digests"

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


def _slot_of(event: dict) -> "int | None":
    value = event.get("digest_slot")
    if value is None or value != value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def digest_rows(events: "list[dict]", slot: int, now: datetime) -> "list[dict]":
    """Every event belonging to one note. Recomputed from the table each run.

    Nothing is remembered about which rows have already been written: the note
    is rendered whole from the events table every time, so an event that
    arrives late simply appears, and one a recompute no longer produces simply
    goes. That is what makes editing safe to repeat.

    An hour that has not happened yet is not written down, the same rule a push
    is held to. It should not arise - a bar has to close before it is scored -
    but a clock skew or a bad bar must not put tomorrow in today's note.
    """
    return [e for e in events
            if str(e.get("channel") or "") == "digest" and _slot_of(e) == int(slot)
            and float(e.get("hour_utc", 0)) <= now.timestamp()]


def live_slots(events: "list[dict]", now: datetime) -> "list[int]":
    """The notes this run should look at: the open one, and any recent one whose
    rows can still change because their settled reading has not landed."""
    from tremor import routing

    current = routing.digest_slot(int(now.timestamp()))
    cutoff = now.timestamp() - DIGEST_TRACK_HOURS * 3600
    slots = {current}
    for event in events:
        if str(event.get("channel") or "") != "digest":
            continue
        slot = _slot_of(event)
        if slot is not None and cutoff <= slot <= now.timestamp():
            slots.add(slot)
    return sorted(slots)


def format_digest(events: "list[dict]", labels: dict[str, str], slot: int,
                  calendar: "list[dict] | None" = None,
                  now: datetime | None = None) -> "list[str]":
    """One note, whole, split into parts Telegram will accept.

    Ordered by severity and then by time, so the rarest move is at the top
    however late it arrived - a note read only as far as its notification
    preview should still lead with its most important line. The order is not
    fixed when a row is added: a once-in-three-years move found on Thursday
    moves to the head of a note opened on Tuesday.
    """
    from tremor import routing
    from tremor.severity import TIERS

    now = now or datetime.now(timezone.utc)
    start, end = routing.digest_window(int(slot))
    opened = datetime.fromtimestamp(start, tz=timezone.utc)
    closes = datetime.fromtimestamp(end, tz=timezone.utc)
    live = now.timestamp() < end

    rank = {name: i for i, name in enumerate(TIERS)}
    ordered = sorted(events, key=lambda e: (-rank.get(str(e.get("tier")), 0),
                                            int(e["hour_utc"])))
    if ordered:
        count = (f"{len(ordered)} event{'s' if len(ordered) != 1 else ''}"
                 + (" so far" if live else ""))
    else:
        count = "Nothing so far" if live else "Nothing in this period"
    header = (f"📋 <b>Digest</b> - {opened:%a %-d} to {closes:%a %-d %B}\n"
              + count + (" - this message is updated as moves are found" if live else ""))

    def block(event: dict) -> str:
        line = describe(event, labels, now=now)
        context = calendar_context(int(event["hour_utc"]), calendar)
        return f"{line}\n     {_escape(context)}" if context else line

    messages, current = [], header
    for text in [block(e) for e in ordered]:
        candidate = f"{current}\n\n{text}"
        if len(candidate) > _MESSAGE_LIMIT and current != header:
            messages.append(current)
            current = text
        else:
            current = candidate
    messages.append(current)

    if len(messages) > 1:
        total = len(messages)
        messages = [f"{m}\n\n<i>part {i} of {total}</i>"
                    for i, m in enumerate(messages, 1)]
    return messages


_EMPTIED_PART = "<i>(this part is no longer needed - the note above is complete)</i>"


def _write_digest(cfg: Config, store: dict, slot: int, texts: "list[str]",
                  may_open: bool) -> "tuple[int, int]":
    """Posts a note's parts, or edits the ones already posted.

    Returns (posted, edited). A note is only ever OPENED while its own period is
    the current one: a period that has closed without a note was a period the
    reader never saw, and posting "here is last Tuesday" three days late is a
    worse answer than not posting it.

    Parts can only grow - events are added, never removed - so a new part is a
    new message and everything before it is an edit. A failed post stops the
    loop rather than skipping a part, because the parts are numbered and a gap
    would be worse than a retry on the next run.
    """
    digests: dict = store.setdefault(DIGEST_STATE, {})
    record = digests.get(str(slot))
    if record is None:
        if not may_open:
            return 0, 0
        record = {"ids": [], "hashes": []}

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
        else:
            try:
                message_id = send_telegram_message(
                    cfg.telegram_bot_token, cfg.telegram_chat_id, text)
            except TelegramError as exc:
                log.error("Could not post the digest for %s: %s", slot, exc)
                break
            ids.append(int(message_id))
            hashes.append(mark)
            posted += 1

    # Remembered only once something is actually up there. A first post that
    # failed must leave no trace, or the note would count as opened and the
    # retry would never happen.
    if ids:
        digests[str(slot)] = record
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


def pending(events: "list[dict]", sent: dict, now: datetime) -> "list[dict]":
    """The pushes that are due and have not gone out.

    Only pushes. A digest row needs no record of having been written: its note
    is rendered whole from the events table every run and edited if it changed,
    so "already sent" is a question the digest side never has to ask.
    """
    pushes = [e for e in events
              if str(e.get("channel") or "") == "push"
              and str(e.get("event_id", ""))
              and str(e.get("event_id", "")) not in sent
              and _fresh(e, now)]
    pushes.sort(key=lambda e: int(e["hour_utc"]))
    return pushes


def _prune(sent: dict, now: datetime) -> dict:
    """Forgets what is too old to matter, so the state file stays small.

    Kept a little longer than an event can be delivered, so that an id is never
    dropped while its event is still deliverable and re-sent as a result.
    """
    cutoff = now.timestamp() - 4 * STALE_AFTER_HOURS * 3600
    return {k: v for k, v in sent.items() if float(v) >= cutoff}


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
    if not events:
        return 0

    from price_monitor import follow_up
    from price_monitor.alerts_log import (load_alerts_log, record_sent_alert,
                                          save_alerts_log)
    from tremor import routing

    store = state.setdefault(STATE_KEY, {})
    sent: dict = store.setdefault(_SENT, {})
    pushes = pending(events, sent, now)

    slots = live_slots(events, now)
    current = routing.digest_slot(int(now.timestamp()))
    notes = {slot: digest_rows(events, slot, now) for slot in slots}
    # A closed note with nothing in it and nothing posted has nothing to say.
    work = [slot for slot, rows in notes.items()
            if rows or slot == current or str(slot) in store.get(DIGEST_STATE, {})]

    # The archive is ninety thousand events, so it is read once for the whole
    # run and only when there is something to render with it.
    calendar = _calendar(cfg) if (pushes or work) else None

    # Corrections to already-sent pushes run on their own schedule - one sent on
    # Monday is edited on Tuesday whether or not Tuesday has news of its own.
    corrected = follow_up.apply(cfg, state, events, calendar, now)

    labels = _labels()
    pushed = posted = edited = 0

    # The record "explain alerts" reads: it looks a push up by the message id
    # printed in its footer and edits that message in place.
    alerts_log = load_alerts_log(cfg.alerts_log_path) if pushes else []

    for event in pushes:
        try:
            message_id = send_telegram_message(
                cfg.telegram_bot_token, cfg.telegram_chat_id,
                format_push(event, labels, calendar))
        except TelegramError as exc:
            log.error("Failed to send Tremor push %s: %s", event.get("event_id"), exc)
            continue
        sent[str(event["event_id"])] = int(event["hour_utc"])
        # Remembered so the two-bar, six-bar and settled check-ins can edit
        # this very message rather than sending three more.
        follow_up.track(store, event, message_id)
        label = labels.get(str(event.get("asset_id", ""))) or str(
            event.get("asset_id", "")).split(":")[-1]
        move = _clean(event.get("r"))
        record_sent_alert(
            alerts_log, chat_id=cfg.telegram_chat_id, message_id=message_id,
            symbol=label, message_text=format_push(event, labels, calendar),
            last_close=float(_clean(event.get("close")) or 0.0),
            last_return_pct=float((move or 0.0) * 100),
            ewma_z=float(_clean(event.get("z_resid")) or 0.0),
            robust_z=float(_clean(event.get("z_resid")) or 0.0),
            volume_z=0.0, signal_type=str(event.get("tier") or "push"),
            now=datetime.fromtimestamp(int(event["hour_utc"]), tz=timezone.utc))
        pushed += 1

    for slot in work:
        texts = format_digest(notes[slot], labels, slot, calendar, now)
        made, changed = _write_digest(cfg, store, slot, texts,
                                      may_open=(slot == current))
        posted += made
        edited += changed
        if made or changed:
            log.info("Digest %s: %d part(s) posted, %d edited (%d event(s))",
                     slot, made, changed, len(notes[slot]))

    if pushed:
        save_alerts_log(cfg.alerts_log_path, alerts_log)
    if pushes:
        log.info("Tremor pushes sent: %d of %d due", pushed, len(pushes))
    store[_SENT] = _prune(sent, now)
    store[DIGEST_STATE] = _prune_digests(store.get(DIGEST_STATE, {}), now)
    return pushed + posted + edited + corrected

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

WHEN A NOTE MAY BE OPENED is a rule of its own, and the strictest one here. Only
in its own hour, or the three after it - because the whole arrangement is worth
having precisely because those two interruptions land at noon on a Tuesday and a
Friday, and a note opened whenever the system happened to next run is an
ordinary unscheduled buzz wearing a schedule's clothes. A period that misses
that window is not lost: the next note covers from where the last note that
actually went out left off, so the buzz is always at noon AND no move is
silently dropped for want of a scheduler.

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
# period, because "about once every three years" needs no calibration intuition
# where a 1-to-100 score would.
#
# AND SAID AS A FREQUENCY, not as a record. "Biggest move in about three years"
# claims the last three years held nothing larger, and the ladder claims no such
# thing - it says a move this size is expected about once in three years, on
# average, which in a fat-tailed market means several can arrive in a month. The
# old wording flatly contradicted the line beneath it: "biggest move in about
# three years" over "the last one this big was 23 days ago". Only one of the two
# was wrong, and it was the headline.
TIER_PERIOD = {
    "routine": "about once a fortnight",
    "notable": "about once every two months",
    "major": "about once a year",
    "extreme": "about once every three years",
}

# WHICH LADDER the tier was measured against, said in the noun rather than in a
# parenthesis. Two ladders exist and they answer different questions: the
# absolute one ranks the raw return, the abnormal one ranks what is left after
# the market is taken out (see tremor.residuals). "Biggest move in about a year"
# would be false for the second - the instrument may well have had larger hours
# the market accounted for perfectly - so the second says "biggest move OF ITS
# OWN in about a year", which needs no glossary.
#
# It replaces "(not explained by the rest of the market)", and then "(more than
# the market explains)", both of which asked the reader to hold an idea nobody
# had defined for them. The idea is now shown instead of named, one line down,
# in the units they are already reading: see _market_share_note.
BASIS_NOUN = {
    "abnormal": "a move of its own this big happens",
    "absolute": "a move this big happens",
    "both": "a move this big happens",
}


def _headline(tier: str, basis: str) -> str:
    period = TIER_PERIOD.get(tier, tier)
    if basis == "market":
        return f"an hour this disorderly happens {period}"
    return f"{BASIS_NOUN.get(basis, 'a move this big happens')} {period}"


# What the basket actually is, said once inside the alert rather than left as a
# word. "The whole watchlist" answers nothing: the reader wants to know which
# instruments were drifting together, and there are only five kinds of them.
BASKET_FOOTER = ("<i>The basket is 24 instruments: US equities, Treasuries and "
                 "credit, commodities, six major currency pairs, crypto.</i>")

# What each block is called in a sentence. The internal names are lower case and
# two of them are abbreviations.
BLOCK_LABEL = {
    "equity": "US equities",
    "rates": "bonds and credit",
    "commodities": "commodities",
    "FX": "currencies",
    "crypto": "crypto",
}


def _split_lines(event: dict, label: str) -> "list[str]":
    """The move broken into the three things it can be, adding back to the move.

    THE WORD "MARKET" IS DELIBERATELY ABSENT. Every attempt to name this idea
    failed on the same objection, and the objection was right: for the S&P 500,
    "the market" IS the S&P 500, so "following the market would have given
    +6.01%" invites "which market, and how would I have followed it?".

    So the parts are named by what they actually are. Each instrument is
    regressed on two things it moves with - the weighted median return of the
    basket, and the median of its own block with itself left out - over the five
    hundred bars before this one, stopping three bars short so the move being
    tested cannot adjust its own coefficients. The two fitted parts and the
    leftover are all carried on the event, and they sum to the return exactly.

    THREE PARTS RATHER THAN TWO, because two was sometimes wrong. On 2008-11-20
    the financial sector's basket beta was NEGATIVE and its +10.50% came almost
    entirely from the equity block - calling that "the whole watchlist drifting"
    would have named the wrong cause. Splitting the block out says "its own
    sector did this", which is the diagnosis a reader can act on.
    """
    move = _clean(event.get("r"))
    own = _clean(event.get("e_resid"))
    if move is None or own is None:
        return []

    basket = _clean(event.get("co_basket"))
    block = _clean(event.get("co_block"))
    if basket is None or block is None:
        # Before the two components were carried, only the total was. Falling
        # back to it keeps an older row renderable rather than silent.
        basket, block = move - own, 0.0

    named = BLOCK_LABEL.get(str(event.get("block")), str(event.get("block") or "its block"))
    lines = ["of that move:",
             f"     {basket * 100:+.2f}%  the whole basket drifting together"]
    if block:
        lines.append(f"     {block * 100:+.2f}%  its own block, {_escape(named)}")
    lines.append(f"     {own * 100:+.2f}%  {_escape(label)} on its own")
    return lines


def _since_note(event: dict, events: "list[dict]") -> str:
    """When this instrument was last this rare, as a date.

    "Biggest move in about a year" is a return period fitted to a tail, which is
    the honest way to say how unusual something is and a hard thing to picture.
    The date is the same claim in a form nobody needs statistics for: it is the
    last time this instrument produced an event at this tier or a rarer one.

    Read off the events table, which the delivery layer already holds, so this
    costs nothing. Silent where there is no earlier one - a young instrument, or
    genuinely the first in twenty-two years, and claiming either would be a
    guess.
    """
    from tremor.severity import TIERS

    rank = {name: i for i, name in enumerate(TIERS)}
    here = rank.get(str(event.get("tier")), -1)
    asset_id = str(event.get("asset_id") or "")
    hour = int(event["hour_utc"])
    earlier = [int(e["hour_utc"]) for e in events
               if str(e.get("asset_id") or "") == asset_id
               and int(e["hour_utc"]) < hour
               and rank.get(str(e.get("tier")), -1) >= here]
    if not earlier:
        return ""
    when = datetime.fromtimestamp(max(earlier), tz=timezone.utc)
    days = (hour - max(earlier)) / 86400
    ago = (f"{days / 365.25:.1f} years" if days >= 365 else
           f"{days / 30.44:.0f} months" if days >= 60 else
           f"{days:.0f} days")
    return f"the last one this big was {when:%-d %B %Y}, {ago} ago"


def _retention_note(value: float) -> str:
    """How the move stood once settled, in words rather than a bare ratio.

    Used on a DIGEST line, where every horizon has long since elapsed and one
    settled sentence is the whole answer - a push carries the three-line
    follow-up instead, because for a push the answer is still arriving.

    "By the next day's close" rather than "a day later": the settled reading is
    taken at the close of the next day the instrument TRADES, which in something
    that trades six and a half hours is not the same thing as twenty-four hours
    later. And "the next day's" rather than "the next", because a move at eleven
    in the morning has a close of its own a few hours later and that is not the
    one being measured.

    A ratio above one means the move CONTINUED, and rendering that as a
    percentage still standing produces sentences like "360% of it still
    standing", which reads as an error rather than as the strongest thing the
    system can say about an event.
    """
    if value > 1.15:
        return (f"and it kept going - {value:.1f}x the original move "
                f"by the next day's close")
    if value >= 0.85:
        return "still there at the next day's close"
    if value > 0:
        return f"{value * 100:.0f}% of it still there at the next day's close"
    return "fully reversed before the next day's close"




def companions_of(event: dict, events: "list[dict]") -> "dict[str, dict]":
    """The events folded into this push, by asset_id.

    A push speaks for a whole episode: a second instrument moving inside the
    collapse window does not buzz again, it is folded into the first (see
    tremor.routing.collapse). Those folded events take no line of their own
    anywhere else, so this is where their numbers have to come from.
    """
    from tremor.routing import same_day

    anchor = str(event.get("asset_id") or "")
    if not anchor:
        return {}
    hour = int(event["hour_utc"])
    return {str(e.get("asset_id") or ""): e for e in events
            if str(e.get("folded_into") or "") == anchor
            and int(e["hour_utc"]) >= hour and same_day(int(e["hour_utc"]), hour)}


# How long the whole message may run before Telegram rejects it. A push that had
# to be split into two messages would buzz twice, which is the one thing the
# collapse exists to prevent - so companions are dropped from the end until it
# fits and the message says how many it dropped.
_PUSH_LIMIT = 3600


def _companion_blocks(event: dict, labels: dict[str, str],
                      companions: "dict[str, dict] | None",
                      now: datetime | None,
                      events: "list[dict] | None",
                      budget: int) -> str:
    """The rest of the day's episode, each instrument written out in full.

    Not a list of names, and no longer a list of names with a number beside
    them. A push speaks for everything that moved with it until midnight, and
    each of those has its own size, its own rarity, its own split and its own
    two check-ins - the financial sector kept going to 1.9x on 2008-11-20 while
    short Treasuries gave two thirds back, and a shared line could say neither.

    Ordered rarest first and then biggest, because a folded companion moved MORE
    than the push that spoke for it 49% of the time: the most important number
    in the message is often down here rather than in the headline.
    """
    raw = event.get("also_moved")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    ids = [a for a in str(raw).split(" ") if a]
    if not ids:
        return ""

    companions = companions or {}
    rank = {"routine": 0, "notable": 1, "major": 2, "extreme": 3}

    def sort_key(asset_id: str):
        row = companions.get(asset_id) or {}
        return (-rank.get(str(row.get("tier")), -1),
                -abs(_clean(row.get("r")) or 0.0))

    blocks, used, dropped = [], 0, 0
    for asset_id in sorted(ids, key=sort_key):
        row = companions.get(asset_id)
        if row is None:
            # Named but not yet in the table: the ordinary case at the hour a
            # push is sent, when the day it collects has not happened yet.
            dropped += 1
            continue
        block = describe(row, labels, now, events)
        if used + len(block) > budget:
            dropped += 1
            continue
        blocks.append(block)
        used += len(block)

    if not blocks:
        named = [labels.get(a) or a.split(":")[-1] for a in ids]
        return "Also moved, within the day: " + _escape(", ".join(named))

    head = ["<b>Also moved, within the day</b>"]
    if dropped:
        head.append(f"<i>and {dropped} more not shown</i>")
    return "\n\n".join(head + blocks)


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
    # Shown at every size now, where it used to be suppressed below three times
    # normal. That silence was itself confusing: 21% of events fall under the old
    # floor, and a reader who has seen the line on one alert reads its absence on
    # the next as a gap rather than as "this one was only 2.7x". A decimal below
    # ten, because "3x" for 2.7 looks like a rounding that flatters the alert.
    size = f"{ratio:.0f}x" if ratio >= 10 else f"{ratio:.1f}x"
    usual_pct = (f"{usual * 100:.3f}%" if usual * 100 < 0.1
                 else f"{usual * 100:.2f}%")
    return f"that is {size} its usual hour, which is {usual_pct}"


def check_in_lines(event: dict, now: datetime | None = None,
                   horizons=None) -> "list[str]":
    """How the move held, one line per check-in, on every block that has a move.

    Both horizons are listed from the first message onward, so the reader can
    see what is still coming rather than wondering whether the bot forgot. One
    whose answer has not arrived yet says when it is due; the message is edited
    in place as each lands (see follow_up.py).

    Carried by EVERY instrument in a message now, not only the one in the
    headline. A push speaks for a whole day's episode and each instrument in it
    held or gave back its move on its own terms - the financial sector kept
    going to 1.9x while short Treasuries gave two thirds back, and one shared
    verdict at the bottom of the message could say neither.

    Reading the ABNORMAL series or the RAW one is not a detail: an event found
    because the market did not account for the move is tested on whether THAT
    survived, and one found because the move was simply large is tested on the
    price itself. persistence.held makes the same choice for the same reason.
    """
    raw_basis = str(event.get("basis") or "") == "absolute"
    lines = []
    for h in horizons or FOLLOW_UP_HORIZONS:
        key = f"retention_raw_{h}" if raw_basis else f"retention_{h}"
        value = _clean(event.get(key))
        label = _HORIZON_LABEL.get(h, str(h))
        if h == "today" and _closed_the_day(event):
            # 6% of moves are made in the last hour their instrument trades that
            # day. There is nothing left of the day to hold through, so the
            # ratio is one by construction and saying "still there" would be
            # reporting arithmetic as news.
            answer = "the move was in the closing hour"
        elif value is not None:
            answer = _retention_word(value)
        else:
            answer = _due_in(event, h, now)
        lines.append(f"     {label} - {answer}")
    return lines


def _closed_the_day(event: dict) -> bool:
    """Whether the move was made in the last hour its instrument traded that day."""
    due = due_moment(event, "today")
    return due is not None and due == int(event["hour_utc"]) + 3600


def describe(event: dict, labels: dict[str, str],
             now: datetime | None = None,
             events: "list[dict] | None" = None) -> str:
    """One instrument's whole story, as it appears in a push or a digest row.

    THE SAME BLOCK EVERYWHERE. A pushed move, an instrument folded into that
    push, and a row in the running note are the same kind of thing seen at
    different volumes, and they all deserve the same account: what moved, how
    far, how that compares with its ordinary hour, when it was last this rare,
    what the move was made of, and how it held at each of the two closes.

    Written the hour the move is found, long before the answers exist, so the
    check-in lines say when each is due and the message is edited when they
    land.
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

    detail = f"{move * 100:+.2f}% " if move is not None else ""
    parts.append(f"     {detail}in the hour to {when:%Y-%m-%d %H:%M} UTC")

    for line in (_scale_note(event), _since_note(event, events or [])):
        if line:
            parts.append(f"     {line}")

    for line in _split_lines(event, label):
        parts.append(f"     {line}")

    parts.extend(check_in_lines(event, now))
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

# High and Medium, the same two the weekly calendar shows, and each carries
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
FOLLOW_UP_HORIZONS = ("today", "settled")
_HORIZON_LABEL = {"today": "this day's close", "settled": "next day's close"}


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
        return sessions.today_close_after(hour, template, table)
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
        return ("coming at the next day's close" if horizon == "settled"
                else "coming at this day's close")

    left = (due - now.timestamp()) / 3600.0
    if left <= 0:
        # The bar has closed and the answer has not appeared: the pipeline runs
        # a few minutes past the hour, and a bar the quality gate threw out
        # never produces one at all.
        return "coming with the next update"

    # Named as a CLOSE, never as a countdown or a bare timestamp. Both horizons
    # are day closes, and for anything whose day is the UTC one that close falls
    # at midnight - so "coming Thursday at 00:00 UTC" was the end of Wednesday
    # wearing Thursday's name, and read as a day later than it is. Saying whose
    # close it is removes the ambiguity, and it does not tick, so a message is
    # not edited every hour to count it down.
    moment = datetime.fromtimestamp(due, tz=timezone.utc)
    ended = datetime.fromtimestamp(due - 1, tz=timezone.utc)
    day = f"{ended:%A}" if left <= _WEEKDAY_LIMIT_HOURS else f"{ended:%-d %B}"
    return f"coming at {day}'s close ({moment:%H:%M} UTC)"


def format_push(event: dict, labels: dict[str, str],
                calendar: "list[dict] | None" = None,
                companions: "dict[str, dict] | None" = None,
                events: "list[dict] | None" = None,
                now: datetime | None = None) -> str:
    """A single interrupting alert, speaking for its whole day's episode.

    Ordered so the reader meets one instrument first and the day second: the
    move written out in full, then the news scheduled around it, then every
    other instrument that moved before midnight, each written out the same way.
    """
    lines = [describe(event, labels, now, events)]
    context = calendar_context(int(event["hour_utc"]), calendar)
    if context:
        lines.append("")
        lines.append(_escape(context))
    budget = _PUSH_LIMIT - len("\n\n".join(lines))
    blocks = _companion_blocks(event, labels, companions, now, events, budget)
    if blocks:
        lines.append("")
        lines.append(blocks)
    # Once at the foot of the message rather than under every instrument: the
    # reader needs to know what "the basket" is, and needs it said once.
    if any("the whole basket" in line for line in lines):
        lines.append("")
        lines.append(BASKET_FOOTER)
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
#
# WHICH IS WHY THE OPENING HOUR IS THE ONE THING THAT CANNOT SLIP. The whole
# arrangement is worth having because the two interruptions land at noon on a
# Tuesday and a Friday; a note that opened whenever the system happened to next
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


def digest_rows(events: "list[dict]", window: "tuple[int, int]",
                now: datetime) -> "list[dict]":
    """Every event one note speaks for. Recomputed from the table each run.

    Nothing is remembered about which rows have already been written: the note
    is rendered whole from the events table every time, so an event that
    arrives late simply appears, and one a recompute no longer produces simply
    goes. That is what makes editing safe to repeat.

    An event folded into a push is NOT here. It does not buzz again, but the
    push already speaks for it and now carries its size, and a row of its own in
    the note - arriving under that push, within the hour - is one episode
    reaching the reader twice. Only push tiers are ever folded, so this is
    exactly the rule "a move that belongs to an alert belongs to that alert".

    An hour that has not happened yet is not written down, the same rule a push
    is held to. It should not arise - a bar has to close before it is scored -
    but a clock skew or a bad bar must not put tomorrow in today's note.
    """
    start, end = window
    return [e for e in events
            if str(e.get("channel") or "") == "digest"
            and not str(e.get("folded_into") or "")
            and start <= float(e.get("hour_utc", 0)) < end
            and float(e.get("hour_utc", 0)) <= now.timestamp()]


def format_digest(events: "list[dict]", labels: dict[str, str],
                  window: "tuple[int, int]",
                  calendar: "list[dict] | None" = None,
                  now: datetime | None = None,
                  all_events: "list[dict] | None" = None) -> "list[str]":
    """One note, whole, split into parts Telegram will accept.

    Ordered by severity and then by time, so the rarest move is at the top
    however late it arrived - a note read only as far as its notification
    preview should still lead with its most important line. The order is not
    fixed when a row is added: a once-in-three-years move found on Thursday
    moves to the head of a note opened on Tuesday.

    The header states the period the note speaks for rather than the day it was
    posted, because those come apart exactly when it matters: a note that had
    to pick up a period whose own note never opened covers six days, and saying
    so is the difference between a complete record and a puzzling one.
    """
    from tremor.severity import TIERS

    now = now or datetime.now(timezone.utc)
    start, end = int(window[0]), int(window[1])
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
        line = describe(event, labels, now, all_events)
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

    if any("the whole basket" in m for m in messages):
        messages[-1] += "\n\n" + BASKET_FOOTER

    if len(messages) > 1:
        total = len(messages)
        messages = [f"{m}\n\n<i>part {i} of {total}</i>"
                    for i, m in enumerate(messages, 1)]
    return messages


_EMPTIED_PART = "<i>(this part is no longer needed - the note above is complete)</i>"


def _write_digest(cfg: Config, slot: int, record: dict,
                  texts: "list[str]") -> "tuple[int, int]":
    """Posts a note's parts, or edits the ones already posted.

    Returns (posted, edited). Whether the note may exist at all was decided
    before this was called; here it either has messages behind it or is having
    its first ones sent.

    Parts can only grow - events are added, never removed - so a new part is a
    new message and everything before it is an edit. A failed post stops the
    loop rather than skipping a part, because the parts are numbered and a gap
    would be worse than a retry on the next run.
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

    # Open this period's note if its hour has come and it is not open already.
    # Nothing else ever creates one: a period whose hour passed unopened is
    # picked up by the next note instead (see carried_from).
    digests: dict = store.setdefault(DIGEST_STATE, {})
    current = routing.digest_slot(int(now.timestamp()))
    if str(current) not in digests and due_to_open(current, now):
        digests[str(current)] = {"ids": [], "hashes": [],
                                 "from": carried_from(digests, current),
                                 "to": routing.next_digest_slot(current)}

    notes = {int(key): (record, digest_rows(events, note_window(int(key), record), now))
             for key, record in digests.items()}

    # The archive is ninety thousand events, so it is read once for the whole
    # run and only when there is something to render with it.
    calendar = _calendar(cfg) if (pushes or notes) else None

    # Corrections to already-sent pushes run on their own schedule - one sent on
    # Monday is edited on Tuesday whether or not Tuesday has news of its own.
    corrected = follow_up.apply(cfg, state, events, calendar, now)

    labels = _labels()
    pushed = posted = edited = 0

    # The record "explain alerts" reads: it looks a push up by the message id
    # printed in its footer and edits that message in place.
    alerts_log = load_alerts_log(cfg.alerts_log_path) if pushes else []

    for event in pushes:
        # What this push speaks for. Usually nothing: only a fifth of pushes
        # have a companion, and at the hour one is sent the window it collapses
        # is still open, so the block fills in through the follow-up edits.
        with_it = companions_of(event, events)
        try:
            message_id = send_telegram_message(
                cfg.telegram_bot_token, cfg.telegram_chat_id,
                format_push(event, labels, calendar, with_it, events, now))
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
            symbol=label,
            message_text=format_push(event, labels, calendar, with_it, events, now),
            last_close=float(_clean(event.get("close")) or 0.0),
            last_return_pct=float((move or 0.0) * 100),
            ewma_z=float(_clean(event.get("z_resid")) or 0.0),
            robust_z=float(_clean(event.get("z_resid")) or 0.0),
            volume_z=0.0, signal_type=str(event.get("tier") or "push"),
            now=datetime.fromtimestamp(int(event["hour_utc"]), tz=timezone.utc))
        pushed += 1

    for slot in sorted(notes):
        record, rows = notes[slot]
        texts = format_digest(rows, labels, note_window(slot, record), calendar,
                              now, events)
        made, changed = _write_digest(cfg, slot, record, texts)
        posted += made
        edited += changed
        if made or changed:
            log.info("Digest %s: %d part(s) posted, %d edited (%d event(s))",
                     slot, made, changed, len(rows))

    if pushed:
        save_alerts_log(cfg.alerts_log_path, alerts_log)
    if pushes:
        log.info("Tremor pushes sent: %d of %d due", pushed, len(pushes))
    store[_SENT] = _prune(sent, now)
    store[DIGEST_STATE] = _prune_digests(digests, now)
    return pushed + posted + edited + corrected

"""Posts a Saturday digest of the coming week's Medium/High-impact economic
calendar events to Telegram.

Piggybacks on the existing hourly trigger (see .github/workflows/price-monitor.yml
and README - external cron-job.org calls workflow_dispatch roughly once an hour)
instead of provisioning a second schedule: __main__.py calls
maybe_send_weekly_digest on every run, and it's a no-op except during the one
hourly run that lands on the Saturday note's opening. "Already sent" is tracked
in state.json (already loaded/saved every run) so a second run inside the grace
window - or the external trigger firing a little early or late - never posts the
digest twice.

IMMEDIATELY BEFORE THE WEEKEND PRICE NOTE, and that ordering is the reason for
the day. __main__ calls this first and tremor_delivery second, so in the one run
that lands on the Saturday slot both go out in that order and the running price
note is the last message in the chat - which is where it should be, because it
is the one that keeps changing for the next two days.

THE DAY IS NOT WRITTEN DOWN HERE AS A WEEKDAY. It is read off tremor.routing,
which owns the note boundaries, and this module only asks whether the note due
to open right now is the WEEKEND one. Two constants both spelling "Saturday" in
two files is exactly the pair that survives one of them being changed, and the
entire point of the day is that these two messages arrive in the same run.

The weekend note and not the Monday one, because a forecast wants to arrive
before the week it forecasts, with a weekend to read it in. A calendar of the
coming week delivered one minute past the start of that week is a schedule
handed out after the meeting began.

It used to go out on Friday, and before that on Saturday-or-Sunday by way of a
test on the feed. That whole apparatus is gone. The old digest was built from the
LIVE WEEKLY FEED, which serves "this week" without saying where its week starts,
so the send day had to be one the feed could be expected to have rolled over on -
and even then it had to be checked and deferred when it had not.

It is built from THE ARCHIVE instead, over a window this module states outright:
the next whole calendar week, Monday 00:00 UTC to the following Monday 00:00
UTC. The archive reaches weeks into the future because ForexFactory's monthly
pages are read into it (see refresh_months), so the coming week is simply looked
up rather than hoped for, and the window no longer depends on a boundary nobody
can see. Consecutive windows ABUT EXACTLY: nothing is listed twice and no hour
of the calendar falls between two digests.

Low-impact events and holidays are both excluded (see _DIGEST_IMPACTS) - only
Medium/High. No LLM involved on purpose (see README, "Daily signal" and the
weekly digest section): just a plain, programmatically formatted list grouped
by day - the source data already carries the impact tag and the numbers, so
there's nothing here for an LLM to add.

Every value the source gave is printed under each event: actual, forecast,
previous. The actual takes separate work - the live weekly feed does not serve it
at all, see refresh_months.

Also runnable directly as a one-off, bypassing the day and dedup checks - see
main() and .github/workflows/weekly-digest-test.yml - for manually checking
what the digest actually looks like without waiting for Saturday:
    python -m price_monitor.weekly_digest --force
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, time, timedelta, timezone

import requests

from price_monitor import economic_calendar
from price_monitor.config import Config, load_config
from price_monitor.notifier import TelegramError, send_telegram_message
from tremor import routing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.weekly_digest")

_DIGEST_IMPACTS = set(economic_calendar.SHOWN_IMPACTS)

# datetime.weekday(): Monday=0 ... Saturday=5. Which of routing.DIGEST_WEEKDAYS
# this digest rides on - the weekend note, not the Monday one. The MOMENT it goes
# out is not stated here at all: it is whatever routing says the weekend note
# opens at, so the two cannot drift apart no matter which is edited.
_DIGEST_WEEKDAY = 5

# And the same hours of grace the price note has (tremor_delivery
# .DIGEST_OPEN_WITHIN_HOURS), for the same reason: the trigger is an external
# service, one failed run must not cost the week's calendar, and both messages
# must keep landing in the same run so their order never inverts.
_DIGEST_WITHIN_HOURS = 4

# What "the coming week" means, stated rather than inferred from a feed: THE
# CALENDAR WEEK AFTER THIS ONE, Monday 00:00 UTC to the following Monday 00:00
# UTC. Sent at 00:05 on Saturday the 1st, the digest lists the 3rd through the
# 9th - a whole week, starting on the day a week starts.
#
# Seven days exactly and aligned to the week, rather than seven days from the
# moment of sending, and the alignment is what makes the guarantee possible:
# CONSECUTIVE DIGESTS ABUT. The one sent on Saturday the 25th covered the 27th
# to the 3rd, which is why this one may begin on the 3rd - the weekend it is
# itself sent in was listed a week ago. Nothing is listed twice and no hour of
# the calendar falls between two digests.
#
# That is worth spelling out because starting on the Monday LOOKS like it drops
# the sending weekend, and the sending weekend is not empty: 1,158 Medium/High
# releases in the archive fall on a Saturday or Sunday by UTC clock, nearly all
# Asia-Pacific prints at 21:45 or 23:50 on the Sunday - Monday morning in Tokyo.
# They are not dropped. They were announced in the previous digest, seven days
# earlier, which is a question about how fresh the warning is rather than about
# whether it was given. A window running from the moment of sending would buy
# that freshness and pay for it by overlapping its predecessor by a week, so
# that most of every message was a week-old repeat.
_COMING_WEEK_DAYS = 7

# datetime.weekday(): Monday=0. Where a week is taken to start, which is the
# only reason this file knows about weekdays twice - the SEND day comes from
# routing (see _DIGEST_WEEKDAY) and is free to move without touching this.
_WEEK_STARTS_ON = 0

# How far short of the window's end the archive may stop and still be trusted to
# say "nothing is scheduled". Three days, so the test lands on the Friday that
# closes the working week rather than in the weekend behind it: a Saturday with
# only a handful of Medium or High releases is the normal case and says nothing
# about whether the archive ran out.
_COVERAGE_SLACK_DAYS = 3

# The note-opening it was last sent for, as the exact UTC second routing gives.
# The slot rather than a date or a week number, because the slot is what the
# grace window has to de-duplicate: two runs inside the same four hours must not
# both send, and unlike a calendar day it needs no timezone to be unambiguous.
_STATE_KEY = "weekly_digest:last_sent_week"

# And the day the archive was last topped up from the live feed. The digest's
# own refresh happens once a week, which is often enough for a message about
# next week and far too seldom for the OTHER use of this archive: every push
# names the releases in the three hours around the move, all week long, and a
# schedule fetched last Saturday does not have the speech that was added on
# Wednesday. One request a day fixes that.
_REFRESH_KEY = "weekly_digest:last_refreshed"

# Shared with the push and digest messages so the two never drift apart; see
# economic_calendar.IMPACT_EMOJI for why the colour lives there.
_IMPACT_EMOJI = economic_calendar.IMPACT_EMOJI

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday")

# Telegram rejects a message longer than 4096 characters outright rather than
# truncating it, so the digest is cut into parts on day boundaries. The 96
# characters of headroom cover the "part N of M" line, which would otherwise have
# to be counted recursively.
_MESSAGE_LIMIT = 4000


def _weekend_slot(now: datetime) -> "int | None":
    """The weekend note-opening this moment belongs to, or None on a weekday.

    routing.digest_slot answers "which note is open right now", which is either
    the Monday one or the Saturday one. Only the Saturday one takes a calendar,
    so a Monday slot is simply not this digest's business.
    """
    slot = routing.digest_slot(int(now.timestamp()))
    opens = datetime.fromtimestamp(slot, tz=timezone.utc).astimezone(routing.DIGEST_TZ)
    return slot if opens.weekday() == _DIGEST_WEEKDAY else None


def _is_digest_window(now: datetime) -> bool:
    """Whether a digest may go out at this moment: as the weekend note opens, or
    within the few hours after it if those runs were missed."""
    slot = _weekend_slot(now)
    if slot is None:
        return False
    return 0 <= now.timestamp() - slot < _DIGEST_WITHIN_HOURS * 3600


def _week_identifier(now: datetime) -> str:
    """Dedup key: the note-opening this digest belongs to.

    The slot it is sent for rather than anything read out of the data, because
    the grace window means several runs can qualify and only the first may send.
    """
    return str(_weekend_slot(now) or "")


def coming_week(now: datetime) -> "tuple[datetime, datetime]":
    """The period this digest speaks for: the next whole week, Monday to Monday.

    The Monday STRICTLY AFTER `now`, so the answer does not depend on the hour
    the run happens to land on. Sent at 00:05 on Saturday the 1st or at 03:00
    after two missed runs, the digest covers the 3rd to the 10th either way -
    which is what makes the grace window free and what makes two digests abut
    instead of overlapping by however long the trigger was late.
    """
    moment = now.astimezone(timezone.utc)
    ahead = (_WEEK_STARTS_ON - moment.weekday()) % 7 or 7
    start = datetime.combine(moment.date() + timedelta(days=ahead), time(0),
                             tzinfo=timezone.utc)
    return start, start + timedelta(days=_COMING_WEEK_DAYS)


def _escape(text: str) -> str:
    """The message goes out with parse_mode=HTML, and event names come from an
    external feed. One unescaped "M&A" or "S&P" is enough for Telegram to reject
    the whole message - and the weekly digest then never arrives at all.
    """
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def format_event_values(event: dict) -> str:
    """An event's value line: actual, forecast, previous.

    Empty fields are skipped rather than printed as dashes. The feed's coverage
    varies - a forecast exists for roughly 70% of events, a previous value for
    80% - and a line of three dashes would say nothing except that the source
    stayed silent.

    The actual is essentially always empty in a week-ahead digest: the events have
    not happened yet. It is printed when present because the same format is used
    for a manual run over a past week.
    """
    parts = []
    for label, key in (("actual", "actual"), ("forecast", "forecast"),
                       ("prev.", "previous")):
        value = str(event.get(key) or "").strip()
        if value:
            parts.append(f"{label} {_escape(value)}")
    return " · ".join(parts)


def _event_lines(event: dict) -> list[str]:
    event_time = economic_calendar.parse_event_time(event["date"])
    lines = [f"{_IMPACT_EMOJI[event['impact']]} <b>{event_time.strftime('%H:%M')}</b> "
             f"{_escape(economic_calendar.country_label(event['country']))} "
             f"— {_escape(event['title'])}"]
    values = format_event_values(event)
    if values:
        lines.append(f"    <i>{values}</i>")
    return lines


def format_digest(events: list[dict], start: datetime | None = None,
                  end: datetime | None = None) -> list[str]:
    """The week's digest, split by day and, when needed, across several messages.
    Returns a list - the sender posts them in order.

    Every event's time is UTC and that is stated once in the header rather than on
    every line: with two or three dozen events, repeating "UTC" on each line takes
    more space than it carries meaning.

    The header states the WINDOW ASKED FOR rather than the span of the events
    that happen to be in it. Those differ exactly when the week is quiet at one
    end, and a header taken from the events would then quietly narrow the claim
    the message is making.
    """
    header = "📅 <b>Economic calendar for the week</b>"
    if not events:
        return [f"{header}\n\nNo Medium/High impact events found for this week."]

    ordered = sorted(events, key=lambda e: e["date"])
    high = sum(1 for e in ordered if e["impact"] == "High")
    first = start or economic_calendar.parse_event_time(ordered[0]["date"])
    # A second before the end, because the window closes at midnight and
    # midnight belongs to the day that just finished.
    last = (end - timedelta(seconds=1)) if end \
        else economic_calendar.parse_event_time(ordered[-1]["date"])
    intro = (f"{header}\n"
             f"<i>{first.strftime('%d.%m')} — {last.strftime('%d.%m')}, "
             f"times UTC · {len(ordered)} events, of them 🔴 {high}</i>")

    # Days are assembled whole and only then laid out across messages: a break
    # inside a day would leave its header in one message and its events in
    # another.
    days: list[list[str]] = []
    current_day = None
    for event in ordered:
        moment = economic_calendar.parse_event_time(event["date"])
        if moment.date() != current_day:
            current_day = moment.date()
            days.append([f"\n<b>{_WEEKDAYS[moment.weekday()]} "
                         f"{moment.strftime('%d.%m')}</b>"])
        days[-1].extend(_event_lines(event))

    messages: list[str] = []
    block = [intro]
    for day in days:
        candidate = block + day
        if len("\n".join(candidate)) > _MESSAGE_LIMIT and len(block) > 1:
            messages.append("\n".join(block))
            block = [f"{header} <i>(continued)</i>"] + day
        else:
            block = candidate
    messages.append("\n".join(block))
    return messages


def refresh_months(path: str, session: requests.Session | None = None,
                   now: datetime | None = None,
                   through: datetime | None = None) -> int:
    """Reads ForexFactory's monthly pages into the archive, backwards and forwards.

    Backwards, this fills in released values (`actual`). Forwards, it is what
    puts the coming week in the archive at all - and the digest is built from
    the archive, so this is not a nicety attached to the digest, it is the
    digest's source of data.

    Without this the archive would grow forward with a permanently empty actual.
    The live weekly feed is the only source that arrives here regularly, and it
    has NO actual FIELD AT ALL: verified against the output, the feed's keys are
    country, date, forecast, impact, previous, title. An event enters the archive
    a week before publication, with a forecast and a previous value, and the
    released figure would never appear, because the feed never returns to that
    event.

    ForexFactory's monthly pages do serve the actual - 85% of events for August
    2026, 77% for March 2021 - so once a week two months are read back: the
    current one and the previous one. The previous month is needed for events at
    the end of it whose actual is released in the new month, and because the
    source sometimes revises a figure after the fact. Two requests a week is the
    price this gap is worth.

    Returns the number of records added or updated in the archive.
    """
    now = now or datetime.now(timezone.utc)
    months = {(now.year, now.month)}
    previous = (now.replace(day=1) - timedelta(days=1))
    months.add((previous.year, previous.month))
    # And the month the coming week runs into, which is a different one whenever
    # the digest goes out in the last days of a month.
    if through is not None:
        months.add((through.year, through.month))

    fetched: list[dict] = []
    for year, month in sorted(months):
        try:
            fetched.extend(economic_calendar.fetch_forexfactory_month(
                year, month, session=session))
        except economic_calendar.CalendarError as exc:
            # The backfill is not what the digest is run for. A page may fail to
            # open, and that is no reason to withhold the message.
            log.warning("Could not read back %04d-%02d: %s", year, month, exc)

    if not fetched:
        return 0
    updated = economic_calendar.merge_events(path, fetched)
    log.info("Actual backfill: %d events over %d month(s), records changed %d",
             len(fetched), len(months), updated)
    return updated


def maybe_refresh_calendar(cfg: Config, state: dict,
                           session: requests.Session | None = None,
                           now: datetime | None = None) -> bool:
    """Merges the live weekly feed into the archive, once a day.

    Not for the digest - that refreshes the archive itself when it sends. This
    is for the pushes, which name the scheduled releases around a move on every
    day of the week and would otherwise be reading a schedule fetched last
    Saturday. Cheap enough to be unremarkable: one request, and only the first run
    of each UTC day makes it.

    Returns True if the archive was actually refreshed. A feed that will not
    load is a warning, not a failure: the archive still has last week's copy
    and the alerts still go out.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date().isoformat()
    if state.get(_REFRESH_KEY) == today:
        return False

    try:
        fetched = economic_calendar.fetch_calendar(session=session)
    except economic_calendar.CalendarError as exc:
        log.warning("The weekly feed could not be read: %s", exc)
        return False

    changed = economic_calendar.merge_events(
        economic_calendar.store_path(cfg.calendar_dir), fetched)
    state[_REFRESH_KEY] = today
    log.info("Calendar refreshed from the weekly feed: %d events, %d changed",
             len(fetched), changed)
    return True


def _refresh_archive(cfg: Config, session: requests.Session | None,
                     now: datetime, through: datetime) -> str:
    """Brings the archive up to date and returns its path.

    The weekly feed is merged for what it is good at - it is the freshest view
    of the days immediately ahead - and its failure is a warning rather than an
    abort, because the monthly pages carry the coming week and the digest is
    built from the archive either way.
    """
    path = economic_calendar.store_path(cfg.calendar_dir)
    try:
        economic_calendar.merge_events(
            path, economic_calendar.fetch_calendar(session=session))
    except economic_calendar.CalendarError as exc:
        log.warning("The weekly feed could not be read: %s", exc)
    refresh_months(path, session=session, now=now, through=through)
    return path


def _covers(events: list[dict], through: datetime) -> bool:
    """Whether the archive reaches the end of the window being reported on.

    An archive that stops short cannot tell "nothing is scheduled" from "nothing
    was imported", and only one of those is safe to print under the heading "for
    the week".
    """
    if not events:
        return False
    last = max(economic_calendar.parse_event_time(e["date"]) for e in events)
    return last >= through - timedelta(days=_COVERAGE_SLACK_DAYS)


def _send_digest(cfg: Config, session: requests.Session | None,
                 now: datetime) -> bool:
    """Refreshes the archive and sends the coming week's Medium+High digest.

    Failures are swallowed rather than raised: the digest lives inside the hourly
    monitoring run, and a failed send must not bring the whole run down - the same
    approach as the per-asset error handling in __main__.py. Returns True if the
    digest actually went out.
    """
    start, end = coming_week(now)
    path = _refresh_archive(cfg, session, now, end)
    archive = economic_calendar.load_events(path)
    if not _covers(archive, end):
        log.warning("The archive does not reach %s - digest held back", end.date())
        return False

    digest_events = [e for e in economic_calendar.events_in_window(archive, start, end)
                     if e["impact"] in _DIGEST_IMPACTS]
    messages = format_digest(digest_events, start, end)
    try:
        for text in messages:
            send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, text)
    except TelegramError as exc:
        log.error("Failed to send weekly digest: %s", exc)
        return False

    log.info("Weekly digest sent (%d Medium/High events, %s .. %s, %d message(s))",
             len(digest_events), start.date(), end.date(), len(messages))
    return True


def maybe_send_weekly_digest(
    cfg: Config, state: dict, session: requests.Session, now: datetime | None = None,
) -> bool:
    """No-ops outside the weekend note's opening window, and no-ops if this
    week's digest has already been sent. Returns True if one actually went."""
    now = now or datetime.now(timezone.utc)
    if not _is_digest_window(now):
        return False

    week_id = _week_identifier(now)
    if state.get(_STATE_KEY) == week_id:
        return False
    if not _send_digest(cfg, session, now):
        return False

    state[_STATE_KEY] = week_id
    return True


def main() -> int:
    """Manual one-off: sends the digest right now, regardless of day/time,
    without touching state.json's "already sent this week" tracking - this
    isn't part of the regular Saturday schedule (see
    .github/workflows/weekly-digest-test.yml), just a way to see what the
    digest actually looks like without waiting for Saturday."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--force", action="store_true",
        help="Send immediately, bypassing the day-window and already-sent-this-week checks")
    args = parser.parse_args()
    if not args.force:
        parser.error("nothing to do - pass --force (see module docstring)")

    cfg = load_config()
    session = requests.Session()
    return 0 if _send_digest(cfg, session, datetime.now(timezone.utc)) else 1


if __name__ == "__main__":
    sys.exit(main())

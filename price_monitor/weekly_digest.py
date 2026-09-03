"""Posts a Saturday digest of the coming week's Medium/High-impact economic
calendar events to Telegram.

Piggybacks on the existing hourly trigger (see .github/workflows/price-monitor.yml
and README - external cron-job.org calls workflow_dispatch roughly once an hour)
instead of provisioning a second schedule: __main__.py calls
maybe_send_weekly_digest on every run, and it's a no-op except during the one
hourly run that happens to land on Saturday, ~12:00 Israel time. "Already sent
this week" is tracked in state.json (already loaded/saved every run) so a
second run landing in the same hour - or the external trigger firing a little
early or late - never posts the digest twice.

The send day used to be Saturday, on the assumption that the ForexFactory feed
(CALENDAR_URL) has already rolled over to the coming week by then. That cannot be
verified except by a request on an actual Saturday, and both possible week
boundaries at the source (Sunday-Saturday and Saturday-Friday) fit equally well
what the feed serves on a weekday.

So the day is no longer chosen, it is tested. There are two windows, Saturday and
Sunday, and the digest goes out in the first one where the feed genuinely looks
forward (_looks_forward: the feed's last event is still ahead). If Saturday
serves the week that is ending, the message simply waits a day. It will not go
out twice: the dedup key is taken from the feed itself - from the date of its
first event - and Saturday and Sunday, having served the same week, give the same
key.

Low-impact events and holidays are both excluded (see _DIGEST_IMPACTS) - only
Medium/High. No LLM involved on purpose (see README, "Daily signal" and the
weekly digest section): just a plain, programmatically formatted list grouped
by day - the source data already carries the impact tag and the numbers, so
there's nothing here for an LLM to add.

Every value the source gave is printed under each event: actual, forecast,
previous. The actual takes separate work - the live weekly feed does not serve it
at all, see backfill_actuals.

Also runnable directly as a one-off, bypassing the Saturday/dedup checks - see
main() and .github/workflows/weekly-digest-test.yml - for manually checking
what the digest actually looks like without waiting for Saturday:
    python -m price_monitor.weekly_digest --force
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from price_monitor import economic_calendar
from price_monitor.config import Config, load_config
from price_monitor.notifier import TelegramError, send_telegram_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.weekly_digest")

_DIGEST_IMPACTS = {"Medium", "High"}
# datetime.weekday(): Monday=0 ... Saturday=5, Sunday=6.
#
# There are two windows, and that is not belt-and-braces. The feed serves only
# "this week", and where ForexFactory's week boundary falls has been confirmed
# live only for Sunday: on Sunday the request returns exactly the coming week. For
# Saturday it stayed a guess, and both possible boundaries (Sun-Sat and Sat-Fri)
# fit equally well what is served on a weekday - they can only be told apart by a
# request on an actual Saturday.
#
# So the day is not chosen, it is tested. The digest tries to go out on Saturday
# but leaves only if the feed genuinely looks forward (_looks_forward); if
# Saturday still serves the week that is ending, the Sunday window sends it a day
# later. It goes out exactly once: the dedup key is taken from THE FEED ITSELF -
# from the date of its first event - so Saturday and Sunday, having served the
# same week, give the same key.
_DIGEST_WEEKDAYS = (5, 6)
_DIGEST_HOUR_ISRAEL = 12
_STATE_KEY = "weekly_digest:last_sent_week"

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
_IMPACT_EMOJI = {"High": "🔴", "Medium": "🟠"}

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday")

# Telegram rejects a message longer than 4096 characters outright rather than
# truncating it, so the digest is cut into parts on day boundaries. The 96
# characters of headroom cover the "part N of M" line, which would otherwise have
# to be counted recursively.
_MESSAGE_LIMIT = 4000


def _is_digest_window(now: datetime) -> bool:
    israel_now = now.astimezone(_ISRAEL_TZ)
    return (israel_now.weekday() in _DIGEST_WEEKDAYS
            and israel_now.hour == _DIGEST_HOUR_ISRAEL)


def _week_identifier(events: list[dict]) -> str:
    """Dedup key - the date of the FEED's first event, not today's date.

    The digest is about a week, not about the day it is sent, and the key must be
    the week. Taking the run date, Saturday and Sunday would get different keys
    and the same week would go out to the chat twice.
    """
    if not events:
        return ""
    first = min(economic_calendar.parse_event_time(e["date"]) for e in events)
    return first.date().isoformat()


def _looks_forward(events: list[dict], now: datetime) -> bool:
    """Whether the feed looks forward, that is, whether it is about the coming week
    or the one that is ending.

    Judged by the last event: for the coming week it is still ahead, for the
    ending one it is already behind. The last rather than the first: the feed's
    week starts on Sunday, and at Sunday noon some events have already passed even
    though the week is indeed the coming one.
    """
    if not events:
        return False
    last = max(economic_calendar.parse_event_time(e["date"]) for e in events)
    return last > now


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
             f"{_escape(event['country'])} — {_escape(event['title'])}"]
    values = format_event_values(event)
    if values:
        lines.append(f"    <i>{values}</i>")
    return lines


def format_digest(events: list[dict]) -> list[str]:
    """The week's digest, split by day and, when needed, across several messages.
    Returns a list - the sender posts them in order.

    Every event's time is UTC and that is stated once in the header rather than on
    every line: with two or three dozen events, repeating "UTC" on each line takes
    more space than it carries meaning.
    """
    header = "📅 <b>Economic calendar for the week</b>"
    if not events:
        return [f"{header}\n\nNo Medium/High impact events found for this week."]

    ordered = sorted(events, key=lambda e: e["date"])
    high = sum(1 for e in ordered if e["impact"] == "High")
    first = economic_calendar.parse_event_time(ordered[0]["date"])
    last = economic_calendar.parse_event_time(ordered[-1]["date"])
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


def backfill_actuals(path: str, session: requests.Session | None = None,
                     now: datetime | None = None) -> int:
    """Fills in released values (`actual`) for the current and previous month.

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


def _send_digest(cfg: Config, session: requests.Session | None,
                 raw_events: list[dict]) -> bool:
    """Puts the fetched feed into the archive (every impact level - see the
    economic_calendar module docstring), reads back released values and sends the
    Medium+High digest to Telegram.

    Failures are swallowed rather than raised: the digest lives inside the hourly
    monitoring run, and a failed send must not bring the whole run down - the same
    approach as the per-asset error handling in __main__.py. Returns True if the
    digest actually went out.
    """
    path = economic_calendar.store_path(cfg.calendar_dir)
    economic_calendar.merge_events(path, raw_events)
    backfill_actuals(path, session=session)

    digest_events = [e for e in raw_events if e["impact"] in _DIGEST_IMPACTS]
    messages = format_digest(digest_events)
    try:
        for text in messages:
            send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, text)
    except TelegramError as exc:
        log.error("Failed to send weekly digest: %s", exc)
        return False

    log.info("Weekly digest sent (%d Medium/High events of %d total, %d message(s))",
             len(digest_events), len(raw_events), len(messages))
    return True


def _fetch_and_send_digest(cfg: Config, session: requests.Session) -> bool:
    """Manual one-off run (main, --force): fetches the feed and sends the digest
    without the day or already-sent checks. There is deliberately no
    looks-forward check here either - the point of --force is to see the message
    as it is, on any day of the week.
    """
    try:
        raw_events = economic_calendar.fetch_calendar(session=session)
    except economic_calendar.CalendarError as exc:
        log.error("Failed to fetch economic calendar for weekly digest: %s", exc)
        return False
    return _send_digest(cfg, session, raw_events)


def maybe_send_weekly_digest(
    cfg: Config, state: dict, session: requests.Session, now: datetime | None = None,
) -> bool:
    """No-ops outside the Saturday ~12:00 Israel-time window, and no-ops if
    this week's digest has already been sent. Returns True if a digest was
    actually sent."""
    now = now or datetime.now(timezone.utc)
    if not _is_digest_window(now):
        return False

    try:
        raw_events = economic_calendar.fetch_calendar(session=session)
    except economic_calendar.CalendarError as exc:
        log.error("Failed to fetch economic calendar for weekly digest: %s", exc)
        return False

    week_id = _week_identifier(raw_events)
    if state.get(_STATE_KEY) == week_id:
        return False
    if not _looks_forward(raw_events, now):
        # The feed still serves the week that is ending. Sending out a list of
        # what has already happened under the heading "for the week" is not on,
        # and the next day's window will send the real coming week.
        log.info("The feed serves the ending week (%s) - digest deferred", week_id)
        return False

    if not _send_digest(cfg, session, raw_events):
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
        help="Send immediately, bypassing the Saturday-window and already-sent-this-week checks")
    args = parser.parse_args()
    if not args.force:
        parser.error("nothing to do - pass --force (see module docstring)")

    cfg = load_config()
    session = requests.Session()
    return 0 if _fetch_and_send_digest(cfg, session) else 1


if __name__ == "__main__":
    sys.exit(main())

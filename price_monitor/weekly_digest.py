"""Posts a Sunday digest of the coming week's Medium/High-impact economic
calendar events to Telegram.

Piggybacks on the existing hourly trigger (see .github/workflows/price-monitor.yml
and README - external cron-job.org calls workflow_dispatch roughly once an hour)
instead of provisioning a second schedule: __main__.py calls
maybe_send_weekly_digest on every run, and it's a no-op except during the one
hourly run that happens to land on Sunday, ~12:00 Israel time. "Already sent
this week" is tracked in state.json (already loaded/saved every run) so a
second run landing in the same hour - or the external trigger firing a little
early or late - never posts the digest twice.

Low-impact events and holidays are both excluded (see _DIGEST_IMPACTS) - only
Medium/High. No LLM involved on purpose (see README, "Дневной сигнал" and the
weekly digest section): just a plain, programmatically formatted list sorted
by time - the source data already carries the impact tag, so there's nothing
here for an LLM to add.

Also runnable directly as a one-off, bypassing the Sunday/dedup checks - see
main() and .github/workflows/weekly-digest-test.yml - for manually checking
what the digest actually looks like without waiting for Sunday:
    python -m price_monitor.weekly_digest --force
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from price_monitor import economic_calendar
from price_monitor.config import Config, load_config
from price_monitor.notifier import TelegramError, send_telegram_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.weekly_digest")

_DIGEST_IMPACTS = {"Medium", "High"}
# datetime.weekday(): Monday=0 ... Sunday=6.
_DIGEST_WEEKDAY = 6
_DIGEST_HOUR_ISRAEL = 12
_STATE_KEY = "weekly_digest:last_sent_week"

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
_IMPACT_EMOJI = {"High": "🔴", "Medium": "🟠"}


def _is_digest_window(now: datetime) -> bool:
    israel_now = now.astimezone(_ISRAEL_TZ)
    return israel_now.weekday() == _DIGEST_WEEKDAY and israel_now.hour == _DIGEST_HOUR_ISRAEL


def _week_identifier(now: datetime) -> str:
    """One value per Sunday, used to dedupe in state.json - the Israel-time
    calendar date is already unique per week since this only ever runs in the
    Sunday window."""
    return now.astimezone(_ISRAEL_TZ).date().isoformat()


def format_digest(events: list[dict]) -> str:
    lines = ["📅 <b>Экономический календарь на неделю</b>", ""]
    if not events:
        lines.append("На этой неделе не найдено событий Medium/High impact.")
        return "\n".join(lines)
    for event in sorted(events, key=lambda e: e["date"]):
        event_time = economic_calendar.parse_event_time(event["date"])
        emoji = _IMPACT_EMOJI[event["impact"]]
        lines.append(
            f"{emoji} {event_time.strftime('%a %d.%m %H:%M UTC')} "
            f"[{event['country']}] {event['title']}"
        )
    return "\n".join(lines)


def _fetch_and_send_digest(cfg: Config, session: requests.Session) -> bool:
    """Fetches the live ForexFactory feed, persists its High-impact events to
    the archive, and sends the formatted Medium+High digest to Telegram.
    Shared by the scheduled Sunday path (maybe_send_weekly_digest) and the
    manual one-off CLI (main(), --force) - see module docstring. Returns True
    if a digest was actually sent (fetch/send failures are logged and
    swallowed, same as the rest of __main__.py's per-asset error handling, so
    a digest failure never fails the whole hourly run)."""
    try:
        raw_events = economic_calendar.fetch_calendar(session=session)
    except economic_calendar.CalendarError as exc:
        log.error("Failed to fetch economic calendar for weekly digest: %s", exc)
        return False

    # The archive only keeps High-impact events (see economic_calendar's
    # module docstring for why) - the Telegram digest below still shows
    # Medium+High regardless, since that's about what's coming up this week,
    # not about the archive's cross-period consistency.
    economic_calendar.merge_events(
        economic_calendar.store_path(cfg.calendar_dir),
        economic_calendar.filter_high_impact_only(raw_events))

    digest_events = [e for e in raw_events if e["impact"] in _DIGEST_IMPACTS]
    digest_text = format_digest(digest_events)
    try:
        send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, digest_text)
    except TelegramError as exc:
        log.error("Failed to send weekly digest: %s", exc)
        return False

    log.info("Weekly digest sent (%d Medium/High events of %d total)", len(digest_events), len(raw_events))
    return True


def maybe_send_weekly_digest(
    cfg: Config, state: dict, session: requests.Session, now: datetime | None = None,
) -> bool:
    """No-ops outside the Sunday ~12:00 Israel-time window, and no-ops if
    this week's digest has already been sent. Returns True if a digest was
    actually sent."""
    now = now or datetime.now(timezone.utc)
    if not _is_digest_window(now):
        return False
    week_id = _week_identifier(now)
    if state.get(_STATE_KEY) == week_id:
        return False

    if not _fetch_and_send_digest(cfg, session):
        return False

    state[_STATE_KEY] = week_id
    return True


def main() -> int:
    """Manual one-off: sends the digest right now, regardless of day/time,
    without touching state.json's "already sent this week" tracking - this
    isn't part of the regular Sunday schedule (see
    .github/workflows/weekly-digest-test.yml), just a way to see what the
    digest actually looks like without waiting for Sunday."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--force", action="store_true",
        help="Send immediately, bypassing the Sunday-window and already-sent-this-week checks")
    args = parser.parse_args()
    if not args.force:
        parser.error("nothing to do - pass --force (see module docstring)")

    cfg = load_config()
    session = requests.Session()
    return 0 if _fetch_and_send_digest(cfg, session) else 1


if __name__ == "__main__":
    sys.exit(main())

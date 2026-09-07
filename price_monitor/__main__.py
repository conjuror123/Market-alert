"""One hourly pass: deliver what Tremor found, and say so if something broke.

This module used to be the detector. It fetched sixteen assets, scored each one
against its own recent history with a pair of z-scores, and messaged when either
crossed a threshold. Tremor replaced that - not refined it, replaced it - and the
old signals were switched off long before they were removed, so their absence
here is the end of a migration rather than a loss of function. What they did
badly is on the record in docs/decisions.md: one threshold shared by
every instrument, so a 1.5% hour meant the same thing in SHY as in SOL; nothing
to say how rare a move was once it fired; and no way to tell an instrument moving
on its own from the whole market moving together.

What is left is a delivery pass. Three things run, none of which decide anything:

  the weekly calendar digest - a forecast of the coming week's scheduled
  releases, tried on Saturday at 12:00 Israel time and sent on Sunday instead
  if the feed is still serving the week that is ending. Once a week, a no-op
  every other hour (see weekly_digest.py).

  Tremor delivery - the pushes and the Tuesday/Friday digest, read off the event
  table the pipeline wrote earlier in this same workflow run. If that pipeline
  did not run, the events are stale and delivery's own 48-hour rule sends
  nothing, which is the safe direction.

  the health report - whether the previous runs failed, and a message when that
  changes. It reports runs that FAILED. It cannot report runs that never
  happened, and that gap is real: the external trigger stopped on 2026-09-01 and
  nothing noticed for six days.
"""
from __future__ import annotations

import logging
import sys

import requests

from price_monitor import health, tremor_delivery, weekly_digest
from price_monitor.config import load_config
from price_monitor.notifier import TelegramError, send_telegram_message
from price_monitor.state import load_state, save_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor")


def format_health_down(streak: int, error_details: list[str]) -> str:
    lines = [
        f"⚠️ <b>Monitoring has been failing for {streak} run(s) in a row</b>",
        "Check the Actions tab in the repository — the data source may have broken",
        "or the Telegram token may be invalid.",
        "",
    ]
    lines.extend(f"• {d}" for d in error_details[:10])
    return "\n".join(lines)


def format_health_recovered(streak: int) -> str:
    return f"✅ Monitoring recovered after {streak} failed run(s) in a row."


def main() -> int:
    cfg = load_config()
    state = load_state(cfg.state_path)
    session = requests.Session()

    had_error = False
    error_details: list[str] = []

    # No-op except in the 12:00 Israel-time hour on Saturday or Sunday, and then
    # only once a week - see weekly_digest.py's module docstring for which of the
    # two days it lands on and why this piggybacks on the hourly trigger instead
    # of taking a schedule of its own.
    try:
        weekly_digest.maybe_send_weekly_digest(cfg, state, session)
    except Exception as exc:                     # pragma: no cover - defensive
        log.error("Weekly calendar digest failed: %s", exc)
        had_error = True
        error_details.append(f"weekly calendar digest failed ({exc})")

    # Wrapped for the same reason it always was: a fault in a delivery layer must
    # not cost the run its health reporting, which is the thing that would tell
    # anyone the fault exists.
    try:
        sent = tremor_delivery.maybe_deliver(cfg, state)
        log.info("Tremor delivery: %d message(s) sent", sent)
    except Exception as exc:                     # pragma: no cover - defensive
        log.error("Tremor delivery failed: %s", exc)
        had_error = True
        error_details.append(f"Tremor delivery failed ({exc})")

    if had_error:
        streak = health.record_failure(state)
        if health.should_alert_down(streak, cfg.health_alert_after_failures,
                                    cfg.health_reminder_every_failures):
            try:
                send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                      format_health_down(streak, error_details))
                log.info("Monitoring-down alert sent (streak=%d)", streak)
            except TelegramError as exc:
                log.error("Failed to send monitoring-down alert: %s", exc)
    else:
        previous_streak = health.record_success(state)
        if previous_streak >= cfg.health_alert_after_failures:
            try:
                send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                      format_health_recovered(previous_streak))
                log.info("Monitoring-recovered alert sent")
            except TelegramError as exc:
                log.error("Failed to send monitoring-recovered alert: %s", exc)

    save_state(cfg.state_path, state)
    log.info("Run complete.")
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())

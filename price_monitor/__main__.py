"""One hourly pass: deliver what Tremor found, and say so if something broke.

Detection lives in `tremor`; this module decides nothing. Four things run:

  a daily top-up of the economic-calendar archive from the live weekly feed.
  Not for the digest, which refreshes the archive itself when it sends, but for
  the pushes: they name the releases around a move on every day of the week, and
  a schedule fetched last Friday does not have the speech added on Wednesday.

  the weekly calendar digest - a forecast of the coming week's scheduled
  releases, in the run that opens the week's note (after the week's last NYSE
  close, tremor.routing). Once a week, a no-op every other hour (see
  weekly_digest.py). It runs FIRST, and that is the point of the order: the
  note goes out in the same run, and it is the one that keeps changing all
  week, so it belongs last in the chat.

  Tremor delivery - the pushes and pings, and the weekly note opened at the
  start of its week and edited in place for the rest of it. Read off the event
  table the pipeline wrote earlier in this same workflow run. If that pipeline
  did not run, the events are stale and delivery's own rules - nothing rings
  more than 24 hours after it was found - send nothing new, which is the safe
  direction; the crash itself is reported as a failure.

  the health report - whether the previous runs failed, and a message when that
  changes. It reports runs that FAILED, which is all a check living inside the
  run can report: a run that never happened increments nothing. Hours with no
  run at all are named by the first run after them; a run that died before
  this module is named by the workflow's last step (tools/run_died.sh) - see
  docs/manual.md, "Running it".
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests

from price_monitor import health, tremor_delivery, weekly_digest
from price_monitor.config import load_config
from price_monitor.notifier import quote, send_health
from price_monitor.state import CorruptState, load_state, save_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor")


def format_health_down(streak: int, error_details: list[str]) -> str:
    lines = [
        f"⚠️ <b>Monitoring has been failing for {streak} run(s) in a row</b>",
        "Check the Actions tab in the repository — the data source may have broken",
        "or the Telegram token may be invalid.",
        "",
    ]
    lines.extend(f"• {quote(d)}" for d in error_details[:10])
    return "\n".join(lines)


def format_missed_runs(previous: int, now: int, missed: int) -> str:
    since = datetime.fromtimestamp(previous, timezone.utc)
    until = datetime.fromtimestamp(now, timezone.utc)
    return (f"⚠️ <b>No run between {since:%Y-%m-%d %H:%M} and {until:%Y-%m-%d %H:%M} "
            f"UTC</b>\n{missed} hourly run(s) missing: the trigger did not fire, or the "
            "runs died before saving. Moves in that time were not sent when found; "
            "those from its last 24 hours go out now, late.")


def format_health_recovered(streak: int) -> str:
    return f"✅ Monitoring recovered after {streak} failed run(s) in a row."


def tremor_pipeline_crashed() -> bool:
    """True when the pipeline or the detector itself died, so no events were
    written this run - as opposed to a backfill that lost an instrument, which
    names who went dark in its own alert. The workflow tells them apart: a
    crash exits before the step writes its `failed` output."""
    value = os.environ.get("TREMOR_PIPELINE_CRASHED", "").strip().lower()
    return value in ("1", "true", "yes")


# Steps that do not stop the run when they fail, so nothing else would say so.
# Each is a failed run here: broken, if not yet urgent.
SIDE_STEPS = {
    "SESSIONS_STEP_OUTCOME":
        "the session table could not be extended: it has under two years left "
        "(tremor/sessions.py)",
    "HOT_SAVE_STEP_OUTCOME":
        "the open months of the bars were not saved to the release: the next run "
        "restores an older copy and fetches the difference again",
}


def failed_side_steps() -> list[str]:
    return [text for env, text in SIDE_STEPS.items()
            if os.environ.get(env, "").strip().lower() in ("failure", "cancelled")]


def tremor_stopped_at(now: float | None = None) -> "tuple[str, int] | None":
    """Where the Tremor step was when it stopped short, and about how many
    minutes into it (counted to now): (stage, minutes), or None if it reached
    its end or left no readable record. The step appends "<stage> <epoch>" as
    each part starts. Never raises: it is read after delivery and before the
    state is saved, where a raise would lose what the run sent."""
    try:
        with open(os.environ.get("TREMOR_STAGE_FILE", ""), encoding="utf-8") as f:
            rows = [line.split() for line in f if line.strip()]
        if not rows or rows[-1][0] == "done":
            return None
        now = time.time() if now is None else now
        return rows[-1][0], int((now - int(rows[0][1])) // 60)
    except (OSError, IndexError, ValueError):
        return None


def main() -> int:
    cfg = load_config()
    try:
        state = load_state(cfg.state_path)
    except CorruptState as exc:
        log.error("Refusing to run with a corrupt sent map: %s", exc)
        return 2
    session = requests.Session()

    # Hours with no run at all, which no run inside them could report. Said
    # straight away, by the first run after them, not through the streak.
    now = int(time.time())
    previous = health.record_run(state, now)
    missed = health.missed_runs(previous, now)
    if missed:
        send_health(format_missed_runs(previous, now, missed),
                    cfg.telegram_bot_token, cfg.telegram_health_chat_id)

    had_error = False
    error_details: list[str] = []

    # Once a day, and nothing to do on the other twenty-three runs. Separate
    # from the digest because it serves the PUSHES: they name the releases in
    # the three hours around a move, every day of the week, and the digest's own
    # weekly refresh would leave them reading last Friday's schedule.
    try:
        weekly_digest.maybe_refresh_calendar(cfg, state, session)
    except Exception as exc:                     # pragma: no cover - defensive
        log.error("Calendar refresh failed: %s", exc)
        had_error = True
        error_details.append(f"calendar refresh failed ({exc})")

    # Once a week, in the run that opens the week's note, and before it: see
    # this module's docstring for the order.
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

    if tremor_pipeline_crashed():
        had_error = True
        stopped = tremor_stopped_at()
        if stopped:
            # Its time limit, or a crash: the minutes say which.
            error_details.append(f"the Tremor step stopped during {stopped[0]}, "
                                 f"about {stopped[1]} min into the step: events were "
                                 "not refreshed")
        else:
            error_details.append("the Tremor pipeline or detector crashed: events "
                                 "were not refreshed")

    for text in failed_side_steps():
        had_error = True
        error_details.append(text)

    if had_error:
        streak = health.record_failure(state)
        if health.should_alert_down(streak, cfg.health_alert_after_failures,
                                    cfg.health_reminder_every_failures):
            send_health(format_health_down(streak, error_details),
                        cfg.telegram_bot_token, cfg.telegram_health_chat_id)
    else:
        # Clean as a run, whatever one instrument did: the instrument has its
        # own line on the health chat every run, and the streak is the run's.
        previous_streak = health.record_success(state)
        if previous_streak >= cfg.health_alert_after_failures:
            send_health(format_health_recovered(previous_streak),
                        cfg.telegram_bot_token, cfg.telegram_health_chat_id)

    save_state(cfg.state_path, state)
    log.info("Run complete.")
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())

"""The watchdog (.github/workflows/watchdog.yml): tells the health chat, once a
week, when the hourly run has stopped.

Every other health line is sent by the hourly run itself, so a run that never
starts is silent - cron-job.org's job disabled after its failures, the GitHub
token it calls with expired, the service down - and the channel reads as a
quiet market. This runs on GitHub's own schedule, unreliable to the hour
(docs/manual.md, "Running it") but independent of cron-job.org, on Monday
(the user's, 2026-11-04: a week is soon enough). It reads the last committed
run's time from data/state.json and nothing else, and writes nothing.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone

from price_monitor import health, notifier
from price_monitor.state import CorruptState, load_state

log = logging.getLogger(__name__)

STATE_PATH = os.path.join("data", "state.json")
# Runs begin at :05 and commit within ~3 minutes; three hours is two runs missed
# and a third late, not a slow one.
STALE_SECONDS = 3 * 3600


def _ago(seconds: int) -> str:
    hours = seconds // 3600
    return f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"


def message(state: "dict | None", now: int) -> "str | None":
    """The health line when the last committed run is older than STALE_SECONDS,
    or there is no record of one (state None: the file could not be read)."""
    last = ((state or {}).get(health.STATE_KEY) or {}).get("last_run_utc")
    if last is not None and now - int(last) <= STALE_SECONDS:
        return None
    if last is None:
        when = "no run on record in data/state.json"
    else:
        stamp = datetime.fromtimestamp(int(last), timezone.utc).strftime("%Y-%m-%d %H:%M")
        when = f"last run {stamp} UTC, {_ago(now - int(last))}"
    return ("🚨 <b>The hourly run has stopped</b>\n"
            f"Watchdog: {when}. Nothing reaches the channel or this chat until it runs "
            "again. Check the cron-job.org job (enabled, its last answers) and the GitHub "
            "token it calls with (not expired); then the workflow's last runs.")


def main(path: str = STATE_PATH, now: "int | None" = None) -> int:
    """0 when the run is current or the health chat was told; 1 when it could not
    be (the red job is then GitHub's own e-mail)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    now = int(time.time()) if now is None else now
    try:
        state = load_state(path)
    except CorruptState:
        state = None
    text = message(state, now)
    if text is None:
        last = state[health.STATE_KEY]["last_run_utc"]
        log.info("The hourly run is current: last committed %d min ago", (now - last) // 60)
        return 0
    log.warning(text.splitlines()[1])
    return 0 if notifier.send_health(text) else 1


if __name__ == "__main__":
    sys.exit(main())

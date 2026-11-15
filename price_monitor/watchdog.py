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
    return f"{hours} ч назад" if hours < 48 else f"{hours // 24} дн. назад"


def message(state: "dict | None", now: int) -> "str | None":
    """The health line when the last committed run is older than STALE_SECONDS,
    or there is no record of one (state None: the file could not be read). In
    Russian and at length, as all of the health chat is (the user's, 2026-11-15)."""
    last = ((state or {}).get(health.STATE_KEY) or {}).get("last_run_utc")
    if last is not None and now - int(last) <= STALE_SECONDS:
        return None
    if last is None:
        when = "в data/state.json нет записи ни об одном прогоне"
    else:
        stamp = datetime.fromtimestamp(int(last), timezone.utc).strftime("%Y-%m-%d %H:%M")
        when = f"последний прогон был {stamp} UTC, {_ago(now - int(last))}"
    return ("🚨 <b>Часовой прогон остановился</b>\n"
            f"Сторож (раз в неделю, по понедельникам): {when}.\n"
            "Что это значит: пока прогон не запустится снова, в канал не приходит ничего, "
            "и сюда тоже: остальные сообщения здоровья шлёт сам прогон. Канал молчит не "
            "потому, что рынок спокоен.\n"
            "Что делать, по порядку: 1) на cron-job.org: включено ли задание и что оно "
            "отвечало последние разы; 2) не истёк ли токен GitHub, с которым оно запускает "
            "прогон (если истёк, выпустить новый и вставить в задание); 3) последние "
            "прогоны во вкладке Actions репозитория.")


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

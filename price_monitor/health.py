"""Tracks consecutive failed monitoring runs and decides when to page about it.

A "failed run" is one where the calendar or Tremor's delivery raised, or the
Tremor pipeline crashed (price_monitor.__main__). Without this, a broken data source or a
revoked Telegram token would fail silently forever - GitHub Actions would show a
red X on the workflow, but nobody would notice unless they went looking.
"""
from __future__ import annotations

STATE_KEY = "_monitoring_health"


def _health(state: dict) -> dict:
    return state.setdefault(STATE_KEY, {"consecutive_failures": 0})


def record_failure(state: dict) -> int:
    """Bump the failure streak after a run that had errors. Returns the new streak."""
    h = _health(state)
    h["consecutive_failures"] += 1
    return h["consecutive_failures"]


def record_success(state: dict) -> int:
    """Reset the failure streak after a clean run. Returns the streak length just before
    the reset, so the caller can tell whether this run is recovering from an outage."""
    h = _health(state)
    previous = h["consecutive_failures"]
    h["consecutive_failures"] = 0
    return previous


def should_alert_down(streak: int, alert_after: int, reminder_every: int) -> bool:
    """Whether this run's failure streak warrants a new "monitoring is down" message.

    Fires once when the streak first reaches `alert_after`, then every
    `reminder_every` further failures so a long outage isn't forgotten
    (reminder disabled if `reminder_every` <= 0 - only the initial alert fires).
    """
    if alert_after <= 0 or streak < alert_after:
        return False
    if reminder_every <= 0:
        return streak == alert_after
    return (streak - alert_after) % reminder_every == 0


# Runs begin at :05 every hour (2026-08-28..10-05: 761 runs, every gap 60 min
# but three outages); the monitor reaches this point 2 to 12 minutes in. A
# gap past 90 minutes is a run that never began or never saved its state.
MISSED_RUN_GAP_SECONDS = 90 * 60


def record_run(state: dict, now: int) -> int | None:
    """Note this run's time. Returns the previous run's, or None on the first."""
    h = _health(state)
    previous = h.get("last_run_utc")
    h["last_run_utc"] = int(now)
    return previous


def missed_runs(previous: int | None, now: int) -> int:
    """How many hourly runs are missing between the previous run and this one."""
    if previous is None or now - previous <= MISSED_RUN_GAP_SECONDS:
        return 0
    return max(1, round((now - previous) / 3600) - 1)

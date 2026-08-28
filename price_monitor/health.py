"""Tracks consecutive failed monitoring runs and decides when to page about it.

A "failed run" is any run where fetching market data or sending a Telegram alert
raised an error for at least one asset. Without this, a broken data source or a
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

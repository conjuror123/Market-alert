import os
from datetime import datetime, timedelta, timezone

from price_monitor.state import load_state, record_alert, save_state, should_notify


def test_save_and_load_roundtrip(tmp_path):
    path = os.path.join(tmp_path, "state.json")
    state = {}
    record_alert(state, "BTCUSDT", severity=5.0, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    save_state(path, state)

    loaded = load_state(path)
    assert loaded == state


def test_load_state_missing_file_returns_empty(tmp_path):
    path = os.path.join(tmp_path, "missing.json")
    assert load_state(path) == {}


def test_load_state_corrupt_file_returns_empty(tmp_path):
    path = os.path.join(tmp_path, "state.json")
    with open(path, "w") as f:
        f.write("not json")
    assert load_state(path) == {}


def test_no_previous_alert_always_notifies():
    assert should_notify({}, "ETHUSDT", severity=3.0, cooldown_minutes=120, escalation_factor=1.3)


def test_cooldown_blocks_similar_severity_repeat():
    state = {}
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    record_alert(state, "BTCUSDT", severity=5.0, now=now)
    assert not should_notify(
        state, "BTCUSDT", severity=5.0, cooldown_minutes=120, escalation_factor=1.3,
        now=now + timedelta(minutes=30),
    )


def test_cooldown_expires_after_window():
    state = {}
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    record_alert(state, "BTCUSDT", severity=5.0, now=now)
    assert should_notify(
        state, "BTCUSDT", severity=5.0, cooldown_minutes=120, escalation_factor=1.3,
        now=now + timedelta(minutes=130),
    )


def test_escalation_breaks_through_cooldown():
    state = {}
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    record_alert(state, "BTCUSDT", severity=5.0, now=now)
    # 30 minutes later, well inside a 120-minute cooldown, but the new event is
    # 1.3x more severe than what triggered the last alert - should still fire.
    assert should_notify(
        state, "BTCUSDT", severity=6.6, cooldown_minutes=120, escalation_factor=1.3,
        now=now + timedelta(minutes=30),
    )


def test_marginal_escalation_still_suppressed():
    state = {}
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    record_alert(state, "BTCUSDT", severity=5.0, now=now)
    # Bigger, but not by the required 1.3x factor.
    assert not should_notify(
        state, "BTCUSDT", severity=5.5, cooldown_minutes=120, escalation_factor=1.3,
        now=now + timedelta(minutes=30),
    )


def test_override_severity_bypasses_cooldown_unconditionally():
    state = {}
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    record_alert(state, "BTCUSDT", severity=5.0, now=now)
    # Only 5 minutes later, and not even a big escalation over the last alert (5.0) -
    # but this reading alone clears the override bar, so it must go through anyway.
    assert should_notify(
        state, "BTCUSDT", severity=6.0, cooldown_minutes=2880, escalation_factor=1.3,
        override_severity=6.0, now=now + timedelta(minutes=5),
    )


def test_below_override_severity_still_respects_cooldown():
    state = {}
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    record_alert(state, "BTCUSDT", severity=5.0, now=now)
    assert not should_notify(
        state, "BTCUSDT", severity=5.9, cooldown_minutes=2880, escalation_factor=1.3,
        override_severity=6.0, now=now + timedelta(minutes=5),
    )


def test_record_alert_updates_severity_for_next_comparison():
    state = {}
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    record_alert(state, "BTCUSDT", severity=5.0, now=t0)
    record_alert(state, "BTCUSDT", severity=8.0, now=t0 + timedelta(minutes=10))
    assert state["BTCUSDT"]["last_severity"] == 8.0
    # a later, only mildly bigger event than the *new* baseline (8.0) is suppressed
    assert not should_notify(
        state, "BTCUSDT", severity=8.5, cooldown_minutes=120, escalation_factor=1.3,
        now=t0 + timedelta(minutes=20),
    )

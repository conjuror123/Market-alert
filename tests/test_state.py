import json
import os
from datetime import datetime, timedelta, timezone

from price_monitor.state import is_in_cooldown, load_state, record_alert, save_state


def test_save_and_load_roundtrip(tmp_path):
    path = os.path.join(tmp_path, "state.json")
    state = {}
    record_alert(state, "BTCUSDT", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
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


def test_cooldown_blocks_recent_alert():
    state = {}
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    record_alert(state, "BTCUSDT", now=now)
    assert is_in_cooldown(state, "BTCUSDT", cooldown_minutes=120, now=now + timedelta(minutes=30))


def test_cooldown_expires():
    state = {}
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    record_alert(state, "BTCUSDT", now=now)
    assert not is_in_cooldown(state, "BTCUSDT", cooldown_minutes=120, now=now + timedelta(minutes=130))


def test_no_cooldown_for_unseen_symbol():
    assert not is_in_cooldown({}, "ETHUSDT", cooldown_minutes=120)

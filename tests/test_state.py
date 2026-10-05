import os

import pytest

from price_monitor.state import CorruptState, load_state, save_state


def test_save_and_load_roundtrip(tmp_path):
    path = os.path.join(tmp_path, "state.json")
    state = {"jump_delivery": {"sent": {"twelvedata:SPY:1767225600": 1767225600}}}
    save_state(path, state)
    assert load_state(path) == state
    assert not os.path.exists(path + ".tmp")


def test_load_state_missing_file_returns_empty(tmp_path):
    assert load_state(os.path.join(tmp_path, "nope.json")) == {}


def test_load_state_corrupt_file_is_refused(tmp_path):
    # A half-written state file must not be treated as a cold start: loading {}
    # would re-send every push still inside the 48-hour window.
    path = os.path.join(tmp_path, "state.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    with pytest.raises(CorruptState, match="not valid JSON"):
        load_state(path)


def test_a_state_written_before_the_rename_is_read_under_the_new_key(tmp_path):
    # Production's state keeps the week's messages under "tremor_delivery".
    # Read under the new name only, the calendar digest would see no week and
    # go out mid-week, and nothing of the channel would be adopted.
    from datetime import datetime, timezone
    from price_monitor import jump_delivery
    path = tmp_path / "state.json"
    old = {"digests": {}, "sent": {"a": {"hour": 1, "id": 7}}, "tracked": {}, "pings": {}}
    save_state(str(path), {"tremor_delivery": old, "_monitoring_health": {}})
    state = load_state(str(path))
    assert state[jump_delivery.STATE_KEY] == old and "tremor_delivery" not in state
    assert jump_delivery.note_due(state, datetime.now(timezone.utc)) is False


def test_a_state_already_under_the_new_key_is_left_alone(tmp_path):
    path = tmp_path / "state.json"
    save_state(str(path), {"jump_delivery": {"week": {"slot": 1}}, "tremor_delivery": {"x": 1}})
    assert load_state(str(path))["jump_delivery"] == {"week": {"slot": 1}}

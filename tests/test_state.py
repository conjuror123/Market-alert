import os

from price_monitor.state import load_state, save_state


def test_save_and_load_roundtrip(tmp_path):
    path = os.path.join(tmp_path, "state.json")
    state = {"tremor_delivery": {"sent": {"twelvedata:SPY:1767225600": 1767225600}}}
    save_state(path, state)
    assert load_state(path) == state


def test_load_state_missing_file_returns_empty(tmp_path):
    assert load_state(os.path.join(tmp_path, "nope.json")) == {}


def test_load_state_corrupt_file_returns_empty(tmp_path):
    # A half-written state file must not take the run down with it: the whole
    # point of the file is to survive a run that went wrong.
    path = os.path.join(tmp_path, "state.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert load_state(path) == {}

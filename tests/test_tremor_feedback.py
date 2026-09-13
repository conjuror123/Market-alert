"""Recording what a message was worth, and reading the knobs back off it."""
import numpy as np
import pandas as pd
import pytest

from tremor import feedback as fb

HOUR = 3600


def events(rows):
    return pd.DataFrame(rows, columns=["asset_id", "hour_utc", "tier", "basis",
                                       "channel", "r", "sigma_lt"])


SAMPLE = events([
    ("twelvedata:IEI", 100 * HOUR, "noticeable", "abnormal", "digest", 0.0003, 0.0058),
    ("twelvedata:SPY", 200 * HOUR, "major", "absolute", "push", 0.0400, 0.0050),
    ("twelvedata:TLT", 300 * HOUR, "high", "both", "digest", 0.0120, 0.0040),
])


def test_a_time_is_read_the_way_a_message_writes_it():
    assert fb.parse_when("2026-09-09 13:00") == fb.parse_when("2026-09-09 13:00 UTC")
    assert fb.parse_when("2026-09-09T13:00") == fb.parse_when("2026-09-09 13:00")
    with pytest.raises(ValueError):
        fb.parse_when("last Tuesday")


def test_a_verdict_round_trips_and_re_judging_replaces(tmp_path):
    path = str(tmp_path / "feedback.csv")
    fb.record("twelvedata:IEI", 100 * HOUR, fb.BORING, path)
    fb.record("twelvedata:SPY", 200 * HOUR, fb.BORING, path)
    assert len(fb.load(path)) == 2

    # Changing your mind about the same bar replaces it rather than stacking.
    fb.record("twelvedata:IEI", 100 * HOUR, fb.MISSED, path)
    rows = fb.load(path)
    assert len(rows) == 2
    assert {r["asset_id"]: r["verdict"] for r in rows}["twelvedata:IEI"] == fb.MISSED

    with pytest.raises(ValueError):
        fb.record("twelvedata:IEI", 100 * HOUR, "meh", path)


def test_a_quoted_hour_matches_the_bar_the_message_meant():
    # A message names a bar by its CLOSING moment and the store by its opening,
    # so the quoted time is an hour off by construction and must still match.
    rows = [{"asset_id": "twelvedata:IEI", "hour_utc": 101 * HOUR,
             "verdict": fb.BORING}]
    (_, match), = fb.attach(rows, SAMPLE)
    assert match is not None and int(match["hour_utc"]) == 100 * HOUR


def test_a_verdict_about_nothing_says_so_rather_than_guessing():
    rows = [{"asset_id": "twelvedata:IEI", "hour_utc": 9000 * HOUR,
             "verdict": fb.BORING}]
    (_, match), = fb.attach(rows, SAMPLE)
    assert match is None
    assert any("no event within" in line for line in fb.suggest(rows, SAMPLE))


def test_a_small_boring_move_points_at_the_floor():
    # The IEI case: 0.03% at half its usual hour. The floor is the right lever
    # and the report should name it.
    rows = [{"asset_id": "twelvedata:IEI", "hour_utc": 100 * HOUR,
             "verdict": fb.BORING}]
    text = "\n".join(fb.suggest(rows, SAMPLE))
    assert "0.05x" in text or "0.06x" in text      # 0.0003 / 0.0058
    assert "min_move_sigma" in text
    assert "sensitivity is the one" not in text


def test_a_large_boring_move_refuses_to_blame_the_floor():
    # An 8x move judged not worth reading cannot be fixed with a size floor
    # without taking nearly everything else with it, and saying otherwise would
    # be advice that quietly guts the basket.
    rows = [{"asset_id": "twelvedata:SPY", "hour_utc": 200 * HOUR,
             "verdict": fb.BORING}]
    text = "\n".join(fb.suggest(rows, SAMPLE))
    assert "the floor is the wrong lever here" in text
    assert "sensitivity is the one" in text


def test_a_missed_move_that_was_found_is_a_routing_problem():
    # It existed and was not delivered, so no amount of sensitivity would have
    # helped - the report has to separate "not found" from "not sent".
    rows = [{"asset_id": "twelvedata:TLT", "hour_utc": 300 * HOUR,
             "verdict": fb.MISSED}]
    text = "\n".join(fb.suggest(rows, SAMPLE))
    assert "routing question" in text


def test_nothing_recorded_is_said_plainly():
    assert any("nothing recorded yet" in line for line in fb.suggest([], SAMPLE))

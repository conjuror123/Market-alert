import numpy as np
import pandas as pd

from meals import evaluate


def flags(*bits):
    return np.array(bits, dtype=bool)


# --- episodes -------------------------------------------------------------

def test_consecutive_significant_hours_form_one_episode():
    # A 24-hour forward label turns one shock into a run of significant hours;
    # counting each of them separately would be counting the same shock over.
    assert evaluate.episodes(flags(0, 1, 1, 1, 0)) == [(1, 3)]


def test_a_gap_separates_two_episodes():
    assert evaluate.episodes(flags(1, 1, 0, 1)) == [(0, 1), (3, 3)]


def test_an_episode_touching_either_edge_is_closed():
    assert evaluate.episodes(flags(1, 1)) == [(0, 1)]


def test_nothing_significant_is_no_episodes():
    assert evaluate.episodes(flags(0, 0, 0)) == []


# --- cooldown -------------------------------------------------------------

def test_the_cooldown_keeps_the_first_firing_and_silences_the_rest():
    kept = evaluate.apply_cooldown(np.array([0, 1, 5, 100, 101]), cooldown=72)
    assert list(kept) == [0, 100]


def test_the_cooldown_measures_from_the_kept_firing_not_the_last_seen():
    # Firings at 0, 70, 140: the one at 70 is inside the pause opened at 0, so
    # the next pause still starts at 0 and 140 is free.
    kept = evaluate.apply_cooldown(np.array([0, 70, 140]), cooldown=72)
    assert list(kept) == [0, 140]


# --- scoring --------------------------------------------------------------

def test_a_detection_inside_an_episode_is_a_hit():
    result = evaluate.score(np.array([5]), [(3, 8)], lead_max=0)
    assert result["precision"] == 1.0 and result["recall"] == 1.0


def test_a_detection_before_an_episode_counts_within_the_lead_window():
    early = evaluate.score(np.array([0]), [(10, 12)], lead_max=24)
    assert early["caught"] == 1 and early["median_lead"] == 10


def test_a_detection_too_far_ahead_does_not_count():
    far = evaluate.score(np.array([0]), [(100, 102)], lead_max=24)
    assert far["caught"] == 0 and far["precision"] == 0.0


def test_a_detection_after_an_episode_ends_does_not_count():
    late = evaluate.score(np.array([20]), [(3, 8)], lead_max=24)
    assert late["caught"] == 0


def test_lead_is_measured_from_the_earliest_alert_of_the_episode():
    # Two alerts credited to one episode: the first is the one that would have
    # reached a person, so it is the one the lead describes.
    result = evaluate.score(np.array([2, 9]), [(10, 14)], lead_max=24)
    assert result["caught"] == 1 and result["median_lead"] == 8


def test_one_alert_covering_two_episodes_is_counted_once_for_precision():
    result = evaluate.score(np.array([10]), [(10, 11), (12, 13)], lead_max=24)
    assert result["precision"] == 1.0
    assert result["recall"] == 1.0 and result["caught"] == 2


def test_recall_counts_episodes_not_hours():
    # One alert, one long episode: a per-hour reading would call this 1 of 6
    # because the cooldown forbids the other five.
    result = evaluate.score(np.array([3]), [(0, 5)], lead_max=0)
    assert result["recall"] == 1.0


def test_missing_every_episode_gives_zero_not_nan():
    result = evaluate.score(np.array([50]), [(0, 2)], lead_max=24)
    assert result["recall"] == 0.0 and result["f1"] == 0.0


def test_no_episodes_leaves_the_rates_undefined():
    result = evaluate.score(np.array([1, 2]), [])
    assert result["episodes"] == 0
    assert result["precision"] != result["precision"]      # NaN


def test_a_detector_that_never_fires_has_no_precision_but_zero_recall():
    result = evaluate.score(np.array([], dtype=int), [(0, 2)], lead_max=24)
    assert result["recall"] == 0.0
    assert result["detections"] == 0


# --- warm-up --------------------------------------------------------------

def test_evaluation_starts_when_the_last_input_is_warm():
    warmup = pd.DataFrame({
        "scope": ["basket", "twelvedata:SPY", "coinbase:BTC-USD", "twelvedata:SPY"],
        "trigger": ["single_factor", "breach_q95", "breach_q95", "v_r"],
        "first_valid_hour": [100.0, 900.0, 300.0, 10_000.0],
        "evaluated_hours": [1, 1, 1, 1], "total_hours": [1, 1, 1, 1]})

    # v_r is not what the cluster shift is built from, so its later warm-up does
    # not hold the whole evaluation back.
    assert evaluate.evaluation_start(warmup) == 900


def test_f1_is_undefined_only_when_nothing_fired():
    silent = evaluate.score(np.array([], dtype=int), [(0, 2)], lead_max=24)
    assert silent["f1"] != silent["f1"]      # NaN: there is no precision to take

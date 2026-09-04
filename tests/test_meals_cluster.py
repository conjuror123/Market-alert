import numpy as np
import pandas as pd

from meals import cluster, si_index, windows

HOUR = 3600


def frame(n=400, **overrides):
    """An even run of hours where by default nothing happens."""
    data = {
        "quorum_ok": [True] * n,
        "trigger_cluster_shift": [False] * n,
        "si_total": [0.0] * n,
        "base_points": [0] * n,
        "breadth_q99": [False] * n,
        "m_weighted_median": [0.0] * n,
        "sigma_m": [0.01] * n,
        "k": [2.0] * n,
    }
    for key, positions in overrides.items():
        for index, value in positions.items():
            data[key][index] = value
    return pd.DataFrame(data, index=[(i + 1) * HOUR for i in range(n)])


# Scores are expressed relative to the configured thresholds rather than as
# literals: these tests are about the automaton's logic, and a calibration that
# moves THRESHOLD should not make them fail.
GATE = si_index.THRESHOLD
ESCALATE = si_index.ESCALATION_THRESHOLD
OVER_GATE = GATE + 3.0
UNDER_GATE = GATE - 3.0
OVER_ESCALATION = ESCALATE + 1.0


def fire(index, si=OVER_GATE, points=7):
    return {"trigger_cluster_shift": {index: True}, "si_total": {index: si},
            "base_points": {index: points}}


def test_gate_needs_both_the_shift_and_the_threshold():
    only_shift = frame(**{"trigger_cluster_shift": {5: True},
                          "si_total": {5: UNDER_GATE}})
    events, journal = cluster.run(only_shift)
    assert events == []
    assert journal["decision"].iloc[5] == "gate_below_threshold"

    only_score = frame(**{"si_total": {5: OVER_ESCALATION}})
    assert cluster.run(only_score)[0] == []


def test_event_is_created_when_both_hold():
    events, journal = cluster.run(frame(**fire(5)))
    assert len(events) == 1
    assert events[0].t0_utc == 6 * HOUR
    assert journal["decision"].iloc[5] == "event_created"


def test_cooldown_suppresses_a_repeat():
    data = frame(**fire(5))
    data.loc[data.index[10], ["trigger_cluster_shift", "si_total"]] = [True, OVER_GATE]
    events, journal = cluster.run(data)

    assert len(events) == 1
    assert journal["decision"].iloc[10] == "suppressed_by_cooldown"


def test_a_new_event_opens_after_the_cooldown_expires():
    later = 5 + windows.CLUSTER_COOLDOWN + 1
    data = frame(n=later + 10, **fire(5))
    data.loc[data.index[later], ["trigger_cluster_shift", "si_total"]] = [True, OVER_GATE]
    events, _ = cluster.run(data)

    assert len(events) == 2
    assert events[0].status == "finished"


def test_higher_order_shock_escalates_without_a_new_event():
    data = frame(**fire(5))
    data.loc[data.index[20], "si_total"] = OVER_ESCALATION
    events, journal = cluster.run(data)

    assert len(events) == 1
    assert events[0].escalation_seq == 1
    assert journal["decision"].iloc[20] == "escalation"
    assert events[0].escalations[0]["reason"] == "si>=escalation_threshold"


def test_escalation_restarts_the_cooldown():
    data = frame(n=200, **fire(5))
    data.loc[data.index[20], "si_total"] = OVER_ESCALATION
    # The hour right after the original cooldown would have expired.
    data.loc[data.index[5 + windows.CLUSTER_COOLDOWN + 1],
             ["trigger_cluster_shift", "si_total"]] = [True, OVER_GATE]
    events, journal = cluster.run(data)

    # The cooldown restarted from the escalation, so there is no new event.
    assert len(events) == 1
    assert journal["decision"].iloc[5 + windows.CLUSTER_COOLDOWN + 1] == "suppressed_by_cooldown"


def test_breadth_alone_can_escalate_below_the_threshold():
    # §4.5: on the breadth scale an emergency alert is possible even below the SI threshold.
    data = frame(**fire(5))
    data.loc[data.index[20], "breadth_q99"] = True
    data.loc[data.index[20], "si_total"] = UNDER_GATE
    events, _ = cluster.run(data)

    assert events[0].escalation_seq == 1
    assert events[0].escalations[0]["reason"] == "breadth_q99"


def test_higher_order_shock_is_forbidden_in_the_first_hour():
    data = frame(**fire(5))
    data.loc[data.index[5], "breadth_q99"] = True
    events, _ = cluster.run(data)
    assert events[0].escalation_seq == 0


def test_vector_reversal_opens_a_child_event():
    data = frame(**fire(5))
    data.loc[data.index[5], "m_weighted_median"] = -0.05    # M at T0 deep down
    data.loc[data.index[20], ["trigger_cluster_shift", "si_total"]] = [True, OVER_GATE]
    data.loc[data.index[20], "m_weighted_median"] = 0.05    # and it flipped upward
    events, journal = cluster.run(data)

    assert len(events) == 2
    assert events[1].parent_event_id == events[0].event_id
    assert journal["decision"].iloc[20] == "reversal_event"


def test_reversal_needs_the_gate_too():
    # A sign change without the gate is only logged.
    data = frame(**fire(5))
    data.loc[data.index[5], "m_weighted_median"] = -0.05
    data.loc[data.index[20], "m_weighted_median"] = 0.05
    events, _ = cluster.run(data)
    assert len(events) == 1


def test_reversal_is_not_allowed_before_the_delay():
    data = frame(**fire(5))
    data.loc[data.index[5], "m_weighted_median"] = -0.05
    early = 5 + windows.REVERSAL_DELAY - 1
    data.loc[data.index[early], ["trigger_cluster_shift", "si_total"]] = [True, OVER_GATE]
    data.loc[data.index[early], "m_weighted_median"] = 0.05
    events, _ = cluster.run(data)
    assert len(events) == 1


def test_higher_order_shock_wins_when_both_branches_fire():
    # The priority is stated outright: the reversal is not considered that hour.
    # The difference matters - a shock extends the event, a reversal opens a new one.
    data = frame(**fire(5))
    data.loc[data.index[5], "m_weighted_median"] = -0.05
    data.loc[data.index[20], ["trigger_cluster_shift", "si_total"]] = [
        True, OVER_ESCALATION]
    data.loc[data.index[20], "m_weighted_median"] = 0.05
    events, journal = cluster.run(data)

    assert len(events) == 1
    assert events[0].escalation_seq == 1
    assert journal["decision"].iloc[20] == "escalation"


def test_debounce_limits_early_breaks_to_two_per_window():
    data = frame(**fire(5))
    for position in (10, 15, 20):
        data.loc[data.index[position], "si_total"] = OVER_ESCALATION
    events, journal = cluster.run(data)

    assert events[0].escalation_seq == cluster.MAX_EARLY_BREAKS
    assert journal["decision"].iloc[20] == "branch_a_debounced"


def test_debounce_frees_up_after_the_window_passes():
    data = frame(n=300, **fire(5))
    for position in (10, 15, 15 + windows.ESCALATION_DEBOUNCE + 1):
        data.loc[data.index[position], "si_total"] = OVER_ESCALATION
    events, _ = cluster.run(data)
    assert events[0].escalation_seq == 3


def test_hours_without_quorum_are_skipped():
    data = frame(**fire(5))
    data.loc[data.index[5], "quorum_ok"] = False
    events, journal = cluster.run(data)

    assert events == []
    assert journal["decision"].iloc[5] == "no_quorum"


def saed_rows(*hours):
    return pd.DataFrame({"event_id": [f"e{h}" for h in hours],
                         "asset_id": ["twelvedata:SPY"] * len(hours),
                         "block": ["equity"] * len(hours),
                         "hour_utc": list(hours)})


def test_overlap_is_true_inside_the_cooldown_and_false_outside():
    events, _ = cluster.run(frame(**fire(5)))
    t0 = events[0].t0_utc
    end = events[0].cooldown_until_utc

    tagged = cluster.tag_overlap(saed_rows(t0 - HOUR, t0, t0 + HOUR, end), events)

    assert list(tagged["overlap_with_cluster"]) == [False, True, True, False]


def test_overlap_covers_hours_the_reference_calendar_does_not_have():
    # The automaton only ever walks reference-calendar hours, so a weekend is a
    # gap in its index. A crypto asset trades through that gap, and a cluster
    # event opened before it is still active inside it - the span is compared in
    # wall clock precisely so that such an hour is not silently missed.
    weekend = frame(**fire(5))
    gap_start = weekend.index[10]
    weekend = weekend.drop(index=weekend.index[10:58])   # 48 hours with no bar

    events, _ = cluster.run(weekend)

    assert gap_start not in weekend.index
    tagged = cluster.tag_overlap(saed_rows(gap_start), events)
    assert bool(tagged["overlap_with_cluster"].iloc[0]) is True


def test_an_event_still_open_at_the_end_of_history_has_no_end():
    events, _ = cluster.run(frame(n=20, **fire(15)))
    assert events[0].cooldown_until_utc is None

    far = cluster.tag_overlap(saed_rows(events[0].t0_utc + 10_000 * HOUR), events)

    assert bool(far["overlap_with_cluster"].iloc[0]) is True


def test_a_reversal_closes_the_parents_span_early():
    n = 400
    reversal = frame(n=n, **fire(5))
    for i in range(n):
        reversal.iloc[i, reversal.columns.get_loc("m_weighted_median")] = -0.05
    reversal.iloc[20, reversal.columns.get_loc("m_weighted_median")] = 0.05
    reversal.iloc[20, reversal.columns.get_loc("trigger_cluster_shift")] = True
    reversal.iloc[20, reversal.columns.get_loc("si_total")] = OVER_GATE
    reversal.iloc[20, reversal.columns.get_loc("base_points")] = 7

    events, _ = cluster.run(reversal)

    assert len(events) == 2 and events[1].parent_event_id == events[0].event_id
    # The parent stops being active where the child begins, not 72 hours later.
    assert events[0].cooldown_until_utc == events[1].t0_utc


def test_tagging_an_empty_table_yields_a_boolean_column():
    events, _ = cluster.run(frame(**fire(5)))
    tagged = cluster.tag_overlap(saed_rows(), events)
    assert tagged["overlap_with_cluster"].dtype.name == "boolean"

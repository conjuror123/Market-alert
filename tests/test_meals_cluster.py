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


def fire(index, si=10.0, points=7):
    return {"trigger_cluster_shift": {index: True}, "si_total": {index: si},
            "base_points": {index: points}}


def test_gate_needs_both_the_shift_and_the_threshold():
    only_shift = frame(**{"trigger_cluster_shift": {5: True}, "si_total": {5: 3.0}})
    events, journal = cluster.run(only_shift)
    assert events == []
    assert journal["decision"].iloc[5] == "gate_below_threshold"

    only_score = frame(**{"si_total": {5: 20.0}})
    assert cluster.run(only_score)[0] == []


def test_event_is_created_when_both_hold():
    events, journal = cluster.run(frame(**fire(5)))
    assert len(events) == 1
    assert events[0].t0_utc == 6 * HOUR
    assert journal["decision"].iloc[5] == "event_created"


def test_cooldown_suppresses_a_repeat():
    data = frame(**fire(5))
    data.loc[data.index[10], ["trigger_cluster_shift", "si_total"]] = [True, 10.0]
    events, journal = cluster.run(data)

    assert len(events) == 1
    assert journal["decision"].iloc[10] == "suppressed_by_cooldown"


def test_a_new_event_opens_after_the_cooldown_expires():
    later = 5 + windows.CLUSTER_COOLDOWN + 1
    data = frame(n=later + 10, **fire(5))
    data.loc[data.index[later], ["trigger_cluster_shift", "si_total"]] = [True, 10.0]
    events, _ = cluster.run(data)

    assert len(events) == 2
    assert events[0].status == "finished"


def test_higher_order_shock_escalates_without_a_new_event():
    data = frame(**fire(5))
    data.loc[data.index[20], "si_total"] = si_index.ESCALATION_THRESHOLD + 1
    events, journal = cluster.run(data)

    assert len(events) == 1
    assert events[0].escalation_seq == 1
    assert journal["decision"].iloc[20] == "escalation"
    assert events[0].escalations[0]["reason"] == "si>=escalation_threshold"


def test_escalation_restarts_the_cooldown():
    data = frame(n=200, **fire(5))
    data.loc[data.index[20], "si_total"] = si_index.ESCALATION_THRESHOLD + 1
    # The hour right after the original cooldown would have expired.
    data.loc[data.index[5 + windows.CLUSTER_COOLDOWN + 1],
             ["trigger_cluster_shift", "si_total"]] = [True, 10.0]
    events, journal = cluster.run(data)

    # The cooldown restarted from the escalation, so there is no new event.
    assert len(events) == 1
    assert journal["decision"].iloc[5 + windows.CLUSTER_COOLDOWN + 1] == "suppressed_by_cooldown"


def test_breadth_alone_can_escalate_below_the_threshold():
    # §4.5: on the breadth scale an emergency alert is possible even below the SI threshold.
    data = frame(**fire(5))
    data.loc[data.index[20], "breadth_q99"] = True
    data.loc[data.index[20], "si_total"] = 2.0
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
    data.loc[data.index[20], ["trigger_cluster_shift", "si_total"]] = [True, 10.0]
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
    data.loc[data.index[early], ["trigger_cluster_shift", "si_total"]] = [True, 10.0]
    data.loc[data.index[early], "m_weighted_median"] = 0.05
    events, _ = cluster.run(data)
    assert len(events) == 1


def test_higher_order_shock_wins_when_both_branches_fire():
    # The priority is stated outright: the reversal is not considered that hour.
    # The difference matters - a shock extends the event, a reversal opens a new one.
    data = frame(**fire(5))
    data.loc[data.index[5], "m_weighted_median"] = -0.05
    data.loc[data.index[20], ["trigger_cluster_shift", "si_total"]] = [
        True, si_index.ESCALATION_THRESHOLD + 1]
    data.loc[data.index[20], "m_weighted_median"] = 0.05
    events, journal = cluster.run(data)

    assert len(events) == 1
    assert events[0].escalation_seq == 1
    assert journal["decision"].iloc[20] == "escalation"


def test_debounce_limits_early_breaks_to_two_per_window():
    data = frame(**fire(5))
    for position in (10, 15, 20):
        data.loc[data.index[position], "si_total"] = si_index.ESCALATION_THRESHOLD + 1
    events, journal = cluster.run(data)

    assert events[0].escalation_seq == cluster.MAX_EARLY_BREAKS
    assert journal["decision"].iloc[20] == "branch_a_debounced"


def test_debounce_frees_up_after_the_window_passes():
    data = frame(n=300, **fire(5))
    for position in (10, 15, 15 + windows.ESCALATION_DEBOUNCE + 1):
        data.loc[data.index[position], "si_total"] = si_index.ESCALATION_THRESHOLD + 1
    events, _ = cluster.run(data)
    assert events[0].escalation_seq == 3


def test_hours_without_quorum_are_skipped():
    data = frame(**fire(5))
    data.loc[data.index[5], "quorum_ok"] = False
    events, journal = cluster.run(data)

    assert events == []
    assert journal["decision"].iloc[5] == "no_quorum"

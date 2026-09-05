import numpy as np
import pandas as pd
import pytest

from meals import saed, severity
from meals.basket import Asset

HOUR = 3600


def asset(**over):
    base = dict(ticker="SPY", source="twelvedata", tier=1, block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="1h", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def scored(hits, n=40, sigma=0.01, tier="routine"):
    """A series where the trigger condition holds at positions `hits`, quiet elsewhere.

    The score the trigger reads is z_resid_bmp - the residual standardised against
    its peers in the same hour - so that is what the hits are placed in. The tier
    column is set directly rather than fitted: severity.rolling_levels needs two
    years of bars before it will say anything, and these fixtures are forty bars
    long. The levels are still filled in, because triggers() reads them to tell
    "quieter than routine" apart from "nothing fitted yet".

    `hits` may be a list of positions, or a dict of position -> tier name when a
    test needs the tiers to differ.
    """
    placed = hits if isinstance(hits, dict) else {i: tier for i in hits}
    z = [0.5] * n
    e = [0.001] * n
    tiers = [pd.NA] * n
    for i, name in placed.items():
        z[i] = 10.0
        e[i] = 0.05
        tiers[i] = name
    frame = pd.DataFrame({
        "hour_utc": [(i + 1) * HOUR for i in range(n)],
        "z_resid": z, "z_resid_bmp": z, "e_resid": e,
        "q99_resid": [3.0] * n,
        "sigma_lt_resid": [sigma] * n,
        "r": [0.01] * n,
        "beta": [1.0] * n,
        "tier": pd.array(tiers, dtype="string"),
    })
    for rank, name in enumerate(severity.TIERS):
        frame[f"level_{name}"] = 5.0 + rank
    return frame


def test_trigger_fires_on_the_tier_not_on_the_size_of_the_move():
    # The condition is "rarer than this instrument's once-a-fortnight level", and
    # nothing else is a second hurdle. That was already true of the critical
    # value it replaces, for the reason the event-study literature gives - the
    # standardisation is the test - and the raw-magnitude leg removed before it
    # passed 139.9x more often in the loudest hours than the quietest.
    frame = scored({0: "routine"})
    frame.loc[0, "e_resid"] = 0.000001    # a tiny raw move, and it still counts
    frame.loc[1, "e_resid"] = 10.0        # a huge raw move with no tier, and it does not

    out = saed.triggers(frame)
    assert bool(out.iloc[0])
    assert not bool(out.iloc[1])


def test_trigger_fires_in_both_directions():
    frame = scored({0: "routine", 1: "major"})
    frame.loc[0, "z_resid_bmp"] = -10.0
    out = saed.triggers(frame)
    assert bool(out.iloc[0]) and bool(out.iloc[1])


def test_trigger_is_null_where_the_score_is_unknown():
    # §1.2: an unassessed hour is NULL, not False. Without enough assets in
    # session there is no peer spread to standardise against.
    frame = scored([0])
    frame.loc[0, "z_resid_bmp"] = np.nan
    assert pd.isna(saed.triggers(frame).iloc[0])


def test_trigger_is_null_before_the_levels_are_fitted():
    # A quiet bar and a bar during severity's warm-up both carry no tier, and
    # they are not the same claim: the first says the move was ordinary, the
    # second says nothing was measured. The evaluation harness asks the
    # difference when it wants to know why an instrument was silent in 2015.
    frame = scored([])
    frame[f"level_{severity.TIERS[0]}"] = np.nan
    assert saed.triggers(frame).isna().all()


def test_trigger_falls_back_to_the_raw_score_without_a_peer_spread():
    # An asset with no cross-section to compare against - a lone instrument in a
    # backtest - is still assessed rather than silently dropped.
    frame = scored([0]).drop(columns=["z_resid_bmp"])
    assert bool(saed.triggers(frame).iloc[0])


def test_an_hour_is_assessed_if_either_ladder_could_speak():
    # One channel still warming up does not make the hour unassessed - the
    # other one answered. Only an hour where neither could speak is NULL.
    frame = scored([0])
    frame[f"level_{severity.TIERS[0]}"] = np.nan     # the abnormal ladder is blank
    frame["abs_level_routine"] = 0.01                # the absolute one is not
    assert bool(saed.triggers(frame).iloc[0])


def test_an_hour_is_null_only_when_neither_ladder_could_speak():
    frame = scored([0])
    frame[f"level_{severity.TIERS[0]}"] = np.nan
    assert saed.triggers(frame).isna().all()


def test_the_event_records_which_question_it_answered():
    # "Gold moved and nothing else did" and "everything moved, gold included"
    # read as entirely different news, so the basis travels with the event.
    frame = scored({5: "major"})
    frame["basis"] = pd.array([pd.NA] * len(frame), dtype="string")
    frame.loc[5, "basis"] = "absolute"
    assert saed.build_events(asset(), frame)[0].basis == "absolute"


def test_trigger_needs_severity_to_have_run():
    with pytest.raises(KeyError):
        saed.triggers(scored([0]).drop(columns=["tier"]))


def test_cooldown_folds_repeats_into_one_event():
    # §8.3: repeat firings inside the pause create no events but are logged as a
    # continuation of the current one.
    events = saed.build_events(asset(), scored([5, 8, 10]), cooldown_bars=12)

    assert len(events) == 1
    assert events[0].hour_utc == 6 * HOUR
    assert events[0].repeat_count == 2


def test_the_event_keeps_the_worst_tier_it_reached_inside_the_pause():
    # A move that opens routine and turns major an hour later is a major event.
    # Reporting the tier it happened to open at would understate it purely
    # because of when the automaton opened.
    events = saed.build_events(asset(), scored({5: "routine", 7: "major"}),
                               cooldown_bars=12)
    assert len(events) == 1
    assert events[0].tier == "major"
    assert events[0].repeat_count == 1


def test_the_event_is_not_downgraded_by_a_milder_repeat():
    events = saed.build_events(asset(), scored({5: "major", 7: "routine"}),
                               cooldown_bars=12)
    assert len(events) == 1 and events[0].tier == "major"


def test_a_new_event_opens_after_the_cooldown():
    events = saed.build_events(asset(), scored([5, 20]), cooldown_bars=12)

    assert len(events) == 2
    assert [e.repeat_count for e in events] == [0, 0]


def test_cooldown_is_counted_in_asset_bars_not_calendar_hours():
    # Twelve bars are one and a half sessions for an ETF and half a day for
    # crypto. Were the pause counted in calendar hours, an ETF would stay silent
    # for nearly two days where a round-the-clock instrument recovers in twelve.
    sparse = scored([0, 13])
    # The bars are a day apart, but only 13 of the asset's own bars separate them.
    sparse["hour_utc"] = [(i + 1) * HOUR * 24 for i in range(len(sparse))]

    events = saed.build_events(asset(), sparse, cooldown_bars=12)
    assert len(events) == 2


def test_no_events_without_triggers():
    assert saed.build_events(asset(), scored([])) == []


def test_block_alert_aggregates_the_same_hour():
    # §8.4: simultaneous events of assets in one block are one observation about
    # the block, not three identical messages.
    events = pd.DataFrame({
        "event_id": ["a", "b", "c"],
        "asset_id": ["twelvedata:SPY", "twelvedata:QQQ", "twelvedata:TLT"],
        "block": ["equity", "equity", "rates"],
        "hour_utc": [HOUR, HOUR, HOUR],
        "z_resid": [5.0, -8.0, 4.0],
        "e_resid": [0.05, -0.06, 0.04],
        "r": [0.01, 0.02, 0.03], "beta": [1.0, 1.0, 1.0], "repeat_count": [0, 0, 0],
        "tier": ["routine", "major", "notable"],
        "channel": ["digest", "push", "dropped"],
    })
    alerts = saed.aggregate_block_alerts(events)

    equity = alerts[alerts["block"] == "equity"].iloc[0]
    assert equity["n_assets"] == 2
    assert equity["max_abs_z_resid"] == 8.0
    assert "twelvedata:QQQ" in equity["assets"]
    # The block is delivered at the severity of its worst member, not its
    # first, and on its most urgent member's channel.
    assert equity["tier"] == "major"
    assert equity["channel"] == "push"
    assert len(alerts) == 2   # equity and rates are different alerts


def test_different_hours_are_different_alerts():
    events = pd.DataFrame({
        "event_id": ["a", "b"], "asset_id": ["x", "y"], "block": ["FX", "FX"],
        "hour_utc": [HOUR, 2 * HOUR], "z_resid": [5.0, 6.0],
        "e_resid": [0.05, 0.06], "r": [0.01, 0.01], "beta": [1.0, 1.0],
        "repeat_count": [0, 0], "tier": ["routine", "routine"],
    })
    assert len(saed.aggregate_block_alerts(events)) == 2


def test_events_link_back_to_their_alert():
    events = pd.DataFrame({
        "event_id": ["a", "b"], "asset_id": ["x", "y"], "block": ["FX", "FX"],
        "hour_utc": [HOUR, HOUR], "z_resid": [5.0, 6.0], "e_resid": [0.05, 0.06],
        "r": [0.01, 0.01], "beta": [1.0, 1.0], "repeat_count": [0, 0],
        "tier": ["routine", "routine"],
    })
    alerts = saed.aggregate_block_alerts(events)
    linked = saed.link_alerts(events, alerts)

    assert linked["aggregate_alert_id"].nunique() == 1
    assert linked["aggregate_alert_id"].iloc[0] == alerts["alert_id"].iloc[0]


def test_empty_inputs_keep_the_schema():
    empty = saed.events_frame([])
    alerts = saed.aggregate_block_alerts(empty)
    assert empty.empty and alerts.empty
    assert "repeat_count" in empty.columns
    assert "max_abs_z_resid" in alerts.columns


def test_overlap_starts_out_null_rather_than_false():
    # §1.2: NULL is not False. SAED runs before the cluster automaton, so at this
    # point "was there an active cluster event" is unanswered, not answered "no".
    events = saed.events_frame([
        saed.SaedEvent(event_id="x", asset_id="a", block="FX", hour_utc=3600,
                       z_resid=4.0, e_resid=0.01, r=0.01, beta=1.0, repeat_count=0,
                       tier="routine", basis="abnormal")])

    tagged = saed.unevaluated_overlap(events)

    assert tagged["overlap_with_cluster"].dtype.name == "boolean"
    assert tagged["overlap_with_cluster"].isna().all()

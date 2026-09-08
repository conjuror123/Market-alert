import numpy as np
import pandas as pd
import pytest

from tremor import saed, severity
from tremor.basket import Asset

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
                       peak_hour_utc=3600, rank_confirms=None, ou_reverts=None,
                       z_resid=4.0, e_resid=0.01, r=0.01, beta=1.0, repeat_count=0,
                       tier="routine", basis="abnormal", sigma_lt=0.002)])

    tagged = saed.unevaluated_overlap(events)

    assert tagged["overlap_with_cluster"].dtype.name == "boolean"
    assert tagged["overlap_with_cluster"].isna().all()


def test_an_escalated_event_reports_the_move_that_earned_its_tier():
    # The tier comes from the peak bar, so the magnitude beside it must too.
    # Reporting the opening bar's move next to the peak bar's tier describes
    # two different hours as one event, and the delivery layer prints them
    # together - it produced pushes reading "biggest move in 3 years, +0.01%".
    frame = scored({5: "routine", 7: "extreme"})
    frame.loc[5, ["r", "z_resid", "z_resid_bmp", "e_resid"]] = [0.00005, 3.0, 3.0, 0.00002]
    frame.loc[7, ["r", "z_resid", "z_resid_bmp", "e_resid"]] = [0.013, 30.0, 30.0, 0.02]

    events = saed.build_events(asset(), frame, cooldown_bars=12)

    assert len(events) == 1
    event = events[0]
    assert event.tier == "extreme"
    assert event.hour_utc == 6 * HOUR            # identity stays at the opening
    assert event.peak_hour_utc == 8 * HOUR       # description follows the peak
    assert event.r == pytest.approx(0.013)
    assert event.z_resid == pytest.approx(30.0)


def test_an_event_that_opens_at_the_top_tier_still_follows_its_biggest_bar():
    # extreme is the top of the ladder, so an event that opens there can never
    # escalate, and the "highest tier wins" rule alone leaves it describing
    # whichever bar the automaton happened to open on. On 2015-01-15 the Swiss
    # franc peg broke: USD/CHF was already extreme at -3.5% at 09:00 and moved
    # -10.5% at 10:00, and the alert quoted the first of the two.
    frame = scored({5: "extreme", 7: "extreme"})
    frame.loc[5, ["r", "z_resid", "z_resid_bmp", "e_resid"]] = [-0.035, -62.0, -62.0, -0.036]
    frame.loc[7, ["r", "z_resid", "z_resid_bmp", "e_resid"]] = [-0.105, -153.0, -153.0, -0.105]

    events = saed.build_events(asset(), frame, cooldown_bars=12)

    assert len(events) == 1
    assert events[0].tier == "extreme"
    assert events[0].hour_utc == 6 * HOUR            # identity stays at the opening
    assert events[0].peak_hour_utc == 8 * HOUR       # description follows the peak
    assert events[0].r == pytest.approx(-0.105)


def test_a_smaller_bar_at_the_same_tier_does_not_move_the_peak():
    # The tie-break is one-directional, like the tier rule it extends: an event
    # keeps the worst bar it has seen, so a second extreme half the size of the
    # first leaves the description alone.
    frame = scored({5: "extreme", 7: "extreme"})
    frame.loc[5, ["r", "z_resid", "z_resid_bmp", "e_resid"]] = [-0.105, -153.0, -153.0, -0.105]
    frame.loc[7, ["r", "z_resid", "z_resid_bmp", "e_resid"]] = [-0.035, -62.0, -62.0, -0.036]

    events = saed.build_events(asset(), frame, cooldown_bars=12)

    assert len(events) == 1
    assert events[0].peak_hour_utc == 6 * HOUR
    assert events[0].r == pytest.approx(-0.105)


def test_an_event_that_never_escalates_peaks_where_it_opened():
    events = saed.build_events(asset(), scored({5: "major", 7: "routine"}),
                               cooldown_bars=12)
    assert len(events) == 1
    assert events[0].peak_hour_utc == events[0].hour_utc == 6 * HOUR


def test_retention_is_measured_from_the_peak_not_the_opening():
    # Retention divides the forward move by the move being retained. Taken off
    # an opening bar of a fifth of a basis point while the event is reported at
    # a later bar's tier, it returns ratios that describe the mismatch rather
    # than the market.
    from tremor import persistence

    frame = scored({5: "routine", 7: "extreme"})
    events = saed.events_frame(saed.build_events(asset(), frame, cooldown_bars=12))
    lookup = pd.DataFrame({"hour_utc": frame["hour_utc"]})
    for column in persistence.RETENTION_COLUMNS:
        lookup[column] = 0.0
    abnormal = [f"retention_{h}" for h in persistence.HORIZONS]
    lookup.loc[5, abnormal] = 99.0    # the opening bar
    lookup.loc[7, abnormal] = 0.5     # the peak bar
    out = persistence.attach(events, {asset().asset_id: lookup})

    assert out["retention_24"].iloc[0] == pytest.approx(0.5)


def test_a_move_smaller_than_the_instrument_can_resolve_is_not_an_event():
    # A one-cent move on a hundred-dollar fund is the smallest change the
    # price can express. It is not a small event; it is an unobserved one, and
    # it is how a one-tick move came to be reported as the biggest in 3 years.
    frame = scored([5])
    frame["close"] = 100.0
    frame.loc[5, "r"] = 0.0001            # one cent on 100 dollars = 1 tick
    assert saed.build_events(asset(), frame, cooldown_bars=12) == []


def test_two_ticks_is_enough_to_be_an_event():
    # Two ticks is the smallest OBSERVED change that guarantees the true move
    # exceeded one tick, which is why the threshold is two.
    frame = scored([5])
    frame["close"] = 100.0
    frame.loc[5, "r"] = 0.0002
    assert len(saed.build_events(asset(), frame, cooldown_bars=12)) == 1


def test_the_gate_scales_with_the_price_not_with_a_fixed_percentage():
    # A tick is a fixed number of cents, so the SAME percentage move is two
    # basis points either way and yet is eight ticks on a $400 fund and four
    # tenths of a tick on a $20 one. A percentage threshold could not express
    # that; this is why the gate is stated in ticks.
    dear = scored([5]);  dear["close"] = 400.0;  dear.loc[5, "r"] = 0.0002
    cheap = scored([5]); cheap["close"] = 20.0;  cheap.loc[5, "r"] = 0.0002
    assert len(saed.build_events(asset(), dear, cooldown_bars=12)) == 1
    assert saed.build_events(asset(), cheap, cooldown_bars=12) == []


def test_an_instrument_without_a_price_column_is_not_gated():
    # FX and crypto frames reach here the same way; nothing should silently
    # vanish because a column is absent.
    frame = scored([5]).drop(columns=["close"], errors="ignore")
    assert len(saed.build_events(asset(), frame, cooldown_bars=12)) == 1


def _rank_frame(basis, confirms, tier="major"):
    n = len(basis)
    frame = pd.DataFrame({
        "hour_utc": [(i + 1) * HOUR for i in range(n)],
        "tier_abnormal": pd.array([tier] * n, dtype="string"),
        "tier_absolute": pd.array([pd.NA] * n, dtype="string"),
        "basis": pd.array(basis, dtype="string"),
        "rank_confirms": pd.array(confirms, dtype="boolean"),
    })
    return frame


def _combined(frame):
    """As the pipeline hands it over: combine() has already set tier and basis."""
    return severity.combine(frame, saed.TIER_SOURCES)


def test_an_abnormal_only_hour_the_ranks_contradict_is_withdrawn():
    # The abnormal channel is a t-statistic - "large relative to the peers this
    # hour" - so an asleep block makes six basis points look extreme. The rank
    # test shares none of that machinery and never estimates a variance, which
    # is the quantity thin trading corrupts.
    out = saed.withdraw_unconfirmed(_combined(
        _rank_frame(["abnormal"] * 4, [True, False, None, True])))
    assert list(out["tier"].isna()) == [False, True, False, False]


def test_the_ranks_do_not_withdraw_an_hour_the_absolute_channel_also_claimed():
    # If the price itself moved, the hour was never abnormal-only and the whole
    # objection does not apply - and this is what keeps the rule safe in a
    # crisis, when the rank test is itself misspecified.
    frame = _rank_frame(["both"] * 2, [False, False])
    frame["tier_absolute"] = pd.array(["extreme"] * 2, dtype="string")
    out = saed.withdraw_unconfirmed(_combined(frame))
    assert not out["tier"].isna().any()
    assert list(out["tier"]) == ["extreme", "extreme"]


def test_a_rank_window_that_has_not_filled_has_not_disagreed():
    # pandas NA is not False. An hour whose rank window is still warming up has
    # said nothing, and silence is not a contradiction.
    out = saed.withdraw_unconfirmed(_combined(
        _rank_frame(["abnormal"] * 3, [None, None, None])))
    assert not out["tier"].isna().any()

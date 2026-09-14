from datetime import datetime, timezone

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


def scored(hits, n=40, sigma=0.01, tier="noticeable"):
    """A series where the trigger condition holds at positions `hits`, quiet elsewhere.

    The score the trigger reads is z_resid_bmp - the residual standardised against
    its peers in the same hour - so that is what the hits are placed in. The tier
    column is set directly rather than fitted: severity.rolling_levels needs two
    years of bars before it will say anything, and these fixtures are forty bars
    long. The levels are still filled in, because triggers() reads them to tell
    "quieter than noticeable" apart from "nothing fitted yet".

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
    frame = scored({0: "noticeable"})
    frame.loc[0, "e_resid"] = 0.000001    # a tiny raw move, and it still counts
    frame.loc[1, "e_resid"] = 10.0        # a huge raw move with no tier, and it does not

    out = saed.triggers(frame)
    assert bool(out.iloc[0])
    assert not bool(out.iloc[1])


def test_trigger_fires_in_both_directions():
    frame = scored({0: "noticeable", 1: "major"})
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
    frame["abs_level_noticeable"] = 0.01                # the absolute one is not
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


def test_every_later_firing_that_day_joins_the_open_event():
    # §8.3: repeat firings inside the instrument's day create no events but are
    # logged as a continuation of the current one.
    events = saed.build_events(asset(), scored([5, 8, 10]))

    assert len(events) == 1
    assert events[0].hour_utc == 6 * HOUR
    assert events[0].repeat_count == 2


def test_the_event_keeps_the_worst_tier_it_reached_that_day():
    # A move that opens noticeable and turns major an hour later is a major event.
    # Reporting the tier it happened to open at would understate it purely
    # because of when the automaton opened.
    events = saed.build_events(asset(), scored({5: "noticeable", 7: "major"}))
    assert len(events) == 1
    assert events[0].tier == "major"
    assert events[0].repeat_count == 1


def test_the_event_is_not_downgraded_by_a_milder_repeat():
    events = saed.build_events(asset(), scored({5: "major", 7: "noticeable"}))
    assert len(events) == 1 and events[0].tier == "major"


def test_a_new_event_opens_when_the_day_turns():
    # Fifteen bars apart used to be two events and four apart used to be one.
    # Neither is the rule any more: what separates these two is a date.
    events = saed.build_events(asset(), scored([5, 30]))

    assert len(events) == 2
    assert [e.repeat_count for e in events] == [0, 0]


def test_a_second_firing_later_the_same_day_is_not_a_second_event():
    # Fifteen bars apart, which the twelve-bar pause let through as a fresh
    # alert. It is the same session, so it is the same event.
    events = saed.build_events(asset(), scored([5, 20]))

    assert len(events) == 1
    assert events[0].repeat_count == 1


def test_the_day_that_matters_is_the_instruments_own():
    # 22:00 and 02:00 UTC are four hours apart and sit either side of midnight.
    # For a fund listed in New York they are 17:00 and 21:00 of ONE session and
    # the second is not news; for a coin, whose day is the clock's, they are two
    # days and the second is. A single calendar rule could not say both.
    frame = scored([2, 6], n=12)
    base = int(datetime(2021, 3, 10, 20, tzinfo=timezone.utc).timestamp())
    frame["hour_utc"] = [base + i * HOUR for i in range(len(frame))]

    assert len(saed.build_events(asset(), frame)) == 1
    assert len(saed.build_events(asset(session_template="crypto_24_7"), frame)) == 2


def test_a_currency_pair_takes_the_utc_day_like_a_coin():
    # Not the FX convention's 17:00 New York roll, and sessions.day_tz says at
    # length why. Pinned because the convention is the obvious thing to reach
    # for and the measurement that rejected it is not in the diff.
    frame = scored([3, 7], n=12)
    base = int(datetime(2016, 6, 23, 18, tzinfo=timezone.utc).timestamp())
    frame["hour_utc"] = [base + i * HOUR for i in range(len(frame))]

    pair = saed.build_events(asset(session_template="fx_continuous"), frame)
    coin = saed.build_events(asset(session_template="crypto_24_7"), frame)
    assert len(pair) == len(coin) == 2


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
        "tier": ["noticeable", "major", "high"],
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
        "repeat_count": [0, 0], "tier": ["noticeable", "noticeable"],
    })
    assert len(saed.aggregate_block_alerts(events)) == 2


def test_events_link_back_to_their_alert():
    events = pd.DataFrame({
        "event_id": ["a", "b"], "asset_id": ["x", "y"], "block": ["FX", "FX"],
        "hour_utc": [HOUR, HOUR], "z_resid": [5.0, 6.0], "e_resid": [0.05, 0.06],
        "r": [0.01, 0.01], "beta": [1.0, 1.0], "repeat_count": [0, 0],
        "tier": ["noticeable", "noticeable"],
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


def test_an_escalated_event_reports_the_move_that_earned_its_tier():
    # The tier comes from the peak bar, so the magnitude beside it must too.
    # Reporting the opening bar's move next to the peak bar's tier describes
    # two different hours as one event, and the delivery layer prints them
    # together - it produced pushes reading "biggest move in 3 years, +0.01%".
    frame = scored({5: "noticeable", 7: "extreme"})
    frame.loc[5, ["r", "z_resid", "z_resid_bmp", "e_resid"]] = [0.00005, 3.0, 3.0, 0.00002]
    frame.loc[7, ["r", "z_resid", "z_resid_bmp", "e_resid"]] = [0.013, 30.0, 30.0, 0.02]

    events = saed.build_events(asset(), frame)

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

    events = saed.build_events(asset(), frame)

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

    events = saed.build_events(asset(), frame)

    assert len(events) == 1
    assert events[0].peak_hour_utc == 6 * HOUR
    assert events[0].r == pytest.approx(-0.105)


def test_an_event_that_never_escalates_peaks_where_it_opened():
    events = saed.build_events(asset(), scored({5: "major", 7: "noticeable"}))
    assert len(events) == 1
    assert events[0].peak_hour_utc == events[0].hour_utc == 6 * HOUR


def test_retention_is_measured_from_the_peak_not_the_opening():
    # Retention divides the forward move by the move being retained. Taken off
    # an opening bar of a fifth of a basis point while the event is reported at
    # a later bar's tier, it returns ratios that describe the mismatch rather
    # than the market.
    from tremor import persistence

    frame = scored({5: "noticeable", 7: "extreme"})
    events = saed.events_frame(saed.build_events(asset(), frame))
    lookup = pd.DataFrame({"hour_utc": frame["hour_utc"]})
    for column in persistence.RETENTION_COLUMNS:
        lookup[column] = 0.0
    abnormal = [f"retention_{h}" for h in persistence.HORIZONS]  # today, settled
    lookup.loc[5, abnormal] = 99.0    # the opening bar
    lookup.loc[7, abnormal] = 0.5     # the peak bar
    out = persistence.attach(events, {asset().asset_id: lookup})

    assert out["retention_settled"].iloc[0] == pytest.approx(0.5)


def test_a_move_smaller_than_the_instrument_can_resolve_is_not_an_event():
    # A one-cent move on a hundred-dollar fund is the smallest change the
    # price can express. It is not a small event; it is an unobserved one, and
    # it is how a one-tick move came to be reported as the biggest in 3 years.
    frame = scored([5])
    frame["close"] = 100.0
    frame.loc[5, "r"] = 0.0001            # one cent on 100 dollars = 1 tick
    assert saed.build_events(asset(), frame) == []


def test_two_ticks_is_enough_to_be_an_event():
    # Two ticks is the smallest OBSERVED change that guarantees the true move
    # exceeded one tick, which is why the threshold is two.
    frame = scored([5])
    frame["close"] = 100.0
    frame.loc[5, "r"] = 0.0002
    assert len(saed.build_events(asset(), frame)) == 1


def test_the_gate_scales_with_the_price_not_with_a_fixed_percentage():
    # A tick is a fixed number of cents, so the SAME percentage move is two
    # basis points either way and yet is eight ticks on a $400 fund and four
    # tenths of a tick on a $20 one. A percentage threshold could not express
    # that; this is why the gate is stated in ticks.
    dear = scored([5]);  dear["close"] = 400.0;  dear.loc[5, "r"] = 0.0002
    cheap = scored([5]); cheap["close"] = 20.0;  cheap.loc[5, "r"] = 0.0002
    assert len(saed.build_events(asset(), dear)) == 1
    assert saed.build_events(asset(), cheap) == []


def test_an_instrument_without_a_price_column_is_not_gated():
    # FX and crypto frames reach here the same way; nothing should silently
    # vanish because a column is absent.
    frame = scored([5]).drop(columns=["close"], errors="ignore")
    assert len(saed.build_events(asset(), frame)) == 1


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


def test_a_move_smaller_than_its_own_usual_hour_is_not_an_event():
    # The IEI case, found by a reader rather than by a test: +0.03% at half the
    # instrument's usual hour, reported as a once-a-month event because the
    # RESIDUAL was unusual while the move was not. The abnormal channel asks
    # whether a move was unexplained, never whether it was large.
    frame = pd.DataFrame({
        "hour_utc": [0, 3600, 7200],
        "r":        [0.0003, 0.0200, 0.0005],   # tiny, large, tiny
        "sigma_lt": [0.0060, 0.0060, 0.0060],   # its usual hour
        "z_resid_bmp": [9.0, 9.0, 9.0],         # all three look remarkable
        "level_noticeable": [1.0, 1.0, 1.0],
        "abs_level_noticeable": [1.0, 1.0, 1.0],
        "tier": ["high", "high", "high"],
    })
    fired = saed.triggers(frame).fillna(False).tolist()
    assert fired == [False, True, False]


def test_an_instrument_with_no_usual_hour_yet_is_not_filtered_out():
    # A bar whose sigma_LT has not been measured has not FAILED the size test,
    # it has not taken it - the floor must not silence a warming-up instrument.
    frame = pd.DataFrame({
        "hour_utc": [0, 3600],
        "r":        [0.0001, 0.0001],
        "sigma_lt": [np.nan, 0.0],
        "z_resid_bmp": [9.0, 9.0],
        "level_noticeable": [1.0, 1.0],
        "abs_level_noticeable": [1.0, 1.0],
        "tier": ["high", "high"],
    })
    assert saed.triggers(frame).fillna(False).tolist() == [True, True]


# --- the per-instrument size floor -------------------------------------------

def floored(tmp_path, monkeypatch, overrides, shared=1.0):
    """Point load_tuning at a config carrying per-instrument floors."""
    import yaml

    from tremor import basket as basket_module

    raw = {"anchor_exchange_tz": "America/New_York", "history_since": "2021-01-01",
           "session_templates": {"s": {"tz": "UTC"}},
           "volatility_index": {"series_id": "V", "source": "fred", "interval": "1d"},
           "min_move_sigma": shared,
           "assets": [{"ticker": t, "source": "twelvedata", "tier": 1,
                       "block": "equity", "has_volume": True, "tick_size": 0.01,
                       "session_template": "s", "fetch_interval": "1h",
                       "min_move_sigma": v} for t, v in overrides.items()]}
    path = tmp_path / "basket.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    basket_module.load_tuning.cache_clear()
    monkeypatch.setattr(basket_module, "DEFAULT_BASKET_PATH", str(path))
    monkeypatch.setattr(saed, "load_tuning",
                        lambda *a, **k: basket_module.load_tuning(str(path)))


def with_size(frame, asset_id, move, sigma):
    return frame.assign(asset_id=asset_id, r=move, sigma_lt=sigma)


def test_the_floor_silences_the_instrument_it_was_raised_on(tmp_path, monkeypatch):
    # Two instruments, the same move in their own terms, one floor raised. The
    # reader pointed at LOUD and must not lose QUIET with it.
    floored(tmp_path, monkeypatch, {"LOUD": 4.0, "QUIET": 1.0})
    # a move of 2x each instrument's usual hour
    loud = with_size(scored([5]), "twelvedata:LOUD", 0.02, 0.01)
    quiet = with_size(scored([5]), "twelvedata:QUIET", 0.02, 0.01)

    assert saed.triggers(loud).fillna(False).sum() == 0     # under its 4x floor
    assert saed.triggers(quiet).fillna(False).sum() == 1    # over its 1x floor


def test_an_instrument_with_no_override_uses_the_shared_floor(tmp_path, monkeypatch):
    floored(tmp_path, monkeypatch, {"LOUD": 4.0}, shared=3.0)
    other = with_size(scored([5]), "twelvedata:SOMETHING-ELSE", 0.02, 0.01)
    assert saed.triggers(other).fillna(False).sum() == 0    # 2x, under the shared 3x
    bigger = with_size(scored([5]), "twelvedata:SOMETHING-ELSE", 0.04, 0.01)
    assert saed.triggers(bigger).fillna(False).sum() == 1   # 4x, over it


def test_a_floor_of_zero_lets_everything_its_tier_allows_through(tmp_path, monkeypatch):
    floored(tmp_path, monkeypatch, {"OPEN": 0.0}, shared=5.0)
    tiny = with_size(scored([5]), "twelvedata:OPEN", 0.0001, 0.01)
    assert saed.triggers(tiny).fillna(False).sum() == 1


def test_a_frame_with_no_asset_id_falls_back_to_the_shared_floor(tmp_path, monkeypatch):
    # Older callers and fixtures hand over a frame with no asset_id. Reading
    # nothing at all there would silence them; guessing an instrument would be
    # worse.
    floored(tmp_path, monkeypatch, {"LOUD": 4.0}, shared=3.0)
    frame = scored([5]).assign(r=0.02, sigma_lt=0.01)
    assert "asset_id" not in frame
    assert saed.triggers(frame).fillna(False).sum() == 0    # 2x under the shared 3x

import numpy as np
import pandas as pd
import pytest

from meals import saed, windows
from meals.basket import Asset

HOUR = 3600


def asset(**over):
    base = dict(ticker="SPY", source="twelvedata", tier=1, block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="1h", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def scored(hits, n=40, sigma=0.01):
    """A series where the §8.2 condition holds at positions `hits`, quiet elsewhere."""
    z = [0.5] * n
    e = [0.001] * n
    for i in hits:
        z[i] = 10.0
        e[i] = 0.05
    return pd.DataFrame({
        "hour_utc": [(i + 1) * HOUR for i in range(n)],
        "z_resid": z, "e_resid": e,
        "q99_resid": [3.0] * n,
        "sigma_lt_resid": [sigma] * n,
        "r": [0.01] * n,
        "beta": [1.0] * n,
    })


def test_trigger_needs_both_legs():
    frame = scored([])
    frame.loc[0, "z_resid"] = 10.0      # the relative leg passed
    frame.loc[0, "e_resid"] = 0.001     # the absolute one did not
    frame.loc[1, "z_resid"] = 1.0
    frame.loc[1, "e_resid"] = 0.05      # absolute only
    frame.loc[2, "z_resid"] = 10.0
    frame.loc[2, "e_resid"] = 0.05      # both

    out = saed.triggers(frame)
    assert not bool(out.iloc[0])
    assert not bool(out.iloc[1])
    assert bool(out.iloc[2])


def test_trigger_is_null_when_thresholds_are_unknown():
    frame = scored([0])
    frame.loc[0, "q99_resid"] = np.nan
    assert pd.isna(saed.triggers(frame).iloc[0])


def test_cooldown_folds_repeats_into_one_event():
    # §8.3: repeat firings inside the pause create no events but are logged as a
    # continuation of the current one.
    events = saed.build_events(asset(), scored([5, 8, 10]), cooldown_bars=12)

    assert len(events) == 1
    assert events[0].hour_utc == 6 * HOUR
    assert events[0].repeat_count == 2


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
    })
    alerts = saed.aggregate_block_alerts(events)

    equity = alerts[alerts["block"] == "equity"].iloc[0]
    assert equity["n_assets"] == 2
    assert equity["max_abs_z_resid"] == 8.0
    assert "twelvedata:QQQ" in equity["assets"]
    assert len(alerts) == 2   # equity and rates are different alerts


def test_different_hours_are_different_alerts():
    events = pd.DataFrame({
        "event_id": ["a", "b"], "asset_id": ["x", "y"], "block": ["FX", "FX"],
        "hour_utc": [HOUR, 2 * HOUR], "z_resid": [5.0, 6.0],
        "e_resid": [0.05, 0.06], "r": [0.01, 0.01], "beta": [1.0, 1.0],
        "repeat_count": [0, 0],
    })
    assert len(saed.aggregate_block_alerts(events)) == 2


def test_events_link_back_to_their_alert():
    events = pd.DataFrame({
        "event_id": ["a", "b"], "asset_id": ["x", "y"], "block": ["FX", "FX"],
        "hour_utc": [HOUR, HOUR], "z_resid": [5.0, 6.0], "e_resid": [0.05, 0.06],
        "r": [0.01, 0.01], "beta": [1.0, 1.0], "repeat_count": [0, 0],
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

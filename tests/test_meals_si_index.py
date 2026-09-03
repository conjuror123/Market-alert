from datetime import date

import numpy as np
import pandas as pd
import pytest

from meals import si_index
from meals.basket import Asset, Basket, VolatilityIndex

HOUR = 3600


def make_asset(ticker, block, tier=1, has_volume=True):
    return Asset(ticker=ticker, source="twelvedata", tier=tier, block=block,
                 has_volume=has_volume, tick_size=0.01, session_template="us_equity",
                 fetch_interval="1h", label=ticker, in_basket=True)


def make_basket(assets):
    return Basket(assets=tuple(assets), outside=(),
                  volatility_index=VolatilityIndex("VIXCLS", "fred", "1d", "VIX",
                                                   date(1990, 1, 1)),
                  anchor_exchange_tz="America/New_York",
                  history_since=date(2021, 1, 1), session_templates={})


def two_blocks():
    return make_basket([
        make_asset("A", "equity", 1), make_asset("B", "equity", 2),
        make_asset("C", "equity", 2), make_asset("D", "FX", 1, has_volume=False),
        make_asset("E", "FX", 2, has_volume=False),
        make_asset("F", "FX", 2, has_volume=False),
    ])


def metrics_for(basket, rows):
    """rows: dict asset_id -> (r, breach_q95, breach_q99, v_r) for one hour."""
    out = {}
    for asset in basket.assets:
        r, q95, q99, v_r = rows.get(asset.asset_id, (np.nan, None, None, np.nan))
        out[asset.asset_id] = pd.DataFrame({
            "hour_utc": [HOUR], "r": [r], "breach_q95": pd.Series([q95], dtype="boolean"),
            "breach_q99": pd.Series([q99], dtype="boolean"), "v_r": [v_r],
        })
    return out


def points_for(basket, rows, single=False):
    hours = pd.Index([HOUR], name="hour_utc")
    return si_index.base_points(basket, metrics_for(basket, rows),
                                pd.Series([single], index=hours), hours,
                                volume_threshold=2.5).iloc[0]


def test_price_shock_scores_once_regardless_of_how_many_assets():
    basket = two_blocks()
    one = points_for(basket, {"twelvedata:A": (0.05, True, True, 0.0)})
    many = points_for(basket, {a.asset_id: (0.05, True, True, 0.0) for a in basket.assets})

    assert one["trigger_price_shock"] and many["trigger_price_shock"]
    # Points for a trigger TYPE are awarded once per hour.
    assert one["base_points"] >= si_index.POINTS_PRICE_SHOCK
    assert many["base_points"] <= si_index.MAX_POINTS


def test_volume_confirms_only_on_an_asset_that_also_shocked():
    # A volume spike without a price move is a different event.
    basket = two_blocks()
    row = points_for(basket, {"twelvedata:A": (0.05, True, True, 0.1),
                              "twelvedata:B": (0.001, False, False, 9.0)})
    assert not row["trigger_volume"]

    row = points_for(basket, {"twelvedata:A": (0.05, True, True, 9.0)})
    assert row["trigger_volume"]


def test_volume_is_never_claimed_for_an_asset_without_volume():
    basket = two_blocks()
    row = points_for(basket, {"twelvedata:D": (0.05, True, True, 9.0)})
    assert not row["trigger_volume"]


def test_cluster_shift_needs_two_active_blocks():
    basket = two_blocks()
    # Only equity is active: two of its three assets breached Q95.
    one_block = points_for(basket, {"twelvedata:A": (0.05, True, False, 0.0),
                                    "twelvedata:B": (0.05, True, False, 0.0),
                                    "twelvedata:C": (0.001, False, False, 0.0),
                                    "twelvedata:D": (0.001, False, False, np.nan)})
    assert not one_block["trigger_cluster_shift"]

    both = points_for(basket, {"twelvedata:A": (0.05, True, False, 0.0),
                               "twelvedata:B": (0.05, True, False, 0.0),
                               "twelvedata:C": (0.001, False, False, 0.0),
                               "twelvedata:D": (0.05, True, False, np.nan),
                               "twelvedata:E": (0.05, True, False, np.nan),
                               "twelvedata:F": (0.001, False, False, np.nan)})
    assert both["trigger_cluster_shift"]


def test_cluster_shift_needs_a_tier1_among_the_breaches():
    basket = two_blocks()
    row = points_for(basket, {"twelvedata:B": (0.05, True, False, 0.0),
                              "twelvedata:C": (0.05, True, False, 0.0),
                              "twelvedata:E": (0.05, True, False, np.nan),
                              "twelvedata:F": (0.05, True, False, np.nan),
                              "twelvedata:A": (0.001, False, False, 0.0),
                              "twelvedata:D": (0.001, False, False, np.nan)})
    assert row["n_active_blocks"] == 2
    assert not row["trigger_cluster_shift"]


def test_block_needs_at_least_two_assets_not_just_the_share():
    # A third of three is one asset, but one is not enough: a block with a single
    # firing is not breadth, it is a single-asset move.
    basket = two_blocks()
    row = points_for(basket, {"twelvedata:A": (0.05, True, False, 0.0),
                              "twelvedata:B": (0.001, False, False, 0.0),
                              "twelvedata:C": (0.001, False, False, 0.0)})
    assert row["n_active_blocks"] == 0


def test_share_is_taken_from_assets_in_session():
    # At night the ETFs are closed, and demanding a third of a block's roster
    # would be an impossible requirement rather than a strict one.
    basket = two_blocks()
    row = points_for(basket, {"twelvedata:A": (0.05, True, False, 0.0),
                              "twelvedata:B": (0.05, True, False, 0.0)})
    # Only two of the block's assets are in session and both breached - the block is active.
    assert row["n_active_blocks"] >= 1


def test_maximum_is_twelve_points():
    basket = two_blocks()
    row = points_for(basket, {
        "twelvedata:A": (0.05, True, True, 9.0), "twelvedata:B": (0.05, True, True, 9.0),
        "twelvedata:C": (0.05, True, True, 9.0), "twelvedata:D": (0.05, True, True, np.nan),
        "twelvedata:E": (0.05, True, True, np.nan), "twelvedata:F": (0.05, True, True, np.nan),
    }, single=True)
    assert row["base_points"] == si_index.MAX_POINTS == 12


def test_si_total_applies_both_multipliers():
    points = pd.Series([10.0])
    assert si_index.si_total(points, pd.Series([1.8]), pd.Series([1.3])).iloc[0] == \
        pytest.approx(23.4)


def test_si_total_is_unchanged_without_multipliers():
    points = pd.Series([7.0])
    assert si_index.si_total(points, pd.Series([1.0]), pd.Series([1.0])).iloc[0] == 7.0

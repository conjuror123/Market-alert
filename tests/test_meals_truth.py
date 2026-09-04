from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from meals import truth
from meals.basket import Asset, Basket, VolatilityIndex

HOUR = 3600


def make_asset(ticker, block, in_basket=True):
    return Asset(ticker=ticker, source="twelvedata", tier=1, block=block,
                 has_volume=True, tick_size=0.01, session_template="us_equity",
                 fetch_interval="1h", label=ticker, in_basket=in_basket)


def make_basket(assets, outside=()):
    return Basket(assets=tuple(assets), outside=tuple(outside),
                  volatility_index=VolatilityIndex("VIXCLS", "fred", "1d", "VIX",
                                                   date(1990, 1, 1)),
                  anchor_exchange_tz="America/New_York",
                  history_since=date(2021, 1, 1), session_templates={})


def hours_from(n):
    """A run of consecutive hours starting inside the train period."""
    start = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp())
    return pd.Index([start + i * HOUR for i in range(n)], name="hour_utc")


def metrics_for(hours, **series):
    """One metrics frame per asset id, each a plain series of returns by hour."""
    return {aid: pd.DataFrame({"hour_utc": list(hours)[:len(values)], "r": values})
            for aid, values in series.items()}


# --- forward_sum ----------------------------------------------------------

def test_forward_sum_takes_the_next_hours_not_the_current_one():
    values = np.array([10.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    accumulated, _ = truth.forward_sum(values, horizon=2)
    # From position 0 the next two are 1 and 2 - the 10 sitting on the hour
    # itself is not part of what follows it.
    assert accumulated[0] == 3.0
    assert accumulated[1] == 5.0


def test_forward_sum_leaves_the_tail_undefined():
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    accumulated, _ = truth.forward_sum(values, horizon=2)
    # The last two hours have no full window ahead of them: that is missing, not
    # calm, and must not become False downstream.
    assert np.isnan(accumulated[-1]) and np.isnan(accumulated[-2])
    assert np.isfinite(accumulated[-3])


def test_a_gap_contributes_nothing_and_is_counted():
    values = np.array([0.0, 1.0, np.nan, 3.0, 0.0])
    accumulated, observed = truth.forward_sum(values, horizon=3)
    assert accumulated[0] == 4.0
    assert observed[0] == 2


def test_an_all_nan_window_accumulates_to_zero_observed_none():
    values = np.array([0.0, np.nan, np.nan, 0.0])
    accumulated, observed = truth.forward_sum(values, horizon=2)
    assert accumulated[0] == 0.0
    assert observed[0] == 0


# --- M_block --------------------------------------------------------------

def test_block_return_is_the_median_of_its_own_assets():
    basket = make_basket([make_asset("A", "equity"), make_asset("B", "equity"),
                          make_asset("C", "equity")])
    hours = hours_from(2)
    metrics = metrics_for(hours, **{"twelvedata:A": [0.01, 0.0],
                                    "twelvedata:B": [0.02, 0.0],
                                    "twelvedata:C": [0.09, 0.0]})

    blocks = truth.block_returns(metrics, basket, hours)

    assert blocks.loc[hours[0], "equity"] == 0.02


def test_an_asset_out_of_session_is_skipped_not_treated_as_zero():
    basket = make_basket([make_asset("A", "equity"), make_asset("B", "equity")])
    hours = hours_from(1)
    metrics = metrics_for(hours, **{"twelvedata:A": [0.04], "twelvedata:B": [np.nan]})

    blocks = truth.block_returns(metrics, basket, hours)

    # The median of what was observed, not of a 0.04 averaged against a made-up 0.
    assert blocks.loc[hours[0], "equity"] == 0.04


def test_a_block_with_nothing_observed_is_null():
    basket = make_basket([make_asset("A", "equity")])
    hours = hours_from(1)
    metrics = metrics_for(hours, **{"twelvedata:A": [np.nan]})
    blocks = truth.block_returns(metrics, basket, hours)
    assert pd.isna(blocks.loc[hours[0], "equity"])


def test_instruments_outside_the_basket_do_not_enter_a_block():
    # §8.1: a non-basket instrument has a block, but it serves SAED alone.
    basket = make_basket([make_asset("A", "FX")],
                         outside=[make_asset("Z", "FX", in_basket=False)])
    hours = hours_from(1)
    metrics = metrics_for(hours, **{"twelvedata:A": [0.01], "twelvedata:Z": [0.99]})

    blocks = truth.block_returns(metrics, basket, hours)

    assert blocks.loc[hours[0], "FX"] == 0.01


# --- thresholds and labels -------------------------------------------------

def test_thresholds_are_taken_on_train_only():
    hours = hours_from(4)
    # Two hours in train, two far later in test carrying a much larger move.
    hours = pd.Index(list(hours[:2]) +
                     [int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()) + i * HOUR
                      for i in range(2)], name="hour_utc")
    accumulated = pd.DataFrame({"equity": [0.01, 0.02, 5.0, 6.0]}, index=hours)

    thresholds = truth.train_thresholds(accumulated)

    assert thresholds.loc[0, "n_train_windows"] == 2
    assert thresholds.loc[0, "q99_abs_move"] < 0.03


def test_an_hour_is_significant_when_a_block_beats_its_own_q99():
    basket = make_basket([make_asset("A", "equity")])
    # A long quiet run so the train Q99 is small, then one large move ahead of
    # the hour under test.
    values = [0.0001] * 1000
    values[800] = 0.5
    hours = hours_from(len(values))
    metrics = metrics_for(hours, **{"twelvedata:A": values})

    frame, thresholds = truth.label(metrics, basket, hours, horizon=4)

    fired = frame[frame["significant"].fillna(False)]
    assert not fired.empty
    # The move at position 800 is seen from the four hours before it.
    assert set(fired.index) == set(range(796, 800))
    assert (fired["driving_block"] == "equity").all()


def test_the_tail_without_a_forward_window_is_null_not_false():
    basket = make_basket([make_asset("A", "equity")])
    hours = hours_from(50)
    metrics = metrics_for(hours, **{"twelvedata:A": [0.001] * 50})

    frame, _ = truth.label(metrics, basket, hours, horizon=4)

    assert frame["significant"].tail(4).isna().all()
    assert frame["significant"].head(1).notna().all()


def test_the_baseline_reads_two_percent_on_the_log_scale():
    basket = make_basket([make_asset("SPY", "equity")])
    values = [0.0] * 20
    values[10] = 0.03            # comfortably past ln(1.02)
    hours = hours_from(len(values))
    metrics = metrics_for(hours, **{"twelvedata:SPY": values})

    frame, _ = truth.label(metrics, basket, hours, horizon=2)

    assert truth.BASELINE_MOVE < 0.02
    assert bool(frame.loc[9, "baseline_spy"]) is True
    assert bool(frame.loc[0, "baseline_spy"]) is False


def test_a_missing_baseline_asset_leaves_the_column_null():
    basket = make_basket([make_asset("A", "equity")])
    hours = hours_from(30)
    metrics = metrics_for(hours, **{"twelvedata:A": [0.001] * 30})

    frame, _ = truth.label(metrics, basket, hours, horizon=2)

    assert frame["baseline_spy"].isna().all()


def test_a_fall_counts_as_much_as_a_rise():
    # §7 speaks of the size of a move; a signed Q99 would only ever flag rallies.
    basket = make_basket([make_asset("A", "equity")])
    values = [0.0001] * 1000
    values[800] = -0.5
    hours = hours_from(len(values))
    metrics = metrics_for(hours, **{"twelvedata:A": values})

    frame, _ = truth.label(metrics, basket, hours, horizon=4)

    assert frame["significant"].fillna(False).any()

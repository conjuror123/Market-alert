import numpy as np
import pandas as pd
import pytest

from tremor import cross_section as cs
from tremor.basket import Asset, Basket, VolatilityIndex
from datetime import date

HOUR = 3600


def make_asset(ticker, block, tier=1):
    return Asset(ticker=ticker, source="twelvedata", tier=tier, block=block,
                 has_volume=True, tick_size=0.01, session_template="us_equity",
                 fetch_interval="1h", label=ticker, in_basket=True)


def make_basket(assets):
    return Basket(assets=tuple(assets), outside=(),
                  volatility_index=VolatilityIndex("VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
                  anchor_exchange_tz="America/New_York",
                  history_since=date(2021, 1, 1), session_templates={})


def four_by_two():
    """A basket in miniature mirroring the real one: a daytime block that drops out
    at night, and two round-the-clock blocks that between them make quorum.
    """
    return make_basket([
        make_asset("A", "equity", 1), make_asset("B", "equity", 2),
        make_asset("C", "FX", 1), make_asset("D", "FX", 2),
        make_asset("E", "FX", 2), make_asset("F", "FX", 2),
        make_asset("I", "FX", 2), make_asset("J", "FX", 2),
        make_asset("G", "crypto", 1), make_asset("H", "crypto", 2),
        make_asset("K", "crypto", 2),
    ])


def panel_from(rows, columns):
    return pd.DataFrame(rows, columns=columns,
                        index=[h * HOUR for h in range(1, len(rows) + 1)])


def test_panel_keeps_gaps_as_gaps():
    # Filling gaps with zeros is forbidden: a zero asserts "the asset did not
    # move", while a gap means "we do not know".
    metrics = {
        "a": pd.DataFrame({"hour_utc": [HOUR, 2 * HOUR], "r": [0.01, 0.02]}),
        "b": pd.DataFrame({"hour_utc": [HOUR], "r": [0.03]}),
    }
    panel = cs.build_panel(metrics, "r")

    assert np.isnan(panel.loc[2 * HOUR, "b"])
    assert (panel.loc[2 * HOUR] == 0).sum() == 0


def test_block_factor_excludes_the_asset_itself():
    # Without the exclusion, an instrument in a block of three would subtract a
    # third of itself, and its own move would partly vanish from the residual.
    basket = make_basket([make_asset("A", "crypto"), make_asset("B", "crypto", 2),
                          make_asset("C", "crypto", 2),
                          make_asset("D", "FX"), make_asset("E", "FX", 2)])
    columns = [a.asset_id for a in basket.assets]
    panel = panel_from([[0.10, 0.01, 0.02, 0.0, 0.0]], columns)

    factors = cs.block_factors(panel, basket)

    # For A the factor is the median of B and C, excluding A itself.
    assert factors["twelvedata:A"].iloc[0] == pytest.approx(0.015)
    # For B, the median of A and C.
    assert factors["twelvedata:B"].iloc[0] == pytest.approx(0.06)


def test_block_factor_is_a_plain_median_because_weights_are_equal():
    # Under the equality rule weights within a block are equal, so a
    # block's weighted median coincides with the plain one.
    basket = four_by_two()
    weights = basket.weights()
    fx = [a.asset_id for a in basket.assets if a.block == "FX"]
    assert len({round(weights[a], 12) for a in fx}) == 1


def test_outside_basket_instrument_uses_the_whole_block():
    # A non-basket instrument does not enter the factor; nothing to exclude.
    basket = Basket(
        assets=(make_asset("A", "FX"), make_asset("B", "FX", 2),
                make_asset("C", "crypto"), make_asset("D", "crypto", 2)),
        outside=(make_asset("Z", "FX", 2),),
        volatility_index=cs.Basket.__annotations__ and make_basket([]).volatility_index,
        anchor_exchange_tz="America/New_York", history_since=date(2021, 1, 1),
        session_templates={})
    columns = [a.asset_id for a in basket.assets]
    panel = panel_from([[0.02, 0.04, 0.0, 0.0]], columns)

    factors = cs.block_factors(panel, basket)
    assert factors["twelvedata:Z"].iloc[0] == pytest.approx(0.03)


def _fx_basket():
    """A basket whose FX block is quoted from both sides of the dollar."""
    return make_basket([make_asset(t, "FX") for t in
                        ("EUR/USD", "GBP/USD", "AUD/USD",
                         "USD/JPY", "USD/CHF", "USD/CAD")])


def test_the_block_factor_sees_a_dollar_move_the_median_would_cancel():
    # A pure dollar rally: the three USD-quote pairs fall, the three USD-base
    # pairs rise. An unoriented median of the six is about zero, and the move
    # leaks into every member's residual instead of being removed from it.
    move = 0.01
    panel = pd.DataFrame({
        "twelvedata:EUR/USD": [-move], "twelvedata:GBP/USD": [-move],
        "twelvedata:AUD/USD": [-move], "twelvedata:USD/JPY": [move],
        "twelvedata:USD/CHF": [move], "twelvedata:USD/CAD": [move],
    })
    out = cs.block_factors(panel, _fx_basket())

    # each pair sees the move in ITS OWN direction, at full size
    assert out["twelvedata:EUR/USD"].iloc[0] == pytest.approx(-move)
    assert out["twelvedata:USD/JPY"].iloc[0] == pytest.approx(+move)
    # and an unoriented median would have seen nothing at all
    assert abs(float(np.median(panel.iloc[0]))) < 1e-12


def test_orientation_does_not_disturb_a_block_that_moves_together():
    # Equities respond to their block's move with one sign, so the correction
    # has to be the identity there.
    basket = make_basket([make_asset(t, "equity") for t in ("SPY", "QQQ", "IWM")])
    panel = pd.DataFrame({"twelvedata:SPY": [0.02], "twelvedata:QQQ": [0.03],
                          "twelvedata:IWM": [0.04]})
    out = cs.block_factors(panel, basket)
    assert out["twelvedata:SPY"].iloc[0] == pytest.approx(0.035)


def test_the_asset_is_still_left_out_of_its_own_block_factor():
    # Orientation must not quietly undo the leave-one-out: an instrument that
    # moves alone must not find its own move waiting in its block factor.
    panel = pd.DataFrame({
        "twelvedata:EUR/USD": [10.0], "twelvedata:GBP/USD": [-0.01],
        "twelvedata:AUD/USD": [-0.01], "twelvedata:USD/JPY": [0.01],
        "twelvedata:USD/CHF": [0.01], "twelvedata:USD/CAD": [0.01],
    })
    out = cs.block_factors(panel, _fx_basket())
    assert abs(out["twelvedata:EUR/USD"].iloc[0]) < 0.02


def test_the_orientation_comes_from_the_ticker_not_from_the_data():
    # A sign fitted per window could flip between windows, which is worse than
    # not correcting at all. It is a fact about how the pair is quoted.
    assert make_asset("EUR/USD", "FX").block_sign == -1.0
    assert make_asset("USD/JPY", "FX").block_sign == +1.0
    assert make_asset("SPY", "equity").block_sign == +1.0
    # a cross with no dollar leg is left alone rather than guessed at
    assert make_asset("EUR/GBP", "FX").block_sign == +1.0

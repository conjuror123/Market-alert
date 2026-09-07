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


def test_weighted_median_respects_weights():
    # Six currency pairs against three crypto assets: without weights the median
    # would be decided by headcount rather than by equality between blocks.
    values = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.0, -5.0, -5.0, -5.0])
    fx_weight, crypto_weight = 1 / 12, 1 / 6   # the FX block and the crypto block weigh the same
    weights = np.array([fx_weight] * 6 + [crypto_weight] * 3)

    assert cs.weighted_median(values, weights) == pytest.approx(-2.0)
    # Without weights a simple majority would win.
    assert np.median(values) == 1.0


def test_weighted_median_picks_the_half_weight_point():
    assert cs.weighted_median(np.array([1.0, 2.0, 3.0]),
                              np.array([0.1, 0.1, 0.8])) == 3.0


def test_quorum_needs_assets_tier1_and_two_populated_blocks():
    basket = four_by_two()
    columns = [a.asset_id for a in basket.assets]
    full = [0.01] * 11
    thin = [0.01, 0.01] + [np.nan] * 9
    night = [np.nan, np.nan] + [0.01] * 9   # the daytime block is closed

    frame = cs.quorum(panel_from([full, thin, night], columns), basket)

    assert bool(frame["quorum_ok"].iloc[0])
    assert not bool(frame["quorum_ok"].iloc[1])   # too few assets
    # At night two round-the-clock blocks suffice - the fifth block was added to
    # the basket for exactly this case.
    assert bool(frame["quorum_ok"].iloc[2])


def test_quorum_rejects_a_single_populated_block():
    basket = four_by_two()
    columns = [a.asset_id for a in basket.assets]
    # Only the FX block: enough assets, not enough blocks.
    row = [np.nan, np.nan] + [0.01] * 6 + [np.nan] * 3
    frame = cs.quorum(panel_from([row], columns), basket)

    assert frame["n_blocks_populated"].iloc[0] == 1
    assert not bool(frame["quorum_ok"].iloc[0])


def test_quorum_needs_two_tier1_assets():
    basket = make_basket([
        make_asset("A", "equity", 1), make_asset("B", "equity", 2),
        make_asset("C", "FX", 2), make_asset("D", "FX", 2),
        make_asset("E", "FX", 2), make_asset("F", "FX", 2),
        make_asset("G", "crypto", 2), make_asset("H", "crypto", 2),
        make_asset("I", "crypto", 2),
    ])
    columns = [a.asset_id for a in basket.assets]
    frame = cs.quorum(panel_from([[0.01] * 9], columns), basket)

    assert frame["n_tier1"].iloc[0] == 1
    assert not bool(frame["quorum_ok"].iloc[0])


def test_panel_keeps_gaps_as_gaps():
    # §3.3 forbids filling gaps with zeros: a zero asserts "the asset did not
    # move", while a gap means "we do not know".
    metrics = {
        "a": pd.DataFrame({"hour_utc": [HOUR, 2 * HOUR], "r": [0.01, 0.02]}),
        "b": pd.DataFrame({"hour_utc": [HOUR], "r": [0.03]}),
    }
    panel = cs.build_panel(metrics, "r")

    assert np.isnan(panel.loc[2 * HOUR, "b"])
    assert (panel.loc[2 * HOUR] == 0).sum() == 0


def test_csv_uses_ddof_one():
    basket = four_by_two()
    columns = [a.asset_id for a in basket.assets]
    values = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.11]
    panel = panel_from([values], columns)
    sigma = panel_from([[0.01] * 11], columns)

    frame = cs.cross_sectional_volatility(panel, sigma, basket)
    assert frame["csv"].iloc[0] == pytest.approx(np.std(values, ddof=1))


def test_compression_needs_both_a_narrow_spread_and_a_real_move():
    # A narrow spread on its own is just a quiet hour.
    rng = np.random.default_rng(7)
    csv_norm = pd.Series(list(rng.normal(1.0, 0.1, 50)) + [0.1, 0.1])
    m = pd.Series(list(rng.normal(0.0, 0.001, 50)) + [0.0001, 0.5])

    out, _ = cs.csv_compression(csv_norm, m, window=50)

    assert not bool(out.iloc[50])   # spread narrow, basket standing still
    assert bool(out.iloc[51])       # spread narrow AND the basket moved


def test_compression_is_null_before_the_window_fills():
    out, _ = cs.csv_compression(pd.Series([1.0] * 10), pd.Series([0.01] * 10), window=50)
    assert out.isna().all()


def test_pc1_ratio_is_one_when_assets_move_together():
    basket = make_basket([make_asset(t, "equity") for t in "ABC"])
    rng = np.random.default_rng(0)
    common = rng.normal(size=200)
    rows = [[c, c * 2, c * 3] for c in common]   # perfect coherence
    panel = panel_from(rows, [a.asset_id for a in basket.assets])
    ok = pd.Series(True, index=panel.index)

    ratio, _ = cs.pc1_ratio(panel, ok, basket, window=100)
    assert ratio.dropna().iloc[-1] == pytest.approx(1.0, abs=1e-9)


def test_pc1_ratio_is_low_when_assets_are_independent():
    basket = make_basket([make_asset(t, "equity") for t in "ABC"])
    rng = np.random.default_rng(1)
    rows = rng.normal(size=(300, 3)).tolist()
    panel = panel_from(rows, [a.asset_id for a in basket.assets])
    ok = pd.Series(True, index=panel.index)

    ratio, _ = cs.pc1_ratio(panel, ok, basket, window=200)
    ratio = ratio.dropna()
    # Three independent series: each component explains about a third.
    assert 0.25 < ratio.iloc[-1] < 0.55


def test_pc1_ratio_needs_enough_rows_and_assets():
    basket = make_basket([make_asset(t, "equity") for t in "AB"])
    rng = np.random.default_rng(2)
    panel = panel_from(rng.normal(size=(100, 2)).tolist(),
                       [a.asset_id for a in basket.assets])
    ok = pd.Series(True, index=panel.index)

    # Fewer than three assets - the conditioning fails, the value is NULL.
    assert cs.pc1_ratio(panel, ok, basket, window=80)[0].isna().all()


def test_pc1_ratio_skips_incomplete_columns():
    basket = make_basket([make_asset(t, "equity") for t in "ABCD"])
    rng = np.random.default_rng(3)
    rows = rng.normal(size=(200, 4))
    rows[:, 3] = np.nan          # the fourth asset never traded
    panel = panel_from(rows.tolist(), [a.asset_id for a in basket.assets])
    ok = pd.Series(True, index=panel.index)

    # The three remaining assets give a valid value; the fourth simply drops out.
    assert cs.pc1_ratio(panel, ok, basket, window=100)[0].dropna().size > 0


def test_single_factor_falls_back_to_compression_when_pca_is_null():
    # §3.4: an hour that passed quorum must receive a definite trigger value, or
    # it contributes no term to the SI-Index.
    compression = pd.Series([True, False, pd.NA], dtype="boolean")
    sync = pd.Series([pd.NA, pd.NA, pd.NA], dtype="boolean")

    out = cs.single_factor(compression, sync)

    assert bool(out.iloc[0])
    assert out.iloc[1] is np.False_ or not bool(out.iloc[1])
    assert pd.isna(out.iloc[2])   # neither sub-condition was assessed


def test_single_factor_is_an_or():
    compression = pd.Series([False, True, False], dtype="boolean")
    sync = pd.Series([True, False, False], dtype="boolean")
    out = cs.single_factor(compression, sync)
    assert list(out) == [True, True, False]


def test_subcondition_correlation_is_undefined_when_one_never_fires():
    # Exactly what happened on real data: compression fired once in five years,
    # and never inside the overlap with synchrony. In that case the §3.4
    # correlation cannot be computed from anything, and pretending it is zero is
    # not on.
    frame = pd.DataFrame({
        "csv_compression": pd.Series([False] * 10, dtype="boolean"),
        "pca_sync": pd.Series([True, False] * 5, dtype="boolean"),
    })
    assert np.isnan(cs.subcondition_correlation(frame))


def test_subcondition_correlation_is_computed_when_both_vary():
    frame = pd.DataFrame({
        "csv_compression": pd.Series([True, True, False, False], dtype="boolean"),
        "pca_sync": pd.Series([True, False, True, False], dtype="boolean"),
    })
    assert cs.subcondition_correlation(frame) == pytest.approx(0.0)


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
    # Under the equality rule of §2.3 weights within a block are equal, so a
    # block's weighted median coincides with the plain one.
    basket = four_by_two()
    weights = basket.weights()
    fx = [a.asset_id for a in basket.assets if a.block == "FX"]
    assert len({round(weights[a], 12) for a in fx}) == 1


def test_outside_basket_instrument_uses_the_whole_block():
    # A non-basket instrument does not enter the factor (§8.1); nothing to exclude.
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


def test_coherence_is_high_when_every_asset_moves_the_same_way():
    # All assets at the same Z: a large cross-sectional mean over a spread of
    # nearly nothing.
    hours = pd.Index([HOUR, 2 * HOUR])
    together = pd.DataFrame({"a": [2.0, 0.1], "b": [2.1, -0.9], "c": [1.9, 0.8]},
                            index=hours)
    ok = pd.Series([True, True], index=hours)

    values = cs.coherence(together, ok)

    assert values.iloc[0] > values.iloc[1]


def test_coherence_ignores_an_hour_without_quorum():
    hours = pd.Index([HOUR, 2 * HOUR])
    panel = pd.DataFrame({"a": [2.0, 2.0], "b": [2.0, 2.0], "c": [2.1, 2.1]}, index=hours)
    ok = pd.Series([True, False], index=hours)

    values = cs.coherence(panel, ok)

    assert np.isfinite(values.iloc[0]) and np.isnan(values.iloc[1])


def test_coherence_is_undefined_when_the_assets_do_not_vary_at_all():
    # A zero spread would divide by zero; the answer is "not measurable", not
    # "infinitely coherent".
    hours = pd.Index([HOUR])
    panel = pd.DataFrame({"a": [1.0], "b": [1.0], "c": [1.0]}, index=hours)
    values = cs.coherence(panel, pd.Series([True], index=hours))
    assert np.isnan(values.iloc[0])


def test_compression_needs_the_basket_to_have_moved_as_well():
    # The second leg is unchanged from §3.2: agreement about nothing much is not
    # an event.
    n = 2500
    hours = pd.Index([(i + 1) * HOUR for i in range(n)])
    coherent = pd.Series(np.linspace(0.1, 5.0, n), index=hours)
    still = pd.Series(np.zeros(n), index=hours)

    fired, _ = cs.coherence_compression(coherent, still)

    assert not fired.fillna(False).any()


def test_the_specs_own_compression_is_kept_and_still_computed():
    # It fires nowhere on this basket, and the empty column is the evidence -
    # §3.4 wants both sub-conditions logged separately.
    n = 2500
    hours = pd.Index([(i + 1) * HOUR for i in range(n)])
    csv_norm = pd.Series(np.linspace(1.0, 2.0, n), index=hours)
    m = pd.Series(np.zeros(n), index=hours)

    fired, threshold = cs.csv_compression(csv_norm, m)

    assert fired.notna().any() and threshold.notna().any()


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

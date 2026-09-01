import numpy as np
import pandas as pd
import pytest

from meals import cross_section as cs
from meals.basket import Asset, Basket, VolatilityIndex
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
    """Корзина в миниатюре, повторяющая настоящую: дневной блок, который ночью
    выпадает, и два круглосуточных, которых вдвоём хватает на кворум."""
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
    # Шесть валютных пар против трёх криптоактивов: без весов медиана
    # определялась бы числом участников, а не равновесностью блоков.
    values = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.0, -5.0, -5.0, -5.0])
    fx_weight, crypto_weight = 1 / 12, 1 / 6   # блок FX и блок crypto весят поровну
    weights = np.array([fx_weight] * 6 + [crypto_weight] * 3)

    assert cs.weighted_median(values, weights) == pytest.approx(-2.0)
    # Без весов победило бы простое большинство.
    assert np.median(values) == 1.0


def test_weighted_median_picks_the_half_weight_point():
    assert cs.weighted_median(np.array([1.0, 2.0, 3.0]),
                              np.array([0.1, 0.1, 0.8])) == 3.0


def test_quorum_needs_assets_tier1_and_two_populated_blocks():
    basket = four_by_two()
    columns = [a.asset_id for a in basket.assets]
    full = [0.01] * 11
    thin = [0.01, 0.01] + [np.nan] * 9
    night = [np.nan, np.nan] + [0.01] * 9   # дневной блок закрыт

    frame = cs.quorum(panel_from([full, thin, night], columns), basket)

    assert bool(frame["quorum_ok"].iloc[0])
    assert not bool(frame["quorum_ok"].iloc[1])   # мало активов
    # Ночью хватает двух круглосуточных блоков - ровно ради этого случая в
    # корзину и добавлен пятый блок.
    assert bool(frame["quorum_ok"].iloc[2])


def test_quorum_rejects_a_single_populated_block():
    basket = four_by_two()
    columns = [a.asset_id for a in basket.assets]
    # Только блок FX: активов хватает, блоков - нет.
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
    # Заполнять пропуски нулями по п.3.3 запрещено: ноль это утверждение
    # "актив не двигался", а пропуск означает "мы не знаем".
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
    # Узкий разброс сам по себе - это просто тихий час.
    rng = np.random.default_rng(7)
    csv_norm = pd.Series(list(rng.normal(1.0, 0.1, 50)) + [0.1, 0.1])
    m = pd.Series(list(rng.normal(0.0, 0.001, 50)) + [0.0001, 0.5])

    out = cs.csv_compression(csv_norm, m, window=50)

    assert not bool(out.iloc[50])   # разброс узок, корзина стоит
    assert bool(out.iloc[51])       # разброс узок И корзина сдвинулась


def test_compression_is_null_before_the_window_fills():
    out = cs.csv_compression(pd.Series([1.0] * 10), pd.Series([0.01] * 10), window=50)
    assert out.isna().all()


def test_pc1_ratio_is_one_when_assets_move_together():
    basket = make_basket([make_asset(t, "equity") for t in "ABC"])
    rng = np.random.default_rng(0)
    common = rng.normal(size=200)
    rows = [[c, c * 2, c * 3] for c in common]   # идеальная согласованность
    panel = panel_from(rows, [a.asset_id for a in basket.assets])
    ok = pd.Series(True, index=panel.index)

    ratio = cs.pc1_ratio(panel, ok, basket, window=100)
    assert ratio.dropna().iloc[-1] == pytest.approx(1.0, abs=1e-9)


def test_pc1_ratio_is_low_when_assets_are_independent():
    basket = make_basket([make_asset(t, "equity") for t in "ABC"])
    rng = np.random.default_rng(1)
    rows = rng.normal(size=(300, 3)).tolist()
    panel = panel_from(rows, [a.asset_id for a in basket.assets])
    ok = pd.Series(True, index=panel.index)

    ratio = cs.pc1_ratio(panel, ok, basket, window=200).dropna()
    # Три независимых ряда: каждая компонента объясняет около трети.
    assert 0.25 < ratio.iloc[-1] < 0.55


def test_pc1_ratio_needs_enough_rows_and_assets():
    basket = make_basket([make_asset(t, "equity") for t in "AB"])
    rng = np.random.default_rng(2)
    panel = panel_from(rng.normal(size=(100, 2)).tolist(),
                       [a.asset_id for a in basket.assets])
    ok = pd.Series(True, index=panel.index)

    # Меньше трёх активов - обусловленность не выполнена, значение NULL.
    assert cs.pc1_ratio(panel, ok, basket, window=80).isna().all()


def test_pc1_ratio_skips_incomplete_columns():
    basket = make_basket([make_asset(t, "equity") for t in "ABCD"])
    rng = np.random.default_rng(3)
    rows = rng.normal(size=(200, 4))
    rows[:, 3] = np.nan          # четвёртый актив не торговал ни разу
    panel = panel_from(rows.tolist(), [a.asset_id for a in basket.assets])
    ok = pd.Series(True, index=panel.index)

    # Три оставшихся актива дают валидное значение, четвёртый просто выпадает.
    assert cs.pc1_ratio(panel, ok, basket, window=100).dropna().size > 0


def test_single_factor_falls_back_to_compression_when_pca_is_null():
    # П.3.4: час, прошедший кворум, обязан получить определённое значение
    # триггера, иначе он не даст слагаемого в SI-Index.
    compression = pd.Series([True, False, pd.NA], dtype="boolean")
    sync = pd.Series([pd.NA, pd.NA, pd.NA], dtype="boolean")

    out = cs.single_factor(compression, sync)

    assert bool(out.iloc[0])
    assert out.iloc[1] is np.False_ or not bool(out.iloc[1])
    assert pd.isna(out.iloc[2])   # не оценено ни одно подусловие


def test_single_factor_is_an_or():
    compression = pd.Series([False, True, False], dtype="boolean")
    sync = pd.Series([True, False, False], dtype="boolean")
    out = cs.single_factor(compression, sync)
    assert list(out) == [True, True, False]


def test_subcondition_correlation_is_undefined_when_one_never_fires():
    # Ровно то, что получилось на реальных данных: сжатие сработало один раз за
    # пять лет, и внутри пересечения с синхронностью - ни разу. Корреляцию из
    # п.3.4 в таком случае считать не из чего, и притворяться, что она нулевая,
    # нельзя.
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
    # Без исключения инструмент в блоке из трёх на треть вычитал бы сам себя,
    # и собственное движение частично исчезало бы из остатка.
    basket = make_basket([make_asset("A", "crypto"), make_asset("B", "crypto", 2),
                          make_asset("C", "crypto", 2),
                          make_asset("D", "FX"), make_asset("E", "FX", 2)])
    columns = [a.asset_id for a in basket.assets]
    panel = panel_from([[0.10, 0.01, 0.02, 0.0, 0.0]], columns)

    factors = cs.block_factors(panel, basket)

    # Для A фактор - медиана B и C, без самого A.
    assert factors["twelvedata:A"].iloc[0] == pytest.approx(0.015)
    # Для B - медиана A и C.
    assert factors["twelvedata:B"].iloc[0] == pytest.approx(0.06)


def test_block_factor_is_a_plain_median_because_weights_are_equal():
    # По правилу равновесности п.2.3 веса внутри блока равны, поэтому
    # взвешенная медиана блока совпадает с обычной.
    basket = four_by_two()
    weights = basket.weights()
    fx = [a.asset_id for a in basket.assets if a.block == "FX"]
    assert len({round(weights[a], 12) for a in fx}) == 1


def test_outside_basket_instrument_uses_the_whole_block():
    # Внекорзинный инструмент в фактор не входит (п.8.1), исключать нечего.
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

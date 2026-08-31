import math

import numpy as np
import pandas as pd
import pytest

from meals import windows, zscore

LAM = windows.LAMBDA  # 2/25 = 0.08


def test_z_is_measured_against_the_previous_bar_state():
    # Эталон, посчитанный руками. Начальное состояние: mean = 0,
    # var = r[0]^2 = 1e-4, значит sigma_eff = 0.01 и Z[0] = 0.01/0.01 = 1.
    r = np.array([0.01, 0.02])
    sigma_lt = np.array([0.001, 0.001])  # отсечка 2e-4, знаменатель не задевает
    z, _ = zscore.ewma_state(r, r, sigma_lt)

    assert z[0] == pytest.approx(1.0)

    # После первого бара: mean = 0.08*0.01 = 8e-4,
    # var = 0.08*(0.01-0)^2 + 0.92*1e-4 = 1e-4, sigma_eff = 0.01.
    assert z[1] == pytest.approx((0.02 - 8e-4) / 0.01)


def test_variance_update_uses_the_previous_mean():
    # ТЗ оговаривает это отдельной строкой: в дисперсию подставляется
    # ewma_mean ПРЕДЫДУЩЕГО бара. Обе формы выглядят естественно, но дают
    # разные числа, и разойтись здесь означает тихо считать не то.
    r = np.array([0.01, 0.02, -0.03])
    sigma_lt = np.full(3, 0.001)
    z, _ = zscore.ewma_state(r, r, sigma_lt)

    mean1 = LAM * 0.01
    var1 = LAM * (0.01 - 0.0) ** 2 + (1 - LAM) * 1e-4
    mean2 = LAM * 0.02 + (1 - LAM) * mean1
    var2 = LAM * (0.02 - mean1) ** 2 + (1 - LAM) * var1  # именно mean1, не mean2

    assert z[2] == pytest.approx((-0.03 - mean2) / math.sqrt(var2))

    # Для контраста: с новым средним в дисперсии ответ был бы другим.
    wrong_var2 = LAM * (0.02 - mean2) ** 2 + (1 - LAM) * var1
    assert z[2] != pytest.approx((-0.03 - mean2) / math.sqrt(wrong_var2))


def test_state_update_takes_the_winsorised_return():
    # Всплеск не должен раздувать оценку нормы: в состояние идёт r_w, в Z - r.
    r = np.array([0.01, 0.50, 0.01])
    r_w = np.array([0.01, 0.02, 0.01])   # всплеск подрезан для состояния
    sigma_lt = np.full(3, 0.001)

    z_clipped, _ = zscore.ewma_state(r, r_w, sigma_lt)
    z_raw, _ = zscore.ewma_state(r, r, sigma_lt)

    # Z самого всплеска одинаков - он считается против ПРОШЛОГО состояния.
    assert z_clipped[1] == pytest.approx(z_raw[1])
    # А вот следующий бар различается: неподрезанное состояние раздулось.
    assert abs(z_clipped[2]) > abs(z_raw[2])


def test_sigma_eff_never_falls_below_the_floor():
    # Замершая котировка схлопывает EWMA-дисперсию. Без отсечки любое
    # последующее движение дало бы астрономический Z.
    r = np.array([0.0] * 30 + [0.001])
    sigma_lt = np.full(31, 0.01)  # отсечка 0.002
    z, sigma_eff = zscore.ewma_state(r, r, sigma_lt)

    assert sigma_eff[-1] == pytest.approx(0.2 * 0.01)
    assert abs(z[-1]) < 1.0


def test_thresholds_exclude_the_current_bar():
    # Порог, в который включено оцениваемое наблюдение, подстраивается под него
    # и занижает собственное срабатывание.
    abs_z = pd.Series([1.0] * 10 + [100.0])
    q95, q99 = zscore.adaptive_thresholds(abs_z, window=10)

    # На последнем баре окно - это первые десять единиц, всплеск в него не вошёл.
    assert q99.iloc[-1] == pytest.approx(1.0)


def test_thresholds_are_smoothed_by_the_specified_recurrence():
    abs_z = pd.Series([1.0] * 10 + [5.0] * 10)
    q95, _ = zscore.adaptive_thresholds(abs_z, window=10)

    raw = abs_z.shift(1).rolling(10, min_periods=10).quantile(0.95)
    expected = raw.ewm(alpha=windows.LAMBDA_Q, adjust=False).mean()
    pd.testing.assert_series_equal(q95, expected, check_names=False)

    # Сглаживание тормозит порог: он не прыгает вслед за окном.
    assert q95.iloc[-1] < raw.iloc[-1]


def test_breach_needs_both_legs():
    abs_z = pd.Series([5.0, 5.0, 1.0])
    abs_r = pd.Series([0.10, 0.001, 0.10])
    sigma_lt = pd.Series([0.01, 0.01, 0.01])
    q95 = pd.Series([2.0, 2.0, 2.0])
    q99 = pd.Series([3.0, 3.0, 3.0])

    out = zscore.breaches(abs_z, abs_r, sigma_lt, q95, q99)

    # Обе ноги: |Z| > Q99 и |r| >= 3 * sigma_LT.
    assert bool(out["breach_q99"].iloc[0])
    # Относительная нога прошла, абсолютная нет - движение ничтожно по меркам
    # всей истории, каким бы редким оно ни было для текущего затишья.
    assert not bool(out["breach_q99"].iloc[1])
    # Абсолютная прошла, относительная нет.
    assert not bool(out["breach_q99"].iloc[2])


def test_q95_leg_is_looser_than_q99():
    abs_z = pd.Series([2.5])
    abs_r = pd.Series([0.02])
    sigma_lt = pd.Series([0.01])
    out = zscore.breaches(abs_z, abs_r, sigma_lt, pd.Series([2.0]), pd.Series([3.0]))

    assert bool(out["breach_q95"].iloc[0])
    assert not bool(out["breach_q99"].iloc[0])


def test_unevaluated_breach_is_null_not_false():
    # П.1.2: NULL и False - разные вещи. "Порога ещё не существует" нельзя
    # записать как "порог не превышен".
    abs_z = pd.Series([5.0, 5.0])
    abs_r = pd.Series([0.1, 0.1])
    sigma_lt = pd.Series([np.nan, 0.01])
    out = zscore.breaches(abs_z, abs_r, sigma_lt, pd.Series([2.0, 2.0]),
                          pd.Series([3.0, 3.0]))

    assert pd.isna(out["breach_q99"].iloc[0])
    assert out["breach_q99"].iloc[1] is np.True_ or bool(out["breach_q99"].iloc[1])


def test_compute_drops_z_while_sigma_lt_is_unknown():
    # На разогреве у знаменателя нет нижней отсечки, и на замерших котировках
    # Z получается бессмысленным. Такие значения не должны попадать в окно
    # перцентилей - по п.6.6 час до first_valid_hour не участвует в статистике.
    frame = pd.DataFrame({
        "r": [0.0, 0.0, 0.0, 0.01, 0.01],
        "r_w": [0.0, 0.0, 0.0, 0.01, 0.01],
        "sigma_lt": [np.nan, np.nan, np.nan, 0.01, 0.01],
    })
    out = zscore.compute(frame, w_asset=2)

    assert out["z"].iloc[:3].isna().all()
    assert out["z"].iloc[3:].notna().all()


def test_compute_on_empty_input_keeps_columns():
    out = zscore.compute(pd.DataFrame({"r": [], "r_w": [], "sigma_lt": []}), w_asset=10)
    assert out.empty
    assert {"z", "q95", "q99", "breach_q95", "breach_q99"} <= set(out.columns)

"""Идиосинкратический остаток: вход модуля SAED (ТЗ п.3.6).

Смысл всей конструкции в одном вопросе: движение этого актива - его
собственное, или он просто плывёт вместе со всем рынком? Сырая доходность на
такой вопрос не отвечает. В день, когда падает всё, каждый актив покажет
крупное движение и крупный Z, и детектор по сырой доходности выдаст двадцать
одинаковых алертов об одном и том же событии - ровно так и ведёт себя часовой
сигнал нынешнего бота.

Поэтому из доходности вычитается общий фактор корзины: r = alpha + beta * F + e,
и дальше в дело идёт только остаток e. Бета оценивается скользящим окном и
строго на данных ДО текущего бара - иначе движение, которое мы хотим
задетектировать, само подправило бы коэффициент и частично вычлось бы из себя.

Остаток обрабатывается тем же аппаратом п.3.1, что и цена, но с полностью
СВОИМИ состояниями: своя EWMA, своя долгосрочная сигма, свои сглаженные пороги.
Смешивать их с ценовыми нельзя - у остатка другой масштаб и другое
распределение.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from meals import windows
from meals.basket import Asset


def rolling_beta(returns: pd.Series, factor: pd.Series,
                 window: int = windows.REGRESSION_WINDOW,
                 minimum: int = windows.REGRESSION_MIN) -> pd.DataFrame:
    """Скользящая регрессия r на F по СОВМЕСТНЫМ валидным барам (п.2.7).

    Окно измеряется в барах, где определены обе величины, а не в календарных
    часах: у фонда фактор корзины существует только в американскую сессию, и
    окно из пятисот календарных часов дало бы ему вчетверо меньше наблюдений,
    чем валютной паре.

    Коэффициенты сдвинуты на бар вперёд - оценка, доступная НА момент t,
    построена по данным до t включительно предыдущего бара.
    """
    joint = pd.DataFrame({"r": returns, "f": factor}).dropna()
    if len(joint) < minimum:
        return pd.DataFrame({"alpha": np.nan, "beta": np.nan}, index=returns.index)

    rolling = joint.rolling(window, min_periods=minimum)
    mean_r = rolling["r"].mean()
    mean_f = rolling["f"].mean()
    var_f = rolling["f"].var(ddof=1)
    cov = rolling.cov().unstack()[("r", "f")]

    beta = (cov / var_f).where(var_f > 0)
    alpha = mean_r - beta * mean_f
    # Сдвиг на бар: на баре t используется оценка, посчитанная по данным до t.
    estimates = pd.DataFrame({"alpha": alpha.shift(1), "beta": beta.shift(1)})
    return estimates.reindex(returns.index)


def rolling_two_factor(returns: pd.Series, factor: pd.Series, block_factor: pd.Series,
                       window: int = windows.REGRESSION_WINDOW,
                       minimum: int = windows.REGRESSION_MIN) -> pd.DataFrame:
    """Регрессия на два фактора: корзину и собственный блок актива.

    Решается через нормальные уравнения, а не подгонкой в цикле: для двух
    регрессоров система 2x2 выписывается явно через скользящие дисперсии и
    ковариации, и весь расчёт остаётся векторным (слой A из п.6.1).

    Вырожденный случай оговорён отдельно. Если два фактора в окне почти
    коллинеарны, определитель стремится к нулю, и коэффициенты разлетаются на
    произвольные величины с противоположными знаками - формально решение есть,
    по смыслу это шум. В таком окне регрессия откатывается к одному фактору
    корзины, то есть к поведению п.3.6.
    """
    joint = pd.DataFrame({"y": returns, "x1": factor, "x2": block_factor}).dropna()
    if len(joint) < minimum:
        return pd.DataFrame({"alpha": np.nan, "beta": np.nan, "beta_block": np.nan},
                            index=returns.index)

    rolling = joint.rolling(window, min_periods=minimum)
    mean_y, mean_1, mean_2 = rolling["y"].mean(), rolling["x1"].mean(), rolling["x2"].mean()
    var_1, var_2 = rolling["x1"].var(ddof=1), rolling["x2"].var(ddof=1)
    covariances = rolling.cov().unstack()
    cov_12 = covariances[("x1", "x2")]
    cov_1y = covariances[("x1", "y")]
    cov_2y = covariances[("x2", "y")]

    determinant = var_1 * var_2 - cov_12 ** 2
    # Определитель сравнивается не с нулём, а с произведением дисперсий: сам по
    # себе он мал просто потому, что доходности малы, и абсолютный порог
    # объявил бы вырожденным любое окно.
    degenerate = (determinant / (var_1 * var_2)).abs() < 1e-8

    beta = ((var_2 * cov_1y - cov_12 * cov_2y) / determinant).mask(degenerate)
    beta_block = ((var_1 * cov_2y - cov_12 * cov_1y) / determinant).mask(degenerate)

    single = (cov_1y / var_1).where(var_1 > 0)
    beta = beta.fillna(single)
    beta_block = beta_block.fillna(0.0)

    alpha = mean_y - beta * mean_1 - beta_block * mean_2
    estimates = pd.DataFrame({"alpha": alpha.shift(1), "beta": beta.shift(1),
                              "beta_block": beta_block.shift(1)})
    return estimates.reindex(returns.index)


def residuals(asset: Asset, frame: pd.DataFrame, factor: pd.Series,
              block_factor: pd.Series | None = None) -> pd.DataFrame:
    """Остаток e и всё, что нужно аппарату п.3.1 для его обработки."""
    out = frame.copy()
    if out.empty:
        return out.assign(alpha=pd.Series(dtype="float64"),
                          beta=pd.Series(dtype="float64"),
                          beta_block=pd.Series(dtype="float64"),
                          e_resid=pd.Series(dtype="float64"),
                          e_resid_w=pd.Series(dtype="float64"),
                          sigma_lt_resid=pd.Series(dtype="float64"))

    factor_series = pd.Series(factor.reindex(out["hour_utc"]).to_numpy(), index=out.index)

    if block_factor is None:
        estimates = rolling_beta(out["r"], factor_series)
        estimates["beta_block"] = 0.0
        block_series = pd.Series(0.0, index=out.index)
    else:
        block_series = pd.Series(block_factor.reindex(out["hour_utc"]).to_numpy(),
                                 index=out.index)
        estimates = rolling_two_factor(out["r"], factor_series, block_series)

    out["alpha"] = estimates["alpha"].to_numpy()
    out["beta"] = estimates["beta"].to_numpy()
    out["beta_block"] = estimates["beta_block"].to_numpy()
    out["e_resid"] = out["r"] - (out["alpha"] + out["beta"] * factor_series
                                 + out["beta_block"] * block_series)

    # Собственная долгосрочная сигма остатка, по данным строго до текущего бара.
    out["sigma_lt_resid"] = (out["e_resid"].shift(1)
                             .rolling(windows.SIGMA_LT_BARS,
                                      min_periods=windows.SIGMA_LT_MIN_BARS)
                             .std(ddof=1))

    # Винзоризация остатка по п.2.5 - со своим MAD и своей нижней отсечкой.
    # Нижняя граница берёт ту же доходность в половину тика: мельче шага цены
    # остаток всё равно не бывает.
    from meals.returns import _rolling_mad

    mad_24 = _rolling_mad(out["e_resid"], windows.MAD_WINDOW)
    half_tick = np.log1p(asset.tick_size / 2 / out["close"])
    eps = np.fmax(0.2 * out["sigma_lt_resid"], half_tick)
    mad_eff = np.maximum(mad_24, eps)
    limit = 5 * mad_eff
    out["e_resid_w"] = np.where(out["e_resid"].abs() > limit,
                                np.sign(out["e_resid"]) * limit, out["e_resid"])
    out.loc[mad_eff.isna(), "e_resid_w"] = out["e_resid"][mad_eff.isna()]
    return out


def score_residuals(frame: pd.DataFrame, w_asset: int) -> pd.DataFrame:
    """Прогоняет ряд остатков через аппарат п.3.1 с собственными состояниями.

    Q95_resid по п.3.6 считается, хранится и экспортируется ИСКЛЮЧИТЕЛЬНО для
    диагностики: ни в одном условии документа он не участвует. В условии
    генерации событий (п.8.2) работает только Q99_resid.
    """
    from meals import zscore

    out = frame.copy()
    if out.empty:
        return out.assign(z_resid=pd.Series(dtype="float64"),
                          q95_resid=pd.Series(dtype="float64"),
                          q99_resid=pd.Series(dtype="float64"))

    z, sigma_eff = zscore.ewma_state(
        out["e_resid"].to_numpy(dtype="float64"),
        out["e_resid_w"].to_numpy(dtype="float64"),
        out["sigma_lt_resid"].to_numpy(dtype="float64"))
    out["z_resid"] = z
    out["sigma_eff_resid"] = sigma_eff
    # Та же причина, что и у ценового ряда: без нижней отсечки, которой нет до
    # появления sigma_LT, Z на разогреве бессмысленен и портит перцентили.
    out.loc[out["sigma_lt_resid"].isna(), ["z_resid", "sigma_eff_resid"]] = np.nan

    q95, q99 = zscore.adaptive_thresholds(out["z_resid"].abs(), w_asset)
    out["q95_resid"], out["q99_resid"] = q95, q99
    return out

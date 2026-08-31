"""Адаптивный EWMA Z-score и адаптивные пороги (ТЗ п.3.1).

Порядок вычислений здесь не деталь реализации, а само содержание метода.

Z считается ДО обновления состояния, против параметров предыдущего бара. Если
сделать наоборот, текущее движение сначала попадёт в оценку нормы, а потом
будет сравниваться с ней же - и чем крупнее событие, тем сильнее оно поднимет
собственный знаменатель. Детектор, устроенный так, тем хуже видит движение, чем
оно больше; это называется look-ahead bias и это ровно то, от чего защищает
формулировка "out-of-sample" в заголовке п.3.1.

В обновление состояния идёт винзоризованная r_w (п.2.5), а в расчёт Z -
исходная r. Одна и та же величина в двух ролях: как наблюдение, которое надо
оценить, и как вклад в оценку нормы. Ограничивается только вторая роль.

И ещё одна тонкость, которую легко потерять: в обновление дисперсии
подставляется ewma_mean ПРЕДЫДУЩЕГО бара, не только что пересчитанное. ТЗ
оговаривает это отдельной строкой, потому что обе формы выглядят одинаково
естественно, а результаты у них разные.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from meals import windows

# Доля долгосрочной сигмы, ниже которой знаменатель Z не опускается (п.3.1).
# Без этой отсечки в затишье EWMA-дисперсия схлопывается, и Z взлетает не
# потому, что движение большое, а потому, что знаменатель стал крошечным.
SIGMA_EFF_FLOOR = 0.2


def ewma_state(returns: np.ndarray, winsorized: np.ndarray, sigma_lt: np.ndarray,
               lam: float = windows.LAMBDA) -> tuple[np.ndarray, np.ndarray]:
    """Прогоняет автомат п.3.1 по ряду и возвращает (Z, sigma_eff).

    Последовательный проход - это слой B из п.6.1: состояние на баре зависит от
    состояния на предыдущем, и векторизовать это без потери смысла нельзя.

    Начальное состояние не имеет значения: при lambda = 2/25 период
    полураспада около восьми баров, так что к концу разогрева в 500 баров
    (п.6.6) вес начального значения порядка 1e-18.
    """
    n = len(returns)
    z = np.full(n, np.nan)
    sigma_eff_out = np.full(n, np.nan)
    mean = 0.0
    var = float(returns[np.isfinite(returns)][0] ** 2) if np.isfinite(returns).any() else 0.0

    for i in range(n):
        r = returns[i]
        if not np.isfinite(r):
            continue

        # Шаг 1: Z против параметров ПРЕДЫДУЩЕГО бара.
        floor = SIGMA_EFF_FLOOR * sigma_lt[i] if np.isfinite(sigma_lt[i]) else 0.0
        sigma_eff = max(np.sqrt(var), floor)
        if sigma_eff > 0:
            z[i] = (r - mean) / sigma_eff
            sigma_eff_out[i] = sigma_eff

        # Шаг 2: обновление состояния, на винзоризованной доходности и со
        # СТАРЫМ средним в формуле дисперсии.
        r_w = winsorized[i] if np.isfinite(winsorized[i]) else r
        previous_mean = mean
        mean = lam * r_w + (1 - lam) * mean
        var = lam * (r_w - previous_mean) ** 2 + (1 - lam) * var

    return z, sigma_eff_out


def adaptive_thresholds(abs_z: pd.Series, window: int,
                        lam_q: float = windows.LAMBDA_Q) -> tuple[pd.Series, pd.Series]:
    """Сглаженные пороги Q95 и Q99 (п.3.1).

    Перцентили считаются на скользящем окне |Z|, ИСКЛЮЧАЯ текущий бар: порог,
    в который включено оцениваемое наблюдение, подстраивается под него и тем
    самым занижает собственное срабатывание.

    Сглаживание по ТЗ обязательно. Без него порог дёргается вслед за тем, какие
    именно значения вошли и вышли из окна, и одно и то же движение может
    оказаться то значимым, то нет - только из-за того, что произошло 840 баров
    назад.
    """
    raw = abs_z.shift(1).rolling(window, min_periods=window)
    q95_raw = raw.quantile(0.95)
    q99_raw = raw.quantile(0.99)
    # ewm с adjust=False - это ровно рекуррентная формула ТЗ
    # Q_t = lambda_q * Q_raw_t + (1 - lambda_q) * Q_{t-1}.
    q95 = q95_raw.ewm(alpha=lam_q, adjust=False).mean()
    q99 = q99_raw.ewm(alpha=lam_q, adjust=False).mean()
    return q95, q99


def breaches(abs_z: pd.Series, abs_r: pd.Series, sigma_lt: pd.Series,
             q95: pd.Series, q99: pd.Series) -> pd.DataFrame:
    """Гибридное условие значимости п.3.1: относительная И абсолютная нога
    одновременно.

    Возвращает NULL (pd.NA), а не False, там где условие не оценено - пороги
    ещё не набрались или нет долгосрочной сигмы. По п.1.2 это разные вещи:
    "не превысило порог" и "порога пока не существует".
    """
    known = q95.notna() & q99.notna() & sigma_lt.notna() & abs_z.notna()
    q99_hit = (abs_z > q99) & (abs_r >= windows.ABS_LEG_Q99 * sigma_lt)
    q95_hit = (abs_z > q95) & (abs_r >= windows.ABS_LEG_Q95 * sigma_lt)
    return pd.DataFrame({
        "breach_q99": q99_hit.where(known, pd.NA).astype("boolean"),
        "breach_q95": q95_hit.where(known, pd.NA).astype("boolean"),
    })


def compute(frame: pd.DataFrame, w_asset: int) -> pd.DataFrame:
    """Собирает Z, пороги и признаки пробоя для одного ряда.

    На вход идёт кадр после winsorize: с колонками r, r_w и sigma_lt. Тот же
    аппарат применяется к ряду остатков SAED (п.3.6) и к ряду VIX (п.4.4) - у
    них свои состояния и свои пороги, но формулы те же, поэтому функция не
    знает, чей ряд обрабатывает.
    """
    out = frame.copy()
    if out.empty:
        return out.assign(z=pd.Series(dtype="float64"), q95=pd.Series(dtype="float64"),
                          q99=pd.Series(dtype="float64"),
                          breach_q95=pd.Series(dtype="boolean"),
                          breach_q99=pd.Series(dtype="boolean"))

    z, sigma_eff = ewma_state(out["r"].to_numpy(dtype="float64"),
                              out["r_w"].to_numpy(dtype="float64"),
                              out["sigma_lt"].to_numpy(dtype="float64"))
    out["z"] = z
    out["sigma_eff"] = sigma_eff

    # Пока sigma_LT не набралась, у знаменателя нет нижней отсечки - той самой,
    # что не даёт EWMA-дисперсии схлопнуться. На замерших котировках это даёт
    # бессмысленные значения: у EUR/USD 1 января 2021 после четырёх часов
    # стоящей цены один обычный полупроцентный сдвиг дал Z в 224. Само по себе
    # это безвредно - пробои там всё равно NULL, - но такие Z попадали в окно
    # перцентилей и сдвигали пороги на тысячи баров вперёд. По п.6.6 час до
    # first_valid_hour вообще не участвует в статистике, поэтому Z на разогреве
    # не просто не используется, а не существует.
    out.loc[out["sigma_lt"].isna(), ["z", "sigma_eff"]] = np.nan

    abs_z = out["z"].abs()
    q95, q99 = adaptive_thresholds(abs_z, w_asset)
    out["q95"], out["q99"] = q95, q99
    return out.join(breaches(abs_z, out["r"].abs(), out["sigma_lt"], q95, q99))

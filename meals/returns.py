"""Доходности, гэп-канал и винзоризация (ТЗ п.2.4, п.2.5).

Главная идея п.2.4 - разделить два движения, которые обычная доходность
смешивает в одно. Между закрытием прошлой сессии и открытием следующей цена
меняется без торгов: выходят новости, происходит отсечка дивиденда, идёт торг
на другой площадке. Если сложить этот разрыв с внутричасовым движением, каждое
утро выглядело бы аномалией.

Поэтому первый бар сессии раскладывается на два канала:
    гэп-канал:      r_gap = ln(open_первого / close_последнего прошлой сессии)
    внутричасовой:  r_t   = ln(close_первого / open_первого)
и в Z-score, CSV, PCA и SAED подаётся ТОЛЬКО r_t. Гэп-канал ведётся отдельно,
логируется и баллов в SI-Index не даёт.

Это же решает и проблему нескорректированных рядов. Биржевые фонды приходят от
источника без коррекции на дивиденды, и в день отсечки цена механически падает
на размер выплаты. Но падение случается между сессиями, то есть попадает
именно в гэп-канал, а не в r_t. Вдобавок такие даты помечаются по таблице
корпоративных действий, и их гэп исключается - иначе распределение самого
гэп-канала перекосили бы регулярные дивидендные ступеньки.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from meals import windows
from meals.basket import Asset

HOUR = 3600
NYSE_TZ = ZoneInfo("America/New_York")


def session_ids(asset: Asset, hours: pd.Series, anchor_tz: str = "America/New_York") -> pd.Series:
    """Номер сессии для каждого часа. Первый бар сессии - тот, у кого номер
    отличается от предыдущего.

    У биржевых фондов сессия это торговый день по времени биржи. У валютных пар
    сессия - вся торговая неделя целиком, с вечера воскресенья до вечера
    пятницы: внутри неё торги не прерываются, и единственный разрыв за неделю -
    выходные. У круглосуточной крипты сессий нет вовсе, разрывов тоже, поэтому
    все часы принадлежат одной бесконечной сессии.
    """
    if asset.session_template == "crypto_24_7":
        return pd.Series(0, index=hours.index)

    moments = pd.to_datetime(hours, unit="s", utc=True)
    if asset.session_template == "us_equity":
        local = moments.dt.tz_convert(NYSE_TZ)
        return local.dt.strftime("%Y-%m-%d")

    if asset.session_template == "fx_continuous":
        from meals.sessions import reference_week_bounds
        return pd.Series(
            [reference_week_bounds(m.to_pydatetime(), anchor_tz)[0] for m in moments],
            index=hours.index)

    raise ValueError(f"{asset.ticker}: неизвестный шаблон сессии '{asset.session_template}'")


def split_channels(asset: Asset, usable: pd.DataFrame,
                   action_days: set[date] | None = None,
                   anchor_tz: str = "America/New_York") -> pd.DataFrame:
    """Считает r и r_gap по правилам п.2.4.

    На вход идут ТОЛЬКО пригодные бары (прошедшие гейт п.2.6 и лежащие внутри
    сессии): доходность через невалидный или послеторговый бар не имеет смысла.

    Пропуск бара внутри сессии не заполняется вперёд - forward-fill к
    доходностям запрещён п.2.4. Доходность просто считается от последнего
    валидного закрытия, то есть охватывает два часа вместо одного; это честнее,
    чем выдумывать несуществующее закрытие.
    """
    out = usable.copy().sort_values("hour_utc").reset_index(drop=True)
    if out.empty:
        return out.assign(r=pd.Series(dtype="float64"), r_gap=pd.Series(dtype="float64"),
                          is_session_open=pd.Series(dtype=bool),
                          gap_masked=pd.Series(dtype=bool))

    session = session_ids(asset, out["hour_utc"], anchor_tz)
    is_open = session != session.shift(1)
    is_open.iloc[0] = True  # первый бар истории: предыдущей сессии нет

    prev_close = out["close"].shift(1)
    out["r"] = np.where(is_open,
                        np.log(out["close"] / out["open"]),
                        np.log(out["close"] / prev_close))
    out["r_gap"] = np.where(is_open, np.log(out["open"] / prev_close), np.nan)
    # У самого первого бара истории предыдущего закрытия нет ни для одного
    # канала - обе величины неопределены, а не равны нулю.
    out.loc[0, "r_gap"] = np.nan
    out.loc[0, "r"] = np.nan
    out["is_session_open"] = is_open

    # Гэп в день корпоративного действия отражает выплату, а не движение рынка.
    # Маскируется только он: внутричасовая доходность первого бара к отсечке
    # отношения не имеет, отбрасывать её вместе с гэпом значило бы терять
    # исправные данные.
    if action_days:
        local_day = pd.to_datetime(out["hour_utc"], unit="s", utc=True)
        local_day = local_day.dt.tz_convert(NYSE_TZ).dt.date
        masked = is_open & local_day.isin(action_days)
        out["gap_masked"] = masked
        out.loc[masked, "r_gap"] = np.nan
    else:
        out["gap_masked"] = False
    return out


def _rolling_mad(series: pd.Series, window: int) -> pd.Series:
    """Медианное абсолютное отклонение на скользящем окне, БЕЗ текущего бара -
    окно заканчивается на предыдущем (п.2.5)."""
    def mad(values: np.ndarray) -> float:
        median = np.median(values)
        return float(np.median(np.abs(values - median)))
    return series.shift(1).rolling(window).apply(mad, raw=True)


def winsorize(asset: Asset, frame: pd.DataFrame) -> pd.DataFrame:
    """Винзоризация доходностей по п.2.5.

    Смысл в том, ЧТО именно ограничивается. В обновление состояния EWMA идёт
    подрезанная r_w: один экстремальный час не должен раздувать оценку нормы на
    много баров вперёд, иначе после каждого шока система на время слепнет. А в
    расчёт Z, всех триггеров, SAED и экспорта идёт ИСХОДНАЯ r - подрезать то,
    что мы как раз и хотим задетектировать, было бы бессмысленно.

    Нижняя отсечка eps_MAD не даёт границе схлопнуться. В тихие часы, когда
    котировка стоит, MAD_24 обращается в ноль, и без отсечки любое движение
    оказывалось бы "больше пяти MAD". Отсечка берёт большее из двух: пятой
    части долгосрочной сигмы и доходности в половину тика при текущей цене -
    то есть шага, мельче которого инструмент физически двигаться не умеет.
    """
    out = frame.copy()
    if out.empty or "r" not in out:
        return out.assign(mad_eff=pd.Series(dtype="float64"),
                          r_w=pd.Series(dtype="float64"))

    returns = out["r"]
    mad_24 = _rolling_mad(returns, windows.MAD_WINDOW)

    # sigma_LT считается по данным строго до текущего бара - той же дисциплины
    # out-of-sample, что и всё остальное в п.3.1.
    sigma_lt = (returns.shift(1)
                .rolling(windows.SIGMA_LT_BARS, min_periods=windows.SIGMA_LT_MIN_BARS)
                .std(ddof=1))

    half_tick_return = np.log1p(asset.tick_size / 2 / out["close"])
    # fmax, а не maximum: пока истории меньше 720 баров, sigma_LT не определена,
    # и обычный максимум вернул бы NaN - то есть отсечка исчезла бы целиком, а
    # вместе с ней и винзоризация, ровно на разогреве EWMA, где один выброс
    # портит оценку нормы надолго. fmax игнорирует NaN и оставляет вторую
    # половину отсечки - доходность в половину тика.
    eps_mad = np.fmax(0.2 * sigma_lt, half_tick_return)
    # А здесь именно maximum: пока не набралось 24 бара, MAD_24 неизвестен, и
    # предела нет вовсе. Подставить вместо него одну лишь отсечку в полтика
    # значило бы подрезать почти каждый бар: у фонда за 770 долларов это 0.003%.
    mad_eff = np.maximum(mad_24, eps_mad)

    limit = 5 * mad_eff
    out["mad_eff"] = mad_eff
    out["sigma_lt"] = sigma_lt
    out["r_w"] = np.where(returns.abs() > limit,
                          np.sign(returns) * limit,
                          returns)
    # Пока окна не набрались, предела нет и подрезать нечем: r_w равна r.
    out.loc[mad_eff.isna(), "r_w"] = returns[mad_eff.isna()]
    return out

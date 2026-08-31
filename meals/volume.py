"""Робастный профиль объёма (ТЗ п.3.5).

Объём торгов подчиняется резкой внутридневной сезонности: первый и последний
час американской сессии кратно активнее середины дня, и сравнивать текущий час
с "обычным объёмом" вообще - значит каждый день объявлять открытие и закрытие
аномалией, а тихий полдень никогда не замечать. Поэтому норма берётся не общая,
а по КАЖДОМУ ЛОКАЛЬНОМУ БИРЖЕВОМУ ЧАСУ отдельно: полдень сравнивается с
полднями, открытие - с открытиями.

Час именно локальный биржевой, а не UTC: сезонность привязана к расписанию
торгов, а оно живёт в местном времени и переезжает относительно UTC дважды в
год при переходе на летнее время.

Оценка робастная - медиана и MAD вместо среднего и стандартного отклонения. По
объёму это принципиальнее, чем по цене: один день с новостью даёт всплеск в
десятки раз, и обычное среднее после него надолго перестаёт быть нормой.
Логарифм ln(1 + V) берётся до всего остального, потому что распределение объёма
скошено вправо на порядки величины.
"""
from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from meals import windows
from meals.basket import Asset

MAD_TO_SIGMA = 1.4826  # приводит MAD к масштабу стандартного отклонения


def _mad(values: np.ndarray) -> float:
    return float(np.median(np.abs(values - np.median(values))))


def robust_volume_z(asset: Asset, frame: pd.DataFrame, exchange_tz: str,
                    full_days: set[date] | None = None,
                    profile_days: int = windows.VOLUME_PROFILE_DAYS) -> pd.Series:
    """V_R по п.3.5: насколько объём часа выделяется среди объёмов этого же
    локального часа за последние 20 ПОЛНЫХ торговых дней.

    Полных - значит без полусессий и праздников: в сокращённый день объём
    заведомо меньше, и держать такие дни в норме означало бы занижать её для
    всех остальных. Сами бары полусессии при этом оцениваются на общих
    основаниях, просто по профилю, собранному из полных дней.

    Текущий день в профиль не входит: норма, в которую включено оцениваемое
    наблюдение, подстраивается под него.

    У инструментов без объёма (спот-форекс) возвращается NULL по всему ряду -
    по п.3.5 подтверждение объёмом для них не оценивается вовсе, а не считается
    несработавшим.
    """
    if frame.empty:
        return pd.Series(dtype="float64", index=frame.index)
    if not asset.has_volume:
        return pd.Series(np.nan, index=frame.index)

    local = pd.to_datetime(frame["hour_utc"], unit="s", utc=True).dt.tz_convert(
        ZoneInfo(exchange_tz))
    work = pd.DataFrame({
        # Ключ дня - datetime64, а не date: merge_asof ниже требует числовой
        # или временной ключ и на объектном типе отказывается работать.
        "day": local.dt.tz_localize(None).dt.normalize(),
        "local_hour": local.dt.hour,
        "ln_volume": np.log1p(frame["volume"].astype("float64")),
    }, index=frame.index)
    work["is_full_day"] = (local.dt.date.isin(full_days) if full_days is not None
                           else True)

    result = pd.Series(np.nan, index=frame.index)
    for _, group in work.groupby("local_hour", sort=False):
        group = group.sort_values("day")
        full = group[group["is_full_day"]]
        if len(full) < profile_days:
            continue

        window = full["ln_volume"].rolling(profile_days, min_periods=profile_days)
        profile = pd.DataFrame({
            "day": full["day"].to_numpy(),
            "median_h": window.median().to_numpy(),
            "mad_h": window.apply(_mad, raw=True).to_numpy(),
        }).dropna()
        if profile.empty:
            continue

        # merge_asof без точного совпадения: профиль берётся по дням СТРОГО
        # раньше текущего.
        matched = pd.merge_asof(
            group.reset_index().sort_values("day"),
            profile.sort_values("day"),
            on="day", allow_exact_matches=False, direction="backward",
        ).set_index("index")

        scaled_mad = MAD_TO_SIGMA * matched["mad_h"]
        # Вырожденный профиль: объём этого часа не менялся 20 дней подряд.
        # Делить на такое нельзя, а объявлять любое отклонение бесконечным - тем
        # более, поэтому по п.3.5 V_R = 0.
        degenerate = scaled_mad < windows.VOLUME_MAD_FLOOR
        values = (matched["ln_volume"] - matched["median_h"]) / scaled_mad
        values[degenerate] = 0.0
        result.loc[matched.index] = values

    return result


def full_session_days(session_table: dict[date, object]) -> set[date]:
    """Дни полных сессий: и не праздник (день есть в таблице), и не полусессия."""
    return {day for day, session in session_table.items() if not session.is_early_close}

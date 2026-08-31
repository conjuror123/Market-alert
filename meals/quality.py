"""Гейт качества данных и принадлежность часа сессии (ТЗ п.2.2, п.2.6).

Две разные вещи, которые удобно считать вместе, потому что обе отвечают на
вопрос "участвует ли этот бар в расчётах":

- ВАЛИДНОСТЬ - свойство самого бара: цены положительны, OHLC согласованы,
  объём неотрицателен, метка времени не повторяется. Невалидный бар не
  участвует ни в чём и не обновляет состояний (п.2.6).
- ПРИНАДЛЕЖНОСТЬ СЕССИИ - свойство часа: по п.2.2 часы вне торговой сессии
  актива исключаются из EWMA, объёма, CSV, PCA и кластерного сдвига.

Второе оказалось не формальностью. Источник отдаёт бары и после закрытия
полусессии: 26 ноября 2021 биржа закрылась в 13:00 по Нью-Йорку, а бары за
14:00 и 15:00 всё равно пришли - с нулевым объёмом и ползущей ценой. Без
фильтра сессии эти часы попали бы в состояние EWMA и в профиль объёма как
настоящая торговля.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from meals import sessions as sessions_mod
from meals.basket import Asset

HOUR = 3600
NYSE_TZ = ZoneInfo("America/New_York")


def _session_bounds_utc(day: date, session: sessions_mod.Session) -> tuple[int, int]:
    """Границы сессии в epoch UTC. Хранятся в локальном времени биржи и
    переводятся на лету - хранить их сразу в UTC запрещено п.2.2."""
    def at(hhmm: str) -> int:
        hour, minute = (int(x) for x in hhmm.split(":"))
        return int(datetime.combine(day, time(hour, minute), tzinfo=NYSE_TZ).timestamp())
    return at(session.local_open), at(session.local_close)


def in_session(asset: Asset, hours: pd.Series,
               session_table: dict[date, sessions_mod.Session] | None = None,
               anchor_tz: str = "America/New_York") -> pd.Series:
    """Попадает ли час (по моменту ОТКРЫТИЯ бара, п.1.2) в сессию актива.

    Час считается торговым, если интервал [h, h+1) пересекается с сессией по
    ПОЛУОТКРЫТОМУ правилу: h < закрытие и h + час > открытие. Закрывающий
    аукцион при этом не теряется - он печатается до момента закрытия и потому
    попадает в последний бар, который целиком лежит внутри сессии. А вот час,
    НАЧИНАЮЩИЙСЯ ровно в момент закрытия, - это уже послеторговые сделки, и
    его правило отбрасывает.
    """
    if asset.session_template == "crypto_24_7":
        return pd.Series(True, index=hours.index)

    if asset.session_template == "fx_continuous":
        # Форекс торгуется непрерывно с вс 17:00 до пт 17:00 по времени якорной
        # биржи - это ровно эталонная неделя корзины из п.2.2.
        return hours.map(lambda h: sessions_mod.is_reference_hour(int(h), anchor_tz))

    if asset.session_template != "us_equity":
        raise ValueError(f"{asset.ticker}: неизвестный шаблон сессии "
                         f"'{asset.session_template}'")

    table = session_table if session_table is not None else sessions_mod.load_sessions()
    local_days = pd.to_datetime(hours, unit="s", utc=True).dt.tz_convert(NYSE_TZ).dt.date
    bounds = {d: _session_bounds_utc(d, s) for d, s in table.items()}

    def inside(hour: int, day: date) -> bool:
        window = bounds.get(day)
        if window is None:
            return False
        opened, closed = window
        return hour < closed and hour + HOUR > opened

    return pd.Series([inside(int(h), d) for h, d in zip(hours, local_days)],
                     index=hours.index)


def invalid_reasons(asset: Asset, frame: pd.DataFrame) -> pd.Series:
    """Причина невалидности каждого бара, пустая строка у исправных (п.2.6).

    Строкой, а не флагом: когда бар выбрасывается из расчётов, нужно уметь
    ответить почему, не переоткрывая данные.
    """
    reasons = pd.Series("", index=frame.index, dtype="object")
    if frame.empty:
        return reasons

    prices = frame[["open", "high", "low", "close"]]
    reasons[prices.le(0).any(axis=1)] = "цена не положительна"

    # Согласованность OHLC с допуском в полтика: источник округляет поля бара
    # независимо, и расхождение меньше тика - артефакт округления, а не
    # сломанный бар (см. meals/audit.py и docs/meals-otstupleniya.md).
    tolerance = asset.tick_size / 2
    inconsistent = (
        (frame["low"] > prices[["open", "close"]].min(axis=1) + tolerance)
        | (prices[["open", "close"]].max(axis=1) > frame["high"] + tolerance)
    )
    reasons[inconsistent & (reasons == "")] = "OHLC не согласованы"

    reasons[(frame["volume"] < 0) & (reasons == "")] = "объём отрицателен"
    reasons[frame["hour_utc"].duplicated(keep="last") & (reasons == "")] = "повтор часа"
    return reasons


def expected_hours(asset: Asset, first_hour: int, last_hour: int,
                   session_table: dict[date, sessions_mod.Session] | None = None,
                   anchor_tz: str = "America/New_York") -> list[int]:
    """Часы, которые ДОЛЖНЫ быть у актива в диапазоне - то есть все часы его
    сессии. Разница с фактическими и есть is_missing (п.2.4)."""
    hours = pd.Series(range(first_hour - first_hour % HOUR, last_hour + HOUR, HOUR))
    mask = in_session(asset, hours, session_table, anchor_tz)
    return [int(h) for h in hours[mask]]


def apply_gate(asset: Asset, frame: pd.DataFrame,
               session_table: dict[date, sessions_mod.Session] | None = None,
               anchor_tz: str = "America/New_York") -> pd.DataFrame:
    """Добавляет к барам колонки гейта: причину невалидности, признак сессии и
    итоговое is_usable. В расчёты идут только строки с is_usable."""
    out = frame.copy()
    out["invalid_reason"] = invalid_reasons(asset, frame)
    out["in_session"] = (in_session(asset, frame["hour_utc"], session_table, anchor_tz)
                         if not frame.empty else pd.Series(dtype=bool))
    out["is_usable"] = (out["invalid_reason"] == "") & out["in_session"]
    return out

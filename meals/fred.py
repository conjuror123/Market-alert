"""Клиент FRED для дневного ряда VIX (ТЗ п.4.4).

Почему FRED, а не биржевой фонд на фьючерсы VIX: это настоящий индекс с 1990
года из официального источника, без контанго-дрейфа, которым страдает любой
фьючерсный ETF. Цена этого выбора - две особенности, обе зафиксированы как
отступление от буквы п.4.4:

1. Ряд ДНЕВНОЙ. Внутридневного VIX на FRED нет ни в одной серии - проверено
   поиском по всем сериям волатильности CBOE, все они "Daily, Close". Значит
   скачок VIX определяется аппаратом п.3.1 на дневных барах, а не на часовых.

2. Значение публикуется на следующий рабочий день, утром по Чикаго. Поле
   realtime_start у FRED для этой серии проставлено задним числом (равно дате
   самого наблюдения), поэтому доверять ему как дате публикации нельзя -
   момент доступности вычисляется явно, функцией available_at ниже. Иначе
   бэктест применял бы множитель в час, когда значения ещё не существовало,
   то есть заглядывал бы в будущее.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import requests

API_ROOT = "https://api.stlouisfed.org/fred"

# Публикация - утром следующего рабочего дня по Чикаго. 14:00 UTC - это 08:00
# или 09:00 по Чикаго в зависимости от сезона; берём с запасом на час позже
# наблюдавшегося времени обновления, чтобы никогда не считать значение
# доступным раньше, чем оно реально появилось.
PUBLICATION_HOUR_UTC = 15


class FredError(RuntimeError):
    pass


def available_at(observation_day: date) -> int:
    """Момент (epoch, UTC), начиная с которого значение за `observation_day`
    известно системе. Следующий рабочий день, PUBLICATION_HOUR_UTC.

    Выходные пропускаются: значение за пятницу публикуется в понедельник.
    """
    day = observation_day + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return int(datetime.combine(day, time(PUBLICATION_HOUR_UTC), tzinfo=timezone.utc).timestamp())


def fetch_series(
    series_id: str,
    api_key: str,
    start: date,
    session: requests.Session | None = None,
    timeout: int = 40,
) -> pd.DataFrame:
    """Забирает наблюдения серии начиная с `start`.

    Возвращает колонки: day (epoch полуночи UTC того дня, к которому относится
    значение), close, available_at (epoch момента, с которого значение можно
    использовать, см. модульную строку).
    """
    if not api_key:
        raise FredError("Не задан ключ FRED (FRED_API_KEY)")

    sess = session or requests
    resp = sess.get(
        f"{API_ROOT}/series_observations",
        params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start.isoformat(),
        },
        timeout=timeout,
        headers={"User-Agent": "market-alert-bot"},
    )
    if resp.status_code != 200:
        raise FredError(f"{series_id}: неожиданный статус {resp.status_code}: {resp.text[:200]}")

    rows = []
    for obs in resp.json().get("observations", []):
        # Пропуски FRED кодирует точкой - это выходные и праздники, когда
        # индекс не рассчитывался, а не потеря данных.
        if obs.get("value") in (None, "", "."):
            continue
        day = datetime.strptime(obs["date"], "%Y-%m-%d").date()
        rows.append({
            "day": int(datetime.combine(day, time(0), tzinfo=timezone.utc).timestamp()),
            "close": float(obs["value"]),
            "available_at": available_at(day),
        })
    if not rows:
        raise FredError(f"{series_id}: FRED не вернул ни одного наблюдения с {start}")
    return pd.DataFrame(rows).astype({"day": "int64", "close": "float64", "available_at": "int64"})

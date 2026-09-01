"""Асимметричный календарный мультипликатор (ТЗ п.4.3).

Одно и то же движение значит разное в зависимости от того, когда оно
произошло. Скачок за десять минут до публикации данных по инфляции и такой же
скачок в тихий вторник - разные события, хотя цифры одинаковые. Множитель
поднимает вес часов вокруг важных публикаций.

Асимметрия намеренная: окно ДО публикации шире окна ПОСЛЕ (шесть часов против
трёх у важных событий). Рынок готовится к выходу данных заранее - позиции
двигают загодя, - а после публикации реакция укладывается быстро.

Окна здесь единственное в ТЗ исключение из правила единиц: они измеряются в
КАЛЕНДАРНЫХ часах и не сокращаются, даже если пересекают закрытие рынка или
выходные. Публикация макроданных не подчиняется биржевому расписанию.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from meals import windows

DEFAULT_CALENDAR_PATH = os.path.join("data", "economic_calendar", "calendar.ndjson")

HOUR = 3600

# Пик множителя в момент публикации. Помечены в ТЗ звёздочкой.
PEAK = {"High": 1.8, "Medium": 1.5}
BEFORE = {"High": windows.CALENDAR_HIGH_BEFORE, "Medium": windows.CALENDAR_MEDIUM_BEFORE}
AFTER = {"High": windows.CALENDAR_HIGH_AFTER, "Medium": windows.CALENDAR_MEDIUM_AFTER}


def multiplier_at(hours_from_event: float, importance: str) -> float:
    """Значение множителя для момента, отстоящего от публикации на
    `hours_from_event` часов (отрицательное - до публикации).

    Функция кусочно-линейная и непрерывная: на обеих границах окна она равна
    единице, поэтому включать границу или нет - на результат не влияет. В самой
    точке публикации обе ветви дают пик, так что отдельной ветви "в T_event" не
    требуется.
    """
    if importance not in PEAK:
        return 1.0
    peak, before, after = PEAK[importance], BEFORE[importance], AFTER[importance]
    if -before <= hours_from_event <= 0:
        return 1.0 + (peak - 1.0) * (hours_from_event + before) / before
    if 0 < hours_from_event <= after:
        return peak - (peak - 1.0) * hours_from_event / after
    return 1.0


def load_events(path: str = DEFAULT_CALENDAR_PATH) -> list[tuple[int, str]]:
    """Публикации важности High и Medium: (момент в epoch UTC, важность).

    Low в множителе не участвует - по п.4.3 учитываются только High и Medium.
    """
    if not os.path.exists(path):
        return []
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            importance = record.get("impact")
            if importance not in PEAK:
                continue
            moment = datetime.fromisoformat(record["date"])
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            events.append((int(moment.timestamp()), importance))
    return events


def multiplier_series(hours_utc, path: str = DEFAULT_CALENDAR_PATH) -> dict[int, float]:
    """Множитель для каждого часа: максимум по ВСЕМ накрывающим публикациям.

    Именно максимум, а не произведение: два важных события подряд не делают час
    вдвое значимее, они просто оба говорят, что час важный. Перемножение
    множителей раздувало бы SI-Index в дни, когда публикаций много - а таких
    дней большинство.

    Аргумент функции - t, момент ЗАКРЫТИЯ бара (п.1.2), поэтому от hour_utc,
    хранящего момент открытия, отсчитывается час вперёд.
    """
    events = load_events(path)
    result: dict[int, float] = defaultdict(lambda: 1.0)
    wanted = set(int(h) for h in hours_utc)
    if not wanted:
        return {}

    for moment, importance in events:
        before, after = BEFORE[importance], AFTER[importance]
        # Часы, чьё ЗАКРЫТИЕ попадает в окно события.
        first_close = moment - int(before * HOUR)
        last_close = moment + int(after * HOUR)
        first_hour = (first_close // HOUR) * HOUR - HOUR
        for close in range(first_hour, last_close + HOUR, HOUR):
            hour_utc = close - HOUR
            if hour_utc not in wanted:
                continue
            value = multiplier_at((close - moment) / HOUR, importance)
            if value > result[hour_utc]:
                result[hour_utc] = value
    return {h: result[h] for h in wanted}

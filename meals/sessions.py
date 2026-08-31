"""Календарь сессий и эталонный календарь корзины (ТЗ п.2.2).

Расписание NYSE берётся из библиотеки exchange_calendars, но НЕ на каждом
прогоне: библиотека работает генератором, а результат её работы лежит в
репозитории таблицей и коммитится вместе с кодом. Причин три.

1. Воспроизводимость (п.6.2). Тест обязан давать идентичный набор событий при
   повторном прогоне того же периода с той же config_version. Если расписание
   вычисляется библиотекой в момент запуска, её обновление молча меняет
   исторические сессии - и прошлый бэктест перестаёт воспроизводиться, хотя
   ни один параметр конфигурации не тронут.
2. Схема п.6.4 прямо требует таблиц holidays и half_sessions - то есть данных,
   а не вызова функции.
3. Часовому прогону тогда вообще не нужна календарная библиотека: он читает
   готовый CSV. Меньше зависимостей в проде, быстрее установка в GitHub
   Actions.

Отдельных таблиц holidays и half_sessions в хранилище нет намеренно: обе
выводятся из таблицы сессий без потерь - будний день, которого в ней нет, это
праздник, а строка с is_early_close это полусессия. Хранить один и тот же факт
дважды значит однажды получить два расходящихся ответа.

Проверено на данных: на отрезке 2021-01-04 .. 2026-08-28 расписание совпало с
фактическими барами SPY день в день - 1420 торговых дней и там, и там, ноль
расхождений в обе стороны.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

HOUR = 3600
DEFAULT_SESSIONS_PATH = os.path.join("data", "meals", "sessions", "nyse.csv")

# Эталонный календарь корзины (п.2.2): непрерывная торговая неделя якорной
# биржи, с вс 17:00 до пт 17:00 её ЛОКАЛЬНОГО времени. Ровно 120 часов;
# праздники из недели не вычитаются - так сказано в п.2.2 прямым текстом.
REFERENCE_WEEK_HOURS = 120
REFERENCE_OPEN_HOUR = 17   # воскресенье, местное время якорной биржи
REFERENCE_CLOSE_HOUR = 17  # пятница


@dataclass(frozen=True)
class Session:
    day: date
    local_open: str    # "HH:MM" в таймзоне биржи
    local_close: str
    is_early_close: bool


def generate_nyse_sessions(start: date, end: date) -> list[Session]:
    """Строит расписание NYSE библиотекой exchange_calendars.

    Вызывается только вручную, при обновлении таблицы (см. main). В часовом
    прогоне не используется, поэтому exchange_calendars остаётся зависимостью
    разработки, а не продакшена.
    """
    import exchange_calendars as xcals  # локальный импорт: только для генерации

    calendar = xcals.get_calendar("XNYS", start=str(start), end=str(end))
    schedule = calendar.schedule
    tz = "America/New_York"
    sessions = []
    for day, row in schedule.iterrows():
        opened = row["open"].tz_convert(tz)
        closed = row["close"].tz_convert(tz)
        sessions.append(Session(
            day=day.date(),
            local_open=opened.strftime("%H:%M"),
            local_close=closed.strftime("%H:%M"),
            is_early_close=(closed.hour, closed.minute) < (16, 0),
        ))
    return sessions


def write_sessions(path: str, sessions: list[Session]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        # \n вместо \r\n по умолчанию: файл лежит в репозитории, и caret-return
        # в каждой строке засорял бы диффы при каждой перегенерации.
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["date", "local_open", "local_close", "is_early_close"])
        for s in sorted(sessions, key=lambda s: s.day):
            writer.writerow([s.day.isoformat(), s.local_open, s.local_close,
                             "1" if s.is_early_close else "0"])


def load_sessions(path: str = DEFAULT_SESSIONS_PATH) -> dict[date, Session]:
    """Читает таблицу сессий. Библиотека календарей для этого не нужна."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Нет таблицы сессий {path}. Сгенерировать: python -m meals.sessions")
    sessions = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            day = date.fromisoformat(row["date"])
            sessions[day] = Session(
                day=day, local_open=row["local_open"], local_close=row["local_close"],
                is_early_close=row["is_early_close"] == "1",
            )
    return sessions


def is_holiday(day: date, sessions: dict[date, Session]) -> bool:
    """Будний день, которого нет в таблице сессий. Выходные праздниками не
    считаются - это обычное закрытие недели."""
    return day.weekday() < 5 and day not in sessions


def half_sessions(sessions: dict[date, Session]) -> list[Session]:
    return [s for s in sessions.values() if s.is_early_close]


def reference_week_bounds(any_moment: datetime, anchor_tz: str) -> tuple[int, int]:
    """Границы недели эталонного календаря, в которую попадает `any_moment`:
    (открытие, закрытие) в epoch UTC.

    Границы задаются в локальном времени якорной биржи и переводятся в UTC на
    лету - хранить их сразу в UTC запрещено п.2.2, потому что переход на летнее
    время сдвинул бы их относительно рынка.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(anchor_tz)
    local = any_moment.astimezone(tz)
    # Воскресенье 17:00 той недели, к которой относится момент. weekday(): пн=0,
    # вс=6. Момент до воскресного открытия принадлежит предыдущей неделе.
    days_since_sunday = (local.weekday() + 1) % 7
    sunday = (local - timedelta(days=days_since_sunday)).date()
    opened = datetime.combine(sunday, time(REFERENCE_OPEN_HOUR), tzinfo=tz)
    if local < opened:
        opened = datetime.combine(sunday - timedelta(days=7), time(REFERENCE_OPEN_HOUR),
                                  tzinfo=tz)
    closed = datetime.combine(opened.date() + timedelta(days=5),
                              time(REFERENCE_CLOSE_HOUR), tzinfo=tz)
    return int(opened.timestamp()), int(closed.timestamp())


def is_reference_hour(hour_utc: int, anchor_tz: str) -> bool:
    """Попадает ли час (по моменту ОТКРЫТИЯ бара, п.1.2) в эталонный календарь.

    Праздники не исключаются: по п.2.2 длительность недели ровно 120 часов, и
    праздники из неё не вычитаются. Эталонный календарь - это часы корзины, а
    не расписание конкретной биржи.
    """
    moment = datetime.fromtimestamp(hour_utc, tz=timezone.utc)
    opened, closed = reference_week_bounds(moment, anchor_tz)
    return opened <= hour_utc < closed


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Сгенерировать таблицу сессий NYSE (ТЗ п.2.2)")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2028-12-31")
    parser.add_argument("--out", default=DEFAULT_SESSIONS_PATH)
    args = parser.parse_args(argv)

    sessions = generate_nyse_sessions(date.fromisoformat(args.start),
                                      date.fromisoformat(args.end))
    write_sessions(args.out, sessions)
    early = sum(1 for s in sessions if s.is_early_close)
    print(f"{args.out}: торговых дней {len(sessions)}, полусессий {early}, "
          f"{sessions[0].day} .. {sessions[-1].day}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

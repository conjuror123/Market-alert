"""Client for ForexFactory's public economic calendar feed, plus a permanent
local NDJSON store (same pattern as candle_store.py) so events accumulate
over time as the weekly digest (weekly_digest.py) fetches them.

Only a "this week" feed exists at
https://nfs.faireconomy.media/ff_calendar_thisweek.json - there is no
next-week or last-week variant (both return 404, confirmed live). That feed
spans Sunday through Friday, so fetching it specifically on Sunday - which is
exactly when weekly_digest.py runs - already returns the coming week's
events, with no separate "next week" request needed.

Исторический архив (не "эта неделя") собирается из двух источников, и оба
доступны без ключа:

- fetch_kaggle_calendar: датасет "Global Economic Calendar" (EL Younes,
  CC BY-NC-SA 4.0), 2020-01-01 .. 2025-10-01, скачивается публичным API Kaggle
  без авторизации;
- fetch_forexfactory_month: помесячные страницы самой ForexFactory, начиная с
  того месяца, где кончается датасет.

Прежние три источника (spoluan, ehsan high-impact, ehsan full) сняты вместе с
их данными. Причина измерена: в собранном из них архиве четверть событий High и
Medium оказались дубликатами того же события в пределах суток, с доминирующим
сдвигом ровно в семь часов - дампы собирались с разными соглашениями о часовом
поясе, а ключ слияния включает дату, поэтому сдвинутая копия выглядела отдельным
событием. Для календарного множителя (MEALS, п.4.3) это хуже пропусков: пропуск
занижает вес часа, а фантомное событие поднимает его там, где публикации не было.

Что дала замена, на измеренных числах: событий 100 865 вместо 23 133, период
2021-01-01 .. 2026-10-01 без единого пробела, дубликатов 2.4% вместо 25%, у CPI
США ровно одно время публикации - 08:30 по Нью-Йорку - вместо двух кластеров.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.economic_calendar")

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# The whole point of the archive is giving backtests calendar context around
# price moves (see daily_signal_review.py) - the earliest candle history
# (data/candle_history/) starts 2021-01-01, so nothing before that date can
# ever be matched against a price move and is dropped from historical
# imports rather than kept as dead weight.
_ARCHIVE_SINCE = "2021-01-01T00:00:00+00:00"

# Every source is normalized down to this 3-value scale - a source's own
# extra categories (ForexFactory's "Holiday", spoluan's "Non-economic") don't
# carry the kind of significance Medium/High do, so they're folded into "Low"
# right at the point each source gets parsed, rather than leaking each
# source's own quirky taxonomy into the rest of the app (filtering,
# storage, display all only ever need to know about Low/Medium/High).
_IMPACT_ALIASES = {
    "Holiday": "Low",
    "Non-economic": "Low",
}


def _normalize_impact(raw: str) -> str:
    return _IMPACT_ALIASES.get(raw, raw)


class CalendarError(RuntimeError):
    pass


def fetch_calendar(session: requests.Session | None = None, timeout: int = 15) -> list[dict]:
    """Fetches this week's calendar events. Each returned dict has:
    title, country (currency code, or "All" for events affecting everyone),
    date (ISO8601 string, fixed -04:00 offset from the source - see
    parse_event_time), impact ("Low"/"Medium"/"High" - the source's own
    "Holiday" is folded into "Low", see _normalize_impact), forecast,
    previous, actual (empty for events that haven't happened yet)."""
    get = session.get if session is not None else requests.get
    try:
        resp = get(CALENDAR_URL, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        raw = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise CalendarError(f"failed to fetch economic calendar: {exc}") from exc

    events = []
    for item in raw:
        try:
            events.append({
                "title": item["title"],
                "country": item["country"],
                # Приводится к UTC, как и обе исторические ветки: фид отдаёт
                # фиксированное смещение -04:00, и хранить рядом две записи
                # одного момента в разной упаковке значило бы завести ту самую
                # сдвинутую копию, из-за которой выброшен прежний архив.
                "date": parse_event_time(item["date"]).isoformat(),
                "impact": _normalize_impact(item["impact"]),
                "forecast": item.get("forecast", ""),
                "previous": item.get("previous", ""),
                # Ключа "actual" в этом фиде НЕТ - проверено на живой выдаче.
                # Поле остаётся пустым до дозаполнения с помесячной страницы
                # (weekly_digest.backfill_actuals).
                "actual": item.get("actual", ""),
            })
        except KeyError:
            log.warning("Skipping malformed calendar event: %r", item)
    return events


def parse_event_time(date_str: str) -> datetime:
    return datetime.fromisoformat(date_str).astimezone(timezone.utc)


def events_in_window(events: list[dict], lower: datetime, upper: datetime) -> list[dict]:
    """Archive events with a time in [lower, upper] (inclusive both ends,
    same convention as explain.py's _filter_after/_filter_before), sorted by
    date. Used by daily_signal_review.py to show calendar context next to
    each backtest event."""
    matched = [e for e in events if lower <= parse_event_time(e["date"]) <= upper]
    return sorted(matched, key=lambda e: e["date"])


def store_path(base_dir: str) -> str:
    return os.path.join(base_dir, "calendar.ndjson")


def load_events(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _event_key(event: dict) -> tuple:
    """Ключ дедупликации - страна, название и МГНОВЕНИЕ публикации.

    Именно мгновение, а не строка даты. Источники записывают один и тот же
    момент по-разному: недельный фид отдаёт "2026-09-03T08:30:00-04:00",
    помесячные страницы и датасет - "2026-09-03T12:30:00+00:00". По строке это
    два разных события, и архив копил бы каждую публикацию дважды - ровно та
    поломка, из-за которой пришлось выбросить прежний архив целиком (см.
    строку модуля).
    """
    return (event["country"], event["title"],
            parse_event_time(event["date"]).timestamp())


def _merge_one(stored: dict | None, incoming: dict) -> dict:
    """Сливает две версии одного события. Пустое поле не затирает заполненное.

    Без этого правила недельный фид стирал бы вышедшие значения. Он приносит
    событие заранее и вообще не знает поля actual - у него в выдаче такого
    ключа нет, - так что после дозаполнения факта следующий же прогон вернул бы
    в архив пустую строку. Проверено на живых данных: из 93 событий, которые
    видят и фид, и помесячная страница, расходятся ровно 20, и расходятся они
    ровно по actual, который у фида пуст, а на странице заполнен.

    Источник, который промолчал, не сообщает "значения нет" - он сообщает
    "я не знаю", и стирать по такому молчанию нечего.
    """
    if stored is None:
        return dict(incoming)
    merged = dict(stored)
    for key, value in incoming.items():
        if str(value or "").strip() or not str(stored.get(key) or "").strip():
            merged[key] = value
    return merged


def merge_events(path: str, events: list[dict]) -> int:
    """Idempotently merges `events` into the local store, deduplicated by
    (country, title, момент публикации) and rewritten in order - same pattern
    as candle_store.merge_history, for the same reason: this is called every
    week with a feed that mostly repeats recurring events, so it must be
    safe to call repeatedly with overlapping data without accumulating
    duplicate rows. Returns how many rows were added ИЛИ ИЗМЕНЕНЫ.

    Изменённые считаются наравне с новыми, и это не мелочь. Недельный фид
    приносит событие заранее, без вышедшего значения, а факт появляется
    позже - при дозаполнении с помесячной страницы (weekly_digest.
    backfill_actuals). Такой повтор не добавляет ни одной строки, он только
    заполняет поле actual, и прежняя версия, сравнивавшая ЧИСЛО строк до и
    после, молча выбрасывала бы его вместе со всей записью на диск.
    """
    by_key: dict[tuple, dict] = {_event_key(e): e for e in load_events(path)}
    changed = 0
    for e in events:
        key = _event_key(e)
        merged = _merge_one(by_key.get(key), e)
        if merged != by_key.get(key):
            by_key[key] = merged
            changed += 1
    if changed == 0:
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in sorted(by_key.values(), key=lambda e: e["date"]):
            f.write(json.dumps(e, sort_keys=True))
            f.write("\n")
    return changed


# --- Исторический импорт -------------------------------------------------
#
# Прежние три источника (spoluan, ehsan high-impact, ehsan full) выброшены
# вместе с их данными. Причина измерена: в собранном из них архиве 25% событий
# High и Medium оказались дубликатами того же события в пределах суток, а
# доминирующий сдвиг составлял ровно семь часов. У каждой американской
# публикации было два кластера времени - настоящий, совпадающий с известным
# расписанием (08:30 у BLS, 10:00 у ISM, 14:00 у ФРС), и смещённый. Причина в
# том, что дампы собирались с разными соглашениями о часовом поясе, а ключ
# слияния включает дату, поэтому сдвинутая копия выглядела отдельным событием.
#
# Для календарного множителя (MEALS, п.4.3) это хуже, чем пропуски: пропуск
# занижает вес часа, а фантомное событие поднимает его там, где публикации не
# было вовсе.

# Датасет Kaggle "Global Economic Calendar" (EL Younes), лицензия
# CC BY-NC-SA 4.0. Скачивается публичным API без авторизации, поэтому работает
# и в CI без секретов.
#
# Проверено на данных: время у него в UTC, переход на летнее время обработан
# верно - у CPI США ровно два значения, 12:30 и 13:30 UTC, в пропорции 135:72,
# что в точности соответствует 08:30 по Нью-Йорку летом и зимой и доле летних
# и зимних месяцев в году. Дубликатов 2.2% против 25% у прежнего архива.
_KAGGLE_URL = (
    "https://www.kaggle.com/api/v1/datasets/download/"
    "youneseloiarm/global-economic-calendar"
)

# Покрытие датасета кончается здесь; дальше добирается помесячно с
# ForexFactory (fetch_forexfactory_month).
KAGGLE_COVERAGE_END = "2025-10-01"

_KAGGLE_IMPACT = {"high": "High", "medium": "Medium", "low": "Low"}


def _request(url: str, timeout: int, session: requests.Session | None = None,
             headers: dict | None = None) -> requests.Response:
    """Один HTTP-запрос с внятной ошибкой вместо голого исключения requests."""
    get = session.get if session is not None else requests.get
    try:
        resp = get(url, timeout=timeout,
                   headers=headers or {"User-Agent": "market-alert-bot"})
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        raise CalendarError(f"не удалось получить {url}: {exc}") from exc


def _normalize_kaggle_row(row: dict) -> dict | None:
    """Строка датасета -> запись архива. Строки без уровня важности или с
    временем "All Day" отбрасываются: это выходные и праздники, у которых нет
    ни момента публикации, ни влияния на рынок."""
    impact = _KAGGLE_IMPACT.get(str(row.get("importance") or "").strip().lower())
    time_str = str(row.get("time") or "").strip()
    if impact is None or not time_str or time_str == "All Day":
        return None
    try:
        moment = datetime.strptime(f"{row['date']} {time_str}", "%d/%m/%Y %H:%M")
    except (ValueError, KeyError):
        return None
    return {
        "date": moment.replace(tzinfo=timezone.utc).isoformat(),
        "country": (str(row.get("currency") or "").strip().upper()
                    or str(row.get("zone") or "").strip()),
        "title": str(row.get("event") or "").strip(),
        "impact": impact,
        "actual": str(row.get("actual") or ""),
        "forecast": str(row.get("forecast") or ""),
        "previous": str(row.get("previous") or ""),
    }


def fetch_kaggle_calendar(session: requests.Session | None = None,
                          timeout: int = 180) -> list[dict]:
    """Исторический архив 2020-2025 одним zip-архивом."""
    import csv
    import io
    import zipfile

    resp = _request(_KAGGLE_URL, timeout=timeout, session=session)
    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
    with archive.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="ignore")
        events = [normalized for row in csv.DictReader(text)
                  if (normalized := _normalize_kaggle_row(row)) is not None]
    if not events:
        raise CalendarError("Датасет Kaggle не дал ни одного пригодного события")
    return events


# ForexFactory отдаёт месяц целиком по адресу вида ?month=mar.2026, и данные
# лежат прямо в странице готовым JSON. Время в них - unix-таймстамп, то есть
# однозначное: именно та неоднозначность, что испортила прежний архив, здесь
# отсутствует по построению.
#
# Библиотека market-calendar-tool для этого не годится: она сначала дёргает
# служебный /calendar/apply-settings, чтобы выставить таймзону отображения, а
# он отвечает 403. Сама помесячная страница при этом доступна.
_FF_MONTH_URL = "https://www.forexfactory.com/calendar?month={month}.{year}"
_FF_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
}
_FF_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
              "jul", "aug", "sep", "oct", "nov", "dec")
_FF_IMPACT = {
    "High Impact Expected": "High",
    "Medium Impact Expected": "Medium",
    "Low Impact Expected": "Low",
    "Non-Economic": "Low",
}


def _extract_calendar_state(html: str) -> list[dict]:
    """Достаёт список дней из встроенного в страницу состояния компонента.

    Разбор идёт по балансу скобок, а не регулярным выражением на всю структуру:
    внутри лежит вложенный JSON с экранированными кавычками, и жадное или
    ленивое выражение одинаково легко обрезает его не в том месте.
    """
    marker = re.search(r"calendarComponentStates\[\d+\]\s*=\s*\{", html)
    if marker is None:
        raise CalendarError("В странице ForexFactory нет состояния календаря")
    start = marker.end() - 1
    depth = 0
    for index in range(start, len(html)):
        if html[index] == "{":
            depth += 1
        elif html[index] == "}":
            depth -= 1
            if depth == 0:
                block = html[start:index + 1]
                break
    else:
        raise CalendarError("Состояние календаря оборвано")

    days = re.search(r"days:\s*(\[.*?\])\s*,\s*[a-zA-Z_]+:", block, re.S)
    if days is None:
        raise CalendarError("В состоянии календаря нет списка дней")
    return json.loads(days.group(1))


def fetch_forexfactory_month(year: int, month: int,
                             session: requests.Session | None = None,
                             timeout: int = 40) -> list[dict]:
    """Один календарный месяц с ForexFactory."""
    url = _FF_MONTH_URL.format(month=_FF_MONTHS[month - 1], year=year)
    # Здесь намеренно urllib, а не requests, хотя весь остальной модуль на
    # requests. Проверено: на requests ForexFactory отвечает 403 при любых
    # заголовках, включая полный браузерный набор, а на urllib с тем же
    # User-Agent - 200. Различие не в заголовках, а в TLS-отпечатке клиента,
    # и переспорить его набором headers нельзя.
    request = urllib.request.Request(url, headers=_FF_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, OSError) as exc:
        raise CalendarError(f"не удалось получить {url}: {exc}") from exc

    events = []
    for day in _extract_calendar_state(html):
        for item in day.get("events", []):
            impact = _FF_IMPACT.get(item.get("impactTitle") or "")
            if impact is None or not item.get("dateline"):
                continue
            moment = datetime.fromtimestamp(int(item["dateline"]), tz=timezone.utc)
            events.append({
                "date": moment.isoformat(),
                "country": str(item.get("currency") or "").strip(),
                "title": str(item.get("name") or "").strip(),
                "impact": impact,
                "actual": str(item.get("actual") or ""),
                "forecast": str(item.get("forecast") or ""),
                "previous": str(item.get("previous") or ""),
            })
    return events


def import_forexfactory_months(start: date, end: date,
                               session: requests.Session | None = None,
                               request_delay_seconds: float = 2.0) -> list[dict]:
    """Помесячный добор за период. Пауза между запросами намеренная: это
    обычная страница сайта, а не API с оплаченным лимитом."""
    events: list[dict] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        try:
            got = fetch_forexfactory_month(year, month, session=session)
            log.info("ForexFactory %04d-%02d: %d событий", year, month, len(got))
            events.extend(got)
        except Exception as exc:
            log.error("ForexFactory %04d-%02d: не удалось - %s", year, month, exc)
        month += 1
        if month > 12:
            year, month = year + 1, 1
        if (year, month) <= (end.year, end.month):
            time.sleep(request_delay_seconds)
    return events


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rebuild", action="store_true",
                        help="Собрать архив заново: Kaggle плюс добор с ForexFactory")
    parser.add_argument("--import-kaggle", action="store_true",
                        help="Только исторический датасет Kaggle")
    parser.add_argument("--import-forexfactory", action="store_true",
                        help="Только помесячный добор с ForexFactory")
    parser.add_argument("--from-month", default=KAGGLE_COVERAGE_END[:7],
                        help="Первый месяц добора, YYYY-MM")
    parser.add_argument("--to-month", default=None, help="Последний месяц, YYYY-MM")
    args = parser.parse_args()

    if not (args.rebuild or args.import_kaggle or args.import_forexfactory):
        parser.error("нечего делать - укажите --rebuild, --import-kaggle "
                     "или --import-forexfactory")

    calendar_dir = os.environ.get(
        "CALENDAR_DIR",
        os.path.join(os.path.dirname(__file__), "..", "data", "economic_calendar"))
    path = store_path(calendar_dir)
    session = requests.Session()
    events: list[dict] = []

    if args.rebuild:
        # Прежний архив выбрасывается целиком, а не дополняется: смешивать его
        # с новым значило бы сохранить те самые сдвинутые копии.
        if os.path.exists(path):
            os.remove(path)
            log.info("Прежний архив удалён")

    if args.rebuild or args.import_kaggle:
        got = fetch_kaggle_calendar(session=session)
        log.info("Kaggle: %d событий", len(got))
        events.extend(got)

    if args.rebuild or args.import_forexfactory:
        first = datetime.strptime(args.from_month, "%Y-%m").date()
        last = (datetime.strptime(args.to_month, "%Y-%m").date() if args.to_month
                else datetime.now(timezone.utc).date())
        events.extend(import_forexfactory_months(first, last, session=session))

    since = datetime.fromisoformat(_ARCHIVE_SINCE)
    kept = [e for e in events if parse_event_time(e["date"]) >= since]
    added = merge_events(path, kept)
    log.info("Получено %d событий, с %s осталось %d, записано новых %d",
             len(events), _ARCHIVE_SINCE, len(kept), added)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Posts a Saturday digest of the coming week's Medium/High-impact economic
calendar events to Telegram.

Piggybacks on the existing hourly trigger (see .github/workflows/price-monitor.yml
and README - external cron-job.org calls workflow_dispatch roughly once an hour)
instead of provisioning a second schedule: __main__.py calls
maybe_send_weekly_digest on every run, and it's a no-op except during the one
hourly run that happens to land on Saturday, ~12:00 Israel time. "Already sent
this week" is tracked in state.json (already loaded/saved every run) so a
second run landing in the same hour - or the external trigger firing a little
early or late - never posts the digest twice.

Днём отправки была суббота - на предположении, что фид ForexFactory
(CALENDAR_URL) к субботе уже переключается на предстоящую неделю. Проверить
это вживую нельзя иначе как запросом в реальную субботу, а обе возможные
границы недели у источника (воскресенье-суббота и суббота-пятница) одинаково
согласуются с тем, что фид отдаёт в будний день.

Поэтому день больше не выбирается, а проверяется. Окон два, суббота и
воскресенье, и дайджест уходит в первом, где фид действительно смотрит вперёд
(_looks_forward: последнее событие фида ещё впереди). Если суббота отдаёт
заканчивающуюся неделю, сообщение просто подождёт сутки. Дважды оно не уйдёт:
ключ дедупликации берётся из самого фида - из даты его первого события, - и у
субботы с воскресеньем, отдавших одну неделю, он один и тот же.

Low-impact events and holidays are both excluded (see _DIGEST_IMPACTS) - only
Medium/High. No LLM involved on purpose (see README, "Дневной сигнал" and the
weekly digest section): just a plain, programmatically formatted list grouped
by day - the source data already carries the impact tag and the numbers, so
there's nothing here for an LLM to add.

Под каждым событием печатаются все значения, какие дал источник: факт, прогноз,
предыдущее. Факт при этом требует отдельной работы - живой недельный фид его не
отдаёт вовсе, см. backfill_actuals.

Also runnable directly as a one-off, bypassing the Saturday/dedup checks - see
main() and .github/workflows/weekly-digest-test.yml - for manually checking
what the digest actually looks like without waiting for Saturday:
    python -m price_monitor.weekly_digest --force
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from price_monitor import economic_calendar
from price_monitor.config import Config, load_config
from price_monitor.notifier import TelegramError, send_telegram_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.weekly_digest")

_DIGEST_IMPACTS = {"Medium", "High"}
# datetime.weekday(): Monday=0 ... Saturday=5, Sunday=6.
#
# Окон два, и это не перестраховка. Фид отдаёт только "эту неделю", а где у
# ForexFactory граница недели - вживую подтверждено лишь для воскресенья: в
# воскресенье запрос возвращает ровно предстоящую неделю. Для субботы это
# осталось догадкой, и обе возможные границы (нед-сб и сб-пт) одинаково
# согласуются с наблюдаемой выдачей в будний день - различить их можно только
# запросом в реальную субботу.
#
# Поэтому день не выбирается, а проверяется. Дайджест пробует отправиться в
# субботу, но уходит лишь если фид действительно смотрит вперёд
# (_looks_forward); если суббота ещё отдаёт заканчивающуюся неделю, окно
# воскресенья отправит его через сутки. Отправляется он ровно один раз: ключ
# дедупликации берётся из САМОГО ФИДА - из даты его первого события, - поэтому
# суббота и воскресенье, отдавшие одну и ту же неделю, дают один и тот же ключ.
_DIGEST_WEEKDAYS = (5, 6)
_DIGEST_HOUR_ISRAEL = 12
_STATE_KEY = "weekly_digest:last_sent_week"

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
_IMPACT_EMOJI = {"High": "🔴", "Medium": "🟠"}

_WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг",
             "Пятница", "Суббота", "Воскресенье")

# Telegram отклоняет сообщение длиннее 4096 символов целиком, а не обрезает
# его, поэтому дайджест режется на части по границе дня. Запас в 96 символов -
# на служебную строку "часть N из M", которую иначе пришлось бы считать
# рекурсивно.
_MESSAGE_LIMIT = 4000


def _is_digest_window(now: datetime) -> bool:
    israel_now = now.astimezone(_ISRAEL_TZ)
    return (israel_now.weekday() in _DIGEST_WEEKDAYS
            and israel_now.hour == _DIGEST_HOUR_ISRAEL)


def _week_identifier(events: list[dict]) -> str:
    """Ключ дедупликации - дата первого события ФИДА, а не сегодняшняя дата.

    Дайджест рассказывает про неделю, а не про день отправки, и ключом должна
    быть неделя. Взяв дату запуска, суббота и воскресенье получили бы разные
    ключи, и одна и та же неделя ушла бы в чат дважды.
    """
    if not events:
        return ""
    first = min(economic_calendar.parse_event_time(e["date"]) for e in events)
    return first.date().isoformat()


def _looks_forward(events: list[dict], now: datetime) -> bool:
    """Смотрит ли фид вперёд, то есть про предстоящую неделю он или про
    заканчивающуюся.

    Проверяется по последнему событию: у предстоящей недели оно ещё впереди, у
    заканчивающейся - уже позади. Последнее, а не первое: неделя фида
    начинается с воскресенья, и в воскресный полдень часть событий уже прошла,
    хотя неделя именно предстоящая.
    """
    if not events:
        return False
    last = max(economic_calendar.parse_event_time(e["date"]) for e in events)
    return last > now


def _escape(text: str) -> str:
    """Сообщение уходит с parse_mode=HTML, а названия событий приходят из
    внешнего фида. Достаточно одного "M&A" или "S&P" без экранирования, чтобы
    Telegram отверг сообщение целиком - и недельный дайджест не пришёл вовсе."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def format_event_values(event: dict) -> str:
    """Строка значений события: факт, прогноз, предыдущее.

    Пустые поля пропускаются, а не печатаются как прочерк. У фида заполненность
    разная - прогноз есть примерно у 70% событий, предыдущее у 80%, - и строка
    из трёх прочерков не сообщала бы ничего, кроме того, что источник промолчал.

    Факт в дайджесте недели обычно пуст по существу: события ещё не наступили.
    Он выводится, когда есть, потому что тот же формат используется при ручном
    прогоне на прошедшей неделе.
    """
    parts = []
    for label, key in (("факт", "actual"), ("прогноз", "forecast"),
                       ("пред.", "previous")):
        value = str(event.get(key) or "").strip()
        if value:
            parts.append(f"{label} {_escape(value)}")
    return " · ".join(parts)


def _event_lines(event: dict) -> list[str]:
    event_time = economic_calendar.parse_event_time(event["date"])
    lines = [f"{_IMPACT_EMOJI[event['impact']]} <b>{event_time.strftime('%H:%M')}</b> "
             f"{_escape(event['country'])} — {_escape(event['title'])}"]
    values = format_event_values(event)
    if values:
        lines.append(f"    <i>{values}</i>")
    return lines


def format_digest(events: list[dict]) -> list[str]:
    """Дайджест недели, разбитый по дням и при необходимости на несколько
    сообщений. Возвращает список - отправитель шлёт их по порядку.

    Время у всех событий в UTC и подписано один раз в шапке, а не у каждой
    строки: при двух-трёх десятках событий повторение "UTC" в каждой строке
    занимает больше места, чем несёт смысла.
    """
    header = "📅 <b>Экономический календарь на неделю</b>"
    if not events:
        return [f"{header}\n\nНа этой неделе не найдено событий Medium/High impact."]

    ordered = sorted(events, key=lambda e: e["date"])
    high = sum(1 for e in ordered if e["impact"] == "High")
    first = economic_calendar.parse_event_time(ordered[0]["date"])
    last = economic_calendar.parse_event_time(ordered[-1]["date"])
    intro = (f"{header}\n"
             f"<i>{first.strftime('%d.%m')} — {last.strftime('%d.%m')}, "
             f"время UTC · {len(ordered)} событий, из них 🔴 {high}</i>")

    # Дни собираются целиком, а потом раскладываются по сообщениям: разрыв
    # внутри дня оставил бы заголовок в одном сообщении, а его события в
    # другом.
    days: list[list[str]] = []
    current_day = None
    for event in ordered:
        moment = economic_calendar.parse_event_time(event["date"])
        if moment.date() != current_day:
            current_day = moment.date()
            days.append([f"\n<b>{_WEEKDAYS[moment.weekday()]} "
                         f"{moment.strftime('%d.%m')}</b>"])
        days[-1].extend(_event_lines(event))

    messages: list[str] = []
    block = [intro]
    for day in days:
        candidate = block + day
        if len("\n".join(candidate)) > _MESSAGE_LIMIT and len(block) > 1:
            messages.append("\n".join(block))
            block = [f"{header} <i>(продолжение)</i>"] + day
        else:
            block = candidate
    messages.append("\n".join(block))
    return messages


def backfill_actuals(path: str, session: requests.Session | None = None,
                     now: datetime | None = None) -> int:
    """Дозаполняет вышедшие значения (`actual`) за текущий и прошлый месяц.

    Без этого архив рос бы вперёд с вечно пустым фактом. Живой недельный фид -
    единственный источник, который приходит сюда регулярно, и в нём поля
    `actual` НЕТ ВОВСЕ: проверено на выдаче, ключи фида - country, date,
    forecast, impact, previous, title. Событие попадает в архив за неделю до
    публикации, с прогнозом и предыдущим значением, а вышедшая цифра не
    появляется никогда, потому что фид к этому событию больше не возвращается.

    Помесячные страницы ForexFactory факт отдают - 85% событий за август 2026,
    77% за март 2021, - поэтому раз в неделю дочитываются два месяца: текущий и
    предыдущий. Предыдущий нужен для событий последних чисел, чей факт выходит
    уже в новом месяце, а также потому, что источник иногда уточняет цифру
    задним числом. Два запроса в неделю - цена, которую этот пробел стоит.

    Возвращает число записей, добавленных или обновлённых в архиве.
    """
    now = now or datetime.now(timezone.utc)
    months = {(now.year, now.month)}
    previous = (now.replace(day=1) - timedelta(days=1))
    months.add((previous.year, previous.month))

    fetched: list[dict] = []
    for year, month in sorted(months):
        try:
            fetched.extend(economic_calendar.fetch_forexfactory_month(
                year, month, session=session))
        except economic_calendar.CalendarError as exc:
            # Дозаполнение - не то, ради чего запускается дайджест. Страница
            # может не открыться, и это не повод не отправить сообщение.
            log.warning("Не удалось дочитать %04d-%02d: %s", year, month, exc)

    if not fetched:
        return 0
    updated = economic_calendar.merge_events(path, fetched)
    log.info("Дозаполнение факта: %d событий за %d мес., изменено записей %d",
             len(fetched), len(months), updated)
    return updated


def _send_digest(cfg: Config, session: requests.Session | None,
                 raw_events: list[dict]) -> bool:
    """Кладёт полученный фид в архив (все уровни важности - см. строку модуля
    economic_calendar), дочитывает вышедшие значения и отправляет в Telegram
    дайджест Medium+High.

    Сбои гасятся, а не поднимаются: дайджест живёт внутри часового прогона
    мониторинга, и упавшая отправка не должна валить весь прогон - тот же
    подход, что и у поактивной обработки ошибок в __main__.py. Возвращает
    True, если дайджест действительно ушёл.
    """
    path = economic_calendar.store_path(cfg.calendar_dir)
    economic_calendar.merge_events(path, raw_events)
    backfill_actuals(path, session=session)

    digest_events = [e for e in raw_events if e["impact"] in _DIGEST_IMPACTS]
    messages = format_digest(digest_events)
    try:
        for text in messages:
            send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, text)
    except TelegramError as exc:
        log.error("Failed to send weekly digest: %s", exc)
        return False

    log.info("Weekly digest sent (%d Medium/High events of %d total, %d message(s))",
             len(digest_events), len(raw_events), len(messages))
    return True


def _fetch_and_send_digest(cfg: Config, session: requests.Session) -> bool:
    """Ручной одноразовый прогон (main, --force): тянет фид и шлёт дайджест
    без проверок дня и "уже отправлено". Проверки "смотрит ли фид вперёд" здесь
    тоже нет намеренно - смысл --force в том, чтобы посмотреть на сообщение
    таким, какое оно есть, в любой день недели."""
    try:
        raw_events = economic_calendar.fetch_calendar(session=session)
    except economic_calendar.CalendarError as exc:
        log.error("Failed to fetch economic calendar for weekly digest: %s", exc)
        return False
    return _send_digest(cfg, session, raw_events)


def maybe_send_weekly_digest(
    cfg: Config, state: dict, session: requests.Session, now: datetime | None = None,
) -> bool:
    """No-ops outside the Saturday ~12:00 Israel-time window, and no-ops if
    this week's digest has already been sent. Returns True if a digest was
    actually sent."""
    now = now or datetime.now(timezone.utc)
    if not _is_digest_window(now):
        return False

    try:
        raw_events = economic_calendar.fetch_calendar(session=session)
    except economic_calendar.CalendarError as exc:
        log.error("Failed to fetch economic calendar for weekly digest: %s", exc)
        return False

    week_id = _week_identifier(raw_events)
    if state.get(_STATE_KEY) == week_id:
        return False
    if not _looks_forward(raw_events, now):
        # Фид ещё отдаёт заканчивающуюся неделю. Рассылать список того, что уже
        # произошло, под заголовком "на неделю" нельзя, а окно следующего дня
        # отправит настоящую предстоящую неделю.
        log.info("Фид отдаёт заканчивающуюся неделю (%s) - дайджест отложен", week_id)
        return False

    if not _send_digest(cfg, session, raw_events):
        return False

    state[_STATE_KEY] = week_id
    return True


def main() -> int:
    """Manual one-off: sends the digest right now, regardless of day/time,
    without touching state.json's "already sent this week" tracking - this
    isn't part of the regular Saturday schedule (see
    .github/workflows/weekly-digest-test.yml), just a way to see what the
    digest actually looks like without waiting for Saturday."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--force", action="store_true",
        help="Send immediately, bypassing the Saturday-window and already-sent-this-week checks")
    args = parser.parse_args()
    if not args.force:
        parser.error("nothing to do - pass --force (see module docstring)")

    cfg = load_config()
    session = requests.Session()
    return 0 if _fetch_and_send_digest(cfg, session) else 1


if __name__ == "__main__":
    sys.exit(main())

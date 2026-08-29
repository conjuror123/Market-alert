"""Manually-triggered step: for every alert sent but not yet explained, look up
recent news for that asset, ask the configured LLM to connect the news to the
price move, and edit the original Telegram message in place with the answer.

Deliberately separate from the main monitor loop (price_monitor/__main__.py),
which runs every hour on a schedule: this step costs LLM tokens and needs an
API key, so it only runs when triggered by hand (see
.github/workflows/explain-alerts.yml -> "Run workflow" in the Actions tab).

Run it as: python -m price_monitor.explain
"""
from __future__ import annotations

import html
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from price_monitor.alerts_log import load_alerts_log, pending_entries, save_alerts_log
from price_monitor.config import Config, load_config
from price_monitor.llm import LLMError, chat_completion, select_model
from price_monitor.news import NewsError, fetch_news
from price_monitor.notifier import TelegramError, edit_telegram_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.explain")

EXPLANATION_HEADER = "🧠 <b>Возможная причина (по новостям, определено автоматически)</b>"

# News is only searched for in [alert + START, alert + END] - a fixed window
# relative to the alert, not to whenever the workflow happens to run. Waiting
# until the window has fully elapsed (see explain_min_age_hours, which should
# match NEWS_WINDOW_END_HOURS) means the search always covers the same real
# span of time regardless of when you click "Run workflow".
NEWS_WINDOW_START_HOURS = 6
NEWS_WINDOW_END_HOURS = 12

# Google News' after:/before: search operators turned out (verified live,
# against both a summer and a winter date, i.e. across the DST transition) to
# use America/Los_Angeles calendar days, not UTC ones - presumably tied to the
# hl=en-US/gl=US params news.py always sends. Using the real zoneinfo entry
# (not a hardcoded UTC-7) means this stays correct through DST changes.
_GOOGLE_NEWS_TZ = ZoneInfo("America/Los_Angeles")

SYSTEM_PROMPT = (
    "Ты помогаешь трейдеру понять, почему актив резко изменился в цене. Тебе дают "
    "название актива, цифры движения и заголовки недавних новостей о нём. Если "
    "заголовки правдоподобно объясняют движение, в 2-4 коротких предложениях на "
    "русском языке объясни, что произошло и через какой экономический механизм это "
    "привело именно к такому движению цены - не ограничивайся пересказом заголовка, "
    "а раскрывай причинно-следственную связь (например: рост ставок ФРС повышает "
    "привлекательность облигаций и снижает спрос на активы без процентного дохода, "
    "к которым рынок относит и крипту, и золото). Такие общие экономические "
    "закономерности упоминать можно, но не утверждай ничего конкретного о движении "
    "других активов - данных об этом у тебя нет, и это будет уже не логика, а "
    "выдумка. Если ни один заголовок не объясняет движение явно, честно напиши, что "
    "очевидной причины в новостях не нашлось - не выдумывай её. Пиши простым "
    "текстом, без markdown-разметки."
)


def _news_query_by_symbol(cfg: Config) -> dict[str, str]:
    return {asset.label: asset.news_query for asset in cfg.assets}


def _build_user_prompt(entry: dict, articles: list[dict]) -> str:
    lines = [
        f"Актив: {entry['symbol']}",
        f"Изменение цены: {entry['last_return_pct']:+.2f}% за последний интервал, "
        f"текущая цена {entry['last_close']:g}",
        f"Время алерта (UTC): {entry['sent_at']}",
        "",
    ]
    if articles:
        lines.append("Недавние заголовки новостей:")
        for a in articles:
            when = a["published"].strftime("%Y-%m-%d %H:%M UTC") if a["published"] else "дата неизвестна"
            source = f" ({a['source']})" if a["source"] else ""
            lines.append(f"- [{when}] {a['title']}{source}")
    else:
        lines.append("Заголовков новостей не найдено.")
    return "\n".join(lines)


def _scope_query(query: str, lower_cutoff: datetime | None, upper_cutoff: datetime | None) -> str:
    """Add Google News' "after:"/"before:" search operators so the
    (Google-imposed, fixed) 100-result cap isn't spent on coverage from
    outside [lower_cutoff, upper_cutoff]. Google's date operators are
    day-granularity only, in America/Los_Angeles days (see _GOOGLE_NEWS_TZ) -
    dates are converted to that zone before formatting, rather than padded
    with a blind day of buffer on each side.

    after: is set to the Pacific calendar day containing lower_cutoff - that
    day always starts at or before lower_cutoff itself, in any timezone, so
    this never excludes anything it shouldn't. before: needs the day *after*
    the Pacific calendar day containing upper_cutoff, since Google's before:
    excludes everything from the start of that date onward; using the same
    date would risk cutting off part of upper_cutoff's own day. Either way,
    _filter_after/_filter_before still do the exact cutoff against the real
    UTC timestamps afterwards - this only controls how tightly scoped the raw
    Google fetch is, never correctness.
    """
    if lower_cutoff is None:
        return query
    parts = [query, f"after:{lower_cutoff.astimezone(_GOOGLE_NEWS_TZ).date()}"]
    if upper_cutoff is not None:
        before_date = upper_cutoff.astimezone(_GOOGLE_NEWS_TZ).date() + timedelta(days=1)
        parts.append(f"before:{before_date}")
    return " ".join(parts)


def _filter_after(articles: list[dict], cutoff: datetime | None) -> list[dict]:
    """Keep only articles published at/after `cutoff`. We're explaining a move
    that already happened, so a prediction or rumor from before it isn't a
    cause - it's speculation. Articles with no known publish date are dropped
    too, since there's no way to confirm they qualify.
    """
    if cutoff is None:
        return articles
    return [a for a in articles if a["published"] is not None and a["published"] >= cutoff]


def _filter_before(articles: list[dict], cutoff: datetime) -> list[dict]:
    """Keep only articles published at/before `cutoff` - see _scope_query for
    why an upper bound matters at all."""
    return [a for a in articles if a["published"] is not None and a["published"] <= cutoff]


def _is_old_enough(entry: dict, min_age_hours: float, now: datetime | None = None) -> bool:
    """Whether enough time has passed since the alert for the full
    [alert + NEWS_WINDOW_START_HOURS, alert + NEWS_WINDOW_END_HOURS] window to
    have already elapsed - running before that would search a window that
    partly hasn't happened yet. min_age_hours should match
    NEWS_WINDOW_END_HOURS (see explain_min_age_hours in config.yaml).
    """
    try:
        alert_time = datetime.fromisoformat(entry["sent_at"])
    except (KeyError, ValueError):
        return True
    now = now or datetime.now(timezone.utc)
    return now - alert_time >= timedelta(hours=min_age_hours)


def explain_entry(cfg: Config, entry: dict, query: str) -> str:
    """Fetch news for `query` (within [alert + NEWS_WINDOW_START_HOURS,
    alert + NEWS_WINDOW_END_HOURS]) and ask the LLM to explain this one alert
    entry. If nothing turns up in that window, the LLM is still asked - the
    window is fixed relative to the alert, so unlike an open-ended "up to
    now" search, waiting and retrying later would search the exact same
    window and find the exact same (lack of) results. The system prompt
    already handles "no headlines found" honestly.
    """
    try:
        alert_time = datetime.fromisoformat(entry["sent_at"])
    except (KeyError, ValueError):
        alert_time = None

    lower_cutoff = alert_time + timedelta(hours=NEWS_WINDOW_START_HOURS) if alert_time is not None else None
    upper_cutoff = alert_time + timedelta(hours=NEWS_WINDOW_END_HOURS) if alert_time is not None else None

    try:
        articles = fetch_news(_scope_query(query, lower_cutoff, upper_cutoff), limit=100)
    except NewsError as exc:
        log.warning("%s: news fetch failed: %s", entry["symbol"], exc)
        articles = []

    if alert_time is not None:
        articles = _filter_after(articles, lower_cutoff)
        articles = _filter_before(articles, upper_cutoff)

    return chat_completion(
        base_url=cfg.llm_base_url,
        api_key=cfg.llm_api_key,
        model=select_model(cfg.llm_model_peak, cfg.llm_model_offpeak),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(entry, articles)},
        ],
    )


def _select_todo(entries: list[dict], only_message_id: int | None) -> list[dict]:
    """Which pending entries this run should process - all of them, or (when
    someone fills in the "Message ID" workflow input) just the one they
    picked. Explaining a specific alert is the common case in practice: it's
    the only way to control exactly which (and how many) alerts spend tokens
    in a given run."""
    todo = pending_entries(entries)
    if only_message_id is None:
        return todo
    return [e for e in todo if e.get("message_id") == only_message_id]


def main() -> int:
    cfg = load_config()
    if not cfg.llm_api_key:
        print("LLM_API_KEY не задан в окружении.", file=sys.stderr)
        return 1

    raw_message_id = os.environ.get("EXPLAIN_MESSAGE_ID", "").strip()
    only_message_id: int | None = None
    if raw_message_id:
        try:
            only_message_id = int(raw_message_id)
        except ValueError:
            print(f"Message ID должен быть числом, получено: {raw_message_id!r}", file=sys.stderr)
            return 1

    entries = load_alerts_log(cfg.alerts_log_path)
    todo = _select_todo(entries, only_message_id)
    if not todo:
        if only_message_id is not None:
            print(f"Алерт с ID {only_message_id} не найден среди необъяснённых.", file=sys.stderr)
            return 1
        log.info("No pending alerts to explain.")
        return 0

    query_by_symbol = _news_query_by_symbol(cfg)
    had_error = False

    for entry in todo:
        if not _is_old_enough(entry, cfg.explain_min_age_hours):
            log.info("%s: alert too recent, skipping for now (will retry later)", entry["symbol"])
            continue

        query = query_by_symbol.get(entry["symbol"], entry["symbol"])
        try:
            explanation = explain_entry(cfg, entry, query)
        except LLMError as exc:
            log.error("%s: LLM call failed, will retry next run: %s", entry["symbol"], exc)
            had_error = True
            continue

        base_text = entry.get("message_text") or f"{entry['symbol']}: {entry['last_return_pct']:+.2f}%"
        new_text = f"{base_text}\n\n{EXPLANATION_HEADER}\n{html.escape(explanation, quote=False)}"

        try:
            edit_telegram_message(cfg.telegram_bot_token, entry["chat_id"], entry["message_id"], new_text)
        except TelegramError as exc:
            log.error("%s: failed to edit Telegram message, will retry next run: %s", entry["symbol"], exc)
            had_error = True
            continue

        entry["explained"] = True
        entry["explanation"] = explanation
        entry["explained_at"] = datetime.now(timezone.utc).isoformat()
        log.info("%s: explained and message updated", entry["symbol"])

    save_alerts_log(cfg.alerts_log_path, entries)
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())

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
import sys
from datetime import datetime, timedelta, timezone

from price_monitor.alerts_log import load_alerts_log, pending_entries, save_alerts_log
from price_monitor.config import Config, load_config
from price_monitor.llm import LLMError, chat_completion, select_model
from price_monitor.news import NewsError, fetch_news
from price_monitor.notifier import TelegramError, edit_telegram_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.explain")

EXPLANATION_HEADER = "🧠 <b>Возможная причина (по новостям, определено автоматически)</b>"

# How many days after an alert we still consider news "about" it. Only matters
# when an alert sits unprocessed for a while - see _scope_query.
NEWS_WINDOW_DAYS = 2

SYSTEM_PROMPT = (
    "Ты помогаешь трейдеру понять, почему актив резко изменился в цене. Тебе дают "
    "название актива, цифры движения и заголовки недавних новостей о нём. Если "
    "заголовки правдоподобно объясняют движение, в 2-3 коротких предложениях на "
    "русском языке объясни, что произошло и как это связано с ценой. Если ни один "
    "заголовок не объясняет движение явно, честно напиши, что очевидной причины в "
    "новостях не нашлось - не выдумывай её. Пиши простым текстом, без markdown-разметки."
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


def _scope_query(query: str, alert_time: datetime | None) -> str:
    """Add Google News' "after:"/"before:" search operators so the
    (Google-imposed, fixed) 100-result cap isn't spent on coverage from
    outside a window around the alert. Google's date operators are
    day-granularity only and its own timezone handling is unclear, so this
    asks for one extra day of buffer on each side - _filter_after and
    _filter_before do the exact cutoffs against the real timestamps.

    The upper bound matters only when an alert sits unprocessed for a while:
    without one, an old alert's search would run "from just before it to
    right now" - for an alert from a week ago, "right now" is overwhelmingly
    today's unrelated news, not coverage of that old move. For an alert
    processed promptly (the normal case) "now" is already inside the window,
    so this has no effect.
    """
    if alert_time is None:
        return query
    after_date = (alert_time - timedelta(days=1)).strftime("%Y-%m-%d")
    before_date = (alert_time + timedelta(days=NEWS_WINDOW_DAYS + 1)).strftime("%Y-%m-%d")
    return f"{query} after:{after_date} before:{before_date}"


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


def _effective_cutoff(alert_time: datetime) -> datetime:
    """The actual "at/after" boundary passed to _filter_after.

    Normally that's just the alert time itself. But late in the UTC day (Google
    News runs on UTC, see _scope_query) there simply isn't much
    more coverage published between, say, 23:30 and midnight - filtering to the
    exact alert minute would starve the LLM of context it would otherwise have
    had. For alerts from 18:00 UTC onward, widen the window back to 18:00 UTC
    the same day instead.

    Alerts from 00:00-06:00 UTC have the opposite problem (the day's news cycle
    hasn't built up yet) but there's no similar fix - artificially reaching back
    into the previous day would pull in news about a different move entirely.
    explain_entry() just skips those when nothing survives the filter.
    """
    if alert_time.hour >= 18:
        return alert_time.replace(hour=18, minute=0, second=0, microsecond=0)
    return alert_time


def _is_old_enough(entry: dict, min_age_hours: float, now: datetime | None = None) -> bool:
    """Whether enough time has passed since the alert to bother processing it
    yet. Running "Explain Alerts" soon after an alert fires means whatever
    news exists so far only covers a sliver of time (see _effective_cutoff) -
    better to wait so there's a real window of coverage to search, and so the
    window's width doesn't vary wildly run to run depending on exactly when
    you happen to click "Run workflow". See explain_min_age_hours in
    config.yaml.
    """
    try:
        alert_time = datetime.fromisoformat(entry["sent_at"])
    except (KeyError, ValueError):
        return True
    now = now or datetime.now(timezone.utc)
    return now - alert_time >= timedelta(hours=min_age_hours)


def explain_entry(cfg: Config, entry: dict, query: str) -> str | None:
    """Fetch news for `query` and ask the LLM to explain this one alert entry.

    Returns None if there isn't enough post-alert news to work with (fetch
    failed, or nothing survived the date filter) - the caller should leave the
    entry unexplained for a later retry rather than spend tokens asking the LLM
    to explain an empty context.
    """
    try:
        alert_time = datetime.fromisoformat(entry["sent_at"])
    except (KeyError, ValueError):
        alert_time = None

    try:
        articles = fetch_news(_scope_query(query, alert_time), limit=100)
    except NewsError as exc:
        log.warning("%s: news fetch failed: %s", entry["symbol"], exc)
        articles = []

    if alert_time is not None:
        articles = _filter_after(articles, _effective_cutoff(alert_time))
        articles = _filter_before(articles, alert_time + timedelta(days=NEWS_WINDOW_DAYS))
        if not articles:
            return None

    return chat_completion(
        base_url=cfg.llm_base_url,
        api_key=cfg.llm_api_key,
        model=select_model(cfg.llm_model_peak, cfg.llm_model_offpeak),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(entry, articles)},
        ],
    )


def main() -> int:
    cfg = load_config()
    if not cfg.llm_api_key:
        print("LLM_API_KEY не задан в окружении.", file=sys.stderr)
        return 1

    entries = load_alerts_log(cfg.alerts_log_path)
    todo = pending_entries(entries)
    if not todo:
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

        if explanation is None:
            log.info("%s: not enough post-alert news yet, skipping for now (will retry later)", entry["symbol"])
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

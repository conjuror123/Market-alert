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
from datetime import datetime, timezone

from price_monitor.alerts_log import load_alerts_log, pending_entries, save_alerts_log
from price_monitor.config import Config, load_config
from price_monitor.llm import LLMError, chat_completion
from price_monitor.news import NewsError, fetch_news
from price_monitor.notifier import TelegramError, edit_telegram_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.explain")

EXPLANATION_HEADER = "🧠 <b>Возможная причина (по новостям, определено автоматически)</b>"

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


def explain_entry(cfg: Config, entry: dict, query: str) -> str:
    """Fetch news for `query` and ask the LLM to explain this one alert entry."""
    try:
        articles = fetch_news(query, limit=6)
    except NewsError as exc:
        log.warning("%s: news fetch failed, asking the LLM without headlines: %s", entry["symbol"], exc)
        articles = []

    return chat_completion(
        base_url=cfg.llm_base_url,
        api_key=cfg.llm_api_key,
        model=cfg.llm_model,
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

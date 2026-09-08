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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from price_monitor.alerts_log import load_alerts_log, pending_entries, save_alerts_log
from price_monitor.config import Config, load_config
from price_monitor.llm import LLMError, chat_completion, select_model
from price_monitor.news import NewsError, fetch_news
from price_monitor.notifier import TelegramError, edit_telegram_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.explain")

EXPLANATION_HEADER = "🧠 <b>Possible cause (from the news, determined automatically)</b>"

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
    "You help a trader understand why an asset moved sharply in price. You are given "
    "the asset name, the numbers of the move, and headlines of recent news about it. "
    "If the headlines plausibly explain the move, explain in 2-4 short sentences in "
    "English what happened and through which economic mechanism it led to that "
    "particular price move - do not stop at restating the headline, but spell out the "
    "causal link (for example: rising Fed rates make bonds more attractive and reduce "
    "demand for assets that pay no interest, a group the market takes to include both "
    "crypto and gold). Such general economic regularities may be mentioned, but do not "
    "assert anything specific about the movement of other assets - you have no data on "
    "that, and it would be invention rather than reasoning. If no headline explains the "
    "move clearly, say honestly that no obvious cause was found in the news - do not "
    "invent one. Write in plain text, without markdown."
)


@dataclass
class ExplainResult:
    """What explain_entry produced, kept alongside the explanation text itself
    so a bogus answer can be diagnosed later without guessing what was sent -
    which model, and the exact request messages (system + user prompt,
    including which articles the LLM actually saw)."""
    text: str
    model: str
    messages: list[dict]


def _news_query_by_symbol(cfg: Config) -> dict[str, str]:
    """Label -> the phrase to search news for.

    Taken from Tremor's basket rather than the old per-asset config, which went
    with the detector that used to write these alerts. The label is the query:
    "Gold" and "Pound / dollar" are what a person would type, and news_query
    defaulted to exactly that anyway.
    """
    try:
        from tremor.basket import load_basket

        return {a.label or a.ticker: a.label or a.ticker
                for a in load_basket().instruments}
    except Exception:                            # pragma: no cover - defensive
        return {}


def _build_user_prompt(entry: dict, articles: list[dict]) -> str:
    lines = [
        f"Asset: {entry['symbol']}",
        f"Price change: {entry['last_return_pct']:+.2f}% over the last interval, "
        f"current price {entry['last_close']:g}",
        f"Alert time (UTC): {entry['sent_at']}",
        "",
    ]
    if articles:
        lines.append("Recent news headlines:")
        for a in articles:
            when = a["published"].strftime("%Y-%m-%d %H:%M UTC") if a["published"] else "date unknown"
            source = f" ({a['source']})" if a["source"] else ""
            lines.append(f"- [{when}] {a['title']}{source}")
    else:
        lines.append("No news headlines found.")
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


def explain_entry(cfg: Config, entry: dict, query: str) -> ExplainResult:
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

    model = select_model(cfg.llm_model_peak, cfg.llm_model_offpeak)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(entry, articles)},
    ]
    text = chat_completion(
        base_url=cfg.llm_base_url,
        api_key=cfg.llm_api_key,
        model=model,
        messages=messages,
    )
    return ExplainResult(text=text, model=model, messages=messages)


def _select_todo(entries: list[dict], message_id: int) -> list[dict]:
    """The one pending entry matching `message_id` (from the "Message ID"
    workflow input), or an empty list if it's not pending. There is no "leave
    it blank to explain everything" mode: that would spend LLM tokens on
    every not-yet-explained alert at once, with no way to preview or limit
    the cost before it happens - always requiring a specific ID keeps each
    run's cost predictable (one alert, one call).
    """
    return [e for e in pending_entries(entries) if e.get("message_id") == message_id]


def main() -> int:
    cfg = load_config()
    if not cfg.llm_api_key:
        print("LLM_API_KEY is not set in the environment.", file=sys.stderr)
        return 1

    raw_message_id = os.environ.get("EXPLAIN_MESSAGE_ID", "").strip()
    if not raw_message_id:
        print("Give a Message ID — which alert to explain (see README).", file=sys.stderr)
        return 1
    try:
        message_id = int(raw_message_id)
    except ValueError:
        print(f"Message ID must be a number, got: {raw_message_id!r}", file=sys.stderr)
        return 1

    entries = load_alerts_log(cfg.alerts_log_path)
    todo = _select_todo(entries, message_id)
    if not todo:
        print(f"No unexplained alert with ID {message_id} was found.", file=sys.stderr)
        return 1

    query_by_symbol = _news_query_by_symbol(cfg)
    had_error = False

    for entry in todo:
        if not _is_old_enough(entry, cfg.explain_min_age_hours):
            log.info("%s: alert too recent, skipping for now (will retry later)", entry["symbol"])
            continue

        query = query_by_symbol.get(entry["symbol"], entry["symbol"])
        try:
            result = explain_entry(cfg, entry, query)
        except LLMError as exc:
            log.error("%s: LLM call failed, will retry next run: %s", entry["symbol"], exc)
            had_error = True
            continue

        base_text = entry.get("message_text") or f"{entry['symbol']}: {entry['last_return_pct']:+.2f}%"
        new_text = f"{base_text}\n\n{EXPLANATION_HEADER}\n{html.escape(result.text, quote=False)}"

        try:
            edit_telegram_message(cfg.telegram_bot_token, entry["chat_id"], entry["message_id"], new_text)
        except TelegramError as exc:
            log.error("%s: failed to edit Telegram message, will retry next run: %s", entry["symbol"], exc)
            had_error = True
            continue

        entry["explained"] = True
        entry["explanation"] = result.text
        # Kept so a bogus explanation can be diagnosed later - which model
        # answered, and exactly what it was asked (including which news
        # articles it actually saw), without having to guess or dig through
        # Actions run logs that eventually expire.
        entry["llm_model"] = result.model
        entry["llm_messages"] = result.messages
        entry["explained_at"] = datetime.now(timezone.utc).isoformat()
        log.info("%s: explained and message updated", entry["symbol"])

    save_alerts_log(cfg.alerts_log_path, entries)
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())

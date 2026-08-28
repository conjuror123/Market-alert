"""Fetches recent news headlines for an asset via Google News' public RSS
search - no API key needed. Used only by the manually-triggered "explain
alerts" step (price_monitor/explain.py): the headlines are handed to an LLM,
which connects them to a price move or says plainly that it found no likely
cause, rather than guessing from the price move alone.
"""
from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import requests

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"


class NewsError(RuntimeError):
    pass


def fetch_news(
    query: str,
    limit: int = 6,
    session: requests.Session | None = None,
    timeout: int = 15,
) -> list[dict]:
    """Return up to `limit` recent headlines for `query`, newest first.

    Each item is {"title", "link", "source", "published"} - "published" is a
    timezone-aware datetime, or None if Google News didn't provide one.
    """
    http = session or requests
    resp = http.get(
        GOOGLE_NEWS_RSS_URL,
        params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise NewsError(f"Google News RSS error {resp.status_code}: {resp.text[:300]}")

    try:
        root = ElementTree.fromstring(resp.content)
    except ElementTree.ParseError as exc:
        raise NewsError(f"Could not parse Google News RSS response: {exc}") from exc

    articles = []
    for item in root.findall("./channel/item")[:limit]:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        articles.append({
            "title": title,
            "link": (item.findtext("link") or "").strip(),
            "source": (item.findtext("source") or "").strip(),
            "published": _parse_pub_date(item.findtext("pubDate")),
        })
    return articles


def _parse_pub_date(raw: str | None):
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

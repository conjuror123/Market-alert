"""Client for ForexFactory's public economic calendar feed, plus a permanent
local NDJSON store (same pattern as candle_store.py) so events accumulate
over time as the weekly digest (weekly_digest.py) fetches them.

Only a "this week" feed exists at
https://nfs.faireconomy.media/ff_calendar_thisweek.json - there is no
next-week or last-week variant (both return 404, confirmed live). That feed
spans Sunday through Friday, so fetching it specifically on Sunday - which is
exactly when weekly_digest.py runs - already returns the coming week's
events, with no separate "next week" request needed.

There is deliberately no historical backfill here: scraping ForexFactory's
own historical calendar pages is blocked by a Cloudflare bot challenge
(confirmed live, HTTP 403 with a "Just a moment..." JS challenge even with a
realistic browser User-Agent), so there's no way to backfill years of past
events the way candle_store's history was backfilled from price APIs. This
store only ever grows forward from whatever week it's first run in - see
README. Enriching calibration_review.py with calendar context for events
already in the backtest's history therefore isn't possible yet; it's
deferred until either a historical source is found or enough weeks
accumulate here on their own.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger("price_monitor.economic_calendar")

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


class CalendarError(RuntimeError):
    pass


def fetch_calendar(session: requests.Session | None = None, timeout: int = 15) -> list[dict]:
    """Fetches this week's calendar events. Each returned dict has:
    title, country (currency code, or "All" for events affecting everyone),
    date (ISO8601 string, fixed -04:00 offset from the source - see
    parse_event_time), impact ("Low"/"Medium"/"High"/"Holiday"), forecast,
    previous."""
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
                "date": item["date"],
                "impact": item["impact"],
                "forecast": item.get("forecast", ""),
                "previous": item.get("previous", ""),
            })
        except KeyError:
            log.warning("Skipping malformed calendar event: %r", item)
    return events


def parse_event_time(date_str: str) -> datetime:
    return datetime.fromisoformat(date_str).astimezone(timezone.utc)


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
    return (event["country"], event["title"], event["date"])


def merge_events(path: str, events: list[dict]) -> int:
    """Idempotently merges `events` into the local store, deduplicated by
    (country, title, date) and rewritten in order - same pattern as
    candle_store.merge_history, for the same reason: this is called every
    week with a feed that mostly repeats recurring events, so it must be
    safe to call repeatedly with overlapping data without accumulating
    duplicate rows. Returns how many new rows were added."""
    by_key: dict[tuple, dict] = {_event_key(e): e for e in load_events(path)}
    before = len(by_key)
    for e in events:
        by_key[_event_key(e)] = e
    added = len(by_key) - before
    if added == 0:
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in sorted(by_key.values(), key=lambda e: e["date"]):
            f.write(json.dumps(e, sort_keys=True))
            f.write("\n")
    return added

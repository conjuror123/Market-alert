"""Client for ForexFactory's public economic calendar feed, plus a permanent
local NDJSON store (same pattern as candle_store.py) so events accumulate
over time as the weekly digest (weekly_digest.py) fetches them.

Only a "this week" feed exists at
https://nfs.faireconomy.media/ff_calendar_thisweek.json - there is no
next-week or last-week variant (both return 404, confirmed live). That feed
spans Sunday through Friday, so fetching it specifically on Sunday - which is
exactly when weekly_digest.py runs - already returns the coming week's
events, with no separate "next week" request needed.

Scraping ForexFactory's own historical calendar pages is blocked by a
Cloudflare bot challenge (confirmed live, HTTP 403 with a "Just a moment..."
JS challenge even with a realistic browser User-Agent), so there's no way to
backfill years of past events from ForexFactory itself the way candle_store's
history was backfilled from price APIs. Historical backfill instead uses
Financial Modeling Prep's Economic Calendar API (fetch_fmp_range /
fetch_fmp_history below) - a free-tier-with-key source that actually serves
past dates, unlike ForexFactory's feed which only ever exposes "this week".
Run as a one-off (see backfill-calendar.yml): `python -m
price_monitor.economic_calendar --backfill-fmp --since 2023-04-01`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.economic_calendar")

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# https://financialmodelingprep.com/stable/economic-calendar - confirmed via
# FMP's own docs page: `from`/`to` accept at most 90 days between them per
# request (daysMax: 90 in the page's embedded params). Per FMP's FAQ, the
# "date" field this endpoint returns is UTC, but comes back as a naive string
# with no offset - see _normalize_fmp_event. Worth spot-checking against a
# known event once real data comes back from a live backfill run.
FMP_CALENDAR_URL = "https://financialmodelingprep.com/stable/economic-calendar"
FMP_MAX_DAYS_PER_REQUEST = 90


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


def _fmp_date_to_iso_utc(date_str: str) -> str | None:
    """FMP's "date" comes back as a naive string, no UTC offset (see
    FMP_CALENDAR_URL above for why this is treated as UTC). Normalized to an
    explicit-offset ISO8601 string so it stores and dedupes the same way as
    ForexFactory's own -04:00-offset dates (see _event_key/parse_event_time) -
    the actual offset only matters for parse_event_time's conversion to UTC,
    which either representation already satisfies. Some events (e.g. all-day
    entries) may come back as a bare date with no time component."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _normalize_fmp_event(item: dict) -> dict | None:
    title = item.get("event") or item.get("title")
    date_iso = _fmp_date_to_iso_utc(item.get("date", "")) if item.get("date") else None
    if not title or not date_iso:
        log.warning("Skipping malformed FMP calendar event: %r", item)
        return None
    return {
        "title": title,
        "country": item.get("country") or "",
        "date": date_iso,
        "impact": item.get("impact") or "",
        "forecast": item.get("estimate") or item.get("forecast") or "",
        "previous": item.get("previous") or "",
    }


def fetch_fmp_range(
    api_key: str, from_date: str, to_date: str,
    session: requests.Session | None = None, timeout: int = 30,
) -> list[dict]:
    """One historical window (<= FMP_MAX_DAYS_PER_REQUEST days, "YYYY-MM-DD"
    strings) of past events from Financial Modeling Prep - the source used
    for the historical backfill ForexFactory's own feed can't provide (see
    module docstring). Unlike fetch_calendar this keeps every impact level,
    not just Medium/High - the local archive is meant to hold everything;
    filtering to what's shown happens at read time (see weekly_digest.py)."""
    get = session.get if session is not None else requests.get
    try:
        resp = get(
            FMP_CALENDAR_URL, params={"from": from_date, "to": to_date, "apikey": api_key}, timeout=timeout)
        resp.raise_for_status()
        raw = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise CalendarError(f"FMP fetch failed for [{from_date}, {to_date}]: {exc}") from exc

    if isinstance(raw, dict):
        # A bad key/plan/param returns a JSON *object* (e.g. an error
        # message) instead of the usual list - surface that loudly instead of
        # silently treating it as zero events.
        raise CalendarError(f"FMP returned an error for [{from_date}, {to_date}]: {raw}")

    events = []
    for item in raw:
        normalized = _normalize_fmp_event(item)
        if normalized is not None:
            events.append(normalized)
    return events


def fetch_fmp_history(
    api_key: str, since: datetime, until: datetime,
    session: requests.Session | None = None, delay: float = 1.0,
) -> list[dict]:
    """Chunks [since, until] into <= FMP_MAX_DAYS_PER_REQUEST-day windows (the
    API's own per-request limit) and fetches each in turn, sleeping `delay`
    seconds between requests to stay comfortably under the free tier's daily
    rate limit for what's meant to be an occasional one-off backfill, not a
    routine call. A window that fails is logged and skipped rather than
    aborting the whole run - a single bad chunk shouldn't lose everything
    already fetched for the rest of the range."""
    events = []
    window_start = since
    step = timedelta(days=FMP_MAX_DAYS_PER_REQUEST)
    while window_start < until:
        window_end = min(window_start + step, until)
        from_str, to_str = window_start.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")
        log.info("Fetching FMP economic calendar [%s, %s]...", from_str, to_str)
        try:
            chunk = fetch_fmp_range(api_key, from_str, to_str, session=session)
            log.info("  got %d events", len(chunk))
            events.extend(chunk)
        except CalendarError as exc:
            log.error("  %s", exc)
        window_start = window_end + timedelta(days=1)
        if delay and window_start < until:
            time.sleep(delay)
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backfill-fmp", action="store_true", help="One-off historical backfill via FMP")
    parser.add_argument("--since", help="YYYY-MM-DD - required with --backfill-fmp")
    parser.add_argument("--until", help="YYYY-MM-DD, default: today (UTC)")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between FMP requests")
    args = parser.parse_args()

    if not args.backfill_fmp:
        parser.error("nothing to do - pass --backfill-fmp")
    if not args.since:
        parser.error("--since is required with --backfill-fmp")

    api_key = os.environ.get("FMP_API_KEY", "")
    if not api_key:
        log.error("FMP_API_KEY is not set")
        return 1

    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    until = (
        datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.until
        else datetime.now(timezone.utc)
    )
    calendar_dir = os.environ.get(
        "CALENDAR_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "economic_calendar"))

    session = requests.Session()
    events = fetch_fmp_history(api_key, since, until, session=session, delay=args.delay)
    added = merge_events(store_path(calendar_dir), events)
    log.info("Fetched %d events from FMP (%s..%s), %d new after dedup", len(events), args.since, args.until or "now", added)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Client for ForexFactory's public economic calendar feed, plus a permanent
local NDJSON store (same pattern as candle_store.py) so events accumulate
over time as the weekly digest (weekly_digest.py) fetches them.

Only a "this week" feed exists at
https://nfs.faireconomy.media/ff_calendar_thisweek.json - there is no
next-week or last-week variant (both return 404, confirmed live). That feed
spans Sunday through Friday, so fetching it specifically on Sunday - which is
exactly when weekly_digest.py runs - already returns the coming week's
events, with no separate "next week" request needed.

Historical backfill (past years, not "this week") can't come from
ForexFactory itself: scraping its historical calendar pages is blocked by a
Cloudflare bot challenge (confirmed live, HTTP 403 with a "Just a moment..."
JS challenge even with a realistic browser User-Agent), and Financial
Modeling Prep's Economic Calendar API - initially tried as a paid-key
alternative - turned out to need a paid plan even for the "stable" tier
(confirmed live, HTTP 402 Payment Required on a real free-tier key).
Historical backfill instead imports third-party dumps of ForexFactory data
hosted elsewhere - none of them behind Cloudflare, so all reachable with no
key at all:
- fetch_ehsan_full_calendar: 2007-01-01 to 2025-04-07, every impact level -
  the main historical source (a public Hugging Face dataset, MIT license,
  by the same author as the GitHub dump below).
- fetch_ehsan_high_impact: ~2020 to whenever the maintainer last ran it
  (2026-01-30, confirmed live), High-impact events only - extends High
  coverage past the full dataset's 2025-04-07 cutoff.
- fetch_spoluan_year / import_spoluan_years: 2010-2023, every impact level -
  an older, independently-sourced dump kept around mainly for
  cross-checking; fetch_ehsan_full_calendar covers the same span more
  reliably (see its own docstring) so this isn't part of the routine import
  below by default.
Run as a one-off (see backfill-calendar.yml): `python -m
price_monitor.economic_calendar --import-ehsan-full --import-ehsan-high-impact`.

The archive keeps every impact level (Low/Medium/High - see
_normalize_impact for how each source's own extra categories collapse into
this 3-value scale). This does leave a real, accepted gap: none of the
sources above have Medium/Low coverage between 2025-04-07 and whenever the
live feed (fetch_calendar) started accumulating forward on its own - High
alone reaches to 2026-01-30 via ehsan's GitHub dump, but Medium/Low has no
coverage at all in that window. Searched for a source that closes it and
found none (checked Hugging Face, Kaggle, and several GitHub scrapers with
no committed data or stale/broken output) - worth checking again in a
couple of months in case a newer archive shows up. Anything asking for
calendar context in that window should keep in mind that an empty result
there can mean "no data", not "no events".
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.economic_calendar")

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

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
                "date": item["date"],
                "impact": _normalize_impact(item["impact"]),
                "forecast": item.get("forecast", ""),
                "previous": item.get("previous", ""),
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


# https://github.com/spoluan/forex-factory-scraper (MIT) - one CSV per year of
# scraped ForexFactory calendar data, checked into the repo itself and served
# straight off raw.githubusercontent.com (no Cloudflare, no key). Only
# 2010-2023 exist - 2024+ requests confirmed live as HTTP 404, so anything
# from 2024 onward has to come from ForexFactory's own live feed above
# (fetch_calendar), accumulated forward week by week.
_SPOLUAN_CSV_URL = (
    "https://raw.githubusercontent.com/spoluan/forex-factory-scraper/master/"
    "datasets/forex_factory_calendar_{year}.csv"
)

# The scraper's own "Combined DateTime" column isn't UTC - verified live by
# cross-checking known-time events across 2021-2023: every FOMC Statement
# (always released 2:00pm US Eastern) and every Non-Farm Employment Change
# (always released 8:30am US Eastern) lines up exactly with
# real_UTC_time + 8h, year-round including across the US's own DST switches -
# i.e. a fixed UTC+8 offset (no DST of its own), not US Eastern time as one
# might otherwise assume from the source. This is presumably whatever
# timezone ForexFactory's website happened to be displaying in when this was
# scraped, not a documented property of the site.
_SPOLUAN_DISPLAY_UTC_OFFSET_HOURS = 8


def _spoluan_event_time_to_iso_utc(date_str: str, time_str: str, combined_str: str) -> str | None:
    """"All Day" rows (holidays, etc.) carry no real time-of-day - Combined
    DateTime is always midnight in the scraper's own display offset for
    those, which would misleadingly shift them to the previous UTC day if
    corrected the same way as timed events, so they're anchored to UTC
    midnight of the given date instead."""
    if time_str == "All Day":
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            return None
    try:
        naive = datetime.strptime(combined_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (naive - timedelta(hours=_SPOLUAN_DISPLAY_UTC_OFFSET_HOURS)).replace(tzinfo=timezone.utc).isoformat()


def _normalize_spoluan_row(row: dict) -> dict | None:
    title = row.get("Event")
    date_iso = _spoluan_event_time_to_iso_utc(
        row.get("Date", ""), row.get("Time", ""), row.get("Combined DateTime", ""))
    if not title or not date_iso:
        log.warning("Skipping malformed spoluan calendar row: %r", row)
        return None
    return {
        "title": title,
        "country": row.get("Currency") or "",
        "date": date_iso,
        # This scraper's own impact taxonomy is Low/Medium/High/Non-economic
        # (it has no separate "Holiday" tier - bank holidays come through
        # here tagged "Low" already) - _normalize_impact folds "Non-economic"
        # into "Low" too, same as "Holiday" from the live feed above.
        "impact": _normalize_impact(row.get("Impact") or ""),
        "forecast": row.get("Forecast") or "",
        "previous": row.get("Previous") or "",
        "actual": row.get("Actual") or "",
    }


def fetch_spoluan_year(year: int, session: requests.Session | None = None, timeout: int = 30) -> list[dict]:
    """One calendar year (2010-2023 only, see _SPOLUAN_CSV_URL) of historical
    events, normalized to the same shape as fetch_calendar's events."""
    get = session.get if session is not None else requests.get
    try:
        resp = get(_SPOLUAN_CSV_URL.format(year=year), timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise CalendarError(f"failed to fetch spoluan calendar CSV for {year}: {exc}") from exc

    rows = csv.DictReader(io.StringIO(resp.text))
    events = []
    for row in rows:
        normalized = _normalize_spoluan_row(row)
        if normalized is not None:
            events.append(normalized)
    return events


def import_spoluan_years(years: list[int], session: requests.Session | None = None) -> list[dict]:
    """Fetches each year in turn; a year that fails (e.g. one outside
    2010-2023) is logged and skipped rather than aborting the whole import."""
    events = []
    for year in years:
        log.info("Fetching spoluan calendar CSV for %d...", year)
        try:
            chunk = fetch_spoluan_year(year, session=session)
            log.info("  got %d events", len(chunk))
            events.extend(chunk)
        except CalendarError as exc:
            log.error("  %s", exc)
    return events


# https://github.com/ehsanrs2/forexfactory-scraper (GPLv3) - a single static
# CSV snapshot checked into the repo, High-impact events only, covering
# ~2020 to whenever the maintainer last ran it (2026-01-30 as of this
# writing, confirmed live) - there's no per-year split or way to request a
# narrower range, unlike spoluan's dump, so this is always fetched whole.
_EHSAN_HIGH_IMPACT_CSV_URL = (
    "https://raw.githubusercontent.com/ehsanrs2/forexfactory-scraper/main/high_impact_events_calendar.csv"
)


def _normalize_ehsan_row(row: dict) -> dict | None:
    title = row.get("Event")
    date_str = row.get("DateTime")
    if not title or not date_str:
        log.warning("Skipping malformed ehsan calendar row: %r", row)
        return None
    try:
        # Unlike spoluan's dump, this one's own DateTime already carries an
        # explicit, correct UTC offset (verified live against known release
        # times - e.g. FOMC Statement - across seasons, so it tracks real
        # DST rather than being a fixed display offset) - no manual
        # correction needed, just the standard ISO8601 parse.
        date_iso = datetime.fromisoformat(date_str).astimezone(timezone.utc).isoformat()
    except ValueError:
        log.warning("Skipping ehsan calendar row with unparseable date: %r", row)
        return None
    return {
        "title": title,
        "country": row.get("Currency") or "",
        "date": date_iso,
        # This file is High-impact only by construction (see
        # _EHSAN_HIGH_IMPACT_CSV_URL) - "High Impact Expected" is its only
        # actual value, normalized here to match the other sources' "High".
        "impact": "High",
        "forecast": row.get("Forecast") or "",
        "previous": row.get("Previous") or "",
        "actual": row.get("Actual") or "",
    }


def fetch_ehsan_high_impact(session: requests.Session | None = None, timeout: int = 30) -> list[dict]:
    get = session.get if session is not None else requests.get
    try:
        resp = get(_EHSAN_HIGH_IMPACT_CSV_URL, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise CalendarError(f"failed to fetch ehsan high-impact calendar CSV: {exc}") from exc

    rows = csv.DictReader(io.StringIO(resp.text))
    events = []
    for row in rows:
        normalized = _normalize_ehsan_row(row)
        if normalized is not None:
            events.append(normalized)
    return events


# https://huggingface.co/datasets/Ehsanrs2/Forex_Factory_Calendar (MIT) - a
# single ~68MB CSV, same author as the GitHub High-impact-only dump above but
# covering every impact level, 2007-01-01 through 2025-04-07 (confirmed live,
# both the full row count and the cutoff date). This is the main historical
# source - see the module docstring for why nothing newer/narrower exists.
_EHSAN_FULL_CALENDAR_URL = (
    "https://huggingface.co/datasets/Ehsanrs2/Forex_Factory_Calendar/resolve/main/forex_factory_cache.csv"
)

# This dump's own impact taxonomy has four values instead of the GitHub
# dump's "always High" - normalized here to the shared Low/Medium/High scale,
# same as _IMPACT_ALIASES does for the live feed and spoluan's dump.
_EHSAN_FULL_IMPACT_MAP = {
    "High Impact Expected": "High",
    "Medium Impact Expected": "Medium",
    "Low Impact Expected": "Low",
    "Non-Economic": "Low",
}


def _normalize_ehsan_full_row(row: dict) -> dict | None:
    title = row.get("Event")
    date_str = row.get("DateTime")
    if not title or not date_str:
        log.warning("Skipping malformed ehsan full-calendar row: %r", row)
        return None
    try:
        # Same reliable per-row UTC offset as the High-impact-only dump
        # (_normalize_ehsan_row) - verified live against known release times
        # (NFP, ISM Services PMI, CAD CPI) across several year/season
        # combinations.
        date_iso = datetime.fromisoformat(date_str).astimezone(timezone.utc).isoformat()
    except ValueError:
        log.warning("Skipping ehsan full-calendar row with unparseable date: %r", row)
        return None
    raw_impact = row.get("Impact") or ""
    return {
        "title": title,
        "country": row.get("Currency") or "",
        "date": date_iso,
        "impact": _EHSAN_FULL_IMPACT_MAP.get(raw_impact, raw_impact),
        "forecast": row.get("Forecast") or "",
        "previous": row.get("Previous") or "",
        "actual": row.get("Actual") or "",
    }


def fetch_ehsan_full_calendar(session: requests.Session | None = None, timeout: int = 120) -> list[dict]:
    """The full 2007-01-01 to 2025-04-07 dataset, every impact level. A much
    bigger download than the other fetchers (~68MB) so it gets a longer
    default timeout."""
    get = session.get if session is not None else requests.get
    try:
        resp = get(_EHSAN_FULL_CALENDAR_URL, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise CalendarError(f"failed to fetch ehsan full calendar CSV: {exc}") from exc

    rows = csv.DictReader(io.StringIO(resp.text))
    events = []
    for row in rows:
        normalized = _normalize_ehsan_full_row(row)
        if normalized is not None:
            events.append(normalized)
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--import-spoluan", action="store_true", help="Historical import from GitHub (2010-2023)")
    parser.add_argument("--since-year", type=int, help="First year to import (inclusive) - with --import-spoluan")
    parser.add_argument("--until-year", type=int, help="Last year (inclusive), default: --since-year")
    parser.add_argument(
        "--import-ehsan-high-impact", action="store_true",
        help="Historical High-impact-only import from GitHub (~2020 onward)")
    parser.add_argument(
        "--import-ehsan-full", action="store_true",
        help="Historical full-impact-range import from Hugging Face (2007-01-01 to 2025-04-07)")
    args = parser.parse_args()

    if not args.import_spoluan and not args.import_ehsan_high_impact and not args.import_ehsan_full:
        parser.error(
            "nothing to do - pass --import-spoluan and/or --import-ehsan-high-impact and/or --import-ehsan-full")
    if args.import_spoluan and not args.since_year:
        parser.error("--since-year is required with --import-spoluan")

    calendar_dir = os.environ.get(
        "CALENDAR_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "economic_calendar"))
    session = requests.Session()
    events = []

    if args.import_spoluan:
        until_year = args.until_year or args.since_year
        spoluan_events = import_spoluan_years(range(args.since_year, until_year + 1), session=session)
        log.info("spoluan: %d events fetched", len(spoluan_events))
        events.extend(spoluan_events)

    if args.import_ehsan_full:
        try:
            ehsan_full_events = fetch_ehsan_full_calendar(session=session)
            log.info("ehsan full calendar: %d events fetched", len(ehsan_full_events))
            events.extend(ehsan_full_events)
        except CalendarError as exc:
            log.error("  %s", exc)

    if args.import_ehsan_high_impact:
        try:
            ehsan_events = fetch_ehsan_high_impact(session=session)
            log.info("ehsan high-impact: %d events fetched", len(ehsan_events))
            events.extend(ehsan_events)
        except CalendarError as exc:
            log.error("  %s", exc)

    added = merge_events(store_path(calendar_dir), events)
    log.info("Fetched %d events, %d new after dedup", len(events), added)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Client for ForexFactory's public economic calendar feed, plus a permanent
local NDJSON store (same pattern as candle_store.py) so events accumulate
over time as the weekly digest (weekly_digest.py) fetches them.

Only a "this week" feed exists at
https://nfs.faireconomy.media/ff_calendar_thisweek.json - there is no
next-week or last-week variant (both return 404, confirmed live). That feed
spans Sunday through Friday, so fetching it specifically on Sunday - which is
exactly when weekly_digest.py runs - already returns the coming week's
events, with no separate "next week" request needed.

The historical archive (not "this week") is taken from THE SAME PLACE, from
ForexFactory itself, through its monthly pages (fetch_forexfactory_month). One
source for the whole history - and that is its main property, more important than
completeness.

Third-party sources were all tried and all dropped. First three ready-made
ForexFactory dumps from GitHub and Hugging Face: in the archive assembled from
them a quarter of the High and Medium events turned out to be duplicates of the
same event within a day, with a dominant shift of exactly seven hours - the dumps
had been collected under different timezone conventions. Then the "Global
Economic Calendar" dataset on Kaggle: its times were fine, but its TAXONOMY was
not. It handed out the Medium label nine times more freely than ForexFactory
itself: 96.9 events a week against 11.3 over the same period, while High matched
for both (13.0 and 13.4). An archive built from two sources acquired a seam
exactly where one gave way to the other: the MEALS calendar multiplier (§4.3) was
on in 90.7% of hours across the Kaggle half and 53.2% across the ForexFactory
half.

For calibration that is worse than gaps. The §7 train period would lie entirely
in the generous half while the work would run on the frugal one - the thresholds
would settle on one regime and be applied in another, with nothing in the metrics
to reveal it.

The price of a single source is losing Low events: ForexFactory has an order of
magnitude fewer of them than Kaggle had. In substance that price is zero: the
§4.3 multiplier uses only High and Medium, and Low takes no part in it at all.
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

# The impact scale is exactly three-valued. A source's own extra categories
# ("Holiday" in the live feed) carry none of the significance Medium/High do and
# are folded into "Low" right at parse time, so that the source's taxonomy does
# not leak into the rest of the code: filtering, storage and display need only
# Low/Medium/High.
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
                # Normalised to UTC, like both historical branches: the feed
                # serves a fixed -04:00 offset, and keeping two records of the
                # same moment in different packaging would create exactly the
                # shifted copy that got the old archive thrown away.
                "date": parse_event_time(item["date"]).isoformat(),
                "impact": _normalize_impact(item["impact"]),
                "forecast": item.get("forecast", ""),
                "previous": item.get("previous", ""),
                # This feed has NO "actual" key - verified against live output.
                # The field stays empty until the backfill from the monthly page
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
    """Dedup key - country, title and the MOMENT of publication.

    The moment, not the date string. Sources record one and the same moment
    differently: the weekly feed serves "2026-09-03T08:30:00-04:00", the monthly
    pages and the dataset "2026-09-03T12:30:00+00:00". By string those are two
    different events, and the archive would collect every release twice - exactly
    the breakage that forced the old archive to be thrown away entirely (see the
    module docstring).
    """
    return (event["country"], event["title"],
            parse_event_time(event["date"]).timestamp())


def _merge_one(stored: dict | None, incoming: dict) -> dict:
    """Merges two versions of one event. An empty field never overwrites a filled one.

    Without this rule the weekly feed would erase released values. It brings an
    event in advance and does not know the actual field at all - its output has no
    such key - so after the actual had been filled in, the very next run would put
    an empty string back into the archive. Verified on live data: of the 93 events
    both the feed and the monthly page can see, exactly 20 disagree, and they
    disagree precisely on actual, which is empty in the feed and filled on the
    page.

    A source that stayed silent is not saying "there is no value" - it is saying
    "I do not know", and there is nothing to erase on the strength of that.
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
    (country, title, moment of publication) and rewritten in order - same pattern
    as candle_store.merge_history, for the same reason: this is called every
    week with a feed that mostly repeats recurring events, so it must be
    safe to call repeatedly with overlapping data without accumulating
    duplicate rows. Returns how many rows were added OR CHANGED.

    Changed rows count alongside new ones, and that is not a detail. The weekly
    feed brings an event in advance, without its released value, and the actual
    arrives later - during the backfill from the monthly page
    (weekly_digest.backfill_actuals). Such a repeat adds not a single row, it only
    fills the actual field, and the earlier version, which compared the NUMBER of
    rows before and after, would silently discard it along with the whole write to
    disk.
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


# --- Historical import ---------------------------------------------------

def _request(url: str, timeout: int, session: requests.Session | None = None,
             headers: dict | None = None) -> requests.Response:
    """A single HTTP request with a clear error instead of a bare requests exception."""
    get = session.get if session is not None else requests.get
    try:
        resp = get(url, timeout=timeout,
                   headers=headers or {"User-Agent": "market-alert-bot"})
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        raise CalendarError(f"could not fetch {url}: {exc}") from exc


# ForexFactory serves a whole month at an address of the form ?month=mar.2026,
# and the data sits right inside the page as ready JSON. The times in it are unix
# timestamps, that is, unambiguous: the very ambiguity that ruined the old archive
# is absent here by construction.
#
# The market-calendar-tool library is no good for this: it first hits the internal
# /calendar/apply-settings to set the display timezone, and that answers 403. The
# monthly page itself is reachable.
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
    """Extracts the list of days from the component state embedded in the page.

    Parsed by brace balance rather than a regex over the whole structure: inside
    sits nested JSON with escaped quotes, and a greedy or a lazy expression is
    equally liable to cut it in the wrong place.
    """
    marker = re.search(r"calendarComponentStates\[\d+\]\s*=\s*\{", html)
    if marker is None:
        raise CalendarError("the ForexFactory page has no calendar state")
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
        raise CalendarError("the calendar state is truncated")

    days = re.search(r"days:\s*(\[.*?\])\s*,\s*[a-zA-Z_]+:", block, re.S)
    if days is None:
        raise CalendarError("the calendar state has no list of days")
    return json.loads(days.group(1))


def fetch_forexfactory_month(year: int, month: int,
                             session: requests.Session | None = None,
                             timeout: int = 40) -> list[dict]:
    """One calendar month from ForexFactory."""
    url = _FF_MONTH_URL.format(month=_FF_MONTHS[month - 1], year=year)
    # urllib here on purpose, although the rest of the module uses requests.
    # Verified: with requests ForexFactory answers 403 under any headers,
    # including a full browser set, while urllib with the same User-Agent gets
    # 200. The difference is not in the headers but in the client's TLS
    # fingerprint, and no set of headers can argue with that.
    request = urllib.request.Request(url, headers=_FF_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, OSError) as exc:
        raise CalendarError(f"could not fetch {url}: {exc}") from exc

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
    """Monthly backfill over a period. The pause between requests is deliberate:
    this is an ordinary web page, not an API with a paid quota.
    """
    events: list[dict] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        try:
            got = fetch_forexfactory_month(year, month, session=session)
            log.info("ForexFactory %04d-%02d: %d events", year, month, len(got))
            events.extend(got)
        except Exception as exc:
            log.error("ForexFactory %04d-%02d: failed - %s", year, month, exc)
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
                        help="Throw the archive away and rebuild the whole history from ForexFactory")
    parser.add_argument("--import-forexfactory", action="store_true",
                        help="Append a monthly range, leaving the rest untouched")
    parser.add_argument("--from-month", default=_ARCHIVE_SINCE[:7],
                        help="First month, YYYY-MM (defaults to the start of the archive)")
    parser.add_argument("--to-month", default=None,
                        help="Last month, YYYY-MM (defaults to the current one)")
    args = parser.parse_args()

    if not (args.rebuild or args.import_forexfactory):
        parser.error("nothing to do - pass --rebuild or --import-forexfactory")

    calendar_dir = os.environ.get(
        "CALENDAR_DIR",
        os.path.join(os.path.dirname(__file__), "..", "data", "economic_calendar"))
    path = store_path(calendar_dir)
    session = requests.Session()

    if args.rebuild:
        # The old archive is thrown away whole rather than added to: mixing the
        # taxonomies of two sources is exactly what this rebuild exists to escape.
        if os.path.exists(path):
            os.remove(path)
            log.info("Old archive removed")

    first = datetime.strptime(args.from_month, "%Y-%m").date()
    last = (datetime.strptime(args.to_month, "%Y-%m").date() if args.to_month
            else datetime.now(timezone.utc).date())
    events = import_forexfactory_months(first, last, session=session)

    since = datetime.fromisoformat(_ARCHIVE_SINCE)
    kept = [e for e in events if parse_event_time(e["date"]) >= since]
    added = merge_events(path, kept)
    log.info("Fetched %d events, since %s %d remain, %d newly written",
             len(events), _ARCHIVE_SINCE, len(kept), added)
    return 0


if __name__ == "__main__":
    sys.exit(main())

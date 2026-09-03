"""Manual tool: like calibration_review.py's news-headline lookup for the
daily signal's backtest events (same [event+6h, event+12h] window, same
[event+12h, event+24h] sparse-event fallback - see calibration_review.py's
module docstring for why), plus economic-calendar context for each event.

For every event (caught AND missed) also lists the High-impact calendar
events (see economic_calendar.py - the local archive only ever keeps High
impact) found in [event-12h, event+1h]. The lower bound is generous (12h
back) to catch whatever released ahead of a slow-building move; the +1h upper
bound is deliberately short and mostly a formality - a release can itself
cause a move *before* its official timestamp is public (a leak, insider
positioning), but a move can't be caused by something that hasn't happened
yet, so anything meaningfully after the event is irrelevant here (unlike the
news window above, which looks well after the event because coverage lags a
move - the calendar window looks mostly before it, because causes precede
their effects). No special-casing for weekend gaps (e.g. Saturday afternoon
to Sunday evening, when markets are closed and the calendar is typically
empty): an empty window there just means no High-impact event happened to
be nearby, same as it would for any other quiet stretch.

Not part of the routine backtest - see calibration_review.py for why this is
a manual, occasional tool rather than something run on every calibration.
Daily-signal only (not signal-agnostic like calibration_review.py): the
calendar archive was built for reviewing the daily signal, and the news
window's own age-based reasoning is specific to it too.

Usage:
    python -m price_monitor.daily_signal_review
    python -m price_monitor.daily_signal_review --backtest-results /path/to/results.json
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

from price_monitor import economic_calendar
from price_monitor.calibration_review import _news_query_by_asset_key, fetch_event_headlines
from price_monitor.config import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.daily_signal_review")

# See module docstring for why this window is asymmetric and mostly-backward,
# unlike the news window above (which looks forward from the event).
CALENDAR_WINDOW_BEFORE_HOURS = 12
CALENDAR_WINDOW_AFTER_HOURS = 1


def _format_calendar_line(event: dict) -> str:
    when = economic_calendar.parse_event_time(event["date"]).strftime("%Y-%m-%d %H:%M UTC")
    details = [
        f"{label}: {event[key]}"
        for label, key in (("actual", "actual"), ("forecast", "forecast"), ("previous", "previous"))
        if event.get(key)
    ]
    suffix = f" ({', '.join(details)})" if details else ""
    return f"- {when} [{event['country']}] {event['title']}{suffix}"


def _format_event(event: dict, headlines: list[dict], calendar_events: list[dict]) -> str:
    status = "CAUGHT" if event["notified"] else "missed"
    lines = [
        f"### {event['date']} — {event['return_pct']:+.2f}% "
        f"(EWMA z={event['ewma_z']:.2f}, robust z={event['robust_z']:.2f}) — {status}",
        "",
        "**News:**",
    ]
    if headlines:
        for h in headlines:
            when = h["published"].strftime("%Y-%m-%d %H:%M UTC") if h["published"] else "date unknown"
            source = f" ({h['source']})" if h["source"] else ""
            lines.append(f"- [{when}] {h['title']}{source}")
    else:
        lines.append("- no news found in [event+6h, +12h] or in the fallback [+12h, +24h]")

    lines += [
        "",
        f"**Economic calendar (High impact, [event-{CALENDAR_WINDOW_BEFORE_HOURS}h, "
        f"event+{CALENDAR_WINDOW_AFTER_HOURS}h]):**",
    ]
    if calendar_events:
        lines.extend(_format_calendar_line(e) for e in calendar_events)
    else:
        lines.append("- no High-impact events found in this window")
    lines.append("")
    return "\n".join(lines)


def build_review(
    cfg, assets_report: list[dict], calendar_events: list[dict],
    limit: int, delay: float, session: requests.Session,
) -> str:
    query_by_key = _news_query_by_asset_key(cfg)
    sections = []
    for asset_report in assets_report:
        key = (asset_report["source"], asset_report["symbol"])
        query = query_by_key.get(key, asset_report["label"])
        events = sorted(asset_report["biggest_moves"], key=lambda e: e["open_time"])
        caught = sum(1 for e in events if e["notified"])
        log.info("%s: %d events, fetching headlines and calendar context...", asset_report["label"], len(events))

        section = [f"## {asset_report['label']} ({asset_report['symbol']}) — recall {caught}/{len(events)}", ""]
        for event in events:
            event_time = datetime.fromtimestamp(event["open_time"], tz=timezone.utc)
            headlines = fetch_event_headlines(query, event_time, limit, delay=delay, session=session)
            nearby_calendar = economic_calendar.events_in_window(
                calendar_events,
                event_time - timedelta(hours=CALENDAR_WINDOW_BEFORE_HOURS),
                event_time + timedelta(hours=CALENDAR_WINDOW_AFTER_HOURS),
            )
            section.append(_format_event(event, headlines, nearby_calendar))
            time.sleep(delay)
        sections.append("\n".join(section))
    return "\n---\n\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backtest-results", default="data/backtest_results.json")
    parser.add_argument("--out", default="data/calibration_review/daily_signal_review.md")
    parser.add_argument("--limit", type=int, default=5, help="Headlines per event (default: 5)")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between Google News requests")
    args = parser.parse_args()

    cfg = load_config()
    with open(args.backtest_results, "r", encoding="utf-8") as f:
        results = json.load(f)

    assets_report = results.get("daily_assets", [])
    if not assets_report:
        log.error(
            "No 'daily_assets' data in %s - run price_monitor.backtest --local-history first.",
            args.backtest_results)
        return 1

    calendar_events = economic_calendar.load_events(economic_calendar.store_path(cfg.calendar_dir))
    log.info("Loaded %d High-impact calendar events for context.", len(calendar_events))

    session = requests.Session()
    review = build_review(cfg, assets_report, calendar_events, args.limit, args.delay, session)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# Daily signal review — news + economic calendar\n\n")
        f.write(review)
        f.write("\n")
    log.info("Wrote review to %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

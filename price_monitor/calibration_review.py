"""Manual tool: pulls real news headlines for every event in a backtest
report's top-N list (caught AND missed alike) so a human can judge, asset by
asset, which historical moves the current threshold should or shouldn't be
catching - instead of guessing from z-scores and recall percentages alone.

Tries the [event+6h, event+12h] window first; an event that comes up sparse
(see _SPARSE_HEADLINE_THRESHOLD) also gets a second, later, non-overlapping
[event+12h, event+24h] attempt before being reported as having no coverage -
a real move sometimes only gets written up after the first news cycle has
passed. The primary window itself is left alone rather than widened, since
it's already correct for events that do find enough coverage in it.

Not part of the routine backtest (price_monitor/backtest.py) - fetching up to
`--limit` headlines for potentially hundreds of events is slow, hits Google
News hundreds of times, and is meant to be run occasionally by hand while
tuning thresholds, not on every calibration run. Signal-agnostic (works for
either --signal hourly or --signal daily) even though only the daily signal
has been calibrated against real recall targets so far - see README,
"Дневной сигнал".

Usage:
    python -m price_monitor.calibration_review --signal daily
    python -m price_monitor.calibration_review --signal daily --backtest-results /path/to/results.json
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

from price_monitor.config import load_config
from price_monitor.explain import (
    NEWS_WINDOW_END_HOURS,
    NEWS_WINDOW_START_HOURS,
    _filter_after,
    _filter_before,
    _scope_query,
)
from price_monitor.news import NewsError, fetch_news

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor.calibration_review")


def _news_query_by_asset_key(cfg) -> dict[tuple[str, str], str]:
    return {(asset.source, asset.symbol): asset.news_query for asset in cfg.assets}


# How many raw candidates to pull from Google News before filtering down to
# the exact time window - matches explain.py's own limit=100. Google's
# after:/before: operators are day-granularity (see _scope_query), coarser
# than our hour-level windows, so under-fetching here silently starves the
# exact filter of candidates it would otherwise have kept - this bit us once
# already (see README, "Дневной сигнал"): passing the *final* desired
# headline count as this candidate pool meant a whole day's worth of Google
# results got cut down to a handful before the precise filter even ran.
_CANDIDATE_POOL_SIZE = 100

# If the primary [event+6h, event+12h] window leaves an event this sparse (or
# sparser), a second, later, non-overlapping window is tried as a fallback
# before giving up - a real move sometimes only gets covered by outlets after
# the first news cycle has already passed. Left alone (not widening the
# primary window itself) when the primary window already found enough on its
# own - see README, "Дневной сигнал".
_SPARSE_HEADLINE_THRESHOLD = 3
_FALLBACK_WINDOW_START_HOURS = 12
_FALLBACK_WINDOW_END_HOURS = 24


def _fetch_window(
    query: str, lower_cutoff: datetime, upper_cutoff: datetime, session: requests.Session | None,
) -> list[dict]:
    """One [lower_cutoff, upper_cutoff] fetch+exact-filter pass - the same
    Pacific-day query scoping price_monitor/explain.py uses for live alerts
    (see its module docstring for why that window and why Pacific days),
    reused as-is against a historical event time. Shared by both the primary
    and the sparse-event fallback window in fetch_event_headlines."""
    try:
        articles = fetch_news(
            _scope_query(query, lower_cutoff, upper_cutoff), limit=_CANDIDATE_POOL_SIZE, session=session)
    except NewsError as exc:
        log.warning("  news fetch failed for %r in [%s, %s]: %s", query, lower_cutoff, upper_cutoff, exc)
        return []
    articles = _filter_after(articles, lower_cutoff)
    articles = _filter_before(articles, upper_cutoff)
    return articles


def fetch_event_headlines(
    query: str, event_time: datetime, limit: int, delay: float = 0.0,
    session: requests.Session | None = None,
) -> list[dict]:
    """Tries [event+6h, event+12h] first; if that leaves fewer than
    _SPARSE_HEADLINE_THRESHOLD headlines, also tries the later, non-
    overlapping [event+12h, event+24h] window and adds whatever it finds -
    see _SPARSE_HEADLINE_THRESHOLD. An event that already found enough in the
    primary window never touches the fallback window at all."""
    lower_cutoff = event_time + timedelta(hours=NEWS_WINDOW_START_HOURS)
    upper_cutoff = event_time + timedelta(hours=NEWS_WINDOW_END_HOURS)
    headlines = _fetch_window(query, lower_cutoff, upper_cutoff, session)

    if len(headlines) < _SPARSE_HEADLINE_THRESHOLD:
        if delay:
            time.sleep(delay)
        fallback_lower = event_time + timedelta(hours=_FALLBACK_WINDOW_START_HOURS)
        fallback_upper = event_time + timedelta(hours=_FALLBACK_WINDOW_END_HOURS)
        headlines = headlines + _fetch_window(query, fallback_lower, fallback_upper, session)

    return headlines[:limit]


def _format_event(event: dict, headlines: list[dict]) -> str:
    status = "ПОЙМАНО" if event["notified"] else "пропущено"
    lines = [
        f"### {event['date']} — {event['return_pct']:+.2f}% "
        f"(EWMA z={event['ewma_z']:.2f}, робастный z={event['robust_z']:.2f}) — {status}",
        "",
    ]
    if headlines:
        for h in headlines:
            when = h["published"].strftime("%Y-%m-%d %H:%M UTC") if h["published"] else "дата неизвестна"
            source = f" ({h['source']})" if h["source"] else ""
            lines.append(f"- [{when}] {h['title']}{source}")
    else:
        lines.append("- новостей не найдено ни в [событие+6ч, +12ч], ни в резервном [+12ч, +24ч]")
    lines.append("")
    return "\n".join(lines)


def build_review(cfg, assets_report: list[dict], limit: int, delay: float, session: requests.Session) -> str:
    query_by_key = _news_query_by_asset_key(cfg)
    sections = []
    for asset_report in assets_report:
        key = (asset_report["source"], asset_report["symbol"])
        query = query_by_key.get(key, asset_report["label"])
        events = sorted(asset_report["biggest_moves"], key=lambda e: e["open_time"])
        caught = sum(1 for e in events if e["notified"])
        log.info("%s: %d events, fetching headlines...", asset_report["label"], len(events))

        section = [f"## {asset_report['label']} ({asset_report['symbol']}) — recall {caught}/{len(events)}", ""]
        for event in events:
            event_time = datetime.fromtimestamp(event["open_time"], tz=timezone.utc)
            headlines = fetch_event_headlines(query, event_time, limit, delay=delay, session=session)
            section.append(_format_event(event, headlines))
            time.sleep(delay)
        sections.append("\n".join(section))
    return "\n---\n\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--signal", choices=["hourly", "daily"], default="daily")
    parser.add_argument("--backtest-results", default="data/backtest_results.json")
    parser.add_argument("--out", default=None, help="Default: data/calibration_review/<signal>_signal_review.md")
    parser.add_argument("--limit", type=int, default=5, help="Headlines per event (default: 5)")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between Google News requests")
    args = parser.parse_args()

    out_path = args.out or f"data/calibration_review/{args.signal}_signal_review.md"

    cfg = load_config()
    with open(args.backtest_results, "r", encoding="utf-8") as f:
        results = json.load(f)

    key = "assets" if args.signal == "hourly" else "daily_assets"
    assets_report = results.get(key, [])
    if not assets_report:
        log.error("No %r data in %s - run price_monitor.backtest first.", key, args.backtest_results)
        return 1

    session = requests.Session()
    review = build_review(cfg, assets_report, args.limit, args.delay, session)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# Ревью калибровки — {args.signal} сигнал\n\n")
        f.write(review)
        f.write("\n")
    log.info("Wrote review to %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

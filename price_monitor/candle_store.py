"""Permanent local history of every candle the monitor has ever seen, plus
resampling that history into daily candles.

Stored one file per asset, one JSON object per line (NDJSON) - an hourly
production run only ever appends a line, so git diffs stay tiny no matter how
many years of history pile up (see README). Two things build on top of this:

- The daily signal (see __main__.py) needs day-scale history far longer than
  any single API call returns - it reads the full local file and resamples it
  into daily closes itself, rather than asking a provider for a native daily
  series (which would cost extra API credits every run - see README).
- Running a backtest (price_monitor/backtest.py) already fetches months of
  real history per asset anyway, so it merges that fetch into this same local
  store as a side effect - that's how the store gets seeded with enough
  history to be useful immediately, rather than waiting to accumulate one
  hour at a time.

Storage growth over years isn't addressed here - deliberately deferred until
it's an actual problem, not a hypothetical one.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone

from price_monitor.models import Candle


def safe_asset_filename(source: str, symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{source}_{symbol}")


def store_path(base_dir: str, source: str, symbol: str) -> str:
    return os.path.join(base_dir, f"{safe_asset_filename(source, symbol)}.ndjson")


def _candle_to_row(c: Candle) -> dict:
    return {
        "open_time": c.open_time, "open": c.open, "high": c.high,
        "low": c.low, "close": c.close, "volume": c.volume, "close_time": c.close_time,
    }


def _row_to_candle(row: dict) -> Candle:
    return Candle(
        open_time=row["open_time"], open=row["open"], high=row["high"], low=row["low"],
        close=row["close"], volume=row["volume"], close_time=row["close_time"],
    )


def load_candles(path: str) -> list[Candle]:
    if not os.path.exists(path):
        return []
    candles = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                candles.append(_row_to_candle(json.loads(line)))
    return candles


def append_candles(path: str, candles: list[Candle]) -> None:
    """Appends `candles` as-is, in open_time order. Callers are responsible
    for only passing candles not already stored (the production monitor
    tracks this in state.json - see __main__.py - rather than this module
    re-reading a growing file every run just to check the last line)."""
    if not candles:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for c in sorted(candles, key=lambda c: c.open_time):
            f.write(json.dumps(_candle_to_row(c), sort_keys=True))
            f.write("\n")


def merge_history(path: str, candles: list[Candle]) -> int:
    """Idempotently brings the local store up to the union of what's already
    there and `candles`, deduplicated by open_time and rewritten in order.

    Unlike `append_candles`, safe to call repeatedly with overlapping data
    (e.g. every time a backtest is re-run and re-fetches months of history) -
    it never produces duplicate rows. This does rewrite the whole file rather
    than only appending, so it's meant for occasional, manual/backtest use,
    not the hourly production path. Returns how many new rows were added.
    """
    by_time: dict[int, Candle] = {c.open_time: c for c in load_candles(path)}
    before = len(by_time)
    for c in candles:
        by_time[c.open_time] = c
    added = len(by_time) - before
    if added == 0:
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for c in sorted(by_time.values(), key=lambda c: c.open_time):
            f.write(json.dumps(_candle_to_row(c), sort_keys=True))
            f.write("\n")
    return added


def deduplicate(path: str) -> int:
    """Removes duplicate open_time rows from the file and rewrites it in ascending
    time order. Returns the number of rows dropped.

    The file is appended line by line, which makes it vulnerable to branch
    merges: if two branches wrote the same range of hours, git will splice both
    blocks in one after the other without noticing the repeat. That is exactly how
    a duplicated block of 299 hours once got into the history - in all sixteen
    files at once.

    When two versions of one hour disagree, the last in the file wins. The earlier
    copy may have caught the hour still open - lower volume and a narrower range -
    while a later download sees it whole. On real data the last copy was never
    once poorer than the earlier one.
    """
    if not os.path.exists(path):
        return 0
    candles = load_candles(path)
    by_time: dict[int, Candle] = {c.open_time: c for c in candles}
    removed = len(candles) - len(by_time)
    if removed == 0:
        return 0
    with open(path, "w", encoding="utf-8") as f:
        for c in sorted(by_time.values(), key=lambda c: c.open_time):
            f.write(json.dumps(_candle_to_row(c), sort_keys=True))
            f.write("\n")
    return removed


def daily_closes(candles: list[Candle], now: datetime | None = None) -> list[Candle]:
    """Resamples candles (any granularity) into one synthetic daily candle per
    UTC calendar day: close = that day's last close, volume = that day's
    summed volume. This is what makes the daily signal genuinely day-scale
    rather than a live-updating partial one (see README) - the boundary is
    always 00:00 UTC, chosen by us, regardless of which session convention
    the underlying data source itself uses for its own "daily" bar.

    The final calendar day is dropped unless it's already fully in the past
    relative to `now` - a day still in progress isn't a real day's move yet.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    by_day: dict[date, list[Candle]] = {}
    for c in sorted(candles, key=lambda c: c.open_time):
        day = datetime.fromtimestamp(c.open_time, tz=timezone.utc).date()
        by_day.setdefault(day, []).append(c)

    result = []
    for day in sorted(by_day):
        if day >= today:
            continue
        day_candles = by_day[day]
        day_start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
        result.append(Candle(
            open_time=day_start,
            open=day_candles[0].open,
            high=max(c.high for c in day_candles),
            low=min(c.low for c in day_candles),
            close=day_candles[-1].close,
            volume=sum(c.volume for c in day_candles),
            close_time=day_candles[-1].close_time,
        ))
    return result


def main(argv: list[str] | None = None) -> int:
    """One-off cleanup of the store from duplicates - see deduplicate.

    Kept as a command rather than a throwaway script: the cause of duplicates
    (a branch merge) can strike again while the history lives in NDJSON.
    """
    import argparse
    import glob

    parser = argparse.ArgumentParser(description="Remove duplicates from the local candle history")
    parser.add_argument("--dir", default=os.path.join("data", "candle_history"))
    parser.add_argument("--dry-run", action="store_true",
                        help="only report how many rows are redundant, rewriting nothing")
    args = parser.parse_args(argv)

    total = 0
    for path in sorted(glob.glob(os.path.join(args.dir, "*.ndjson"))):
        if args.dry_run:
            candles = load_candles(path)
            removed = len(candles) - len({c.open_time for c in candles})
        else:
            removed = deduplicate(path)
        total += removed
        if removed:
            print(f"{os.path.basename(path)}: duplicates {removed}")
    print(f"Duplicates in total: {total}" + (" (nothing rewritten)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

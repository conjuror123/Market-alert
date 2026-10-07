"""The official open: where a fund's night ends.

WHY. A fund's gap is the night's move, from one session's close to the next
one's open, and its first bar's move runs from that open. The store's first bar
opens at its feed's first print, and that print is not always the market's
open:
  - a stale print at the previous close, after which the bar trades far away.
    The night's move then lands in the first hour and the gap reads 0 (TLH
    2020-03-09: gap +0.03%, first hour +3.8%; the official gap was +4.65%).
    Across the 133 funds, 2002-2026, 3.9% of fund-days; 7-12% a year in
    2020-22, about 0.6% a year since. The zeros also collapse the nights'
    yardstick, so small real gaps read as 6 sigma;
  - IEX's first print, for the funds an IEX feed serves, tens of bp off the
    market's open on some mornings (Sina's consolidated bars, 2026-06-10 to
    10-06, sided with the official open 109 times, with the IEX store 12);
  - a price from before a split (IHI 2021-07-19 opened at 365.68 over a
    60.99 close).
No rule on the bar alone finds them (the best caught 7%, at 66% precision),
so the night is measured from the exchange's official open instead: Yahoo's
daily bar, its open over the previous day's close (jump.returns splices that
ratio onto the stored close).

WHERE IT COMES FROM. Live, from the answer the morning dividend check already
gets (jump.backfill.check_dividends): Yahoo's daily bars, on the run that scores
the gap. History, once: `python -m jump.backfill --official-opens`.

THE TABLE, data/jump/opens/: one row per fund and day - ticker, day, the
official open and the previous official close, as Yahoo gave them
(split-adjusted; only their ratio is used). One Parquet file per finished
year, one CSV per month of the current year. A row is never overwritten.
"""
from __future__ import annotations

import glob
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from jump import atomic

DEFAULT_DIR = os.path.join("data", "jump", "opens")
COLUMNS = ["ticker", "day", "open", "prev_close"]
NY = ZoneInfo("America/New_York")


def from_candles(ticker: str, candles, zone=NY) -> "list[dict]":
    """Rows from a fund's daily candles: each day's open, with the close of
    the candle before it. A flat candle (open = high = low = close) or one
    without a positive price is no open, and leaves its day out; its close
    still stands as the next day's previous close."""
    out = []
    ordered = sorted(candles, key=lambda c: c.open_time)
    for before, c in zip(ordered, ordered[1:]):
        flat = c.open == c.high == c.low == c.close
        if flat or not (c.open > 0 and before.close > 0):
            continue
        out.append({"ticker": ticker,
                    "day": datetime.fromtimestamp(c.open_time, zone).date().isoformat(),
                    "open": float(c.open), "prev_close": float(before.close)})
    return out


def _files(directory: str) -> "list[str]":
    return sorted(glob.glob(os.path.join(directory, "*.parquet"))
                  + glob.glob(os.path.join(directory, "*.csv")))


def load_frame(directory: str = DEFAULT_DIR) -> pd.DataFrame:
    parts = [pd.read_parquet(p) if p.endswith(".parquet")
             else pd.read_csv(p, dtype={"ticker": str, "day": str})
             for p in _files(directory)]
    if not parts:
        return pd.DataFrame(columns=COLUMNS)
    frame = pd.concat(parts, ignore_index=True)[COLUMNS]
    frame["day"] = frame["day"].astype(str)
    return frame.drop_duplicates(["ticker", "day"], keep="first")


def load(directory: str = DEFAULT_DIR) -> "dict[str, dict[str, float]]":
    """ticker -> ISO day -> official open over the previous official close."""
    frame = load_frame(directory)
    out: "dict[str, dict[str, float]]" = {}
    ratio = (frame["open"].astype(float) / frame["prev_close"].astype(float)).tolist()
    for ticker, day, r in zip(frame["ticker"], frame["day"], ratio):
        out.setdefault(ticker, {})[day] = r
    return out


def merge(rows: "list[dict]", directory: str = DEFAULT_DIR,
          today: "date | None" = None) -> int:
    """Adds the rows the table does not hold yet; returns how many. A finished
    year is one Parquet file, the current year's months are CSVs; months of a
    year that has since ended are folded into its Parquet file."""
    today = today or datetime.now(NY).date()
    held = load_frame(directory)
    have = set(zip(held["ticker"], held["day"]))
    new = [r for r in rows if (r["ticker"], r["day"]) not in have]
    new = list({(r["ticker"], r["day"]): r for r in new}.values())
    stale_months = [p for p in glob.glob(os.path.join(directory, "*-*.csv"))
                    if int(os.path.basename(p)[:4]) < today.year]
    if not new and not stale_months:
        return 0
    parts = [f for f in (held, pd.DataFrame(new, columns=COLUMNS)) if len(f)]
    frame = pd.concat(parts, ignore_index=True)
    frame["year"] = frame["day"].str[:4].astype(int)
    frame["month"] = frame["day"].str[:7]
    touched_years = {int(r["day"][:4]) for r in new} \
        | {int(os.path.basename(p)[:4]) for p in stale_months}
    touched_months = {r["day"][:7] for r in new if int(r["day"][:4]) == today.year}
    for year in sorted(y for y in touched_years if y < today.year):
        part = frame[frame["year"] == year].sort_values(["day", "ticker"])[COLUMNS]
        atomic.write_parquet(os.path.join(directory, f"{year}.parquet"), part)
    for month in sorted(touched_months):
        part = frame[frame["month"] == month].sort_values(["day", "ticker"])[COLUMNS]
        atomic.write_csv(os.path.join(directory, f"{month}.csv"), part)
    for path in stale_months:
        os.remove(path)
    return len(new)

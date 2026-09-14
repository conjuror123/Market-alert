"""Does Tiingo's IEX feed price the basket the same way Twelve Data does?

This is the question that decides whether the migration is safe, and neither
coverage nor speed answers it. IEX is a single exchange with a low single-digit
share of US equity volume; Twelve Data serves the consolidated tape. Two feeds
can both be complete and still disagree on where a thin ETF closed at 14:00,
because they saw different prints. A disagreement of a few basis points is
noise to a human and a fabricated event to a detector whose hourly sigma is
tens of basis points - which is the "fires without meaning" failure, arriving
through the data rather than through the maths.

So: fetch the same hours from Tiingo, fold them to the hourly grid with the
same code the pipeline uses, join to the bars already stored from Twelve Data,
and report how far apart they are in basis points.

Run this in Actions, where both the Tiingo key and the checked-out store are.
Read-only.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from price_monitor.models import Candle
from tremor import bars

BASE = "https://api.tiingo.com"
TIMEOUT = 30
BARS_DIR = "data/tremor/bars"

# Liquid names first, then the thin ones where IEX's small share bites hardest.
LIQUID = ["SPY", "QQQ", "IWM", "XLK", "XLF", "TLT", "HYG", "GLD"]
THIN = ["CPER", "SOYB", "CORN", "UGA", "PPLT", "BKLN", "UNG", "PALL"]

_key = ""


def fetch_hourly(ticker: str, start: str) -> pd.DataFrame:
    """Tiingo 30-minute bars, folded to the same hourly grid as the store."""
    r = requests.get(
        f"{BASE}/iex/{ticker}/prices",
        params={"startDate": start, "resampleFreq": "30min",
                "columns": "open,high,low,close,volume"},
        headers={"Content-Type": "application/json",
                 "Authorization": f"Token {_key}"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        return bars.empty_frame()
    candles = []
    for row in rows:
        stamp = pd.Timestamp(row["date"])
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        open_time = int(stamp.timestamp())
        candles.append(Candle(
            open_time=open_time, open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row.get("volume") or 0.0), close_time=open_time + 1800))
    return bars.to_hourly(bars.candles_to_frame(candles))


def main() -> int:
    global _key
    _key = (os.environ.get("TIINGO_API_KEY") or "").strip()
    if not _key:
        print("TIINGO_API_KEY is not set.")
        return 1

    start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    print(f"Comparing Tiingo IEX against stored Twelve Data bars, from {start}.")
    print("close_bps / open_bps: median and worst absolute difference, in basis")
    print("points. vol_share: IEX volume as a fraction of the stored volume.\n")

    header = (f"{'ticker':<8}{'hours':>6}{'med_bps':>9}{'p90_bps':>9}"
              f"{'max_bps':>9}{'max_hour':>18}{'vol_share':>11}")
    print(header)
    print("-" * len(header))

    summary = []
    for group, tickers in (("LIQUID", LIQUID), ("THIN", THIN)):
        print(f"  -- {group} --")
        for ticker in tickers:
            try:
                fresh = fetch_hourly(ticker, start)
            except Exception as exc:
                print(f"{ticker:<8} fetch failed: {str(exc)[:60]}")
                continue
            stored = bars.load(bars.store_path(BARS_DIR, f"twelvedata_{ticker}"))
            if fresh.empty or stored.empty:
                print(f"{ticker:<8} no overlap (fresh={len(fresh)}, "
                      f"stored={len(stored)})")
                continue
            joined = fresh.merge(stored, on="hour_utc", suffixes=("_tg", "_td"))
            # Only compare hours the store considers closed; the newest stored
            # bar may still have been open when it was written.
            joined = joined[joined["hour_utc"] < int(stored["hour_utc"].max())]
            if joined.empty:
                print(f"{ticker:<8} no overlapping closed hours")
                continue
            bps = ((joined["close_tg"] - joined["close_td"]).abs()
                   / joined["close_td"] * 1e4)
            worst = int(joined.loc[bps.idxmax(), "hour_utc"])
            vol_td = joined["volume_td"].sum()
            share = (joined["volume_tg"].sum() / vol_td) if vol_td else float("nan")
            print(f"{ticker:<8}{len(joined):>6}{bps.median():>9.2f}"
                  f"{bps.quantile(0.9):>9.2f}{bps.max():>9.2f}"
                  f"{datetime.fromtimestamp(worst, timezone.utc):%Y-%m-%d %H:%M}"
                  f"{share:>11.1%}")
            summary.append((group, ticker, bps.median(), bps.max(), share))
            time.sleep(0.05)

    print()
    print("=" * 78)
    for group in ("LIQUID", "THIN"):
        rows = [s for s in summary if s[0] == group]
        if not rows:
            continue
        med = sorted(r[2] for r in rows)[len(rows) // 2]
        worst = max(r[3] for r in rows)
        shares = [r[4] for r in rows if r[4] == r[4]]
        print(f"{group:<7} median-of-medians {med:6.2f} bps   worst single hour "
              f"{worst:7.2f} bps   IEX volume share "
              f"{sum(shares) / len(shares):5.1%}" if shares else "")
    print()
    print("For scale: an hourly move of 1 sigma is roughly 20-40 bps for these")
    print("ETFs, so a feed disagreement of 1 bps is ~0.03 sigma and invisible;")
    print("10 bps is ~0.3 sigma and would start to move the ladder.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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

# Every ETF in the basket. The point of the exercise is to decide, per symbol,
# which feed it should come from, so every symbol has to be measured - a rule
# inferred from eight of them would be a guess about the other thirty-six.
ETFS = [
    "XLK", "XLF", "XLY", "XLP", "XLE", "XLV", "XLI", "XLB", "XLU", "XLRE",
    "XLC", "SPY", "QQQ", "IWM", "EFA", "EEM", "SHY", "IEI", "IEF", "TLH",
    "TLT", "TIP", "MBB", "LQD", "HYG", "JNK", "EMB", "BKLN", "PFF", "USO",
    "BNO", "UGA", "UNG", "GLD", "SLV", "PPLT", "PALL", "DBB", "CPER", "DBA",
    "CORN", "WEAT", "SOYB", "DBC",
]

# The line between "same feed" and "a different feed that will invent events".
# An hourly sigma for these ETFs is 20-40 bps, so 2 bps of median disagreement
# is under a tenth of a sigma and vanishes into the noise the detector already
# tolerates; past that, the feed starts contributing its own moves.
SAFE_MEDIAN_BPS = 2.0
SAFE_P90_BPS = 5.0

_key = ""


class RateLimited(RuntimeError):
    """Tiingo's 50-requests-per-hour bucket is empty; stop and keep what we have."""


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
    if r.status_code == 429:
        raise RateLimited(f"{ticker}: hourly request budget spent")
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

    # 44 tickers is most of the 50-per-hour bucket, and anything else spent in
    # the same hour pushes the sweep over it. TIINGO_TICKERS runs a named
    # subset so the basket can be measured across two runs instead of one.
    wanted = [t.strip().upper() for t in
              (os.environ.get("TIINGO_TICKERS") or "").split(",") if t.strip()]
    tickers = wanted or ETFS
    unknown = [t for t in tickers if t not in ETFS]
    if unknown:
        print(f"not in the basket, ignoring: {unknown}")
        tickers = [t for t in tickers if t in ETFS]

    start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    print(f"Comparing Tiingo IEX against stored Twelve Data bars, from {start}.")
    print("bps columns: absolute difference between the two feeds' hourly close,")
    print("in basis points. vol_share: IEX volume as a fraction of the stored")
    print("(consolidated) volume for the same hours.\n")

    header = (f"{'ticker':<8}{'hours':>6}{'med_bps':>9}{'p90_bps':>9}"
              f"{'max_bps':>9}{'vol_share':>11}   verdict")
    print(header)
    print("-" * len(header))

    summary = []
    stopped = False
    for ticker in tickers:
        try:
            fresh = fetch_hourly(ticker, start)
        except RateLimited as exc:
            print(f"\n  stopped early: {exc}")
            stopped = True
            break
        except Exception as exc:
            print(f"{ticker:<8} fetch failed: {str(exc)[:60]}")
            continue
        stored = bars.load(bars.store_path(BARS_DIR, f"twelvedata_{ticker}"))
        if fresh.empty or stored.empty:
            print(f"{ticker:<8} no overlap (fresh={len(fresh)}, stored={len(stored)})")
            continue
        joined = fresh.merge(stored, on="hour_utc", suffixes=("_tg", "_td"))
        joined = joined[joined["hour_utc"] < int(stored["hour_utc"].max())]
        if joined.empty:
            print(f"{ticker:<8} no overlapping closed hours")
            continue
        bps = ((joined["close_tg"] - joined["close_td"]).abs()
               / joined["close_td"] * 1e4)
        med, p90, mx = bps.median(), bps.quantile(0.9), bps.max()
        vol_td = joined["volume_td"].sum()
        share = (joined["volume_tg"].sum() / vol_td) if vol_td else float("nan")
        safe = med <= SAFE_MEDIAN_BPS and p90 <= SAFE_P90_BPS
        print(f"{ticker:<8}{len(joined):>6}{med:>9.2f}{p90:>9.2f}{mx:>9.2f}"
              f"{share:>11.1%}   {'TIINGO' if safe else 'keep on TD'}")
        summary.append((ticker, med, p90, mx, share, safe))
        time.sleep(0.05)

    print()
    print("=" * 78)
    safe = [r[0] for r in summary if r[5]]
    unsafe = [r[0] for r in summary if not r[5]]
    print(f"Measured {len(summary)} of {len(tickers)} requested ETFs"
          + ("  (stopped early on the rate limit)" if stopped else ""))
    print(f"\nAGREES WITH TWELVE DATA ({len(safe)}) - safe to move to Tiingo:")
    print("  " + " ".join(safe))
    print(f"\nDISAGREES ({len(unsafe)}) - keep on Twelve Data:")
    print("  " + " ".join(unsafe))
    if summary:
        meds = sorted(r[1] for r in summary)
        shares = [r[4] for r in summary if r[4] == r[4]]
        print(f"\nmedian disagreement across all measured: "
              f"{meds[len(meds) // 2]:.2f} bps")
        print(f"IEX volume share, average: {sum(shares) / len(shares):.1%}")
    print()
    print(f"Threshold: median <= {SAFE_MEDIAN_BPS} bps and p90 <= {SAFE_P90_BPS} bps.")
    print("An hourly move of 1 sigma is roughly 20-40 bps for these ETFs, so 2 bps")
    print("is under a tenth of a sigma - inside the noise the detector already")
    print("tolerates. Past that the feed starts contributing moves of its own.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

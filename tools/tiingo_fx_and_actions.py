"""Two things Tiingo may fix outright, both currently open in docs/decisions.md.

ONE - the FX pairs. They are 41% of all requests the monitor makes (24 a day
each, never skipped, because FX has no session table to skip by) while being
8 of 52 symbols. Moving them is the single largest saving available, so the
question is whether Tiingo's FX agrees with what is stored.

TWO - the dividend steps on the five SPDRs that split 2:1 on 2025-12-05.
decisions.md records that Twelve Data's adjust=all series divides the NOMINAL
pre-split dividend by the SPLIT-ADJUSTED price, so every dividend step before
that date reads exactly twice its true size; that error is 504-2705 bps
against verify_alignment's 25 bps tolerance, so the HF Data deepening import
is refused and those five carry six years of history instead of twenty-four.
The recorded fix is to fetch a third Twelve Data series at adjust=none and
take a ratio. Tiingo publishes divCash and splitFactor as declared values, so
if they are right there is nothing to infer and no third series to pay for.

Read-only.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from price_monitor.models import Candle
from tremor import bars, corporate_actions

BASE = "https://api.tiingo.com"
TIMEOUT = 30
BARS_DIR = "data/tremor/bars"

# config spelling -> Tiingo spelling -> store file stem
PAIRS = [("EUR/USD", "eurusd", "twelvedata_EUR_USD"),
         ("USD/JPY", "usdjpy", "twelvedata_USD_JPY"),
         ("GBP/USD", "gbpusd", "twelvedata_GBP_USD"),
         ("AUD/USD", "audusd", "twelvedata_AUD_USD"),
         ("NZD/USD", "nzdusd", "twelvedata_NZD_USD"),
         ("USD/CHF", "usdchf", "twelvedata_USD_CHF"),
         ("USD/CAD", "usdcad", "twelvedata_USD_CAD"),
         ("USD/CNH", "usdcnh", "twelvedata_USD_CNH")]

SPLIT_FIVE = ["XLK", "XLY", "XLE", "XLU", "XLB"]

_key = ""


def _get(path: str, **params):
    r = requests.get(f"{BASE}{path}", params=params,
                     headers={"Content-Type": "application/json",
                              "Authorization": f"Token {_key}"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def main() -> int:
    global _key
    _key = (os.environ.get("TIINGO_API_KEY") or "").strip()
    if not _key:
        print("TIINGO_API_KEY is not set.")
        return 1

    start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    print("=" * 78)
    print("1. FX  - does Tiingo price the pairs the way the store does?")
    print("=" * 78)
    hdr = (f"{'pair':<10}{'hours':>6}{'med_bps':>9}{'p90_bps':>9}"
           f"{'max_bps':>9}   verdict")
    print(hdr)
    print("-" * len(hdr))
    for label, tg, stem in PAIRS:
        rows = _get(f"/tiingo/fx/{tg}/prices", startDate=start,
                    resampleFreq="1hour")
        if not isinstance(rows, list) or not rows:
            print(f"{label:<10} no data")
            continue
        candles = []
        for row in rows:
            stamp = pd.Timestamp(row["date"])
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            t = int(stamp.timestamp())
            candles.append(Candle(open_time=t, open=float(row["open"]),
                                  high=float(row["high"]), low=float(row["low"]),
                                  close=float(row["close"]), volume=0.0,
                                  close_time=t + 3600))
        fresh = bars.to_hourly(bars.candles_to_frame(candles))
        stored = bars.load(bars.store_path(BARS_DIR, stem))
        if stored.empty:
            print(f"{label:<10} nothing stored")
            continue
        j = fresh.merge(stored, on="hour_utc", suffixes=("_tg", "_td"))
        j = j[j["hour_utc"] < int(stored["hour_utc"].max())]
        if j.empty:
            print(f"{label:<10} no overlapping closed hours")
            continue
        bps = ((j["close_tg"] - j["close_td"]).abs() / j["close_td"] * 1e4)
        ok = bps.median() <= 2.0 and bps.quantile(0.9) <= 5.0
        print(f"{label:<10}{len(j):>6}{bps.median():>9.2f}"
              f"{bps.quantile(0.9):>9.2f}{bps.max():>9.2f}   "
              f"{'TIINGO' if ok else 'keep on TD'}")

    print()
    print("=" * 78)
    print("2. DIVIDENDS AND SPLITS  - declared, instead of inferred from a ratio")
    print("=" * 78)
    stored_steps = corporate_actions.load_steps()
    print("The store's step is derived from a ratio of two adjusted series and,")
    print("per decisions.md, reads twice its true size on these five before the")
    print("2025-12-05 split. Tiingo states the cash amount, so the true step is")
    print("divCash / previous close - no inference.\n")
    hdr = (f"{'ticker':<8}{'ex-date':<12}{'divCash':>9}{'prev_close':>11}"
           f"{'true_step':>11}{'stored_step':>13}{'ratio':>8}")
    print(hdr)
    print("-" * len(hdr))
    for ticker in SPLIT_FIVE:
        rows = _get(f"/tiingo/daily/{ticker}/prices", startDate="2024-06-01",
                    endDate="2025-12-31", resampleFreq="daily")
        if not isinstance(rows, list):
            continue
        by_date = {str(r["date"])[:10]: r for r in rows}
        dates = sorted(by_date)
        steps = dict(stored_steps.get(ticker, []))
        shown = 0
        for i, d in enumerate(dates):
            row = by_date[d]
            div = float(row.get("divCash") or 0.0)
            if div <= 0 or i == 0:
                continue
            prev_close = float(by_date[dates[i - 1]]["close"] or 0.0)
            if not prev_close:
                continue
            true_step = div / prev_close
            key = datetime.strptime(d, "%Y-%m-%d").date()
            stored = steps.get(key)
            ratio = (stored / true_step) if (stored and true_step) else float("nan")
            print(f"{ticker:<8}{d:<12}{div:>9.4f}{prev_close:>11.2f}"
                  f"{true_step:>11.6f}"
                  + (f"{stored:>13.6f}{ratio:>8.2f}x" if stored
                     else f"{'-':>13}{'-':>8}"))
            shown += 1
            if shown >= 3:
                break
    print()
    print("A ratio near 2.00x is decisions.md's bug, measured against a declared")
    print("amount rather than against a second adjusted series.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

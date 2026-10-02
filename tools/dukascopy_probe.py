"""Whether Dukascopy's archive can reach under the gap instruments' history:
USD/BRL, USD/KRW and USD/INR (Twelve Data's from 2019-2020) and coffee, cocoa
and cotton (Yahoo's from 2024-05, cotton's from 2026-06).

Per symbol: the first year whose June the archive holds, then its first month;
and three months where the store already has bars, folded to hours and held
against them - the scale (the archive's integers are in units of a point that
differs by instrument), return correlation, the median level gap, and how many
of the stored hours it has. The same two gates the deepening applies
(tremor.backfill.verify_alignment) decide whether a splice could pass.

Read-only; prints. Dispatched on Actions (alpaca-probe.yml, only=dukascopy):
the archive throttles a sandbox's address within a few requests.
"""
from __future__ import annotations

import math
import sys
import time
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import requests

from price_monitor import dukascopy
from tremor import bars
from tremor.basket import load_basket

CANDIDATES = {
    "USD/BRL": "USDBRL", "USD/KRW": "USDKRW", "USD/INR": "USDINR",
    "KC=F": "COFFEECMDUSX", "CC=F": "COCOACMDUSD", "CT=F": "COTTONCMDUSX",
}
# Three months the store holds for each, the oldest it is likely to be asked to
# meet: the store's first months for the pairs, a year in for the futures.
OVERLAP = {
    "USD/BRL": [(2019, 10), (2019, 11), (2019, 12)],
    "USD/KRW": [(2020, 2), (2020, 3), (2020, 4)],
    "USD/INR": [(2019, 12), (2020, 1), (2020, 2)],
    "KC=F": [(2024, 7), (2024, 8), (2024, 9)],
    "CC=F": [(2024, 7), (2024, 8), (2024, 9)],
    "CT=F": [(2026, 7), (2026, 8), (2026, 9)],
}
PAUSE = 2.0


def month(symbol: str, year: int, mon: int, side: str = "BID"):
    try:
        rows = dukascopy.fetch_month(symbol, year, mon, side)
    except Exception as exc:                      # noqa: BLE001 - a probe reports
        print(f"   {symbol} {year}-{mon:02d} {side}: {exc}", flush=True)
        rows = "error"
    time.sleep(PAUSE)
    return rows


def traded(rows) -> list:
    return [r for r in rows if r[5] > 0] if isinstance(rows, list) else []


def first_month(symbol: str) -> "tuple[int, int] | None":
    first_year = None
    for year in range(2003, 2027):
        if traded(month(symbol, year, 6)):
            first_year = year
            break
    if first_year is None:
        return None
    for mon in range(1, 7):
        if traded(month(symbol, first_year, mon)):
            return first_year, mon
    return first_year, 6


def overlap(ticker: str, symbol: str, stored: pd.DataFrame) -> None:
    rows = []
    for year, mon in OVERLAP[ticker]:
        bid = {r[0]: r for r in traded(month(symbol, year, mon, "BID"))}
        ask = {r[0]: r for r in traded(month(symbol, year, mon, "ASK"))}
        for t in sorted(bid.keys() & ask.keys()):
            rows.append((t, (bid[t][4] + ask[t][4]) / 2, bid[t][5]))
    if not rows:
        print(f"   {ticker}: no traded hours in the overlap months")
        return
    new = pd.DataFrame(rows, columns=["hour_utc", "raw", "ticks"])
    joined = new.merge(stored[["hour_utc", "close"]], on="hour_utc")
    lo, hi = new["hour_utc"].min(), new["hour_utc"].max()
    held = stored[(stored["hour_utc"] >= lo) & (stored["hour_utc"] <= hi)]
    if len(joined) < 20:
        print(f"   {ticker}: {len(new)} archive hours, {len(joined)} shared with the store")
        return
    ratio = float(np.median(joined["close"] / joined["raw"]))
    point = 10.0 ** round(math.log10(ratio))
    price = joined["raw"] * point
    gap_bp = float(np.median(np.abs(price - joined["close"]) / joined["close"]) * 1e4)
    p90_bp = float(np.quantile(np.abs(price - joined["close"]) / joined["close"], 0.9) * 1e4)
    a, b = np.diff(np.log(price.to_numpy())), np.diff(np.log(joined["close"].to_numpy()))
    corr = float(np.corrcoef(a, b)[0, 1])
    hours = pd.to_datetime(new["hour_utc"], unit="s", utc=True).dt.hour
    stored_hours = pd.to_datetime(held["hour_utc"], unit="s", utc=True).dt.hour
    print(f"   {ticker}: point {point:g} (ratio {ratio:.6g}); archive {len(new)} h, "
          f"store {len(held)} h, shared {len(joined)}; median gap {gap_bp:.1f} bp, "
          f"p90 {p90_bp:.1f}; return corr {corr:.4f}")
    print(f"     archive hours of day (UTC): {sorted(hours.unique().tolist())}")
    print(f"     store hours of day (UTC):   {sorted(stored_hours.unique().tolist())}")
    worst = np.argsort(np.abs(a - b))[::-1][:3]
    print("     worst hours (archive bp, store bp): " + ", ".join(
        f"{datetime.fromtimestamp(int(joined['hour_utc'].iloc[i + 1]), timezone.utc):%Y-%m-%d %H:%M}"
        f" {a[i] * 1e4:+.0f}/{b[i] * 1e4:+.0f}" for i in worst))


def main() -> int:
    by_ticker = {a.ticker: a for a in load_basket().instruments}
    only = [t for t in (sys.argv[1].split(",") if len(sys.argv) > 1 else CANDIDATES)]
    for ticker in only:
        symbol = CANDIDATES[ticker]
        asset = by_ticker[ticker]
        stored = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
        start = pd.to_datetime(stored["hour_utc"].min(), unit="s", utc=True)
        print(f"{ticker} ({symbol}); store from {start:%Y-%m-%d}", flush=True)
        first = first_month(symbol)
        print(f"   first month held: {first}", flush=True)
        if first is not None:
            overlap(ticker, symbol, stored)
    return 0


if __name__ == "__main__":
    sys.exit(main())

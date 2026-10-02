"""One-off: every coin's hourly history from Binance, its USDT pair from listing.

Two pairs are joined to the pair they were before (price_monitor/binance.py):
POL/USDT continues MATICUSDT, which stopped 2024-09-10 02:00 three days before
POLUSDT opened (MATIC became POL one for one), and BCH/USDT continues
BCHABCUSDT, the name Binance gave Bitcoin Cash from the 2018-11 fork until
2019-11-28. The hours between are holes. A join is refused if the price across
it moved more than JOIN_MAX - a renaming moves nothing by itself, so a step
past that would be the two names not being the same coin.

Writes data/tremor/bars/binance_*. Run from the repository root:
    python -m tools.binance_history
"""
from __future__ import annotations

import math
import shutil
import sys
from datetime import datetime, timezone

import requests

from price_monitor import binance
from tremor import bars
from tremor.basket import load_basket

START = datetime(2017, 1, 1, tzinfo=timezone.utc)
BEFORE = {"POL/USDT": "MATICUSDT", "BCH/USDT": "BCHABCUSDT"}
JOIN_MAX = 0.15


def main() -> int:
    session = requests.Session()
    coins = [a for a in load_basket().instruments if a.source == "binance"]
    for asset in coins:
        candles = binance.fetch_history(binance.symbol_for(asset.ticker), START,
                                        session=session)
        if asset.ticker in BEFORE:
            older = binance.fetch_history(BEFORE[asset.ticker], START, session=session)
            older = [c for c in older if c.open_time < candles[0].open_time]
            step = math.log(candles[0].open / older[-1].close)
            print(f"{asset.ticker}: {BEFORE[asset.ticker]} to "
                  f"{datetime.fromtimestamp(older[-1].open_time, timezone.utc):%Y-%m-%d %H:%M}, "
                  f"then {binance.symbol_for(asset.ticker)}; {100 * step:+.1f}% across the join")
            if abs(step) > JOIN_MAX:
                print(f"   refused: more than {JOIN_MAX:.0%}")
                return 1
            candles = older + candles
        path = bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem)
        shutil.rmtree(path, ignore_errors=True)
        bars.write(path, bars.to_hourly(bars.candles_to_frame(candles)))
        stored = bars.load(path)
        first = datetime.fromtimestamp(int(stored["hour_utc"].min()), timezone.utc)
        print(f"{asset.ticker}: {len(stored)} hours from {first:%Y-%m-%d}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Dump one year of a Dukascopy symbol's hourly candles, bid and ask, to CSV.

For research (coffee and cotton under the store's history): the archive
throttles one address to a request every half minute or so, so a year is one
Actions job and the years run side by side (alpaca-probe.yml,
only=dukascopy-dump). Prices are as the archive's integers times 1e-5; the
caller finds the scale against the store.

    python -m tools.dukascopy_dump COTTONCMDUSX 2019 out/
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone

from price_monitor import dukascopy


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dump one year of a Dukascopy symbol's hourly bid and ask to CSV.")
    parser.add_argument("symbol", help="Dukascopy's name, e.g. COTTONCMDUSX")
    parser.add_argument("year", type=int)
    parser.add_argument("out", help="the folder to write SYMBOL-YEAR.csv into")
    args = parser.parse_args(argv)
    symbol, year, out = args.symbol, args.year, args.out
    os.makedirs(out, exist_ok=True)
    now = datetime.now(timezone.utc)
    path = os.path.join(out, f"{symbol}-{year}.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["hour_utc", "bid_o", "bid_h", "bid_l", "bid_c", "bid_v",
                    "ask_o", "ask_h", "ask_l", "ask_c", "ask_v"])
        for month in range(1, 13):
            if (year, month) > (now.year, now.month):
                break
            sides = {}
            for side in ("BID", "ASK"):
                rows = dukascopy.fetch_month(symbol, year, month, side)
                sides[side] = {r[0]: r for r in rows or []}
            n = 0
            for t in sorted(sides["BID"].keys() & sides["ASK"].keys()):
                b, a = sides["BID"][t], sides["ASK"][t]
                if b[5] <= 0 and a[5] <= 0:
                    continue
                w.writerow([t, *b[1:], *a[1:]])
                n += 1
            print(f"{symbol} {year}-{month:02d}: {n} traded hours", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""One-off: fill XRP's history from Bitstamp where Coinbase has none - its
905-day suspension (2021-01-19 to 2023-07-13) and the years before its listing
(2019-02-26).

THE GATES. Bitstamp is held to the store wherever both have the hour, with the
splice's two tests (tremor.backfill.verify_alignment). Measured 2026-10-02 over
18,857 shared hours: return correlation 0.989, median level gap 7.5 bp. The
filled years have no Coinbase to meet, so they were refereed by Binance's
public archive (XRP/USDT, data.binance.vision), a market of its own: over the
suspension 21,689 shared hours, return correlation 0.9945, every month at 0.989
or better, median level gap 5.7 bp (the dollar against USDT included), three
hours whose returns differ by more than 3%; over 2018-05 to 2019-02,
correlation 0.975 (a 35 bp level gap, which is USDT off its peg in 2018).

FROM MARCH 2017, the first month Bitstamp traded XRP in over 90% of hours
(January 33%, February 68%): a market trading in fits is stale prices and then
catch-ups, which read as moves.

Only hours the store does not hold are written; Coinbase's own bars stay.
Run from the repository root:
    python -m tools.bitstamp_fill
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import requests

from price_monitor import bitstamp
from tremor import bars
from tremor.backfill import verify_alignment
from tremor.basket import load_basket

START = datetime(2017, 3, 1, tzinfo=timezone.utc)
# Past Coinbase's reopening, for the overlap the gate needs on that side too.
OVERLAP_DAYS = 93


def main() -> int:
    asset = next(a for a in load_basket().instruments if a.ticker == "XRP-USD")
    path = bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem)
    stored = bars.load(path)
    reopened = datetime(2023, 7, 13, tzinfo=timezone.utc)
    end = reopened + timedelta(days=OVERLAP_DAYS)
    candles = bitstamp.fetch_history("xrpusd", START, end, requests.Session())
    frame = bars.to_hourly(bars.candles_to_frame(candles))
    check = verify_alignment(frame, stored)
    print(f"Bitstamp XRP/USD: {len(frame)} hours; against the store: {check['hours']} "
          f"shared, correlation {check['correlation']:.4f}, median gap "
          f"{check['median_bp']:.1f} bp")
    if not check["ok"]:
        print(f"refused: {check['why']}")
        return 1
    new = frame[~frame["hour_utc"].isin(stored["hour_utc"])]
    added = bars.merge(path, new)
    print(f"added {added} hours; the store now holds {len(bars.load(path))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Whether Sina Finance's US fund bars can be a second consolidated live feed:
its hourly closes for every fund now on Yahoo held against Alpaca's
consolidated tape (SIP) over the last 28 days, with the line every feed is held
to (tools/fund_verdict.py: median <= 2 bp, p90 <= 5, at most 2% of the tape's
hours missing), and its volume against the tape's - a feed of one exchange
reports a fraction.

Sina's US bars (US_MinKService.getMinK, type 60) are labelled by their end in
New York time on the clock hour, the first by 10:00 for 09:30-10:00; each is
stamped at the hour its first minute falls in, which is the store's grid.

Read-only; prints. One Sina request a fund, a few Alpaca ones.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from tools import fund_verdict as fv
from tremor import bars
from tremor.basket import load_basket

URL = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20t=/US_MinKService.getMinK"
HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}


def sina_hours(symbol: str) -> pd.DataFrame:
    r = requests.get(URL, params={"symbol": symbol.lower(), "type": 60}, headers=HEADERS,
                     timeout=30)
    time.sleep(0.5)
    m = re.search(r"var t=\((.*)\);", r.text, re.S)
    rows = json.loads(m.group(1)) if m else None
    if not rows:
        return bars.empty_frame()
    out = []
    for row in rows:
        end = pd.Timestamp(row["d"], tz=fv.NY)
        start = (end - pd.Timedelta(hours=1)).floor("h")
        out.append((int(start.timestamp()), float(row["o"]), float(row["h"]), float(row["l"]),
                    float(row["c"]), float(row["v"]), 1))
    return pd.DataFrame(out, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)


def main() -> int:
    key = (os.environ.get("ALPACA_KEY_ID") or "").strip()
    secret = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    if not key or not secret:
        print("ALPACA_KEY_ID / ALPACA_SECRET_KEY are not set")
        return 1
    fv._alpaca_headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    basket = load_basket()
    funds = [a.ticker for a in basket.instruments
             if a.session_template == "us_equity" and a.fetched_from == "yahoo"]
    controls = ["SPY", "TLT", "GLD", "USO"]
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=20)
    start = end - timedelta(days=fv.DAYS)
    stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    tape = {s: fv._regular(bars.candles_to_frame(c)) for s, c in
            fv.alpaca_bars(funds + controls, "30Min", stamp(start), stamp(end), "sip").items()}
    lo = int(start.timestamp())
    passed, rows = [], []
    for s in controls + funds:
        feed = sina_hours(s)
        feed = feed[feed["hour_utc"] >= lo]
        t = tape.get(s, bars.empty_frame())
        t = t[t["hour_utc"] >= lo]
        m = fv.measure(feed, t)
        vol = (feed.merge(t, on="hour_utc", suffixes=("_f", "_t")))
        share = vol["volume_f"].sum() / vol["volume_t"].sum() if len(vol) else float("nan")
        ok = fv.passes(m)
        if ok and s in funds:
            passed.append(s)
        rows.append(f"   {s:5s} {fv._cell(m)}   volume {share:5.0%} of the tape"
                    + ("" if s in funds else "   (control)"))
    print(f"Sina US hourly bars against the consolidated tape, {fv.DAYS} days to "
          f"{end:%Y-%m-%d %H:%M} UTC (median/p90 bp, missing, x = fails)")
    print("\n".join(rows))
    print(f"\npass: {len(passed)} of {len(funds)} Yahoo funds: {' '.join(passed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

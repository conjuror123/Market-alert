"""Whether Sina Finance's US fund bars can be a second consolidated live feed:
its hourly closes for every fund now on Yahoo held against Alpaca's
consolidated tape (SIP) over the last 28 days, with the line every feed is held
to (tools/fund_verdict.py: median <= 2 bp, p90 <= 5, at most 2% of the tape's
hours missing), and its volume against the tape's - a feed of one exchange
reports a fraction.

Sina's US bars (US_MinKService.getMinK) are labelled by their end in New York
time. Its hourly ones run 09:30-10:30, 10:30-11:30 ... - a grid the store's
clock hours cannot be folded from, and the first run of this probe compared
them across half an hour (SPY 5 bp apart where the closes are identical). So
the half-hour bars (type 30, about 78 days) are taken and folded like the
tape's.

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
from jump import bars
from jump.basket import load_basket

URL = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20t=/US_MinKService.getMinK"
HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}


def sina_hours(symbol: str) -> pd.DataFrame:
    r = requests.get(URL, params={"symbol": symbol.lower(), "type": 30}, headers=HEADERS,
                     timeout=30)
    time.sleep(0.5)
    m = re.search(r"var t=\((.*)\);", r.text, re.S)
    rows = json.loads(m.group(1)) if m else None
    if not rows:
        return bars.empty_frame()
    out = []
    for row in rows:
        start = pd.Timestamp(row["d"], tz=fv.NY) - pd.Timedelta(minutes=30)
        out.append((int(start.timestamp()), float(row["o"]), float(row["h"]), float(row["l"]),
                    float(row["c"]), float(row["v"]), 1))
    return fv._regular(pd.DataFrame(out, columns=list(bars.SCHEMA)).astype(bars.SCHEMA))


def freshness() -> int:
    """During the session: the newest half-hour bar Sina serves for a liquid
    and a thin fund, and how long after its end it is being read. The hourly
    run reads at :05 and needs the bar that ended at :00."""
    from price_monitor import sina
    now = datetime.now(timezone.utc)
    print(f"Sina US freshness at {now:%H:%M:%S} UTC")
    for s in ("SPY", "SLQD", "CEMB", "GLTR"):
        candles = sina.fetch_us_bars(s, now=now + timedelta(hours=1))   # unended too
        if not candles:
            print(f"   {s:5s} no bars")
            continue
        last = candles[-1]
        end = datetime.fromtimestamp(last.close_time, timezone.utc)
        print(f"   {s:5s} newest bar ends {end:%H:%M} UTC ({(now - end).total_seconds() / 60:+.0f} "
              f"min ago), close {last.close}, volume {last.volume:.0f}")
    return 0


def main() -> int:
    if os.environ.get("SINA_PROBE_ONLY") == "fresh":
        return freshness()
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

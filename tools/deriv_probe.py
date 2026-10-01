"""What Deriv's public market-data websocket serves for what is still unsourced.

Deriv (developers.deriv.com) answers `active_symbols` and `ticks_history` on a
public app id, without an account. This asks, for the needed list - USD/BRL's
history, TUR, RWX, coffee, cocoa, cotton, live cattle, aluminium, nickel, tin -
which symbols exist, how far back hourly candles go, how fresh the newest one
is, and, on EUR/USD, whether its hourly closes agree with the stored bars.

Read-only; prints, writes nothing. A few dozen requests. Runs in Actions: the
sandbox's egress is refused by Deriv's Cloudflare front.
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import pandas as pd
from websocket import create_connection

from tremor import bars

URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
WANTED = ("brl", "coffee", "cocoa", "cotton", "cattle", "nickel", "tin", "alumin",
          "turkey", "rwx", "tur ")


def ask(ws, req: dict) -> dict:
    ws.send(json.dumps(req))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("msg_type") != "ping":
            return msg


def candles(ws, symbol: str, start: int, end, count: int = 5000) -> "list[dict] | str":
    msg = ask(ws, {"ticks_history": symbol, "style": "candles", "granularity": 3600,
                   "start": start, "end": end, "count": count, "adjust_start_time": 1})
    if msg.get("error"):
        return f"{msg['error'].get('code')}: {msg['error'].get('message')}"
    return msg.get("candles") or []


def _day(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M")


def main() -> int:
    ws = create_connection(URL, timeout=60)
    syms = ask(ws, {"active_symbols": "brief"}).get("active_symbols") or []
    print(f"SYMBOLS: {len(syms)}; by market {dict(Counter(s['market'] for s in syms))}")
    found = []
    for s in syms:
        text = f"{s['symbol']} {s['display_name']}".lower()
        if s["market"] == "commodities" or any(w in text for w in WANTED):
            found.append(s["symbol"])
            print(f"   {s['market']:12s} {s['submarket']:22s} {s['symbol']:14s} "
                  f"{s['display_name']}  open={s.get('exchange_is_open')}")
    print(f"   all forex: {' '.join(s['symbol'] for s in syms if s['market'] == 'forex')}")
    print(f"   all indices/stocks: {' '.join(s['symbol'] for s in syms if s['market'] in ('indices', 'stocks'))}\n")

    now = int(time.time())
    print("DEPTH AND NEWEST - hourly candles")
    for sym in found + ["frxEURUSD"]:
        cells = []
        for year in (2019, 2022, 2025):
            start = int(datetime(year, 3, 4, tzinfo=timezone.utc).timestamp())
            got = candles(ws, sym, start, start + 7 * 86400, 200)
            cells.append(f"{year}: {len(got) if isinstance(got, list) else got[:40]}")
        got = candles(ws, sym, now - 4 * 86400, "latest", 200)
        newest = (f"{_day(got[-1]['epoch'])} UTC ({(now - got[-1]['epoch']) / 60:.0f} min ago)"
                  if isinstance(got, list) and got else str(got)[:60])
        print(f"   {sym:14s} " + "  ".join(cells) + f"   newest {newest}")
        time.sleep(0.5)
    print()

    print("AGREEMENT - frxEURUSD hourly closes against the stored EUR/USD, 60 days")
    got = candles(ws, "frxEURUSD", now - 60 * 86400, now - 7200, 5000)
    if isinstance(got, list) and got:
        d = pd.DataFrame({"hour_utc": [int(c["epoch"]) for c in got],
                          "close": [float(c["close"]) for c in got]})
        stored = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, "twelvedata_EUR_USD"))
        j = d.merge(stored[["hour_utc", "close"]], on="hour_utc", suffixes=("_d", "_s"))
        bps = (j["close_d"] - j["close_s"]).abs() / j["close_s"] * 1e4
        print(f"   {len(j)} hours in common: median {bps.median():.2f} bps, "
              f"p90 {bps.quantile(0.9):.2f}")
        # Deriv may stamp a candle at its open or its close: try the shift too.
        d2 = d.assign(hour_utc=d["hour_utc"] - 3600)
        j2 = d2.merge(stored[["hour_utc", "close"]], on="hour_utc", suffixes=("_d", "_s"))
        b2 = (j2["close_d"] - j2["close_s"]).abs() / j2["close_s"] * 1e4
        print(f"   stamped one hour earlier: median {b2.median():.2f} bps, p90 {b2.quantile(0.9):.2f}")
    else:
        print(f"   {str(got)[:200]}")
    ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

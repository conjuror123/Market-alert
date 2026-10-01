"""What Financial Modeling Prep's key serves for what is still unsourced.

The needed list (docs/concerns-for-later.md) after every other source: TUR and
RWX (no live feed agrees with the consolidated tape), USD/BRL's history before
2025, and seven commodities whose ETNs no longer trade - coffee, cocoa, cotton,
live cattle, nickel, aluminium, tin. This asks FMP, on whatever plan the key
holds, the questions that decide each:

  plan       whether hourly and half-hour bars come back at all (they are a paid
             feature) and what an error says;
  catalog    which commodity and forex symbols it lists for those names;
  funds      TUR and RWX: 28 days of half-hour bars against Alpaca's
             consolidated tape, the line every feed is held to;
  depth      how far back hourly bars go for each found symbol;
  newest     how old the newest bar is.

Read-only; prints, writes nothing. About 40 requests of the free plan's 250 a
day.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from tools import fund_verdict as fv
from tremor import bars

BASE = "https://financialmodelingprep.com/stable"
WANTED = ("coffee", "cocoa", "cotton", "cattle", "nickel", "alumin", "tin")


def get(path: str, **params) -> "tuple[int, object]":
    params["apikey"] = os.environ["FMP_API_KEY"]
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=fv.TIMEOUT)
    except requests.RequestException as exc:
        return 0, f"request failed: {type(exc).__name__}"
    finally:
        time.sleep(0.4)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


def chart(symbol: str, interval: str, start: datetime, end: datetime):
    return get(f"/historical-chart/{interval}", symbol=symbol,
               **{"from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d")})


def frame(rows, tz: str = "America/New_York") -> pd.DataFrame:
    """FMP's intraday rows, stamped in New York local time at the bar's open, as
    the store's frame."""
    if not isinstance(rows, list) or not rows:
        return bars.empty_frame()
    d = pd.DataFrame(rows)
    local = pd.to_datetime(d["date"]).dt.tz_localize(tz, ambiguous="NaT",
                                                     nonexistent="NaT")
    keep = local.notna()
    d, local = d[keep], local[keep]
    out = pd.DataFrame({"hour_utc": local.dt.tz_convert("UTC").astype("int64") // 10**9,
                        "open": d["open"].astype(float), "high": d["high"].astype(float),
                        "low": d["low"].astype(float), "close": d["close"].astype(float),
                        "volume": d.get("volume", pd.Series(0.0, index=d.index))
                        .fillna(0.0).astype(float), "n_src": 1})
    return out.astype(bars.SCHEMA).sort_values("hour_utc").reset_index(drop=True)


def _span(rows) -> str:
    if not isinstance(rows, list) or not rows:
        return "no bars"
    dates = sorted(r["date"] for r in rows)
    return f"{len(rows)} bars, {dates[0]} .. {dates[-1]}"


def plan(now: datetime) -> None:
    print("PLAN - does an intraday chart come back on this key?")
    for interval in ("1hour", "30min"):
        code, body = chart("SPY", interval, now - timedelta(days=5), now)
        print(f"   SPY {interval}: HTTP {code} "
              f"{_span(body) if isinstance(body, list) else str(body)[:200]}")
    code, body = get("/historical-price-eod/light", symbol="SPY",
                     **{"from": (now - timedelta(days=10)).strftime("%Y-%m-%d")})
    print(f"   SPY daily: HTTP {code} "
          f"{len(body) if isinstance(body, list) else str(body)[:200]} rows\n")


def catalog() -> "list[tuple[str, str]]":
    print("CATALOG - commodities and pairs it lists for what is needed")
    found = []
    code, body = get("/commodities-list")
    if isinstance(body, list):
        print(f"   commodities-list: {len(body)} symbols")
        for r in body:
            text = f"{r.get('symbol')} {r.get('name')}".lower()
            if any(w in text for w in WANTED):
                found.append((r.get("symbol"), r.get("name")))
                print(f"      {r.get('symbol'):10s} {r.get('name')}  "
                      f"{r.get('exchange', '')} {r.get('currency', '')}")
    else:
        print(f"   commodities-list: HTTP {code} {str(body)[:200]}")
    code, body = get("/forex-list")
    if isinstance(body, list):
        brl = [r.get("symbol") for r in body if "BRL" in str(r.get("symbol"))]
        print(f"   forex-list: {len(body)} pairs; with BRL: {' '.join(brl[:10])}")
    else:
        print(f"   forex-list: HTTP {code} {str(body)[:200]}")
    print()
    return found


def funds(now: datetime) -> None:
    print(f"FUNDS - TUR and RWX, {fv.DAYS} days of half-hour bars against the tape")
    end = now - timedelta(minutes=20)
    start = end - timedelta(days=fv.DAYS)
    stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    symbols = ["TUR", "RWX", "SPY"]
    tape = {s: fv._regular(bars.candles_to_frame(c)) for s, c in
            fv.alpaca_bars(symbols, "30Min", stamp(start), stamp(end), "sip").items()}
    for s in symbols:
        code, body = chart(s, "30min", start, end)
        if not isinstance(body, list):
            print(f"   {s:4s} HTTP {code} {str(body)[:160]}")
            continue
        m = fv.measure(fv._regular(frame(body)), tape.get(s, bars.empty_frame()))
        print(f"   {s:4s} {fv._cell(m)}  ({_span(body)})")
    print()


def depth_and_newest(now: datetime, symbols: "list[str]") -> None:
    print("DEPTH AND NEWEST - hourly bars, a week at each date, and the newest")
    for s in symbols:
        cells = []
        for year in (2019, 2022, 2025):
            start = datetime(year, 3, 4, tzinfo=timezone.utc)
            code, body = chart(s, "1hour", start, start + timedelta(days=7))
            cells.append(f"{year}: {len(body) if isinstance(body, list) else f'HTTP {code}'}")
        code, body = chart(s, "1hour", now - timedelta(days=4), now)
        newest = max((r["date"] for r in body), default="-") if isinstance(body, list) else \
            f"HTTP {code} {str(body)[:80]}"
        print(f"   {s:10s} " + "  ".join(cells) + f"   newest: {newest}")
    print()


def main() -> int:
    if not (os.environ.get("FMP_API_KEY") or "").strip():
        print("FMP_API_KEY is not set")
        return 1
    key = (os.environ.get("ALPACA_KEY_ID") or "").strip()
    secret = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    fv._alpaca_headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    now = datetime.now(timezone.utc)
    plan(now)
    found = catalog()
    if key and secret:
        funds(now)
    depth_and_newest(now, ["USDBRL", "TUR", "RWX"] + [s for s, _ in found][:8])
    return 0


if __name__ == "__main__":
    sys.exit(main())

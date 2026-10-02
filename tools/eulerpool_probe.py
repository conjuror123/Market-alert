"""What Eulerpool's free plan serves, measured against the consolidated tape.

Eulerpool (docs/decisions.md, the fund verdict) is the one source whose
free licence might fit a public channel: non-commercial use with the line "Data
by Eulerpool". This asks it what tools/fund_verdict.py asked every other feed:

  shape      what one hourly and one half-hour pull returns, raw, and what the
             quota headers say;
  agreement  every held fund and every candidate, 28 days of half-hour bars
             folded to the store's hours, against Alpaca's consolidated tape -
             the same line: median <= 2 bps, p90 <= 5, at most 2% of the tape's
             hours missing. Timestamps are tried as the bar's open and as its
             close, and the one that agrees is reported;
  depth      how far back hourly bars go, on SPY and two thin funds;
  newest     the newest bar and how old it is (the run fires at :05 and needs
             the bar that ended at :00);
  FX, crypto whether the hourly endpoint answers a currency pair at all, and
             how Binance's hourly BTC compares with the stored Coinbase bars.

Read-only; prints, writes nothing. Runs in Actions, where the keys are
repository secrets. About 160 requests of the free plan's 100,000 a month.
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

BASE = "https://api.eulerpool.com/api/1"
_quota: dict[str, str] = {}


def get(path: str, **params) -> "tuple[int, object]":
    params["token"] = os.environ["EULERPOOL_API_KEY"]
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=fv.TIMEOUT)
    except requests.RequestException as exc:
        return 0, f"request failed: {type(exc).__name__}"
    finally:
        time.sleep(0.4)
    for k, v in r.headers.items():
        if "limit" in k.lower() or "quota" in k.lower() or "remaining" in k.lower():
            _quota[k] = v
    try:
        body = r.json()
    except ValueError:
        body = r.text[:300]
    return r.status_code, body


def ohlcv(symbol: str, resolution: str, start: datetime, end: datetime):
    return get(f"/charting/ohlcv/{symbol}", resolution=resolution,
               **{"from": int(start.timestamp()), "to": int(end.timestamp())})


def frame(body, shift: int = 0) -> pd.DataFrame:
    """The t/o/h/l/c/v arrays as the store's frame, stamped `shift` seconds
    earlier than given (0: t is the bar's open; the span: t is its close)."""
    if not isinstance(body, dict) or not body.get("t"):
        return bars.empty_frame()
    rows = pd.DataFrame({k: body.get(k) or [0.0] * len(body["t"])
                         for k in ("t", "o", "h", "l", "c", "v")})
    out = pd.DataFrame({"hour_utc": rows["t"].astype("int64") - shift,
                        "open": rows["o"].astype(float), "high": rows["h"].astype(float),
                        "low": rows["l"].astype(float), "close": rows["c"].astype(float),
                        "volume": rows["v"].astype(float), "n_src": 1})
    return out.astype(bars.SCHEMA)


def _stamp(t) -> str:
    return datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def shape(now: datetime) -> None:
    print("SHAPE - SPY, the last five days")
    for res in ("60", "30"):
        code, body = ohlcv("SPY", res, now - timedelta(days=5), now)
        if not isinstance(body, dict) or not body.get("t"):
            print(f"   resolution {res}: HTTP {code} {str(body)[:300]}")
            continue
        t = body["t"]
        keys = ", ".join(sorted(body))
        print(f"   resolution {res}: HTTP {code}, {len(t)} bars, keys {keys}")
        for i in list(range(min(3, len(t)))) + list(range(max(3, len(t) - 3), len(t))):
            print(f"      {_stamp(t[i])} UTC  o {body['o'][i]}  c {body['c'][i]}  "
                  f"v {(body.get('v') or [None] * len(t))[i]}")
    print(f"   quota headers: {_quota or 'none'}\n")


def agreement(now: datetime) -> None:
    basket = fv.load_basket()
    held = {a.ticker: a for a in basket.instruments if a.session_template == "us_equity"}
    candidates = {t: b for b, names in fv.CANDIDATES.items() for t in names.split()}
    symbols = list(held) + list(candidates)
    end = now - timedelta(minutes=20)
    start = end - timedelta(days=fv.DAYS)
    stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    tape = {s: fv._regular(bars.candles_to_frame(c)) for s, c in
            fv.alpaca_bars(symbols, "30Min", stamp(start), stamp(end), "sip").items()}

    print(f"AGREEMENT - {len(held)} held + {len(candidates)} candidates, "
          f"{fv.DAYS} days of half-hour bars folded to hours, against the tape")
    print(f"{'fund':<6}{'held on':<9}{'hours':>6}{'eulerpool med/p90 miss':>24}  as")
    good, bad, errors, stamped = [], [], {}, {"open": 0, "close": 0}
    for s in symbols:
        code, body = ohlcv(s, "30", start, end)
        t = tape.get(s, bars.empty_frame())
        if not isinstance(body, dict) or not body.get("t"):
            errors[s] = f"HTTP {code} {str(body)[:60]}"
            print(f"{s:<6}{(held[s].fetched_from if s in held else ''):<9}{len(t):>6}"
                  f"{'none':>24}  {errors[s]}")
            bad.append(s)
            continue
        best = None
        for name, shift in (("open", 0), ("close", 1800)):
            m = fv.measure(fv._regular(frame(body, shift)), t)
            if m and m.get("n") and (best is None or m["med"] < best[1]["med"]):
                best = (name, m)
        if best is None:
            print(f"{s:<6}{'':<9}{len(t):>6}{'no overlap':>24}")
            bad.append(s)
            continue
        stamped[best[0]] += 1
        (good if fv.passes(best[1]) else bad).append(s)
        print(f"{s:<6}{(held[s].fetched_from if s in held else ''):<9}{len(t):>6}"
              f"{fv._cell(best[1]):>24}  t = bar {best[0]}")
    cand_good = [s for s in good if s in candidates]
    print(f"\n   agrees on {len(good)} of {len(symbols)}: "
          f"{len([s for s in good if s in held])} held, {len(cand_good)} candidates")
    print(f"   candidates that agree: {' '.join(cand_good)}")
    print(f"   no bars: {' '.join(errors) or 'none'}")
    print(f"   timestamps read as: {stamped}")
    print(f"   quota headers: {_quota or 'none'}\n")


def depth() -> None:
    print("DEPTH - one week of hourly bars at each date")
    for s in ("SPY", "CPER", "TUR"):
        cells = []
        for year in (2025, 2022, 2020, 2016, 2012, 2008):
            start = datetime(year, 3, 2, tzinfo=timezone.utc)
            code, body = ohlcv(s, "60", start, start + timedelta(days=7))
            n = len(body["t"]) if isinstance(body, dict) and body.get("t") else 0
            cells.append(f"{year}: {n if n else f'HTTP {code}' if code != 200 else 0}")
        print(f"   {s:5s} " + "  ".join(cells))
    print()


def newest(now: datetime) -> None:
    print(f"NEWEST at {now:%H:%M} UTC (market {'open' if 13 <= now.hour < 20 else 'closed'})")
    for s in ("SPY", "XLK", "TLT", "EWZ", "SLQD"):
        for res in ("60", "30", "1"):
            code, body = ohlcv(s, res, now - timedelta(days=4), now)
            if isinstance(body, dict) and body.get("t"):
                t = max(body["t"])
                age = (now.timestamp() - t) / 60
                print(f"   {s:5s} res {res:>2}: newest stamped {_stamp(t)} UTC "
                      f"({age:.0f} min ago)")
            else:
                print(f"   {s:5s} res {res:>2}: HTTP {code} {str(body)[:80]}")
    print()


def fx_and_crypto(now: datetime) -> None:
    print("FX - does the hourly endpoint answer a currency pair?")
    for s in ("EURUSD", "EUR/USD", "USDBRL", "USD/BRL", "USDKRW", "USDINR"):
        code, body = ohlcv(s, "60", now - timedelta(days=5), now)
        n = len(body["t"]) if isinstance(body, dict) and body.get("t") else 0
        print(f"   {s:8s} HTTP {code}, {n} bars {'' if n else str(body)[:80]}")
    code, body = get("/market/fx/USD/BRL", range="1m")
    print(f"   /market/fx/USD/BRL: HTTP {code} {str(body)[:160]}")

    print("\nCRYPTO - Binance hourly BTCUSDT against the stored Coinbase BTC-USD")
    code, body = get("/crypto-extended/candles/BTCUSDT", interval="1h", limit=500)
    rows = body if isinstance(body, list) else (body.get("data") or body.get("candles")
                                                if isinstance(body, dict) else None)
    if not rows:
        print(f"   HTTP {code} {str(body)[:300]}")
        return
    print(f"   HTTP {code}, {len(rows)} candles; first: {str(rows[0])[:200]}")
    try:
        stored = pd.concat(pd.read_parquet(p) for p in
                           sorted(__import__('glob').glob(
                               "data/tremor/bars/coinbase_BTC-USD/*.parquet"))[-2:])
        first = rows[0]
        if isinstance(first, dict):
            tkey = next(k for k in first if k.lower() in ("t", "time", "timestamp",
                                                          "open_time", "opentime", "date"))
            ckey = next(k for k in first if k.lower() in ("c", "close"))
            got = pd.DataFrame({"t": [r[tkey] for r in rows],
                                "close": [float(r[ckey]) for r in rows]})
        else:
            got = pd.DataFrame({"t": [r[0] for r in rows], "close": [float(r[4]) for r in rows]})
        t = pd.to_datetime(got["t"], utc=True, unit="ms" if float(got["t"].iloc[0]) > 1e11
                           else "s") if not isinstance(got["t"].iloc[0], str) \
            else pd.to_datetime(got["t"], utc=True)
        got["hour_utc"] = (t.astype("int64") // 10**9).astype("int64")
        j = got.merge(stored[["hour_utc", "close"]], on="hour_utc", suffixes=("_b", "_c"))
        bps = (j["close_b"] - j["close_c"]).abs() / j["close_c"] * 1e4
        print(f"   {len(j)} hours in common: median {bps.median():.1f} bps, "
              f"p90 {bps.quantile(0.9):.1f} (USDT against USD, so not zero)")
    except Exception as exc:
        print(f"   could not compare: {type(exc).__name__}: {str(exc)[:120]}")
    print()


def identify(now: datetime) -> None:
    """Which identifiers and which endpoints know an ETF at all."""
    print("IDENTIFY - which endpoint knows which identifier")
    week = {"from": int((now - timedelta(days=5)).timestamp()), "to": int(now.timestamp())}
    for ident in ("AAPL", "US0378331005", "SPY", "US78462F1030", "SPY.US", "XLK",
                  "US81369Y8030", "CPER", "US9092221079"):
        for path, params in ((f"/charting/ohlcv/{ident}", {"resolution": "60", **week}),
                             (f"/equity/candles/{ident}", {"range": "1m"}),
                             (f"/etf/quotes/{ident}", {}),
                             (f"/equity/quotes/{ident}", {}),
                             (f"/market/quotes/intraday/{ident}", {})):
            code, body = get(path, **params)
            if isinstance(body, dict) and body.get("t"):
                what = f"{len(body['t'])} bars, newest {_stamp(max(body['t']))}"
            elif isinstance(body, list):
                what = f"list of {len(body)}: {str(body[:1])[:140]}"
            else:
                what = str(body)[:140]
            print(f"   {ident:13s} {path.split('/' + ident)[0]:28s} HTTP {code} {what}")
    for path in ("/etf/list", "/equity/search?q=SPY", "/search?q=SPY"):
        code, body = get(path)
        print(f"   {path:28s} HTTP {code} {str(body)[:200]}")
    print(f"   quota headers: {_quota or 'none'}\n")


def main() -> int:
    if not (os.environ.get("EULERPOOL_API_KEY") or "").strip():
        print("EULERPOOL_API_KEY is not set")
        return 1
    key = (os.environ.get("ALPACA_KEY_ID") or "").strip()
    secret = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    fv._alpaca_headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    now = datetime.now(timezone.utc)
    if os.environ.get("EULERPOOL_ONLY") == "eulerpool:identify":
        identify(now)
        return 0
    shape(now)
    newest(now)
    if os.environ.get("EULERPOOL_ONLY") == "eulerpool:newest":
        return 0
    fx_and_crypto(now)
    depth()
    if key and secret:
        agreement(now)
    else:
        print("No Alpaca keys: agreement skipped")
    print(f"quota headers at the end: {_quota or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

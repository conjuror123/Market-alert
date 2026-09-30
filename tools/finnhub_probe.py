"""What can Finnhub serve this project, on the key we have?

The widening needs a LIVE source for about 96 more US-listed funds and 9 FX
pairs (docs/concerns-for-later.md, item 6). This asks Finnhub the four questions
in the order that disqualifies cheapest:

  ACCESS     which endpoints this key may call at all: hourly stock candles,
             quotes, forex candles and rates. A 403 ends that line of inquiry.
  FRESHNESS  how old is the newest price it serves? The run fires at :05 and
             wants the bar that closed at :00.
  HEADROOM   the rate limit it reports in its own response headers.
  COVERAGE   which candidate tickers and pairs answer at all.

Read-only. Fetches and prints; writes nothing. Runs in Actions, where the key
is a repository secret.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import requests

BASE = "https://finnhub.io/api/v1"
TIMEOUT = 30
PAUSE = 1.1           # the free tier is documented at 60 calls a minute

FUNDS = """KRE XRT ITB IYT XME XBI XPH IHI VDC VHT VPU SMH SOXX IGV FDN CIBR SKYY
DIA RSP MDY EWJ EWG EWU EWQ EWC EWA EWL EWN EZU FXI EWZ EWW INDA EWY EWT EZA EPI
TUR VNQ IYR RWR SCHH REM VNQI RWX GOVT SCHO VGIT VGLT SPTL BWX VTIP SCHP STIP
VMBS SPMB LMBS JMBS VCIT VCSH IGIB SPIB USIG QLTA GIGB SLQD SHYG USHY ANGL SRLN
FALN EMLC VWOB PCY EBND LEMB EMHY CEMB DBO DBE UNL IAU SGOL SIVR GLTR JJC JJN JJU
LIT REMX SLX CANE JO NIB BAL COW""".split()
PAIRS = ["USD/SEK", "USD/NOK", "USD/MXN", "USD/ZAR", "USD/BRL", "USD/TRY",
         "USD/INR", "USD/KRW", "USD/PLN"]
# Two held today on a consolidated feed, to see whether its price agrees.
REFERENCE = ["SPY", "GLD"]


def _get(path: str, **params):
    params["token"] = os.environ["FINNHUB_API_KEY"]
    try:
        resp = requests.get(f"{BASE}{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return None, f"request failed: {type(exc).__name__}", {}
    limits = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")}
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:160]}", limits
    try:
        return resp.json(), "ok", limits
    except ValueError:
        return None, f"not JSON: {resp.text[:160]}", limits


def _age(epoch) -> str:
    if not epoch:
        return "no timestamp"
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    return f"{delta.total_seconds() / 60:.0f} min old"


def main() -> int:
    if not os.environ.get("FINNHUB_API_KEY"):
        print("FINNHUB_API_KEY is not set")
        return 1
    now = datetime.now(timezone.utc)
    print(f"Probing Finnhub at {now:%Y-%m-%d %H:%M} UTC\n")

    print("1. ACCESS AND FRESHNESS")
    end = int(now.timestamp())
    for label, path, params in [
        ("stock candles, 60 min", "/stock/candle",
         dict(symbol="SPY", resolution="60", **{"from": end - 7 * 86400, "to": end})),
        ("stock candles, 1 min", "/stock/candle",
         dict(symbol="SPY", resolution="1", **{"from": end - 86400, "to": end})),
        ("quote", "/quote", dict(symbol="SPY")),
        ("forex candles, 60 min", "/forex/candle",
         dict(symbol="OANDA:USD_MXN", resolution="60", **{"from": end - 7 * 86400, "to": end})),
        ("forex rates", "/forex/rates", dict(base="USD")),
        ("forex symbols (oanda)", "/forex/symbol", dict(exchange="oanda")),
        ("crypto candles, 60 min", "/crypto/candle",
         dict(symbol="BINANCE:BTCUSDT", resolution="60", **{"from": end - 86400, "to": end})),
    ]:
        data, status, limits = _get(path, **params)
        detail = ""
        if data is not None and isinstance(data, dict):
            if "t" in data and isinstance(data["t"], list) and data["t"]:
                detail = f"{len(data['t'])} bars, newest {_age(data['t'][-1])}"
            elif "t" in data:
                detail = f"price {data.get('c')}, {_age(data.get('t'))}"
            elif data.get("s") == "no_data":
                detail = "no_data"
            elif "quote" in data:
                quote = data["quote"]
                have = [p for p in PAIRS if p.split("/")[1] in quote]
                detail = f"{len(quote)} rates; EM/Nordic present: {len(have)}/{len(PAIRS)}"
            else:
                detail = f"keys: {sorted(data)[:8]}"
        elif isinstance(data, list):
            names = {s.get("displaySymbol", "") for s in data}
            have = [p for p in PAIRS if p in names]
            detail = f"{len(data)} symbols; wanted pairs listed: {len(have)}/{len(PAIRS)} {have}"
        print(f"   {label:24s} {status:10.60s} {detail}")
        if limits:
            print(f"   {'':24s} limits: {limits}")
        time.sleep(PAUSE)

    print("\n2. COVERAGE: a quote for every candidate fund")
    missing, stale, served = [], [], 0
    for ticker in FUNDS + REFERENCE:
        data, status, _ = _get("/quote", symbol=ticker)
        if data is None:
            missing.append(f"{ticker}({status[:20]})")
        elif not data.get("c"):
            missing.append(ticker)
        else:
            served += 1
            age = now.timestamp() - int(data.get("t") or 0)
            if age > 3 * 86400:
                stale.append(f"{ticker}({age / 86400:.0f}d)")
            if ticker in REFERENCE:
                print(f"   {ticker}: {data.get('c')} ({_age(data.get('t'))})")
        time.sleep(PAUSE)
    print(f"   served {served} of {len(FUNDS) + len(REFERENCE)}")
    print(f"   no quote: {' '.join(missing) or 'none'}")
    print(f"   last trade over 3 days old: {' '.join(stale) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

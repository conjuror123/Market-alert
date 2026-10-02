"""Bitfinex's hourly candles, for history only: ETH from 2016-03 and LTC from
2013-05, before Binance's own pairs matured (tools/binance_history.py).

/v2/candles/trade:1h:{symbol}/hist, up to 10,000 a request, ascending with
sort=1, no key; 30 requests a minute. Each row is [ms, open, CLOSE, HIGH, LOW,
volume] - close before high. An hour without a trade is not served.
"""
from __future__ import annotations

import time
from datetime import datetime

import requests

from price_monitor.models import Candle, ExchangeError

URL = "https://api-pub.bitfinex.com/v2/candles/trade:1h:{symbol}/hist"
LIMIT = 10000
REQUEST_DELAY_SECONDS = 2.5
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 10.0


def parse(rows: list) -> list[Candle]:
    out = []
    for ms, o, c, h, l, v in rows:
        if v <= 0:
            continue
        t = int(ms) // 1000
        out.append(Candle(open_time=t, open=float(o), high=float(h), low=float(l),
                          close=float(c), volume=float(v), close_time=t + 3600))
    return out


def _get(session, symbol: str, start_ms: int, end_ms: int) -> list:
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(
                URL.format(symbol=symbol),
                params={"start": start_ms, "end": end_ms, "limit": LIMIT, "sort": 1},
                timeout=60)
        except requests.RequestException as exc:
            last = ExchangeError(f"{symbol}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = ExchangeError(f"{symbol}: Bitfinex answered {resp.status_code}")
            continue
        body = resp.json()
        if resp.status_code != 200 or not isinstance(body, list):
            raise ExchangeError(f"{symbol}: Bitfinex answered {resp.status_code}: {str(body)[:200]}")
        return body
    raise last or ExchangeError(f"{symbol}: no response")


def fetch_history(symbol: str, start: datetime, end: datetime,
                  session: requests.Session | None = None) -> list[Candle]:
    """Traded hourly candles of `symbol` (tLTCUSD) opening in [start, end)."""
    cur, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    out: dict[int, Candle] = {}
    while cur < hi:
        rows = _get(session, symbol, cur, hi)
        if not rows:
            break
        for c in parse(rows):
            if c.open_time * 1000 < hi:
                out[c.open_time] = c
        nxt = int(rows[-1][0]) + 3600 * 1000
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(REQUEST_DELAY_SECONDS)
    return [out[t] for t in sorted(out)]

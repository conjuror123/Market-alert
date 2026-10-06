"""Coinbase Exchange's hourly candles: a second source for the coins.

WHY. The coins are fetched from Binance, whose prices are its own trades. A
wick on Binance alone is real there and not the market's. Coinbase is another
exchange, dollar-quoted, keyless, and reachable from US runners. Over the 300
hours to 2026-09-25 its hourly moves correlated 0.991-1.000 with the stored
Binance bars for all 16 coins (docs/decisions.md, "The second source").

WHAT IS ASKED. /products/{BTC}-USD/candles, granularity 3600, at most 300
candles a request, by start and end. Each row is [time, low, high, open, close,
volume], newest first, stamped at the start of its hour in UTC. An hour with no
trade has no row, which is a hole like any other missing hour.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from price_monitor.models import Candle, ExchangeError, Unreachable

URL = "https://api.exchange.coinbase.com/products/{product}/candles"
STEP = 3600
LIMIT = 300
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
REQUEST_DELAY_SECONDS = 0.3


def product_for(ticker: str) -> str:
    """BTC/USDT -> BTC-USD: the same coin against the dollar."""
    return ticker.split("/")[0] + "-USD"


def parse(rows: list) -> list[Candle]:
    """Traded hourly candles from one answer, ascending."""
    if not isinstance(rows, list):
        raise ExchangeError(f"Coinbase's answer has no candles: {str(rows)[:200]}")
    out = []
    for row in rows:
        t, low, high, opened, close, volume = row[:6]
        if float(volume) <= 0:
            continue
        out.append(Candle(open_time=int(t), open=float(opened), high=float(high),
                          low=float(low), close=float(close), volume=float(volume),
                          close_time=int(t) + STEP))
    return sorted(out, key=lambda c: c.open_time)


def _get(session, product: str, start: int, end: int) -> list:
    params = {"granularity": STEP,
              "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
              "end": datetime.fromtimestamp(end, timezone.utc).isoformat()}
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(
                URL.format(product=product), params=params,
                headers={"User-Agent": "market-alert-bot"}, timeout=30)
        except requests.RequestException as exc:
            last = Unreachable(f"{product}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = (Unreachable if resp.status_code >= 500 else ExchangeError)(
                f"{product}: Coinbase answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{product}: Coinbase answered {resp.status_code}: "
                                f"{resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ExchangeError(f"{product}: Coinbase's answer is not JSON") from exc
    raise last or ExchangeError(f"{product}: no response")


def fetch_history(product: str, start: datetime, end: datetime,
                  session: requests.Session | None = None,
                  request_delay_seconds: float = REQUEST_DELAY_SECONDS) -> list[Candle]:
    """Traded hourly candles of `product` (BTC-USD) opening in [start, end)."""
    lo, hi = int(start.timestamp()) // STEP * STEP, int(end.timestamp())
    seen: dict[int, Candle] = {}
    cursor = lo
    while cursor < hi:
        upto = min(hi, cursor + (LIMIT - 1) * STEP)
        for c in parse(_get(session, product, cursor, upto)):
            if lo <= c.open_time < hi:
                seen[c.open_time] = c
        cursor = upto + STEP
        if cursor < hi and request_delay_seconds:
            time.sleep(request_delay_seconds)
    return [seen[t] for t in sorted(seen)]

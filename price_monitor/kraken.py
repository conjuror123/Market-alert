"""Kraken's hourly candles: the coins' other voter, beside Coinbase.

WHY. A second independent dollar exchange, keyless and reachable from US
runners: over the 300 hours to 2026-09-25 its hourly moves correlated
0.988-1.000 with the stored Binance bars for all 16 coins. It is the third
vote, beside the store's and Coinbase's, so one exchange's wick is outvoted.

WHAT IS ASKED. /0/public/OHLC, interval 60: the last 720 hourly candles at
most, whatever `since` asks for. Each row is [time, open, high, low, close,
vwap, volume, count], stamped at the start of its hour in UTC. Kraken names
bitcoin XBT and dogecoin XDG. An hour with no trade comes as a candle of zero
volume at the last price, which is not a price: it is dropped.
"""
from __future__ import annotations

import time
from datetime import datetime

import requests

from price_monitor.models import Candle, ExchangeError, Unreachable

URL = "https://api.kraken.com/0/public/OHLC"
STEP = 3600
REACH_HOURS = 720
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
RENAMED = {"BTC": "XBT", "DOGE": "XDG"}


def pair_for(ticker: str) -> str:
    """BTC/USDT -> XBTUSD: the same coin against the dollar, in Kraken's names."""
    coin = ticker.split("/")[0]
    return RENAMED.get(coin, coin) + "USD"


def parse(payload: dict, pair: str) -> list[Candle]:
    """Traded hourly candles from one answer, ascending; the last, still open
    candle included (the caller keeps what has ended)."""
    errors = (payload or {}).get("error") or []
    if errors:
        raise ExchangeError(f"{pair}: Kraken answered {errors[0]}")
    result = (payload or {}).get("result") or {}
    rows = next((v for k, v in result.items() if k != "last"), None)
    if rows is None:
        raise ExchangeError(f"{pair}: Kraken's answer has no candles: {str(payload)[:200]}")
    out = []
    for row in rows:
        if float(row[6]) <= 0:
            continue
        t = int(row[0])
        out.append(Candle(open_time=t, open=float(row[1]), high=float(row[2]),
                          low=float(row[3]), close=float(row[4]), volume=float(row[6]),
                          close_time=t + STEP))
    return sorted(out, key=lambda c: c.open_time)


def fetch_history(pair: str, start: datetime, end: datetime,
                  session: requests.Session | None = None) -> list[Candle]:
    """Traded hourly candles of `pair` (XBTUSD) opening in [start, end), as far
    as Kraken's last 720 hours reach."""
    lo, hi = int(start.timestamp()), int(end.timestamp())
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(
                URL, params={"pair": pair, "interval": 60, "since": lo - STEP},
                headers={"User-Agent": "market-alert-bot"}, timeout=30)
        except requests.RequestException as exc:
            last = Unreachable(f"{pair}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = (Unreachable if resp.status_code >= 500 else ExchangeError)(
                f"{pair}: Kraken answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{pair}: Kraken answered {resp.status_code}: "
                                f"{resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ExchangeError(f"{pair}: Kraken's answer is not JSON") from exc
        return [c for c in parse(payload, pair) if lo <= c.open_time < hi]
    raise last or ExchangeError(f"{pair}: no response")

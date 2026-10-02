"""Binance's spot klines, for every coin: one exchange, one quote currency.

WHY BINANCE. The owner's call (2026-10-02): every coin as its USDT pair on one
exchange, for homogeneity, and named for what it is - ADA/USDT, not ADA-USD.
It also reaches further back than Coinbase for the coins Coinbase listed late:
POL (as MATIC) and ATOM 2019-04, DOGE 2019-07. For the five older coins a
dollar exchange's record goes under Binance's first, thin months, and for LINK
and ADA the coin's BTC pair times BTCUSDT under their late USDT listing
(tools/binance_history.py): BTC from 2013, LTC 2013-12, ETH 2016-04, XRP
2017-03, LINK 2017-10, ADA and BCH 2017-12/2018-01.

WHICH HOST. api.binance.com answers HTTP 451 to US addresses, which GitHub's
runners are. data-api.binance.vision is Binance's own market-data-only mirror
and answers them (measured from a runner, 2026-10-02).

WHAT IS ASKED. /api/v3/klines, interval 1h, up to 1,000 a request from
`startTime`; a start before the pair's listing returns its first bars. Each
kline is stamped at its open, in milliseconds. The hour still forming is kept,
as every live source here keeps it (jumps.ended judges it once it has ended).
Volume is in the coin.

SYMBOLS. Our ticker ADA/USDT is Binance's ADAUSDT. Two pairs are older pairs
renamed, joined by tools/binance_history.py: POL/USDT is MATICUSDT until
2024-09-10 (MATIC became POL one for one), and BCH/USDT is BCHABCUSDT from the
2018-11 fork until 2019-11-28.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests

from price_monitor.models import Candle, ExchangeError

BASE_URL = "https://data-api.binance.vision/api/v3/klines"
STEP = 3600
LIMIT = 1000
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
REQUEST_DELAY_SECONDS = 0.2


def symbol_for(ticker: str) -> str:
    """ADA/USDT -> ADAUSDT."""
    return ticker.replace("/", "").upper()


def parse(rows: list) -> list[Candle]:
    out = []
    for row in rows:
        t = int(row[0]) // 1000
        out.append(Candle(open_time=t, open=float(row[1]), high=float(row[2]),
                          low=float(row[3]), close=float(row[4]),
                          volume=float(row[5]), close_time=t + STEP))
    return out


def _get(session, symbol: str, start: int) -> list:
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(
                BASE_URL, params={"symbol": symbol, "interval": "1h",
                                  "startTime": start * 1000, "limit": LIMIT},
                timeout=30)
        except requests.RequestException as exc:
            last = ExchangeError(f"{symbol}: {exc}")
            continue
        if resp.status_code in (418, 429, 500, 502, 503, 504):
            last = ExchangeError(f"{symbol}: Binance answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{symbol}: Binance answered {resp.status_code}: "
                                f"{resp.text[:200]}")
        body = resp.json()
        if not isinstance(body, list):
            raise ExchangeError(f"{symbol}: Binance's answer has no klines: {str(body)[:200]}")
        return body
    raise last or ExchangeError(f"{symbol}: no response")


def fetch_history(symbol: str, start: datetime, end: datetime | None = None,
                  session: requests.Session | None = None,
                  request_delay_seconds: float = REQUEST_DELAY_SECONDS) -> list[Candle]:
    """Hourly candles of a Binance symbol (ADAUSDT) opening in [start, end)."""
    hi = int((end or datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    cursor = int(start.timestamp())
    out: dict[int, Candle] = {}
    while cursor < hi:
        rows = _get(session, symbol, cursor)
        if not rows:
            break
        for c in parse(rows):
            if c.open_time < hi:
                out[c.open_time] = c
        nxt = int(rows[-1][0]) // 1000 + STEP
        if nxt <= cursor or len(rows) < LIMIT:
            break
        cursor = nxt
        if request_delay_seconds:
            time.sleep(request_delay_seconds)
    return [out[t] for t in sorted(out)]


def fetch_full_history(symbol: str, interval: str, days: float,
                       session: requests.Session | None = None,
                       end: datetime | None = None) -> list[Candle]:
    """The backfill's entry point: `days` of hourly candles ending at `end`;
    `symbol` is our ticker (ADA/USDT)."""
    if interval != "1h":
        raise ExchangeError(f"{symbol}: only hourly klines are asked of Binance")
    stop = end or datetime.now(timezone.utc) + timedelta(hours=1)
    return fetch_history(symbol_for(symbol), stop - timedelta(days=days), stop, session)

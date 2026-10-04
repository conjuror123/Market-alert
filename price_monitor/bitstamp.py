"""Bitstamp's hourly candles, used for history only: XRP's years Coinbase did
not trade it.

WHY. Coinbase suspended XRP from 2021-01-19 to 2023-07-13 (the SEC's case), so
the store's XRP has a 905-day hole, and it begins at Coinbase's listing,
2019-02-26. Bitstamp kept trading XRP/USD through the suspension (for clients
outside the US) and has hourly bars from 2017, in dollars, no key. The splice
is gated on the hours both exchanges hold (tools/binance_history.py).

WHAT IS ASKED. /api/v2/ohlc/{pair}/, step 3600, up to 1,000 bars a request from
`start`. Each bar is stamped at its start, UTC. An hour with no trade is
served as a bar of zero volume at the last price, which is not a price: it is
dropped, and the hour is a hole like any other missing one.
"""
from __future__ import annotations

import time
from datetime import datetime

import requests

from price_monitor.models import Candle, ExchangeError

URL = "https://www.bitstamp.net/api/v2/ohlc/{pair}/"
STEP = 3600
LIMIT = 1000
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
REQUEST_DELAY_SECONDS = 0.5


def parse(payload: dict) -> list[Candle]:
    """Traded hourly candles from one answer, ascending."""
    rows = ((payload or {}).get("data") or {}).get("ohlc")
    if rows is None:
        raise ExchangeError(f"Bitstamp's answer has no candles: {str(payload)[:200]}")
    out = []
    for row in rows:
        volume = float(row["volume"])
        if volume <= 0:
            continue
        t = int(row["timestamp"])
        out.append(Candle(open_time=t, open=float(row["open"]), high=float(row["high"]),
                          low=float(row["low"]), close=float(row["close"]),
                          volume=volume, close_time=t + STEP))
    return sorted(out, key=lambda c: c.open_time)


def _get(session, pair: str, start: int) -> dict:
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(
                URL.format(pair=pair), params={"step": STEP, "limit": LIMIT, "start": start},
                headers={"User-Agent": "market-alert-bot"}, timeout=30)
        except requests.RequestException as exc:
            last = ExchangeError(f"{pair}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = ExchangeError(f"{pair}: Bitstamp answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{pair}: Bitstamp answered {resp.status_code}: "
                                f"{resp.text[:200]}")
        return resp.json()
    raise last or ExchangeError(f"{pair}: no response")


def fetch_history(pair: str, start: datetime, end: datetime,
                  session: requests.Session | None = None,
                  request_delay_seconds: float = REQUEST_DELAY_SECONDS) -> list[Candle]:
    """Traded hourly candles of `pair` (xrpusd) opening in [start, end)."""
    lo, hi = int(start.timestamp()), int(end.timestamp())
    out: list[Candle] = []
    cursor = lo
    while cursor < hi:
        payload = _get(session, pair, cursor)
        rows = ((payload or {}).get("data") or {}).get("ohlc") or []
        out.extend(c for c in parse(payload) if lo <= c.open_time < hi)
        # An empty answer is a stretch before the pair was listed: step over it.
        last = max((int(r["timestamp"]) for r in rows), default=cursor + (LIMIT - 1) * STEP)
        cursor = max(last, cursor) + STEP
        if request_delay_seconds:
            time.sleep(request_delay_seconds)
    seen: dict[int, Candle] = {c.open_time: c for c in out}
    return [seen[t] for t in sorted(seen)]

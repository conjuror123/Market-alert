"""SiftingIO REST client, used for the currency pairs.

WHY THE PAIRS LIVE HERE. Measured on 2026-09-30: the
free tier serves the bar of the hour that is still forming, so the bar that
closed at :00 is there by :05; it carries every pair the basket holds or plans
to - the Nordic and EM ones Tiingo and Dukascopy do not (BRL, INR, KRW); and
against the stored bars the eight held pairs agree to a median of 0.11 to 0.35
bps over three hundred hours. Moving them here also frees eight of Tiingo's
fifty hourly requests for funds.

NOT FOR THIN FUNDS. Its equity bars are "aggregated, derived" rather than the
consolidated tape, and on the thin commodity funds they drift from it - UGA by a
median 8.8 bps, p90 23.7 - which is the feed disagreement that fabricates
moves. The liquid funds agree (SPY 0.20 median); the stock path is here for
them, and for measuring, not for the thin ones.

THE WIRE. `X-API-Key` header, and `Accept-Encoding: gzip` is REQUIRED - the API
answers 406 without it. Times go out as RFC 3339; bars come back with `t` in
epoch milliseconds, the OPEN of the bar, covering [t, t + interval). A page is
at most 2000 bars and carries `meta.next_cursor` while more remain.

LIMITS. The free tier is 10,000 calls a MONTH and a few a second; the response
says how many remain (`X-Quota-Remaining`). Sixteen pairs asked once an hour
through the FX week, and USD/BRL in its session, is about 8,500 a month -
inside it, without much room. A
429 means the budget or the burst is spent, and RateLimited stops the run for
this provider rather than retrying into it.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from price_monitor.models import Candle, ExchangeError

log = logging.getLogger("price_monitor.sifting")

BASE_URL = "https://api.sifting.io/v1"
FX_ENDPOINT = "/hist/forex/{pair}/bars"
STOCK_ENDPOINT = "/hist/stocks/{ticker}/bars"

# This app's interval names -> SiftingIO's.
INTERVAL_CODES = {"30min": "30m", "1h": "1h"}
INTERVAL_SECONDS = {"30min": 1800, "1h": 3600}

PAGE_LIMIT = 2000
MAX_PAGES = 200            # a whole-history walk of one pair, with room
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
PAGE_PAUSE_SECONDS = 0.25  # the burst limit is a few a second


class RateLimited(ExchangeError):
    """The monthly quota or the per-second burst is spent. Not retried here:
    the caller stops asking this provider for the rest of the run."""

    def __init__(self, message: str, remaining: str | None = None):
        super().__init__(message)
        self.remaining = remaining


def is_fx(ticker: str) -> bool:
    return "/" in ticker or "_" in ticker


def fx_pair(ticker: str) -> str:
    """'USD/MXN' -> 'USDMXN', the only spelling the forex endpoint takes."""
    return ticker.replace("/", "").replace("_", "").upper()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(session, url: str, params: dict, api_key: str, ticker: str) -> dict:
    sess = session or requests
    headers = {"X-API-Key": api_key, "Accept-Encoding": "gzip"}
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = sess.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:
            last = ExchangeError(f"{ticker}: {exc}")
            continue
        if resp.status_code == 429:
            raise RateLimited(f"{ticker}: SiftingIO request budget spent",
                              remaining=resp.headers.get("X-Quota-Remaining"))
        if resp.status_code in (400, 401, 403, 404, 406):
            raise ExchangeError(
                f"{ticker}: SiftingIO rejected the request ({resp.status_code}): "
                f"{resp.text[:200]}")
        if resp.status_code in (500, 502, 503, 504):
            last = ExchangeError(f"{ticker}: status {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(
                f"{ticker}: unexpected status {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            last = ExchangeError(f"{ticker}: response was not JSON ({exc})")
            continue
        if not isinstance(payload, dict):
            raise ExchangeError(f"{ticker}: unexpected response shape "
                                f"{type(payload).__name__}")
        return payload
    raise last or ExchangeError(f"{ticker}: no response")


def _parse(rows: object, granularity: int, ticker: str) -> list[Candle]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ExchangeError(f"{ticker}: unexpected bar list {type(rows).__name__}")
    out: list[Candle] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("t") is None:
            continue
        o, h, l, c = row.get("o"), row.get("h"), row.get("l"), row.get("c")
        if o is None or h is None or l is None or c is None:
            continue
        t = int(row["t"]) // 1000
        out.append(Candle(open_time=t, open=float(o), high=float(h), low=float(l),
                          close=float(c), volume=float(row.get("v") or 0.0),
                          close_time=t + granularity))
    return out


def fetch_full_history(
    symbol: str,
    interval: str,
    days: float,
    base_url: str = BASE_URL,
    api_key: str = "",
    session: requests.Session | None = None,
    end: datetime | None = None,
) -> list[Candle]:
    """Up to `days` of candles ending at `end` (default now), paged through
    `next_cursor` until the window is exhausted."""
    if not api_key:
        raise ExchangeError("No SiftingIO API key configured (SIFTING_API_KEY)")
    try:
        code, granularity = INTERVAL_CODES[interval], INTERVAL_SECONDS[interval]
    except KeyError:
        raise ExchangeError(f"Unsupported SiftingIO interval '{interval}'") from None
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=max(days, 1.0 / 24))
    path = (FX_ENDPOINT.format(pair=fx_pair(symbol)) if is_fx(symbol)
            else STOCK_ENDPOINT.format(ticker=symbol))
    params = {"start": _stamp(start), "end": _stamp(end), "interval": code,
              "limit": PAGE_LIMIT}
    candles: list[Candle] = []
    for page in range(MAX_PAGES):
        payload = _get(session, f"{base_url}{path}", params, api_key, symbol)
        candles.extend(_parse(payload.get("data"), granularity, symbol))
        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        params = dict(params, cursor=cursor)
        time.sleep(PAGE_PAUSE_SECONDS)
    else:
        log.warning("%s: stopped after %d pages; the window may be incomplete",
                    symbol, MAX_PAGES)
    return candles

"""SiftingIO REST client, used for the currency pairs and spot gold and silver.

WHY THE PAIRS LIVE HERE. Measured on 2026-09-30: the
free tier serves the bar of the hour that is still forming, so the bar that
closed at :00 is there by :05; it carries every pair the basket holds or plans
to - the Nordic and EM ones Tiingo and Dukascopy do not (BRL, INR, KRW); and
against the stored bars the eight held pairs agree to a median of 0.11 to 0.35
bps over three hundred hours. Moving them here also frees eight of Tiingo's
fifty hourly requests for funds.

GOLD AND SILVER TOO, from its commodities endpoint (docs/decisions.md, "Gold and
silver from SiftingIO"). Asked at 04:02 UTC on 2026-10-12, it held the bar that
ended at 04:00 and the forming one, 117-118 hours a week with no Saturday bars,
and moved with Dukascopy's at 0.9992 (gold) and 0.9980 (silver) over 799 hours,
medians 0.25 and 1.06 bps. It keeps a thin bar in the metals' daily break (21:00
UTC in New York's summer) that Dukascopy and Wallstreetcn leave empty. Its spot
platinum (XPT/USD) votes on the platinum future, as do its SPY and UNG on those
funds (jump.verify, within the votes' share of config/provider_limits.csv).

NOT FOR THIN FUNDS. Its equity bars are "aggregated, derived" rather than the
consolidated tape, and on the thin commodity funds they drift from it - UGA by a
median 8.8 bps, p90 23.7 - which is the feed disagreement that fabricates
moves. The liquid funds agree (SPY 0.20 median); the stock path is here for
them, and for measuring, not for the thin ones.

THE WIRE. `X-API-Key` header, and `Accept-Encoding: gzip` is REQUIRED - the API
answers 406 without it. Times go out as RFC 3339; bars come back with `t` in
epoch milliseconds, the OPEN of the bar, covering [t, t + interval). A page is
at most 2000 bars and carries `meta.next_cursor` while more remain.

LIMITS. The free tier is 10,000 calls a MONTH and a few a second. The response's
`X-Quota-Remaining` is NOT the month: after a run's 16 requests it said 4 while the
account showed 181 for the month (2026-10-06), so it counts a short window; the
month is on the account page only. Seven pairs and the two metals asked once an
hour through the FX week are about 4,600 a month (sixteen pairs and USD/BRL were
about 8,700 until stage 1). A 429 means the budget or the burst is spent, and
RateLimited stops the run for this provider rather than retrying into it.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from price_monitor.models import Candle, ExchangeError, KeyRefused, Unreachable

log = logging.getLogger("price_monitor.sifting")

BASE_URL = "https://api.sifting.io/v1"
FX_ENDPOINT = "/hist/forex/{pair}/bars"
STOCK_ENDPOINT = "/hist/stocks/{ticker}/bars"
COMMODITY_ENDPOINT = "/hist/commodities/{symbol}/bars"
# Spelled as pairs, served by the commodities endpoint.
COMMODITIES = {"XAU/USD", "XAG/USD", "XPT/USD"}

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
            last = Unreachable(f"{ticker}: {exc}")
            continue
        if resp.status_code == 429:
            raise RateLimited(f"{ticker}: SiftingIO request budget spent",
                              remaining=resp.headers.get("X-Quota-Remaining"))
        if resp.status_code in (401, 403):
            raise KeyRefused(f"{ticker}: SiftingIO refused the key ({resp.status_code})")
        if resp.status_code in (400, 404, 406):
            raise ExchangeError(
                f"{ticker}: SiftingIO rejected the request ({resp.status_code}): "
                f"{resp.text[:200]}")
        if resp.status_code in (500, 502, 503, 504):
            last = Unreachable(f"{ticker}: status {resp.status_code}")
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
    if symbol in COMMODITIES:
        path = COMMODITY_ENDPOINT.format(symbol=fx_pair(symbol))
    elif is_fx(symbol):
        path = FX_ENDPOINT.format(pair=fx_pair(symbol))
    else:
        path = STOCK_ENDPOINT.format(ticker=symbol)
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

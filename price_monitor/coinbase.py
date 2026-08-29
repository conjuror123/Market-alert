"""Coinbase Exchange public REST client (crypto spot pairs, no API key required).

Binance's public API returns HTTP 451 (geo-restricted) from US-based IP ranges,
which is exactly where GitHub Actions' default hosted runners live - so it isn't a
reliable choice for this workflow. Coinbase Exchange's public candles endpoint is
free, unauthenticated, and not geo-restricted for US traffic.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests

from price_monitor.models import Candle, ExchangeError

CANDLES_ENDPOINT = "/products/{symbol}/candles"

# Coinbase Exchange only accepts these granularities, in seconds.
GRANULARITY_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
}

# The API refuses requests spanning more than 300 candles in one call.
MAX_CANDLES_PER_REQUEST = 300


def _granularity_seconds(interval: str) -> int:
    try:
        return GRANULARITY_SECONDS[interval]
    except KeyError as exc:
        raise ExchangeError(
            f"Unsupported interval '{interval}'. Supported: {sorted(GRANULARITY_SECONDS)}"
        ) from exc


def _request_candles(
    sess, url: str, params: dict, granularity: int, retries: int, backoff_seconds: float, symbol: str,
) -> list[Candle]:
    """One Coinbase candles request, with retries, parsed and sorted ascending."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = sess.get(url, params=params, timeout=15, headers={"User-Agent": "market-alert-bot"})
            if resp.status_code != 200:
                raise ExchangeError(
                    f"{symbol}: unexpected status {resp.status_code}: {resp.text[:200]}")
            candles = [
                Candle(
                    open_time=int(row[0]),
                    low=float(row[1]),
                    high=float(row[2]),
                    open=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    close_time=int(row[0]) + granularity,
                )
                for row in resp.json()
            ]
            # Coinbase returns newest-first; the rest of the app expects chronological order.
            candles.sort(key=lambda c: c.open_time)
            return candles
        except (requests.RequestException, ExchangeError, ValueError, KeyError, IndexError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
    raise ExchangeError(f"Failed to fetch klines for {symbol} after {retries} attempts: {last_error}")


def fetch_klines(
    symbol: str,
    interval: str,
    limit: int,
    base_url: str,
    retries: int = 3,
    backoff_seconds: float = 2.0,
    session: requests.Session | None = None,
    request_delay_seconds: float = 0.3,
) -> list[Candle]:
    """Fetch the most recent `limit` candles for `symbol`/`interval`, oldest first.

    `symbol` is a Coinbase product id, e.g. "BTC-USD". A single Coinbase request
    caps out at MAX_CANDLES_PER_REQUEST candles, so `limit` above that is served by
    paging backwards with explicit start/end windows and merging the results (the
    same approach `fetch_full_history` uses for backtesting) - callers just ask for
    however many candles `lookback` needs and get them, regardless of the API's
    per-request cap.
    """
    granularity = _granularity_seconds(interval)
    url = f"{base_url}{CANDLES_ENDPOINT.format(symbol=symbol)}"
    sess = session or requests

    if limit <= MAX_CANDLES_PER_REQUEST:
        candles = _request_candles(
            sess, url, {"granularity": granularity}, granularity, retries, backoff_seconds, symbol)
        return candles[-limit:]

    chunk_span = timedelta(seconds=granularity * (MAX_CANDLES_PER_REQUEST - 1))
    end = datetime.now(timezone.utc)
    start_bound = end - timedelta(seconds=granularity * limit)
    by_time: dict[int, Candle] = {}
    chunk_end = end
    while chunk_end > start_bound and len(by_time) < limit:
        chunk_start = max(start_bound, chunk_end - chunk_span)
        params = {
            "granularity": granularity,
            "start": chunk_start.isoformat(),
            "end": chunk_end.isoformat(),
        }
        for c in _request_candles(sess, url, params, granularity, retries, backoff_seconds, symbol):
            by_time[c.open_time] = c
        chunk_end = chunk_start
        if chunk_end > start_bound:
            time.sleep(request_delay_seconds)

    return sorted(by_time.values(), key=lambda c: c.open_time)[-limit:]


def fetch_full_history(
    symbol: str,
    interval: str,
    days: float,
    base_url: str,
    session: requests.Session | None = None,
    request_delay_seconds: float = 0.3,
) -> list[Candle]:
    """Page through Coinbase's 300-candle-per-request cap with explicit start/end
    windows to build up to `days` of history. Only meant for offline backtesting,
    which wants "N days" rather than "N candles" - the production monitor uses
    `fetch_klines` (which paginates the same way, just keyed by candle count).
    """
    granularity = _granularity_seconds(interval)
    sess = session or requests
    url = f"{base_url}{CANDLES_ENDPOINT.format(symbol=symbol)}"
    end = datetime.now(timezone.utc)
    start_bound = end - timedelta(days=days)
    chunk_span = timedelta(seconds=granularity * (MAX_CANDLES_PER_REQUEST - 1))

    by_time: dict[int, Candle] = {}
    chunk_end = end
    while chunk_end > start_bound:
        chunk_start = max(start_bound, chunk_end - chunk_span)
        params = {
            "granularity": granularity,
            "start": chunk_start.isoformat(),
            "end": chunk_end.isoformat(),
        }
        resp = sess.get(url, params=params, timeout=15, headers={"User-Agent": "market-alert-bot"})
        if resp.status_code != 200:
            raise ExchangeError(f"{symbol}: unexpected status {resp.status_code}: {resp.text[:200]}")
        for row in resp.json():
            t = int(row[0])
            by_time[t] = Candle(
                open_time=t, low=float(row[1]), high=float(row[2]), open=float(row[3]),
                close=float(row[4]), volume=float(row[5]), close_time=t + granularity,
            )
        chunk_end = chunk_start
        if chunk_end > start_bound:
            time.sleep(request_delay_seconds)

    return sorted(by_time.values(), key=lambda c: c.open_time)

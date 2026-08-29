"""Twelve Data REST client (spot forex pairs), used in place of Yahoo Finance
for this asset class.

Unlike Yahoo's unofficial chart endpoint (used elsewhere in this app for
futures/indices), this is a documented, officially supported API - a second,
independent provider so a Yahoo outage doesn't take every asset down at once.
Needs a free API key (see README): the free tier is 800 API credits/day and
8/minute, one credit per symbol per request - which is why config.yaml caps
forex at exactly 8 pairs, to use the whole per-minute budget in one run
without tripping the limit.

As with spot FX on Yahoo, there is no centralized trade volume for currency
pairs - `volume` is always 0.0 here too, and that's expected, not a bug.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests

from price_monitor.models import Candle, ExchangeError

TIME_SERIES_ENDPOINT = "/time_series"

# Twelve Data's interval codes, keyed by this app's own interval names.
INTERVAL_CODES = {
    "1h": "1h",
    "1d": "1day",
}

# The API accepts at most 5000 candles per request.
MAX_OUTPUTSIZE = 5000


def _interval_code(interval: str) -> str:
    try:
        return INTERVAL_CODES[interval]
    except KeyError as exc:
        raise ExchangeError(
            f"Unsupported interval '{interval}'. Supported: {sorted(INTERVAL_CODES)}"
        ) from exc


def _parse_datetime(value: str) -> datetime:
    # Always requested with timezone=UTC (see callers) - Twelve Data's default
    # is "Exchange" time, which for forex is NOT UTC (verified live: off by a
    # fixed 10 hours from a same-moment UTC request) and would silently
    # misalign every candle against the rest of the app if left unset.
    fmt = "%Y-%m-%d %H:%M:%S" if " " in value else "%Y-%m-%d"
    return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)


def _request(
    sess, url: str, params: dict, granularity_seconds: int, retries: int, backoff_seconds: float, symbol: str,
) -> list[Candle]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = sess.get(url, params=params, timeout=15, headers={"User-Agent": "market-alert-bot"})
            if resp.status_code != 200:
                raise ExchangeError(f"{symbol}: unexpected status {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            if data.get("status") == "error":
                raise ExchangeError(f"{symbol}: Twelve Data error: {data.get('message', data)}")
            values = data.get("values") or []
            candles = [
                Candle(
                    open_time=int(_parse_datetime(v["datetime"]).timestamp()),
                    open=float(v["open"]),
                    high=float(v["high"]),
                    low=float(v["low"]),
                    close=float(v["close"]),
                    volume=0.0,
                    close_time=int(_parse_datetime(v["datetime"]).timestamp()) + granularity_seconds,
                )
                for v in values
            ]
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
    api_key: str,
    retries: int = 3,
    backoff_seconds: float = 2.0,
    session: requests.Session | None = None,
) -> list[Candle]:
    """Fetch the most recent `limit` candles for `symbol`/`interval`, oldest first.

    `symbol` is a Twelve Data forex pair, e.g. "EUR/USD". A single request
    covers up to MAX_OUTPUTSIZE candles - comfortably more than this app's
    lookback needs - so no pagination is needed for live use.
    """
    if not api_key:
        raise ExchangeError("No Twelve Data API key configured (TWELVEDATA_API_KEY)")

    granularity_seconds = 3600 if interval == "1h" else 86400
    url = f"{base_url}{TIME_SERIES_ENDPOINT}"
    params = {
        "symbol": symbol,
        "interval": _interval_code(interval),
        "outputsize": min(limit, MAX_OUTPUTSIZE),
        "timezone": "UTC",
        "apikey": api_key,
    }
    sess = session or requests
    candles = _request(sess, url, params, granularity_seconds, retries, backoff_seconds, symbol)
    return candles[-limit:]


def fetch_full_history(
    symbol: str,
    interval: str,
    days: float,
    base_url: str,
    api_key: str,
    session: requests.Session | None = None,
    request_delay_seconds: float = 8.0,
) -> list[Candle]:
    """Page through date ranges to build up to `days` of history - only meant
    for offline backtesting, which wants a full year even though a single
    request caps out at MAX_OUTPUTSIZE candles. Chunked by calendar days
    (not candle count, since a chunk may span a weekend with no candles) and
    paced well under the 8-credits/minute free-tier limit by default.
    """
    if not api_key:
        raise ExchangeError("No Twelve Data API key configured (TWELVEDATA_API_KEY)")

    granularity_seconds = 3600 if interval == "1h" else 86400
    interval_code = _interval_code(interval)
    url = f"{base_url}{TIME_SERIES_ENDPOINT}"
    sess = session or requests
    end = datetime.now(timezone.utc)
    start_bound = end - timedelta(days=days)
    # MAX_OUTPUTSIZE hourly candles is ~208 days if every hour had one - use a
    # safely smaller chunk so weekends/holidays inside a chunk never risk
    # brushing up against the per-request cap.
    chunk_span = timedelta(days=150)

    by_time: dict[int, Candle] = {}
    chunk_end = end
    while chunk_end > start_bound:
        chunk_start = max(start_bound, chunk_end - chunk_span)
        params = {
            "symbol": symbol,
            "interval": interval_code,
            "timezone": "UTC",
            "apikey": api_key,
            "start_date": chunk_start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
        }
        for c in _request(sess, url, params, granularity_seconds, retries=3, backoff_seconds=8.0, symbol=symbol):
            by_time[c.open_time] = c
        chunk_end = chunk_start
        if chunk_end > start_bound:
            time.sleep(request_delay_seconds)

    return sorted(by_time.values(), key=lambda c: c.open_time)

"""Public market data client (Coinbase Exchange REST API, no API key required).

Binance's public API returns HTTP 451 (geo-restricted) from US-based IP ranges,
which is exactly where GitHub Actions' default hosted runners live - so it isn't a
reliable choice for this workflow. Coinbase Exchange's public candles endpoint is
free, unauthenticated, not geo-restricted for US traffic, and offers a native
900-second (15 minute) granularity that lines up with the schedule this app runs on.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

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


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


class ExchangeError(RuntimeError):
    pass


def _granularity_seconds(interval: str) -> int:
    try:
        return GRANULARITY_SECONDS[interval]
    except KeyError as exc:
        raise ExchangeError(
            f"Unsupported interval '{interval}'. Supported: {sorted(GRANULARITY_SECONDS)}"
        ) from exc


def fetch_klines(
    symbol: str,
    interval: str,
    limit: int,
    base_url: str,
    retries: int = 3,
    backoff_seconds: float = 2.0,
    session: requests.Session | None = None,
) -> list[Candle]:
    """Fetch the most recent `limit` candles for `symbol`/`interval`, oldest first.

    `symbol` is a Coinbase product id, e.g. "BTC-USD". `limit` is capped at 300
    (the API's per-request maximum).
    """
    if limit > MAX_CANDLES_PER_REQUEST:
        raise ExchangeError(
            f"limit={limit} exceeds Coinbase's {MAX_CANDLES_PER_REQUEST}-candle "
            "per-request maximum; lower `lookback` in config."
        )

    granularity = _granularity_seconds(interval)
    url = f"{base_url}{CANDLES_ENDPOINT.format(symbol=symbol)}"
    params = {"granularity": granularity}
    sess = session or requests
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            resp = sess.get(url, params=params, timeout=15, headers={"User-Agent": "market-alert-bot"})
            if resp.status_code != 200:
                raise ExchangeError(
                    f"{symbol}: unexpected status {resp.status_code}: {resp.text[:200]}")
            raw = resp.json()
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
                for row in raw
            ]
            # Coinbase returns newest-first; the rest of the app expects chronological order.
            candles.sort(key=lambda c: c.open_time)
            return candles[-limit:]
        except (requests.RequestException, ExchangeError, ValueError, KeyError, IndexError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
    raise ExchangeError(f"Failed to fetch klines for {symbol} after {retries} attempts: {last_error}")

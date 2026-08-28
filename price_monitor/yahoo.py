"""Yahoo Finance chart API client (futures/indices/FX, no API key required).

Covers asset classes Coinbase doesn't have: commodities and index futures
(gold GC=F, WTI crude CL=F, S&P 500 e-mini ES=F, 30-year T-bond ZB=F) and spot FX
(EURUSD=X, CNY=X for USD/CNY). This is Yahoo's unofficial/undocumented chart
endpoint - there's no official public API for this data without a paid vendor, and
Yahoo could change or rate-limit it without notice. Spot FX quotes never carry a
real trade volume here (no centralized volume for FX), so `volume` comes back as 0
for those symbols and the volume-based alert simply never fires for them - only the
price-based signal applies.
"""
from __future__ import annotations

import time
import urllib.parse

import requests

from price_monitor.models import Candle, ExchangeError

CHART_ENDPOINT = "/v8/finance/chart/{symbol}"

# Yahoo's chart interval codes, keyed by this app's own interval names.
YAHOO_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "60m",
    "6h": "60m",  # Yahoo has no native 6h bar; caller should aggregate if needed.
    "1d": "1d",
}

GRANULARITY_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
}

# Wide enough to comfortably cover any `lookback` this app uses for 1h/1d bars.
DEFAULT_RANGE = "60d"


def _granularity_seconds(interval: str) -> int:
    try:
        return GRANULARITY_SECONDS[interval]
    except KeyError as exc:
        raise ExchangeError(
            f"Unsupported interval '{interval}'. Supported: {sorted(GRANULARITY_SECONDS)}"
        ) from exc


def _parse_chart_response(raw: dict, granularity: int, limit: int) -> list[Candle]:
    try:
        result = raw["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as exc:
        error = (raw.get("chart") or {}).get("error") if isinstance(raw, dict) else None
        raise ExchangeError(f"Unexpected Yahoo chart response ({error or 'no data'})") from exc

    opens, highs, lows, closes, volumes = (
        quote.get("open", []), quote.get("high", []), quote.get("low", []),
        quote.get("close", []), quote.get("volume", []),
    )

    candles = []
    for t, o, h, l, c, v in zip(timestamps, opens, highs, lows, closes, volumes):
        # Yahoo pads non-trading periods with nulls (market closed, holiday, etc.).
        if None in (o, h, l, c):
            continue
        candles.append(Candle(
            open_time=int(t), open=float(o), high=float(h), low=float(l),
            close=float(c), volume=float(v or 0.0), close_time=int(t) + granularity,
        ))

    candles.sort(key=lambda c: c.open_time)
    return candles[-limit:]


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

    `symbol` is a Yahoo Finance ticker, e.g. "GC=F" (gold futures) or "EURUSD=X".
    """
    granularity = _granularity_seconds(interval)
    yahoo_interval = YAHOO_INTERVAL[interval]
    url = f"{base_url}{CHART_ENDPOINT.format(symbol=urllib.parse.quote(symbol))}"
    params = {"interval": yahoo_interval, "range": DEFAULT_RANGE}
    headers = {"User-Agent": "Mozilla/5.0 (market-alert-bot)"}
    sess = session or requests
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            resp = sess.get(url, params=params, timeout=15, headers=headers)
            if resp.status_code != 200:
                raise ExchangeError(
                    f"{symbol}: unexpected status {resp.status_code}: {resp.text[:200]}")
            return _parse_chart_response(resp.json(), granularity, limit)
        except (requests.RequestException, ExchangeError, ValueError, KeyError, IndexError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
    raise ExchangeError(f"Failed to fetch klines for {symbol} after {retries} attempts: {last_error}")

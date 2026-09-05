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
pairs - `volume` is always 0.0 for them here too, and that's expected, not a
bug. Exchange-traded funds served by the same API do carry real hourly volume,
and it is parsed when present.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from price_monitor.models import Candle, ExchangeError

log = logging.getLogger("price_monitor.twelvedata")

TIME_SERIES_ENDPOINT = "/time_series"

# Twelve Data's interval codes, keyed by this app's own interval names.
INTERVAL_CODES = {
    "30min": "30min",
    "1h": "1h",
    "1d": "1day",
}

# Bar duration in seconds - needed to set close_time. Kept next to INTERVAL_CODES
# on purpose: an interval added to only one of the two dictionaries would quietly
# drift out of step with the other.
INTERVAL_SECONDS = {
    "30min": 1800,
    "1h": 3600,
    "1d": 86400,
}

# The API accepts at most 5000 candles per request.
MAX_OUTPUTSIZE = 5000

# Twelve Data says this when the requested window lies entirely before the data
# it holds for that symbol and interval. It is not a failure - it is the answer
# "that is as far back as this goes", and the only way to find the edge, since
# the API does not publish per-symbol history depth. Intraday depth on the free
# tier is a few years, so any walk back to 2015 reaches it.
_NO_DATA_MESSAGE = "no data is available"


class PermanentExchangeError(ExchangeError):
    """A request that will fail identically however many times it is repeated.

    Retrying one costs credits and wall-clock time and cannot succeed: a walk
    back through history hits the same 400 on every chunk past the edge, and at
    three attempts with 8-second backoff that is 3 credits and 24 seconds burnt
    per dead chunk. Rate limiting (429) and server faults are NOT this - those
    are exactly the ones worth retrying.
    """


class NoDataInRange(PermanentExchangeError):
    """The requested window is before the start of this symbol's history."""


def _interval_code(interval: str) -> str:
    try:
        return INTERVAL_CODES[interval]
    except KeyError as exc:
        raise ExchangeError(
            f"Unsupported interval '{interval}'. Supported: {sorted(INTERVAL_CODES)}"
        ) from exc


def _granularity_seconds(interval: str) -> int:
    try:
        return INTERVAL_SECONDS[interval]
    except KeyError as exc:
        raise ExchangeError(
            f"Unsupported interval '{interval}'. Supported: {sorted(INTERVAL_SECONDS)}"
        ) from exc


def _parse_datetime(value: str) -> datetime:
    # Always requested with timezone=UTC (see callers) - Twelve Data's default
    # is "Exchange" time, which for forex is NOT UTC (verified live: off by a
    # fixed 10 hours from a same-moment UTC request) and would silently
    # misalign every candle against the rest of the app if left unset.
    fmt = "%Y-%m-%d %H:%M:%S" if " " in value else "%Y-%m-%d"
    return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)


def _classify(symbol: str, status_code: int, body: str) -> ExchangeError:
    """Turns a failed response into the exception that says what to do with it."""
    # Matched on the status as well as the message. Reading the message alone
    # would let a 429 whose body happened to carry that phrase be classified
    # permanent and never retried - and a rate limit dropped as if it were the
    # end of history ends the walk early and silently, which is the one failure
    # here that looks exactly like success.
    if status_code == 400 and _NO_DATA_MESSAGE in body.lower():
        return NoDataInRange(f"{symbol}: no data before the requested window")
    # 429 is the rate limit and 5xx are the server's problem; both are worth
    # another attempt. Everything else in the 4xx range is a statement about the
    # request itself and will not become true by being asked again.
    if 400 <= status_code < 500 and status_code != 429:
        return PermanentExchangeError(
            f"{symbol}: unexpected status {status_code}: {body[:200]}")
    return ExchangeError(f"{symbol}: unexpected status {status_code}: {body[:200]}")


def _request(
    sess, url: str, params: dict, granularity_seconds: int, retries: int, backoff_seconds: float, symbol: str,
) -> list[Candle]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = sess.get(url, params=params, timeout=15, headers={"User-Agent": "market-alert-bot"})
            if resp.status_code != 200:
                raise _classify(symbol, resp.status_code, resp.text)
            data = resp.json()
            if data.get("status") == "error":
                message = str(data.get("message", data))
                raise _classify(symbol, int(data.get("code") or 0), message)
            values = data.get("values") or []
            candles = [
                Candle(
                    open_time=int(_parse_datetime(v["datetime"]).timestamp()),
                    open=float(v["open"]),
                    high=float(v["high"]),
                    low=float(v["low"]),
                    close=float(v["close"]),
                    # Spot FX arrives without volume (no provider has a
                    # consolidated exchange volume for it), while ETFs arrive with
                    # the real thing. This used to be a hard-coded zero: correct
                    # for currency pairs, but for ETFs it silently discarded real
                    # data on which the §3.5 volume profile is built.
                    volume=float(v.get("volume") or 0.0),
                    close_time=int(_parse_datetime(v["datetime"]).timestamp()) + granularity_seconds,
                )
                for v in values
            ]
            candles.sort(key=lambda c: c.open_time)
            return candles
        except PermanentExchangeError:
            raise
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

    granularity_seconds = _granularity_seconds(interval)
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
    chunk_days: int = 150,
    end: datetime | None = None,
) -> list[Candle]:
    """Page through date ranges to build up to `days` of history - only meant
    for offline backtesting, which wants a full year even though a single
    request caps out at MAX_OUTPUTSIZE candles. Chunked by calendar days
    (not candle count, since a chunk may span a weekend with no candles) and
    paced well under the 8-credits/minute free-tier limit by default.
    """
    if not api_key:
        raise ExchangeError("No Twelve Data API key configured (TWELVEDATA_API_KEY)")

    granularity_seconds = _granularity_seconds(interval)
    interval_code = _interval_code(interval)
    url = f"{base_url}{TIME_SERIES_ENDPOINT}"
    sess = session or requests
    # `end` lets a caller that already holds the recent bars start the walk at
    # its own oldest one instead of at today, so deepening an archive does not
    # spend a credit per chunk re-fetching what is already stored. On the free
    # tier that is the difference between reaching 2015 and running out of
    # daily credits somewhere in 2019.
    end = end or datetime.now(timezone.utc)
    start_bound = end - timedelta(days=days)
    # MAX_OUTPUTSIZE hourly candles is ~208 days if every hour had one - the
    # 150-day default leaves room so weekends/holidays inside a chunk never risk
    # brushing up against the per-request cap. Callers fetching a sparser series
    # (a US equity ETF trades ~13 half-hour bars a day, not 24 hourly ones) can
    # raise chunk_days and cover the same span in far fewer credits.
    chunk_span = timedelta(days=chunk_days)

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
        try:
            fetched = _request(sess, url, params, granularity_seconds,
                               retries=3, backoff_seconds=8.0, symbol=symbol)
        except NoDataInRange:
            # The edge of this symbol's history, which is the normal way a walk
            # back to a date older than the provider holds ends. Not an error,
            # and nothing further back can exist either - so stop asking.
            break
        except ExchangeError as exc:
            # Anything else: keep what the earlier chunks already cost. This
            # used to propagate, and the whole dict went with it - a walk from
            # 2026 back to 2015 that succeeded for five years and then hit one
            # bad chunk returned NOTHING, discarding every candle fetched and
            # every credit spent on them. A short history is recoverable on the
            # next run; a spent daily quota is not.
            log.warning("%s: stopping the history walk at %s: %s",
                        symbol, chunk_start.date(), exc)
            break
        for c in fetched:
            by_time[c.open_time] = c
        chunk_end = chunk_start
        if chunk_end > start_bound:
            time.sleep(request_delay_seconds)

    return sorted(by_time.values(), key=lambda c: c.open_time)

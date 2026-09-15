"""Tiingo REST client, used for the currency pairs and the liquid ETFs.

WHY THE PAIRS LIVE HERE. Eight currency pairs are 15% of the basket and 41% of
every request the monitor makes, because FX has no session table to skip by and
so is asked twenty-four times a day each. On Twelve Data that is 192 credits of
an 800-credit budget and 64 seconds of enforced pacing. Tiingo serves the same
pairs with no pacing at all, at 0.281 seconds a request.

Free FX access is not documented - the pricing page lists "EOD composite
prices, crypto, IEX feed, and news" and omits forex entirely - but a free key
reaches /tiingo/fx and returns OHLC at both 30min and 1hour. Measured against
the stored bars over 124 overlapping hours, the eight pairs agree to a median
of 0.43 to 1.57 bps, which is under a twentieth of an hourly sigma. Yahoo,
tested the same way, is two to three times worse on the same pairs because its
FX quotes are indicative rather than a traded feed - so FX belongs here and the
ETFs mostly do not.

WHICH ETFs, AND WHY NOT ALL OF THEM. The intraday feed is IEX only - one
exchange, about 5.5% of consolidated volume. For liquid funds that is the same
price to a fifth of a basis point. For thin ones it is a different price: over
the same window SOYB drifts 7.2 bps, UGA 7.97 with a worst hour of 76.8, which
is roughly two sigma of pure feed disagreement and would be delivered as a move
that never happened. Those go to Yahoo, which serves the consolidated tape.
config/basket.yaml records the split; docs/tiingo-findings.md records the
measurement behind it.

VOLUME MUST BE ASKED FOR BY NAME. The intraday response carries no volume field
at all unless `columns` names it - not zero, absent. Callers that forget get
bars that look complete and silently carry 0.0.

LIMITS. 50 requests/hour and 1000/day on the free tier. The hourly bucket is
the binding one and it is small: 37 symbols a run leaves 13 spare. Both limits
answer 429, which is why RateLimited stops the run for this provider rather
than retrying into a wall.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

from price_monitor.models import Candle, ExchangeError

log = logging.getLogger("price_monitor.tiingo")

BASE_URL = "https://api.tiingo.com"
IEX_ENDPOINT = "/iex/{ticker}/prices"
FX_ENDPOINT = "/tiingo/fx/{ticker}/prices"
DAILY_ENDPOINT = "/tiingo/daily/{ticker}/prices"

# This app's interval names -> Tiingo's resampleFreq.
INTERVAL_CODES = {"30min": "30min", "1h": "1hour", "1d": "daily"}

INTERVAL_SECONDS = {"30min": 1800, "1h": 3600, "1d": 86400}

# Without this the intraday rows carry OHLC and nothing else.
IEX_COLUMNS = "open,high,low,close,volume"

# How far back each feed will serve. The IEX intraday archive reaches January
# 2018 at least, but a single response is capped at 10000 rows, so anything
# deeper than a routine top-up belongs to a provider that pages.
MAX_LOOKBACK_DAYS = {"30min": 365, "1h": 365, "1d": 36500}

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


class RateLimited(ExchangeError):
    """The hourly or daily request bucket is empty.

    Not retried here. Tiingo answers 429 for both the 50-an-hour and the
    1000-a-day limit, and neither clears inside a run - so the caller stops
    asking this provider and keeps what it already has, exactly as the Twelve
    Data client does with a spent daily budget.
    """

    def __init__(self, message: str, remaining: str | None = None):
        super().__init__(message)
        self.remaining = remaining


def _interval_code(interval: str) -> str:
    try:
        return INTERVAL_CODES[interval]
    except KeyError:
        raise ExchangeError(f"Unsupported Tiingo interval '{interval}'") from None


def _granularity_seconds(interval: str) -> int:
    try:
        return INTERVAL_SECONDS[interval]
    except KeyError:
        raise ExchangeError(f"Unsupported Tiingo interval '{interval}'") from None


def fx_ticker(ticker: str) -> str:
    """'EUR/USD' -> 'eurusd', which is the only spelling the FX endpoint takes."""
    return ticker.replace("/", "").replace("_", "").lower()


def is_fx(ticker: str) -> bool:
    return "/" in ticker or "_" in ticker


def _parse(rows: object, granularity: int, ticker: str) -> list[Candle]:
    if not isinstance(rows, list):
        raise ExchangeError(f"{ticker}: unexpected response shape {type(rows).__name__}")
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = row.get("date")
        if not stamp:
            continue
        # Tiingo stamps intraday rows in UTC with a trailing Z and FX rows the
        # same way. A naive value would be read as local time by fromisoformat,
        # which on a UTC runner is right by accident and wrong anywhere else.
        text = str(stamp).replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            raise ExchangeError(f"{ticker}: cannot read timestamp {stamp!r}") from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        o, h = row.get("open"), row.get("high")
        l, c = row.get("low"), row.get("close")
        if o is None or h is None or l is None or c is None:
            continue
        t = int(moment.timestamp())
        candles.append(Candle(open_time=t, open=float(o), high=float(h),
                              low=float(l), close=float(c),
                              volume=float(row.get("volume") or 0.0),
                              close_time=t + granularity))
    return candles


def _get_json(session, url: str, params: dict, api_key: str, ticker: str):
    """HTTP half of a Tiingo GET: auth, retries, the status codes that stop us.

    Returns the decoded JSON payload. Callers that want candles go through
    `_request`; the daily endpoint has a different row shape and reads this
    directly so `divCash` / `splitFactor` cannot be dropped by `_parse`.
    """
    sess = session or requests
    headers = {"Content-Type": "application/json",
               "Authorization": f"Token {api_key}"}
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
            remaining = (resp.headers.get("X-RateLimit-Remaining")
                         or resp.headers.get("X-Ratelimit-Remaining"))
            raise RateLimited(f"{ticker}: Tiingo request budget spent",
                              remaining=remaining)
        if resp.status_code in (404, 400):
            raise ExchangeError(
                f"{ticker}: Tiingo rejected the request ({resp.status_code}): "
                f"{resp.text[:200]}")
        if resp.status_code in (500, 502, 503, 504):
            last = ExchangeError(f"{ticker}: status {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(
                f"{ticker}: unexpected status {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            last = ExchangeError(f"{ticker}: response was not JSON ({exc})")
            continue
    raise last or ExchangeError(f"{ticker}: no response")


def _request(session, url: str, params: dict, api_key: str, granularity: int,
             ticker: str) -> list[Candle]:
    return _parse(_get_json(session, url, params, api_key, ticker),
                  granularity, ticker)


@dataclass(frozen=True)
class DailyRow:
    day: date
    close: float
    adj_close: float
    div_cash: float
    split_factor: float


def _as_day(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _parse_day(stamp: object) -> date | None:
    text = str(stamp).replace("Z", "+00:00")
    try:
        if "T" in text:
            moment = datetime.fromisoformat(text)
            return moment.date()
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_daily(rows: object, ticker: str) -> list[DailyRow]:
    if not isinstance(rows, list):
        raise ExchangeError(f"{ticker}: unexpected response shape {type(rows).__name__}")
    out: list[DailyRow] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = _parse_day(row.get("date"))
        close = row.get("close")
        if day is None or close is None:
            continue
        adj = row.get("adjClose")
        if adj is None:
            adj = close
        out.append(DailyRow(
            day=day,
            close=float(close),
            adj_close=float(adj),
            div_cash=float(row.get("divCash") or 0.0),
            split_factor=float(row.get("splitFactor") or 1.0),
        ))
    out.sort(key=lambda r: r.day)
    return out


def fetch_daily_history(
    symbol: str,
    start,
    api_key: str = "",
    session: requests.Session | None = None,
    end=None,
    base_url: str = BASE_URL,
) -> list[DailyRow]:
    """EOD composite rows, including declared `divCash` and `splitFactor`.

    `columns` is deliberately not sent. On the intraday endpoint omitting it
    silently drops volume; on this endpoint the full row is the default and
    asking for a subset risks losing `divCash` the same way. This is not the
    IEX feed, so the liquidity split that governs which ETFs may use `/iex`
    does not apply.
    """
    if not api_key:
        raise ExchangeError("No Tiingo API key configured (TIINGO_API_KEY)")
    url = f"{base_url}{DAILY_ENDPOINT.format(ticker=symbol)}"
    params = {"startDate": _as_day(start)}
    if end is not None:
        params["endDate"] = _as_day(end)
    return _parse_daily(_get_json(session, url, params, api_key, symbol), symbol)


def fetch_full_history(
    symbol: str,
    interval: str,
    days: float,
    base_url: str = BASE_URL,
    api_key: str = "",
    session: requests.Session | None = None,
    end: datetime | None = None,
) -> list[Candle]:
    """Up to `days` of candles ending at `end` (default now).

    One request covers the window. `startDate` is always sent: without it the
    intraday endpoint answers with the current day only, which would silently
    truncate a gap fill to whatever happened since midnight.
    """
    if not api_key:
        raise ExchangeError("No Tiingo API key configured (TIINGO_API_KEY)")
    granularity = _granularity_seconds(interval)
    limit = MAX_LOOKBACK_DAYS[interval]
    if days > limit:
        raise ExchangeError(
            f"{symbol}: Tiingo serves at most {limit} days at {interval} in one "
            f"response; {days:.0f} were asked for.")
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=max(days, 1.0))

    if is_fx(symbol):
        url = f"{base_url}{FX_ENDPOINT.format(ticker=fx_ticker(symbol))}"
        params = {"startDate": start.strftime("%Y-%m-%d"),
                  "endDate": end.strftime("%Y-%m-%d"),
                  "resampleFreq": _interval_code(interval)}
    else:
        url = f"{base_url}{IEX_ENDPOINT.format(ticker=symbol)}"
        params = {"startDate": start.strftime("%Y-%m-%d"),
                  "endDate": end.strftime("%Y-%m-%d"),
                  "resampleFreq": _interval_code(interval),
                  "columns": IEX_COLUMNS}

    # Not trimmed to the requested window - see the note in the Yahoo client:
    # an extra bar from before `start` is real data and bars.merge unions it.
    return _request(session, url, params, api_key, granularity, symbol)

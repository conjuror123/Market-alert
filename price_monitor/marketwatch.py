"""MarketWatch's hourly bars: a second source for the currency pairs and live cattle.

THE ENDPOINT is the one MarketWatch's own charts read (api.wsj.net, Dow Jones'
"michelangelo" time series): hourly bars for about the last ten days, keyed as
CURRENCY/US/XTUP/USDINR for a pair and FUTURE/US/XCME/LC00 for live cattle's
continuous contract. Each tick is the start of its bar in UTC milliseconds -
against the stored bars, no shift (2026-10-04) - and a bar with no price comes
as nulls.

It has every hour of the 17 pairs, 0-0.6 bp from the stored bars at the
median, and its quotes are its own; live cattle's continuous contract moves
with the stored front contract at 0.92 hour by hour (docs/decisions.md, "The
second source"). Its coffee is Yahoo's, one upstream, so not a second source
for the softs.

AND THE STANDING RISK. An undocumented endpoint that wants the access token
MarketWatch's own pages send with every chart: public, in their page, not a
secret of this project. Reading it from a script sits in the same grey area as
Google Finance's page (price_monitor/google.py). Should it change or close, the
second-source check gets nothing from here and scores those moves as before.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import requests

from price_monitor.models import Candle, ExchangeError

URL = "https://api.wsj.net/api/michelangelo/timeseries/history"
# The token MarketWatch's own chart pages send; public, not this project's.
TOKEN = "cecc4267a0194af89ca343805a3e57af"
CKEY = "cecc4267a0"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
           "Dylan2010.EntitlementToken": TOKEN}
HOUR = 3600
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def _query(key: str, frame: str) -> dict:
    return {"Step": "PT1H", "TimeFrame": frame, "EntitlementToken": TOKEN,
            "IncludeMockTick": False, "FilterNullSlots": True, "FilterClosedPoints": True,
            "IncludeClosedSlots": False, "IncludeOfficialClose": False, "InjectOpen": False,
            "ShowPreMarket": False, "ShowAfterHours": False, "UseExtendedTimeFrame": True,
            "WantPriorClose": False, "IncludeCurrentQuotes": False,
            "ResetTodaysAfterHoursPercentChange": False,
            "Series": [{"Key": key, "Dialect": "Charting", "Kind": "Ticker", "SeriesId": "s1",
                        "DataTypes": ["Open", "High", "Low", "Last"], "Indicators": []}]}


def parse(payload: dict, key: str, now: datetime | None = None) -> list[Candle]:
    """Hourly candles from one answer: ended bars with a price only."""
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    try:
        ticks = payload["TimeInfo"]["Ticks"]
        points = payload["Series"][0]["DataPoints"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ExchangeError(f"{key}: MarketWatch's answer has no series") from exc
    out = []
    for tick, point in zip(ticks, points):
        if not point or any(v is None for v in point[:4]):
            continue
        start = int(tick) // 1000
        if start % HOUR or start + HOUR > now_ts:
            continue                             # off the hour, or not ended yet
        o, h, low, c = (float(v) for v in point[:4])
        out.append(Candle(open_time=start, open=o, high=h, low=low, close=c, volume=0.0,
                          close_time=start + HOUR))
    return out


def fetch_hourly(key: str, session: requests.Session | None = None,
                 now: datetime | None = None, frame: str = "D10") -> list[Candle]:
    """The last ~ten days of `key`'s hourly bars that have ended."""
    params = {"json": json.dumps(_query(key, frame)), "ckey": CKEY}
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(URL, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException as exc:
            last = ExchangeError(f"{key}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = ExchangeError(f"{key}: MarketWatch answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{key}: MarketWatch answered {resp.status_code}")
        try:
            return parse(resp.json(), key, now)
        except ValueError as exc:
            raise ExchangeError(f"{key}: MarketWatch's answer is not JSON") from exc
    raise last or ExchangeError(f"{key}: no response")

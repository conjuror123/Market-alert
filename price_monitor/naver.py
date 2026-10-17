"""Naver's KOSPI: a voter on the KOSPI, minute by minute, folded to hours here.

THE ENDPOINT is the one Naver's stock pages chart from (api.stock.naver.com,
chart/domestic/index/KOSPI/minute): each minute's bar between two local times,
without a key. Each row is stamped with its minute in Seoul time (YYYYMMDDHHMMSS)
and carries that minute's open, high, low and price; the session's last row is
15:30, the closing auction. Folded onto the hour each minute starts in, it moves
with Yahoo's stored hours at 1.00 in every hour of the session, on 37 hours to
2026-10-12, and reaches about 12 days back (the source audit, docs/decisions.md).

AND THE STANDING RISK. An undocumented endpoint, read without a key as Naver's
pages read it. Should it change or close, it is an outage in the KOSPI's vote,
left out of the count: MarketWatch votes alone.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from price_monitor.models import Candle, ExchangeError, Unreachable

URL = "https://api.stock.naver.com/chart/domestic/index/{code}/minute"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SEOUL = ZoneInfo("Asia/Seoul")
HOUR = 3600
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def parse(rows: list, now: "datetime | None" = None) -> list[Candle]:
    """Hourly candles from the minute rows: each hour's first open, highest
    high, lowest low and last price, for the hours that have ended."""
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    hours: "dict[int, list[float]]" = {}
    for row in sorted(rows or [], key=lambda r: str(r.get("localDateTime"))):
        try:
            local = datetime.strptime(str(row["localDateTime"]), "%Y%m%d%H%M%S")
            o, h, low, c = (float(row[k]) for k in
                            ("openPrice", "highPrice", "lowPrice", "currentPrice"))
        except (KeyError, TypeError, ValueError):
            continue                             # no price that minute
        start = int(local.replace(tzinfo=SEOUL).timestamp()) // HOUR * HOUR
        bar = hours.get(start)
        if bar is None:
            hours[start] = [o, h, low, c]
        else:
            bar[1], bar[2], bar[3] = max(bar[1], h), min(bar[2], low), c
    return [Candle(open_time=t, open=o, high=h, low=low, close=c, volume=0.0,
                   close_time=t + HOUR)
            for t, (o, h, low, c) in sorted(hours.items()) if t + HOUR <= now_ts]


def fetch_hourly(code: str = "KOSPI", session: "requests.Session | None" = None,
                 now: "datetime | None" = None, days: float = 10.0) -> list[Candle]:
    """The index's hourly bars of about the last `days` that have ended."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(SEOUL)
    params = {"startDateTime": (local - timedelta(days=max(days, 1.0))).strftime("%Y%m%d0000"),
              "endDateTime": local.strftime("%Y%m%d%H%M")}
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(URL.format(code=code), params=params,
                                             headers=HEADERS, timeout=30)
        except requests.RequestException as exc:
            last = Unreachable(f"{code}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = (Unreachable if resp.status_code >= 500 else ExchangeError)(
                f"{code}: Naver answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{code}: Naver answered {resp.status_code}")
        try:
            return parse(resp.json(), now)
        except ValueError as exc:
            raise ExchangeError(f"{code}: Naver's answer is not JSON") from exc
    raise last or ExchangeError(f"{code}: no response")

"""EEX's own Dutch TTF gas future, its trades folded into hourly bars: a voter on
TTF.

THE ENDPOINT is the one the market-data widget on EEX's site reads
(api.eex-group.com/pub/market-data, chart/intraday): one day's trades of one
contract - the physical TTF month future (shortCode G3BM) of a delivery month
(maturity 202611) - each a time in UTC to the nanosecond and its price, in euros
a megawatt hour. One request a day of the window, trading days only. EEX's
contract is not ICE Endex's, which the store holds through MarketWatch: the same
gas and month on another exchange, about 169 trades an hour at the median
(2026-09-30 to 10-16).

Against the stored bars, 2026-09-30 to 10-16 on the November contract: hourly
moves correlated 0.964 over 117 hours, all 5 of the store's big moves confirmed,
no hour worse than 0.88 (docs/decisions.md, "Every source on every instrument").

AND THE STANDING RISK. An endpoint behind EEX's public pages, read without a key.
Should it change or close, it is an outage in TTF's vote, left out of the count:
TTF has no other voter, and its moves are uncertain again.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests

from price_monitor.models import Candle, ExchangeError, Unreachable

URL = "https://api.eex-group.com/pub/market-data/chart/intraday"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
           "Origin": "https://www.eex.com", "Referer": "https://www.eex.com/"}
CONTRACT = {"commodity": "NATGAS", "pricing": "F", "area": "TTF", "product": "Physical",
            "shortCode": "G3BM"}
HOUR = 3600
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def parse(payload: dict, maturity: str) -> "list[tuple[int, float]]":
    """One day's trades from one answer: (UTC seconds, price), in time order."""
    try:
        series = {s["serieName"]: s["timeAndValue"] for s in payload["series"]}
    except (KeyError, TypeError) as exc:
        raise ExchangeError(f"TTF {maturity}: EEX's answer has no series") from exc
    out = []
    for item in series.get("lastPx") or ():
        try:
            stamp, price = str(item[0]), float(item[1])
            when = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except (IndexError, TypeError, ValueError):
            continue                             # no price on that line
        out.append((int(when.timestamp()), price))
    return sorted(out)


def hourly(trades: "list[tuple[int, float]]", now: datetime | None = None) -> list[Candle]:
    """Trades folded into the hours that have ended: the first trade's price
    opens the hour, the last closes it."""
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    hours: "dict[int, list[float]]" = {}
    for t, price in sorted(trades):
        start = t // HOUR * HOUR
        bar = hours.get(start)
        if bar is None:
            hours[start] = [price, price, price, price]
        else:
            bar[1], bar[2], bar[3] = max(bar[1], price), min(bar[2], price), price
    return [Candle(open_time=t, open=o, high=h, low=low, close=c, volume=0.0,
                   close_time=t + HOUR)
            for t, (o, h, low, c) in sorted(hours.items()) if t + HOUR <= now_ts]


def _day(maturity: str, day: str, session) -> "list[tuple[int, float]]":
    params = {**CONTRACT, "maturity": maturity, "startDate": day}
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(URL, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException as exc:
            last = Unreachable(f"TTF {maturity}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = (Unreachable if resp.status_code >= 500 else ExchangeError)(
                f"TTF {maturity}: EEX answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"TTF {maturity}: EEX answered {resp.status_code}")
        try:
            return parse(resp.json(), maturity)
        except ValueError as exc:
            raise ExchangeError(f"TTF {maturity}: EEX's answer is not JSON") from exc
    raise last or ExchangeError(f"TTF {maturity}: no response")


def fetch_hourly(maturity: str, session: requests.Session | None = None,
                 now: datetime | None = None, days: float = 9.0) -> list[Candle]:
    """The delivery month's hourly bars of about the last `days` that have
    ended: one request for each weekday."""
    now = now or datetime.now(timezone.utc)
    day = (now - timedelta(days=max(days, 1.0))).date()
    trades: "list[tuple[int, float]]" = []
    while day <= now.date():
        if day.weekday() < 5:
            trades += _day(maturity, day.isoformat(), session)
        day += timedelta(days=1)
    return hourly(trades, now)

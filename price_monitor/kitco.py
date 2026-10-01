"""Kitco's chart gateway, used for nickel: the one hourly nickel series found.

WHAT IT IS. kitco.com's own charts ask a GraphQL gateway (`GetMetalHistoryV3`)
that takes no key. It serves five-minute quotes from 2020-11 to the last five
minutes, in US dollars a pound. Its level matches the LME's official price to
a ratio of 1.000 in every year from 2020 to 2026, and it moves on the LME's
hours - 01:00 to 19:00 London on weekdays - and is flat outside them.

WHAT A BAR IS. A five-minute row carries a bid and an ask (and the day's
running high and low, which are not the bar's); the mid is the price. The rows
come back here as five-minute candles of that one price, converted to dollars
a tonne, the unit nickel is quoted in, and tremor.bars folds them into hours
like any finer grid.

DAYS THAT NEVER MOVE ARE NOT DAYS. Kitco keeps writing a quote when nothing
trades: through LME holidays, through the LME's suspension of nickel from 8 to
15 March 2022, and through its own outages (eight days from 2023-12-18). A
London day whose session price never changes is dropped whole, so a holiday is
a closure and the move across it a gap, not a run of zero-return hours.

AND THE GLITCHES. Five times in the history the quote jumped 18 to 86% in one
or two five-minute steps, sat there for minutes to hours, and came back to
where it was: 2022-04-04, 2023-01-23 and 24, 2023-02-27, 2023-07-24 and
2024-09-25. Real steps that size are rare and do not come back - the squeeze
of March 2022 moved 8% at most in five minutes, and 2022-03-21, Kitco catching
up with three limit-down days, fell 22% and stayed - so a step past
GLITCH_STEP that comes more than halfway back within GLITCH_HOURS is dropped
from the step to the return. A step that has not come back yet, and is not yet
GLITCH_HOURS old, is held back: the hours after it are not served until it
either returns (a glitch, dropped) or stays (a market, served then). The cost is
that a real 15% five-minute move would reach the store a day late, and so never
go out; there has not been one.

AND THE STANDING RISK. This is a website's private gateway, not an API: no
terms that allow it, no notice when it changes. A response without the series
raises, so the run's provider report names it.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from price_monitor.models import Candle, ExchangeError

log = logging.getLogger("price_monitor.kitco")

URL = "https://kdb-gw.prod.kitco.com"
HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://www.kitco.com",
           "Referer": "https://www.kitco.com/"}
QUERY = """query MetalHistory($symbol: String!, $startTime: Int!, $endTime: Int!,
  $groupBy: String, $limit: Int, $offset: Int, $currency: String!) {
  GetMetalHistoryV3(symbol: $symbol, startTime: $startTime, endTime: $endTime,
    groupBy: $groupBy, limit: $limit, offset: $offset, currency: $currency) {
    results { bid ask timestamp } } }"""

LONDON = ZoneInfo("Europe/London")
SESSION_HOURS = (1, 19)          # LMEselect, London time, weekdays
POUNDS_PER_TONNE = 2204.62262
BAR_SECONDS = 300
# Five days of five-minute rows a request: 1,440 rows. Fifteen timed out.
WINDOW_DAYS = 5
FIRST = datetime(2020, 11, 1, tzinfo=timezone.utc)

GLITCH_STEP = 0.15               # log move in one five-minute step
GLITCH_RETURN = 0.03             # back at least this close to where it left
GLITCH_HOURS = 24

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def _ask(session, symbol: str, start: int, end: int) -> list[dict]:
    payload = {"query": QUERY, "variables": {
        "symbol": symbol, "startTime": start, "endTime": end, "groupBy": "5m",
        "limit": 5000, "offset": 0, "currency": "USD"}}
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).post(URL, json=payload, headers=HEADERS, timeout=60)
        except requests.RequestException as exc:
            last = ExchangeError(f"{symbol}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = ExchangeError(f"{symbol}: Kitco answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{symbol}: Kitco answered {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            last = ExchangeError(f"{symbol}: Kitco's answer was not JSON")
            continue
        series = (body.get("data") or {}).get("GetMetalHistoryV3")
        if series is None:
            raise ExchangeError(f"{symbol}: Kitco returned no series: "
                                f"{str(body.get('errors'))[:200]}")
        return series.get("results") or []
    raise last or ExchangeError(f"{symbol}: no response")


def in_session(stamp: int) -> bool:
    local = datetime.fromtimestamp(stamp, LONDON)
    return local.weekday() < 5 and SESSION_HOURS[0] <= local.hour < SESSION_HOURS[1]


def drop_still_days(quotes: "list[tuple[int, float]]") -> "list[tuple[int, float]]":
    """The quotes of every London day whose session price changed at least once."""
    days: dict = {}
    for stamp, price in quotes:
        if in_session(stamp):
            days.setdefault(datetime.fromtimestamp(stamp, LONDON).date(), set()).add(price)
    moving = {day for day, prices in days.items() if len(prices) > 1}
    return [(s, p) for s, p in quotes
            if datetime.fromtimestamp(s, LONDON).date() in moving]


def drop_glitches(quotes: "list[tuple[int, float]]", now: int
                  ) -> "list[tuple[int, float]]":
    """The quotes with every glitch taken out, and everything from a step that
    is still undecided held back (see the module docstring)."""
    out: list[tuple[int, float]] = []
    i = 0
    while i < len(quotes):
        stamp, price = quotes[i]
        if not out or abs(math.log(price / out[-1][1])) <= GLITCH_STEP:
            out.append(quotes[i])
            i += 1
            continue
        left = out[-1][1]
        horizon = stamp + GLITCH_HOURS * 3600
        # Back means more than halfway back. The market moves on while the
        # quote is stuck - 2023-01-23 left at 12.42 $/lb and came back at 12.86
        # six hours later - so "where it left" cannot be read tightly.
        band = max(GLITCH_RETURN, abs(math.log(price / left)) / 2)
        back = next((j for j in range(i + 1, len(quotes))
                     if quotes[j][0] <= horizon
                     and abs(math.log(quotes[j][1] / left)) <= band), None)
        if back is not None:
            log.warning("Kitco glitch dropped: %s to %s, %.0f%% away",
                        datetime.fromtimestamp(stamp, timezone.utc),
                        datetime.fromtimestamp(quotes[back][0], timezone.utc),
                        100 * (price / left - 1))
            i = back
            continue
        if now < horizon:
            log.warning("Kitco step of %.0f%% at %s held back until it returns or "
                        "stays for %d hours", 100 * (price / left - 1),
                        datetime.fromtimestamp(stamp, timezone.utc), GLITCH_HOURS)
            break
        out.append(quotes[i])
        i += 1
    return out


def fetch_full_history(symbol: str, interval: str, days: float,
                       session: requests.Session | None = None,
                       end: datetime | None = None) -> list[Candle]:
    """Five-minute candles of the mid, in dollars a tonne, for `days` ending at
    `end`. A day of quotes before the window is read too, so a glitch that
    began just before it is still recognised."""
    now = datetime.now(timezone.utc)
    end = end or now
    start = max(FIRST, end - timedelta(days=max(days, 1 / 24) + GLITCH_HOURS / 24))
    rows: dict[int, float] = {}
    t = start
    while t < end:
        stop = min(t + timedelta(days=WINDOW_DAYS), end)
        for r in _ask(session, symbol, int(t.timestamp()), int(stop.timestamp())):
            if r.get("bid") and r.get("ask"):
                rows[int(r["timestamp"])] = (float(r["bid"]) + float(r["ask"])) / 2
        t = stop
    quotes = sorted((s, p * POUNDS_PER_TONNE) for s, p in rows.items() if p > 0)
    quotes = drop_glitches(drop_still_days(quotes), int(now.timestamp()))
    return [Candle(open_time=s, open=p, high=p, low=p, close=p, volume=0.0,
                   close_time=s + BAR_SECONDS) for s, p in quotes]

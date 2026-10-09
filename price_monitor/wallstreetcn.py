"""Wallstreetcn's hourly bars: the LME's metals' voter, and their history from 2026-04.

THE ENDPOINT is the one Wallstreetcn's own charts read (api-ddc-wscn.awtmt.com,
market/kline): hourly bars of the LME's three-month tin (UKSN.OTC), nickel
(UKNI.OTC) and aluminium (UKAH.OTC), in dollars a tonne, without volume.
`tick_count` asks for about that many hours back, and at most MAX_TICKS are
served - back to 2026-04-22 from 2026-10-09; more is answered with one bar. Each
line is [open, close, high, low, start in UTC seconds]: against the stored bars,
no shift (2026-10-09).

Against Sina's stored bars, 2026-07 to 10, the closes are identical in 53-68% of
hours and 0 bp apart at the median, hourly moves correlated 0.96-0.99: in part
one upstream, its own print in the rest (docs/decisions.md, "Wallstreetcn for
the LME"). It answers a GitHub runner as it answers here.

ITS OPENINGS BEFORE MID-JULY. In its bars of 2026-04 to 07, below Sina's, eleven
sessions open with a print 1% or more off the last close that is gone within the
hour - 8 of aluminium's 59 first bars, 3 of tin's 54, none of nickel's 59; none
since, in its bars or Sina's. The history is taken without them (jump.backfill,
deepen_from_wallstreetcn).

AND THE STANDING RISK. An undocumented endpoint, read without a key, as
Wallstreetcn's page reads it. Should it change or close, it is an outage in
every vote it is in, a vote against: the LME's moves, with no other source,
would all be ties.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import requests

from price_monitor.models import Candle, ExchangeError, Unreachable

URL = "https://api-ddc-wscn.awtmt.com/market/kline"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
# The LME's metals as the store names them, and as Wallstreetcn does.
CODES = {"SND": "UKSN.OTC", "NID": "UKNI.OTC", "AHD": "UKAH.OTC"}
# The most one answer serves (measured 2026-10-09: 4,096 and 4,100 bring the
# same 2,046 bars, 4,500 one).
MAX_TICKS = 4096
HOUR = 3600
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def parse(payload: dict, code: str, now: datetime | None = None) -> list[Candle]:
    """Hourly candles from one answer: ended bars only."""
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    try:
        lines = payload["data"]["candle"][code]["lines"]
    except (KeyError, TypeError) as exc:
        raise ExchangeError(f"{code}: Wallstreetcn's answer has no bars") from exc
    out = []
    for line in lines or ():
        o, c, h, low, start = line[:5]
        start = int(start)
        if start % HOUR or start + HOUR > now_ts:
            continue                             # off the hour, or not ended yet
        out.append(Candle(open_time=start, open=float(o), high=float(h), low=float(low),
                          close=float(c), volume=0.0, close_time=start + HOUR))
    return out


def fetch_hourly(code: str, session: requests.Session | None = None,
                 now: datetime | None = None, days: float = 10.0) -> list[Candle]:
    """`code`'s hourly bars of about the last `days` that have ended."""
    ticks = min(MAX_TICKS, math.ceil(days * 24) + 24)
    params = {"prod_code": code, "tick_count": ticks, "period_type": HOUR,
              "fields": "tick_at,open_px,close_px,high_px,low_px"}
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(URL, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException as exc:
            last = Unreachable(f"{code}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = (Unreachable if resp.status_code >= 500 else ExchangeError)(
                f"{code}: Wallstreetcn answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{code}: Wallstreetcn answered {resp.status_code}")
        try:
            return parse(resp.json(), code, now)
        except ValueError as exc:
            raise ExchangeError(f"{code}: Wallstreetcn's answer is not JSON") from exc
    raise last or ExchangeError(f"{code}: no response")

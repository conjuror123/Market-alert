"""Wallstreetcn's hourly bars: a voter on the LME's metals, cocoa, cotton, the
pairs and four stock indices abroad, and the LME's history from 2026-04.

THE ENDPOINT is the one Wallstreetcn's own charts read (api-ddc-wscn.awtmt.com,
market/kline): hourly bars without volume. The LME's three-month tin
(UKSN.OTC), nickel (UKNI.OTC), aluminium (UKAH.OTC) and copper (UKCA.OTC, for
the basket's stage 1) in dollars a tonne; ICE's
cocoa (USCC.OTC) and cotton (USCT.OTC), continuous futures that change contract
on their own days; and the currency pairs as their six letters (EURUSD.OTC). `tick_count` asks for about that many
hours back from its latest bar, and at most MAX_TICKS are served - back to
2026-04-22 from 2026-10-09, for each of them; more is answered with one bar.
Each line holds the bar's prices and its start in UTC seconds, in the order the
answer's `fields` names - its own, whatever order they are asked in (2026-10-10:
open, close, high, low, start). Against the stored bars, no shift (2026-10-09).

IT CARRIES MORE than the basket uses (its rank lists, 2026-10-10): sugar
(USYO.OTC), lean hogs (LHC.OTC), wheat, corn, soybeans and soybean oil
(USZW, USZC, USZS, USZL), the LME's lead and zinc, gold, silver,
platinum, palladium, WTI and Brent crude and natural gas; 65 pairs and the
dollar index; 38 stock indices and index futures (US500.OTC, JP225.OTC,
DE30.OTC, VIX.OTC, ...); 52 government bond yields (US10YR.OTC, DE10YR.OTC,
JP10YR.OTC, ...). Not coffee, live cattle, the US funds or the coins.

Its LME bars against Sina's stored ones, 2026-07 to 10: the closes identical in
53-68% of hours and 0 bp apart at the median, hourly moves correlated 0.96-0.99 -
in part one upstream, its own print in the rest (docs/decisions.md,
"Wallstreetcn for the LME"). Its cocoa, cotton and pairs are its own quotes,
at most 2% of their closes the store's (docs/decisions.md, "Wallstreetcn on
cocoa, cotton and the pairs"). It answers a GitHub runner as it answers here.

ITS OPENINGS BEFORE MID-JULY. In its bars of 2026-04 to 07, below Sina's, eleven
sessions open with a print 1% or more off the last close that is gone within the
hour - 8 of aluminium's 59 first bars, 3 of tin's 54, none of nickel's 59; none
since, in its bars or Sina's. The history is taken without them (jump.backfill,
deepen_from_wallstreetcn).

AND THE STANDING RISK. An undocumented endpoint, read without a key, as
Wallstreetcn's page reads it. Should it change or close, it is an outage in
every vote it is in, left out of the count: the LME's far moves, with no other
source, would all be uncertain; cocoa and cotton are voted by Sina alone
again, the pairs by Yahoo and MarketWatch.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import requests

from price_monitor.models import Candle, ExchangeError, Unreachable

URL = "https://api-ddc-wscn.awtmt.com/market/kline"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
# The store's futures as Wallstreetcn names them: the LME's metals, and the
# softs it carries (not coffee).
LME = {"SND": "UKSN.OTC", "NID": "UKNI.OTC", "AHD": "UKAH.OTC", "CAD": "UKCA.OTC"}
SOFTS = {"CC=F": "USCC.OTC", "CT=F": "USCT.OTC"}
# The stock indices abroad it carries as cash indices, for the basket's stage 1,
# each moving with Yahoo's folded bars at 0.90 or more hour by hour (2026-08 to
# 10): Saudi Arabia's 0.99, Canada's 0.95; India's is the Sensex, not the Nifty,
# at 0.91. Below 0.90, left out: the ASX 200 (0.80), the KOSPI (0.89, 0.84 in the
# source audit), the Euro Stoxx 50 (0.86); its index futures (HK40F, UK100F)
# change contract on their own days, and MarketWatch carries both indices. The
# Nikkei (JP225.OTC, 0.92 then) was dropped on 2026-10-13: 0.67 in the source
# audit, its last bar 3.5 days old (the user's).
INDICES = {"^TASI.SR": "TASI.OTC", "^GSPTSE": "SPTSX.OTC", "^NSEI": "SENSEX.OTC"}
# Its continuous futures for two more (the source audit, 2026-10-13): ICE Brent
# for Brent, 0.987 hour by hour with the stored contract; NYMEX natural gas for
# the natural gas fund, 0.985 with UNG's own hours. Each changes contract on its
# own days.
FUTURES = {"BZ=F": "UKOIL.OTC", "UNG": "USNG.OTC"}
# The most one answer serves (measured 2026-10-09: 4,096 and 4,100 bring the
# same 2,046 bars, 4,500 one).
MAX_TICKS = 4096
HOUR = 3600
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def pair_code(ticker: str) -> str:
    """A currency pair as Wallstreetcn names it (EUR/USD: EURUSD.OTC)."""
    return ticker.replace("/", "").upper() + ".OTC"


FIELDS = ("open_px", "close_px", "high_px", "low_px", "tick_at")


def parse(payload: dict, code: str, now: datetime | None = None) -> list[Candle]:
    """Hourly candles from one answer: ended bars with every price only. Each
    value is read by the name the answer gives its column."""
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    try:
        lines = payload["data"]["candle"][code]["lines"]
        where = [payload["data"]["fields"].index(name) for name in FIELDS]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ExchangeError(f"{code}: Wallstreetcn's answer has no bars") from exc
    out = []
    for line in lines or ():
        try:
            o, c, h, low, start = (line[i] for i in where)
        except (IndexError, TypeError):
            continue
        if any(v is None for v in (o, c, h, low, start)):
            continue                             # no price that hour
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
              "fields": ",".join(FIELDS)}
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

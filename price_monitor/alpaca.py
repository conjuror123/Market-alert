"""Alpaca's bars: the consolidated tape (SIP) from 2016 for history and the vote,
and the IEX exchange's bars for the hour.

LIVE: IEX. The free plan's IEX bars are current - measured 2026-10-01 at 15:05
UTC, the half-hour bar opened at 15:00 was served, minute bars to 15:04 - and
on the 30 funds whose IEX price agrees with the tape (measured against it:
median <= 2 bp, p90 <= 5, at most 2% of hours missing) that is the same price.
Tiingo's intraday feed is IEX too; this carries the IEX-safe funds Tiingo has
no hourly room for. A fund IEX does not price like the tape belongs on a
consolidated feed instead (Twelve Data, Sina or Yahoo).

HISTORY: SIP. The free plan serves SIP bars only
to fifteen minutes back, so it cannot be the live source, but below that it is
the consolidated tape itself - the line every live feed was held to - from
2016-01-01, for every US-listed fund in the
basket. Twelve Data's intraday archive stops at 2020-02 and HF Data did not
carry about sixty of the funds, so this is what takes those back to 2016. The
same tape votes on every fund's far moves, once it serves the move's hour
whole (jump.verify).

ADJUSTMENT. `split`: split-adjusted, dividends left in, which is the store's
convention (jump/returns.py takes each payout out of its overnight gap). The
deepening that uses this still checks the levels against the stored bars over
an overlap and refuses a series that disagrees on price.

BARS. Thirty-minute bars stamped at their start, in UTC, including pre- and
post-market. Only the regular session is kept: a 09:00 bar holds pre-market
trades and, folded with the 09:30 one, would put a pre-market price in the
opening hour's open.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from price_monitor.models import Candle, ExchangeError, KeyRefused, Unreachable

BASE_URL = "https://data.alpaca.markets/v2"
FIRST = datetime(2016, 1, 1, tzinfo=timezone.utc)
NY = ZoneInfo("America/New_York")
BAR_SECONDS = 1800
PAGE_LIMIT = 10000
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


class RateLimited(ExchangeError):
    """429 after every retry: 200 requests a minute per key, shared with any
    deepening run. The run stops asking Alpaca rather than spend a retry cycle on each
    remaining fund."""


def headers(key_id: str, secret: str) -> dict:
    return {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}


def regular_session(stamp: int) -> bool:
    """A half-hour bar opening inside 09:30-16:00 New York on a weekday. A half
    session's afternoon passes this and is dropped later by the session gate,
    which knows the calendar."""
    local = datetime.fromtimestamp(stamp, NY)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 570 <= minutes < 960


def _get(session, url: str, params: dict, auth: dict, symbol: str) -> dict:
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(url, params=params, headers=auth, timeout=60)
        except requests.RequestException as exc:
            last = Unreachable(f"{symbol}: {exc}")
            continue
        if resp.status_code == 429:
            last = RateLimited(f"{symbol}: Alpaca answered 429")
            continue
        if resp.status_code in (500, 502, 503, 504):
            last = Unreachable(f"{symbol}: Alpaca answered {resp.status_code}")
            continue
        if resp.status_code in (401, 403):
            raise KeyRefused(f"{symbol}: Alpaca refused the key ({resp.status_code})")
        if resp.status_code != 200:
            raise ExchangeError(f"{symbol}: Alpaca answered {resp.status_code}: "
                                f"{resp.text[:200]}")
        return resp.json()
    raise last or ExchangeError(f"{symbol}: no response")


def fetch_history(symbol: str, start: datetime, end: datetime, auth: dict,
                  session: requests.Session | None = None,
                  base_url: str = BASE_URL, feed: str = "sip") -> list[Candle]:
    """Regular-session thirty-minute bars from `start` to `end`, paged. `sip`
    for history (it refuses an end less than 15 minutes ago), `iex` live."""
    start = max(start, FIRST)
    params = {"symbols": symbol, "timeframe": "30Min", "feed": feed,
              "adjustment": "split", "limit": PAGE_LIMIT,
              "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
    out: list[Candle] = []
    while True:
        payload = _get(session, f"{base_url}/stocks/bars", params, auth, symbol)
        for row in (payload.get("bars") or {}).get(symbol) or []:
            t = int(datetime.fromisoformat(row["t"].replace("Z", "+00:00")).timestamp())
            if regular_session(t):
                out.append(Candle(open_time=t, open=float(row["o"]), high=float(row["h"]),
                                  low=float(row["l"]), close=float(row["c"]),
                                  volume=float(row.get("v") or 0.0),
                                  close_time=t + BAR_SECONDS))
        token = payload.get("next_page_token")
        if not token:
            return out
        params["page_token"] = token

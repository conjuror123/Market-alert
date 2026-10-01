"""Google Finance's quote page, used for the one fund no other live feed carries:
TUR.

WHY TUR. Every other feed was held to the consolidated tape and missed it
(docs/concerns-for-later.md): IEX sees a sliver of its trades, and Yahoo's and
Twelve Data's hourly closes stray from the tape by p90 5.6 bp. Google's page did
not - on 2026-09-30, against Alpaca's SIP minute bars, 72 of 77 five-minute
closes identical and the hourly closes 0.0 / 0.0 bp (median / p90), no hour
missing. Measured with tools/google_probe.py.

AND WHY NOT MORE. Google sees exchange trades, not off-exchange ones: 91% of
TUR's volume, but 19% of RWX's and 22% of SPY's. On a fund that trades mostly
off-exchange it has no bar for some hours - one in five of RWX's that day -
where Yahoo, which carries the whole tape, lacks 3%. So RWX stays on Yahoo, and
a fund belongs here only when nothing consolidated agrees with the tape on it.

WHAT THE PAGE CARRIES. The quote page embeds the latest session's five-minute
bars, only for the five minutes a trade fell in, as rows of
[open, close, high, low, stamp, volume]. The stamp is the bar's END: the 16:00
row is the close, and its close matches the tape's last trade of 15:55-16:00.
The 09:30 row alone is the opening print, an instant rather than a span. So a
bar is moved back five minutes to its start, except the opening print, which
stays at 09:30 - otherwise it would land in pre-market and out of the session.

ONE SESSION, NO MORE. The page holds the latest session and nothing earlier,
whatever `days` asks for. The hourly run reads it every hour, and each read
brings the whole session so far, so a missed run heals on the next one; a
session no run read at all is a gap for fill-gaps, which asks Twelve Data.

AND THE STANDING RISK. This is a web page, not an API: undocumented, unversioned
and not meant to be read by a program, which Google's terms forbid. The owner
accepted that for a private channel (2026-10-01). Its shape can change without
notice; a page that no longer carries the quote raises, so the run's provider
report names it rather than the store going quietly stale.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from price_monitor.models import Candle, ExchangeError

log = logging.getLogger("price_monitor.google")

BASE_URL = "https://www.google.com/finance/quote"
USER_AGENT = "Mozilla/5.0"
NY = ZoneInfo("America/New_York")
BAR_SECONDS = 300

# Google's page is addressed by ticker AND listing exchange, which the basket
# does not hold. One fund; another would be one more entry here.
EXCHANGES = {"TUR": "NASDAQ"}

# Retried like every client on the hourly path: two seconds, then four.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0

_BLOCK = re.compile(r"AF_initDataCallback\(\{key: 'ds:\d+'.*?data:(.*?), sideChannel", re.S)
_ROW = re.compile(r'\[([\d.]+),([\d.]+),([\d.]+),([\d.]+),"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d'
                  r'[-+]\d\d:\d\d)",(\d+)\]')


def parse(page: str, symbol: str, exchange: str) -> list[Candle]:
    """The session's five-minute bars on the page, stamped at their start and
    kept inside the regular session.

    The intraday rows are told from the month's daily rows, which have the same
    shape, by all falling on one date. A page with the quote but no intraday
    rows is a session no trade has reached yet: nothing, not an error."""
    listing = f'["{symbol}","{exchange}"]'
    if listing not in page:
        raise ExchangeError(f"{symbol}: the page no longer carries {symbol}:{exchange}")
    rows = []
    for block in _BLOCK.findall(page):
        if listing not in block:
            continue
        found = _ROW.findall(block)
        if found and len({r[4][:10] for r in found}) == 1:
            rows = found
            break
    candles: list[Candle] = []
    for o, c, h, lo, stamp, v in rows:
        end = datetime.fromisoformat(stamp).astimezone(NY)
        opening = end.replace(hour=9, minute=30, second=0, microsecond=0)
        closing = end.replace(hour=16, minute=0, second=0, microsecond=0)
        if end < opening or end > closing:
            continue
        start = end if end == opening else end - timedelta(seconds=BAR_SECONDS)
        t = int(start.timestamp())
        candles.append(Candle(open_time=t, open=float(o), high=float(h), low=float(lo),
                              close=float(c), volume=float(v),
                              close_time=t + BAR_SECONDS))
    return candles


def fetch_full_history(symbol: str, interval: str, days: float,
                       base_url: str = BASE_URL, session: requests.Session | None = None,
                       end: datetime | None = None) -> list[Candle]:
    """The latest session's bars, whatever `days` and `end` ask: the page holds
    nothing else. `interval` is the store's (30min for a fund); the five-minute
    bars fold into its hours like any finer grid."""
    exchange = EXCHANGES.get(symbol)
    if exchange is None:
        raise ExchangeError(f"{symbol}: no Google listing exchange configured "
                            f"(price_monitor/google.py EXCHANGES)")
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(
                f"{base_url}/{symbol}:{exchange}", timeout=30,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US"})
        except requests.RequestException as exc:
            last = ExchangeError(f"{symbol}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = ExchangeError(f"{symbol}: Google answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{symbol}: Google answered {resp.status_code}")
        break
    else:
        raise last or ExchangeError(f"{symbol}: no response")
    candles = parse(resp.text, symbol, exchange)
    if not candles:
        log.info("%s: no trade on Google's page yet this session", symbol)
    return candles

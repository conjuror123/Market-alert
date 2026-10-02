"""Sina Finance's futures bars, used for tin: the Shanghai Futures Exchange's.

WHY SHANGHAI. No source found carries LME tin by the hour - Kitco has no tin,
the LME and Investing.com refuse readers, and Sina's own LME tin quote (hf_SND)
prints a couple of times a day. Shanghai's tin trades 84,000 lots a day, and on
Sina its contracts serve hourly bars back to 2019-07 with no key. It is priced
in yuan a tonne with VAT, so it moves with the yuan and China's market as well
as with tin; it is labelled for what it is, Tin (Shanghai).

WHAT IS ASKED. InnerFuturesNewService.getFewMinLine, type 60: the last 1,023
hourly bars of one symbol. SN0 is Sina's continuous main contract, used live;
a delivery month (SN2611) serves its own last 1,023, which is how the history
is built (tools/sina_history.py). Tin's curve is flat - neighbouring contracts
sit 0-5 bp apart - so the move from one contract to the next is no move.

THE BARS are labelled by their END, in Beijing time, on the exchange's own grid
(22:00, 23:00, 00:00, 01:00 at night; 10:00, 11:15, 14:15, 15:00 by day, the
first day bar from 09:00 and the 11:15 one across the 10:15 break). Each is
stamped at the hour its first minute falls in - its label less an hour,
floored - so the day's bars are 09, 10, 13 and 14 and the lunch hours are
missing ones. A bar whose label is still ahead of now has not ended, and is
left for the next run.

AND THE STANDING RISK. An undocumented endpoint that wants a Referer, with the
answer wrapped in a JavaScript callback. A response without it raises.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from price_monitor.models import Candle, ExchangeError

URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
       "InnerFuturesNewService.getFewMinLine")
HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
BEIJING = ZoneInfo("Asia/Shanghai")
_PAYLOAD = re.compile(r"var t=\((.*)\);", re.S)
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def parse(text: str, symbol: str, now: datetime | None = None) -> list[Candle]:
    """Hourly candles from one answer, ended bars only."""
    match = _PAYLOAD.search(text)
    if match is None:
        raise ExchangeError(f"{symbol}: Sina's answer has no bars")
    rows = json.loads(match.group(1)) or []
    now = now or datetime.now(timezone.utc)
    out = []
    for row in rows:
        end = datetime.strptime(row["d"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING)
        if end > now:
            continue
        start = (end - timedelta(hours=1)).replace(minute=0, second=0)
        t = int(start.timestamp())
        out.append(Candle(open_time=t, open=float(row["o"]), high=float(row["h"]),
                          low=float(row["l"]), close=float(row["c"]),
                          volume=float(row.get("v") or 0.0), close_time=int(end.timestamp())))
    return out


def fetch_bars(symbol: str, session: requests.Session | None = None,
               now: datetime | None = None) -> list[Candle]:
    """The last 1,023 hourly bars of `symbol` that have ended."""
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(URL, params={"symbol": symbol, "type": 60},
                                             headers=HEADERS, timeout=30)
        except requests.RequestException as exc:
            last = ExchangeError(f"{symbol}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = ExchangeError(f"{symbol}: Sina answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{symbol}: Sina answered {resp.status_code}")
        return parse(resp.text, symbol, now)       # [] for a symbol it does not hold
    raise last or ExchangeError(f"{symbol}: no response")

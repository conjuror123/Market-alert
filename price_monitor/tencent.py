"""Tencent's half-hour bars of China's indices: a voter on the CSI 300.

THE ENDPOINT is the one Tencent's own charts read (ifzq.gtimg.cn, kline/mkline):
an A-share code's 30-minute bars (sh000300 for the CSI 300), newest last, each
line its END in Beijing time (YYYYMMDDHHMM), open, close, high, low and volume.
At most MAX_BARS in one answer - more is answered with the default 320 - back to
2026-05-21 from 2026-10-16, about 148 days. Its hourly bars run 09:30-10:30, a
grid the store's round UTC hours do not share; the half hours fold onto them.

Against the stored bars (Yahoo's, folded), 2026-08-17 to 10-16, the whole stored
record: hourly moves correlated 0.991 over 117 hours, all 24 of the store's big
moves confirmed, none of its hours worse than 0.99 (docs/decisions.md, "Every
source on every instrument"). Hong Kong's and other markets' codes are refused
here ("param error"): the A shares only.

AND THE STANDING RISK. An undocumented endpoint, read without a key, as
Tencent's page reads it. Should it change or close, it is an outage in the CSI
300's vote, left out of the count: Sina votes alone again.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from price_monitor.models import Candle, ExchangeError, Unreachable

URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
BEIJING = ZoneInfo("Asia/Shanghai")
# The most one answer serves (measured 2026-10-17: 800 brings 800, 801 and more
# the default 320).
MAX_BARS = 800
HALF_HOUR = 1800
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def parse(payload: dict, code: str, now: datetime | None = None) -> list[Candle]:
    """Half-hour candles from one answer: ended bars with every price only,
    each stamped by its start in UTC."""
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    try:
        lines = payload["data"][code]["m30"]
    except (KeyError, TypeError) as exc:
        raise ExchangeError(f"{code}: Tencent's answer has no bars") from exc
    out = []
    for line in lines or ():
        try:
            end = datetime.strptime(str(line[0]), "%Y%m%d%H%M").replace(tzinfo=BEIJING)
            o, c, h, low = (float(line[i]) for i in (1, 2, 3, 4))
            volume = float(line[5])
        except (IndexError, TypeError, ValueError):
            continue                             # no price that half hour
        close_time = int(end.timestamp())
        if close_time > now_ts:
            continue                             # not ended yet
        start = int((end - timedelta(seconds=HALF_HOUR)).timestamp())
        out.append(Candle(open_time=start, open=o, high=h, low=low, close=c,
                          volume=volume, close_time=close_time))
    return out


def fetch_bars(code: str, session: requests.Session | None = None,
               now: datetime | None = None) -> list[Candle]:
    """`code`'s last MAX_BARS half-hour bars that have ended (about 148 days)."""
    params = {"param": f"{code},m30,,{MAX_BARS}"}
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
                f"{code}: Tencent answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{code}: Tencent answered {resp.status_code}")
        try:
            return parse(resp.json(), code, now)
        except ValueError as exc:
            raise ExchangeError(f"{code}: Tencent's answer is not JSON") from exc
    raise last or ExchangeError(f"{code}: no response")

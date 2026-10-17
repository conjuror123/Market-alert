"""The Taiwan exchange's own TAIEX: a voter on the TAIEX, every five seconds of a
day, folded to hours here.

THE ENDPOINT is the exchange's daily report of its indices every five seconds
(www.twse.com.tw, exchangeReport/MI_5MINS_INDEX), one trading day a request,
without a key: rows of a Taipei time (09:00:00 to the 13:30:00 close) and the
indices at that moment, the TAIEX second. A day without trading answers with no
rows. It answers a GitHub runner (the source audit, 2026-10-13).

Folded onto the hour each print falls in, it moves with Yahoo's stored hours at
1.00 from 09:00 to 12:00, on 40 hours to 2026-10-13. Its 13:00 hour holds the
closing auction's 13:30 print, and Yahoo's does not hold it the same way: 0.50
there. So the vote does not ask it about a move in that hour (jump.verify,
Source.skip_hours; the user's, 2026-10-13).

About 1.2 MB a day: a vote asks the weekdays back to its earliest move, a
pause between days.

AND THE STANDING RISK. A public report the exchange publishes for anyone to read,
which has refused some cloud servers before. Should it refuse the runner, it is
an outage in the TAIEX's vote, left out of the count: the TAIEX has no other
voter.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from price_monitor.models import Candle, ExchangeError, Unreachable

URL = "https://www.twse.com.tw/exchangeReport/MI_5MINS_INDEX"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
TAIPEI = ZoneInfo("Asia/Taipei")
HOUR = 3600
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 3.0
DAY_PAUSE_SECONDS = 1.0


def parse(payload: dict, now: "datetime | None" = None) -> list[Candle]:
    """One day's hourly candles of the TAIEX (the rows' second column), for the
    hours that have ended."""
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    day = str((payload or {}).get("date") or "")
    hours: "dict[int, list[float]]" = {}
    for row in (payload or {}).get("data") or []:
        try:
            local = datetime.strptime(f"{day} {row[0]}", "%Y%m%d %H:%M:%S")
            price = float(str(row[1]).replace(",", ""))
        except (IndexError, TypeError, ValueError):
            continue
        start = int(local.replace(tzinfo=TAIPEI).timestamp()) // HOUR * HOUR
        bar = hours.get(start)
        if bar is None:
            hours[start] = [price, price, price, price]
        else:
            bar[1], bar[2], bar[3] = max(bar[1], price), min(bar[2], price), price
    return [Candle(open_time=t, open=o, high=h, low=low, close=c, volume=0.0,
                   close_time=t + HOUR)
            for t, (o, h, low, c) in sorted(hours.items()) if t + HOUR <= now_ts]


def _day(session, day: datetime) -> dict:
    params = {"response": "json", "date": day.strftime("%Y%m%d")}
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(URL, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException as exc:
            last = Unreachable(f"TAIEX {params['date']}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = (Unreachable if resp.status_code >= 500 else ExchangeError)(
                f"TAIEX {params['date']}: the exchange answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"TAIEX {params['date']}: the exchange answered "
                                f"{resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ExchangeError(f"TAIEX {params['date']}: the answer is not JSON") from exc
    raise last or ExchangeError(f"TAIEX {params['date']}: no response")


def fetch_hourly(session: "requests.Session | None" = None, now: "datetime | None" = None,
                 days: float = 9.0) -> list[Candle]:
    """The TAIEX's hourly bars of the weekdays in about the last `days`, a
    request each."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(TAIPEI)
    out: list[Candle] = []
    asked = 0
    for back in range(int(max(days, 1.0)) + 1):
        day = today - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        if asked:
            time.sleep(DAY_PAUSE_SECONDS)
        asked += 1
        out.extend(parse(_day(session, day), now))
    return sorted(out, key=lambda c: c.open_time)

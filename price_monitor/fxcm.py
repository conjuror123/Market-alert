"""FXCM's public candle archive, used to deepen the FX history below what Twelve
Data's plan will serve.

Twelve Data's free tier stops at 2020-01 for every currency pair at once - the
same cutoff for all of them, which is what gives away that it is a property of
the plan rather than of any symbol. FXCM publishes its own candles as plain
gzipped CSV over HTTPS with no key, no registration and no rate limit, going
back to 2012. That is three years past the 2015 the basket asks for.

This is a HISTORY source only. It does not replace Twelve Data, which keeps
collecting the live hourly bars for all eight pairs; FXCM fills in underneath
what is already stored and stops there. Two reasons it could not do the live
job even if we wanted it to: the archive is frozen (nothing past about week 17
of 2026), and it is weekly files rather than a query API.

WHY THE WEEK NUMBERS ARE PROBED RATHER THAN COMPUTED. The files are indexed by
a Sunday-start trading week, and the year-to-week mapping does not follow from
a formula: 2015 has a week 1 covering Jan 4-9, while 2020 has no week 1 at all
and starts at week 2 on Jan 5. Rather than reverse-engineer a rule that would
break on some year nobody checked, every week number in a year is simply asked
for and the misses skipped. The files are about three kilobytes, so a miss
costs nothing.

TIMESTAMPS ARE UTC, which was measured rather than assumed. Every hour offset
from -6 to +6 was tried against the bars already stored, and zero won on all
three pairs tested, with a median difference under one basis point. Getting
this wrong is the classic way to ruin an FX archive - HistData's files, for
comparison, are US/Eastern WITH daylight saving and say so nowhere, and reading
them as UTC drops the return correlation from 0.94 to 0.53.

The file carries bid and ask separately and no volume. The mid is taken, which
is what every other source in this project effectively serves, and the volume
stays 0.0 - correct for spot FX, where no provider has a consolidated exchange
volume, and consistent with what is already stored for these pairs.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
from datetime import date, datetime, timedelta, timezone

import requests

from price_monitor.models import Candle, ExchangeError

log = logging.getLogger("price_monitor.fxcm")

BASE_URL = "https://candledata.fxcorporate.com"

# Our ticker -> the archive's symbol. USD/CNY is deliberately absent: FXCM does
# not carry it under any spelling (checked), and the offshore USD/CNH is a
# different instrument, not a substitute to be slipped in quietly.
SYMBOLS = {
    "EUR/USD": "EURUSD", "GBP/USD": "GBPUSD", "USD/JPY": "USDJPY",
    "USD/CHF": "USDCHF", "USD/CAD": "USDCAD", "AUD/USD": "AUDUSD",
    "NZD/USD": "NZDUSD",
}

# Their week index runs past 52 in some years; 53 is asked for and usually
# missing, which costs one 404.
MAX_WEEK = 53

_TIMESTAMP = "%m/%d/%Y %H:%M:%S.%f"
HOUR_SECONDS = 3600


def symbol_for(ticker: str) -> str | None:
    """The archive's symbol for one of our tickers, or None if it has none."""
    return SYMBOLS.get(ticker)


def _mid(row: dict, field: str) -> float:
    return (float(row[f"Bid{field}"]) + float(row[f"Ask{field}"])) / 2.0


def parse_week(payload: bytes) -> list[Candle]:
    """Turns one weekly file into candles, bid and ask folded to the mid."""
    text = gzip.decompress(payload).decode("utf-8-sig", errors="replace")
    candles: list[Candle] = []
    for row in csv.DictReader(io.StringIO(text)):
        stamp = (row.get("DateTime") or "").strip()
        if not stamp:
            continue
        try:
            moment = datetime.strptime(stamp, _TIMESTAMP).replace(tzinfo=timezone.utc)
            opened = int(moment.timestamp())
            candles.append(Candle(
                open_time=opened, open=_mid(row, "Open"), high=_mid(row, "High"),
                low=_mid(row, "Low"), close=_mid(row, "Close"),
                # Spot FX has no consolidated exchange volume anywhere, and the
                # pairs already stored carry 0.0. Keeping that avoids a series
                # whose volume means one thing before 2020 and another after.
                volume=0.0, close_time=opened + HOUR_SECONDS))
        except (ValueError, KeyError, TypeError) as exc:
            log.debug("skipping unparsable row %r: %s", stamp, exc)
    candles.sort(key=lambda c: c.open_time)
    return candles


def fetch_week(symbol: str, year: int, week: int,
               session: requests.Session | None = None,
               base_url: str = BASE_URL, timeout: int = 30) -> list[Candle] | None:
    """One week of hourly bars, or None where the archive has no such file.

    None and an empty list are different answers and both happen: None is "this
    week number does not exist for this year", which is ordinary given the
    numbering, while an empty list would be a file that parsed to nothing and
    is worth noticing.
    """
    sess = session or requests
    url = f"{base_url}/H1/{symbol}/{year}/{week}.csv.gz"
    response = sess.get(url, timeout=timeout,
                        headers={"User-Agent": "market-alert-bot"})
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise ExchangeError(
            f"{symbol} {year}w{week}: unexpected status {response.status_code}")
    return parse_week(response.content)


def fetch_history(symbol: str, start: date, end: date,
                  session: requests.Session | None = None,
                  base_url: str = BASE_URL) -> list[Candle]:
    """Every hourly bar the archive has for `symbol` between `start` and `end`.

    Walks whole years and filters afterwards rather than trying to work out
    which week numbers cover the range - see the module docstring on why that
    mapping is not worth deriving. The boundary years fetch a few files more
    than they need, which at three kilobytes each is not worth avoiding.
    """
    if end <= start:
        return []
    lo = int(datetime(start.year, start.month, start.day,
                      tzinfo=timezone.utc).timestamp())
    hi = int(datetime(end.year, end.month, end.day,
                      tzinfo=timezone.utc).timestamp())

    # The last year is taken from the day BEFORE `end`, which is exclusive. A
    # range ending on the 1st of January otherwise walks a whole year that
    # cannot contribute a single bar - fifty-three requests for nothing, per
    # pair.
    last_year = (end - timedelta(days=1)).year

    by_time: dict[int, Candle] = {}
    for year in range(start.year, last_year + 1):
        found = 0
        for week in range(1, MAX_WEEK + 1):
            candles = fetch_week(symbol, year, week, session, base_url)
            if candles is None:
                continue
            found += 1
            for candle in candles:
                if lo <= candle.open_time < hi:
                    by_time[candle.open_time] = candle
        log.info("%s %d: %d weekly files, %d bars kept so far",
                 symbol, year, found, len(by_time))
    return sorted(by_time.values(), key=lambda c: c.open_time)

"""Dukascopy's public tick archive, read as hourly candles.

WHY THIS EXISTS ALONGSIDE FXCM. FXCM's archive starts in 2012 and stopped
updating around week 17 of 2026. Dukascopy serves the same seven majors from
2003 - nine years deeper, which is what puts the FX history in the same era as
the ETFs, so 2008 stops being a crisis the basket can only half see. It also
carries USD/CNH, which FXCM does not carry under any spelling.

NO NODE, NO KEY, NO ACCOUNT. The popular dukascopy-node package is a wrapper
around these same plain HTTPS files; there is no API behind it. One file holds
one month of one side of one instrument:

    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{SIDE}_candles_hour_1.bi5

MM IS ZERO-INDEXED - January is 00. Getting that wrong reads January as
February and shifts an entire archive by a month while every file still parses.

THE FORMAT, measured rather than looked up. The body is LZMA in the ALONE
container (not xz), and decompresses to a flat array of 24-byte big-endian
records:

    >i  seconds from the first moment of the month
    >i  open, >i close, >i low, >i high   - INTEGERS in units of the point
    >f  volume

NOTE THE FIELD ORDER: open, CLOSE, low, high. Reading it as OHLC silently
swaps close and high, which survives every sanity check that only asks whether
high >= low.

THE POINT IS NOT THE SAME FOR EVERY PAIR. It is 1e-5 for the five-decimal
pairs and 1e-3 for the yen ones - measured: in June 2013 USD/JPY's integers run
94085..100672, which is 94.085..100.672 and not 0.94085. A single scale puts
the yen a thousand times too low.

FILLER BARS ARE NOT DATA. The archive emits a record for EVERY hour of the
month, including the ones the market was shut: volume 0.0, and open, high, low
and close all equal to the last traded price. EUR/USD's first quarter of 2013
returns 2160 records for 1516 traded hours. Merging the other 644 would write
thousands of exactly-zero returns into the store, which does not merely add
noise - it DEFLATES the volatility estimate the severity ladder is fitted to,
and a ladder fitted to fabricated calm fires too easily. They are dropped on
volume.

TIMESTAMPS ARE UTC, measured the same way the FXCM ones were: every offset from
-1 to +1 hour tried against the bars already stored, on EUR/USD's 2013 Q1.

    +0h   n=1509   median |level diff| 0.08 bp   return corr 0.9832
    -1h   n=1495   median |level diff| 4.68 bp   return corr -0.045
    +1h   n=1497   median |level diff| 4.73 bp   return corr -0.050

BID AND ASK ARE SEPARATE FILES and both are fetched, because the stored bars
are mids. Bid alone would sit half a spread low and put a step at the seam
where this archive meets what is already there - small (measured at -0.19 bp
against the store, where the mid gives +0.00) but systematic, and a systematic
step at a seam is exactly what the alignment gate exists to catch.

VOLUME IS STORED AS 0.0, as it is for every other currency pair here. What the
archive calls volume is a tick-count liquidity proxy, not a consolidated
exchange volume - no such thing exists for spot FX - so it is used to tell a
traded hour from a filler and then discarded, rather than stored as a quantity
that would mean one thing before 2012 and another after.

RATE LIMITING IS REAL. Sustained requests earn HTTP 503, which is transient and
must be retried with backoff; 404 is the archive genuinely not holding the
month. Treating 503 as absence would silently truncate a pair's history at
whatever month the limiter first bit.
"""
from __future__ import annotations

import logging
import lzma
import struct
import time
from datetime import date, datetime, timedelta, timezone

import requests

from price_monitor.models import Candle, ExchangeError

log = logging.getLogger("price_monitor.dukascopy")

BASE_URL = "https://datafeed.dukascopy.com/datafeed"

# Our ticker -> the archive's symbol.
SYMBOLS = {
    "EUR/USD": "EURUSD", "GBP/USD": "GBPUSD", "USD/JPY": "USDJPY",
    "USD/CHF": "USDCHF", "USD/CAD": "USDCAD", "AUD/USD": "AUDUSD",
    "NZD/USD": "NZDUSD", "USD/CNH": "USDCNH",
}

# The earliest month each symbol holds a traded hour, probed rather than
# assumed. Asking below this costs a 404 per month per side, which is why the
# floor is written down once it has been found.
FIRST_MONTH = {
    "EURUSD": (2003, 5), "GBPUSD": (2003, 5), "USDJPY": (2003, 5),
    "USDCHF": (2003, 5), "USDCAD": (2003, 8), "AUDUSD": (2003, 8),
    "NZDUSD": (2003, 8), "USDCNH": (2012, 4),
}

RECORD = struct.Struct(">iiiiif")   # time, open, close, low, high, volume
RECORD_SIZE = RECORD.size
HOUR_SECONDS = 3600
SIDES = ("BID", "ASK")

# Integer prices are in units of the instrument's point.
POINT_DEFAULT = 1e-5
POINT_JPY = 1e-3

# Polite spacing between requests, and how hard to retry a 503.
REQUEST_DELAY_SECONDS = 1.5
MAX_ATTEMPTS = 6
BACKOFF_SECONDS = 3.0


def symbol_for(ticker: str) -> str | None:
    """The archive's symbol for one of our tickers, or None if it has none."""
    return SYMBOLS.get(ticker)


def point_for(symbol: str) -> float:
    """The price unit of the archive's integers for this symbol."""
    return POINT_JPY if symbol.endswith("JPY") else POINT_DEFAULT


def first_month(symbol: str) -> tuple[int, int] | None:
    return FIRST_MONTH.get(symbol)


def parse_month(payload: bytes, year: int, month: int, point: float) -> list[tuple]:
    """One decompressed month into (hour_utc, open, high, low, close, volume).

    Filler hours are kept here and dropped by the caller: this function's job is
    to report what the file says, and `volume` is how the caller tells a traded
    hour from a stand-in.
    """
    if not payload:
        return []
    body = lzma.decompress(payload, format=lzma.FORMAT_ALONE)
    if len(body) % RECORD_SIZE:
        raise ExchangeError(
            f"{year}-{month:02d}: {len(body)} bytes is not a whole number of "
            f"{RECORD_SIZE}-byte records")
    base = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
    rows = []
    for offset in range(0, len(body), RECORD_SIZE):
        seconds, opened, closed, low, high, volume = RECORD.unpack_from(body, offset)
        rows.append((base + seconds, opened * point, high * point, low * point,
                     closed * point, float(volume)))
    return rows


def fetch_month(symbol: str, year: int, month: int, side: str,
                session: requests.Session | None = None,
                base_url: str = BASE_URL, timeout: int = 90) -> list[tuple] | None:
    """One month of one side, or None where the archive has no such file.

    None (a 404) and an empty list are different answers: the first is "this
    month does not exist for this symbol", the second a file that held nothing.
    A 503 is neither - it is the rate limiter, and it is retried.
    """
    sess = session or requests
    url = f"{base_url}/{symbol}/{year}/{month - 1:02d}/{side}_candles_hour_1.bi5"
    last = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            response = sess.get(url, timeout=timeout,
                                headers={"User-Agent": "market-alert-bot"})
        except requests.RequestException as exc:
            last = str(exc)
            continue
        if response.status_code == 404:
            return None
        if response.status_code == 200:
            return parse_month(response.content, year, month, point_for(symbol))
        last = f"status {response.status_code}"
    raise ExchangeError(f"{symbol} {year}-{month:02d} {side}: gave up ({last})")


def _months(start: date, end: date) -> list[tuple[int, int]]:
    """Every (year, month) touching [start, end). Whole months are fetched and
    filtered afterwards - a month is one request and one file."""
    out = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def fetch_history(symbol: str, start: date, end: date,
                  session: requests.Session | None = None,
                  base_url: str = BASE_URL,
                  request_delay_seconds: float = REQUEST_DELAY_SECONDS) -> list[Candle]:
    """Every traded hourly bar between `start` and `end`, bid and ask folded
    to the mid.

    `end` is exclusive. Months before the symbol's known floor are skipped
    rather than asked for: below it every request is a 404, two per month per
    pair, and the floor is recorded precisely so those are not spent.
    """
    if end <= start:
        return []
    floor = first_month(symbol)
    lo = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    hi = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp())

    # `end` is exclusive, so the month it lands on contributes nothing when it
    # lands on the 1st - two requests a pair for a file every row of which is
    # filtered out again. The same trap the FXCM reader has for years.
    candles: list[Candle] = []
    for year, month in _months(start, end - timedelta(days=1)):
        if floor and (year, month) < floor:
            continue
        sides = {}
        for side in SIDES:
            rows = fetch_month(symbol, year, month, side, session, base_url)
            if request_delay_seconds:
                time.sleep(request_delay_seconds)
            if rows is None:
                sides = {}
                break
            # Filler hours carry no trade and no information; see the module
            # docstring on why keeping them is worse than dropping them.
            sides[side] = {r[0]: r for r in rows if r[5] > 0}
        if len(sides) != len(SIDES):
            continue

        bid, ask = sides["BID"], sides["ASK"]
        kept = 0
        for stamp in sorted(bid.keys() & ask.keys()):
            if not (lo <= stamp < hi):
                continue
            b, a = bid[stamp], ask[stamp]
            candles.append(Candle(
                open_time=stamp,
                open=(b[1] + a[1]) / 2.0, high=(b[2] + a[2]) / 2.0,
                low=(b[3] + a[3]) / 2.0, close=(b[4] + a[4]) / 2.0,
                # See the module docstring: their volume is a tick-count proxy,
                # and the pairs already stored carry 0.0.
                volume=0.0, close_time=stamp + HOUR_SECONDS))
            kept += 1
        log.info("%s %d-%02d: %d traded hours kept (%d bid, %d ask rows)",
                 symbol, year, month, kept, len(bid), len(ask))
    candles.sort(key=lambda c: c.open_time)
    return candles

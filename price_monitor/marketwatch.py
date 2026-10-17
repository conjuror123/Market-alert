"""MarketWatch's hourly bars: the store of the Bund, the gilt, TTF and Japan's
10-year yield (STORE), and a voter on the currency pairs, the funds, the futures,
gold and silver, and the stock indices abroad.

THE ENDPOINT is the one MarketWatch's own charts read (api.wsj.net, Dow Jones'
"michelangelo" time series): hourly bars for about the last ten days, keyed as
CURRENCY/US/XTUP/USDINR for a pair, FUND/US/ARCX/SPY for a fund (by its listing
exchange, jump.verify) and FUTURE/US/XCME/LC00 for live cattle's continuous
contract. Each tick is the start of its bar in UTC milliseconds -
against the stored bars, no shift (2026-10-04) - and a bar with no price comes
as nulls.

It has every hour of the pairs (the 17 of 2026-10-04), 0-0.6 bp from the stored
bars at the median, and its quotes are its own; live cattle's continuous contract moves
with the stored front contract at 0.92 hour by hour (docs/decisions.md, "The
sources' vote").

ITS INDICES (INDICES, for the basket's stage 1) are Yahoo's to the cent where both
hold the hour - hourly correlation 0.96-1.00 and 0 bp at the median over its ten
days to 2026-10-09 - one upstream's prices from two vendors, which is agreement,
not a copy (docs/decisions.md, "The sources' vote"). India's is the Sensex, not
the Nifty: another index of the same market, moving with it at 0.99. The KOSPI,
JSE Top 40 and Bovespa are found by Dow Jones' own search under keys of their
own (the source audit, 2026-10-13: 1.00, 0.99 and 1.00 hour by hour); the TAIEX
(0.86, its 13:00 hour 0.47) and the CSI 300 are not voters.

ITS FUTURES' CONTINUOUS SERIES (FUTURES, METALS, SOFTS) hold the nearest month and
change it on their own days. Yahoo's single contracts to the cent where both hold
the month, and gold's and silver's COMEX series move with SiftingIO's spot at
0.998-0.999, coffee's, cocoa's and cotton's with the stored contract at
0.998-1.00 (the source audit, 2026-10-13): each a voter with its own rolls.

AND THE STANDING RISK. An undocumented endpoint that wants the access token
MarketWatch's own pages send with every chart: public, in their page, not a
secret of this project. Reading it from a script sits in the same grey area as
reading another site's chart data. Should it change or close, it
is an outage in every vote it is in, left out of the count: live cattle is
voted by the FT alone.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import requests

from price_monitor.models import Candle, ExchangeError, Unreachable

URL = "https://api.wsj.net/api/michelangelo/timeseries/history"
# The token MarketWatch's own chart pages send; public, not this project's.
TOKEN = "cecc4267a0194af89ca343805a3e57af"
CKEY = "cecc4267a0"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
           "Dylan2010.EntitlementToken": TOKEN}
HOUR = 3600
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def _query(key: str, frame: str) -> dict:
    return {"Step": "PT1H", "TimeFrame": frame, "EntitlementToken": TOKEN,
            "IncludeMockTick": False, "FilterNullSlots": True, "FilterClosedPoints": True,
            "IncludeClosedSlots": False, "IncludeOfficialClose": False, "InjectOpen": False,
            "ShowPreMarket": False, "ShowAfterHours": False, "UseExtendedTimeFrame": True,
            "WantPriorClose": False, "IncludeCurrentQuotes": False,
            "ResetTodaysAfterHoursPercentChange": False,
            "Series": [{"Key": key, "Dialect": "Charting", "Kind": "Ticker", "SeriesId": "s1",
                        "DataTypes": ["Open", "High", "Low", "Last"], "Indicators": []}]}


def parse(payload: dict, key: str, now: datetime | None = None) -> list[Candle]:
    """Hourly candles from one answer: ended bars with a price only."""
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    try:
        ticks = payload["TimeInfo"]["Ticks"]
        points = payload["Series"][0]["DataPoints"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ExchangeError(f"{key}: MarketWatch's answer has no series") from exc
    out = []
    for tick, point in zip(ticks, points):
        if not point or any(v is None for v in point[:4]):
            continue
        start = int(tick) // 1000
        if start % HOUR or start + HOUR > now_ts:
            continue                             # off the hour, or not ended yet
        o, h, low, c = (float(v) for v in point[:4])
        out.append(Candle(open_time=start, open=o, high=h, low=low, close=c, volume=0.0,
                          close_time=start + HOUR))
    return out


INDICES = {
    "^AXJO": "INDEX/AU/XASX/XJO", "^N225": "INDEX/JP/XTKS/NIK", "^HSI": "INDEX/HK/XHKG/HSI",
    "^NSEI": "INDEX/IN/XBOM/1", "^STOXX50E": "INDEX/XX/XSTX/SX5E", "^FTSE": "INDEX/UK//UKX",
    "^SSMI": "INDEX/CH/XSWX/SMI", "XU100.IS": "INDEX/TR/XIST/XU100",
    "^TASI.SR": "INDEX/SA//TASI", "^GSPTSE": "INDEX/CA/XTSE/GSPTSE", "^MXX": "INDEX/MX//IPC",
    "^KS11": "INDEX/KR/XKRX/180721", "^J200.JO": "INDEX/XX/XJSE/168635",
    "^BVSP": "INDEX/BR/BVMF/BVSP",
    # Yahoo's to the cent at 10:00-12:00 Taipei; its 13:00 bar, the closing
    # auction's, is another grid's (0.46) and not asked about (jump.verify).
    "^TWII": "INDEX/TW/XTAI/Y9999",
}

# Stage 2's futures, each its continuous series (the nearest month, changing on its
# own days): Yahoo's single contract to the cent where both hold the month - hourly
# moves 0.991-1.000, 93-98% of closes the same, 2026-09-28 to 10-12. Brent is ICE's
# (BRN00), with the same last trading day as NYMEX's BZ; NYMEX's own is refused (400).
FUTURES = {
    "BZ=F": "FUTURE/UK/IFEU/BRN00", "ZT=F": "FUTURE/US/XCBT/TU00",
    "ZN=F": "FUTURE/US/XCBT/TY00", "ZB=F": "FUTURE/US/XCBT/US00",
    "ZC=F": "FUTURE/US/XCBT/C00", "ZW=F": "FUTURE/US/XCBT/W00", "ZS=F": "FUTURE/US/XCBT/S00",
    "SB=F": "FUTURE/US/IFUS/SB00", "PL=F": "FUTURE/US/XNYM/PL00",
}
# What MarketWatch serves the store (jump.backfill), by ticker: the Bund's and the
# gilt's single contract - the month and year the store holds appended
# (FGBLZ26: jump.futures.front_contract) -, TTF's continuous series (ICE Endex
# names no single month here) and Japan's 10-year yield. About nine trading days
# back, hourly, without volume (2026-10-13: the Bund 08:00-17:00 Frankfurt, the
# gilt 01:00-20:00 London, TTF 08:00-17:00 Amsterdam, the yield 09:00-17:00 Tokyo).
STORE = {"FGBL=F": "FUTURE/DE/XEUR/FGBL", "RLI=F": "FUTURE/UK/IFEU/RLI",
         "TFM=F": "FUTURE/UK/NDEX/TFMI00", "JP10Y": "BOND/BX/XTUP/TMBMKJP-10Y"}
CONTRACTS = ("FGBL=F", "RLI=F")


def store_key(ticker: str, contract: "str | None" = None) -> str:
    """MarketWatch's key for what the store holds: a contract (FGBLZ26.EUX) by
    its month and year."""
    if ticker not in CONTRACTS:
        return STORE[ticker]
    root = ticker[:-2]
    return STORE[ticker] + contract.split(".")[0][len(root):]


# Spot gold and silver, voted on by COMEX's continuous series.
METALS = {"XAU/USD": "FUTURE/US/XNYM/GC00", "XAG/USD": "FUTURE/US/XNYM/SI00"}
SOFTS = {"KC=F": "FUTURE/US/IFUS/KC00", "CC=F": "FUTURE/US/IFUS/CC00",
         "CT=F": "FUTURE/US/IFUS/CT00"}


def fetch_hourly(key: str, session: requests.Session | None = None,
                 now: datetime | None = None, frame: str = "D10") -> list[Candle]:
    """The last ~ten days of `key`'s hourly bars that have ended."""
    params = {"json": json.dumps(_query(key, frame)), "ckey": CKEY}
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(URL, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException as exc:
            last = Unreachable(f"{key}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = (Unreachable if resp.status_code >= 500 else ExchangeError)(
                f"{key}: MarketWatch answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{key}: MarketWatch answered {resp.status_code}")
        try:
            return parse(resp.json(), key, now)
        except ValueError as exc:
            raise ExchangeError(f"{key}: MarketWatch's answer is not JSON") from exc
    raise last or ExchangeError(f"{key}: no response")

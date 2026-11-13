"""The FT's hourly bars: a voter on live cattle, coffee, cocoa, sugar, Brent, the
grains, platinum, the Bund, the UK's gilts fund and five stock indices abroad.

THE ENDPOINT is the one the FT's markets pages chart from (markets.ft.com's
chartapi/series): an instrument's hourly bars by its xid, the last
MAX_DAYS trading days (2026-10-10: 24 answered, 25 not), without volume. Each
bar is dated by its END - against the stored bars, one hour on - and is stamped
here by its start. It answers a GitHub runner as it answers anywhere (probed
from one, 2026-10-10: the same bars, to the cent).

THE DATES ARE ON THE EXCHANGE'S CLOCK OF THE DAY ASKED. A date is the bar's end
on the exchange's local clock less its offset from UTC at the time of asking
(the answer's UtcOffsetMinutes), not at the bar's: a bar from before the
exchange's last change of clock is an hour off, late after the autumn's, early
after the spring's. Corn's 2026-10-09 WASDE hour, -6.4%, ended at 17:00 UTC when
asked on 10-16 (summer, as the vote recorded it) and at 18:00 when asked on
11-04. So the date plus the offset asked under is the local clock, read in the
exchange's zone (ZONES, by the answer's ExchangeId); an exchange not there, or
an answer without its offset, is an outage, not a guess. Hour by hour against
the store over 2026-10-01 to 11-03, asked on 11-04: read as UTC, the summer's
bars 0.10-0.57 (Europe's, Brent's, cocoa's) and about 0 (America's), read on
the exchange's clock 0.87-0.99; Sao Paulo and Mexico City, with no summer time,
0.97-1.00 either way (docs/decisions.md).

LIVE CATTLE BY ITS CONTRACT. The FT carries each cattle contract (LCZ26:CME;
the CME's LE, its LC) and its continuous series, which holds the nearest month:
October while the store held December from 2026-09-15, 35-170 bp apart. So it
is asked for the contract the store holds (jump.futures.front_contract), found
by the FT's search. On the store's December days, 2026-09-15 to 10-09: 2.3 bp
apart at the median.

COFFEE ONLY CONTINUOUS (KC.1:IUS): no contract is found by name. It changes
contract on its own days - in 2026-09 a week after the store, with no bars
from 09-15 to 09-18 - and the vote treats it as it treats Sina's (own_rolls).
On the store's month, 2026-09-21 to 10-09: 3.6 bp apart, hourly moves
correlated 0.988 (docs/decisions.md, "Every source on every instrument").

AND THE STANDING RISK. An undocumented endpoint, read without a key as the FT's
pages read it, under the FT's terms. Should it change or close, it is an
outage, left out of the count: cattle and coffee are voted by MarketWatch and
Sina alone again.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from price_monitor.models import Candle, ExchangeError, Unreachable

CHART = "https://markets.ft.com/data/chartapi/series"
SEARCH = "https://markets.ft.com/data/searchapi/searchsecurities"
HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
# The continuous series the FT names, by xid.
CONTINUOUS = {"KC.1": "1046650"}
# Cocoa's and sugar's continuous series by their xids (found by the FT's search,
# 2026-10-13: cocoa 0.92 hour by hour with the stored contract; 2026-10-17: Sugar
# No. 11, SB.1:IUS, 0.942 over 2026-09-15 to 10-16, all 5 big moves it had bars
# for confirmed).
SOFTS = {"KC=F": "KC.1", "CC=F": "1055540", "SB=F": "1046731"}
# The stock indices abroad, for the basket's stage 1, by xid: those moving with
# Yahoo's folded bars at 0.90 or more hour by hour over its 24 days to
# 2026-10-09 - Europe's and the Americas' 0.92-0.98. Asia's lag it (the Nikkei
# 0.80, the Hang Seng 0.86, the ASX 200 0.06) and are left out.
INDICES = {"^STOXX50E": "567938", "^FTSE": "572009", "^SSMI": "567786",
           "^BVSP": "570637", "^MXX": "584510"}
# The UK's gilts by iShares' Core UK Gilts fund on the LSE (IGLT:LSE:GBP), by xid:
# against Yahoo's half hours folded, 2026-09-15 to 10-16, hourly moves 0.935 at
# 09:00-14:00 London, all 3 big moves the same way; its opening hour (0.86) and its
# last two (0.75, 0.17: the 16:30 closing auction is Yahoo's) are not asked about
# (jump.verify).
FUNDS = {"IGLT.L": "6141822"}
MAX_DAYS = 24
# Each exchange's zone, by the FT's ExchangeId: the clock its dates are read on.
ZONES = {"LSE": "Europe/London", "FSI": "Europe/London", "IEU": "Europe/London",
         "STX": "Europe/Berlin", "SWX": "Europe/Zurich",
         "IUS": "America/New_York", "NYM": "America/New_York",
         "CME": "America/Chicago", "CBT": "America/Chicago", "IOM": "America/Chicago",
         "SAO": "America/Sao_Paulo", "MEX": "America/Mexico_City"}
HOUR = 3600
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
# A contract's xid, once found by the search in this run.
_found: "dict[str, str]" = {}


# Stage 2's futures the FT carries by contract (2026-10-12): CBOT's grains by their
# pit roots (CZ26 for Yahoo's ZCZ26), NYMEX's platinum by its electronic name
# (US@PLF272 for PLF27). Its Treasuries (US@TYZ262, ...) moved with Yahoo's at
# 0.885-0.89 and are not asked; it holds no Brent or sugar contract.
FUTURES_ROOTS = {"ZC": "C", "ZW": "W", "ZS": "S"}
# Brent by its continuous series' xid, which its search finds (the source audit,
# 2026-10-13: 0.977 hour by hour with the stored contract, 34 days back); the
# Bund by S&P's Euro-Bund futures index (BDPY:IOM): 0.95 with MarketWatch's
# contract, but bars only at 13:00-17:00 UTC, its rolls S&P's.
FUTURES_CONTINUOUS = {"BZ=F": "1054972", "FGBL=F": "31880145"}


def futures_symbol(contract: "str | None") -> "str | None":
    """The FT's name for a stage 2 contract Yahoo names ZCZ26.CBT or PLF27.NYM."""
    code = (contract or "").split(".")[0]
    root, month = code[:2], code[2:]
    if root in FUTURES_ROOTS:
        return FUTURES_ROOTS[root] + month
    if root == "PL":
        return f"US@PL{month}2"
    return None


def cattle_symbol(contract: "str | None") -> "str | None":
    """The FT's name for a live cattle contract: LEZ26.CME is its LCZ26."""
    if not contract or not contract.startswith("LE"):
        return None
    return "LC" + contract.split(".")[0][2:]


def body(xid: str, days: int) -> dict:
    return {"days": days, "dataNormalized": False, "dataPeriod": "Hour", "dataInterval": 1,
            "realtime": False, "yFormat": "0.###", "timeServiceFormat": "JSON",
            "rulerIntradayStart": 26, "rulerIntradayStop": 3, "rulerInterdayStart": 10957,
            "rulerInterdayStop": 365, "returnDateType": "ISO8601",
            "elements": [{"Label": "x", "Type": "price", "Symbol": xid,
                          "OverlayIndicators": [], "Params": {}}]}


def parse(payload: dict, name: str, now: datetime | None = None) -> list[Candle]:
    """Hourly candles from one answer: ended bars with every price only."""
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    try:
        dates = payload["Dates"]
        element = payload["Elements"][0]
        series = {s["Type"]: s["Values"] for s in element["ComponentSeries"]}
        columns = [series[k] for k in ("Open", "High", "Low", "Close")]
    except (KeyError, IndexError, TypeError) as exc:
        raise ExchangeError(f"{name}: the FT's answer has no bars") from exc
    exchange, offset = element.get("ExchangeId"), element.get("UtcOffsetMinutes")
    if exchange not in ZONES or not isinstance(offset, int):
        raise ExchangeError(f"{name}: the FT's exchange {exchange!r} (offset {offset!r}) "
                            "has no zone here")
    zone, asked_under = ZoneInfo(ZONES[exchange]), timedelta(minutes=offset)
    out = []
    for i, text in enumerate(dates):
        prices = [col[i] if i < len(col) else None for col in columns]
        if any(p is None for p in prices):
            continue                             # no price that hour
        local = datetime.fromisoformat(text) + asked_under      # the exchange's clock
        end = int(local.replace(tzinfo=zone).timestamp())
        if end % HOUR or end > now_ts:
            continue                             # off the hour, or not ended yet
        o, h, low, c = (float(p) for p in prices)
        out.append(Candle(open_time=end - HOUR, open=o, high=h, low=low, close=c, volume=0.0,
                          close_time=end))
    return out


def _ask(do, name: str):
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = do()
        except requests.RequestException as exc:
            last = Unreachable(f"{name}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = (Unreachable if resp.status_code >= 500 else ExchangeError)(
                f"{name}: the FT answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{name}: the FT answered {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ExchangeError(f"{name}: the FT's answer is not JSON") from exc
    raise last or ExchangeError(f"{name}: no response")


def xid_for(name: str, session: requests.Session | None = None) -> str:
    """The xid of a continuous series the FT names, or of a contract its
    search finds (LCZ26)."""
    if name in CONTINUOUS:
        return CONTINUOUS[name]
    if name.isdigit():                   # an xid already (INDICES)
        return name
    if name not in _found:
        answer = _ask(lambda: (session or requests).get(
            SEARCH, params={"query": name}, headers=HEADERS, timeout=30), name)
        found = ((answer or {}).get("data") or {}).get("security") or []
        xid = next((s.get("xid") for s in found if s.get("assetClass") == "Commodities"
                    and str(s.get("symbol", "")).split(":")[0] == name), None)
        if xid is None:
            raise ExchangeError(f"{name}: not found by the FT's search")
        _found[name] = str(xid)
    return _found[name]


def fetch_hourly(name: str, session: requests.Session | None = None,
                 now: datetime | None = None, days: float = 10.0) -> list[Candle]:
    """`name`'s hourly bars of about the last `days` that have ended: asked in
    trading days, which reach at least as far."""
    xid = xid_for(name, session)
    payload = json.dumps(body(xid, min(MAX_DAYS, max(1, math.ceil(days)))))
    answer = _ask(lambda: (session or requests).post(CHART, data=payload, headers=HEADERS,
                                                     timeout=30), name)
    return parse(answer, name, now)

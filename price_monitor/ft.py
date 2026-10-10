"""The FT's hourly bars: a voter on live cattle and coffee.

THE ENDPOINT is the one the FT's markets pages chart from (markets.ft.com's
chartapi/series): an instrument's hourly bars by its xid, the last
MAX_DAYS trading days (2026-10-10: 24 answered, 25 not), without volume. Each
bar is dated by its END in UTC - against the stored bars, one hour on - and is
stamped here by its start. It answers a GitHub runner as it answers here (the
backfill's one-off ft-probe, 2026-10-10: the same bars, to the cent).

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
correlated 0.988 (docs/decisions.md, "The FT for live cattle and coffee").

AND THE STANDING RISK. An undocumented endpoint, read without a key as the FT's
pages read it, under the FT's terms. Should it change or close, it is an
outage, left out of the count: cattle and coffee are voted by MarketWatch and
Sina alone again.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone

import requests

from price_monitor.models import Candle, ExchangeError, Unreachable

CHART = "https://markets.ft.com/data/chartapi/series"
SEARCH = "https://markets.ft.com/data/searchapi/searchsecurities"
HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
# The continuous series the FT names, by xid.
CONTINUOUS = {"KC.1": "1046650"}
SOFTS = {"KC=F": "KC.1"}
MAX_DAYS = 24
HOUR = 3600
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
# A contract's xid, once found by the search in this run.
_found: "dict[str, str]" = {}


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
        series = {s["Type"]: s["Values"] for s in payload["Elements"][0]["ComponentSeries"]}
        columns = [series[k] for k in ("Open", "High", "Low", "Close")]
    except (KeyError, IndexError, TypeError) as exc:
        raise ExchangeError(f"{name}: the FT's answer has no bars") from exc
    out = []
    for i, text in enumerate(dates):
        prices = [col[i] if i < len(col) else None for col in columns]
        if any(p is None for p in prices):
            continue                             # no price that hour
        end = int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())
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

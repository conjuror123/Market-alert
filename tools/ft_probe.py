"""One-off: does the FT's chart data answer where the bot runs? Hourly bars of
live cattle's December contract and of the FT's continuous cattle and coffee,
saved for a measurement.

    python -m tools.ft_probe OUT

The FT's markets pages chart from markets.ft.com/data/chartapi/series (an xid,
about 23 trading days of hourly bars, each dated by its end); its search
(searchsecurities) names a cattle contract's xid, LCZ26:CME, and carries
coffee only as its continuous KC.1:IUS. Each answer is written whole to
OUT/<name>.json, and OUT/summary.txt says what came back; each line is printed
as a notice too, so the run's annotations carry it. The bot never runs this: it
writes nothing to the store.
"""
from __future__ import annotations

import argparse
import json
import logging
import os

log = logging.getLogger(__name__)

SEARCH = "https://markets.ft.com/data/searchapi/searchsecurities"
CHART = "https://markets.ft.com/data/chartapi/series"
HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
# Found by the FT's search (LCZ26) or known (the continuous series).
SEARCHED = ("LCZ26",)
CONTINUOUS = {"LC.1": "1044934", "KC.1": "1046650"}
DAYS = 23


def chart_body(xid: str) -> dict:
    return {"days": DAYS, "dataNormalized": False, "dataPeriod": "Hour", "dataInterval": 1,
            "realtime": False, "yFormat": "0.###", "timeServiceFormat": "JSON",
            "rulerIntradayStart": 26, "rulerIntradayStop": 3, "rulerInterdayStart": 10957,
            "rulerInterdayStop": 365, "returnDateType": "ISO8601",
            "elements": [{"Label": "x", "Type": "price", "Symbol": xid,
                          "OverlayIndicators": [], "Params": {}}]}


def xid_of(symbol: str, session) -> "str | None":
    """A commodity contract's xid as the FT's search names it."""
    resp = session.get(SEARCH, params={"query": symbol}, headers=HEADERS, timeout=30)
    found = ((resp.json() or {}).get("data") or {}).get("security") or []
    return next((s.get("xid") for s in found if s.get("assetClass") == "Commodities"
                 and str(s.get("symbol", "")).split(":")[0] == symbol), None)


def closes(payload: dict) -> list:
    for series in ((payload or {}).get("Elements") or [{}])[0].get("ComponentSeries") or []:
        if series.get("Type") == "Close":
            return series.get("Values") or []
    return []


def probe(out_dir: str, session) -> "list[str]":
    os.makedirs(out_dir, exist_ok=True)
    asked = dict(CONTINUOUS)
    summary = []
    for symbol in SEARCHED:
        try:
            asked[symbol] = xid_of(symbol, session)
        except Exception as exc:
            summary.append(f"{symbol}: search failed - {str(exc)[:160]}")
            continue
        if asked[symbol] is None:
            summary.append(f"{symbol}: not found by the search")
    for name, xid in asked.items():
        if xid is None:
            continue
        try:
            resp = session.post(CHART, data=json.dumps(chart_body(xid)), headers=HEADERS,
                                timeout=30)
            payload = resp.json()
        except Exception as exc:
            summary.append(f"{name}: failed - {str(exc)[:160]}")
            continue
        dates, close = payload.get("Dates") or [], closes(payload)
        span = f"{dates[0]} .. {dates[-1]} (ends)" if dates else ""
        summary.append(f"{name} (xid {xid}): HTTP {resp.status_code}, {len(close)} hourly bars "
                       f"{span}, closes sum {sum(c for c in close if c is not None):.3f}")
        with open(os.path.join(out_dir, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.writelines(f"{line}\n" for line in summary)
    return summary


def main(argv: "list[str] | None" = None) -> int:
    import requests

    parser = argparse.ArgumentParser(description="The FT's hourly bars, asked from a runner")
    parser.add_argument("out_dir")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for line in probe(args.out_dir, requests.Session()):
        print(line)
        print(f"::notice title=ft-probe::{line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

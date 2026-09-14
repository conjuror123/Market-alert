"""Second Tiingo pass: settle what the first pass raised rather than answered.

Four questions left over from tools/tiingo_probe.py:

  1. The first pass flagged a 100.0000% one-day step in adjClose/close on all
     five sector SPDRs, on the same date. An identical step on the same day
     across five funds is not five dividends - it is one corporate action, or
     it is an artefact of the detector. splitFactor is in the response, so
     print the rows and let them say which.
  2. The IEX intraday response has no volume field at all - not zero, absent.
     Check whether any parameter brings it back.
  3. The depth probe returned exactly 10000 rows, which is a cap, not an edge.
     Ask for an old narrow window to find the real start of intraday history.
  4. Does the FX endpoint do 30min, and how far back does it go? The ETFs are
     fetched at 30min, the pairs at 1h, so both need checking.

Same rules as the first pass: read-only, key never printed, request budget
respected.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://api.tiingo.com"
TIMEOUT = 30
REQUEST_BUDGET = 25

_spent = 0
_key = ""

SUSPECTS = ["XLK", "XLY", "XLE", "XLU", "XLB"]
# The full ETF list, for a realistic end-to-end timing measurement.
ETFS = [
    "XLK", "XLF", "XLY", "XLP", "XLE", "XLV", "XLI", "XLB", "XLU", "XLRE",
    "XLC", "SPY", "QQQ", "IWM", "EFA", "EEM", "SHY", "IEI", "IEF", "TLH",
    "TLT", "TIP", "MBB", "LQD", "HYG", "JNK", "EMB", "BKLN", "PFF", "USO",
    "BNO", "UGA", "UNG", "GLD", "SLV", "PPLT", "PALL", "DBB", "CPER", "DBA",
    "CORN", "WEAT", "SOYB", "DBC",
]


def _scrub(text: str) -> str:
    out = str(text)
    if _key:
        out = out.replace(_key, "<TIINGO_API_KEY>")
    return out


def get(path: str, **params):
    global _spent
    _spent += 1
    if _spent > REQUEST_BUDGET:
        raise SystemExit(f"probe would exceed its {REQUEST_BUDGET}-request budget")
    started = time.monotonic()
    try:
        r = requests.get(f"{BASE}{path}", params=params,
                         headers={"Content-Type": "application/json",
                                  "Authorization": f"Token {_key}"},
                         timeout=TIMEOUT)
    except requests.RequestException as exc:
        return 0, {"error": _scrub(exc)}, 0, time.monotonic() - started
    took = time.monotonic() - started
    try:
        body = r.json()
    except ValueError:
        body = {"raw": _scrub(r.text[:200])}
    return r.status_code, body, len(r.content), took


def main() -> int:
    global _key
    _key = (os.environ.get("TIINGO_API_KEY") or "").strip()
    if not _key:
        print("TIINGO_API_KEY is not set.")
        return 1

    print("=" * 78)
    print("1. THE 100% STEP  - one corporate action, or a detector artefact?")
    print("=" * 78)
    for ticker in SUSPECTS:
        status, body, size, took = get(
            f"/tiingo/daily/{ticker}/prices",
            startDate="2025-11-28", endDate="2025-12-12", resampleFreq="daily")
        print(f"\n  {ticker}  HTTP {status}  rows="
              f"{len(body) if isinstance(body, list) else '-'}")
        if status != 200 or not isinstance(body, list):
            print(f"    {_scrub(json.dumps(body))[:200]}")
            continue
        print(f"    {'date':<12}{'close':>10}{'adjClose':>11}"
              f"{'divCash':>10}{'splitFactor':>13}{'adj/close':>11}")
        for row in body:
            close = row.get("close") or 0.0
            adj = row.get("adjClose") or 0.0
            ratio = (adj / close) if close else float("nan")
            print(f"    {str(row.get('date'))[:10]:<12}{close:>10.2f}"
                  f"{adj:>11.2f}{row.get('divCash', 0):>10.4f}"
                  f"{row.get('splitFactor', 1):>13.4f}{ratio:>11.4f}")

    print()
    print("=" * 78)
    print("2. VOLUME ON THE IEX FEED  - absent, or just not asked for?")
    print("=" * 78)
    start = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%Y-%m-%d")
    for label, params in (
        ("plain 30min", {"startDate": start, "resampleFreq": "30min"}),
        ("columns=open,high,low,close,volume",
         {"startDate": start, "resampleFreq": "30min",
          "columns": "open,high,low,close,volume"}),
        ("no resampleFreq (raw ticks)", {"startDate": start}),
        ("afterHours+forceFill", {"startDate": start, "resampleFreq": "30min",
                                  "afterHours": "true", "forceFill": "true"}),
    ):
        status, body, size, took = get("/iex/SPY/prices", **params)
        fields = sorted(body[0].keys()) if (
            status == 200 and isinstance(body, list) and body) else []
        rows = len(body) if isinstance(body, list) else 0
        print(f"  {label:<38} HTTP {status:<4} rows={rows:<6} {size:>8,}B")
        print(f"      fields: {fields or _scrub(json.dumps(body))[:140]}")
        if "volume" in fields:
            vols = [r.get("volume") for r in body[:5]]
            print(f"      VOLUME PRESENT, first five: {vols}")

    print()
    print("=" * 78)
    print("3. REAL INTRADAY DEPTH  - where does the 30-minute history start?")
    print("=" * 78)
    for year in ("2018", "2021", "2023"):
        status, body, size, took = get(
            "/iex/SPY/prices", startDate=f"{year}-01-02",
            endDate=f"{year}-01-31", resampleFreq="30min")
        rows = len(body) if isinstance(body, list) else 0
        span = (f"{str(body[0].get('date'))[:10]} .. "
                f"{str(body[-1].get('date'))[:10]}") if rows else "-"
        print(f"  Jan {year}: HTTP {status:<4} rows={rows:<6} {span}")

    print()
    print("=" * 78)
    print("4. FX SHAPE AND DEPTH  - 30min support, and how far back")
    print("=" * 78)
    for label, params in (
        ("eurusd 30min, last 4d",
         {"startDate": start, "resampleFreq": "30min"}),
        ("eurusd 1hour, Jan 2021",
         {"startDate": "2021-01-04", "endDate": "2021-01-31",
          "resampleFreq": "1hour"}),
    ):
        status, body, size, took = get(f"/tiingo/fx/eurusd/prices", **params)
        rows = len(body) if isinstance(body, list) else 0
        print(f"  {label:<32} HTTP {status:<4} rows={rows:<6} {size:>8,}B "
              f"{took:5.2f}s")
        if rows:
            print(f"      fields: {sorted(body[0].keys())}")
            print(f"      span {str(body[0].get('date'))[:16]} .. "
                  f"{str(body[-1].get('date'))[:16]}")
        else:
            print(f"      {_scrub(json.dumps(body))[:200]}")

    print()
    print("=" * 78)
    print("5. FULL-BASKET TIMING  - 44 ETFs back to back, no pacing")
    print("=" * 78)
    started = time.monotonic()
    total_bytes = 0
    failures = []
    # Budget check: this is 44 requests and would blow the hourly bucket on top
    # of everything above, so it is measured on a sample and scaled.
    sample = ETFS[:8]
    for ticker in sample:
        status, body, size, took = get(f"/iex/{ticker}/prices",
                                       startDate=start, resampleFreq="30min")
        total_bytes += size
        if status != 200 or not isinstance(body, list) or not body:
            failures.append(ticker)
    elapsed = time.monotonic() - started
    per = elapsed / len(sample)
    per_bytes = total_bytes / len(sample)
    print(f"  {len(sample)} requests in {elapsed:5.2f}s -> {per:.3f}s each, "
          f"{per_bytes:,.0f}B each")
    print(f"  failures: {failures or 'none'}")
    for n, what in ((44, "44 ETFs"), (52, "all 52 symbols")):
        print(f"  >>> {what:<16} ~{per * n:5.1f}s sequential, "
              f"{per_bytes * n * 24 * 30 / 1e9:6.3f} GB/month at 24 runs/day")

    print()
    print("=" * 78)
    print(f"SPENT {_spent} of {REQUEST_BUDGET}.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

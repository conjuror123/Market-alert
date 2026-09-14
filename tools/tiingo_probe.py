"""Ask Tiingo what it actually serves on a free key, and print the answer.

Read-only: it fetches, measures and reports. Nothing here writes to the store
or to config, so it is safe to run against production secrets.

WHY THIS EXISTS. Tiingo's pricing page lists what a free key includes in
marketing terms ("EOD composite prices, crypto, IEX feed, and news") and the
docs describe endpoints without saying which tier reaches them. Three things
decide whether Tiingo can carry part of this basket, and none can be settled
by reading:

  1. Does a free key reach /tiingo/fx at all? Eight of our 52 symbols are
     currency pairs. If FX is paid-only they must stay on Twelve Data.
  2. Does the IEX feed know our 44 ETFs, including the thin ones? IEX is one
     exchange, not the consolidated tape - CPER and SOYB may simply not print
     there often enough to build a 30-minute bar.
  3. How many bytes does a request cost? The free tier caps bandwidth at 1
     GB/month, and the IEX price endpoint defaults to the most recent 2000
     bars. At 24 runs a day that default would spend the monthly cap in under
     two weeks; an explicit startDate is the difference between viable and not.

BUDGET. Tiingo's free limits are 50 requests/hour and 1000/day. This probe is
built to stay under the hourly bucket in one pass - see REQUEST_BUDGET - so it
can be re-run within the same hour if a question needs re-asking. Coverage of
all 44 ETFs costs ONE request, not 44, because the top-of-book endpoint takes
a ticker list; only the handful most likely to be thin get a full history
request each.

The key is read from TIINGO_API_KEY and is never printed. Errors are scrubbed
before they reach the log, because a 4xx body can echo the query string back.
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

# Every ETF in the basket, in config order, plus the one outside it (DBC).
ETFS = [
    "XLK", "XLF", "XLY", "XLP", "XLE", "XLV", "XLI", "XLB", "XLU", "XLRE",
    "XLC", "SPY", "QQQ", "IWM", "EFA", "EEM", "SHY", "IEI", "IEF", "TLH",
    "TLT", "TIP", "MBB", "LQD", "HYG", "JNK", "EMB", "BKLN", "PFF", "USO",
    "BNO", "UGA", "UNG", "GLD", "SLV", "PPLT", "PALL", "DBB", "CPER", "DBA",
    "CORN", "WEAT", "SOYB", "DBC",
]

# The eight currency pairs, in Tiingo's lowercase concatenated spelling.
FX_PAIRS = ["eurusd", "usdjpy", "gbpusd", "audusd", "nzdusd",
            "usdchf", "usdcad", "usdcnh"]

# The ETFs least likely to print on IEX often enough for a 30-minute bar:
# single-commodity and niche-credit funds that trade a few thousand shares a
# day on the consolidated tape, of which IEX sees a few percent. If these
# work, the liquid majority certainly does; if they don't, they are exactly
# the ones that have to stay on Twelve Data.
THIN = ["CPER", "SOYB", "CORN", "WEAT", "UGA", "BNO", "PPLT", "PALL", "BKLN"]

# The five sector SPDRs whose Twelve Data adjusted series carries the known
# ~2x dividend step. Fetching them here is a free second opinion.
DIVIDEND_SUSPECTS = ["XLK", "XLY", "XLE", "XLU", "XLB"]

REQUEST_BUDGET = 50  # Tiingo free tier, per hour.

_spent = 0
_key = ""


def _scrub(text: str) -> str:
    """Remove the key from anything on its way to the log."""
    out = str(text)
    if _key:
        out = out.replace(_key, "<TIINGO_API_KEY>")
    return out


def get(path: str, **params) -> tuple[int, object, int, float, dict]:
    """One request. Returns (status, parsed body, bytes, seconds, headers)."""
    global _spent
    _spent += 1
    if _spent > REQUEST_BUDGET:
        raise SystemExit(f"probe would exceed its {REQUEST_BUDGET}-request budget")
    url = f"{BASE}{path}"
    started = time.monotonic()
    try:
        r = requests.get(
            url,
            params=params,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Token {_key}"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return 0, {"error": _scrub(exc)}, 0, time.monotonic() - started, {}
    took = time.monotonic() - started
    size = len(r.content)
    try:
        body = r.json()
    except ValueError:
        body = {"raw": _scrub(r.text[:200])}
    interesting = {k: v for k, v in r.headers.items()
                   if "rate" in k.lower() or "limit" in k.lower()
                   or "quota" in k.lower() or k.lower() == "content-encoding"}
    return r.status_code, body, size, took, interesting


def show(label: str, status: int, body: object, size: int, took: float,
         headers: dict, sample: bool = True) -> None:
    n = len(body) if isinstance(body, list) else "-"
    print(f"  {label:<46} HTTP {status:<4} {size:>7,}B {took:5.2f}s  rows={n}")
    if headers:
        print(f"      headers: {json.dumps(headers)}")
    if status != 200:
        print(f"      body: {_scrub(json.dumps(body))[:300]}")
    elif sample and isinstance(body, list) and body:
        print(f"      first: {_scrub(json.dumps(body[0]))[:300]}")
        if len(body) > 1:
            print(f"      last:  {_scrub(json.dumps(body[-1]))[:300]}")
    elif sample and isinstance(body, dict):
        print(f"      body: {_scrub(json.dumps(body))[:300]}")


def main() -> int:
    global _key
    _key = (os.environ.get("TIINGO_API_KEY") or "").strip()
    if not _key:
        print("TIINGO_API_KEY is not set in the environment.")
        return 1
    print(f"key present, {len(_key)} chars. Budget {REQUEST_BUDGET} requests/hour.\n")

    now = datetime.now(timezone.utc)
    # Two calendar days back covers a weekend-free gap; the pipeline needs the
    # last day or two of 30-minute bars, never more.
    start_2d = (now - timedelta(days=4)).strftime("%Y-%m-%d")

    print("=" * 78)
    print("1. AUTH AND COVERAGE  - does the key work, and which ETFs does IEX know?")
    print("=" * 78)
    status, body, size, took, hdr = get("/api/test")
    show("/api/test", status, body, size, took, hdr)

    # One request, 44 tickers: the cheapest possible coverage answer.
    status, body, size, took, hdr = get("/iex/", tickers=",".join(ETFS))
    show(f"/iex/?tickers=<{len(ETFS)} ETFs>", status, body, size, took, hdr,
         sample=False)
    known: set[str] = set()
    if status == 200 and isinstance(body, list):
        for row in body:
            if isinstance(row, dict) and row.get("ticker"):
                known.add(str(row["ticker"]).upper())
        missing = [t for t in ETFS if t not in known]
        print(f"      known to IEX: {len(known)}/{len(ETFS)}")
        print(f"      MISSING: {missing if missing else 'none'}")
        if body and isinstance(body[0], dict):
            print(f"      fields: {sorted(body[0].keys())}")
            print(f"      sample: {_scrub(json.dumps(body[0]))[:300]}")

    print()
    print("=" * 78)
    print("2. FX ON A FREE KEY   - the question that decides where 8 pairs live")
    print("=" * 78)
    status, body, size, took, hdr = get(
        f"/tiingo/fx/eurusd/prices", startDate=start_2d, resampleFreq="1hour")
    show("/tiingo/fx/eurusd/prices 1hour", status, body, size, took, hdr)
    fx_ok = status == 200 and isinstance(body, list) and bool(body)
    status, body, size, took, hdr = get("/tiingo/fx/top",
                                        tickers=",".join(FX_PAIRS))
    show(f"/tiingo/fx/top?tickers=<{len(FX_PAIRS)} pairs>", status, body, size,
         took, hdr)

    print()
    print("=" * 78)
    print("3. INTRADAY SHAPE AND SIZE  - what a bar looks like, what it costs")
    print("=" * 78)
    # The bandwidth question: default response vs one bounded by startDate.
    status, body, size_capped, took, hdr = get(
        "/iex/SPY/prices", startDate=start_2d, resampleFreq="30min")
    show("SPY 30min startDate=-4d", status, body, size_capped, took, hdr)
    if status == 200 and isinstance(body, list) and body:
        print(f"      fields: {sorted(body[0].keys())}")

    status, body, size_default, took, hdr = get(
        "/iex/SPY/prices", resampleFreq="30min")
    show("SPY 30min NO startDate (default)", status, body, size_default, took,
         hdr, sample=False)
    if isinstance(body, list) and body:
        print(f"      rows={len(body)}  span {body[0].get('date')} .. "
              f"{body[-1].get('date')}")
    if size_capped and size_default:
        print(f"      >>> bounded(4d)={size_capped:,}B  default={size_default:,}B "
              f"  ratio {size_capped / size_default:4.1f}x")
        for label, per in (("bounded", size_capped), ("default", size_default)):
            monthly = per * 33 * 24 * 30 / 1e9
            print(f"      >>> {label}: 33 symbols x 24 runs x 30 days = "
                  f"{monthly:6.3f} GB/month against a 1 GB cap")

    status, body, size, took, hdr = get(
        "/iex/SPY/prices", startDate=start_2d, resampleFreq="1hour")
    show("SPY 1hour startDate=-4d", status, body, size, took, hdr, sample=False)
    if isinstance(body, list) and body:
        print(f"      rows={len(body)}  span {body[0].get('date')} .. "
              f"{body[-1].get('date')}")

    # How far back does the intraday feed go? Ask for far more than it has.
    status, body, size, took, hdr = get(
        "/iex/SPY/prices", startDate="2015-01-01", resampleFreq="30min")
    show("SPY 30min startDate=2015-01-01 (depth)", status, body, size, took,
         hdr, sample=False)
    if isinstance(body, list) and body:
        print(f"      rows={len(body)}  EARLIEST {body[0].get('date')}  "
              f"latest {body[-1].get('date')}")

    print()
    print("=" * 78)
    print("4. THE THIN ONES  - does IEX print enough to build a 30-minute bar?")
    print("=" * 78)
    burst_started = time.monotonic()
    thin_report = {}
    for ticker in THIN:
        status, body, size, took, hdr = get(
            f"/iex/{ticker}/prices", startDate=start_2d, resampleFreq="30min")
        rows = len(body) if isinstance(body, list) else 0
        vols = [r.get("volume") for r in body
                if isinstance(r, dict)] if isinstance(body, list) else []
        zero = sum(1 for v in vols if not v)
        thin_report[ticker] = rows
        flag = "OK " if rows else "EMPTY"
        print(f"  {ticker:<6} {flag} HTTP {status:<4} rows={rows:<4} "
              f"zero-volume={zero:<4} {size:>6,}B {took:5.2f}s"
              + ("" if rows else f"  body={_scrub(json.dumps(body))[:120]}"))
    burst = time.monotonic() - burst_started
    per = burst / max(len(THIN), 1)
    print(f"\n      {len(THIN)} sequential requests in {burst:5.2f}s "
          f"({per:.2f}s each, no throttling applied)")
    print(f"      >>> extrapolated: 33 symbols would take {per * 33:5.1f}s "
          f"sequentially")

    print()
    print("=" * 78)
    print("5. ADJUSTED DAILY  - second opinion on the sector-SPDR dividend step")
    print("=" * 78)
    for ticker in DIVIDEND_SUSPECTS:
        status, body, size, took, hdr = get(
            f"/tiingo/daily/{ticker}/prices", startDate="2024-01-01",
            resampleFreq="daily")
        rows = len(body) if isinstance(body, list) else 0
        print(f"  {ticker:<6} HTTP {status:<4} rows={rows:<5} {size:>7,}B "
              f"{took:5.2f}s")
        if status == 200 and isinstance(body, list) and body:
            if ticker == DIVIDEND_SUSPECTS[0]:
                print(f"      fields: {sorted(body[0].keys())}")
            # A dividend step shows up as a one-day jump in the ratio between
            # the adjusted and unadjusted close. A real one is ~0.5%; the bug
            # is ~2x.
            worst = 0.0
            worst_day = ""
            prev = None
            for row in body:
                close = row.get("close")
                adj = row.get("adjClose")
                if not close or not adj:
                    continue
                ratio = adj / close
                if prev is not None and prev:
                    step = abs(ratio / prev - 1.0)
                    if step > worst:
                        worst, worst_day = step, str(row.get("date"))[:10]
                prev = ratio
            verdict = "SUSPECT" if worst > 0.10 else "clean"
            print(f"      largest one-day adj/close step: {worst:7.4%} on "
                  f"{worst_day}  -> {verdict}")

    print()
    print("=" * 78)
    print(f"SPENT {_spent} of {REQUEST_BUDGET} hourly requests.")
    print(f"FX ON FREE KEY: {'YES' if fx_ok else 'NO'}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

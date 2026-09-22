"""What can Alpaca actually serve this project, on the free plan?

tools/alpaca_compare.py answered one question - does it price the basket the way
the stored bars do - and answered it well: SIP agrees to 0.00 bps on all 44
instruments, IEX disagrees on all fourteen thin commodity funds by up to 60 bps
at the ninetieth percentile, which is one and a half sigma of fabricated move.

This asks the four questions that decide whether Alpaca has a JOB here, given
that the account is free and is staying free:

  DEPTH      how far back does each feed go? A new instrument needs bars to
             2020-02-10, the wall the existing thirteen shallow names sit on.
             Anything reaching that is deep enough; anything reaching 2016 is
             four years deeper than the requirement.
  COVERAGE   which of the candidate instruments exist at all? A ticker that
             returns nothing needs a different provider, and knowing which ones
             before the shopping starts is the whole point.
  FRESHNESS  the hourly run fires at :05 and wants the bar that closed at :00.
             Alpaca documents unsubscribed SIP as reachable only for queries
             ending 15 minutes back. Where is the real line? Measured, because
             the answer decides whether Alpaca can touch the live path at all.
  FX         Alpaca has a forex endpoint. It offers 5Sec, 1Min and 1Day and NO
             hourly - but this pipeline already folds sub-hourly bars, so the
             question is coverage and depth rather than the timeframe.

Read-only. Fetches, measures, prints; writes nothing.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://data.alpaca.markets"
TIMEOUT = 30

# What the basket would gain. Grouped only so the output is readable - every one
# of them is a separate question to the provider.
CANDIDATES = {
    "real estate": ["VNQ", "VNQI", "REM", "RWR", "SCHH", "RWX"],
    "private equity": ["PSP", "BIZD"],
    "govt ex-US": ["BWX", "IGOV", "BNDX"],
    "inflation-linked": ["VTIP", "SCHP", "STIP", "WIP", "LTPZ"],
    "credit": ["VCIT", "VCSH", "IGIB", "USHY", "SHYG", "ANGL", "IBND"],
    "EM debt": ["EMLC", "VWOB", "PCY", "EBND"],
    "securitized": ["VMBS", "SPMB", "LMBS", "JMBS"],
    "treasuries": ["GOVT", "VGIT", "VGLT", "SCHO", "SPTL"],
    "US cyclical": ["KRE", "XRT", "ITB", "IYT", "XME"],
    "US defensive": ["XBI", "XPH", "IHI", "VDC"],
    "US tech": ["SMH", "SOXX", "IGV", "FDN", "CIBR", "SKYY"],
    "developed intl": ["EWJ", "EWG", "EWU", "EWQ", "EWC", "EWA", "EZU"],
    "emerging intl": ["FXI", "EWZ", "EWW", "INDA", "EWY", "EWT", "EZA"],
    "energy": ["DBO", "DBE", "UNL", "USL"],
    "agriculture": ["CANE", "JO", "NIB", "BAL", "COW"],
    "metals": ["LIT", "REMX", "SLX", "JJC", "JJN", "JJU"],
}

# Held today, so the depth sweep has something whose history we can reason about.
DEPTH_PROBES = ["SPY", "GLD", "UNG", "XLK"]

# The eight pairs already in the basket, plus the EM pairs that are the largest
# blind spot in it. Alpaca writes them without the slash.
FX_PAIRS = ["EURUSD", "USDJPY", "GBPUSD", "AUDUSD", "NZDUSD", "USDCHF",
            "USDCAD", "USDCNH", "USDMXN", "USDZAR", "USDBRL", "USDTRY",
            "USDINR", "USDKRW"]

_h: dict[str, str] = {}


def get(path: str, params: dict) -> "tuple[int, dict]":
    r = requests.get(f"{BASE}{path}", params=params, headers=_h, timeout=TIMEOUT)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def bars_for(symbols: list[str], start: str, end: str, feed: str,
             timeframe: str = "1Hour") -> "tuple[int, dict]":
    code, payload = get("/v2/stocks/bars", {
        "symbols": ",".join(symbols), "timeframe": timeframe, "start": start,
        "end": end, "adjustment": "raw", "feed": feed, "limit": 10000})
    return code, (payload.get("bars") or {}) if code == 200 else payload


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def depth(feeds: list[str]) -> None:
    section("1. DEPTH - how far back does each feed serve hourly bars?")
    print("A new instrument needs 2020-02-10. Anything older is surplus.\n")
    years = [2015, 2016, 2018, 2020, 2022, 2024]
    print(f"{'feed':>6} {'ticker':>8}  " + "".join(f"{y:>8}" for y in years))
    print("-" * (16 + 8 * len(years)))
    for feed in feeds:
        for ticker in DEPTH_PROBES:
            row = f"{feed:>6} {ticker:>8}  "
            for y in years:
                s = datetime(y, 6, 1, tzinfo=timezone.utc)
                code, got = bars_for([ticker], iso(s), iso(s + timedelta(days=5)),
                                     feed)
                n = len(got.get(ticker, [])) if code == 200 else 0
                row += f"{(str(n) if code == 200 else f'E{code}'):>8}"
            print(row)


def coverage(feed: str) -> None:
    section(f"2. COVERAGE - which candidates exist on feed '{feed}'?")
    now = datetime.now(timezone.utc)
    start, end = iso(now - timedelta(days=5)), iso(now - timedelta(hours=2))
    missing_all = []
    for group, tickers in CANDIDATES.items():
        code, got = bars_for(tickers, start, end, feed)
        if code != 200:
            print(f"{group:<18} request failed: {code} {str(got)[:80]}")
            continue
        have = [t for t in tickers if got.get(t)]
        missing = [t for t in tickers if not got.get(t)]
        missing_all += missing
        line = f"{group:<18} {len(have)}/{len(tickers)}"
        if missing:
            line += f"   MISSING: {' '.join(missing)}"
        print(line)
    print(f"\nNot served by Alpaca ({len(missing_all)}): "
          + (" ".join(missing_all) if missing_all else "none"))


def freshness(feed: str) -> None:
    section(f"3. FRESHNESS - how recent a bar will feed '{feed}' serve?")
    print("The hourly run fires at :05 and wants the bar that closed at :00,")
    print("so it needs the 5-minute column to answer.\n")
    now = datetime.now(timezone.utc)
    for minutes in (2, 5, 10, 15, 20, 30, 60):
        end = now - timedelta(minutes=minutes)
        code, got = bars_for(["SPY"], iso(end - timedelta(hours=6)), iso(end), feed)
        n = len(got.get("SPY", [])) if code == 200 else 0
        note = "served" if code == 200 else f"REFUSED {code}: {str(got)[:90]}"
        print(f"  end = {minutes:>3} min ago   {note}   bars={n}")


def forex() -> None:
    section("4. FX - coverage and depth on the forex endpoint")
    print("No hourly timeframe exists (5Sec / 1Min / 1Day only), so 1Min would")
    print("have to be folded. First: does it answer at all, and how far back?\n")
    now = datetime.now(timezone.utc)
    code, payload = get("/v1beta1/forex/rates", {
        "currency_pairs": ",".join(FX_PAIRS), "timeframe": "1Min",
        "start": iso(now - timedelta(days=3)), "end": iso(now - timedelta(hours=2)),
        "limit": 10000})
    if code != 200:
        print(f"  recent 1Min request refused: {code} {str(payload)[:200]}")
        return
    rates = payload.get("rates") or {}
    have = [p for p in FX_PAIRS if rates.get(p)]
    missing = [p for p in FX_PAIRS if not rates.get(p)]
    print(f"  1Min, last 3 days: {len(have)}/{len(FX_PAIRS)} pairs answered")
    if missing:
        print(f"  MISSING: {' '.join(missing)}")
    for y in (2016, 2020, 2023):
        s = datetime(y, 6, 1, tzinfo=timezone.utc)
        code, payload = get("/v1beta1/forex/rates", {
            "currency_pairs": "EURUSD,USDMXN", "timeframe": "1Min",
            "start": iso(s), "end": iso(s + timedelta(days=2)), "limit": 1000})
        r = (payload.get("rates") or {}) if code == 200 else {}
        print(f"  {y}: EURUSD={len(r.get('EURUSD', []))} "
              f"USDMXN={len(r.get('USDMXN', []))}"
              + ("" if code == 200 else f"   ({code} {str(payload)[:70]})"))


def main() -> int:
    global _h
    key = (os.environ.get("ALPACA_KEY_ID") or "").strip()
    secret = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    missing = [n for n, v in (("ALPACA_KEY_ID", key),
                              ("ALPACA_SECRET_KEY", secret)) if not v]
    if missing:
        print(f"Not set: {', '.join(missing)}")
        return 1
    _h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    print(f"Probing Alpaca at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC "
          f"on a free plan.")

    feeds = [f.strip() for f in
             (os.environ.get("ALPACA_FEEDS") or "iex,sip").split(",") if f.strip()]
    depth(feeds)
    for feed in feeds:
        coverage(feed)
    for feed in feeds:
        freshness(feed)
    forex()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

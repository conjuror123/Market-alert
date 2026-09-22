"""Does Alpaca price the basket the way the stored bars do - and on which feed?

Same question tools/tiingo_compare.py asks, against a different provider, and
for the same reason: two feeds can both be complete and still disagree about
where a thin ETF closed at 14:00, because they saw different prints. A few basis
points is nothing to a human and a fabricated event to a detector whose hourly
sigma is 20-40 bps. That is the "fires without meaning" failure arriving through
the data rather than through the arithmetic, and it is the one worth paying to
avoid - see docs/concerns-for-later.md, item 3.

WHY THIS ASKS THE QUESTION TWICE. Alpaca serves two feeds and the difference
between them is the whole decision:

  iex   the free plan. One exchange, a low single-digit share of US volume -
        the same kind of feed fifteen thin funds were deliberately moved OFF.
  sip   the consolidated tape, CTA and UTP together, 100% of volume. It is the
        paid plan for fresh data, but historical queries ending far enough in
        the past are documented as reachable without a subscription, and
        "documented" is not "measured". This tool measures it.

So it reports both, per instrument, against the bars already on disk. If `iex`
agrees on the thin names, the free plan is enough. If it does not and `sip`
does, the difference between those two columns is what the subscription buys,
in basis points rather than in adjectives.

Run it in Actions, where the keys and the checked-out store are. Read-only: it
fetches, compares and prints. It writes nothing and touches no stored bar.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from price_monitor.models import Candle
from tremor import bars

BASE = "https://data.alpaca.markets/v2"
TIMEOUT = 30
BARS_DIR = "data/tremor/bars"

# Alpaca answers many symbols in one request, so the whole basket costs a
# handful of calls rather than one per name - a different shape from Tiingo,
# whose 50-an-hour bucket is what forced that tool to stop early.
PAGE_LIMIT = 10000

# Every US-listed instrument in the basket. The FX pairs and the crypto are not
# here because Alpaca does not serve the first at all, and the second already
# comes from the exchange itself.
ETFS = [
    "XLK", "XLF", "XLY", "XLP", "XLE", "XLV", "XLI", "XLB", "XLU", "XLRE",
    "XLC", "SPY", "QQQ", "IWM", "EFA", "EEM", "SHY", "IEI", "IEF", "TLH",
    "TLT", "TIP", "MBB", "LQD", "HYG", "JNK", "EMB", "BKLN", "PFF", "USO",
    "BNO", "UGA", "UNG", "GLD", "SLV", "PPLT", "PALL", "DBB", "CPER", "DBA",
    "CORN", "WEAT", "SOYB", "DBC",
]

# The line between "same feed" and "a different feed that will invent events",
# carried over from tiingo_compare so the two providers are judged identically.
# One sigma of an hourly move is 20-40 bps for these ETFs, so 2 bps of median
# disagreement is under a tenth of a sigma and vanishes into noise the detector
# already tolerates. Past that, the feed starts contributing moves of its own.
SAFE_MEDIAN_BPS = 2.0
SAFE_P90_BPS = 5.0

_headers: dict[str, str] = {}


def fetch_hourly(tickers: list[str], start: str, end: str,
                 feed: str) -> dict[str, pd.DataFrame]:
    """Hourly bars for many symbols at once, folded to the store's own grid.

    `adjustment=raw` on purpose. The store holds raw prints and handles splits
    and ex-dates separately through corporate_actions; asking for an adjusted
    series here would compare two different definitions of the price and read
    the difference as a feed disagreement.
    """
    out: dict[str, list[Candle]] = {t: [] for t in tickers}
    token = None
    while True:
        params = {"symbols": ",".join(tickers), "timeframe": "1Hour",
                  "start": start, "end": end, "adjustment": "raw",
                  "feed": feed, "limit": PAGE_LIMIT}
        if token:
            params["page_token"] = token
        r = requests.get(f"{BASE}/stocks/bars", params=params,
                         headers=_headers, timeout=TIMEOUT)
        if r.status_code in (401, 403):
            raise PermissionError(f"{feed}: {r.status_code} {r.text[:160]}")
        r.raise_for_status()
        payload = r.json()
        for ticker, rows in (payload.get("bars") or {}).items():
            for row in rows or []:
                stamp = pd.Timestamp(row["t"])
                if stamp.tzinfo is None:
                    stamp = stamp.tz_localize("UTC")
                open_time = int(stamp.timestamp())
                out.setdefault(ticker, []).append(Candle(
                    open_time=open_time, open=float(row["o"]),
                    high=float(row["h"]), low=float(row["l"]),
                    close=float(row["c"]), volume=float(row.get("v") or 0.0),
                    close_time=open_time + 3600))
        token = payload.get("next_page_token")
        if not token:
            break
    return {t: (bars.to_hourly(bars.candles_to_frame(c)) if c
                else bars.empty_frame()) for t, c in out.items()}


def compare(fresh: pd.DataFrame, stored: pd.DataFrame) -> "tuple | None":
    """Median, p90 and max absolute disagreement in bps over the shared hours."""
    if fresh.empty or stored.empty:
        return None
    joined = fresh.merge(stored, on="hour_utc", suffixes=("_ap", "_st"))
    # The newest stored hour may still be healing - backfill re-asks it - so it
    # is not evidence about the feed.
    joined = joined[joined["hour_utc"] < int(stored["hour_utc"].max())]
    if joined.empty:
        return None
    bps = ((joined["close_ap"] - joined["close_st"]).abs()
           / joined["close_st"] * 1e4)
    vol_st = joined["volume_st"].sum()
    share = (joined["volume_ap"].sum() / vol_st) if vol_st else float("nan")
    return len(joined), bps.median(), bps.quantile(0.9), bps.max(), share


def main() -> int:
    global _headers
    key = (os.environ.get("ALPACA_KEY_ID") or "").strip()
    secret = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    # Named separately so a half-configured pair says WHICH half. Alpaca
    # authenticates with both or neither, and "401" on its own sends the reader
    # hunting through the account rather than through the secrets.
    missing = [n for n, v in (("ALPACA_KEY_ID", key),
                              ("ALPACA_SECRET_KEY", secret)) if not v]
    if missing:
        print(f"Not set: {', '.join(missing)}")
        print("Both are needed - Alpaca sends them as APCA-API-KEY-ID and "
              "APCA-API-SECRET-KEY.")
        return 1
    print(f"ALPACA_KEY_ID set ({key[:2]}…{key[-2:]}, {len(key)} chars), "
          f"ALPACA_SECRET_KEY set ({len(secret)} chars).\n")
    _headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    wanted = [t.strip().upper() for t in
              (os.environ.get("ALPACA_TICKERS") or "").split(",") if t.strip()]
    tickers = [t for t in (wanted or ETFS) if t in ETFS]
    if not tickers:
        print("No known tickers requested.")
        return 1

    days = int(os.environ.get("ALPACA_DAYS") or 30)
    now = datetime.now(timezone.utc)
    # An hour old at least, so nothing here is a partially-formed bar, and well
    # past the 15 minutes the documentation puts on unsubscribed SIP queries.
    end = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Comparing Alpaca against the stored bars, {start} .. {end}.")
    print("bps: absolute difference between the two feeds' hourly close, in "
          "basis points.\n")

    feeds = [f.strip() for f in
             (os.environ.get("ALPACA_FEEDS") or "iex,sip").split(",") if f.strip()]
    results: dict[str, dict] = {}
    for feed in feeds:
        try:
            fetched = fetch_hourly(tickers, start, end, feed)
        except PermissionError as exc:
            print(f"feed {feed}: refused - {exc}")
            print("  (that is the answer for this feed, not a failure of the run)\n")
            continue
        except Exception as exc:
            print(f"feed {feed}: fetch failed - {str(exc)[:160]}\n")
            continue
        rows = {}
        for ticker in tickers:
            stored = bars.load(bars.store_path(BARS_DIR, f"twelvedata_{ticker}"))
            got = compare(fetched.get(ticker, bars.empty_frame()), stored)
            if got:
                rows[ticker] = got
        results[feed] = rows
        print(f"feed {feed}: {len(rows)} of {len(tickers)} instruments overlapped")
    print()

    if not results:
        print("Neither feed returned anything comparable.")
        return 1

    head = f"{'ticker':<8}" + "".join(
        f"{f + '_med':>11}{f + '_p90':>11}" for f in results) + "   verdict"
    print(head)
    print("-" * len(head))
    verdicts: dict[str, list[str]] = {f: [] for f in results}
    for ticker in tickers:
        if not any(ticker in rows for rows in results.values()):
            continue
        line, ok_on = f"{ticker:<8}", []
        for feed, rows in results.items():
            got = rows.get(ticker)
            if not got:
                line += f"{'-':>11}{'-':>11}"
                continue
            _n, med, p90, _mx, _share = got
            line += f"{med:>11.2f}{p90:>11.2f}"
            if med <= SAFE_MEDIAN_BPS and p90 <= SAFE_P90_BPS:
                ok_on.append(feed)
                verdicts[feed].append(ticker)
        print(line + "   " + (", ".join(ok_on) if ok_on else "neither"))

    print("\n" + "=" * 78)
    for feed, rows in results.items():
        ok = verdicts[feed]
        meds = sorted(r[1] for r in rows.values())
        shares = [r[4] for r in rows.values() if r[4] == r[4]]
        print(f"\nFEED {feed.upper()}: {len(ok)} of {len(rows)} instruments agree "
              f"with the store")
        if meds:
            print(f"  median disagreement across all measured: "
                  f"{meds[len(meds) // 2]:.2f} bps")
        if shares:
            print(f"  volume share against the stored bars: "
                  f"{sum(shares) / len(shares):.1%}")
        bad = [t for t in rows if t not in ok]
        if bad:
            print(f"  disagrees: {' '.join(sorted(bad))}")
    print(f"\nThreshold: median <= {SAFE_MEDIAN_BPS} bps and p90 <= "
          f"{SAFE_P90_BPS} bps, the same line tiingo_compare holds a feed to.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

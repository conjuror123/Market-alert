"""One-off: rebuild a continuous future's store from Yahoo.

Yahoo keeps hourly bars for 730 days and serves only the contracts still
listed. So the store is Yahoo's continuous series back to 2024-05, cleaned
once of its other-contract prints (jump.futures.clean_history), with every
still-listed contract's own bars laid over its own front window
(futures.roll_days) - those weeks are then exactly what the live fetch would
have stored. Coffee, cocoa and cotton are built from Dukascopy instead
(tools/dukascopy_futures.py), which leaves live cattle.

NOT TO BE RE-RUN FOR LIVE CATTLE as it stands. Since cattle rolls twelve
business days before its delivery month (2026-10-07), the stretches where
Yahoo's series could lag its roll are most of every cycle, and every one no
still-listed contract fills would become a hole - and Yahoo's series in fact
left the expiring contract early in some of them (2026-09-15, 2025-06-02) and
not in others (2026-09-01). Its import of 2026-10-02 stands, with its two
latest months laid over by December's own bars.

Rewrites the named stores under data/jump/bars, so they must be named. Run
from the repository root:
    python -m tools.futures_history LE=F
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone

from price_monitor import yahoo
from jump import bars, futures, sessions
from jump.basket import load_basket


def chart(symbol: str):
    url = f"{yahoo.BASE_URL}{yahoo.CHART_ENDPOINT.format(symbol=symbol)}"
    try:
        return yahoo._request(None, url, {"interval": "1h", "range": "730d"}, 3600, symbol)
    except Exception as exc:                       # an expired contract is a 404
        print(f"   {symbol}: {exc}")
        return []


# Rebuilt from Dukascopy's CFDs, which hold one contract at a time; Yahoo's
# continuous series interleaves two around a roll (docs/decisions.md).
REBUILT_FROM_DUKASCOPY = {"KC=F", "CT=F", "CC=F"}


def main(argv: "list[str] | None" = None) -> int:
    buildable = {a.ticker: a for a in load_basket().instruments
                 if futures.is_continuous(a.ticker) and a.ticker not in REBUILT_FROM_DUKASCOPY}
    parser = argparse.ArgumentParser(
        description="Rebuild continuous futures stores from Yahoo (rewrites them).")
    parser.add_argument("tickers", nargs="+", metavar="TICKER",
                        help=f"one of {', '.join(sorted(buildable))}")
    args = parser.parse_args(argv)
    for ticker in args.tickers:
        if ticker not in buildable:
            why = (" - built from Dukascopy (tools/dukascopy_futures.py)"
                   if ticker in REBUILT_FROM_DUKASCOPY else "")
            parser.error(f"{ticker} is not a future this tool builds{why}")
    today = datetime.now(timezone.utc).date()
    for ticker in args.tickers:
        asset = buildable[ticker]
        frame = bars.to_hourly(bars.candles_to_frame(chart(asset.ticker)))
        if not futures.SPECS.get(asset.ticker, {}).get("continuous_history", True):
            frame = frame.iloc[0:0]
        frame, dropped, stray, sparse = futures.clean_history(frame)
        print(f"{asset.ticker}: {len(frame)} hours from the continuous series, "
              f"{dropped} other-contract hours dropped, {stray} stray opens reset, "
              f"sparse months dropped: {' '.join(sparse) or 'none'}")
        frame, mixed = futures.drop_mixed(frame, asset.session_template)
        print(f"   stretches interleaving two contract months dropped: {mixed or 'none'}")
        if asset.ticker in futures.SPECS:
            # Keep only the hours where Yahoo's series is on the contract this
            # series holds; the stretches where it lags are holes, unless a
            # still-listed contract fills them below.
            import pandas as pd
            stamps = pd.to_datetime(frame["hour_utc"], unit="s", utc=True).dt.date
            lag = pd.Series(False, index=frame.index)
            windows = futures.yahoo_lag_windows(asset.ticker, 2024, today.year)
            for lo_day, hi_day in windows:
                lag |= (stamps >= lo_day) & (stamps < hi_day)
            frame = frame[~lag].reset_index(drop=True)
            print(f"   {int(lag.sum())} hours where Yahoo's series lags this one's "
                  f"roll left out ({len(windows)} stretches)")
            fronts = futures.roll_days(asset.ticker, 2024, today.year)
            for k, (symbol, start) in enumerate(fronts):
                if start > today:
                    break
                lo = sessions.daily_session_open(start, asset.session_template)
                hi = (sessions.daily_session_open(fronts[k + 1][1], asset.session_template)
                      if k + 1 < len(fronts) else 10 ** 12)
                own = bars.to_hourly(bars.candles_to_frame(
                    [c for c in chart(symbol) if lo <= c.open_time < hi]))
                if own.empty:
                    continue
                # The contract's own hours replace the continuous series' over
                # the whole window, including hours the contract did not trade.
                frame = frame[(frame["hour_utc"] < lo) | (frame["hour_utc"] >= hi)]
                frame = bars.to_hourly(bars.candles_to_frame([])) if frame.empty else frame
                import pandas as pd
                frame = pd.concat([frame, own]).sort_values("hour_utc").reset_index(drop=True)
                print(f"   {symbol}: front from {start}, {len(own)} of its own hours")
        path = bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem)
        shutil.rmtree(path, ignore_errors=True)
        bars.merge(path, frame)
        print(f"   stored {len(bars.load(path))} hours")
    return 0


if __name__ == "__main__":
    sys.exit(main())

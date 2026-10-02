"""One-off: (re)build the continuous futures stores from Yahoo.

Yahoo keeps hourly bars for 730 days and serves only the contracts still
listed. So each store is Yahoo's continuous series (KC=F, ...) back to 2024-05,
cleaned once of its other-contract prints (tremor.futures.clean_history), with
every still-listed contract's own bars laid over its own front window
(futures.roll_days) - those weeks are then exactly what the live fetch would
have stored. Aluminium (ALI=F) is Yahoo's series as it is: not rolled here, and
no flips in it to clean.

Rewrites the five stores under data/tremor/bars. Run from the repository root:
    python -m tools.futures_history
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone

from price_monitor import yahoo
from tremor import bars, futures, sessions
from tremor.basket import load_basket


def chart(symbol: str):
    url = f"{yahoo.BASE_URL}{yahoo.CHART_ENDPOINT.format(symbol=symbol)}"
    try:
        return yahoo._request(None, url, {"interval": "1h", "range": "730d"}, 3600, symbol)
    except Exception as exc:                       # an expired contract is a 404
        print(f"   {symbol}: {exc}")
        return []


# Rebuilt from Dukascopy's CFDs, which hold one contract at a time; Yahoo's
# continuous series interleaves two around a roll (docs/decisions.md).
REBUILT_FROM_DUKASCOPY = {"KC=F", "CT=F"}


def main() -> int:
    today = datetime.now(timezone.utc).date()
    for asset in load_basket().instruments:
        if not futures.is_continuous(asset.ticker):
            continue
        if asset.ticker in REBUILT_FROM_DUKASCOPY:
            print(f"{asset.ticker}: history from Dukascopy (tools/dukascopy_futures.py); skipped")
            continue
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

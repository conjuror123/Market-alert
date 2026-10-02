"""One-off: build Shanghai tin's hourly history from Sina, contract by contract.

Sina serves each delivery month's last 1,023 hourly bars - about seven months -
back to the January 2020 contract (bars from 2019-07). Each day is taken from
the contract that traded most that day (the main contract, as the exchange's
own continuous series would hold); tin's neighbouring contracts sit 0-5 bp
apart, so the day the main contract changes carries no move of its own. The
continuous main contract (SN0) is then laid over its own last 1,023 hours,
which is what the live fetch stores.

Rewrites the store under data/tremor/bars. Run from the repository root:
    python -m tools.sina_history
"""
from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime, timezone

import pandas as pd

from price_monitor import sina
from tremor import bars
from tremor.basket import load_basket

FIRST_YEAR = 2020
# The first contracts Sina serves reach into 2019 with a few bars on a few lots
# (a -14.6% hour on four, 2019-07-02); from August the main contract trades.
START = "2019-08-01"


def main() -> int:
    asset = next(a for a in load_basket().instruments if a.ticker == "SN0")
    today = datetime.now(timezone.utc)
    frames = []
    for year in range(FIRST_YEAR, today.year + 2):
        for month in range(1, 13):
            symbol = f"SN{year % 100:02d}{month:02d}"
            candles = sina.fetch_bars(symbol)
            time.sleep(0.5)
            if candles:
                f = bars.candles_to_frame(candles).assign(contract=symbol)
                frames.append(f)
    every = pd.concat(frames, ignore_index=True)
    every["day"] = pd.to_datetime(every["hour_utc"], unit="s", utc=True) \
        .dt.tz_convert("Asia/Shanghai").dt.date
    volume = every.groupby(["day", "contract"])["volume"].sum().reset_index()
    main = (volume.sort_values("volume").groupby("day").tail(1)
            .set_index("day")["contract"].sort_index())
    chosen = every[every["contract"] == every["day"].map(main)]
    history = bars.to_hourly(chosen[list(bars.SCHEMA)])
    history = history[history["hour_utc"] >= int(pd.Timestamp(START, tz="Asia/Shanghai").timestamp())]
    live = bars.to_hourly(bars.candles_to_frame(sina.fetch_bars("SN0")))
    history = pd.concat([history[history["hour_utc"] < live["hour_utc"].min()], live])
    path = bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem)
    shutil.rmtree(path, ignore_errors=True)
    bars.merge(path, history)
    stored = bars.load(path)
    switches = (main != main.shift()).sum()
    print(f"{len(frames)} contracts, {len(main)} days, main contract changed {switches} "
          f"times; stored {len(stored)} hours from "
          f"{pd.to_datetime(stored['hour_utc'].min(), unit='s').date()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

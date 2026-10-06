"""One-off: the real Sunday opens of the major pairs' 2012 weekends, from Dukascopy.

WHY. The majors' bars from 2012 to 2019 came from a source whose every bar opens
at the previous bar's close. Inside the week that costs nothing - an hour's move
is close to close - but in 2012 it also stitched the Sunday open to Friday's
close: all 52 weekends of the seven majors read as a gap of exactly 0. Half a
year of them collapsed the weekend yardstick, and early 2013 read ordinary
weekends as 30-72 sigma (USD/CAD 2013-02-17: 0.09% flagged extreme).

THE REPAIR. A weekend is mended when its Sunday bar opens exactly at Friday's
close. Dukascopy's own weekend gap is kept, spliced onto the store's Friday:

    open = Dukascopy's Sunday open * (stored Friday close / Dukascopy's Friday close)

and only where Dukascopy's Friday and Sunday closes are both within GATE_BP of
the stored ones, so the two feeds are known to agree on either side. The high
and low widen to hold the new open. Nothing else in the bar changes, and no
other bar is touched.

Rewrites settled months of the store; run only on the instruments named, from
the repository root:
    python -m tools.fx_weekend_opens EUR/USD USD/JPY ... --year 2012
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

import numpy as np
import pandas as pd
import requests

from jump import bars
from jump.basket import load_basket
from price_monitor import dukascopy

WEEKEND_HOURS = 40         # a pause this long between two bars is a weekend
GATE_BP = 10.0


def stitched_weekends(frame: pd.DataFrame, year: int) -> pd.DataFrame:
    """(friday_hour, friday_close, sunday_hour) of each weekend in `year` whose
    Sunday bar opens exactly at Friday's close."""
    f = frame.sort_values("hour_utc").reset_index(drop=True)
    h = f["hour_utc"].to_numpy(dtype="int64")
    after = (h[1:] - h[:-1]) >= WEEKEND_HOURS * 3600
    same = np.isclose(f["open"].to_numpy()[1:], f["close"].to_numpy()[:-1], rtol=0, atol=1e-12)
    years = pd.to_datetime(h[1:], unit="s").year == year
    k = np.flatnonzero(after & same & years)
    return pd.DataFrame({"friday_hour": h[k], "friday_close": f["close"].to_numpy()[k],
                         "sunday_hour": h[k + 1]})


def mend(frame: pd.DataFrame, weekends: pd.DataFrame, duk: pd.DataFrame,
         gate_bp: float = GATE_BP) -> "tuple[pd.DataFrame, int]":
    """The Sunday bars with their open from Dukascopy, and how many weekends
    were refused by the gate."""
    stored = frame.set_index("hour_utc")
    theirs = duk.set_index("hour_utc")
    rows, refused = [], 0
    for w in weekends.itertuples(index=False):
        fri, sun = int(w.friday_hour), int(w.sunday_hour)
        if fri not in theirs.index or sun not in theirs.index:
            refused += 1
            continue
        apart = [abs(np.log(theirs.at[t, "close"] / stored.at[t, "close"])) * 1e4
                 for t in (fri, sun)]
        if max(apart) > gate_bp:
            refused += 1
            continue
        bar = stored.loc[sun].copy()
        opened = float(theirs.at[sun, "open"] * w.friday_close / theirs.at[fri, "close"])
        bar["open"] = opened
        bar["high"] = max(float(bar["high"]), opened)
        bar["low"] = min(float(bar["low"]), opened)
        rows.append(bar.rename(sun))
    out = pd.DataFrame(rows).rename_axis("hour_utc").reset_index() if rows else frame.iloc[:0]
    return out, refused


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--year", type=int, default=2012)
    args = parser.parse_args(argv)
    assets = {a.ticker: a for a in load_basket().instruments}
    session = requests.Session()
    for ticker in args.tickers:
        asset, symbol = assets[ticker], dukascopy.symbol_for(ticker)
        if symbol is None:
            print(f"{ticker}: Dukascopy does not serve it; left as it is")
            continue
        path = bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem)
        frame = bars.load(path)
        weekends = stitched_weekends(frame, args.year)
        if weekends.empty:
            print(f"{ticker}: no stitched weekend in {args.year}")
            continue
        first = pd.to_datetime(int(weekends["friday_hour"].min()), unit="s").date()
        last = pd.to_datetime(int(weekends["sunday_hour"].max()), unit="s").date()
        duk = bars.candles_to_frame(dukascopy.fetch_history(
            symbol, first, date.fromordinal(last.toordinal() + 1), session))
        patch, refused = mend(frame, weekends, duk)
        bars.merge(path, patch, revise_settled=True)
        gaps = np.log(patch["open"].to_numpy() / weekends.set_index("sunday_hour").loc[
            patch["hour_utc"], "friday_close"].to_numpy()) * 1e4
        print(f"{ticker}: {len(weekends)} stitched weekends in {args.year}, {len(patch)} "
              f"mended, {refused} refused by the gate; mended gaps median "
              f"{np.median(np.abs(gaps)):.1f} bp")
    return 0


if __name__ == "__main__":
    sys.exit(main())

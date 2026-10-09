"""One-off: the US funds' first bar of the day, 2016-2022, mended from Alpaca's tape.

    python -m tools.tape_mend TAPE_DIR [--bars-dir data/jump/bars] [--official-dir DIR]

TAPE_DIR is what tools/tape_bars.py saved (the backfill workflow's tape-bars
mode). Yahoo's daily bars give each day's official open: read from DIR when it
holds the fund's (<file_stem>.parquet: day, o, c), fetched otherwise.

WHAT WAS WRONG. In the funds' store, mostly 2020-2022 (Twelve Data's history),
the first bar of a day often did not open where the market did: at the previous
close - a stale open, so the night read 0 and the first hour carried it - or
15 bp or more off elsewhere: IHI at its pre-split price on 2021-07-19, UNG next
to the previous close on 2021-11-29 when it opened 9% lower. Against the tape,
3,599 stale days and about 13,000 others, 2016-2022; the official open sides
with the tape on about 95% of both (docs/decisions.md).

WHICH DAYS. A day is mended when all of these hold:
  - the store and the tape agree on its first bar's close and on the previous
    close, within 10 bp: the same prices otherwise;
  - the store's open is stale (within 2 bp of the previous close, the tape's
    15 bp or more from it) or 15 bp or more from the tape's;
  - the official open is within 5 bp of the tape's, and Yahoo's close within
    10 bp of the store's last of the day;
  - the tape's open lies inside the store's own first bar (2 bp of slack). A
    lone stray print is not an open, and the official open repeats it, being
    taken from the same tape: CPER traded once at 13.01 on 2016-08-22, then at
    14.10 all hour, where the store's bar sat;
  - for a bond fund (credit, rates), the store's open is not an ordinary trade
    of the tape's half hour (strictly inside its range) unless stale.

WHICH OPEN IS RIGHT was judged apart from both: a fund's night predicted from
its most similar funds' nights that morning, where their store and tape agree.
Taking the stale mornings (the store certainly wrong) as the yardstick at the
same distance between the two opens, the tape's open is the right one on about
88% of the mornings whose stored open the tape never traded at, 92% where it
is the tape's high or low, and 85% where both are ordinary trades - 94% in the
equity funds, about half in the bond funds, which are therefore left.

THE MEND. The tape's open, high and low; the stored close is kept (the two agree
within 10 bp) and the range widened to hold it. Only the year files holding a
mended bar are rewritten. The hourly run does not see a revised old bar
(jump.pipeline); the note in config/basket.yaml that records this mend is the
edit that rebuilds the metrics once.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from jump import atomic, bars, quality, sessions
from jump.basket import load_basket

log = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
CLOSES_AGREE = 10e-4       # the store's and the tape's closes, and Yahoo's
STALE = 2e-4               # an open this near the previous close is stale...
MOVED = 15e-4              # ...when the tape's is this far from it; and an open this far
                           # from the tape's is wrong whatever it is
OFFICIAL_AGREES = 5e-4     # the official open against the tape's
SLACK = 2e-4               # around the store's first bar's range
BOND_BLOCKS = ("credit", "rates")
YAHOO_DAYS = 9300


def first_bars(asset, frame: pd.DataFrame, table) -> pd.DataFrame:
    """Per New York day: the first in-session hour and its bar, the day's last
    close, and the previous day's (C0)."""
    frame = frame[~bars.removed(frame).to_numpy()]
    frame = frame[quality.in_session(asset, frame.hour_utc.astype("int64"), table).to_numpy(bool)]
    local = pd.to_datetime(frame.hour_utc.astype("int64"), unit="s", utc=True).dt.tz_convert(NY)
    g = frame.assign(day=local.dt.date.values).groupby("day")
    out = pd.DataFrame({"hour": g.hour_utc.first().astype("int64"), "O1": g.open.first(),
                        "H1": g.high.first(), "L1": g.low.first(), "C1": g.close.first(),
                        "Clast": g.close.last()})
    out["C0"] = out.Clast.shift(1)
    return out


def to_mend(asset, store: pd.DataFrame, tape: pd.DataFrame, official: pd.DataFrame,
            table) -> pd.DataFrame:
    """The first bars to mend: hour_utc and the tape's open, high and low."""
    s = first_bars(asset, store, table)
    t = first_bars(asset, bars.to_hourly(tape), table).rename(columns=lambda c: "t" + c)
    m = s.join(t, how="inner").dropna(subset=["C0", "tC0"])
    m = m[m.hour == m.thour].reset_index()
    m = m.merge(official, on="day", how="inner")
    gs, gt = np.log(m.O1 / m.C0), np.log(m.tO1 / m.C0)
    same = ((np.log(m.C1 / m.tC1).abs() <= CLOSES_AGREE)
            & (np.log(m.C0 / m.tC0).abs() <= CLOSES_AGREE)
            & (np.log(m.c / m.Clast).abs() <= CLOSES_AGREE))
    stale = (gs.abs() <= STALE) & (gt.abs() >= MOVED)
    wrong = stale | ((gs - gt).abs() >= MOVED)
    if asset.block in BOND_BLOCKS:
        ordinary = (m.O1 < m.tH1 * (1 - SLACK)) & (m.O1 > m.tL1 * (1 + SLACK))
        wrong = stale | (wrong & ~ordinary)
    official_agrees = np.log(m.tO1 / m.o).abs() <= OFFICIAL_AGREES
    inside = (m.tO1 <= m.H1 * (1 + SLACK)) & (m.tO1 >= m.L1 * (1 - SLACK))
    m = m[same & wrong & official_agrees & inside]
    return pd.DataFrame({"hour_utc": m.hour.to_numpy(), "open": m.tO1.to_numpy(),
                         "high": m.tH1.to_numpy(), "low": m.tL1.to_numpy()})


def apply(store: str, mend: pd.DataFrame) -> int:
    """Writes `mend` into the year files holding its hours, and touches no other."""
    years = pd.to_datetime(mend.hour_utc, unit="s", utc=True).dt.year
    done = 0
    for year, part in mend.groupby(years.to_numpy()):
        path = os.path.join(store, f"{year}.parquet")
        if not os.path.exists(path):
            raise FileNotFoundError(f"{path}: a mended hour outside a settled year")
        frame = (pd.read_parquet(path).astype(bars.SCHEMA)
                 .sort_values("hour_utc").reset_index(drop=True))
        at = frame.hour_utc.isin(part.hour_utc).to_numpy()
        if at.sum() != len(part):
            raise ValueError(f"{path}: {len(part) - at.sum()} mended hours not stored")
        new = part.set_index("hour_utc").loc[frame.hour_utc[at]]
        close = frame.loc[at, "close"].to_numpy()
        frame.loc[at, "open"] = new.open.to_numpy()
        frame.loc[at, "high"] = np.maximum(new.high.to_numpy(), close)
        frame.loc[at, "low"] = np.minimum(new.low.to_numpy(), close)
        atomic.write_parquet(path, frame)
        done += int(at.sum())
    return done


def official_days(ticker: str, session=None) -> pd.DataFrame:
    """Yahoo's daily bars: the official open and close, per New York day."""
    from price_monitor import yahoo

    candles = yahoo.fetch_full_history(ticker, "1d", YAHOO_DAYS, session=session)
    return pd.DataFrame({"day": [datetime.fromtimestamp(c.open_time, NY).date() for c in candles],
                         "o": [c.open for c in candles],
                         "c": [c.close for c in candles]}).drop_duplicates("day")


def main(argv: "list[str] | None" = None) -> int:
    import requests

    parser = argparse.ArgumentParser(description="The funds' first bars mended from Alpaca's tape")
    parser.add_argument("tape_dir")
    parser.add_argument("--bars-dir", default=os.path.join("data", "jump", "bars"))
    parser.add_argument("--official-dir", default="")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    table = sessions.load_sessions()
    session = requests.Session()
    total, by_year = 0, {}
    for asset in load_basket().instruments:
        tape_path = os.path.join(args.tape_dir, f"{asset.file_stem}.parquet")
        if asset.session_template != "us_equity" or not os.path.exists(tape_path):
            continue
        store = bars.store_path(args.bars_dir, asset.file_stem)
        saved = os.path.join(args.official_dir, f"{asset.file_stem}.parquet")
        if args.official_dir and os.path.exists(saved):
            official = pd.read_parquet(saved, columns=["day", "o", "c"])
            official["day"] = pd.to_datetime(official.day).dt.date
        else:
            official = official_days(asset.ticker, session)
            time.sleep(0.4)
        mend = to_mend(asset, bars.load(store), pd.read_parquet(tape_path), official, table)
        if not mend.empty:
            total += apply(store, mend)
            for y, n in pd.to_datetime(mend.hour_utc, unit="s", utc=True).dt.year.value_counts().items():
                by_year[y] = by_year.get(y, 0) + int(n)
        log.info("%s: %d first bars mended", asset.ticker, len(mend))
    print(f"{total} first bars mended: " + ", ".join(f"{y} {n}" for y, n in sorted(by_year.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

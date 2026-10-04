"""One-off: a soft commodity's history rebuilt from Dukascopy's CFD.

WHY. Yahoo's continuous series, which the first history came from
(tools/futures_history.py), switches between two contract months within days:
on 2026-07-31, 08-03 and 08-05 coffee's held December's prices and September's
on the days between, and the store showed it as runs of same-hour moves the
size of the spread (docs/decisions.md, "Open questions"). Dukascopy's CFD holds one
contract at a time and switches once - coffee onto December on 2026-08-11,
matching KCZ26.NYB to 0 bp from that day - and reaches back to 2018.

THE SEAM. Below it the record is the CFD's, above it the store's own: the
listed contract the series was on, bar for bar. It is where this series rolled
into a contract the CFD was already on and the store holds clean - coffee
2026-08-14 (KCZ26), cotton 2026-06-17 (CTZ26), cocoa 2026-08-11 (CCZ26). The
CFD is quoted in hundredths of the contract's unit; the splice is refused unless, over the SEAM_CHECK
days after the seam, the two pass the gates every splice passes
(tremor.backfill.verify_alignment). Volume, the CFD's tick count, is scaled to
the contracts' median over those days, so the thin-bar rule (tremor.futures)
reads both sides alike.

STRAY CLOSES - a session's last hour closing far off and the next session
opening back where it was (coffee 2018-05-07 and 05-21, cocoa 2018-02-09) - are
set to that next open first (futures.reset_stray_closes).

THE CFD'S OWN ROLLS do not follow this series' calendar: they come 2 to 12
business days before it. Each is looked for in the weeks before each of this
series' rolls, [roll - ROLL_BEFORE, roll + ROLL_AFTER] business days, as the
largest gap between two sessions there. Where it stands out - ROLL_CLEAR times
the next largest and ROLL_TYPICAL times a typical session's gap - its session
goes into data/tremor/rolls.csv and its opening gap is left unscored
(futures.roll_sessions); every bar is kept. Where none stands out the whole
window is dropped: whichever night the switch fell on is then inside a hole too
long for a gap to be scored across.

Input: the CSVs tools/dukascopy_dump.py writes (alpaca-probe.yml,
only=dukascopy-dump). Rewrites the store under data/tremor/bars. Run from the
repository root:
    python -m tools.dukascopy_futures KC=F path/to/dumps
"""
from __future__ import annotations

import glob
import os
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from tremor import bars, futures, sessions
from tremor.backfill import verify_alignment
from tremor.basket import load_basket

SPEC = {
    "KC=F": dict(symbol="COFFEECMDUSX", seam=date(2026, 8, 14)),
    "CT=F": dict(symbol="COTTONCMDUSX", seam=date(2026, 6, 17)),
    "CC=F": dict(symbol="COCOACMDUSD", seam=date(2026, 8, 11)),
}
SCALE = 100.0                 # the CFD in hundredths of the contract: coffee and cotton dollars a pound to cents, cocoa hundreds of dollars a tonne
SEAM_CHECK = 60
ROLL_BEFORE, ROLL_AFTER = 15, 3
ROLL_CLEAR, ROLL_TYPICAL = 1.8, 5.0


def business_days(day: date, n: int) -> date:
    step = 1 if n >= 0 else -1
    while n:
        day += timedelta(days=step)
        if day.weekday() < 5:
            n -= step
    return day


def load_cfd(symbol: str, folder: str) -> pd.DataFrame:
    raw = pd.concat(pd.read_csv(p) for p in sorted(glob.glob(os.path.join(folder, f"{symbol}-*.csv"))))
    raw = raw.drop_duplicates("hour_utc").sort_values("hour_utc")
    out = pd.DataFrame({
        "hour_utc": raw["hour_utc"].astype("int64"),
        "open": (raw["bid_o"] + raw["ask_o"]) / 2 * SCALE,
        "high": (raw["bid_h"] + raw["ask_h"]) / 2 * SCALE,
        "low": (raw["bid_l"] + raw["ask_l"]) / 2 * SCALE,
        "close": (raw["bid_c"] + raw["ask_c"]) / 2 * SCALE,
        "volume": raw["bid_v"].astype(float),
        "n_src": 1,
    })
    return out.astype(bars.SCHEMA).reset_index(drop=True)


def session_gaps(frame: pd.DataFrame, template: str) -> pd.Series:
    """Each session's opening gap (log, first open over the last close before
    it), indexed by session date."""
    key = frame["hour_utc"].map(lambda h: sessions.session_key(int(h), template))
    f = frame.assign(key=key).dropna(subset=["key"])
    first = f.groupby("key").head(1).set_index("key")
    last = f.groupby("key").tail(1).set_index("key")["close"]
    prev = last.shift(1).reindex(first.index)
    return np.log(first["open"] / prev).dropna()


def main() -> int:
    ticker, folder = sys.argv[1], sys.argv[2]
    spec = SPEC[ticker]
    asset = next(a for a in load_basket().instruments if a.ticker == ticker)
    template = asset.session_template
    path = bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem)
    store = bars.load(path)
    cfd = load_cfd(spec["symbol"], folder)
    seam = int(datetime.combine(spec["seam"], datetime.min.time(), timezone.utc).timestamp())

    after = store[(store["hour_utc"] >= seam) & (store["hour_utc"] < seam + SEAM_CHECK * 86400)]
    check = verify_alignment(cfd[(cfd["hour_utc"] >= seam) & (cfd["hour_utc"] < seam + SEAM_CHECK * 86400)], after)
    print(f"{ticker}: {spec['symbol']} against the store's {len(after)} hours after "
          f"{spec['seam']}: {check['hours']} shared, correlation {check['correlation']:.4f}, "
          f"median gap {check['median_bp']:.1f} bp")
    if not check["ok"]:
        print(f"   refused: {check['why']}")
        return 1
    shared = cfd.merge(after[["hour_utc", "volume"]], on="hour_utc", suffixes=("", "_store"))
    cfd["volume"] *= float(shared["volume_store"].median() / shared["volume"].median())

    below = cfd[cfd["hour_utc"] < seam].reset_index(drop=True)
    # Before the rolls are looked for: a stray close and the next open that
    # takes it back would otherwise read as a session gap the size of a switch.
    below, strays = futures.reset_stray_closes(below)
    print(f"   {strays} stray closes set to the next open")
    gaps = session_gaps(below, template)
    typical = float(gaps.abs().median())
    first_day = date.fromisoformat(gaps.index.min())
    found, dropped_days = [], 0
    keep = pd.Series(True, index=below.index)
    session_day = below["hour_utc"].map(lambda h: sessions.session_key(int(h), template))
    for _, roll in futures.roll_days(ticker, first_day.year, spec["seam"].year):
        lo, hi = business_days(roll, -ROLL_BEFORE), business_days(roll, ROLL_AFTER)
        # The roll AT the seam counts too: the CFD switched in the weeks
        # before it, which are still the CFD's.
        if roll <= first_day or lo >= spec["seam"]:
            continue
        w = gaps[(gaps.index >= str(lo)) & (gaps.index <= str(hi))].abs().sort_values(ascending=False)
        if len(w) >= 2 and w.iloc[0] >= ROLL_CLEAR * w.iloc[1] and w.iloc[0] >= ROLL_TYPICAL * typical:
            found.append(w.index[0])
            print(f"   roll {roll}: the CFD switched {w.index[0]} ({1e4 * gaps[w.index[0]]:+.0f} bp, "
                  f"next {1e4 * w.iloc[1]:.0f})")
        else:
            out = session_day.between(str(lo), str(hi))
            keep &= ~out.fillna(False)
            dropped_days += 1
            print(f"   roll {roll}: no clear switch; {lo} to {hi} dropped")
    history = below[keep.to_numpy()]
    rebuilt = pd.concat([history, store[store["hour_utc"] >= seam]], ignore_index=True)
    # The whole record, written over the store: shards that did not change are
    # not touched, and months already in git keep their files.
    bars.write(path, rebuilt)

    table = pd.read_csv(futures.ROLLS_PATH, dtype=str) if os.path.exists(futures.ROLLS_PATH) \
        else pd.DataFrame(columns=["ticker", "date", "source"])
    table = table[table["ticker"] != ticker]
    table = pd.concat([table, pd.DataFrame({"ticker": ticker, "date": found,
                                            "source": "dukascopy"})], ignore_index=True)
    table.sort_values(["ticker", "date"]).to_csv(futures.ROLLS_PATH, index=False, lineterminator="\n")
    print(f"{ticker}: {len(history)} hours from the CFD ({len(found)} switches found, "
          f"{dropped_days} windows dropped), {len(rebuilt)} in all from "
          f"{pd.to_datetime(rebuilt['hour_utc'].min(), unit='s'):%Y-%m-%d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Who is right where a fund's stored open and its official open disagree?

Alpaca's consolidated tape referees, 2016 on: its 09:30 half-hour bar opens at
the tape's first eligible trade. For every fund-day where the stored gap (the
store's first print over its previous close) and the official gap (jump.opens:
Yahoo's daily open over its previous close) differ by DISAGREE_BP or more, the
tape's gap is set beside both. A fund-year where the store is the tape itself
(half its first opens equal to the tape's) is counted apart: there the tape
cannot side against the store.

Read-only; prints REFEREE lines. Needs ALPACA_KEY_ID and ALPACA_SECRET_KEY, so
it runs in the Research workflow (only=opens-referee).
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from jump import bars, corporate_actions, opens, quality, sessions
from jump.basket import load_basket
from price_monitor import alpaca

DISAGREE_BP = 15.0
SIDE_BP = DISAGREE_BP / 2
NY = opens.NY


def firsts(candles_or_frame) -> pd.DataFrame:
    """Per New York day: the first bar's open and the last bar's close."""
    f = candles_or_frame
    if not isinstance(f, pd.DataFrame):
        f = pd.DataFrame({"hour_utc": [c.open_time for c in f], "open": [c.open for c in f],
                          "close": [c.close for c in f]})
    if f.empty:
        return pd.DataFrame(columns=["o", "c"])
    day = pd.to_datetime(f["hour_utc"].astype("int64"), unit="s", utc=True).dt.tz_convert(NY).dt.date
    g = f.assign(day=day.values).sort_values("hour_utc").groupby("day")
    return pd.DataFrame({"o": g["open"].first(), "c": g["close"].last()})


def main() -> int:
    auth = alpaca.headers(os.environ.get("ALPACA_KEY_ID", ""), os.environ.get("ALPACA_SECRET_KEY", ""))
    sess = requests.Session()
    table = sessions.load_sessions()
    official = opens.load()
    end = datetime.now(timezone.utc) - timedelta(days=1)
    tally: Counter = Counter()
    rows = []
    for asset in corporate_actions._funds(load_basket()):
        stored = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem),
                           since=int(alpaca.FIRST.timestamp()) - 10 * 86400)
        stored = stored[quality.in_session(asset, stored["hour_utc"].astype("int64"),
                                           table).to_numpy(bool)]
        try:
            tape = alpaca.fetch_history(asset.ticker, alpaca.FIRST, end, auth, sess)
        except Exception as exc:
            print(f"REFEREE,failed,{asset.ticker},{exc}")
            continue
        s, t = firsts(stored), firsts(tape)
        d = s.join(t, rsuffix="_t", how="inner")
        d["prev_c"], d["prev_ct"] = d["c"].shift(1), d["c_t"].shift(1)
        d = d.iloc[1:]
        mine = official.get(asset.ticker, {})
        d["ratio"] = [mine.get(day.isoformat()) for day in d.index]
        d = d.dropna(subset=["ratio", "prev_c", "prev_ct"])
        d["gs"] = np.log(d["o"] / d["prev_c"]) * 1e4
        d["gy"] = np.log(d["ratio"].astype(float)) * 1e4
        d["gt"] = np.log(d["o_t"] / d["prev_ct"]) * 1e4
        year = pd.Series([day.year for day in d.index], index=d.index)
        equal = pd.Series(np.isclose(d["o"], d["o_t"], rtol=1e-9), index=d.index)
        same = equal.groupby(year).mean() >= 0.5
        d["same_feed"] = year.map(same).astype(bool)
        dis = d[(d["gs"] - d["gy"]).abs() >= DISAGREE_BP]
        for day, r in dis.iterrows():
            near = ("official~0" if abs(r['gy']) <= 0.5 * abs(r['gs'])
                    else "stored~0" if abs(r['gs']) <= 0.5 * abs(r['gy']) else "both moved")
            side = ("official" if abs(r['gt'] - r['gy']) < SIDE_BP else
                    "stored" if abs(r['gt'] - r['gs']) < SIDE_BP else "neither")
            key = ("same feed" if r['same_feed'] else "another feed", near, side)
            tally[key] += 1
            rows.append((asset.ticker, day.isoformat(), r['same_feed'], near, side,
                         round(r['gs'], 1), round(r['gy'], 1), round(r['gt'], 1)))
        tally[("fund-days compared",)] += len(d)
        tally[("fund-days, another feed",)] += int((~d["same_feed"]).sum())
    for key, n in sorted(tally.items()):
        print("REFEREE,count," + ",".join(key) + f",{n}")
    frame = pd.DataFrame(rows, columns=["ticker", "day", "same_feed", "near", "side", "gs", "gy", "gt"])
    if not frame.empty:
        other = frame[~frame["same_feed"]]
        by_year = other.groupby([other["day"].str[:4], "side"]).size().unstack(fill_value=0)
        for year, r in by_year.iterrows():
            print(f"REFEREE,year,{year}," + ",".join(f"{k}={v}" for k, v in r.items()))
        for _, r in other[other["side"] != "official"].head(60).iterrows():
            print("REFEREE,case," + ",".join(str(v) for v in r.tolist()))
        for _, r in frame[frame["ticker"] == "LMBS"].tail(5).iterrows():
            print("REFEREE,lmbs," + ",".join(str(v) for v in r.tolist()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

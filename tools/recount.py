"""The separate check on a history replay's dry run (jump.replay --out DIR):
every move it voted not real, counted again from the bars kept with it.

Written apart from the vote on purpose - it does not import jump.verify, and a
test holds it to that - so a mistake in the vote's code is not repeated here.
Its rule is the vote's, said plainly: the store's provider is one vote that
saw the move; each source that voted is another, which saw it if it moved the
same way at least half as far, from its close up to an hour before the move to
its close up to an hour after; a source with no such bars counts against. Most
saw it: real; a tie: uncertain; most did not: not real. It does not bridge a
missing hour as the vote does: a disagreement it finds there is listed, to be
read, not settled.

    python tools/recount.py DIR       prints every disagreement; exit 1 if any
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys

import pandas as pd

# Run as a script from the repository: the bar store's reader is jump's.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

HOUR = 3600
SHARE = 0.5
LAG = HOUR


def _stems() -> "dict[str, str]":
    from jump.basket import load_basket
    return {a.asset_id: a.file_stem for a in load_basket().instruments}


def _store(bars_dir: str, stem: str) -> pd.DataFrame:
    from jump import bars
    return bars.load(bars.store_path(bars_dir, stem))


def _source(out_dir: str, name: str, stem: str) -> "pd.DataFrame | None":
    path = os.path.join(out_dir, "bars", name, f"{stem}.parquet")
    return pd.read_parquet(path) if os.path.exists(path) else None


def saw(v: "pd.DataFrame | None", start: int, hour: int, p: float, check: str,
        from_open: bool) -> bool:
    """Whether a source moved with the store's move `p`, from `start` to `hour`."""
    if v is None or v.empty:
        return False
    close = dict(zip(v["hour_utc"].astype("int64"), v["close"].astype(float)))
    opened = dict(zip(v["hour_utc"].astype("int64"), v["open"].astype(float)))
    if from_open:
        befores = [opened[hour]] if hour in opened else []
    else:
        befores = [close[t] for t in (start - LAG, start) if t in close]
    afters = ([opened[hour]] if check == "open" and hour in opened else []) + \
        [close[t] for t in (hour, hour + LAG) if t in close]
    moves = [math.log(b / a) for a in befores for b in afters if a > 0 and b > 0]
    return any(m * p > 0 and abs(m) >= SHARE * abs(p) for m in moves)


def recount(out_dir: str, bars_dir: str) -> "list[str]":
    """Every not-real vote in the dry run whose count here differs."""
    with open(os.path.join(out_dir, "votes.csv"), encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["verdict"] == "not_real"]
    stems, stores = _stems(), {}
    disagree = []
    for r in rows:
        asset_id, hour, check = r["asset_id"], int(r["hour_utc"]), r["check"]
        stem = stems[asset_id]
        if asset_id not in stores:
            stores[asset_id] = _store(bars_dir, stem).set_index("hour_utc")
        s = stores[asset_id]
        if hour not in s.index:
            disagree.append(f"{asset_id} {hour} {check}: no stored bar")
            continue
        earlier = s.index[s.index < hour]
        if not len(earlier):
            continue
        start = int(earlier.max())
        p = float(r["stored_move"])
        bar = s.loc[hour]
        # A session's first hour is measured from its own open: the stored
        # move tells which, being one or the other.
        from_open = check == "close" and abs(math.log(bar["close"] / bar["open"]) - p) < \
            abs(math.log(bar["close"] / s.loc[start, "close"]) - p)
        names = [n for n in r["verifier"].split(",") if n]
        yes = 1 + sum(saw(_source(out_dir, n, stem), hour if from_open else start, hour, p,
                          check, from_open) for n in names)
        no = 1 + len(names) - yes
        result = "real" if yes > no else "uncertain" if yes == no else "not_real"
        if result != "not_real":
            disagree.append(f"{asset_id} {pd.to_datetime(hour, unit='s')} {check}: the vote "
                            f"says not real, the recount {result} ({yes} for, {no} against)")
    return disagree


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Recount a replay's not-real votes")
    parser.add_argument("out_dir")
    parser.add_argument("--bars-dir", default=os.path.join("data", "jump", "bars"))
    args = parser.parse_args(argv)
    found = recount(args.out_dir, args.bars_dir)
    for line in found:
        print(line)
    print(f"{len(found)} disagreement(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())

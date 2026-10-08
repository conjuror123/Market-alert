"""The history replay: every far move older than its instrument's recount, put
to every source that covers it, by the live vote (jump.verify).

LIVE, a move is voted when it is found and at each session's end until its
closest source no longer reaches it (verify.recount_days); then its vote
stands. Everything older is this: the same vote, with every source at its
whole reach - the live ones (Yahoo's pairs two years, Coinbase from a coin's
listing, Alpaca's tape from 2016) and the ones only history needs (HISTORY):
HF Data's consolidated tape for the funds, Dukascopy for the pairs, Bitstamp
and Bitfinex for the coins, and the softs' listed contracts on Yahoo.

WHO VOTES ON A MOVE. A source covers the hours from its first bar to its last
(as fetched); outside them it is no voter - a coin before its listing there, a
fund before 2016 for the tape. Inside them, no bars around the move is an
outage, a vote against, as live. A source never votes on the bars it supplied
to the store (verify.supplied).

A FIXED POINT. A move voted not real leaves the yardstick, which can lift
another move far enough to be asked about (verify.candidates): each instrument
is voted again until its votes stop changing.

    python -m jump.replay --out DIR       the votes over all history, into DIR:
                                            votes.csv, changes.csv, summary.txt,
                                            and the sources' bars around every
                                            move (bars/); the record untouched
    python -m jump.replay --apply DIR     the reviewed votes into the record:
                                            every vote older than each
                                            instrument's recount is replaced
                                            by the replay's
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import logging
import math
import os
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from jump import bars, verify
from jump.basket import Asset
from jump.verify import HOUR, Source

log = logging.getLogger(__name__)

FIXED_POINT_PASSES = 5
# Around each move, the hours of every source kept with the dry run for the
# separate check (tools/recount.py).
KEPT_AROUND = 36 * HOUR


def _bitfinex(asset: Asset) -> str:
    coin = asset.ticker.split("/")[0]
    return f"t{coin}USD" if len(coin) == 3 else f"t{coin}:USD"


def _contract(asset: Asset) -> "str | None":
    from jump import futures
    return asset.ticker if asset.ticker in futures.SPECS and asset.session_template != \
        "cme_cattle" else None


# The sources only history needs, by class. Never asked live. Cattle has none:
# its store is Yahoo's continuous series, whose bad prints are other contracts'
# bars - a listed contract could be the very source of one.
HISTORY: "dict[str, tuple[Source, ...]]" = {
    "us_equity": (Source("hfdata", "HF Data", 0, "1h", lambda a: a.ticker,
                         keys=("HFDATA_API_KEY",)),),
    "fx": (Source("dukascopy", "Dukascopy", 0, "1h",
                  lambda a: _dukascopy_symbol(a.ticker)),),
    "crypto_24_7": (Source("bitstamp", "Bitstamp", 0, "1h",
                           lambda a: a.ticker.split("/")[0].lower() + "usd"),
                    Source("bitfinex", "Bitfinex", 0, "1h", _bitfinex)),
    # The listed contracts Yahoo still serves, other than the store's front
    # contract of the hour: a coffee shock moves every month.
    "softs": (Source("yahoo_contract", "Yahoo_contract", 0, "1h", _contract,
                     own_rolls=True),),
}

def _dukascopy_symbol(ticker: str) -> "str | None":
    from price_monitor import dukascopy
    return dukascopy.symbol_for(ticker)


def voters(asset: Asset) -> "list[Source]":
    """Every source that may vote on the instrument's history: the live ones
    and its class's history-only ones, without its own provider or a source
    whose keys are not set."""
    group = HISTORY.get(verify._CLASS.get(asset.session_template, asset.session_template), ())
    extra = [s for s in group if s.name != asset.fetched_from and s.symbol(asset)
             and all(os.environ.get(k, "").strip() for k in s.keys)]
    return verify.sources_for(asset) + extra


def _from(name: str, asset: Asset) -> "date | None":
    """Where a source can first vote on the instrument: past the stretches it
    supplied (verify.SUPPLIED), a month early for the seam. None: anywhere."""
    ends = [datetime.fromisoformat(end).date()
            for which, start, end, *check in verify.SUPPLIED.get(name, ())
            if not check and end and (asset.session_template == which if isinstance(which, str)
                                      else asset.ticker in which)]
    return max(ends) - timedelta(days=31) if ends else None


def fetch(src: Source, asset: Asset, stored: pd.DataFrame, session, now: datetime) -> pd.DataFrame:
    """A source's hourly bars of the instrument, over its whole reach."""
    from price_monitor import bitfinex, bitstamp, coinbase, dukascopy

    symbol = src.symbol(asset)
    first = datetime.fromtimestamp(int(stored["hour_utc"].min()), timezone.utc)
    begin = max(first, datetime.combine(_from(src.name, asset) or first.date(),
                                        datetime.min.time(), tzinfo=timezone.utc))
    end = now.replace(minute=0, second=0, microsecond=0)
    if src.name == "hfdata":
        return _hfdata(asset, stored, session)
    if src.name == "dukascopy":
        candles = dukascopy.fetch_history(symbol, begin.date(), end.date(), session)
    elif src.name == "bitstamp":
        candles = bitstamp.fetch_history(symbol, begin, end, session)
    elif src.name == "bitfinex":
        candles = bitfinex.fetch_history(symbol, begin, end, session)
    elif src.name == "coinbase":
        candles = coinbase.fetch_history(symbol, begin, end, session)
    elif src.name == "yahoo_contract":
        return _contracts(asset, session)
    else:
        # A live source at its whole reach.
        days = (now - begin).total_seconds() / 86400
        return verify.fetch_verifier(src.name, symbol, src.interval,
                                     min(days, src.days), session, now)
    return bars.to_hourly(bars.candles_to_frame(candles))


def _hfdata(asset: Asset, stored: pd.DataFrame, session) -> pd.DataFrame:
    """HF Data's consolidated-tape minute bars, folded to the hour and put on
    the store's unadjusted footing, as the deepening that imported them did."""
    from jump import backfill, corporate_actions
    from price_monitor import hfdata

    payload = hfdata.fetch_parquet(backfill.FORMER_TICKERS.get(asset.ticker, asset.ticker),
                                   os.environ.get("HFDATA_API_KEY", "").strip(), session)
    minutes = hfdata.to_minute_frame(payload, backfill.HFDATA_TIMEZONE)
    if minutes.empty:
        return bars.empty_frame()
    steps = corporate_actions.load_steps().get(asset.ticker, [])
    minutes, _ = backfill.unadjust_to_store(minutes, stored, steps)
    return bars.to_hourly(minutes)


def _contracts(asset: Asset, session) -> pd.DataFrame:
    """The softs' listed contracts on Yahoo, one series: at each hour the
    nearest listed contract that is not the store's front contract then."""
    from jump import futures
    from price_monitor import yahoo

    today = datetime.now(timezone.utc).date()
    rolls = futures.roll_days(asset.ticker, today.year - 3, today.year + 2)
    frames = []
    for order, (symbol, _) in enumerate(rolls):
        try:
            candles = yahoo.fetch_full_history(symbol, "1h", days=yahoo.MAX_LOOKBACK_DAYS["1h"],
                                               session=session)
        except Exception as exc:                     # expired: Yahoo forgets it
            log.info("replay: %s %s - %s", asset.ticker, symbol, exc)
            continue
        f = bars.to_hourly(bars.candles_to_frame(candles))
        if not f.empty:
            frames.append(f.assign(order=order, symbol=symbol))
    if not frames:
        return bars.empty_frame()
    allbars = pd.concat(frames, ignore_index=True)
    front = {h: (futures.front_contract(asset.ticker,
                                        datetime.fromtimestamp(int(h), timezone.utc).date())
                 or ("", None))[0] for h in allbars["hour_utc"].unique()}
    allbars = allbars[allbars["symbol"] != allbars["hour_utc"].map(front)]
    out = allbars.sort_values(["hour_utc", "order"]).drop_duplicates("hour_utc")
    return out.drop(columns=["order", "symbol"]).reset_index(drop=True)


def covers(v: "pd.DataFrame | None", c: dict) -> bool:
    """Whether a source's fetched bars span the move: outside them it is no
    voter, inside them a missing bar is an outage."""
    if v is None or v.empty:
        return False
    return int(v["hour_utc"].min()) <= c["prev_hour"] and c["hour"] <= int(v["hour_utc"].max())


def vote(asset: Asset, frame: pd.DataFrame, fetched: "dict[str, pd.DataFrame | None]",
         record: dict, until: int, now: int, table, basket, dividends, settings,
         sources: "list[Source]") -> dict:
    """One pass over the instrument's history before `until`: {key: row}."""
    found = verify.candidates(asset, frame, table, now, int(frame["hour_utc"].min()),
                              basket, dividends, settings, record)
    n_src = dict(zip(frame["hour_utc"].astype("int64").tolist(),
                     frame["n_src"].astype(float).tolist())) if "n_src" in frame else {}
    rolls = {s.name: verify.switches(frame, fetched[s.name], asset.session_template)
             for s in sources if s.own_rolls and fetched.get(s.name) is not None}
    out = {}
    for c in found:
        if c["hour"] >= until:
            continue
        asked = [(s.name, None if verify.crosses(c, rolls.get(s.name, [])) else fetched[s.name])
                 for s in sources
                 if covers(fetched.get(s.name), c) and not verify.supplied(s.name, asset, c, n_src)]
        result, stored, names, moves, seen = verify.judge_all(c, asked)
        out[(asset.asset_id, c["hour"], c["check"])] = verify._row(
            asset, c, result, stored, names, moves, seen, now)
    return out


def replay(instruments, bars_dir: str, table, out_dir: str, session=None,
           now: "datetime | None" = None, record_path: "str | None" = None) -> dict:
    """The dry run: every instrument's votes over its history, written to
    `out_dir` with what changed against the record. The record is untouched."""
    from jump import corporate_actions, jumps
    from jump.basket import load_basket

    now_dt = now or datetime.now(timezone.utc)
    now_ts = int(now_dt.timestamp())
    record = verify.load(record_path)
    basket, dividends, settings = load_basket(), corporate_actions.load_dividends(), jumps.settings()
    os.makedirs(os.path.join(out_dir, "bars"), exist_ok=True)
    votes, until, failed = {}, {}, []
    for asset in instruments:
        sources = voters(asset)
        if not sources:
            continue
        frame = bars.load(bars.store_path(bars_dir, asset.file_stem))
        if frame.empty:
            continue
        until[asset.asset_id] = now_ts - int(verify.recount_days(asset) * 86400)
        fetched = {}
        for src in sources:
            try:
                fetched[src.name] = verify._priced(fetch(src, asset, frame, session, now_dt))
            except Exception as exc:
                log.warning("replay: %s from %s failed - %s", asset.ticker, src.name, exc)
                failed.append(f"{asset.ticker} {src.name}: {exc}")
                fetched[src.name] = None             # no bars: no voter on any hour
        mine = {k: r for k, r in record.items() if k[0] == asset.asset_id}
        older = {k: r for k, r in mine.items() if k[1] < until[asset.asset_id]}
        current = {k: r for k, r in mine.items() if k not in older}
        passes = 0
        while passes < FIXED_POINT_PASSES:
            passes += 1
            new = vote(asset, frame, fetched, {**current, **older}, until[asset.asset_id],
                       now_ts, table, basket, dividends, settings, sources)
            settled = {k: r["verdict"] for k, r in new.items()} == \
                {k: r["verdict"] for k, r in older.items()}
            older = new
            if settled:
                break
        votes.update(older)
        _keep_bars(out_dir, asset, fetched, older)
        log.info("replay: %s %d votes in %d passes", asset.ticker, len(older), passes)
    _write(out_dir, votes, record, until, failed)
    return {"votes": len(votes), "failed": failed}


def _keep_bars(out_dir: str, asset: Asset, fetched: dict, votes: dict) -> None:
    """Every source's bars within KEPT_AROUND of a voted move, for the
    separate check."""
    import numpy as np

    hours = np.array(sorted({k[1] for k in votes}), dtype="int64")
    for name, v in fetched.items():
        if v is None or v.empty or not len(hours):
            continue
        stamps = v["hour_utc"].to_numpy(dtype="int64")
        at = np.clip(np.searchsorted(hours, stamps), 1, len(hours) - 1) if len(hours) > 1 \
            else np.zeros(len(stamps), dtype=int)
        nearest = np.minimum(np.abs(stamps - hours[at]), np.abs(stamps - hours[at - 1])) \
            if len(hours) > 1 else np.abs(stamps - hours[0])
        path = os.path.join(out_dir, "bars", name)
        os.makedirs(path, exist_ok=True)
        v[nearest <= KEPT_AROUND].to_parquet(os.path.join(path, f"{asset.file_stem}.parquet"),
                                             index=False)


def _write(out_dir: str, votes: dict, record: dict, until: dict, failed: list) -> None:
    rows = sorted(votes.values(), key=lambda r: (r["asset_id"], int(r["hour_utc"]), r["check"]))
    with open(os.path.join(out_dir, "votes.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=verify.COLUMNS, lineterminator="\n",
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(out_dir, "until.json"), "w", encoding="utf-8") as f:
        json.dump(until, f, indent=1, sort_keys=True)
    # What changes: every key the record or the replay has in the replayed range.
    keys = set(votes) | {k for k in record if k[0] in until and k[1] < until[k[0]]}
    changes = []
    for k in sorted(keys, key=lambda k: (k[0], k[1], k[2])):
        before = record.get(k, {}).get("verdict", "-")
        after = votes.get(k, {}).get("verdict", "-")
        if before != after:
            r = votes.get(k) or record.get(k)
            changes.append({"asset_id": k[0], "hour_utc": k[1], "check": k[2],
                            "when": datetime.fromtimestamp(k[1], timezone.utc).strftime(
                                "%Y-%m-%d %H:%M"),
                            "before": before, "after": after,
                            "stored_move": r.get("stored_move", ""),
                            "verifier": r.get("verifier", ""), "seen": r.get("seen", ""),
                            "verifier_move": r.get("verifier_move", "")})
    with open(os.path.join(out_dir, "changes.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["asset_id", "hour_utc", "check", "when", "before",
                                          "after", "stored_move", "verifier", "seen",
                                          "verifier_move"], lineterminator="\n")
        w.writeheader()
        w.writerows(changes)
    count = collections.Counter(r["verdict"] for r in votes.values())
    moved = collections.Counter((c["before"], c["after"]) for c in changes)
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"votes: {len(votes)} {dict(count)}\n")
        f.write(f"changed against the record: {len(changes)}\n")
        for (a, b), n in moved.most_common():
            f.write(f"  {a} -> {b}: {n}\n")
        f.write(f"sources that could not be fetched: {len(failed)}\n")
        for line in failed:
            f.write(f"  {line}\n")


def apply(out_dir: str, record_path: "str | None" = None, now: "datetime | None" = None) -> dict:
    """The reviewed votes into the record: for every instrument the replay
    covered, its votes older than the replay's `until` are the replay's."""
    with open(os.path.join(out_dir, "until.json"), encoding="utf-8") as f:
        until = {k: int(v) for k, v in json.load(f).items()}
    replayed = verify.load(os.path.join(out_dir, "votes.csv"))
    record = verify.load(record_path)
    kept = {k: r for k, r in record.items() if not (k[0] in until and k[1] < until[k[0]])}
    merged = {**kept, **replayed}
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    verify.write(merged, now_ts, record_path)
    return {"replaced": len(record) - len(kept), "written": len(replayed)}


def main(argv: "list[str] | None" = None) -> int:
    import requests

    from jump import sessions
    from jump.basket import load_basket

    parser = argparse.ArgumentParser(description="The sources' vote over all history")
    parser.add_argument("--out", help="the dry run: write the votes here")
    parser.add_argument("--apply", help="the reviewed votes from here into the record")
    parser.add_argument("--instruments", default="", help="comma-separated tickers")
    parser.add_argument("--bars-dir", default=bars.DEFAULT_BARS_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if bool(args.out) == bool(args.apply):
        parser.error("one of --out or --apply")
    if args.apply:
        print(apply(args.apply))
        return 0
    wanted = {t.strip() for t in args.instruments.split(",") if t.strip()}
    instruments = [a for a in load_basket().instruments if not wanted or a.ticker in wanted]
    print(replay(instruments, args.bars_dir, sessions.load_sessions(), args.out,
                 requests.Session()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

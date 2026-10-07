"""One pass over the whole history: every flagged reading asked of a second source
that reaches back to it, as if the check (jump.verify) had run from the start.

WHY. The hourly check judges each move inside its second source's reach - Yahoo
54 to 699 days, Sina 30 to 77, MarketWatch 9, Coinbase 365 - so a broken print
older than that was never asked about. It stays flagged, and it sits in its
instrument's yardstick for the next half-year (USD/PLN 2022-07-31: two flat
bars 15% off, an "extreme" weekend). This asks the readings the detector
flags (6 sigma and more) of a source that does reach them:

    coins   Coinbase's dollar pairs, from each coin's listing there
    pairs   Dukascopy's archive, from 2003, for the 14 pairs it serves
    funds   Alpaca's consolidated tape, from 2016 - its keys are in Actions
            only, so this kind runs in the Research workflow
            (only=history-check), which prints what was not seen
    cattle  every single live cattle contract Yahoo still serves (June and
            October 2025, and the listed ones), all asked at once - confirmed
            if any saw the move. The store's history is Yahoo's continuous
            series, which mixed two contract months (2025-04-09: +2.8% night,
            -2.9% first hour; the contracts -0.3% and -0.2%)

THE SAME RULE as the hourly check: jump.verify's candidates, judge_all and
record. A move the source did not see is unconfirmed and kept for good, so the
detector leaves it out of scoring and out of every later yardstick. Nothing in
the bar store changes.

ONLY ANOTHER FEED COUNTS. Where a source served the store itself (the majors'
2003-2011 are Dukascopy's; BCH's early bars are Coinbase's), asking it is
asking the store again. A month is skipped when SAME_FEED_SHARE of the hours
both hold close at the same price.

Run from the repository root, on a jumps.parquet the current code wrote:
    python -m tools.history_check coins --jumps data/jump/jumps.parquet
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from jump import bars, corporate_actions, jumps, sessions, verify
from jump.basket import load_basket

log = logging.getLogger("tools.history_check")

SAME_FEED_SHARE = 0.5
MIN_SHARED = 50


def flagged(table: pd.DataFrame, asset_id: str) -> "set[tuple[int, str]]":
    """(hour, check) of the instrument's flagged readings: an hour's move is the
    `close` check, a night's or weekend's gap the `open` one."""
    mine = table[(table["asset_id"] == asset_id) & table["word"].notna()]
    return {(int(h), verify.CLOSE if r == jumps.HOUR else verify.OPEN)
            for h, r in zip(mine["hour_utc"], mine["reading"])}


def same_feed(store: pd.DataFrame, theirs: pd.DataFrame) -> "bool | None":
    """Whether the source is the store's own feed over these bars; None when
    too few hours are shared to tell."""
    j = store[["hour_utc", "close"]].merge(theirs[["hour_utc", "close"]], on="hour_utc",
                                           suffixes=("_s", "_v"))
    if len(j) < MIN_SHARED:
        return None
    equal = np.isclose(j["close_s"].to_numpy(float), j["close_v"].to_numpy(float),
                       rtol=1e-9, atol=0)
    return bool(equal.mean() >= SAME_FEED_SHARE)


def _source(kind: str, asset):
    """(name, symbol, fetch one month) for the instrument, or None."""
    from price_monitor import coinbase, dukascopy

    def month_bounds(year, month):
        lo = date(year, month, 1)
        # Never past today: a source refuses a span reaching into the future.
        return lo, min(date(year + month // 12, month % 12 + 1, 1), date.today())

    if kind == "coins" and asset.session_template == "crypto_24_7":
        product = coinbase.product_for(asset.ticker)

        def fetch(year, month, sess):
            lo, hi = month_bounds(year, month)
            return coinbase.fetch_history(
                product, datetime.combine(lo, datetime.min.time(), timezone.utc),
                datetime.combine(hi, datetime.min.time(), timezone.utc), sess)
        return "coinbase", product, fetch
    if kind == "funds" and asset.session_template == "us_equity":
        import os

        from price_monitor import alpaca

        auth = alpaca.headers(os.environ.get("ALPACA_KEY_ID", ""),
                              os.environ.get("ALPACA_SECRET_KEY", ""))
        whole: dict = {}

        def fetch(year, month, sess):
            # The tape is asked once for the whole span (about five pages a
            # fund), then cut by month.
            if "all" not in whole:
                whole["all"] = alpaca.fetch_history(
                    asset.ticker, alpaca.FIRST,
                    datetime.now(timezone.utc) - timedelta(days=1), auth, sess)
            lo, hi = month_bounds(year, month)
            a = int(datetime.combine(lo, datetime.min.time(), timezone.utc).timestamp())
            b = int(datetime.combine(hi, datetime.min.time(), timezone.utc).timestamp())
            return [c for c in whole["all"] if a <= c.open_time < b]
        return "alpaca", asset.ticker, fetch
    if kind == "pairs" and asset.session_template == "fx_continuous":
        symbol = dukascopy.symbol_for(asset.ticker)
        if symbol:
            def fetch(year, month, sess):
                lo, hi = month_bounds(year, month)
                return dukascopy.fetch_history(symbol, lo, hi, sess)
            return "dukascopy", symbol, fetch
    return None


def _months_of(c: dict) -> "set[tuple[int, int]]":
    """The months a reading's judgement reads: from STALE_HOURS before where
    its move starts to STALE_HOURS after it ends (the bridges' reach)."""
    pad = (verify.STALE_HOURS + verify.LAG_HOURS) * 3600
    out = set()
    for t in (c["prev_hour"] - pad, c["hour"] + pad):
        when = datetime.fromtimestamp(t, timezone.utc)
        out.add((when.year, when.month))
    return out


def check(kind: str, table: pd.DataFrame, now: datetime, record: dict,
          only: "set[str] | None" = None, session=None,
          cache_dir: "str | None" = None) -> "list[dict]":
    """Every verdict this pass reached, as record rows; `record` is updated.
    Each source month is fetched once (kept under `cache_dir` if given); a
    year whose first month fetched is the store's own feed is skipped whole."""
    import os

    basket = load_basket()
    sessions_table = sessions.load_sessions()
    dividends = corporate_actions.load_dividends()
    settings = jumps.settings()
    now_ts = int(now.timestamp())
    session = session or requests.Session()
    out = []
    for asset in basket.instruments:
        if only and asset.ticker not in only:
            continue
        source = _source(kind, asset)
        if source is None:
            continue
        name, symbol, fetch = source
        want = flagged(table, asset.asset_id)
        if not want:
            continue
        frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
        found = verify.candidates(asset, frame, sessions_table, now_ts,
                                  int(frame["hour_utc"].min()), basket, dividends, settings)
        found = [c for c in found if (c["hour"], c["check"]) in want
                 and (asset.asset_id, c["hour"], c["check"]) not in record]
        counts = {"asked": len(found), "same feed": 0, "no bars": 0}
        months: dict = {}
        same_years: "dict[int, bool]" = {}

        def month(y, m):
            if (y, m) in months:
                return months[(y, m)]
            path = os.path.join(cache_dir, f"{name}_{symbol}_{y}-{m:02d}.parquet") \
                if cache_dir else None
            if path and os.path.exists(path):
                got = pd.read_parquet(path)
            else:
                if (y, m) > (now.year, now.month):
                    got = pd.DataFrame(columns=["hour_utc", "open", "close"])
                else:
                    got = bars.to_hourly(bars.candles_to_frame(fetch(y, m, session)))
                if path:
                    got.to_parquet(path, index=False)
            months[(y, m)] = got
            return got

        for c in sorted(found, key=lambda c: c["hour"]):
            year = datetime.fromtimestamp(c["hour"], timezone.utc).year
            if same_years.get(year):
                counts["same feed"] += 1
                continue
            try:
                parts = [month(y, m) for y, m in sorted(_months_of(c))]
            except Exception as exc:
                log.warning("%s %s from %s failed - %s", asset.ticker,
                            datetime.fromtimestamp(c["hour"], timezone.utc), name, exc)
                counts["no bars"] += 1
                continue
            theirs = verify._priced(pd.concat(parts, ignore_index=True)
                                    .drop_duplicates("hour_utc").sort_values("hour_utc"))
            if theirs.empty:
                counts["no bars"] += 1
                continue
            if year not in same_years:
                lo, hi = int(theirs["hour_utc"].min()), int(theirs["hour_utc"].max())
                ours = frame[(frame["hour_utc"] >= lo) & (frame["hour_utc"] <= hi)]
                same = same_feed(ours, theirs)
                if same is not None:
                    same_years[year] = same
                    log.info("%s %d: %s", asset.ticker, year,
                             "the store's own feed, skipped" if same else "another feed")
                if same:
                    counts["same feed"] += 1
                    continue
            verdict, stored, names, moves = verify.judge_all(c, [(name, theirs)], now_ts)
            row = {"asset_id": asset.asset_id, "hour_utc": c["hour"], "check": c["check"],
                   "verdict": verdict, "provider": asset.fetched_from,
                   "verifier": ",".join(names), "stored_move": f"{stored:.6f}",
                   "verifier_move": ",".join(f"{m:.6f}" for m in moves),
                   "checked_utc": now_ts}
            counts[verdict] = counts.get(verdict, 0) + 1
            record[(asset.asset_id, c["hour"], c["check"])] = row
            out.append(row)
        log.info("%s: %s", asset.ticker, counts)
    return out


def cattle_contracts(asset, chart=None) -> "dict[str, pd.DataFrame]":
    """Every listed contract of the series, from 2024 to next year, that Yahoo
    still serves: symbol -> its hourly bars."""
    from jump import futures
    from tools.futures_history import chart as yahoo_chart

    chart = chart or yahoo_chart
    spec = futures.SPECS[asset.ticker]
    out = {}
    for y, _, code in futures._contracts(spec["listed"], 2024, date.today().year + 1):
        symbol = f"{asset.ticker[:-2]}{code}{y % 100:02d}.{spec['suffix']}"
        got = bars.to_hourly(bars.candles_to_frame(chart(symbol)))
        if not got.empty:
            out[symbol] = verify._priced(got)
        time.sleep(1)
    return out


def check_contracts(table: pd.DataFrame, now: datetime, record: dict,
                    contracts: "dict[str, pd.DataFrame] | None" = None) -> "list[dict]":
    """Live cattle's flagged readings against its single contracts, every one
    with bars around the reading at once. A contract that is the store's own
    feed there (the windows it was laid over the continuous series) is left
    out of that reading, as in check()."""
    basket = load_basket()
    asset = next(a for a in basket.instruments if a.session_template == "cme_cattle")
    want = flagged(table, asset.asset_id)
    if not want:
        return []
    contracts = contracts if contracts is not None else cattle_contracts(asset)
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
    now_ts = int(now.timestamp())
    found = verify.candidates(asset, frame, sessions.load_sessions(), now_ts,
                              int(frame["hour_utc"].min()), basket,
                              corporate_actions.load_dividends(), jumps.settings())
    found = [c for c in found if (c["hour"], c["check"]) in want
             and (asset.asset_id, c["hour"], c["check"]) not in record]
    pad = (verify.STALE_HOURS + verify.LAG_HOURS) * 3600
    out = []
    for c in sorted(found, key=lambda c: c["hour"]):
        lo, hi = c["prev_hour"] - pad, c["hour"] + pad
        ours = frame[(frame["hour_utc"] >= lo - 7 * 86400) & (frame["hour_utc"] <= hi + 7 * 86400)]
        feeds = []
        for symbol, theirs in contracts.items():
            near = theirs[(theirs["hour_utc"] >= lo) & (theirs["hour_utc"] <= hi)]
            if near.empty:
                continue
            week = theirs[(theirs["hour_utc"] >= lo - 7 * 86400)
                          & (theirs["hour_utc"] <= hi + 7 * 86400)]
            if same_feed(ours, week):
                continue
            feeds.append((symbol, near.reset_index(drop=True)))
        if not feeds:
            continue
        verdict, stored, names, moves = verify.judge_all(c, feeds, now_ts)
        row = {"asset_id": asset.asset_id, "hour_utc": c["hour"], "check": c["check"],
               "verdict": verdict, "provider": asset.fetched_from,
               "verifier": ",".join(names), "stored_move": f"{stored:.6f}",
               "verifier_move": ",".join(f"{m:.6f}" for m in moves),
               "checked_utc": now_ts}
        record[(asset.asset_id, c["hour"], c["check"])] = row
        out.append(row)
    log.info("%s: %d asked, %s", asset.ticker, len(found),
             pd.Series([r["verdict"] for r in out]).value_counts().to_dict())
    return out


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", choices=("coins", "pairs", "funds", "cattle"))
    parser.add_argument("--jumps", default=jumps.DEFAULT_OUT,
                        help="the flagged readings, as the current code wrote them")
    parser.add_argument("--record", default=verify.VERIFIED_PATH)
    parser.add_argument("--all-verdicts", help="also every verdict reached, as CSV")
    parser.add_argument("--only", nargs="*", help="these tickers only")
    parser.add_argument("--cache", help="keep each source month fetched here")
    parser.add_argument("--print-unconfirmed", action="store_true",
                        help="print each verdict not confirmed, for a run whose files "
                             "cannot be read back (Actions)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    now = datetime.now(timezone.utc)
    record = verify.load(args.record)
    if args.kind == "cattle":
        rows = check_contracts(pd.read_parquet(args.jumps), now, record)
    else:
        rows = check(args.kind, pd.read_parquet(args.jumps), now, record,
                     set(args.only) if args.only else None, cache_dir=args.cache)
    verify.write(record, int(now.timestamp()), args.record)
    if args.all_verdicts:
        with open(args.all_verdicts, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=verify.COLUMNS, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
    if args.print_unconfirmed:
        for r in rows:
            if r["verdict"] != verify.CONFIRMED:
                print("VERDICT," + ",".join(str(r[k]) for k in verify.COLUMNS))
    tally = pd.Series([r["verdict"] for r in rows]).value_counts().to_dict() if rows else {}
    print(f"{args.kind}: {len(rows)} verdicts {tally}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

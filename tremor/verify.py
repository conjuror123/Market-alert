"""A second source's word on each far move: did another feed see it too?

A REAL TRADE SHOWS UP ON ANOTHER FEED; A SOURCE'S BAD PRINT DOES NOT. So every
bar that moved far is asked of a second, independent provider, right after the
hourly fetch and before anything is scored:

    USD/INR 2024-12-17 09:00   SiftingIO -0.48%   Yahoo +0.03%   unconfirmed
    USD/TRY 2025-03-14 20:00   SiftingIO -0.42%   Yahoo -0.58%   confirmed
    EUR/USD 2024-12-18 19:00   SiftingIO -1.05%   Yahoo -1.05%   confirmed (the FOMC)

AN UNCONFIRMED MOVE IS NOT SCORED, AND NOTHING IS DELETED. The detector leaves
its reading out - not flagged, and not in any yardstick (tremor.jumps) - but
its bar stays in the store as the provider served it. A message already sent
for it stays on the channel and says `⚠️ unconfirmed: Yahoo shows +0.03%`
(price_monitor.tremor_delivery). Which feed was wrong two feeds cannot always
tell - the second source lags by an hour, misses hours and prints its own bad
ticks - so the verdict is "not seen elsewhere", never "a mistake".

WHO IS ASKED (verifier_for). The currency pairs and the real, served by
SiftingIO: Yahoo's hourly FX. Funds served by Alpaca, Tiingo, Sina, Twelve Data
or Google: Yahoo's 30-minute bars, the consolidated tape. Funds served by Yahoo:
Sina's. Not asked: the coins - Binance's prices are its own trades - and the
futures and the LME's metals, which have no free independent feed.

WHICH BARS (candidates). The detector's own readings, ended within the last
PENDING_HOURS, at CANDIDATE_SIGMA or more of their own kind: an hour's move as
tremor.returns measures it (`close`: from the previous close, or from its own
open on a session's first bar and after a hole) against the instrument's
earlier hours, and a session's gap (`open`) against its earlier gaps. That is
below the detector's bottom word, so nothing it flags is missed: a few a day
across the basket, one request per instrument.

THE VERDICT (judge). Each feed is compared with itself, so a steady offset
between them is not a move. CONFIRMED if the second source moved the same way
at least REAL_SHARE as far, from its closes up to LAG_HOURS before the move to
its closes up to LAG_HOURS after: USD/TRY came back at 11:00 on SiftingIO and
at 12:00 on Yahoo (2025-03-14). An hour it has no bar for is bridged by its
nearest bars within STALE_HOURS: on the night of Seoul's martial law
(2024-12-03) Yahoo has no USD/KRW bar between 07:00 and 15:00, and still
confirms the +2.5%. UNCONFIRMED if it did not move with it - but only once its
bar after the lag has ended too; until then, and while it has no bar after the
move at all, PENDING. UNKNOWN with it silent for STALE_HOURS around the move -
scored as usual.

THE RECORD (VERIFIED_PATH) is what the detector and delivery read. Every
reading is judged again on every run while it is inside its PENDING_HOURS, from
the bars as they now are - a bar that heals gets a new verdict, and one that
heals into no far move loses its verdict - and its last verdict stands after
that. An unconfirmed one is kept for good, because the detector rescores the
whole history every run; the rest go after KEEP_DAYS.

    python -m tremor.verify --history     every far move within the second
                                          source's reach (FX 699 days, funds
                                          54 to 77)
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

from tremor import atomic, bars, quality
from tremor.basket import Asset

log = logging.getLogger("tremor.verify")

HOUR = 3600
VERIFIED_PATH = os.path.join("data", "tremor", "verified.csv")
COLUMNS = ["asset_id", "hour_utc", "check", "verdict", "provider", "verifier",
           "stored_move", "verifier_move", "checked_utc"]

CANDIDATE_SIGMA = 4.0
REAL_SHARE = 0.5
LAG_HOURS = 1
PENDING_HOURS = 24
STALE_HOURS = 12
TAIL_DAYS = 200                   # bars read before the window: the detector's half-year and more
MAX_REQUESTS = 40
KEEP_DAYS = 30
SINA_DAYS = 77                    # how far Sina's US half-hour bars reach back

CONFIRMED, UNCONFIRMED, PENDING, UNKNOWN = "confirmed", "unconfirmed", "pending", "unknown"
CLOSE, OPEN = "close", "open"     # the check: the hour's reading, or the gap's

# Funds' second source is whichever consolidated-tape feed is not serving them.
_FUND_PROVIDERS_CHECKED_BY_YAHOO = ("alpaca", "tiingo", "sina", "twelvedata", "google")


def verifier_for(asset: Asset) -> "tuple[str, str, str] | None":
    """(second source, its symbol, interval) for an instrument, or None if it
    is not asked."""
    if asset.session_template in ("fx_continuous", "b3_fx") \
            and asset.fetched_from == "sifting":
        return "yahoo", asset.ticker.replace("/", "").upper() + "=X", "1h"
    if asset.session_template == "us_equity":
        if asset.fetched_from == "yahoo":
            return "sina", asset.ticker, "30min"
        if asset.fetched_from in _FUND_PROVIDERS_CHECKED_BY_YAHOO:
            return "yahoo", asset.ticker, "30min"
    return None


def reach_days(who: "tuple[str, str, str]") -> int:
    """How far back the second source serves bars."""
    from price_monitor import yahoo

    return yahoo.MAX_LOOKBACK_DAYS[who[2]] - 1 if who[0] == "yahoo" else SINA_DAYS


def fetch_verifier(name: str, symbol: str, interval: str, days: float,
                   session: "requests.Session | None", now: datetime) -> pd.DataFrame:
    """The second source's bars folded onto the store's hourly grid."""
    from price_monitor import sina, yahoo

    if name == "yahoo":
        days = min(max(days, 1.0), float(yahoo.MAX_LOOKBACK_DAYS[interval]))
        candles = yahoo.fetch_full_history(symbol, interval, days=days, session=session,
                                           end=now)
    elif name == "sina":
        candles = sina.fetch_us_bars(symbol, session, now)
    else:
        raise ValueError(f"unknown verifier {name}")
    return bars.to_hourly(bars.candles_to_frame(candles))


# --- which bars ----------------------------------------------------------------

def candidates(asset: Asset, frame: pd.DataFrame, table, now: int,
               since: "int | None" = None, basket=None, dividends=None) -> "list[dict]":
    """The detector's own readings, judgeable from `since` on (default
    PENDING_HOURS ago), at CANDIDATE_SIGMA or more: the metrics built as the
    pipeline builds them (pipeline.build_asset_metrics - the hour's move from
    the previous close, or from its own open on a session's first bar and
    after a hole; the gap dividend-adjusted, unscored where the pipeline
    leaves it so), scored as tremor.jumps scores them, and found when it
    finds them (jumps.ended: an hour once it has ended, a pair's gap at its
    open). Only the recent bars are read: TAIL_DAYS before the window, more
    than the detector's half-year."""
    from tremor import jumps, pipeline
    from tremor.basket import load_basket

    since = int(now) - PENDING_HOURS * HOUR if since is None else int(since)
    recent = frame[frame["hour_utc"] >= since - TAIL_DAYS * 86400]
    metrics = pipeline.build_asset_metrics(asset, basket or load_basket(), recent, table,
                                           dividends)
    if len(metrics) < 30:
        return []
    metrics = metrics.sort_values("hour_utc").reset_index(drop=True)
    template = asset.session_template
    readings = [jumps.score(metrics[["hour_utc", "r"]], template),
                jumps.score_gaps(metrics[["hour_utc", "gap"]], template=template)]
    readings = jumps.ended(pd.concat([f for f in readings if not f.empty], ignore_index=True),
                           template, now)
    far = readings[(readings["hour_utc"] >= since)
                   & (readings["z"].abs() >= CANDIDATE_SIGMA)]
    h = metrics["hour_utc"].to_numpy(dtype="int64")
    close = metrics["close"].to_numpy(dtype="float64")
    opened = metrics["open"].to_numpy(dtype="float64")
    own = (metrics["is_session_open"].to_numpy(dtype=bool)
           | np.isfinite(metrics["hole"].to_numpy(dtype="float64")))
    at = {int(hour): i for i, hour in enumerate(h)}
    out = []
    for hour, kind in zip(far["hour_utc"].astype("int64"), far["reading"]):
        i = at[int(hour)]
        if i == 0:
            continue
        if kind == jumps.HOUR:
            start = (int(h[i]), float(opened[i])) if own[i] else (int(h[i - 1]), float(close[i - 1]))
            out.append({"hour": int(h[i]), "check": CLOSE, "from_open": bool(own[i]),
                        "prev_hour": start[0], "prev_close": start[1], "price": float(close[i])})
        else:
            out.append({"hour": int(h[i]), "check": OPEN, "from_open": False,
                        "prev_hour": int(h[i - 1]), "prev_close": float(close[i - 1]),
                        "price": float(opened[i])})
    return out


# --- the verdict ---------------------------------------------------------------

def judge(c: dict, v: pd.DataFrame, now: int) -> "tuple[str, float, float]":
    """(verdict, stored move, the second source's move) for one candidate,
    against the second source's hourly bars `v` (hour_utc, open, close)."""
    stamps = np.sort(v["hour_utc"].to_numpy(dtype="int64"))
    vclose = dict(zip(v["hour_utc"].astype("int64").tolist(), v["close"].tolist()))
    vopen = dict(zip(v["hour_utc"].astype("int64").tolist(), v["open"].tolist()))
    h, hp, lag, stale = c["hour"], c["prev_hour"], LAG_HOURS * HOUR, STALE_HOURS * HOUR
    p = math.log(c["price"] / c["prev_close"])

    # Where the market was before the move: for an hour measured from its own
    # open, the second source's open of that hour; else its closes up to an
    # hour before, or else its last one, if recent enough.
    if c.get("from_open"):
        if h not in vopen:
            return UNKNOWN, p, 0.0
        befores = [vopen[h]]
    else:
        befores = [vclose[int(t)] for t in stamps[(stamps >= hp - lag) & (stamps <= hp)]]
    if not befores:
        earlier = stamps[stamps <= hp]
        if not len(earlier) or hp - int(earlier[-1]) > stale:
            return UNKNOWN, p, 0.0
        befores = [vclose[int(earlier[-1])]]

    # ... and after it: its open at a session's first print, its closes up to
    # an hour after, or else its first one, once it has one.
    afters = [vopen[h]] if c["check"] == OPEN and h in vopen else []
    afters += [vclose[int(t)] for t in stamps[(stamps >= h) & (stamps <= h + lag)]]
    if not afters:
        later = stamps[stamps > h]
        if not len(later):
            return (UNKNOWN if now - h > PENDING_HOURS * HOUR else PENDING), p, 0.0
        if int(later[0]) - h > stale:
            return UNKNOWN, p, 0.0
        afters = [vclose[int(later[0])]]

    moves = [math.log(b / a) for a in befores for b in afters]
    with_it = max(moves, key=lambda m: m * np.sign(p))
    # Not seen is not a verdict until the second source's next bar is in too:
    # a feed that prints the move an hour late would otherwise read as a
    # mistake at the first look (USD/TRY 2025-03-14 11:00, Yahoo at 12:00).
    lag_passed = now >= h + (LAG_HOURS + 1) * HOUR
    # The move it is reported with is the second source's over the same span
    # - or its nearest bars either side; the lag window only decides.
    start = befores[0] if c.get("from_open") else \
        vclose.get(hp, vclose[int(stamps[stamps <= hp][-1])] if (stamps <= hp).any() else befores[-1])
    end = vopen[h] if c["check"] == OPEN and h in vopen else \
        vclose.get(h, vclose[int(stamps[stamps >= h][0])] if (stamps >= h).any() else afters[0])
    direct = math.log(end / start)
    if with_it * p > 0 and abs(with_it) >= REAL_SHARE * abs(p):
        return CONFIRMED, p, direct
    return (UNCONFIRMED if lag_passed else PENDING), p, direct


# --- the record ----------------------------------------------------------------

def load(path: "str | None" = None) -> "dict[tuple[str, int, str], dict]":
    """{(asset_id, hour_utc, check): row}."""
    path = path or VERIFIED_PATH
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        return {(row["asset_id"], int(row["hour_utc"]), row["check"]): row
                for row in csv.DictReader(f)}


def unconfirmed(path: "str | None" = None) -> "dict[tuple[str, int, str], dict]":
    """The moves the second source did not see, {(asset_id, hour_utc, check):
    row}. A record that cannot be read leaves everything scored, loudly."""
    try:
        return {k: row for k, row in load(path).items()
                if row.get("verdict") == UNCONFIRMED}
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        log.warning("verify: could not read the record - %s", exc)
        return {}


def write(record: dict, now: int, path: "str | None" = None) -> None:
    path = path or VERIFIED_PATH
    keep = [row for row in record.values()
            if row["verdict"] == UNCONFIRMED
            or int(row["hour_utc"]) >= now - KEEP_DAYS * 86400]

    def _write(tmp: str) -> None:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n",
                                    extrasaction="ignore")
            writer.writeheader()
            for row in sorted(keep, key=lambda r: (r["asset_id"], int(r["hour_utc"]),
                                                   r["check"])):
                writer.writerow(row)

    atomic.write_replacing(path, _write)


# --- a pass --------------------------------------------------------------------

def verify(instruments, bars_dir: str, table, session=None, now: "datetime | None" = None,
           path: "str | None" = None, blocked: "set[str] | None" = None,
           history: bool = False) -> dict:
    """One pass: every reading still inside its last PENDING_HOURS is judged
    again from the bars as they now are - or, with `history`, everything
    within the second source's reach. Returns how many of each verdict."""
    from price_monitor import yahoo
    from tremor import corporate_actions
    from tremor.basket import load_basket

    now_dt = now or datetime.now(timezone.utc)
    now_ts = int(now_dt.timestamp())
    record = load(path)
    basket = load_basket()
    dividends = corporate_actions.load_dividends()
    blocked = set(blocked or ())

    # Which readings each instrument has in the window. A verdict there whose
    # reading is no longer a far move - its bar healed - no longer applies.
    due = []
    for asset in instruments:
        who = verifier_for(asset)
        if who is None or who[0] in blocked:
            continue
        since = now_ts - reach_days(who) * 86400 if history else now_ts - PENDING_HOURS * HOUR
        frame = bars.load(bars.store_path(bars_dir, asset.file_stem),
                          since - TAIL_DAYS * 86400)
        if frame.empty:
            continue
        found = candidates(asset, frame, table, now_ts, since, basket, dividends)
        current = {(asset.asset_id, c["hour"], c["check"]) for c in found}
        for key in [k for k in record if k[0] == asset.asset_id and k[1] >= since]:
            if key not in current:
                del record[key]
        if found:
            fresh = any(record.get((asset.asset_id, c["hour"], c["check"]), {}).get("verdict")
                        in (None, PENDING) for c in found)
            due.append((not fresh, asset, who, found))

    # One request per instrument; the ones with a reading not yet judged first,
    # so a busy hour cannot leave the same instruments unasked run after run.
    counts = {CONFIRMED: 0, UNCONFIRMED: 0, PENDING: 0, UNKNOWN: 0}
    requests_left = 10 ** 6 if history else MAX_REQUESTS
    doubted: list[str] = []
    for _, asset, (name, symbol, interval), found in sorted(due, key=lambda d: d[0]):
        if requests_left <= 0 or name in blocked:
            continue
        # From before the earliest bar a move starts at: a Monday gap starts at
        # Friday's close.
        days = (now_ts - min(c["prev_hour"] for c in found)) / 86400 + 1
        requests_left -= 1
        try:
            v = fetch_verifier(name, symbol, interval, days, session, now_dt)
        except Exception as exc:
            log.warning("verify: %s from %s failed - %s", asset.ticker, name, exc)
            if isinstance(exc, yahoo.RateLimited):
                blocked.add(name)
            continue
        for c in found:
            verdict, stored, theirs = judge(c, v, now_ts)
            counts[verdict] += 1
            record[(asset.asset_id, c["hour"], c["check"])] = {
                "asset_id": asset.asset_id, "hour_utc": c["hour"], "check": c["check"],
                "verdict": verdict, "provider": asset.fetched_from, "verifier": name,
                "stored_move": f"{stored:.6f}", "verifier_move": f"{theirs:.6f}",
                "checked_utc": now_ts}
            if verdict == UNCONFIRMED:
                doubted.append(f"{asset.ticker} {datetime.fromtimestamp(c['hour'], timezone.utc):%Y-%m-%d %H:%M}"
                               f" {c['check']} ({asset.fetched_from} {100 * stored:+.2f}%,"
                               f" {name} {100 * theirs:+.2f}%)")
    write(record, now_ts, path)
    if doubted:
        log.warning("verify: not seen by the second source - %s", "; ".join(doubted))
    log.info("verify: %s", ", ".join(f"{k} {n}" for k, n in counts.items()))
    return dict(counts, unconfirmed_moves=doubted)


def main(argv: "list[str] | None" = None) -> int:
    from tremor import sessions
    from tremor.basket import load_basket

    parser = argparse.ArgumentParser(description="Ask a second source about the far moves")
    parser.add_argument("--history", action="store_true",
                        help="every far move within the second source's reach")
    parser.add_argument("--bars-dir", default=bars.DEFAULT_BARS_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(verify(load_basket().instruments, args.bars_dir, sessions.load_sessions(),
                 history=args.history))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

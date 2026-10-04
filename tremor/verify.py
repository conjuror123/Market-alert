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

WHICH BARS (candidates). A bar that has ended within the last PENDING_HOURS and
moved at least CANDIDATE_SIGMA of the instrument's recent hourly bipower sigma -
its close against the previous usable close (`close`, the hour's reading), or a
session's first print against it (`open`, the gap's). That is below the
detector's bottom word, so nothing it flags is missed: about one or two a run,
one request each.

THE VERDICT (judge). Each feed is compared with itself, so a steady offset
between them is not a move. CONFIRMED if the second source moved the same way
at least REAL_SHARE as far, from its closes up to LAG_HOURS before the move to
its closes up to LAG_HOURS after: USD/TRY came back at 11:00 on SiftingIO and
at 12:00 on Yahoo (2025-03-14). An hour it has no bar for is bridged by its
nearest bars within STALE_HOURS: on the night of Seoul's martial law
(2024-12-03) Yahoo has no USD/KRW bar between 07:00 and 15:00, and still
confirms the +2.5%. UNCONFIRMED if it did not move with it. PENDING while it has
no bar after the move yet, asked again next run; UNKNOWN after PENDING_HOURS of
that, or with it silent for STALE_HOURS before the move - scored as usual.

THE RECORD (VERIFIED_PATH) is what the detector and delivery read. A settled
verdict is never asked again; an unconfirmed one is kept for good, because the
detector rescores the whole history every run; the rest go after KEEP_DAYS.

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
SIGMA_MOVES = 500
MAX_REQUESTS = 40
KEEP_DAYS = 30
SINA_DAYS = 77                    # how far Sina's US half-hour bars reach back

CONFIRMED, UNCONFIRMED, PENDING, UNKNOWN = "confirmed", "unconfirmed", "pending", "unknown"
SETTLED = (CONFIRMED, UNCONFIRMED, UNKNOWN)
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

def _sigma(moves: np.ndarray) -> float:
    """Bipower sigma of a run of moves: one jump among them cannot inflate it."""
    m = np.abs(moves[np.isfinite(moves)])
    if len(m) < 20:
        return float("nan")
    return float(math.sqrt(math.pi / 2 * np.mean(m[1:] * m[:-1])))


def candidates(asset: Asset, frame: pd.DataFrame, table, now: int,
               since: "int | None" = None) -> "list[dict]":
    """The ended bars that moved far enough to be asked about, from `since`
    (default PENDING_HOURS ago)."""
    from tremor import returns

    gated = quality.apply_gate(asset, frame, table)
    usable = gated[gated["is_usable"]].sort_values("hour_utc").reset_index(drop=True)
    if len(usable) < 30:
        return []
    h = usable["hour_utc"].to_numpy(dtype="int64")
    close = usable["close"].to_numpy(dtype="float64")
    opened = usable["open"].to_numpy(dtype="float64")
    session = returns.session_ids(asset, usable["hour_utc"])
    is_open = (session != session.shift(1)).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        move = np.concatenate([[np.nan], np.diff(np.log(close))])
        gap = np.concatenate([[np.nan], np.log(opened[1:] / close[:-1])])
    since = int(now) - PENDING_HOURS * HOUR if since is None else int(since)
    out = []
    for i in np.flatnonzero((h >= since) & (h + HOUR <= now)):
        if i == 0:
            continue
        sigma = _sigma(move[max(1, i - SIGMA_MOVES - 24):max(1, i - 24)])
        if not np.isfinite(sigma) or sigma <= 0:
            continue
        for check, size, price in ((CLOSE, move[i], close[i]), (OPEN, gap[i], opened[i])):
            if check == OPEN and not is_open[i]:
                continue
            if abs(size) >= CANDIDATE_SIGMA * sigma:
                out.append({"hour": int(h[i]), "check": check, "prev_hour": int(h[i - 1]),
                            "prev_close": float(close[i - 1]), "price": float(price)})
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

    # Where the market was before the move: its closes up to an hour before,
    # or else its last one, if recent enough.
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
    if with_it * p > 0 and abs(with_it) >= REAL_SHARE * abs(p):
        return CONFIRMED, p, with_it
    return UNCONFIRMED, p, with_it


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
            if row["verdict"] in (UNCONFIRMED, PENDING)
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
    """One pass over the far moves not yet settled: the last PENDING_HOURS', or
    with `history` everything within the second source's reach. Returns how
    many of each verdict."""
    from price_monitor import yahoo

    now_dt = now or datetime.now(timezone.utc)
    now_ts = int(now_dt.timestamp())
    record = load(path)
    counts = {CONFIRMED: 0, UNCONFIRMED: 0, PENDING: 0, UNKNOWN: 0}
    requests_left = 10 ** 6 if history else MAX_REQUESTS
    blocked = set(blocked or ())
    doubted: list[str] = []
    for asset in instruments:
        who = verifier_for(asset)
        if who is None or who[0] in blocked or requests_left <= 0:
            continue
        frame = bars.load(bars.store_path(bars_dir, asset.file_stem))
        if frame.empty:
            continue
        since = now_ts - reach_days(who) * 86400 if history else None
        todo = [c for c in candidates(asset, frame, table, now_ts, since)
                if record.get((asset.asset_id, c["hour"], c["check"]),
                              {}).get("verdict") not in SETTLED]
        if not todo:
            continue
        name, symbol, interval = who
        days = (now_ts - min(c["hour"] for c in todo)) / 86400 + 2
        requests_left -= 1
        try:
            v = fetch_verifier(name, symbol, interval, days, session, now_dt)
        except Exception as exc:
            log.warning("verify: %s from %s failed - %s", asset.ticker, name, exc)
            if isinstance(exc, yahoo.RateLimited):
                blocked.add(name)
            continue
        for c in todo:
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

"""A second source's word on each far move: did another feed see it too?

A REAL TRADE SHOWS UP ON ANOTHER FEED; A SOURCE'S BAD PRINT DOES NOT. So every
bar that moved far is asked of a second, independent provider, right after the
hourly fetch and before anything is scored:

    USD/INR 2024-12-17 09:00   SiftingIO -0.48%   Yahoo +0.03%   unconfirmed
    USD/TRY 2025-03-14 20:00   SiftingIO -0.42%   Yahoo -0.58%   confirmed
    EUR/USD 2024-12-18 19:00   SiftingIO -1.05%   Yahoo -1.05%   confirmed (the FOMC)

AN UNCONFIRMED MOVE IS NOT SCORED, AND NOTHING IS DELETED. The detector leaves
its reading out - not flagged, and not in any yardstick (jump.jumps) - but
its bar stays in the store as the provider served it. A message already sent
for it stays on the channel and says `⚠️ unconfirmed: Yahoo +0.03%`
(price_monitor.jump_delivery). Which feed was wrong two feeds cannot always
tell - the second source lags by an hour, misses hours and prints its own bad
ticks - so the verdict is "not seen elsewhere", never "a mistake".

WHO IS ASKED (verifiers_for). The currency pairs and the real, served by
SiftingIO: Yahoo's hourly FX and MarketWatch's - Yahoo reaches back two years
but has as little as a fifth of USD/INR's hours, MarketWatch has every hour of
the last ten days. Funds served by Alpaca, Tiingo, Sina, Twelve Data or Google:
Yahoo's 30-minute bars, the consolidated tape. Funds served by Yahoo: Sina's.
Coffee, cocoa and cotton, served by Yahoo: Sina's global futures
(SINA_FUTURES). Live cattle: MarketWatch's continuous contract. The coins,
served by Binance, whose prices are its own trades: Coinbase's and Kraken's
dollar pairs - a wick on one exchange is real there and not the market's. Not
asked: the LME's metals, which have no free independent feed found.

WITH TWO SOURCES (judge_all, combine), a move is confirmed if either saw it,
pending while either still waits for its next bar, and unconfirmed only if one
answered and none saw it. A source with bars around the move outweighs one that
only bridges it. A source that fails to answer leaves the other to.

WHICH BARS (candidates). The detector's own readings, ended within the last
PENDING_HOURS, at CANDIDATE_SIGMA (or the detector's bottom level, if set lower)
or more of their own kind, scored with the detector's own window: an hour's move as
jump.returns measures it (`close`: from the previous close, or from its own
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

    python -m jump.verify --history     every far move within the second
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

from price_monitor.models import UNANSWERED_IN_A_ROW, Unreachable
from jump import atomic, bars
from jump.basket import Asset

log = logging.getLogger("jump.verify")

HOUR = 3600
VERIFIED_PATH = os.path.join("data", "jump", "verified.csv")
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

# The softs Yahoo serves, as Sina's global futures name them (the same endpoint
# the LME's metals come from): against the stored bars over 2026-05 to 10,
# 0.2-2.2 bp apart at the median, hourly moves correlated 0.96-0.99, no hour
# missing. Live cattle is there too (LE), but as quotes without volume whose
# hours correlate 0.80 with the store's: not used.
SINA_FUTURES = {"KC=F": "KC", "CC=F": "CC", "CT=F": "CT"}
SINA_FUTURES_DAYS = 30            # inside the 1,023 hourly bars Sina serves

# Sina's softs are continuous series that change contract on their own days,
# not this series' (jump.futures): for those sessions the two feeds hold
# different months, a few hundred bp apart (coffee 2026-08-03..10 about -500,
# cocoa 06-16..22 +220 and 07-21..08-06 -250), against +-10 bp on the same
# month. Each feed is still compared with itself inside such a stretch; only a
# move whose span crosses Sina's change carries the spread between the two
# contracts. A session whose median offset to the store stepped by
# ROLL_STEP_BP or more from the session before opens with such a change.
ROLL_STEP_BP = 50.0


# The pairs MarketWatch is asked about besides Yahoo, and live cattle's
# continuous contract there (price_monitor/marketwatch.py).
MARKETWATCH_CATTLE = "FUTURE/US/XCME/LC00"
MARKETWATCH_DAYS = 9              # inside the ten days of hourly bars it serves

# The coins' two exchanges (price_monitor/coinbase.py, kraken.py). Coinbase
# serves years by start and end; Kraken only its last 720 hours.
COINBASE_DAYS = 365
KRAKEN_DAYS = 29


def verifiers_for(asset: Asset) -> "list[tuple[str, str, str]]":
    """The second sources of an instrument, each (name, its symbol, interval);
    empty if none is asked."""
    if asset.session_template in ("fx_continuous", "b3_fx") \
            and asset.fetched_from == "sifting":
        pair = asset.ticker.replace("/", "").upper()
        return [("yahoo", pair + "=X", "1h"),
                ("marketwatch", "CURRENCY/US/XTUP/" + pair, "1h")]
    if asset.session_template == "us_equity":
        if asset.fetched_from == "yahoo":
            return [("sina", asset.ticker, "30min")]
        if asset.fetched_from in _FUND_PROVIDERS_CHECKED_BY_YAHOO:
            return [("yahoo", asset.ticker, "30min")]
    if asset.ticker in SINA_FUTURES and asset.fetched_from == "yahoo":
        return [("sina", SINA_FUTURES[asset.ticker], "1h")]
    if asset.session_template == "cme_cattle" and asset.fetched_from == "yahoo":
        return [("marketwatch", MARKETWATCH_CATTLE, "1h")]
    if asset.session_template == "crypto_24_7" and asset.fetched_from == "binance":
        from price_monitor import coinbase, kraken
        return [("coinbase", coinbase.product_for(asset.ticker), "1h"),
                ("kraken", kraken.pair_for(asset.ticker), "1h")]
    return []


def reach_days(who: "tuple[str, str, str]") -> int:
    """How far back the second source serves bars."""
    from price_monitor import yahoo

    if who[0] == "yahoo":
        return yahoo.MAX_LOOKBACK_DAYS[who[2]] - 1
    if who[0] == "marketwatch":
        return MARKETWATCH_DAYS
    if who[0] == "coinbase":
        return COINBASE_DAYS
    if who[0] == "kraken":
        return KRAKEN_DAYS
    return SINA_DAYS if who[2] == "30min" else SINA_FUTURES_DAYS


def fetch_verifier(name: str, symbol: str, interval: str, days: float,
                   session: "requests.Session | None", now: datetime) -> pd.DataFrame:
    """The second source's bars folded onto the store's hourly grid."""
    from datetime import timedelta

    from price_monitor import coinbase, kraken, marketwatch, sina, yahoo

    if name in ("coinbase", "kraken"):
        # Only hours that have ended: the open one is still moving.
        end = now.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=max(days, 1.0))
        client = coinbase if name == "coinbase" else kraken
        return bars.to_hourly(bars.candles_to_frame(
            client.fetch_history(symbol, start, end, session)))
    if name == "marketwatch":
        candles = marketwatch.fetch_hourly(symbol, session, now)
    elif name == "yahoo":
        days = min(max(days, 1.0), float(yahoo.MAX_LOOKBACK_DAYS[interval]))
        candles = yahoo.fetch_full_history(symbol, interval, days=days, session=session,
                                           end=now)
    elif name == "sina" and interval == "30min":
        candles = sina.fetch_us_bars(symbol, session, now)
    elif name == "sina":
        candles = sina.fetch_bars(symbol, session, now, url=sina.GLOBAL_URL)
    else:
        raise ValueError(f"unknown verifier {name}")
    return bars.to_hourly(bars.candles_to_frame(candles))


# --- which bars ----------------------------------------------------------------

def tail_days(window: float) -> float:
    """Days of bars read before the window: the detector's own window
    (detector.window_days in config/basket.yaml) and more."""
    return max(float(TAIL_DAYS), float(window) + 18.0)


def candidate_line(levels) -> float:
    """The |z| a reading is asked about from: CANDIDATE_SIGMA, or the
    detector's bottom level if that is set lower, so every reading the
    detector can flag is asked about."""
    return min(CANDIDATE_SIGMA, float(levels[0]))


def candidates(asset: Asset, frame: pd.DataFrame, table, now: int,
               since: "int | None" = None, basket=None, dividends=None,
               settings=None) -> "list[dict]":
    """The detector's own readings, judgeable from `since` on (default
    PENDING_HOURS ago), at candidate_line or more: the metrics built as the
    pipeline builds them (pipeline.build_asset_metrics - the hour's move from
    the previous close, or from its own open on a session's first bar and
    after a hole; the gap dividend-adjusted, unscored where the pipeline
    leaves it so), scored as jump.jumps scores them, and found when it
    finds them (jumps.ended: an hour once it has ended, a pair's gap at its
    open), with the detector's window and levels (`settings`, read from
    config/basket.yaml once per pass). Only the recent bars are read:
    tail_days before the window, more than the detector's own."""
    from jump import jumps, pipeline
    from jump.basket import load_basket

    since = int(now) - PENDING_HOURS * HOUR if since is None else int(since)
    window, levels = settings or jumps.settings()
    recent = frame[frame["hour_utc"] >= since - tail_days(window) * 86400]
    metrics = pipeline.build_asset_metrics(asset, basket or load_basket(), recent, table,
                                           dividends)
    if len(metrics) < 30:
        return []
    metrics = metrics.sort_values("hour_utc").reset_index(drop=True)
    template = asset.session_template
    readings = [jumps.score(metrics[["hour_utc", "r"]], template, window, levels),
                jumps.score_gaps(metrics[["hour_utc", "gap"]], window, levels, template)]
    readings = jumps.ended(pd.concat([f for f in readings if not f.empty], ignore_index=True),
                           template, now)
    far = readings[(readings["hour_utc"] >= since)
                   & (readings["z"].abs() >= candidate_line(levels))]
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


def switches(store: pd.DataFrame, v: pd.DataFrame, template: str) -> "list[int]":
    """The first hours of the sessions the second source changed contract at:
    where its median offset to the store stepped by ROLL_STEP_BP or more from
    the session before. A one-hour bad print in either feed moves one bar, not
    a session's median."""
    from jump import sessions

    j = store[["hour_utc", "close"]].merge(v[["hour_utc", "close"]], on="hour_utc",
                                           suffixes=("_s", "_v"))
    if j.empty:
        return []
    keys = [sessions.session_key(int(h), template) for h in j["hour_utc"]]
    g = pd.DataFrame({"hour": j["hour_utc"].astype("int64"),
                      "off": np.log(j["close_v"].to_numpy(float) / j["close_s"].to_numpy(float)),
                      "key": keys}).dropna(subset=["key"])
    by = g.groupby("key").agg(off=("off", "median"), first=("hour", "min"),
                              n=("hour", "size")).sort_index()
    by = by[by["n"] >= 3]
    stepped = (by["off"].diff().abs() * 1e4) >= ROLL_STEP_BP
    return [int(h) for h in by.loc[stepped, "first"]]


def crosses(c: dict, at: "list[int]") -> bool:
    """Whether a move's span - from its closes up to LAG_HOURS before to those
    up to LAG_HOURS after - takes in one of these hours."""
    lag = LAG_HOURS * HOUR
    return any(c["prev_hour"] - lag < h <= c["hour"] + lag for h in at)


def _priced(v: pd.DataFrame) -> pd.DataFrame:
    """A second source's bars with a real price only: a zero, negative or
    missing open or close is not a price, as in the store's bar gate."""
    prices = v[["open", "close"]].to_numpy(dtype="float64")
    return v[(np.isfinite(prices) & (prices > 0)).all(axis=1)].reset_index(drop=True)


def combine(verdicts: "list[tuple[str, str, float, float]]") -> "tuple[str, float, list, list]":
    """One verdict from every second source that answered, each (name,
    verdict, stored move, its move): confirmed if any saw the move; pending
    while any is still waiting for its next bar; unconfirmed if any answered
    and none saw it; else unknown. Returns (verdict, stored move, the names it
    rests on, their moves)."""
    stored = verdicts[0][2] if verdicts else 0.0
    for verdict in (CONFIRMED, PENDING, UNCONFIRMED):
        mine = [v for v in verdicts if v[1] == verdict]
        if mine:
            chosen = mine[:1] if verdict == CONFIRMED else mine
            return verdict, stored, [v[0] for v in chosen], [v[3] for v in chosen]
    return UNKNOWN, stored, [v[0] for v in verdicts], [v[3] for v in verdicts]


def _around(c: dict, v: pd.DataFrame) -> bool:
    """Whether a source has bars of its own around the move: within LAG_HOURS
    of where it starts and of where it ends - not just a bridge across hours
    it has no bar for."""
    stamps = v["hour_utc"].to_numpy(dtype="int64")
    h, hp, lag = c["hour"], c["prev_hour"], LAG_HOURS * HOUR
    before = (h in set(stamps.tolist())) if c.get("from_open") else \
        bool(((stamps >= hp - lag) & (stamps <= hp)).any())
    return before and bool(((stamps >= h) & (stamps <= h + lag)).any())


def judge_all(c: dict, answers: "list[tuple[str, pd.DataFrame]]",
              now: int) -> "tuple[str, float, list, list]":
    """One candidate against every source that answered, combined. A source
    with bars of its own around the move outweighs one bridging hours it has
    none for: USD/INR's night, where Yahoo's last bar is 10:00 and
    MarketWatch has every hour, is MarketWatch's to judge - a seventeen-hour
    bridge would hold the verdict till morning and then read the night's
    drift as the move."""
    direct = [(name, v) for name, v in answers if _around(c, v)]
    return combine([(name,) + judge(c, v, now) for name, v in (direct or answers)])


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
    from jump import corporate_actions, jumps
    from jump.basket import load_basket

    now_dt = now or datetime.now(timezone.utc)
    now_ts = int(now_dt.timestamp())
    record = load(path)
    basket = load_basket()
    dividends = corporate_actions.load_dividends()
    settings = jumps.settings()
    blocked = set(blocked or ())
    told = set(blocked)

    # Which readings each instrument has in the window. A verdict there whose
    # reading is no longer a far move - its bar healed - no longer applies.
    due = []
    for asset in instruments:
        sources = [w for w in verifiers_for(asset) if w[0] not in blocked]
        if not sources:
            continue
        reach = max(reach_days(w) for w in sources)
        since = now_ts - reach * 86400 if history else now_ts - PENDING_HOURS * HOUR
        frame = bars.load(bars.store_path(bars_dir, asset.file_stem),
                          since - tail_days(settings[0]) * 86400)
        if frame.empty:
            continue
        found = candidates(asset, frame, table, now_ts, since, basket, dividends, settings)
        current = {(asset.asset_id, c["hour"], c["check"]) for c in found}
        for key in [k for k in record if k[0] == asset.asset_id and k[1] >= since]:
            if key not in current:
                del record[key]
        if found:
            fresh = any(record.get((asset.asset_id, c["hour"], c["check"]), {}).get("verdict")
                        in (None, PENDING) for c in found)
            due.append((not fresh, asset, found, frame))

    # One request per source and instrument; the instruments with a reading not
    # yet judged first, so a busy hour cannot leave the same ones unasked run
    # after run. A source that fails leaves the others to answer; a rate limit
    # stops that source for the rest of the run, as does not answering
    # UNANSWERED_IN_A_ROW requests in a row (~96 s each of a 20-minute job).
    counts = {CONFIRMED: 0, UNCONFIRMED: 0, PENDING: 0, UNKNOWN: 0}
    unanswered: dict[str, int] = {}
    requests_left = 10 ** 6 if history else MAX_REQUESTS
    doubted: list[str] = []
    for _, asset, found, frame in sorted(due, key=lambda d: d[0]):
        # From before the earliest bar a move starts at: a Monday gap starts at
        # Friday's close.
        days = (now_ts - min(c["prev_hour"] for c in found)) / 86400 + 1
        answers = []
        for name, symbol, interval in verifiers_for(asset):
            if requests_left <= 0 or name in blocked:
                continue
            requests_left -= 1
            try:
                answers.append((name, _priced(fetch_verifier(name, symbol, interval, days,
                                                             session, now_dt))))
                unanswered.pop(name, None)
            except Exception as exc:
                log.warning("verify: %s from %s failed - %s", asset.ticker, name, exc)
                if isinstance(exc, yahoo.RateLimited) or "answered 429" in str(exc):
                    blocked.add(name)
                elif isinstance(exc, Unreachable):
                    unanswered[name] = unanswered.get(name, 0) + 1
                    if unanswered[name] >= UNANSWERED_IN_A_ROW:
                        log.warning("verify: %s did not answer %d requests in a row; "
                                    "not asked again this run", name, unanswered[name])
                        blocked.add(name)
                else:
                    unanswered.pop(name, None)              # it answered
        if not answers:
            continue
        changed = {name: switches(frame, v, asset.session_template)
                   for name, v in answers if asset.ticker in SINA_FUTURES and name == "sina"}
        for c in found:
            try:
                # Not across the second source's own change of contract.
                usable = [(name, v) for name, v in answers
                          if not crosses(c, changed.get(name, []))]
                if not usable:
                    p = math.log(c["price"] / c["prev_close"])
                    verdict, stored, names, moves = UNKNOWN, p, [
                        f"{answers[0][0]} (changed contract)"], [0.0]
                else:
                    verdict, stored, names, moves = judge_all(c, usable, now_ts)
            except Exception as exc:
                # One instrument's surprise costs that reading, not the pass.
                log.warning("verify: %s %s could not be judged - %s", asset.ticker,
                            c["hour"], exc)
                continue
            counts[verdict] += 1
            record[(asset.asset_id, c["hour"], c["check"])] = {
                "asset_id": asset.asset_id, "hour_utc": c["hour"], "check": c["check"],
                "verdict": verdict, "provider": asset.fetched_from,
                "verifier": ",".join(names),
                "stored_move": f"{stored:.6f}",
                "verifier_move": ",".join(f"{m:.6f}" for m in moves),
                "checked_utc": now_ts}
            if verdict == UNCONFIRMED:
                theirs = ", ".join(f"{n} {100 * m:+.2f}%" for n, m in zip(names, moves))
                doubted.append(f"{asset.ticker} {datetime.fromtimestamp(c['hour'], timezone.utc):%Y-%m-%d %H:%M}"
                               f" {c['check']} ({asset.fetched_from} {100 * stored:+.2f}%; {theirs})")
    write(record, now_ts, path)
    if doubted:
        log.warning("verify: not seen by the second source - %s", "; ".join(doubted))
    log.info("verify: %s", ", ".join(f"{k} {n}" for k, n in counts.items()))
    # The sources stopped during this pass, by a rate limit or silence.
    return dict(counts, unconfirmed_moves=doubted, stopped=sorted(blocked - told))


def main(argv: "list[str] | None" = None) -> int:
    from jump import sessions
    from jump.basket import load_basket

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

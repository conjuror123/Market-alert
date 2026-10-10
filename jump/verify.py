"""The sources' vote on each far move: did the other feeds see it too?

A REAL TRADE SHOWS UP ON OTHER FEEDS; A SOURCE'S BAD PRINT DOES NOT. So every
bar that moved far is put to every other source that carries the instrument,
right after the hourly fetch and before anything is scored. The store's own
provider is one vote that saw it; each other source that can answer is one
vote, and one that is down or has no bars around the move - an outage - is
left out of the count, and named:

    most saw it       real          scored, nothing said
    a tie             uncertain     scored, `⚠️ Yahoo(-2.50%), Alpaca(-2.50%), Sina(outage), MarketWatch(-0.50%)`
    most did not      not real      not scored, `❌ Binance(+2.03%), Coinbase(-0.10%), Kraken(-0.10%)`
    none could tell   uncertain     scored, `⚠️ SiftingIO(+2.10%), Yahoo(outage), MarketWatch(outage)`

A MOVE NOT REAL IS NOT SCORED, AND NOTHING IS DELETED. The detector leaves its
reading out - not flagged, and not in any yardstick (jump.jumps) - but its bar
stays in the store as the provider served it. A message already sent for it
stays on the channel with the line under it (price_monitor.jump_delivery).
Which feed was wrong cannot always be told - sources lag by an hour, miss hours
and print their own bad ticks - so the vote is "not seen elsewhere", never "a
mistake".

WHO IS ASKED (SOURCES, sources_for). Every source of the instrument's class
that carries it, except its own provider. The currency pairs and the real:
Yahoo's hourly FX, MarketWatch's and Wallstreetcn's (not USD/KRW) - Yahoo
reaches back two years but has as little as a fifth of USD/INR's hours,
MarketWatch has every hour of the last nine trading days. Funds: Yahoo's and
Sina's half-hour bars and MarketWatch's hourly ones, and Alpaca's consolidated
tape (Alpaca_SIP), fifteen minutes behind - asked once it serves the move's
hour whole (Source.delay). Coffee, cocoa and cotton: Sina's global futures
(SINA_FUTURES), Wallstreetcn's cocoa and cotton, and the FT's coffee. Live
cattle: MarketWatch's continuous contract, and the FT's bars of the contract
the store holds. The coins, served by Binance, whose prices are its own
trades: Coinbase's and Kraken's dollar pairs - a wick on one exchange is real
there and not the market's. The LME's metals, served by Sina: Wallstreetcn's
hourly bars. Two vendors printing the same bars agree: each is a voice.

THE VOTE (judge_all, combine) is taken with what the sources serve when it is
taken: a source that has not shown the move yet counts against, until the
next count. A source with bars around the move outweighs one that only
bridges it: the bridging one is an outage. Every source of an instrument is
asked, or none that run (MAX_REQUESTS): a vote of all of them, not of
whichever were asked. With every other source down, the move is uncertain
until a count after they are back.

WHICH BARS (candidates). The detector's own readings, ended within the
instrument's recount (recount_days), at CANDIDATE_SIGMA (or the detector's bottom level, if set lower)
or more of their own kind, scored with the detector's own window: an hour's move as
jump.returns measures it (`close`: from the previous close, or from its own
open on a session's first bar and after a hole) against the instrument's
earlier hours, and a session's gap (`open`) against its earlier gaps. That is
below the detector's bottom word, so nothing it flags is missed: a few a day
across the basket, one request per source and instrument.

ONE SOURCE'S ANSWER (judge). Each feed is compared with itself, so a steady offset
between them is not a move. CONFIRMED if the source moved the same way
at least REAL_SHARE as far, from its closes up to LAG_HOURS before the move to
its closes up to LAG_HOURS after: USD/TRY came back at 11:00 on SiftingIO and
at 12:00 on Yahoo (2025-03-14). An hour it has no bar for is bridged by its
nearest bars within STALE_HOURS: on the night of Seoul's martial law
(2024-12-03) Yahoo has no USD/KRW bar between 07:00 and 15:00, and still
confirms the +2.5%. UNCONFIRMED if it did not move with it, or has not yet.
UNKNOWN - an outage - with no bar after the move yet, or silent for
STALE_HOURS around it.

THE RECORD (VERIFIED_PATH) is what the detector and delivery read. A reading
is voted on the run that finds it, then again from the bars as they then are
on the first run after each end of its market's session (due,
sessions.last_close: the NYSE close for funds, a daily-session market's own
close, 00:00 UTC for coins and pairs) - its own provider re-read first
(recount_hours, jump.backfill) - until the closest-reaching of its
sources no longer serves its hour (recount_days: nine days for the funds,
pairs and cattle, 29 for the coins, 30 for coffee, 79 for cocoa, cotton and
the LME); after that the further ones would vote alone, and the vote stands. A
source that corrects its bars turns the vote back; a bar that heals into no far
move loses its vote. A move not real is kept for good, because the detector rescores the whole history
every run; the rest go after KEEP_DAYS. A record from before the vote reads in
its words (_BEFORE_THE_VOTE).

OVERNIGHT, for a session's first hour: most feeds did not see the hour move,
but most saw the move from the previous session's close to that bar's close,
the store one of them. The move happened in the night, and the store's first
print was a stale one at the old price; jump.jumps moves it into the gap with
the feeds' night. Kept for good, like a move not real.

    python -m jump.verify               one pass, as the hourly run makes it

Older than its recount, a move's vote stands as the record holds it: history is
not voted again (docs/decisions.md, "History is not voted again").
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import numpy as np
import pandas as pd
import requests

from price_monitor import alpaca, ft, wallstreetcn
from price_monitor.models import UNANSWERED_IN_A_ROW, Unreachable
from jump import atomic, bars
from jump.basket import Asset

log = logging.getLogger("jump.verify")

HOUR = 3600
VERIFIED_PATH = os.path.join("data", "jump", "verified.csv")
COLUMNS = ["asset_id", "hour_utc", "check", "verdict", "provider", "verifier", "seen",
           "stored_move", "verifier_move", "checked_utc"]

CANDIDATE_SIGMA = 4.0
REAL_SHARE = 0.5
LAG_HOURS = 1
PENDING_HOURS = 24
STALE_HOURS = 12
TAIL_DAYS = 200                   # bars read before the window: the detector's half-year and more
MAX_REQUESTS = 40
KEEP_DAYS = 90                    # longer than any recount (the softs' and the LME's, 79 days)

# One source's answer about a move (judge).
CONFIRMED, UNCONFIRMED, UNKNOWN = "confirmed", "unconfirmed", "unknown"
# The vote on it (combine), the stored provider one vote that saw it.
REAL, UNCERTAIN, NOT_REAL = "real", "uncertain", "not_real"
# A session's first hour whose move most feeds saw happen overnight: kept for
# good, and jump.jumps moves the move into the night (see judge_all).
OVERNIGHT = "overnight"
KEPT = (NOT_REAL, OVERNIGHT)
# A record written before the vote: its confirmed is real, and its
# unconfirmed not real. Its unknown - no other source had bars - reads as the
# vote counted outages until 2026-10-10, against the move (load): history stands.
_BEFORE_THE_VOTE = {CONFIRMED: REAL, UNCONFIRMED: NOT_REAL}
CLOSE, OPEN = "close", "open"     # the check: the hour's reading, or the gap's

# MarketWatch names a fund by its listing exchange; ARCX unless here
# (measured 2026-10-07: 96 funds on ARCX).
MARKETWATCH_FUND_EXCHANGE = {
    **dict.fromkeys(("QQQ", "SHY", "IEI", "IEF", "TLT", "MBB", "EMB", "PFF", "SMH", "SOXX",
                     "CIBR", "SKYY", "TUR", "VNQI", "VGIT", "VGLT", "VTIP", "VMBS", "LMBS",
                     "VCIT", "VCSH", "IGIB", "USIG", "SLQD", "ANGL", "FALN", "VWOB"), "XNAS"),
    **dict.fromkeys(("ITB", "IYT", "IGV", "EZU", "INDA", "REM", "GOVT", "USHY", "EMHY",
                     "CEMB"), "BATS"),
}

# The softs Yahoo serves, as Sina's global futures name them (the same endpoint
# the LME's metals come from): against the stored bars over 2026-05 to 10,
# 0.2-2.2 bp apart at the median, hourly moves correlated 0.96-0.99, no hour
# missing. Live cattle is there too (LE), but as quotes without volume whose
# hours correlate 0.80 with the store's: not used.
SINA_FUTURES = {"KC=F": "KC", "CC=F": "CC", "CT=F": "CT"}

# Sina's softs are continuous series that change contract on their own days,
# not this series' (jump.futures): for those sessions the two feeds hold
# different months, a few hundred bp apart (coffee 2026-08-03..10 about -500,
# cocoa 06-16..22 +220 and 07-21..08-06 -250), against +-10 bp on the same
# month. Each feed is still compared with itself inside such a stretch; only a
# move whose span crosses Sina's change carries the spread between the two
# contracts. A session whose median offset to the store stepped by
# ROLL_STEP_BP or more from the session before opens with such a change.
ROLL_STEP_BP = 50.0

MARKETWATCH_CATTLE = "FUTURE/US/XCME/LC00"


@dataclass(frozen=True)
class Source:
    """A feed that can be asked about another feed's move. `name` is the
    provider it is, so an instrument is never asked of its own provider;
    `days`, how far back it serves, measured; `symbol(asset)`, what it calls
    the instrument (None: it does not carry it); `own_rolls`, a continuous
    future that changes contract on its own days; `delay`, seconds it serves
    behind the clock - it is not asked about a move it cannot serve whole
    yet, and joins the next count; `keys`, the secrets it needs - without
    them it is no voter at all, rather than an outage named on every vote."""
    name: str
    label: str
    days: float
    interval: str
    symbol: "Callable[[Asset], str | None]"
    own_rolls: bool = False
    delay: int = 0
    keys: "tuple[str, ...]" = ()

    def serves(self, c: dict, now: int) -> bool:
        """Whether it can answer about the move by `now`: one without a delay
        answers with what it has; a delayed one once it serves the move's hour
        whole."""
        return not self.delay or c["hour"] + HOUR + self.delay <= now


ALPACA_KEYS = ("ALPACA_KEY_ID", "ALPACA_SECRET_KEY")


def _pair(asset: Asset) -> str:
    return asset.ticker.replace("/", "").upper()


def _ft_cattle(asset: Asset) -> "str | None":
    """The FT's name for the contract the store holds today (LCZ26)."""
    from jump import futures
    held = futures.front_contract(asset.ticker, datetime.now(timezone.utc).date())
    return ft.cattle_symbol(held[0] if held else None)


def _exchange(name: str) -> "Callable[[Asset], str | None]":
    def symbol(asset: Asset) -> "str | None":
        from price_monitor import coinbase, kraken
        return (coinbase.product_for if name == "coinbase" else kraken.pair_for)(asset.ticker)
    return symbol


# Every source, by class. Reach measured 2026-10-08: Yahoo's 30-minute bars 59
# days and hourly 729 (price_monitor.yahoo); Sina's fund bars about 78 trading
# days and its softs back to 2026-05-12 (coffee, cocoa) and 07-20 (cotton);
# MarketWatch about nine trading days; Kraken 720 hours; Coinbase pages back to
# a coin's listing; Alpaca's consolidated tape (SIP) from 2016-01-01, to fifteen
# minutes back on the free plan (price_monitor.alpaca); the FT 24 trading days
# (2026-10-10), counted as 30. Wallstreetcn's bars reach about 170 days
# (2026-10-10); the LME's are counted to 79, as Sina's softs are: the record
# keeps a vote KEEP_DAYS, and the store's LME bars before 2026-07-08 (tin) and
# -15 are Wallstreetcn's own, where it would be no voter (SUPPLIED) - 79 days
# back from 2026-10-09 is 07-22. Never Tiingo, SiftingIO or Twelve Data, whose
# allowances the live run uses, nor Google, one session deep.
SOURCES: "dict[str, tuple[Source, ...]]" = {
    "fx": (
        Source("yahoo", "Yahoo", 729, "1h", lambda a: _pair(a) + "=X"),
        Source("marketwatch", "MarketWatch", 9, "1h",
               lambda a: "CURRENCY/US/XTUP/" + _pair(a)),
        Source("wallstreetcn", "Wallstreetcn", 170, "1h",
               lambda a: wallstreetcn.pair_code(a.ticker)),
    ),
    "us_equity": (
        Source("yahoo", "Yahoo", 59, "30min", lambda a: a.ticker),
        Source("sina", "Sina", 77, "30min", lambda a: a.ticker),
        Source("marketwatch", "MarketWatch", 9, "1h", lambda a: "FUND/US/{}/{}".format(
            MARKETWATCH_FUND_EXCHANGE.get(a.ticker, "ARCX"), a.ticker)),
        # Every exchange's trades: another source than the IEX feed Alpaca
        # serves 30 funds from, which is one exchange's (the user's, 2026-10-08).
        Source("alpaca_sip", "Alpaca_SIP", (datetime.now(timezone.utc) - alpaca.FIRST).days + 1,
               "30min", lambda a: a.ticker, delay=15 * 60, keys=ALPACA_KEYS),
    ),
    "softs": (
        Source("sina", "Sina", 79, "1h", lambda a: SINA_FUTURES.get(a.ticker), own_rolls=True),
        Source("wallstreetcn", "Wallstreetcn", 170, "1h",
               lambda a: wallstreetcn.SOFTS.get(a.ticker), own_rolls=True),
        Source("ft", "FT", 30, "1h", lambda a: ft.SOFTS.get(a.ticker), own_rolls=True),
    ),
    "cme_cattle": (
        Source("marketwatch", "MarketWatch", 9, "1h", lambda a: MARKETWATCH_CATTLE),
        # The store's own contract, so the FT's changes are the store's: a
        # move across one sees the step, as across Sina's (own_rolls).
        Source("ft", "FT", 30, "1h", _ft_cattle, own_rolls=True),
    ),
    "lme": (
        Source("wallstreetcn", "Wallstreetcn", 79, "1h", lambda a: wallstreetcn.LME.get(a.ticker)),
    ),
    "crypto_24_7": (
        Source("coinbase", "Coinbase", 365, "1h", _exchange("coinbase")),
        Source("kraken", "Kraken", 29, "1h", _exchange("kraken")),
    ),
}
_CLASS = {"fx_continuous": "fx", "b3_fx": "fx",
          "ice_coffee": "softs", "ice_cocoa": "softs", "ice_cotton": "softs"}
# What the channel calls each provider: the sources, and the stores' own.
LABELS = {**{s.name: s.label for group in SOURCES.values() for s in group},
          "binance": "Binance", "sifting": "SiftingIO", "tiingo": "Tiingo",
          "alpaca": "Alpaca_IEX", "twelvedata": "Twelve Data", "google": "Google",
          # named in the record by the history check before the vote
          "dukascopy": "Dukascopy", "bitstamp": "Bitstamp", "bitfinex": "Bitfinex"}


# Who supplied which stretch of a store by hand, inside the live recount: a
# source never votes on a move whose bars it supplied - its word there is the
# store's. Each entry: the instruments (tickers, or a template), from, to (a
# day, exclusive), and the check if only one. A seam is known to the day, so
# the day either side is left out too.
SUPPLIED: "dict[str, tuple[tuple, ...]]" = {
    # TUR's holes of 2026-10-01 and -02, mended from Yahoo by hand.
    "yahoo": ((("TUR",), "2026-10-01", "2026-10-03"),),
}


def supplied(name: str, asset: Asset, c: dict) -> bool:
    """Whether source `name` supplied the store's bars a move spans: there it
    is no voter."""
    for which, start, end, *check in SUPPLIED.get(name, ()):
        if (asset.session_template != which if isinstance(which, str)
                else asset.ticker not in which):
            continue
        if check and c["check"] != check[0]:
            continue
        lo = _day(start) - 86400 if start else -math.inf
        hi = _day(end) + 86400 if end else math.inf
        if any(lo <= h < hi for h in (c["prev_hour"], c["hour"])):
            return True
    return False


def _day(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def recount_days(asset: Asset) -> float:
    """How long a reading's vote is counted again: while the closest-reaching
    of the instrument's sources still serves its hour. After that, the
    further ones would vote alone, and the vote stands as it is."""
    return min(src.days for src in sources_for(asset))


def due(row: "dict | None", closed: "int | None") -> bool:
    """Whether a reading inside its recount is counted this run: on the run
    that finds it, then on the first run after each end of its market's
    session (`closed`, sessions.last_close)."""
    return row is None or (closed is not None and int(row.get("checked_utc") or 0) < closed)


def recount_hours(asset: Asset, record: dict, now: int) -> "list[int]":
    """The hours of the instrument's votes due a count this run, inside its
    recount: its own provider is asked for them again too (jump.backfill), so
    a bar it has corrected since is what the sources vote on."""
    if not sources_for(asset):
        return []
    from jump import sessions

    since, closed = now - recount_days(asset) * 86400, sessions.last_close(
        asset.session_template, now)
    return sorted({h for (a, h, _), row in record.items()
                   if a == asset.asset_id and h >= since and due(row, closed)})


def sources_for(asset: Asset) -> "list[Source]":
    """Every source of the instrument's class that carries it, except its own
    provider."""
    group = SOURCES.get(_CLASS.get(asset.session_template, asset.session_template), ())
    return [s for s in group if s.name != asset.fetched_from and s.symbol(asset)
            and all(os.environ.get(k, "").strip() for k in s.keys)]


def fetch_verifier(name: str, symbol: str, interval: str, days: float,
                   session: "requests.Session | None", now: datetime) -> pd.DataFrame:
    """A source's bars folded onto the store's hourly grid."""
    from datetime import timedelta

    from price_monitor import alpaca, coinbase, kraken, marketwatch, sina, yahoo

    if name == "alpaca_sip":
        # The free plan refuses an end less than fifteen minutes back.
        end = now - timedelta(minutes=16)
        auth = alpaca.headers(*(os.environ.get(k, "").strip() for k in ALPACA_KEYS))
        return bars.to_hourly(bars.candles_to_frame(alpaca.fetch_history(
            symbol, end - timedelta(days=max(days, 1.0)), end, auth, session, feed="sip")))
    if name in ("coinbase", "kraken"):
        # Only hours that have ended: the open one is still moving.
        end = now.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=max(days, 1.0))
        client = coinbase if name == "coinbase" else kraken
        return bars.to_hourly(bars.candles_to_frame(
            client.fetch_history(symbol, start, end, session)))
    if name == "marketwatch":
        candles = marketwatch.fetch_hourly(symbol, session, now)
    elif name == "wallstreetcn":
        candles = wallstreetcn.fetch_hourly(symbol, session, now, days=max(days, 1.0))
    elif name == "ft":
        candles = ft.fetch_hourly(symbol, session, now, days=max(days, 1.0))
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
               settings=None, record: "dict | None" = None) -> "list[dict]":
    """The detector's own readings, judgeable from `since` on (default
    PENDING_HOURS ago), at candidate_line or more: the metrics built as the
    pipeline builds them (pipeline.build_asset_metrics - the hour's move from
    the previous close, or from its own open on a session's first bar and
    after a hole; the gap dividend-adjusted, unscored where the pipeline
    leaves it so), scored as jump.jumps scores them, and found when it
    finds them (jumps.ended: an hour once it has ended, a pair's gap at its
    open), with the detector's window and levels (`settings`, read from
    config/basket.yaml once per pass). Only the recent bars are read:
    tail_days before the window, more than the detector's own.

    The detector scores with the moves voted not real left out of the
    yardstick and the nights moved (jump.jumps._flag): with the `record`, a
    move far only against that yardstick - lifted once a bad print left it -
    is a candidate too. The raw yardstick's stay, so a move voted not real is
    still asked about, and its vote counted again rather than lost."""
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

    def far_readings(m: pd.DataFrame) -> pd.DataFrame:
        readings = [jumps.score(m[["hour_utc", "r"]], template, window, levels),
                    jumps.score_gaps(m[["hour_utc", "gap"]], window, levels, template)]
        readings = jumps.ended(pd.concat([f for f in readings if not f.empty],
                                         ignore_index=True), template, now)
        return readings[(readings["hour_utc"] >= since)
                        & (readings["z"].abs() >= candidate_line(levels))]

    far = far_readings(metrics)
    mine = {k: row for k, row in (record or {}).items() if k[0] == asset.asset_id}
    doubts = {k: row for k, row in mine.items() if row.get("verdict") == NOT_REAL}
    nights = _nights(mine)
    if doubts or nights:
        nightly = jumps.with_nights(metrics, asset.asset_id, nights)
        corrected = jumps.without_not_real(nightly, asset.asset_id, doubts)
        far = pd.concat([far, far_readings(corrected)])
        # A move voted not real leaves that yardstick itself too: one far only
        # against the others' is scored with itself back in, or its vote
        # would vanish the count after it was taken, and return the next.
        have = {(int(h), CLOSE if kind == jumps.HOUR else OPEN)
                for h, kind in zip(far["hour_utc"], far["reading"])}
        for key in doubts:
            if key[1] < since or (key[1], key[2]) in have:
                continue
            alone = jumps.without_not_real(nightly, asset.asset_id,
                                           {k: r for k, r in doubts.items() if k != key})
            mine = far_readings(alone)
            mine = mine[(mine["hour_utc"] == key[1])
                        & ((mine["reading"] == jumps.HOUR) == (key[2] == CLOSE))]
            far = pd.concat([far, mine])
        far = far.drop_duplicates(["hour_utc", "reading"])
    h = metrics["hour_utc"].to_numpy(dtype="int64")
    close = metrics["close"].to_numpy(dtype="float64")
    opened = metrics["open"].to_numpy(dtype="float64")
    first = metrics["is_session_open"].to_numpy(dtype=bool)
    own = first | np.isfinite(metrics["hole"].to_numpy(dtype="float64"))
    at = {int(hour): i for i, hour in enumerate(h)}
    out = []
    for hour, kind in zip(far["hour_utc"].astype("int64"), far["reading"]):
        i = at[int(hour)]
        if i == 0:
            continue
        if kind == jumps.HOUR:
            start = (int(h[i]), float(opened[i])) if own[i] else (int(h[i - 1]), float(close[i - 1]))
            c = {"hour": int(h[i]), "check": CLOSE, "from_open": bool(own[i]),
                 "prev_hour": start[0], "prev_close": start[1], "price": float(close[i])}
            if first[i]:
                # A session's first hour: where the previous session closed,
                # for the night correction (judge_all).
                c["night_hour"], c["night_close"] = int(h[i - 1]), float(close[i - 1])
            out.append(c)
        else:
            out.append({"hour": int(h[i]), "check": OPEN, "from_open": False,
                        "prev_hour": int(h[i - 1]), "prev_close": float(close[i - 1]),
                        "price": float(opened[i])})
    return out


# --- the verdict ---------------------------------------------------------------

def judge(c: dict, v: pd.DataFrame) -> "tuple[str, float, float]":
    """(answer, stored move, the source's move) for one candidate, against the
    source's hourly bars `v` (hour_utc, open, close) as it serves them now:
    confirmed, unconfirmed - including a move it has not shown yet - or
    unknown, with no bars around the move."""
    stamps = np.sort(v["hour_utc"].to_numpy(dtype="int64"))
    vclose = dict(zip(v["hour_utc"].astype("int64").tolist(), v["close"].tolist()))
    vopen = dict(zip(v["hour_utc"].astype("int64").tolist(), v["open"].tolist()))
    h, hp, lag, stale = c["hour"], c["prev_hour"], LAG_HOURS * HOUR, STALE_HOURS * HOUR
    p = math.log(c["price"] / c["prev_close"])

    # Where the market was before the move: for an hour measured from its own
    # open, the source's open of that hour; else its closes up to an
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
        if not len(later) or int(later[0]) - h > stale:
            return UNKNOWN, p, 0.0
        afters = [vclose[int(later[0])]]

    moves = [math.log(b / a) for a in befores for b in afters]
    with_it = max(moves, key=lambda m: m * np.sign(p))
    # The move it is reported with is the source's own over the same span
    # - or its nearest bars either side; the lag window only decides.
    start = befores[0] if c.get("from_open") else \
        vclose.get(hp, vclose[int(stamps[stamps <= hp][-1])] if (stamps <= hp).any() else befores[-1])
    end = vopen[h] if c["check"] == OPEN and h in vopen else \
        vclose.get(h, vclose[int(stamps[stamps >= h][0])] if (stamps >= h).any() else afters[0])
    direct = math.log(end / start)
    if with_it * p > 0 and abs(with_it) >= REAL_SHARE * abs(p):
        return CONFIRMED, p, direct
    return UNCONFIRMED, p, direct


def switches(store: pd.DataFrame, v: pd.DataFrame, template: str) -> "list[int]":
    """The first hours of the sessions the source changed contract at:
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
    """A source's bars with a real price only: a zero, negative or
    missing open or close is not a price, as in the store's bar gate."""
    prices = v[["open", "close"]].to_numpy(dtype="float64")
    return v[(np.isfinite(prices) & (prices > 0)).all(axis=1)].reset_index(drop=True)


def _count(yes: int, no: int) -> str:
    return REAL if yes > no else UNCERTAIN if yes == no else NOT_REAL


def combine(verdicts: "list[tuple[str, str, float, float]]") -> "tuple[str, float, list, list, list]":
    """The vote on one move, from every source asked, each (name, answer,
    stored move, its move). The stored provider is one vote that saw it; a
    source that confirmed it is another; one that did not move with it, or
    has not yet, is a vote against. A source with nothing to show - down, no
    bars around the move, across its own change of contract: an outage - is
    left out of the count, and still named. Most saw it: real; a tie:
    uncertain; most did not: not real. With every source asked out, nobody
    could check it: uncertain. Returns (result, stored move, the sources,
    their moves - None for an outage -, those that saw it)."""
    stored = verdicts[0][2] if verdicts else 0.0
    seen = [v[0] for v in verdicts if v[1] == CONFIRMED]
    answered = [v for v in verdicts if v[1] != UNKNOWN]
    result = (UNCERTAIN if verdicts and not answered
              else _count(1 + len(seen), len(answered) - len(seen)))
    return (result, stored, [v[0] for v in verdicts],
            [None if v[1] == UNKNOWN else v[3] for v in verdicts], seen)


def _around(c: dict, v: pd.DataFrame) -> bool:
    """Whether a source has bars of its own around the move: within LAG_HOURS
    of where it starts and of where it ends - not just a bridge across hours
    it has no bar for."""
    stamps = v["hour_utc"].to_numpy(dtype="int64")
    h, hp, lag = c["hour"], c["prev_hour"], LAG_HOURS * HOUR
    before = (h in set(stamps.tolist())) if c.get("from_open") else \
        bool(((stamps >= hp - lag) & (stamps <= hp)).any())
    return before and bool(((stamps >= h) & (stamps <= h + lag)).any())


def judge_all(c: dict, asked: "list[tuple[str, pd.DataFrame | None]]"
              ) -> "tuple[str, float, list, list, list]":
    """One candidate put to every source asked, each (name, its bars, or None
    for a source that was down), and the vote. A source with bars of its own
    around the move outweighs one bridging hours it has none for: USD/INR's
    night, where Yahoo's last bar is 10:00 and MarketWatch has every hour, is
    MarketWatch's to judge - a seventeen-hour bridge would read the night's
    drift as the move - and the bridging one counts as an outage."""
    p = math.log(c["price"] / c["prev_close"])
    direct = {name for name, v in asked if v is not None and _around(c, v)}
    votes = [(name, UNKNOWN, p, 0.0) if v is None or (direct and name not in direct)
             else (name,) + judge(c, v) for name, v in asked]
    result, stored, names, moves, seen = combine(votes)
    if result == NOT_REAL and c.get("night_hour") is not None:
        night = overnight_move(c, asked)
        if night is not None:
            return OVERNIGHT, stored, night[0], night[1], night[0]
    return result, stored, names, moves, seen


def overnight_move(c: dict, asked: "list[tuple[str, pd.DataFrame | None]]"
                   ) -> "tuple[list, list] | None":
    """A session's first hour most feeds did not see move - but did most see
    the move from the previous session's close to this bar's close, the
    store one of them and an outage left out? Then it happened overnight: the
    store's first print was a stale one at the old price (TLH 2020-03-09:
    gap +0.03%, first hour +3.8%; the tape opened +4.65%). Returns the names
    of the feeds that saw it and each one's night - its open of the hour over
    its last close before the night - or None."""
    whole = dict(c, from_open=False, prev_hour=c["night_hour"], prev_close=c["night_close"])
    p = math.log(c["price"] / c["night_close"])
    votes = [(name, UNKNOWN, p, 0.0) if v is None else (name,) + judge(whole, v)
             for name, v in asked]
    if combine(votes)[0] != REAL:
        return None
    names, nights = [], []
    for (name, v), vote in zip(asked, votes):
        if vote[1] != CONFIRMED:
            continue
        stamps = v["hour_utc"].to_numpy(dtype="int64")
        opens = dict(zip(stamps.tolist(), v["open"].tolist()))
        before = stamps[(stamps <= c["night_hour"])
                        & (stamps >= c["night_hour"] - STALE_HOURS * HOUR)]
        if c["hour"] not in opens or not len(before):
            continue
        last = float(v.loc[v["hour_utc"] == before.max(), "close"].iloc[0])
        names.append(name)
        nights.append(math.log(opens[c["hour"]] / last))
    return (names, nights) if names else None


# --- the record ----------------------------------------------------------------

def load(path: "str | None" = None) -> "dict[tuple[str, int, str], dict]":
    """{(asset_id, hour_utc, check): row}."""
    path = path or VERIFIED_PATH
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        rows = [row for row in csv.DictReader(f) if row["verdict"] != "pending"]
    for row in rows:
        if row["verdict"] == UNKNOWN:
            # Before the vote: no other source had bars. Each was an outage.
            asked = [n for n in str(row.get("verifier") or "").split(",") if n]
            row.update(verdict=_count(1, len(asked)), verifier_move="," * (len(asked) - 1))
        row["verdict"] = _BEFORE_THE_VOTE.get(row["verdict"], row["verdict"])
    return {(row["asset_id"], int(row["hour_utc"]), row["check"]): row for row in rows}


def _nights(record: dict) -> "dict[tuple[str, int, str], float]":
    """The record's first hours the feeds saw happen overnight, each with the
    night's move, the median of the feeds'."""
    out = {}
    for k, row in record.items():
        if row.get("verdict") == OVERNIGHT:
            moves = [float(m) for m in str(row["verifier_move"]).split(",") if m]
            if moves:
                out[k] = float(np.median(moves))
    return out


def overnight(path: "str | None" = None) -> "dict[tuple[str, int, str], float]":
    """The first hours the feeds saw happen overnight, {(asset_id, hour_utc,
    check): the night's move, the median of the feeds'}."""
    try:
        return _nights(load(path))
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        log.warning("verify: could not read the record - %s", exc)
        return {}


def votes(path: "str | None" = None) -> "dict[tuple[str, int, str], dict]":
    """Every vote on record, {(asset_id, hour_utc, check): row}. A record that
    cannot be read leaves everything scored and unlabelled, loudly."""
    try:
        return load(path)
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        log.warning("verify: could not read the record - %s", exc)
        return {}


def not_real(path: "str | None" = None) -> "dict[tuple[str, int, str], dict]":
    """The moves most sources did not see, {(asset_id, hour_utc, check): row}."""
    return {k: row for k, row in votes(path).items() if row.get("verdict") == NOT_REAL}


def write(record: dict, now: int, path: "str | None" = None) -> None:
    path = path or VERIFIED_PATH
    keep = [row for row in record.values()
            if row["verdict"] in KEPT
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

def _row(asset: Asset, c: dict, verdict: str, stored: float, names: list, moves: list,
         seen: list, now: int) -> dict:
    return {"asset_id": asset.asset_id, "hour_utc": c["hour"], "check": c["check"],
            "verdict": verdict, "provider": asset.fetched_from,
            "verifier": ",".join(names), "seen": ",".join(seen),
            "stored_move": f"{stored:.6f}",
            "verifier_move": ",".join("" if m is None else f"{m:.6f}" for m in moves),
            "checked_utc": now}


def verify(instruments, bars_dir: str, table, session=None, now: "datetime | None" = None,
           path: "str | None" = None, blocked: "set[str] | None" = None) -> dict:
    """One pass: every reading due a vote (due), voted from the bars as they
    now are. Returns how many of each result."""
    from price_monitor import yahoo
    from jump import corporate_actions, jumps, sessions
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
    todo = []
    for asset in instruments:
        sources = sources_for(asset)
        if not sources:
            continue
        since = now_ts - recount_days(asset) * 86400
        frame = bars.load(bars.store_path(bars_dir, asset.file_stem),
                          since - tail_days(settings[0]) * 86400)
        if frame.empty:
            continue
        found = candidates(asset, frame, table, now_ts, since, basket, dividends, settings,
                           record)
        current = {(asset.asset_id, c["hour"], c["check"]) for c in found}
        for key in [k for k in record if k[0] == asset.asset_id and k[1] >= since]:
            if key not in current:
                del record[key]
        closed = sessions.last_close(asset.session_template, now_ts)
        found = [c for c in found
                 if due(record.get((asset.asset_id, c["hour"], c["check"])), closed)]
        if found:
            fresh = any((asset.asset_id, c["hour"], c["check"]) not in record for c in found)
            todo.append((not fresh, asset, found, frame))

    # One request per source and instrument; the instruments with a reading not
    # yet judged first, so a busy hour cannot leave the same ones unasked run
    # after run. A source that fails leaves the others to answer; a rate limit
    # stops that source for the rest of the run, as does not answering
    # UNANSWERED_IN_A_ROW requests in a row (~96 s each of a 20-minute job).
    counts = {REAL: 0, UNCERTAIN: 0, NOT_REAL: 0, OVERNIGHT: 0}
    unanswered: dict[str, int] = {}
    requests_left = MAX_REQUESTS
    doubted: list[str] = []
    for _, asset, found, frame in sorted(todo, key=lambda d: d[0]):
        # From before the earliest bar a move starts at: a Monday gap starts at
        # Friday's close.
        days = (now_ts - min(c["prev_hour"] for c in found)) / 86400 + 1
        # Every source is asked, or none this run: a vote of all of them, not
        # of whichever were asked. One that is stopped, or fails, is an outage
        # (None), left out of the count. A source whose delay keeps it from
        # serving any of the moves whole is not asked, and joins the next count.
        sources = [src for src in sources_for(asset)
                   if any(src.serves(c, now_ts) for c in found)]
        if requests_left < sum(src.name not in blocked for src in sources):
            continue
        asked: "list[tuple[str, pd.DataFrame | None]]" = []
        for src in sources:
            name = src.name
            if name in blocked:
                asked.append((name, None))
                continue
            requests_left -= 1
            try:
                asked.append((name, _priced(fetch_verifier(name, src.symbol(asset), src.interval,
                                                           days, session, now_dt))))
                unanswered.pop(name, None)
            except Exception as exc:
                log.warning("verify: %s from %s failed - %s", asset.ticker, name, exc)
                asked.append((name, None))
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
        rolls = {src.name for src in sources if src.own_rolls}
        changed = {name: switches(frame, v, asset.session_template)
                   for name, v in asked if name in rolls and v is not None}
        for c in found:
            try:
                # A source across its own change of contract has nothing to
                # say about the move: an outage for it. One that cannot serve
                # the move whole yet is no voter on it this time.
                usable = [(src.name, None if v is None or crosses(c, changed.get(src.name, []))
                           else v)
                          for src, (_, v) in zip(sources, asked)
                          if src.serves(c, now_ts) and not supplied(src.name, asset, c)]
                verdict, stored, names, moves, seen = judge_all(c, usable)
            except Exception as exc:
                # One instrument's surprise costs that reading, not the pass.
                log.warning("verify: %s %s could not be judged - %s", asset.ticker,
                            c["hour"], exc)
                continue
            counts[verdict] += 1
            record[(asset.asset_id, c["hour"], c["check"])] = _row(
                asset, c, verdict, stored, names, moves, seen, now_ts)
            if verdict == NOT_REAL:
                theirs = ", ".join(f"{n} {'outage' if m is None else f'{100 * m:+.2f}%'}"
                                   for n, m in zip(names, moves))
                doubted.append(f"{asset.ticker} {datetime.fromtimestamp(c['hour'], timezone.utc):%Y-%m-%d %H:%M}"
                               f" {c['check']} ({asset.fetched_from} {100 * stored:+.2f}%; {theirs})")
    write(record, now_ts, path)
    if doubted:
        log.warning("verify: not real, most sources did not see it - %s", "; ".join(doubted))
    log.info("verify: %s", ", ".join(f"{k} {n}" for k, n in counts.items()))
    # The sources stopped during this pass, by a rate limit or silence.
    return dict(counts, not_real_moves=doubted, stopped=sorted(blocked - told))


def main(argv: "list[str] | None" = None) -> int:
    from jump import sessions
    from jump.basket import load_basket

    parser = argparse.ArgumentParser(description="One pass of the sources' vote")
    parser.add_argument("--bars-dir", default=bars.DEFAULT_BARS_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(verify(load_basket().instruments, args.bars_dir, sessions.load_sessions()))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

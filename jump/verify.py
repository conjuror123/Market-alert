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

WHO IS ASKED (SOURCES, sources_for). Every source that carries the instrument
and moves with its store - each held to one bar in the source audit of
2026-10-13 (docs/decisions.md, "Every source on every instrument") - except its
own provider:

    the pairs          Yahoo, MarketWatch, Wallstreetcn, Tiingo, Twelve Data (not USD/CNH)
    gold, silver       Yahoo's and MarketWatch's COMEX futures, Sina's and
                       Wallstreetcn's spot, Tiingo, Twelve Data (gold)
    the funds          Yahoo's and Sina's half hours, MarketWatch, Alpaca's tape
                       (Alpaca_SIP, fifteen minutes behind: asked once it serves the
                       move's hour whole, Source.delay); UNG also Wallstreetcn's
                       natural gas future, Tiingo and SiftingIO; SPY SiftingIO;
                       REMX Tiingo
    the coins          Coinbase, Kraken, Bitstamp, Bitfinex, Alpaca's and Yahoo's
                       dollar pairs - a wick on Binance, whose prices are its own
                       trades, is real there and not the market's
    the futures        MarketWatch's continuous series; the FT's bars of the
                       contract the store holds (grains, platinum, live cattle) or
                       its continuous series (coffee, cocoa, Brent); Sina's
                       softs, Brent and spot platinum; Wallstreetcn's cocoa,
                       cotton and Brent; SiftingIO's spot platinum
    the LME            Wallstreetcn; copper also Yahoo's COMEX future
    the indices        MarketWatch (not the TAIEX or CSI 300), Wallstreetcn's
                       Sensex, Saudi and Canadian, the FT's Euro Stoxx, FTSE, SMI,
                       Bovespa and IPC, Naver's KOSPI, the Taiwan exchange's
                       TAIEX, Sina's CSI 300

Two vendors printing the same bars agree: each is a voice. A source whose bar
in one local hour is another grid's is not asked about a move in that hour
(Source.skip_hours: the FT's last hour in Europe, the TAIEX's 13:00, the CSI
300's 11:00), where it is neither a vote nor an outage. The keyed providers -
Tiingo, Twelve Data, SiftingIO - vote within the votes' share of their
allowance (config/provider_limits.csv, vote_room); past it, each is an outage
for the rest of the run, named on the health chat.

THE VOTE (judge_all, combine) is taken with what the sources serve when it is
taken: a source that has not shown the move yet counts against, until the
next count. Every source of an instrument is asked, or none that run
(MAX_REQUESTS, VOTE_SECONDS): a vote of all of them, not of whichever were
asked. With every other source down, the move is uncertain
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
between them is not a move. A move is one bar's: the source's own price at
the same two bars as the store's - its close of the store's previous bar (or
its open of this one, for an hour measured from its open) and its close of
this bar (its open, for a session's gap). CONFIRMED if it moved the same way
at least REAL_SHARE as far; UNCONFIRMED if it did not, or has not yet; UNKNOWN
- an outage - if it has no bar at either end. No window and no bridge: a
source an hour late does not see the move, and one with a hole there is out
(the user's, 2026-10-12).

THE RECORD (VERIFIED_PATH) is what the detector and delivery read. A reading
is voted on the run that finds it, then again from the bars as they then are
on the first run after each end of its market's session (due,
sessions.last_close: the NYSE close for funds, a daily-session market's own
close, 00:00 UTC for coins and pairs) - its own provider re-read first
(recount_hours, jump.backfill) - until the closest-reaching of its
sources no longer serves its hour (recount_days: nine days wherever MarketWatch
votes, 29 for the coins, 79 for the LME), held inside the record's KEEP_DAYS
(the CSI 300's Sina reaches a year, counted to 89); after that the further
ones would vote alone, and the vote stands. A
source that corrects its bars turns the vote back; a bar that heals into no far
move loses its vote. A move not real is kept for good, because the detector rescores the whole history
every run; the rest go after KEEP_DAYS. A record from before the vote reads in
its words (_BEFORE_THE_VOTE).

OVERNIGHT, for a session's first hour: most feeds did not see the hour move,
but most saw the move from the previous session's close to that bar's close,
the store one of them - each on its own bars there, exactly as judge reads
them, an outage without them. The move happened in the night, and the store's
first print was a stale one at the old price; jump.jumps moves it into the gap
with the feeds' night. Kept for good, like a move not real.

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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping

import numpy as np
import pandas as pd
import requests

from price_monitor import alpaca, coinbase, ft, kraken, marketwatch, sina, wallstreetcn
from price_monitor.models import UNANSWERED_IN_A_ROW, Unreachable
from jump import atomic, bars, sessions
from jump.basket import Asset

log = logging.getLogger("jump.verify")

HOUR = 3600
VERIFIED_PATH = os.path.join("data", "jump", "verified.csv")
COLUMNS = ["asset_id", "hour_utc", "check", "verdict", "provider", "verifier", "seen",
           "stored_move", "verifier_move", "checked_utc"]

CANDIDATE_SIGMA = 4.0
REAL_SHARE = 0.5
PENDING_HOURS = 24
TAIL_DAYS = 200                   # bars read before the window: the detector's half-year and more
# A run's requests and seconds for the vote, at most: the busiest count, at
# 00:00 UTC when the pairs, metals and coins all close their day, asks about 70
# (13 instruments, 4-7 sources each); the hourly job's fetch step has twelve
# minutes, and the whole run takes about three (2026-10-13).
MAX_REQUESTS = 120
VOTE_SECONDS = 300
KEEP_DAYS = 90                    # longer than any recount (recount_days)

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
# (measured 2026-10-07).
MARKETWATCH_FUND_EXCHANGE = dict.fromkeys(("SHY", "IEF", "TLT", "EMB"), "XNAS")

# The softs Yahoo serves, as Sina's global futures name them (the same endpoint
# the LME's metals come from): against the stored bars over 2026-05 to 10,
# 0.2-2.2 bp apart at the median, hourly moves correlated 0.96-0.99, no hour
# missing. Live cattle is there too (LE), but as quotes without volume whose
# hours correlate 0.80 with the store's: not used.
SINA_FUTURES = {"KC=F": "KC", "CC=F": "CC", "CT=F": "CT"}
# London's spot gold and silver, from the same global service.
SINA_METALS = {"XAU/USD": "XAU", "XAG/USD": "XAG"}

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


@dataclass(frozen=True, eq=False)
class Source:
    """A feed that can be asked about another feed's move. `name` is the
    provider it is, so an instrument is never asked of its own provider;
    `symbols`, what it calls each instrument it votes on, by ticker - a name,
    or a function of the instrument for one that changes (the FT's contract the
    store holds today); `days`, how far back it serves, measured; `own_rolls`,
    a continuous future that changes contract on its own days, or a spot price
    beside a future the store rolls; `delay`, seconds it serves behind the
    clock - it is not asked about a move it cannot serve whole yet, and joins
    the next count; `keys`, the secrets it needs - without them it is no voter
    at all, rather than an outage named on every vote; `skip_hours`, by ticker,
    the local hours of the instrument's market whose moves it is not asked
    about, its bar there being another grid's (the FT's last hour in Europe):
    there it is neither a vote nor an outage."""
    name: str
    label: str
    days: float
    interval: str
    symbols: "Mapping[str, str | Callable[[Asset], str | None]]"
    own_rolls: bool = False
    delay: int = 0
    keys: "tuple[str, ...]" = ()
    skip_hours: "Mapping[str, tuple[int, ...]]" = field(default_factory=dict)

    def symbol(self, asset: Asset) -> "str | None":
        """What it calls the instrument, or None: it does not vote on it."""
        name = self.symbols.get(asset.ticker)
        return name(asset) if callable(name) else name

    def serves(self, c: dict, now: int) -> bool:
        """Whether it can answer about the move by `now`: one without a delay
        answers with what it has; a delayed one once it serves the move's hour
        whole."""
        return not self.delay or c["hour"] + HOUR + self.delay <= now

    def skips(self, asset: Asset, c: dict) -> bool:
        """Whether the move is in one of the hours it is not asked about."""
        hours = self.skip_hours.get(asset.ticker)
        if not hours:
            return False
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(sessions.DAILY_SESSIONS[asset.session_template][0])
        return datetime.fromtimestamp(c["hour"], zone).hour in hours


ALPACA_KEYS = ("ALPACA_KEY_ID", "ALPACA_SECRET_KEY")
KEYS = {"tiingo": "TIINGO_API_KEY", "twelvedata": "TWELVEDATA_API_KEY",
        "sifting": "SIFTING_API_KEY"}
# The provider a source's calls count against, where its name is not that
# (config/provider_limits.csv, jump.usage).
PROVIDER = {"alpaca_sip": "alpaca", "alpaca_crypto": "alpaca"}

PAIRS = ("EUR/USD", "USD/JPY", "GBP/USD", "AUD/USD", "USD/CHF", "USD/CAD", "USD/CNH")
COINS = ("BTC", "ETH", "SOL", "XRP")
FUNDS = ("XLK", "XLF", "XLE", "XLV", "SPY", "IWM", "TIP", "LQD", "HYG", "EMB", "UNG", "REMX")


def _pair(ticker: str) -> str:
    return ticker.replace("/", "").upper()


def _coins(name: "Callable[[str], str]") -> "dict[str, str]":
    """The coins (BTC/USDT) by what a source calls each coin against the
    dollar, `name` given the coin (BTC)."""
    return {f"{coin}/USDT": name(coin) for coin in COINS}


def _ft_cattle(asset: Asset) -> "str | None":
    """The FT's name for the contract the store holds today (LCZ26)."""
    from jump import futures
    held = futures.front_contract(asset.ticker, datetime.now(timezone.utc).date())
    return ft.cattle_symbol(held[0] if held else None)


def _ft_future(asset: Asset) -> "str | None":
    """The FT's name for the stage 2 contract the store holds today (CZ26,
    US@PLF272), or None for one it does not carry."""
    from jump import futures
    held = futures.front_contract(asset.ticker, datetime.now(timezone.utc).date())
    return ft.futures_symbol(held[0] if held else None)


# Sina's global futures for stage 2's: ICE Brent's continuous series and London's
# spot platinum. Against Yahoo's single contract, 2026-08 to 10: 0.944 and 0.928.
# Its grains (C, W, S: 0.78-0.85) are left out; it holds no Treasuries or sugar.
SINA_STAGE2 = {"BZ=F": "OIL", "PL=F": "XPT"}


# Every source, and what it calls each instrument it votes on. Each was held to
# the same bar (the source audit, 2026-10-13, docs/decisions.md "Every source on
# every instrument"): hourly moves 0.90 or more with the store's, nine in ten of
# the store's big moves confirmed, and no local hour where three or more were
# not. Reach measured 2026-10-08 to 13: Yahoo's 30-minute bars 59 days and hourly
# 729 (price_monitor.yahoo); Sina's fund bars about 78 trading days, its softs
# back to 2026-05-12 (coffee, cocoa) and 07-20 (cotton), its Brent and metals 62
# days, counted as 60; MarketWatch about nine trading days; Wallstreetcn about
# 170 days, its LME counted to 79 (the record keeps a vote KEEP_DAYS, and the
# store's LME bars before 2026-07-22 are Wallstreetcn's own); the FT 24 trading
# days, counted as 30; Kraken 720 hours; Coinbase, Bitstamp, Bitfinex and
# Alpaca's coins page back years, counted as 365; Alpaca's consolidated tape
# (SIP) from 2016-01-01, to fifteen minutes back on the free plan
# (price_monitor.alpaca); Naver's KOSPI about 12 days, counted as 10; the Taiwan
# exchange's day reports any day, asked to nine (a request a day). The keyed
# providers - Tiingo, Twelve Data, SiftingIO - vote within the votes' share of
# config/provider_limits.csv, on the instruments that share covers (vote_room).
SOURCES: "tuple[Source, ...]" = (
    # Yahoo: the pairs' and coins' hourly bars, the funds' half hours folded,
    # COMEX's gold, silver and copper futures.
    Source("yahoo", "Yahoo", 729, "1h",
           {**{t: _pair(t) + "=X" for t in PAIRS}, **_coins(lambda c: f"{c}-USD")}),
    Source("yahoo", "Yahoo", 729, "1h",
           {"XAU/USD": "GC=F", "XAG/USD": "SI=F", "CAD": "HG=F"}, own_rolls=True),
    Source("yahoo", "Yahoo", 59, "30min", {t: t for t in FUNDS}),
    # Sina: the funds' half hours, its global futures and London's metals, the
    # CSI 300. Its softs change contract on their own days (SINA_FUTURES).
    Source("sina", "Sina", 77, "30min", {t: t for t in FUNDS}),
    Source("sina", "Sina", 79, "1h", SINA_FUTURES, own_rolls=True),
    Source("sina", "Sina", 60, "1h", SINA_STAGE2, own_rolls=True),
    Source("sina", "Sina", 60, "1h", SINA_METALS),
    # Its CSI 300 apart from 11:00, the morning session's last half hour before
    # the lunch break: 0.69 there, 0.98 in the afternoon (the user's, 2026-10-13).
    Source("sina", "Sina", 365, "1h", sina.CN_INDICES, skip_hours={"000300.SS": (11,)}),
    # MarketWatch: the pairs, the funds by their listing exchange, the indices,
    # live cattle's continuous contract; the futures' continuous series. Its
    # TAIEX apart from 13:00, as the exchange's: 1.00 at 10:00-12:00 Taipei, 0.46
    # there (2026-10-13).
    Source("marketwatch", "MarketWatch", 9, "1h", {
        **{t: "CURRENCY/US/XTUP/" + _pair(t) for t in PAIRS},
        **{t: "FUND/US/{}/{}".format(MARKETWATCH_FUND_EXCHANGE.get(t, "ARCX"), t)
           for t in FUNDS},
        **marketwatch.INDICES, "LE=F": MARKETWATCH_CATTLE}, skip_hours={"^TWII": (13,)}),
    Source("marketwatch", "MarketWatch", 9, "1h",
           {**marketwatch.FUTURES, **marketwatch.METALS, **marketwatch.SOFTS}, own_rolls=True),
    # Wallstreetcn: the pairs and spot metals, the indices, the LME, its
    # continuous softs and futures.
    Source("wallstreetcn", "Wallstreetcn", 170, "1h",
           {t: wallstreetcn.pair_code(t) for t in PAIRS + ("XAU/USD", "XAG/USD")}),
    Source("wallstreetcn", "Wallstreetcn", 170, "1h", wallstreetcn.INDICES),
    Source("wallstreetcn", "Wallstreetcn", 79, "1h", wallstreetcn.LME),
    Source("wallstreetcn", "Wallstreetcn", 170, "1h",
           {**wallstreetcn.SOFTS, **wallstreetcn.FUTURES}, own_rolls=True),
    # The FT: its continuous softs and Brent, the contract the store holds for
    # live cattle, the grains and platinum - each across a change of contract
    # an outage (own_rolls) - and five indices. Europe's three end their last
    # hour before the closing auction that Yahoo's bar holds: 0.08-0.19 in that
    # hour, so not asked about a move in it (the user's, 2026-10-13).
    # Its S&P Euro-Bund index has bars only at 13:00-17:00 UTC (15:00-19:00
    # Frankfurt in summer, 14:00 on in winter if held to UTC): the Bund's
    # 08:00-14:00 is never served, and not asked about (2026-10-13).
    Source("ft", "FT", 30, "1h",
           {**ft.SOFTS, **ft.FUTURES_CONTINUOUS, "LE=F": _ft_cattle,
            **{t: _ft_future for t in ("ZC=F", "ZW=F", "ZS=F", "PL=F")}}, own_rolls=True,
           skip_hours={"FGBL=F": (8, 9, 10, 11, 12, 13, 14)}),
    Source("ft", "FT", 30, "1h", ft.INDICES,
           skip_hours={"^STOXX50E": (17,), "^FTSE": (16,), "^SSMI": (17,)}),
    # Every exchange's trades: another source than the IEX feeds the funds are
    # fetched from, which are one exchange's (the user's, 2026-10-08).
    Source("alpaca_sip", "Alpaca_SIP", (datetime.now(timezone.utc) - alpaca.FIRST).days + 1,
           "30min", {t: t for t in FUNDS}, delay=15 * 60, keys=ALPACA_KEYS),
    # The coins, served by Binance, whose prices are its own trades: other
    # exchanges' dollar pairs - a wick on one exchange is real there and not the
    # market's.
    Source("coinbase", "Coinbase", 365, "1h",
           _coins(lambda c: coinbase.product_for(f"{c}/USDT"))),
    Source("kraken", "Kraken", 29, "1h", _coins(lambda c: kraken.pair_for(f"{c}/USDT"))),
    Source("bitstamp", "Bitstamp", 365, "1h", _coins(lambda c: f"{c.lower()}usd")),
    Source("bitfinex", "Bitfinex", 365, "1h", _coins(lambda c: f"t{c}USD")),
    Source("alpaca_crypto", "Alpaca", 365, "1h", _coins(lambda c: f"{c}/USD")),
    # Korea's and Taiwan's own. The exchange's TAIEX apart from 13:00, whose bar
    # holds its 13:30 closing auction: 0.50 there, 1.00 in the hours before
    # (the user's, 2026-10-13).
    Source("naver", "Naver", 10, "1h", {"^KS11": "KOSPI"}),
    Source("twse", "TWSE", 9, "1h", {"^TWII": "TAIEX"}, skip_hours={"^TWII": (13,)}),
    # The keyed providers, within the votes' share of their allowance.
    Source("tiingo", "Tiingo", 365, "1h", {t: t for t in PAIRS + ("XAU/USD", "XAG/USD")},
           keys=(KEYS["tiingo"],)),
    Source("tiingo", "Tiingo", 365, "30min", {"UNG": "UNG", "REMX": "REMX"},
           keys=(KEYS["tiingo"],)),
    Source("twelvedata", "Twelve Data", 365, "1h",
           {t: t for t in PAIRS + ("XAU/USD",) if t != "USD/CNH"}, keys=(KEYS["twelvedata"],)),
    Source("sifting", "SiftingIO", 365, "30min", {"SPY": "SPY", "UNG": "UNG"},
           keys=(KEYS["sifting"],)),
    Source("sifting", "SiftingIO", 365, "1h", {"PL=F": "XPT/USD"}, own_rolls=True,
           keys=(KEYS["sifting"],)),
)
# What the channel calls each provider: the sources, and the stores' own.
LABELS = {**{s.name: s.label for s in SOURCES},
          "binance": "Binance", "sifting": "SiftingIO", "tiingo": "Tiingo",
          "alpaca": "Alpaca_IEX", "twelvedata": "Twelve Data",
          # named in the record by the history check before the vote
          "dukascopy": "Dukascopy"}


# Who supplied which stretch of a store by hand, inside the live recount: a
# source never votes on a move whose bars it supplied - its word there is the
# store's. Each entry: the instruments (tickers, or a template), from, to (a
# day, exclusive), and the check if only one. A seam is known to the day, so
# the day either side is left out too.
# None since stage 1 (TUR's holes of 2026-10-01 and -02, mended from Yahoo, left
# with it).
SUPPLIED: "dict[str, tuple[tuple, ...]]" = {}


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
    further ones would vote alone, and the vote stands as it is. Held inside
    the record's KEEP_DAYS: a vote it no longer holds would be due on every
    run (the CSI 300's Sina reaches a year; the store's own Yahoo re-reads 59
    days)."""
    return min(min(src.days for src in sources_for(asset)), KEEP_DAYS - 1)


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
    """Every source that votes on the instrument, except its own provider, and
    a keyed one only with its keys."""
    return [s for s in SOURCES if s.name != asset.fetched_from and s.symbol(asset)
            and all(os.environ.get(k, "").strip() for k in s.keys)]


def provider(name: str) -> str:
    """The provider a source's calls count against (config/provider_limits.csv)."""
    return PROVIDER.get(name, name)


_asked_at: "dict[str, list[float]]" = {}


def _pace(name: str) -> None:
    """Before a request: if the provider's minute share for the vote
    (per_minute) is spent in the last sixty seconds of this process, wait
    until it is not."""
    share = per_minute().get(provider(name))
    if not share:
        return
    recent = [t for t in _asked_at.get(provider(name), []) if time.monotonic() - t < 60]
    if len(recent) >= share:
        time.sleep(max(0.0, 60 - (time.monotonic() - recent[-share])))
    _asked_at[provider(name)] = recent[-share:] + [time.monotonic()]


_PER_MINUTE: "dict[str, int] | None" = None


def per_minute() -> "dict[str, int]":
    """The vote's requests a minute to a provider whose limit is a minute's:
    the votes' share of that minute in config/provider_limits.csv (Twelve
    Data's six of eight, the hourly batch's one beside it). Read once."""
    global _PER_MINUTE
    if _PER_MINUTE is None:
        from jump import usage
        try:
            table = usage.limits()
            minute = table[(table["window"] == "minute") & (table["votes"] > 0)]
            _PER_MINUTE = {row.provider: int(row.votes) for row in minute.itertuples()}
        except (OSError, ValueError, KeyError) as exc:
            log.warning("verify: the limits are unreadable - %s; Twelve Data asked "
                        "one a minute", exc)
            _PER_MINUTE = {"twelvedata": 1}
    return _PER_MINUTE


def vote_room(now: datetime) -> "dict[str, float]":
    """How many requests the vote may still make of each provider this run
    (jump.usage.votes_left): its votes' share of each hour, day and month less
    what the vote has spent of it. A provider without a limit is not here. With
    the limits unreadable, the keyed providers are asked nothing."""
    from jump import usage
    try:
        return usage.votes_left(now)
    except (OSError, ValueError, KeyError) as exc:
        log.warning("verify: the limits are unreadable - %s; the keyed providers "
                    "do not vote this run", exc)
        return dict.fromkeys(KEYS, 0)


def _frame(candles) -> pd.DataFrame:
    return bars.to_hourly(bars.candles_to_frame(candles))


def _regular(candles) -> list:
    """A US fund's half-hour bars inside its regular session only, as the
    tape's (price_monitor.alpaca): a pre-market bar folded into the opening
    hour would put its price in that hour's open."""
    return [c for c in candles if alpaca.regular_session(c.open_time)]


def fetch_verifier(name: str, symbol: str, interval: str, days: float,
                   session: "requests.Session | None", now: datetime) -> pd.DataFrame:
    """A source's bars folded onto the store's hourly grid."""
    from datetime import timedelta

    from price_monitor import (bitfinex, bitstamp, naver, sifting, tiingo, twelvedata,
                               twse, yahoo)

    days = max(days, 1.0)
    # Only hours that have ended, for those that serve the open one.
    hour = now.replace(minute=0, second=0, microsecond=0)
    _pace(name)
    if name == "alpaca_sip":
        # The free plan refuses an end less than fifteen minutes back.
        end = now - timedelta(minutes=16)
        auth = alpaca.headers(*(os.environ.get(k, "").strip() for k in ALPACA_KEYS))
        return _frame(alpaca.fetch_history(symbol, end - timedelta(days=days), end, auth,
                                           session, feed="sip"))
    if name in ("coinbase", "kraken", "bitstamp", "bitfinex", "alpaca_crypto"):
        start = hour - timedelta(days=days)
        if name == "alpaca_crypto":
            return _frame(alpaca.fetch_crypto(symbol, start, hour, session))
        client = {"coinbase": coinbase, "kraken": kraken, "bitstamp": bitstamp,
                  "bitfinex": bitfinex}[name]
        return _frame(client.fetch_history(symbol, start, hour, session))
    key = os.environ.get(KEYS.get(name, ""), "").strip()
    if name == "tiingo":
        candles = tiingo.fetch_full_history(symbol, interval, days, api_key=key,
                                            session=session, end=now)
        return _frame(_regular(candles) if interval == "30min" else candles)
    if name == "twelvedata":
        return _frame(twelvedata.fetch_full_history(
            symbol, interval, days, "https://api.twelvedata.com", key, session, end=now))
    if name == "sifting":
        candles = sifting.fetch_full_history(symbol, interval, days, api_key=key,
                                             session=session, end=now)
        return _frame(_regular(candles) if interval == "30min" else candles)
    if name == "marketwatch":
        candles = marketwatch.fetch_hourly(symbol, session, now)
    elif name == "wallstreetcn":
        candles = wallstreetcn.fetch_hourly(symbol, session, now, days=days)
    elif name == "ft":
        candles = ft.fetch_hourly(symbol, session, now, days=days)
    elif name == "naver":
        candles = naver.fetch_hourly(symbol, session, now, days=days)
    elif name == "twse":
        candles = twse.fetch_hourly(session, now, days=days)
    elif name == "yahoo":
        days = min(days, float(yahoo.MAX_LOOKBACK_DAYS[interval]))
        candles = yahoo.fetch_full_history(symbol, interval, days=days, session=session,
                                           end=now)
    elif name == "sina" and interval == "30min":
        candles = sina.fetch_us_bars(symbol, session, now)
    elif name == "sina" and symbol in sina.CN_INDICES.values():
        candles = sina.fetch_cn_bars(symbol, session, now)
    elif name == "sina":
        candles = sina.fetch_bars(symbol, session, now, url=sina.GLOBAL_URL)
    else:
        raise ValueError(f"unknown verifier {name}")
    return _frame(candles)


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
    still asked about, and its vote counted again rather than lost. A move
    none of the instrument's sources is asked about (skip_hours) is not one."""
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
        elif sessions.is_exchange_template(template):
            # A stock index's night runs to its first bar's close
            # (jump.returns.split_channels): so is it asked about.
            out.append({"hour": int(h[i]), "check": OPEN, "from_open": False,
                        "prev_hour": int(h[i - 1]), "prev_close": float(close[i - 1]),
                        "price": float(close[i]), "to_close": True})
        else:
            out.append({"hour": int(h[i]), "check": OPEN, "from_open": False,
                        "prev_hour": int(h[i - 1]), "prev_close": float(close[i - 1]),
                        "price": float(opened[i])})
    # A move none of its sources is asked about (Source.skip_hours) has no vote
    # to take: counted, the store alone would make it real (the CSI 300's 11:00,
    # the TAIEX's 13:00).
    voters = sources_for(asset)
    return [c for c in out if any(not src.skips(asset, c) for src in voters)] if voters else out


# --- the verdict ---------------------------------------------------------------

def judge(c: dict, v: pd.DataFrame) -> "tuple[str, float, float]":
    """(answer, stored move, the source's move) for one candidate, against the
    source's hourly bars `v` (hour_utc, open, close) as it serves them now. Its
    move is over the same two bars as the store's: from its close of the store's
    previous bar - its open of this bar, for an hour measured from its open - to
    its close of this bar, or its open for a session's gap (a stock index's
    night runs to its first bar's close). Confirmed if it went the same way at
    least REAL_SHARE as far, unconfirmed if not; unknown - an outage - if it has
    no bar at either end."""
    opens = dict(zip(v["hour_utc"].astype("int64").tolist(), v["open"].tolist()))
    closes = dict(zip(v["hour_utc"].astype("int64").tolist(), v["close"].tolist()))
    h, hp = c["hour"], c["prev_hour"]
    p = math.log(c["price"] / c["prev_close"])
    start = opens.get(h) if c.get("from_open") else closes.get(hp)
    end = opens.get(h) if c["check"] == OPEN and not c.get("to_close") else closes.get(h)
    if start is None or end is None:
        return UNKNOWN, p, 0.0
    move = math.log(end / start)
    if move * p > 0 and abs(move) >= REAL_SHARE * abs(p):
        return CONFIRMED, p, move
    return UNCONFIRMED, p, move


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
    keys = [_session_key(int(h), template) for h in j["hour_utc"]]
    g = pd.DataFrame({"hour": j["hour_utc"].astype("int64"),
                      "off": np.log(j["close_v"].to_numpy(float) / j["close_s"].to_numpy(float)),
                      "key": keys}).dropna(subset=["key"])
    by = g.groupby("key").agg(off=("off", "median"), first=("hour", "min"),
                              n=("hour", "size")).sort_index()
    by = by[by["n"] >= 3]
    stepped = (by["off"].diff().abs() * 1e4) >= ROLL_STEP_BP
    return [int(h) for h in by.loc[stepped, "first"]]


def _session_key(hour: int, template: str) -> "str | None":
    """The session an hour belongs to, for a source's change of contract: a
    daily market's own; a fund's day in New York; for the round-the-clock
    pairs and metals, the futures' day, from 18:00 New York (Yahoo's and
    MarketWatch's COMEX gold, which change contract at that open)."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    if sessions.is_calendar_template(template):
        return sessions.session_key(hour, template)
    local = datetime.fromtimestamp(hour, ZoneInfo("America/New_York"))
    if template != "us_equity":
        local += timedelta(hours=6)
    return local.date().isoformat()


def crosses(c: dict, at: "list[int]") -> bool:
    """Whether one of these hours falls inside a move: after the store's
    previous bar, up to its own."""
    return any(c["prev_hour"] < h <= c["hour"] for h in at)


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


def judge_all(c: dict, asked: "list[tuple[str, pd.DataFrame | None]]"
              ) -> "tuple[str, float, list, list, list]":
    """One candidate put to every source asked, each (name, its bars, or None
    for a source that was down), and the vote."""
    p = math.log(c["price"] / c["prev_close"])
    votes = [(name, UNKNOWN, p, 0.0) if v is None else (name,) + judge(c, v)
             for name, v in asked]
    result, stored, names, moves, seen = combine(votes)
    if result == NOT_REAL and c.get("night_hour") is not None:
        night = overnight_move(c, asked)
        if night is not None:
            return OVERNIGHT, stored, night[0], night[1], night[0]
    return result, stored, names, moves, seen


def judge_found(asset: Asset, frame: pd.DataFrame, found: "list[dict]",
                sources: "list[Source]", asked: "list[tuple[str, pd.DataFrame | None]]",
                now_ts: int) -> "list[tuple[dict, tuple]]":
    """One instrument's candidates put to its sources, as the pass judges them: `asked`
    is each source's bars in `sources`' order, or None for one that was down. Returns
    each candidate with judge_all's answer; one that cannot be judged is left out,
    logged. tools/measure.py votes through it too."""
    rolls = {src.name for src in sources if src.own_rolls}
    changed = {name: switches(frame, v, asset.session_template)
               for name, v in asked if name in rolls and v is not None}
    out = []
    for c in found:
        try:
            # A source across its own change of contract has nothing to
            # say about the move: an outage for it. One that cannot serve
            # the move whole yet is no voter on it this time.
            # One whose bar there is another grid's is not asked about it.
            usable = [(src.name, None if v is None or crosses(c, changed.get(src.name, []))
                       else v)
                      for src, (_, v) in zip(sources, asked)
                      if src.serves(c, now_ts) and not supplied(src.name, asset, c)
                      and not src.skips(asset, c)]
            out.append((c, judge_all(c, usable)))
        except Exception as exc:
            # One instrument's surprise costs that reading, not the pass.
            log.warning("verify: %s %s could not be judged - %s", asset.ticker,
                        c["hour"], exc)
    return out


def overnight_move(c: dict, asked: "list[tuple[str, pd.DataFrame | None]]"
                   ) -> "tuple[list, list] | None":
    """A session's first hour most feeds did not see move - but did most see
    the move from the previous session's close to this bar's close, the
    store one of them and an outage left out? Then it happened overnight: the
    store's first print was a stale one at the old price (TLH 2020-03-09:
    gap +0.03%, first hour +3.8%; the tape opened +4.65%). Each feed on its own
    bars, as judge reads them: its close of the last bar before the night and
    its close of the first hour; one missing either is an outage. Returns the
    names of the feeds that saw it and each one's night - its open of the hour
    over its close of the last bar before the night - or None."""
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
        hours = v["hour_utc"].astype("int64").tolist()
        opens = dict(zip(hours, v["open"].tolist()))
        closes = dict(zip(hours, v["close"].tolist()))
        if c["hour"] not in opens or c["night_hour"] not in closes:
            continue
        names.append(name)
        nights.append(math.log(opens[c["hour"]] / closes[c["night_hour"]]))
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
    # UNANSWERED_IN_A_ROW requests in a row (~96 s each of a 20-minute job). A
    # keyed provider past the votes' share of its allowance (vote_room) is an
    # outage for the rest of the run. Past VOTE_SECONDS no instrument is
    # started: those left are due, and counted on a run after.
    from price_monitor import sifting, tiingo, twelvedata
    from price_monitor.models import KeyRefused

    stops = (yahoo.RateLimited, tiingo.RateLimited, sifting.RateLimited,
             alpaca.RateLimited, twelvedata.DailyQuotaExhausted, KeyRefused)
    counts = {REAL: 0, UNCERTAIN: 0, NOT_REAL: 0, OVERNIGHT: 0}
    unanswered: dict[str, int] = {}
    requests_left = MAX_REQUESTS
    room = vote_room(now_dt)
    capped: set[str] = set()
    began = time.monotonic()
    late = 0
    doubted: list[str] = []
    for _, asset, found, frame in sorted(todo, key=lambda d: d[0]):
        if time.monotonic() - began > VOTE_SECONDS:
            late += 1
            continue
        # From before the earliest bar a move starts at: a Monday gap starts at
        # Friday's close.
        days = (now_ts - min(c["prev_hour"] for c in found)) / 86400 + 1
        # Every source is asked, or none this run: a vote of all of them, not
        # of whichever were asked. One that is stopped, or fails, is an outage
        # (None), left out of the count. A source whose delay keeps it from
        # serving any of the moves whole, or that is not asked about any of
        # their hours, is not asked, and joins the next count.
        sources = [src for src in sources_for(asset)
                   if any(src.serves(c, now_ts) and not src.skips(asset, c) for c in found)]
        if requests_left < sum(src.name not in blocked for src in sources):
            continue
        asked: "list[tuple[str, pd.DataFrame | None]]" = []
        for src in sources:
            name = src.name
            if name in blocked:
                asked.append((name, None))
                continue
            if room.get(provider(name), math.inf) <= 0:
                capped.add(provider(name))
                asked.append((name, None))
                continue
            requests_left -= 1
            if provider(name) in room:
                room[provider(name)] -= 1
            try:
                asked.append((name, _priced(fetch_verifier(name, src.symbol(asset), src.interval,
                                                           days, session, now_dt))))
                unanswered.pop(name, None)
            except Exception as exc:
                log.warning("verify: %s from %s failed - %s", asset.ticker, name, exc)
                asked.append((name, None))
                if isinstance(exc, stops) or "answered 429" in str(exc):
                    blocked.add(name)
                elif isinstance(exc, Unreachable):
                    unanswered[name] = unanswered.get(name, 0) + 1
                    if unanswered[name] >= UNANSWERED_IN_A_ROW:
                        log.warning("verify: %s did not answer %d requests in a row; "
                                    "not asked again this run", name, unanswered[name])
                        blocked.add(name)
                else:
                    unanswered.pop(name, None)              # it answered
        for c, (verdict, stored, names, moves, seen) in judge_found(
                asset, frame, found, sources, asked, now_ts):
            counts[verdict] += 1
            record[(asset.asset_id, c["hour"], c["check"])] = _row(
                asset, c, verdict, stored, names, moves, seen, now_ts)
            if verdict == NOT_REAL:
                theirs = ", ".join(f"{n} {'outage' if m is None else f'{100 * m:+.2f}%'}"
                                   for n, m in zip(names, moves))
                doubted.append(f"{asset.ticker} {datetime.fromtimestamp(c['hour'], timezone.utc):%Y-%m-%d %H:%M}"
                               f" {c['check']} ({asset.fetched_from} {100 * stored:+.2f}%; {theirs})")
    if capped:
        log.warning("verify: the votes' share spent for %s (config/provider_limits.csv): "
                    "an outage in the rest of this run's votes", ", ".join(sorted(capped)))
    if late:
        log.warning("verify: %d instruments left for a later run: the vote's %d seconds "
                    "were spent", late, VOTE_SECONDS)
    write(record, now_ts, path)
    if doubted:
        log.warning("verify: not real, most sources did not see it - %s", "; ".join(doubted))
    log.info("verify: %s", ", ".join(f"{k} {n}" for k, n in counts.items()))
    # The sources stopped during this pass, by a rate limit or silence; the
    # providers past the votes' share of their allowance.
    return dict(counts, not_real_moves=doubted, stopped=sorted(blocked - told),
                capped=sorted(capped))


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

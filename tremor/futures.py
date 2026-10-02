"""Futures read as continuous series: when their contract rolls, and which bars
are too thin to be prices.

THE ROLL. Yahoo's continuous series (KC=F, CC=F, ...) is the front contract
until its last trading day and the next one from the session after - measured
on the contracts still listed (2026): coffee moved to December on 2026-09-21,
cocoa on 09-16, cattle to October on 09-01, cotton to October on 07-10, each
the session after that contract's last trading day by the exchange's rule. The
price jumps there by the spread between two contracts, which is not the
market, and it falls in that night's gap, so that gap is left unscored
(tremor.returns.overnight_gaps). The rule counts weekdays back from the end of
the month and does not know exchange holidays, which can move the last trading
day one session earlier - so the two sessions from the computed one on are
both left out: one night in fifty or so, five times a year.

Aluminium (ALI=F) is not here: Yahoo's series follows a monthly contract it
does not roll by any rule the listed contracts show, and the monthly spread is
a few tenths of a percent - below a night's own spread.

THIN BARS. A bar with no or almost no volume is a quote, not a trade: on these
series about one hour in ten has zero volume, and of the moves past six
standard deviations, a third sat on volume under 5% of the series' usual
(coffee's ±12% at 04:00 on 2026-03-30, 04-01 and 04-02, on 15, 34 and 76
contracts, each undone the next hour). tremor.quality marks such bars invalid,
which makes them holes, skipped rather than scored.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import numpy as np
import pandas as pd

MONTH_CODES = "FGHJKMNQUVXZ"

# Per series:
#   listed    the delivery months, and Yahoo's continuous series rolls through
#             all of them on each one's last trading day;
#   ltd_back  that last trading day, as business days before the delivery
#             month's last business day (0 is the last business day itself);
#   held      the months this series holds - the liquid ones (cotton's October
#             trades two to four hours a day in its last weeks, December
#             seventeen);
#   roll_back the business days before the delivery month's first business day
#             on which this series moves to the next held month - five before
#             first notice, when the two contracts trade side by side and
#             Yahoo's series flips between them. None: at the last trading day,
#             as Yahoo does (cattle trades through its month).
#   suffix    Yahoo's exchange suffix for a single contract.
#   continuous_history
#             False: Yahoo's continuous series is not used for history at all.
#             Cotton's holds a third of a normal month's hours in 14 of its 29
#             months, mixes contracts before each first notice, and its thin
#             Sunday opens leave a weekend yardstick a tenth of the real one -
#             so its history is the held contracts' own bars, from the
#             December 2026 contract's front day (2026-06-17).
SPECS: "dict[str, dict]" = {
    "KC=F": dict(listed="HKNUZ", ltd_back=8, held="HKNUZ", roll_back=12, suffix="NYB"),
    "CC=F": dict(listed="HKNUZ", ltd_back=11, held="HKNUZ", roll_back=15, suffix="NYB"),
    "CT=F": dict(listed="HKNVZ", ltd_back=16, held="HKNZ", roll_back=10, suffix="NYB",
                 continuous_history=False),
    "LE=F": dict(listed="GJMQVZ", ltd_back=0, held="GJMQVZ", roll_back=None, suffix="CME"),
}
CONTRACTS = SPECS

# Volume under this share of the series' trailing median is a quote, not a
# trade. The median is of the nonzero volumes among the THIN_WINDOW bars before
# this one - about two months of a ten-hour session.
THIN_SHARE = 0.05
THIN_WINDOW = 500
THIN_MIN_BARS = 50


def is_continuous(ticker: str) -> bool:
    """A continuous futures series, as Yahoo names them."""
    return ticker.endswith("=F")


def _business_days_before(day: date, n: int) -> date:
    while n:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def _month_end(year: int, month: int) -> date:
    end = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    while end.weekday() >= 5:
        end -= timedelta(days=1)
    return end


def _month_start(year: int, month: int) -> date:
    start = date(year, month, 1)
    while start.weekday() >= 5:
        start += timedelta(days=1)
    return start


def _contracts(months: str, first_year: int, last_year: int) -> "list[tuple[int, int, str]]":
    return [(y, MONTH_CODES.index(c) + 1, c) for y in range(first_year, last_year + 1)
            for c in months]


def last_trading_days(ticker: str, first_year: int, last_year: int) -> "list[date]":
    """Each listed contract's last trading day, by the exchange's rule on weekdays."""
    if ticker not in SPECS:
        return []
    spec = SPECS[ticker]
    return [_business_days_before(_month_end(y, m), spec["ltd_back"])
            for y, m, _ in _contracts(spec["listed"], first_year, last_year)]


def roll_days(ticker: str, first_year: int, last_year: int) -> "list[tuple[str, date]]":
    """(contract symbol, the first day it is front) for each held contract, in
    order: this series' own rolls."""
    spec = SPECS[ticker]
    out = []
    for y, m, code in _contracts(spec["held"], first_year, last_year + 1):
        if spec["roll_back"] is None:
            last = _business_days_before(_month_end(y, m), spec["ltd_back"])
        else:
            last = _business_days_before(_month_start(y, m), spec["roll_back"] + 1)
        out.append((f"{ticker[:-2]}{code}{y % 100:02d}.{spec['suffix']}", last))
    # A contract is front from the day after the previous one's last held day.
    fronts = []
    for k in range(1, len(out)):
        start = out[k - 1][1] + timedelta(days=1)
        while start.weekday() >= 5:
            start += timedelta(days=1)
        fronts.append((out[k][0], start))
    return fronts


# Contract switches of a history source whose roll does not follow this
# series' calendar - Dukascopy's CFDs, located in their own data
# (tools/dukascopy_futures.py). Their nights are left unscored like any roll's.
ROLLS_PATH = os.path.join("data", "tremor", "rolls.csv")


def history_rolls(ticker: str, path: "str | None" = None) -> "list[date]":
    path = path or ROLLS_PATH
    if not os.path.exists(path):
        return []
    table = pd.read_csv(path, dtype=str)
    return [date.fromisoformat(d) for d in table.loc[table["ticker"] == ticker, "date"]]


def roll_sessions(ticker: str, session_days) -> "set[str]":
    """The session dates ("YYYY-MM-DD") whose opening gap may hold a roll -
    Yahoo's (its history), this series' own (its live bars) or a history
    source's (ROLLS_PATH): the first two sessions on or after each such day.
    Exchange holidays can move a last trading day one session earlier than the
    weekday count makes it."""
    import bisect

    days = sorted({str(d) for d in session_days})
    if ticker not in SPECS or not days:
        return set()
    first, last = int(days[0][:4]), int(days[-1][:4])
    edges = [ltd + timedelta(days=1)
             for ltd in last_trading_days(ticker, first - 1, last)]
    edges += [start for _, start in roll_days(ticker, first - 1, last)]
    edges += history_rolls(ticker)
    out: "set[str]" = set()
    for edge in edges:
        at = bisect.bisect_left(days, (edge - timedelta(days=1)).isoformat())
        # Only sessions just after the roll: a roll before the stored history
        # began, or across a gap in it, marks nothing.
        out.update(d for d in days[at:at + 2]
                   if date.fromisoformat(d) <= edge + timedelta(days=7))
    return out


def front_contract(ticker: str, today: date) -> "tuple[str, date] | None":
    """The contract this series is on for trading day `today`, and the first
    day it was front. LIVE BARS COME FROM THIS CONTRACT, not from Yahoo's
    continuous series, which mixes in bars of other contracts - 64 hours on
    coffee's since 2024-05 jump past 3% and straight back, against 2 on a single
    contract over the same months. None for a series not rolled here."""
    if ticker not in SPECS:
        return None
    current = None
    for symbol, start in roll_days(ticker, today.year - 2, today.year + 1):
        if start > today:
            break
        current = (symbol, start)
    return current


def thin(volume: pd.Series) -> pd.Series:
    """Per bar, in time order: is its volume under THIN_SHARE of the median of
    the nonzero volumes among the THIN_WINDOW bars before it? A zero is always
    thin. The window ends before the bar, like every window here."""
    v = volume.astype("float64")
    median = (v.where(v > 0).rolling(THIN_WINDOW, min_periods=THIN_MIN_BARS)
              .median().shift(1).ffill())
    return (v <= 0) | (v < THIN_SHARE * median).fillna(False).to_numpy(dtype=bool)


def yahoo_lag_windows(ticker: str, first_year: int, last_year: int
                      ) -> "list[tuple[date, date]]":
    """[from, to) stretches where Yahoo's continuous series is still on a
    contract this series has already rolled out of: from this series' roll
    into a contract to the day after the previous listed contract's last
    trading day. Yahoo's hours there are the expiring contract in its delivery
    weeks, mixed with the next one's prints, and are not this series - the
    history import leaves them out (tools/futures_history.py)."""
    if ticker not in SPECS:
        return []
    spec = SPECS[ticker]
    listed = _contracts(spec["listed"], first_year - 1, last_year + 1)
    ltds = last_trading_days(ticker, first_year - 1, last_year + 1)
    entry = {}
    for k in range(1, len(listed)):
        y, m, code = listed[k]
        enter = ltds[k - 1] + timedelta(days=1)
        while enter.weekday() >= 5:
            enter += timedelta(days=1)
        entry[f"{ticker[:-2]}{code}{y % 100:02d}.{spec['suffix']}"] = enter
    out = []
    for symbol, start in roll_days(ticker, first_year, last_year):
        if symbol in entry and entry[symbol] > start:
            out.append((start, entry[symbol]))
    return out


# The once-over the continuous history gets on import (never in the hourly
# pass, which would need the bar after the one it judges): an hour that moves
# past FLIP and is undone by more than half in the next is another contract's
# print, and an open past the stray line from the last close that its own bar
# undoes by more than half is a stray opening print.
FLIP = 0.03
# A stray open is judged against the series' own hourly moves: STRAY_OPEN_MULT
# times its median absolute hourly return, at least STRAY_OPEN_FLOOR. A fixed
# 4% suited coffee and missed cattle's (2024-06-24 13:00 opened 2.5% up on the
# other contract's price and closed where it had been; cattle's median hour is
# a tenth of coffee's).
STRAY_OPEN_MULT = 8.0
STRAY_OPEN_FLOOR = 0.01
# And a month holding under this share of a normal month (the series' 75th
# percentile - the median itself sinks when a third of the months are sparse,
# as cotton's are) is dropped:
# Yahoo's cotton holds 26-84 bars a month from January to May 2026 against ~370
# in a whole one, a quarter of them repeating the last price, and a half-year
# yardstick built on that is a fifth of cotton's real one.
SPARSE_MONTH = 0.5


def clean_history(frame: pd.DataFrame) -> "tuple[pd.DataFrame, int, int, list]":
    """The continuous series with flipped bars dropped, stray opens set to the
    previous close and sparse months dropped. Returns the frame, the two
    counts and the months dropped."""
    out = frame.sort_values("hour_utc").reset_index(drop=True)
    dropped = 0
    while True:
        r = np.log(out["close"]).diff()
        nxt = r.shift(-1)
        flip = (r.abs() > FLIP) & (nxt.abs() > FLIP) & (r * nxt < 0) & \
            ((r + nxt).abs() < r.abs() / 2)
        if not flip.any():
            break
        dropped += int(flip.sum())
        out = out[~flip].reset_index(drop=True)
    out, stray = reset_stray_opens(out)
    month = pd.to_datetime(out["hour_utc"], unit="s", utc=True).dt.strftime("%Y-%m")
    counts = month.value_counts().sort_index()
    full = counts.iloc[1:-1] if len(counts) > 2 else counts     # not the two ends
    sparse = sorted(m for m, n in counts.items()
                    if n < SPARSE_MONTH * full.quantile(0.75) and m in full.index)
    out = out[~month.isin(sparse)].reset_index(drop=True)
    return out, dropped, stray, sparse


def reset_stray_opens(frame: pd.DataFrame) -> "tuple[pd.DataFrame, int]":
    """Opens past the series' stray line from the last close, which their own
    bar undoes by more than half, set to that close. Returns the count."""
    out = frame.sort_values("hour_utc").reset_index(drop=True)
    prev = out["close"].shift(1)
    line = max(STRAY_OPEN_FLOOR,
               STRAY_OPEN_MULT * float(np.log(out["close"]).diff().abs().median()))
    jump = np.log(out["open"] / prev)
    held = np.log(out["close"] / prev)
    stray = (jump.abs() > line) & (held.abs() < jump.abs() / 2)
    out.loc[stray, "open"] = prev[stray]
    out.loc[stray, "low"] = out.loc[stray, ["low", "open", "close"]].min(axis=1)
    out.loc[stray, "high"] = out.loc[stray, ["high", "open", "close"]].max(axis=1)
    return out, int(stray.sum())


# And whole stretches where the continuous series interleaves two contract
# months within its sessions - live cattle from 2026-02-19 to 04-02 opened each
# session on one month and jumped about 3.5% onto the other at 15:00 UTC on ten
# times the volume, back again the next morning. A session is MIXED when its
# first close sits more than MIXED_OPEN from the previous session's close and
# the session then moves back past three quarters of that. One such session is
# as likely a real reversal (cattle 2025-04-09, the tariff pause); MIXED_RUN of
# them within MIXED_SPAN sessions is not, and everything from the first to the
# last of them is dropped.
MIXED_OPEN = 0.015
MIXED_RUN = 3
MIXED_SPAN = 15


def mixed_sessions(frame: pd.DataFrame, template: str) -> "list[str]":
    from tremor import sessions
    key = frame["hour_utc"].map(lambda h: sessions.session_key(int(h), template))
    f = frame.assign(key=key).dropna(subset=["key"]).sort_values("hour_utc")
    prev_close = f.groupby("key")["close"].last().shift(1)
    out = []
    for k, g in f.groupby("key"):
        pc = prev_close.get(k)
        if pc is None or not np.isfinite(pc):
            continue
        level = np.log(g["close"].to_numpy() / pc)
        first = level[0]
        back = (level - first) * -np.sign(first)
        if abs(first) > MIXED_OPEN and back.max() > 0.75 * abs(first):
            out.append(k)
    return out


def drop_mixed(frame: pd.DataFrame, template: str) -> "tuple[pd.DataFrame, list]":
    """The frame without its mixed stretches, and the stretches dropped."""
    from tremor import sessions
    key = frame["hour_utc"].map(lambda h: sessions.session_key(int(h), template))
    order = sorted(k for k in key.dropna().unique())
    pos = {k: i for i, k in enumerate(order)}
    flagged = [pos[k] for k in mixed_sessions(frame, template)]
    stretches, i = [], 0
    while i < len(flagged):
        j = i
        while j + 1 < len(flagged) and flagged[j + 1] - flagged[i] < MIXED_SPAN:
            j += 1
        # extend while the run keeps finding flags within the span of its last
        while j + 1 < len(flagged) and flagged[j + 1] - flagged[j] < MIXED_SPAN:
            j += 1
        if j - i + 1 >= MIXED_RUN:
            stretches.append((order[flagged[i]], order[flagged[j]]))
        i = j + 1
    drop = pd.Series(False, index=frame.index)
    for lo, hi in stretches:
        drop |= key.between(lo, hi).fillna(False)
    return frame[~drop.to_numpy()].reset_index(drop=True), stretches

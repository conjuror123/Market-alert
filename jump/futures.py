"""Futures read as continuous series: when their contract rolls, and which bars
are too thin to be prices.

THE ROLL. Yahoo's continuous series (KC=F, CC=F, ...) is the front contract
until its last trading day and the next one from the session after - measured
on the contracts still listed (2026): coffee moved to December on 2026-09-21,
cocoa on 09-16, cattle to October on 09-01, cotton to October on 07-10, each
the session after that contract's last trading day by the exchange's rule. The
price jumps there by the spread between two contracts, which is not the
market, and it falls in that night's gap, so that gap is left unscored
(jump.returns.overnight_gaps). The rule counts weekdays back from the end of
the month and does not know exchange holidays, which can move the last trading
day one session earlier - so the two sessions from the computed one on are
both left out: one night in fifty or so, five times a year.

THE LIVE BARS are not that series but one contract at a time (front_contract),
rolled on this series' own calendar (SPECS, roll_back) - before first notice,
when the traders move. Its rolls, Yahoo's in the history and a history source's
(ROLLS_PATH) are all left unscored the same way (roll_sessions).

Aluminium (ALI=F) is not here: Yahoo's series follows a monthly contract it
does not roll by any rule the listed contracts show, and the monthly spread is
a few tenths of a percent - below a night's own spread.

THIN BARS. A bar with no or almost no volume is a quote, not a trade: on these
series about one hour in ten has zero volume, and of the moves past six
standard deviations, a third sat on volume under 5% of the series' usual
(coffee's ±12% at 04:00 on 2026-03-30, 04-01 and 04-02, on 15, 34 and 76
contracts, each undone the next hour). jump.quality marks such bars invalid,
which makes them holes, skipped rather than scored.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

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
#             Yahoo's series flips between them. None: at the last trading day.
#             Cattle has no first notice but its traders leave the same way:
#             December's volume passed October's on 2026-09-11..16 (October
#             3,097 contracts on 10-07 against December's 18,822), and held to
#             its last day the series sat on a contract hardly trading
#             (October 2025 moved +2.2% on 213 contracts, every other flat).
#   suffix    Yahoo's exchange suffix for a single contract.
SPECS: "dict[str, dict]" = {
    "KC=F": dict(listed="HKNUZ", ltd_back=8, held="HKNUZ", roll_back=12, suffix="NYB"),
    "CC=F": dict(listed="HKNUZ", ltd_back=11, held="HKNUZ", roll_back=15, suffix="NYB"),
    "CT=F": dict(listed="HKNVZ", ltd_back=16, held="HKNZ", roll_back=10, suffix="NYB"),
    "LE=F": dict(listed="GJMQVZ", ltd_back=0, held="GJMQVZ", roll_back=12, suffix="CME"),
}

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
# series' calendar - Dukascopy's CFDs, located once in their own data when the
# softs' history was built (docs/decisions.md). Their nights are left unscored
# like any roll's.
ROLLS_PATH = os.path.join("data", "jump", "rolls.csv")


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

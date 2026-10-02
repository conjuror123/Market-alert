"""Returns: the hour's move and the gap before a session.

The central idea is to separate two movements that an ordinary return merges
into one. Between the previous session's close and the next session's open the
price changes without trading: news comes out, a dividend goes ex, trading
happens on another venue. Add that jump to the intra-hour move and every morning
would look like an anomaly.

So the first bar of a session is measured from its OWN open rather than from the
previous session's close:

    first bar of a session:  r_t = ln(close_of_first / open_of_first)
    every other bar:         r_t = ln(close_t / close_{t-1})

The overnight jump is not a return here: r stays the move inside the hour, and
the first bar is not disturbed. It is KEPT beside it, though, as its own column
`gap` - see overnight_gaps, and tremor.jumps for how it is scored.

This also disposes of the unadjusted-series problem. ETFs arrive from the source
without a dividend adjustment, and on the ex-date the price mechanically drops by
the payout. That drop happens between sessions, so it falls in the jump this
module discards and never reaches r_t.

WHAT WAS HERE, WENT, AND CAME BACK. The jump used to be kept as a second channel, r_gap,
alongside a gap_masked flag marking the ex-dates the corporate-actions table
knows about, so that the distribution of the gap channel would not be skewed by
regular dividend steps. Both were computed on every bar, written into every
metrics table, and read by nothing - the channel awarded no points and no message
ever quoted it. They are gone, and with them the corporate-actions lookup that
existed only to feed the flag. tremor.corporate_actions is still used for
un-adjustment, which is a different question and a live one.

It came back because dropping it was measured and found expensive: across the
US funds a median 43% of day-to-day price variance happens between the close
and the next open - 25% for XLU, 70% for CPER - and SPY's largest opening gaps
are 2020-03-16, 2008-10-24, 2015-08-24 and 2024-08-05. Those days the detector
saw only what happened after the first print. This time the gap is scored, not
merely stored: tremor.jumps judges it against the fund's own earlier gaps of
the same kind.
"""
from __future__ import annotations

import bisect
import logging
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from tremor.basket import Asset

HOUR = 3600
NYSE_TZ = ZoneInfo("America/New_York")

log = logging.getLogger("tremor.returns")

# Share-count changes a fund can make, as the price ratio they leave overnight.
# The store is split-adjusted - checked across the five SPDR splits of
# 2025-12-05, whose opening gaps were +0.47%, -0.20% and +0.01% - but a split the
# provider has not back-adjusted yet would arrive as a -69% "gap", so a gap this
# close to one of these is refused rather than scored as the crash of a century.
SPLIT_RATIOS = (2.0, 3.0, 4.0, 5.0, 10.0, 1.5)
SPLIT_TOLERANCE = 0.01

# How far the Friday close may sit behind a currency pair's Sunday open: the
# 16:00 New York bar to the 17:00 one is 49 hours, and the clocks may change in
# between. Anything longer means the store lost the end of the week.
FX_WEEKEND_MAX_SECONDS = 50 * HOUR

# The two days a year currency markets close midweek: Christmas Day and New
# Year's Day. Every pair's store is empty across them.
FX_HOLIDAYS = frozenset({(12, 25), (1, 1)})



def session_ids(asset: Asset, hours: pd.Series, anchor_tz: str = "America/New_York") -> pd.Series:
    """Session id for each hour. The first bar of a session is the one whose id
    differs from the previous bar's.

    For ETFs a session is a trading day in exchange time. For currency pairs a
    session is the entire trading week, from Sunday evening to Friday evening:
    trading inside it never breaks, and the only gap in the week is the weekend.
    Round-the-clock crypto has no sessions at all and no gaps either, so every
    hour belongs to one endless session.
    """
    if asset.session_template == "crypto_24_7":
        return pd.Series(0, index=hours.index)

    moments = pd.to_datetime(hours, unit="s", utc=True)
    if asset.session_template == "us_equity":
        local = moments.dt.tz_convert(NYSE_TZ)
        return local.dt.strftime("%Y-%m-%d")

    if asset.session_template == "fx_continuous":
        # Vectorised, and it has to be: this used to call reference_week_bounds
        # once per bar, which at 145,000 bars was the single largest cost in the
        # whole metrics stage - 292,165 calls, two timezone conversions each.
        from tremor.sessions import reference_week_opens

        return reference_week_opens(moments, anchor_tz).astype("int64") // 10 ** 9

    from tremor.sessions import is_calendar_template, session_key

    if is_calendar_template(asset.session_template):
        return pd.Series([str(session_key(int(h), asset.session_template))
                          for h in hours], index=hours.index)

    raise ValueError(f"{asset.ticker}: unknown session template '{asset.session_template}'")


def fx_holiday_closed(prev_hour: np.ndarray, hours: np.ndarray,
                      anchor_tz: str = "America/New_York") -> np.ndarray:
    """Per bar: do the missing hours before it touch Christmas Day or New Year's
    Day, New York time? Currency markets close for them - 24 to 34 hours with
    no bar - so the stretch is a closure, not a hole, and the price it reopens
    at is a gap judged with the pair's weekends (tremor.jumps.score_gaps)."""
    tz = ZoneInfo(anchor_tz)
    out = np.zeros(len(hours), dtype=bool)
    for i in np.flatnonzero(hours - prev_hour > HOUR):
        first = datetime.fromtimestamp(int(prev_hour[i]) + HOUR, tz).date()
        last = datetime.fromtimestamp(int(hours[i]) - HOUR, tz).date()
        day = first
        while day <= last and not out[i]:
            out[i] = (day.month, day.day) in FX_HOLIDAYS
            day += timedelta(days=1)
    return out


def split_channels(asset: Asset, usable: pd.DataFrame,
                   anchor_tz: str = "America/New_York",
                   dividends=None, session_table=None) -> pd.DataFrame:
    """Computes r, and marks which bars open a session.

    The input is ONLY usable bars (those that passed the quality gate and lie inside
    a session): a return computed across an invalid or after-hours bar is
    meaningless.

    A MISSING HOUR IS SKIPPED, AS IF IT WERE NEVER THERE. The bar after it is
    its own hour, open to close, and scored as usual; the move across the hole -
    from the last close before it to that bar's open - is `hole`, never scored
    (tremor.jumps keeps it only in the price path the close check reads).
    Measured from the last close instead, a thin fund's three quiet hours read
    as one violent one: 122 flags in the history were such moves. A closure
    the calendar knows is not a hole: it opens a session, and its gap is scored
    against the instrument's other gaps.

    Still named split_channels because the split is still what it does: the
    overnight jump is separated from the intra-hour move and then dropped, rather
    than never being separated at all.
    """
    out = usable.copy().sort_values("hour_utc").reset_index(drop=True)
    if out.empty:
        return out.assign(r=pd.Series(dtype="float64"),
                          is_session_open=pd.Series(dtype=bool),
                          hole=pd.Series(dtype="float64"),
                          gap=pd.Series(dtype="float64"))

    session = session_ids(asset, out["hour_utc"], anchor_tz)
    is_open = session != session.shift(1)
    is_open.iloc[0] = True  # first bar of history: there is no prior session
    hours = out["hour_utc"].to_numpy(dtype="int64")
    prev_hour = np.concatenate([[hours[0]], hours[:-1]])
    if asset.session_template == "fx_continuous":
        is_open |= fx_holiday_closed(prev_hour, hours, anchor_tz)
    after_hole = ~is_open.to_numpy() & (hours - prev_hour > HOUR)

    prev_close = out["close"].shift(1)
    own_hour = np.log(out["close"] / out["open"])
    out["r"] = np.where(is_open | after_hole, own_hour, np.log(out["close"] / prev_close))
    # The very first bar of history has no previous close, and its own open is
    # the start of the record rather than a continuation of anything: undefined,
    # not zero.
    out.loc[0, "r"] = np.nan
    out["is_session_open"] = is_open
    out["hole"] = np.where(after_hole, np.log(out["open"] / prev_close), np.nan)
    out["gap"] = (overnight_gaps(asset, out, session, dividends, session_table)
                  if dividends is not None else np.nan)
    return out


def overnight_gaps(asset: Asset, frame: pd.DataFrame, session: pd.Series,
                   dividends, session_table=None) -> np.ndarray:
    """ln(open / previous close) on the first bar of each session, else NaN.

    Every calendar but crypto's has a gap. A daily-session market's (nickel,
    the soft commodities, cattle, aluminium, the real) is its night and its
    weekend. A US fund's is every night, 16:00 to 09:30. A
    currency pair trades Sunday 17:00 to Friday 17:00 New York time, so its one
    gap is the weekend - small most weeks (2% of its variance) and not small on
    the weekends that matter: 2025-02-02, the Canada tariffs, USD/CAD +1.44%;
    2020-03-15, the Sunday Fed cut; 2017-04-23, the French first round. Crypto
    never closes and has none.

    For a fund the dividend comes out, because an ex-date drop is the payout
    leaving the price rather than anything happening to the market. The table's
    step is d/(1-d) with d the payout over the previous close, so ln(1 + step)
    is exactly -ln(1 - d), the log of the drop - see
    corporate_actions.derive_actions_tiingo for why that form and not d.

    NaN, not scored, wherever the answer could be something other than the
    market:
      - when the previous stored bar is not the LAST bar before the close. A
        store missing a whole day turns "overnight" into "since three days
        ago": 2015-01-02 is absent from the FX store, and the Sunday open
        after it read as a 1.2-1.8% weekend gap on three pairs at once. For a
        fund that is checked against the NYSE calendar (`session_table`, and
        without one no fund gap is scored at all); for a pair, the previous bar
        must be the Friday afternoon one, at most FX_WEEKEND_MAX_SECONDS before;
      - for a fund, on a date past its checked-through date, because a payout
        the table has not heard of yet reads as a gap the size of the dividend;
      - for a fund, on a declared split date, and on any gap within
        SPLIT_TOLERANCE of a split ratio, because the store is split-adjusted
        and a gap that looks like a split is a provider that has not adjusted;
      - on the first bar of the record, which has no previous close.
    """
    n = len(frame)
    template = asset.session_template
    from tremor import futures
    from tremor.sessions import (DAILY_CLOSED_MAX_SECONDS, is_calendar_template,
                                 session_key_close)

    if not (template in ("us_equity", "fx_continuous") or is_calendar_template(template)) \
            or n == 0:
        return np.full(n, np.nan)

    is_open = frame["is_session_open"].to_numpy(dtype=bool).copy()
    is_open[0] = False
    hours = frame["hour_utc"].to_numpy(dtype="int64")
    prev_hour = np.concatenate([[0], hours[:-1]])
    prev_close = frame["close"].shift(1).to_numpy(dtype="float64")
    gap = np.log(frame["open"].to_numpy(dtype="float64") / prev_close)

    if template == "fx_continuous":
        complete = (hours - prev_hour) <= FX_WEEKEND_MAX_SECONDS
        usable = is_open & complete & np.isfinite(gap)
        return np.where(usable, gap, np.nan)

    if is_calendar_template(template):
        # The previous bar must come within an hour of its session's close, or
        # the store lost the evening; the close must be no longer than the longest holiday,
        # or the source was out (Kitco's eight days from 2023-12-18). And a
        # future's contract roll is not the market: the continuous series
        # jumps to the next contract across that night (tremor.futures).
        day = session.to_numpy(dtype=object)
        closes = {d: session_key_close(d, template) for d in set(day)}
        prev_day = np.concatenate([[day[0]], day[:-1]])
        # Within an hour of it: Yahoo's last half-hour of the soft
        # commodities is mostly a zero-volume marker (81% of coffee's 13:00
        # bars), dropped as too thin, which leaves the 12:00 bar the last.
        reached = np.array([prev_hour[k] + 2 * HOUR >= closes[prev_day[k]]
                            for k in range(n)], dtype=bool)
        complete = reached & ((hours - prev_hour) <= DAILY_CLOSED_MAX_SECONDS)
        rolled = np.isin(day, sorted(futures.roll_sessions(asset.ticker, day)))
        usable = is_open & complete & ~rolled & np.isfinite(gap)
        return np.where(usable, gap, np.nan)

    day = session.to_numpy(dtype=object)
    complete = _closed_on_the_last_bar(day, prev_hour, is_open, session_table)

    steps = dividends.steps.get(asset.ticker, {})
    step = np.array([steps.get(d, 0.0) for d in day], dtype="float64") \
        if steps else np.zeros(n)
    gap = gap + np.log1p(step)

    checked = dividends.checked_through.get(asset.ticker)
    known = (day <= checked) if checked else np.zeros(n, dtype=bool)

    splits = dividends.splits.get(asset.ticker, frozenset())
    declared = np.array([d in splits for d in day], dtype=bool) if splits \
        else np.zeros(n, dtype=bool)
    ratios = np.log(np.array(SPLIT_RATIOS))
    looks_split = np.zeros(n, dtype=bool)
    for ratio in np.concatenate([ratios, -ratios]):
        looks_split |= np.abs(gap - ratio) < SPLIT_TOLERANCE
    looks_split &= is_open
    for position in np.flatnonzero(looks_split & ~declared & known):
        log.warning("%s: %s opened %+.1f%% from the previous close - a split "
                    "ratio, not scored", asset.ticker, day[position],
                    100 * (math.exp(gap[position]) - 1))

    usable = (is_open & complete & known & ~declared & ~looks_split
              & np.isfinite(gap))
    return np.where(usable, gap, np.nan)


def _closed_on_the_last_bar(day: np.ndarray, prev_hour: np.ndarray,
                            is_open: np.ndarray, session_table) -> np.ndarray:
    """Per bar: is the previous stored bar the closing bar of the previous session?

    The previous session is the calendar's, not the store's - otherwise a
    missing day is invisible, which is the whole point. And the previous bar must
    reach the close (a 30-minute fund folded to hours ends on the 15:00 bar for
    a 16:00 close), so a store that lost the afternoon is refused too.
    """
    out = np.zeros(len(day), dtype=bool)
    if not session_table:
        return out
    from datetime import date as _date

    from tremor import quality

    calendar = sorted(session_table)
    for position in np.flatnonzero(is_open):
        today = _date.fromisoformat(day[position])
        at = bisect.bisect_left(calendar, today)
        if at == 0:
            continue
        previous = calendar[at - 1]
        _, closed = quality._session_bounds_utc(previous, session_table[previous])
        prev_day = datetime.fromtimestamp(int(prev_hour[position]), NYSE_TZ).date()
        out[position] = (prev_day == previous
                         and int(prev_hour[position]) + HOUR >= closed)
    return out

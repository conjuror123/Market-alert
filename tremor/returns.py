"""Returns and winsorization.

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
`gap` - see overnight_gaps, and tremor.gaps for how it is scored.

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
merely stored: tremor.gaps judges it against the fund's own history of gaps,
and it can claim the first bar's event.
"""
from __future__ import annotations

import bisect
import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from tremor import ewma, windows
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

    raise ValueError(f"{asset.ticker}: unknown session template '{asset.session_template}'")


def split_channels(asset: Asset, usable: pd.DataFrame,
                   anchor_tz: str = "America/New_York",
                   dividends=None, session_table=None) -> pd.DataFrame:
    """Computes r, and marks which bars open a session.

    The input is ONLY usable bars (those that passed the quality gate and lie inside
    a session): a return computed across an invalid or after-hours bar is
    meaningless.

    A missing bar inside a session is not forward-filled - that is forbidden,
    forward-fill for returns. The return is simply taken from the last valid
    close, so it spans two hours instead of one; that is more honest than
    inventing a close that never existed.

    Still named split_channels because the split is still what it does: the
    overnight jump is separated from the intra-hour move and then dropped, rather
    than never being separated at all.
    """
    out = usable.copy().sort_values("hour_utc").reset_index(drop=True)
    if out.empty:
        return out.assign(r=pd.Series(dtype="float64"),
                          is_session_open=pd.Series(dtype=bool),
                          gap=pd.Series(dtype="float64"))

    session = session_ids(asset, out["hour_utc"], anchor_tz)
    is_open = session != session.shift(1)
    is_open.iloc[0] = True  # first bar of history: there is no prior session

    prev_close = out["close"].shift(1)
    out["r"] = np.where(is_open,
                        np.log(out["close"] / out["open"]),
                        np.log(out["close"] / prev_close))
    # The very first bar of history has no previous close, and its own open is
    # the start of the record rather than a continuation of anything: undefined,
    # not zero.
    out.loc[0, "r"] = np.nan
    out["is_session_open"] = is_open
    out["gap"] = (overnight_gaps(asset, out, session, dividends, session_table)
                  if dividends is not None else np.nan)
    return out


def overnight_gaps(asset: Asset, frame: pd.DataFrame, session: pd.Series,
                   dividends, session_table=None) -> np.ndarray:
    """ln(open / previous close) on the first bar of each session, else NaN.

    Two calendars have a gap. A US fund's is every night, 16:00 to 09:30. A
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
    if template not in ("us_equity", "fx_continuous") or n == 0:
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


# Rows per pass of the vectorised MAD. The sliding view itself is free - it is a
# stride trick over the original buffer - but the |x - median| step has to
# materialise, so a whole series at once would allocate n*window doubles. At
# this size that is tens of megabytes rather than gigabytes, and chunking keeps
# it flat regardless of how long the history grows.
_MAD_CHUNK = 100_000


def _rolling_mad(series: pd.Series, window: int) -> pd.Series:
    """Median absolute deviation on a rolling window, EXCLUDING the current bar -
    the window ends on the previous one.

    Vectorised over the whole series rather than called back per window, which
    is not a micro-optimisation: this was 54% of the entire pipeline. The window
    is twenty-four bars, and a twenty-four-element double median takes about
    35us of which almost all is call overhead rather than arithmetic - so 1.7
    million of them, one per bar per instrument, cost two minutes of the five a
    full run took, to do a few seconds of actual work.

    NaN handling is inherited rather than coded: np.median returns NaN if any
    element is NaN, exactly as the per-window callback did, so a window
    straddling a gap still yields NaN and the first `window` positions stay NaN
    for want of a full window.
    """
    values = series.shift(1).to_numpy(dtype="float64")
    out = np.full(len(values), np.nan)
    if len(values) < window or window < 1:
        return pd.Series(out, index=series.index)

    view = np.lib.stride_tricks.sliding_window_view(values, window)
    for start in range(0, len(view), _MAD_CHUNK):
        block = view[start:start + _MAD_CHUNK]
        median = np.median(block, axis=1, keepdims=True)
        out[start + window - 1:start + window - 1 + len(block)] = np.median(
            np.abs(block - median), axis=1)
    return pd.Series(out, index=series.index)


def winsorize(asset: Asset, frame: pd.DataFrame) -> pd.DataFrame:
    """Winsorization of returns.

    The point is WHAT exactly gets capped. The EWMA state update is fed the
    clipped r_w: one extreme hour must not inflate the estimate of normal for
    many bars ahead, or the system goes blind for a while after every shock. But
    Z, every trigger, SAED and the export are all fed the RAW r - clipping the
    very thing we are trying to detect would be pointless.

    The eps_MAD floor keeps the limit from collapsing. In quiet hours, when the
    quote stands still, MAD_24 goes to zero, and without a floor any move at all
    would come out "larger than five MADs". The floor takes the greater of two:
    a fifth of the long-term sigma, and the return on half a tick at the current
    price - that is, the step below which the instrument physically cannot move.
    """
    out = frame.copy()
    if out.empty or "r" not in out:
        return out.assign(mad_eff=pd.Series(dtype="float64"),
                          r_w=pd.Series(dtype="float64"))

    returns = out["r"]
    mad_24 = _rolling_mad(returns, windows.MAD_WINDOW)

    # sigma_LT is computed on data strictly before the current bar - the same
    # out-of-sample discipline as everything else here. The shift lives
    # inside ewma.long_run_sigma, where it cannot be left out by a caller.
    sigma_lt = ewma.sigma_lt(returns, asset.session_template)

    half_tick_return = np.log1p(asset.tick_size / 2 / out["close"])
    # fmax, not maximum: while there are fewer than 720 bars of history sigma_LT
    # is undefined, and a plain maximum would return NaN - the floor would vanish
    # entirely, and winsorization with it, precisely during the EWMA burn-in
    # where one outlier spoils the estimate of normal for a long time. fmax
    # ignores NaN and keeps the other half of the floor - the half-tick return.
    eps_mad = np.fmax(0.2 * sigma_lt, half_tick_return)
    # Here it must be maximum: until 24 bars have accumulated MAD_24 is unknown
    # and there is no limit at all. Substituting the half-tick floor alone would
    # clip almost every bar: on a $770 ETF that is 0.003%.
    mad_eff = np.maximum(mad_24, eps_mad)

    limit = 5 * mad_eff
    out["mad_eff"] = mad_eff
    out["sigma_lt"] = sigma_lt
    out["r_w"] = np.where(returns.abs() > limit,
                          np.sign(returns) * limit,
                          returns)
    # Until the windows have filled there is no limit and nothing to clip: r_w equals r.
    out.loc[mad_eff.isna(), "r_w"] = returns[mad_eff.isna()]
    return out

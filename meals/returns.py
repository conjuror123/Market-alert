"""Returns, the gap channel and winsorization (spec §2.4, §2.5).

The central idea of §2.4 is to separate two movements that an ordinary return
merges into one. Between the previous session's close and the next session's
open the price changes without trading: news comes out, a dividend goes
ex, trading happens on another venue. Add that jump to the intra-hour move and
every morning would look like an anomaly.

So the first bar of a session is split into two channels:
    gap channel:  r_gap = ln(open_of_first / close_of_last of prior session)
    intra-hour:   r_t   = ln(close_of_first / open_of_first)
and ONLY r_t is fed into the Z-score, CSV, PCA and SAED. The gap channel is kept
separately, logged, and awards no SI-Index points.

This also solves the unadjusted-series problem. ETFs arrive from the source
without a dividend adjustment, and on the ex-date the price mechanically drops by
the payout. But the drop happens between sessions, that is, it lands in the gap
channel and not in r_t. On top of that such dates are flagged from the
corporate-actions table and their gap is excluded - otherwise the distribution of
the gap channel itself would be skewed by regular dividend steps.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from meals import windows
from meals.basket import Asset

HOUR = 3600
NYSE_TZ = ZoneInfo("America/New_York")


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
        from meals.sessions import reference_week_bounds
        return pd.Series(
            [reference_week_bounds(m.to_pydatetime(), anchor_tz)[0] for m in moments],
            index=hours.index)

    raise ValueError(f"{asset.ticker}: unknown session template '{asset.session_template}'")


def split_channels(asset: Asset, usable: pd.DataFrame,
                   action_days: set[date] | None = None,
                   anchor_tz: str = "America/New_York") -> pd.DataFrame:
    """Computes r and r_gap under the rules of §2.4.

    The input is ONLY usable bars (those that passed the §2.6 gate and lie inside
    a session): a return computed across an invalid or after-hours bar is
    meaningless.

    A missing bar inside a session is not forward-filled - §2.4 forbids
    forward-fill for returns. The return is simply taken from the last valid
    close, so it spans two hours instead of one; that is more honest than
    inventing a close that never existed.
    """
    out = usable.copy().sort_values("hour_utc").reset_index(drop=True)
    if out.empty:
        return out.assign(r=pd.Series(dtype="float64"), r_gap=pd.Series(dtype="float64"),
                          is_session_open=pd.Series(dtype=bool),
                          gap_masked=pd.Series(dtype=bool))

    session = session_ids(asset, out["hour_utc"], anchor_tz)
    is_open = session != session.shift(1)
    is_open.iloc[0] = True  # first bar of history: there is no prior session

    prev_close = out["close"].shift(1)
    out["r"] = np.where(is_open,
                        np.log(out["close"] / out["open"]),
                        np.log(out["close"] / prev_close))
    out["r_gap"] = np.where(is_open, np.log(out["open"] / prev_close), np.nan)
    # The very first bar of history has no previous close for either channel -
    # both quantities are undefined, not zero.
    out.loc[0, "r_gap"] = np.nan
    out.loc[0, "r"] = np.nan
    out["is_session_open"] = is_open

    # The gap on a corporate-action day reflects the payout, not a market move.
    # Only the gap is masked: the first bar's intra-hour return has nothing to do
    # with the ex-date, and discarding it along with the gap would throw away
    # sound data.
    if action_days:
        local_day = pd.to_datetime(out["hour_utc"], unit="s", utc=True)
        local_day = local_day.dt.tz_convert(NYSE_TZ).dt.date
        masked = is_open & local_day.isin(action_days)
        out["gap_masked"] = masked
        out.loc[masked, "r_gap"] = np.nan
    else:
        out["gap_masked"] = False
    return out


def _rolling_mad(series: pd.Series, window: int) -> pd.Series:
    """Median absolute deviation on a rolling window, EXCLUDING the current bar -
    the window ends on the previous one (§2.5)."""
    def mad(values: np.ndarray) -> float:
        median = np.median(values)
        return float(np.median(np.abs(values - median)))
    return series.shift(1).rolling(window).apply(mad, raw=True)


def winsorize(asset: Asset, frame: pd.DataFrame) -> pd.DataFrame:
    """Winsorization of returns per §2.5.

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
    # out-of-sample discipline as everything else in §3.1.
    sigma_lt = (returns.shift(1)
                .rolling(windows.SIGMA_LT_BARS, min_periods=windows.SIGMA_LT_MIN_BARS)
                .std(ddof=1))

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

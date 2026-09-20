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

The overnight jump is simply not a return here. It is not stored either - see
below.

This also disposes of the unadjusted-series problem. ETFs arrive from the source
without a dividend adjustment, and on the ex-date the price mechanically drops by
the payout. That drop happens between sessions, so it falls in the jump this
module discards and never reaches r_t.

WHAT WAS HERE AND IS NOT. The jump used to be kept as a second channel, r_gap,
alongside a gap_masked flag marking the ex-dates the corporate-actions table
knows about, so that the distribution of the gap channel would not be skewed by
regular dividend steps. Both were computed on every bar, written into every
metrics table, and read by nothing - the channel awarded no points and no message
ever quoted it. They are gone, and with them the corporate-actions lookup that
existed only to feed the flag. tremor.corporate_actions is still used for
un-adjustment, which is a different question and a live one.
"""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from tremor import ewma, windows
from tremor.basket import Asset

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
        # Vectorised, and it has to be: this used to call reference_week_bounds
        # once per bar, which at 145,000 bars was the single largest cost in the
        # whole metrics stage - 292,165 calls, two timezone conversions each.
        from tremor.sessions import reference_week_opens

        return reference_week_opens(moments, anchor_tz).astype("int64") // 10 ** 9

    raise ValueError(f"{asset.ticker}: unknown session template '{asset.session_template}'")


def split_channels(asset: Asset, usable: pd.DataFrame,
                   anchor_tz: str = "America/New_York") -> pd.DataFrame:
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
                          is_session_open=pd.Series(dtype=bool))

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

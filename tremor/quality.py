"""Data-quality gate and session membership of an hour (spec §2.2, §2.6).

Two different things, convenient to compute together because both answer the
question "does this bar take part in the calculations":

- VALIDITY is a property of the bar itself: prices positive, OHLC consistent,
  volume non-negative, timestamp not repeated. An invalid bar takes part in
  nothing and updates no state (§2.6).
- SESSION MEMBERSHIP is a property of the hour: per §2.2, hours outside an
  asset's trading session are excluded from EWMA, volume, CSV, PCA and the
  cluster shift.

The second turned out not to be a formality. The source keeps serving bars after
a half session closes: on 26 November 2021 the exchange closed at 13:00 New York
time, yet bars for 14:00 and 15:00 arrived anyway - with zero volume and a
creeping price. Without the session filter those hours would have entered the
EWMA state and the volume profile as genuine trading.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from tremor import sessions as sessions_mod
from tremor.basket import Asset

HOUR = 3600
NYSE_TZ = ZoneInfo("America/New_York")


def _session_bounds_utc(day: date, session: sessions_mod.Session) -> tuple[int, int]:
    """Session bounds in epoch UTC. They are stored in the exchange's local time
    and converted on the fly - §2.2 forbids storing them as UTC."""
    def at(hhmm: str) -> int:
        hour, minute = (int(x) for x in hhmm.split(":"))
        return int(datetime.combine(day, time(hour, minute), tzinfo=NYSE_TZ).timestamp())
    return at(session.local_open), at(session.local_close)


def in_session(asset: Asset, hours: pd.Series,
               session_table: dict[date, sessions_mod.Session] | None = None,
               anchor_tz: str = "America/New_York") -> pd.Series:
    """Whether an hour (by the bar's OPENING moment, §1.2) falls in the asset's session.

    An hour counts as trading if the interval [h, h+1) overlaps the session under
    a HALF-OPEN rule: h < close and h + hour > open. The closing auction is not
    lost by this - it prints before the closing moment and therefore lands in the
    last bar that lies wholly inside the session. But an hour that STARTS exactly
    at the close is already after-hours trading, and the rule discards it.
    """
    if asset.session_template == "crypto_24_7":
        return pd.Series(True, index=hours.index)

    if asset.session_template == "fx_continuous":
        # Spot FX trades continuously from Sun 17:00 to Fri 17:00 in the anchor
        # exchange's time - exactly the basket's reference week from §2.2.
        return sessions_mod.reference_hours_mask(hours, anchor_tz)

    if asset.session_template != "us_equity":
        raise ValueError(f"{asset.ticker}: unknown session template "
                         f"'{asset.session_template}'")

    table = session_table if session_table is not None else sessions_mod.load_sessions()
    local_days = pd.to_datetime(hours, unit="s", utc=True).dt.tz_convert(NYSE_TZ).dt.date
    bounds = {d: _session_bounds_utc(d, s) for d, s in table.items()}

    def inside(hour: int, day: date) -> bool:
        window = bounds.get(day)
        if window is None:
            return False
        opened, closed = window
        return hour < closed and hour + HOUR > opened

    return pd.Series([inside(int(h), d) for h, d in zip(hours, local_days)],
                     index=hours.index)


def invalid_reasons(asset: Asset, frame: pd.DataFrame) -> pd.Series:
    """Reason each bar is invalid, empty string for sound ones (§2.6).

    A string rather than a flag: when a bar is dropped from the calculations you
    need to be able to say why without reopening the data.
    """
    reasons = pd.Series("", index=frame.index, dtype="object")
    if frame.empty:
        return reasons

    prices = frame[["open", "high", "low", "close"]]
    reasons[prices.le(0).any(axis=1)] = "price not positive"

    # OHLC consistency with a half-tick tolerance: the source rounds the bar's
    # fields independently, and a discrepancy smaller than a tick is a rounding
    # artefact, not a broken bar (see tremor/audit.py and
    # docs/decisions.md).
    tolerance = asset.tick_size / 2
    inconsistent = (
        (frame["low"] > prices[["open", "close"]].min(axis=1) + tolerance)
        | (prices[["open", "close"]].max(axis=1) > frame["high"] + tolerance)
    )
    reasons[inconsistent & (reasons == "")] = "OHLC inconsistent"

    reasons[(frame["volume"] < 0) & (reasons == "")] = "volume negative"
    reasons[frame["hour_utc"].duplicated(keep="last") & (reasons == "")] = "duplicate hour"
    return reasons


def expected_hours(asset: Asset, first_hour: int, last_hour: int,
                   session_table: dict[date, sessions_mod.Session] | None = None,
                   anchor_tz: str = "America/New_York") -> list[int]:
    """Hours the asset SHOULD have in the range - that is, every hour of its
    session. The difference from what actually arrived is is_missing (§2.4)."""
    hours = pd.Series(range(first_hour - first_hour % HOUR, last_hour + HOUR, HOUR))
    mask = in_session(asset, hours, session_table, anchor_tz)
    return [int(h) for h in hours[mask]]


def apply_gate(asset: Asset, frame: pd.DataFrame,
               session_table: dict[date, sessions_mod.Session] | None = None,
               anchor_tz: str = "America/New_York") -> pd.DataFrame:
    """Adds the gate columns to the bars: invalidity reason, session flag and the
    resulting is_usable. Only rows with is_usable enter the calculations."""
    out = frame.copy()
    out["invalid_reason"] = invalid_reasons(asset, frame)
    out["in_session"] = (in_session(asset, frame["hour_utc"], session_table, anchor_tz)
                         if not frame.empty else pd.Series(dtype=bool))
    out["is_usable"] = (out["invalid_reason"] == "") & out["in_session"]
    return out

# A rounded price change of one tick can come from a true move of almost
# nothing: if prices are recorded on a grid of `tick`, an observed change of
# one tick means the true change was somewhere in (0, 2 ticks). Two ticks is
# the smallest observed change that GUARANTEES the true move exceeded one tick,
# which is why the threshold is two and not a number chosen for how it looked.
MIN_RESOLVABLE_TICKS = 2.0


def resolvable(close, r, tick_size: float,
               minimum_ticks: float = MIN_RESOLVABLE_TICKS):
    """Whether an hour's move is large enough to be a measurement of the market
    rather than of the price grid.

    This is NOT a second filter on magnitude, and the distinction is the whole
    of its justification. §8.3's trigger deliberately has no such filter: a
    single-asset move is grounds in itself and how much it matters is carried
    by the tier. But that presumes the move was observed at all. Below two
    ticks it was not - what varied was the rounding, and the instrument cannot
    express anything smaller.

    It matters for exactly one instrument here. Measured over the whole store,
    hourly price changes in ticks:

        SHY  median  1.1 ticks   29.8% of hours do not move at all
        IEF  median  5.4 ticks    7.0%
        SPY  median 29.9 ticks    1.4%

    SHY is a large-tick asset in the microstructure sense - the price resists
    moves of a single tick and the spread sits at one - so its return
    distribution measures the grid, and a ladder fitted to it ranks the grid.
    That is how a one-cent move on IEF came to be reported as the biggest in
    three years.

    The same reasoning already lives in §2.5's winsorization, whose eps_MAD
    floor includes the return on half a tick so that MAD cannot collapse to
    zero in quiet hours. This extends it from the scale to the event.
    """
    import numpy as np

    step = np.abs(np.asarray(r, dtype="float64")) * np.abs(
        np.asarray(close, dtype="float64"))
    return step >= minimum_ticks * tick_size

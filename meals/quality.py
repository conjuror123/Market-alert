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

from meals import sessions as sessions_mod
from meals.basket import Asset

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
        return hours.map(lambda h: sessions_mod.is_reference_hour(int(h), anchor_tz))

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
    # artefact, not a broken bar (see meals/audit.py and
    # docs/meals-deviations.md).
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

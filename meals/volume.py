"""Robust volume profile (spec §3.5).

Trading volume follows a sharp intraday seasonality: the first and last hour of
the US session are several times busier than midday, and comparing the current
hour against "usual volume" in general means declaring the open and the close an
anomaly every single day while never noticing a quiet noon. So the norm is taken
not globally but PER LOCAL EXCHANGE HOUR: noon is compared with noons, the open
with opens.

The hour is specifically the local exchange hour, not UTC: the seasonality is
tied to the trading schedule, and that lives in local time and shifts relative to
UTC twice a year with daylight saving.

The estimate is robust - median and MAD instead of mean and standard deviation.
For volume this matters more than for price: one day with news produces a spike
tens of times over, and after it an ordinary mean stops being a norm for a long
while. The logarithm ln(1 + V) is taken before everything else, because the
volume distribution is right-skewed by orders of magnitude.
"""
from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from meals import windows
from meals.basket import Asset

MAD_TO_SIGMA = 1.4826  # brings MAD onto the scale of a standard deviation


def _mad(values: np.ndarray) -> float:
    return float(np.median(np.abs(values - np.median(values))))


def robust_volume_z(asset: Asset, frame: pd.DataFrame, exchange_tz: str,
                    full_days: set[date] | None = None,
                    profile_days: int = windows.VOLUME_PROFILE_DAYS) -> pd.Series:
    """V_R per §3.5: how far an hour's volume stands out among volumes of the same
    local hour over the last 20 FULL trading days.

    Full means excluding half sessions and holidays: on a shortened day volume is
    lower by construction, and keeping such days in the norm would depress it for
    everyone else. The half-session bars themselves are still assessed normally,
    just against a profile built from full days.

    The current day is not in the profile: a norm that includes the observation
    being judged adjusts itself towards that observation.

    Instruments without volume (spot FX) get NULL across the whole series - per
    §3.5 volume confirmation is not assessed for them at all, rather than counted
    as having failed.
    """
    if frame.empty:
        return pd.Series(dtype="float64", index=frame.index)
    if not asset.has_volume:
        return pd.Series(np.nan, index=frame.index)

    local = pd.to_datetime(frame["hour_utc"], unit="s", utc=True).dt.tz_convert(
        ZoneInfo(exchange_tz))
    work = pd.DataFrame({
        # The day key is datetime64, not date: merge_asof below needs a numeric
        # or temporal key and refuses to work on an object dtype.
        "day": local.dt.tz_localize(None).dt.normalize(),
        "local_hour": local.dt.hour,
        "ln_volume": np.log1p(frame["volume"].astype("float64")),
    }, index=frame.index)
    work["is_full_day"] = (local.dt.date.isin(full_days) if full_days is not None
                           else True)

    result = pd.Series(np.nan, index=frame.index)
    for _, group in work.groupby("local_hour", sort=False):
        group = group.sort_values("day")
        full = group[group["is_full_day"]]
        if len(full) < profile_days:
            continue

        window = full["ln_volume"].rolling(profile_days, min_periods=profile_days)
        profile = pd.DataFrame({
            "day": full["day"].to_numpy(),
            "median_h": window.median().to_numpy(),
            "mad_h": window.apply(_mad, raw=True).to_numpy(),
        }).dropna()
        if profile.empty:
            continue

        # merge_asof without exact matches: the profile is taken from days
        # STRICTLY earlier than the current one.
        matched = pd.merge_asof(
            group.reset_index().sort_values("day"),
            profile.sort_values("day"),
            on="day", allow_exact_matches=False, direction="backward",
        ).set_index("index")

        scaled_mad = MAD_TO_SIGMA * matched["mad_h"]
        # Degenerate profile: this hour's volume has not moved for 20 days
        # running. Dividing by that is impossible, and declaring any deviation
        # infinite is worse, so §3.5 sets V_R = 0.
        degenerate = scaled_mad < windows.VOLUME_MAD_FLOOR
        values = (matched["ln_volume"] - matched["median_h"]) / scaled_mad
        values[degenerate] = 0.0
        result.loc[matched.index] = values

    return result


def full_session_days(session_table: dict[date, object]) -> set[date]:
    """Full-session days: neither a holiday (the day is in the table) nor a half session."""
    return {day for day, session in session_table.items() if not session.is_early_close}

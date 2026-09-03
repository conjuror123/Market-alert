"""VIX stress multiplier (spec §4.4).

The condition is one-sided, and that is the point: fear and relief are not
symmetric states of the market. A sharp rise in VIX means participants are
paying for protection, that is, they consider the near future dangerous; a fall
of the same size merely means a return to normal. So a falling VIX does not
count as stress and gives no multiplier.

The window is fixed and is NOT extended by repeat spikes. Otherwise a drawn-out
period of high volatility - when VIX jerks upward every day - would keep the
multiplier on for weeks, and it would stop distinguishing an acute moment from
the general background. Repeats inside a window are counted and logged, but they
do not move the window.

A departure from the letter of §4.4, recorded in docs/meals-deviations.md: the
series is daily, because no available source offers intraday VIX, and the window
starts at the moment the value became KNOWN to the system, not at the
observation date. FRED publishes the value on the next business day, and
counting from the observation date would mean the backtest using something that
did not yet exist in that hour.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from meals import windows, zscore

# Multiplier size and window length. Both starred in the spec.
M_VIX = 1.3
WINDOW_HOURS = windows.VIX_WINDOW

# Absolute-leg threshold for the VIX series (§4.4): 1.5 * sigma_LT.
ABS_LEG = windows.ABS_LEG_Q95


@dataclass(frozen=True)
class VixWindow:
    opened_at: int      # moment from which the multiplier applies (epoch UTC)
    closes_at: int      # moment after which it is back to 1.0
    spike_count: int    # how many spikes fell into this window, the first included


def load_series(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No VIX series at {path}. Fetch it with: python -m meals.backfill")
    return pd.read_parquet(path).sort_values("day").reset_index(drop=True)


def score(series: pd.DataFrame, window: int = windows.SIGMA_LT_MIN_BARS) -> pd.DataFrame:
    """Runs the VIX series through the §3.1 machinery with its own states.

    The threshold window for a daily series is 720 bars: §2.7 gives
    max(120 * B_asset, 720), and a daily series has one bar per day, so the
    floor is what applies.
    """
    out = series.copy()
    out["r"] = np.log(out["close"] / out["close"].shift(1))
    out["sigma_lt"] = (out["r"].shift(1)
                       .rolling(windows.SIGMA_LT_BARS,
                                min_periods=windows.SIGMA_LT_MIN_BARS).std(ddof=1))

    from meals.returns import _rolling_mad

    mad_24 = _rolling_mad(out["r"], windows.MAD_WINDOW)
    mad_eff = np.maximum(mad_24, 0.2 * out["sigma_lt"])
    limit = 5 * mad_eff
    out["r_w"] = np.where(out["r"].abs() > limit, np.sign(out["r"]) * limit, out["r"])
    out.loc[mad_eff.isna(), "r_w"] = out["r"][mad_eff.isna()]

    z, sigma_eff = zscore.ewma_state(out["r"].to_numpy(dtype="float64"),
                                     out["r_w"].to_numpy(dtype="float64"),
                                     out["sigma_lt"].to_numpy(dtype="float64"))
    out["z"] = z
    out.loc[out["sigma_lt"].isna(), "z"] = np.nan
    q95, _ = zscore.adaptive_thresholds(out["z"].abs(), window)
    out["q95"] = q95

    # All three conditions at once, and Z is taken WITH its sign.
    out["is_spike"] = ((out["z"] > out["q95"])
                       & (out["r"] > 0)
                       & (out["r"].abs() >= ABS_LEG * out["sigma_lt"]))
    return out


def windows_from_spikes(scored: pd.DataFrame, reference_hours: np.ndarray,
                        window_hours: int = WINDOW_HOURS) -> list[VixWindow]:
    """Builds the windows during which the multiplier applies.

    A new window opens only on a spike AFTER the previous one has closed; spikes
    inside an active window merely increment the counter.
    """
    reference = np.asarray(sorted(reference_hours))
    result: list[VixWindow] = []
    for _, row in scored[scored["is_spike"].fillna(False)].iterrows():
        opened = int(row["available_at"])
        if result and opened < result[-1].closes_at:
            last = result[-1]
            result[-1] = VixWindow(last.opened_at, last.closes_at, last.spike_count + 1)
            continue
        # The window ends 24 REFERENCE-CALENDAR hours later, not 24 calendar
        # hours: weekends do not count.
        start = int(np.searchsorted(reference, opened, side="left"))
        end_index = start + window_hours
        closes = (int(reference[end_index]) if end_index < len(reference)
                  else int(reference[-1]) + 3600)
        result.append(VixWindow(opened, closes, 1))
    return result


def multiplier_series(hours_utc, vix_windows: list[VixWindow]) -> dict[int, float]:
    """Multiplier per hour: 1.3 inside a window, 1.0 outside."""
    result = {int(h): 1.0 for h in hours_utc}
    if not vix_windows:
        return result
    hours = np.array(sorted(result))
    for window in vix_windows:
        inside = hours[(hours >= window.opened_at) & (hours < window.closes_at)]
        for hour in inside:
            result[int(hour)] = M_VIX
    return result

"""Registry of windows and constants (spec §2.7).

All in one place, because the main hazard here is mixing units. The
specification divides windows into three incompatible kinds, and confusing them
does not raise an error - it silently changes what the calculation means:

- per-asset windows are counted in VALID TRADING BARS of that asset. For a US
  ETF that is 7 bars a day, for a currency pair 24, so "120 bars" means 17
  trading days for one and 5 calendar days for the other;
- cross-sectional windows are counted in REFERENCE-CALENDAR HOURS of the
  basket, that is, on a scale shared by everything;
- and exactly one exception - the calendar-multiplier windows, which are
  measured in CALENDAR hours and are not shortened even when they cross a
  market close or a weekend.

Constant names deliberately mirror the notation of the spec: that way they can
be checked against it by eye, without holding a rename table in your head.
"""
from __future__ import annotations

# --- per-asset windows, in valid trading bars -----------------------------

# EWMA of returns (§3.1). Period 24 bars.
LAMBDA = 2 / (24 + 1)

# Winsorization window (§2.5): median absolute deviation over the last 24 valid
# bars, EXCLUDING the current one.
MAD_WINDOW = 24

# Smoothing of the Q95/Q99 thresholds (§3.1). Period 120 bars.
LAMBDA_Q = 2 / (120 + 1)

# Long-term sigma (§2.5, §3.1): 5000 bars or the whole history, but no fewer
# than 720.
SIGMA_LT_BARS = 5000
SIGMA_LT_MIN_BARS = 720

# Volume profile (§3.5) - 20 FULL trading days per local exchange hour; half
# sessions and holidays are excluded from the profile.
VOLUME_PROFILE_DAYS = 20

# Regression window on the basket factor (§3.6) - in bars where the instrument
# and the factor are BOTH valid.
REGRESSION_WINDOW = 500
REGRESSION_MIN = 200

# Per-asset cooldown of a single-asset event (§8.3) - in that asset's own bars;
# calendar hours are not used here.
SAED_COOLDOWN_BARS = 12

# Burn-in of an asset's EWMA state (§6.6).
EWMA_BURN_IN_BARS = 500

# --- cross-sectional windows, in reference-calendar hours -----------------

W_PCA = 120           # PCA window, one trading week (§3.3)
W_CS = 1200           # window for CSV_norm, PC1_ratio, sigma_M, k_t (§3.2, 3.3, 5.2)
CLUSTER_COOLDOWN = 72  # cluster-event cooldown (§5.1)
VIX_WINDOW = 24        # VIX multiplier window (§4.4)
ESCALATION_DEBOUNCE = 24  # escalation debounce window (§5.2)
TRUTH_HORIZON = 24     # horizon for truth labelling and baseline (§7)
REVERSAL_DELAY = 3     # delay before the vector-reversal branch (§5.2)
EXPORT_HALF_WINDOW = 12  # event export window around T0 (§6.5)

# --- calendar hours: the single exception (§2.7, §4.3) --------------------

CALENDAR_HIGH_BEFORE, CALENDAR_HIGH_AFTER = 6.0, 3.0
CALENDAR_MEDIUM_BEFORE, CALENDAR_MEDIUM_AFTER = 4.0, 2.0


def w_asset(bars_per_session: float) -> int:
    """Window for an asset's Q95/Q99 thresholds: max(120 * B_asset, 720) bars (§2.7).

    B_asset is the median number of valid bars in that asset's session. It
    cannot be a constant: a US ETF session yields 7 hourly bars, a currency pair
    24, and the same window expressed in bars would cover a different stretch of
    history for each. The floor of 720 bars keeps the window from collapsing on
    an instrument with a short session.
    """
    if bars_per_session <= 0:
        raise ValueError("B_asset must be positive")
    return max(int(120 * bars_per_session), 720)


def sigma_lt_bars(available_bars: int) -> int:
    """How many bars to take for the long-term sigma (§2.7).

    The spec's wording - "5000 bars or the whole history, but no fewer than
    720" - means that on a young series the whole available history is used, and
    that the value is undefined at all while there are fewer than 720 bars.
    """
    if available_bars < SIGMA_LT_MIN_BARS:
        raise ValueError(
            f"Not enough history for sigma_LT: {available_bars} bars "
            f"against a minimum of {SIGMA_LT_MIN_BARS}")
    return min(SIGMA_LT_BARS, available_bars)


# --- absolute legs of the hybrid significance condition (§3.1) -----------
#
# Starred in the spec: starting values, calibrated on train (§7).
# The point of the second, absolute leg is that the relative one is misleading
# on its own. In a very quiet stretch an asset's own volatility collapses, and a
# move that is negligible in absolute terms honestly clears its percentile. The
# absolute leg demands that the move also be large by the standards of the whole
# available history.
ABS_LEG_Q99 = 3.0   # |r_t| >= 3.0 * sigma_LT
ABS_LEG_Q95 = 1.5   # |r_t| >= 1.5 * sigma_LT

# Volume confirmation threshold (§3.5), also a starting value.
VOLUME_CONFIRM = 2.5

# Below this the scaled volume MAD counts as degenerate (§3.5).
VOLUME_MAD_FLOOR = 1e-6

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

# Bars between the end of the estimation window and the bar being scored. One
# bar is causality - the estimate at t may not have seen t - and that is all it
# ever was here. This is the ESTIMATION GAP the event-study literature treats as
# basic hygiene: a move that begins to leak in before the hour being judged
# would otherwise enter the estimate of what normal looks like, and normal would
# quietly absorb the front of the event. Three bars because the leak the field
# worries about is short and the cost is three bars of a five-hundred-bar
# window - the coefficients do not measurably move.
REGRESSION_GAP_BARS = 3

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

# The percentile the basket's coherence must clear for the single-factor trigger
# (§3.4). Starred: §7 calibrates it. 0.90 fires in 1.12% of hours once the second
# leg - the basket must actually have shifted - is applied.
COHERENCE_QUANTILE = 0.90

# The matched-horizon channel (deviation §24). §4.2's triggers are all one-hour
# statistics, while §7 asks what happens over the following 24 hours; the same
# basket move accumulated over 24 reference hours is three times more precise
# than its one-hour form (80% against 25% on train). These two say how far past
# its own recent history that accumulation and the single-asset event count must
# reach. Both starred.
SUSTAINED_WINDOW = 24
SUSTAINED_QUANTILE = 0.99
SAED_BREADTH_QUANTILE = 0.98
REVERSAL_DELAY = 3     # delay before the vector-reversal branch (§5.2)

# Floor under k_t in §5.2: the empirical percentile alone would sink so low in a
# prolonged lull that any wobble would read as a reversal. Starred - §7 lists k_t
# among the parameters calibrated on train.
REVERSAL_K_MIN = 1.5
EXPORT_HALF_WINDOW = 12  # event export window around T0 (§6.5)

# --- calendar hours: the single exception (§2.7, §4.3) --------------------

# Hours before and after a release, by (tier, importance). All starred: §7
# calibrates the multiplier's parameters on train.
#
# The spec's own numbers were 6/3 for High and 4/2 for Medium with no tiering,
# and on this calendar they do not discriminate. ForexFactory labels impact PER
# COUNTRY, which yields 823 High-impact releases a year; at a nine-hour window
# each that is 84.5% of the clock before Medium is counted at all, and the
# multiplier measured little beyond "a weekday, business hours, somewhere".
# Splitting by tier and shortening the windows takes the multiplier from covering
# 59.5% of hours to 17.4%. See docs/decisions.md.
CALENDAR_WINDOWS = {
    ("core", "High"): (2.0, 1.0),
    ("core", "Medium"): (1.0, 0.5),
    ("other", "High"): (1.0, 0.5),
    ("other", "Medium"): (0.5, 0.5),
}


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
ABS_LEG_Q99 = 6.0   # |r_t| >= 6.0 * sigma_LT  (*) calibrated on train, spec 3.0

# §8.2's absolute leg for the RESIDUAL series. The spec states it separately from
# §3.1's and both start at 3.0; this implementation shared one constant for both,
# which meant calibrating the price leg silently moved SAED's sensitivity too -
# and now that the SAED count feeds the cluster score, the search would have been
# optimising against a channel it was also disturbing without modelling it.
# Separate constants, as §3.1 and §8.2 have them. Starred.
# Critical value for a standardised abnormal return, two-sided 1%. This is how
# event studies decide: one standardised statistic against one critical value.
# The standardisation IS the test - weighting by precision is where the power
# comes from (Patell 1976, Boehmer et al. 1991) - and no second raw-magnitude
# filter is part of the standard procedure.
#
# We had one, and it was the whole remaining problem. Measured: the relative leg
# passed 2.3x more often in the widest fifth of hours than the calmest, the raw
# absolute leg 139.9x. A floor against a slow long-term sigma is no floor at all
# when the market is loud.
# The critical value itself has now gone the same way, and for a related reason.
# One value shared by every instrument answers "is this distinguishable from
# noise", which is a question about the null hypothesis rather than about the
# recipient: it made a once-a-decade move in SHY and a Tuesday in SOL come out
# looking identical. What replaced it is a return level fitted per instrument on
# its own history - see tremor.severity - so the threshold is no longer a
# constant and does not live here.

ABS_LEG_RESID = 3.0   # retired from the trigger; kept for the older reports
ABS_LEG_Q95 = 1.5   # |r_t| >= 1.5 * sigma_LT

# Volume confirmation threshold (§3.5), also a starting value.
VOLUME_CONFIRM = 2.5

# Below this the scaled volume MAD counts as degenerate (§3.5).
VOLUME_MAD_FLOOR = 1e-6

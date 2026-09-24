"""Registry of windows and constants.

All in one place, because the main hazard here is mixing units. Windows come in
three incompatible kinds, and confusing them does not raise an error - it
silently changes what the calculation means:

- per-asset windows are counted in VALID TRADING BARS of that asset. For a US
  ETF that is 7 bars a day, for a currency pair 24, so "120 bars" means 17
  trading days for one and 5 calendar days for the other;
- cross-sectional windows are counted in REFERENCE-CALENDAR HOURS of the
  basket, that is, on a scale shared by everything;
- and exactly one exception - the calendar-multiplier windows, which are
  measured in CALENDAR hours and are not shortened even when they cross a
  market close or a weekend.

Constant names deliberately mirror the notation used in the formulas they feed,
so the two can be checked against each other by eye without holding a rename
table in your head.
"""
from __future__ import annotations

# --- per-asset windows, in valid trading bars -----------------------------

# EWMA of returns. Period 24 bars.
LAMBDA = 2 / (24 + 1)

# Winsorization window: median absolute deviation over the last 24 valid
# bars, EXCLUDING the current one.
MAD_WINDOW = 24

# Smoothing of the Q95/Q99 thresholds. Period 120 bars.
LAMBDA_Q = 2 / (120 + 1)

# Long-term sigma. Exponentially weighted, not a box - see
# tremor/ewma.py for the shape, tools/sigma_window.py for the measurement.
#
# THE HALF-LIFE IS THE SETTING; THE SPAN IS A CONSEQUENCE. And it is set PER
# TRADING CALENDAR, because a bar is not a unit of anything until you say how
# many of them a day holds. The volatility-forecasting literature settles on
# 120-240 DAILY observations - short enough to follow the regime, long enough
# that the estimate is not mostly noise - and these are that band read in each
# instrument's own bars:
#
#   us_equity      600 bars =  86 trading days   (7 bars a day)
#   fx_continuous  1,400    =  82 trading days   (17)
#   crypto_24_7    2,000    =  83 calendar days  (24, and every one of them)
#   a daily series    83    =  83 trading days
#
# Which is the point: one number of BARS meant 290 calendar days of memory for
# an ETF and 58 for a coin, a five-fold spread nobody chose - it fell out of
# exchange hours. Three numbers, each the same span of market, is the honest
# way to write "the same amount of recent history" down.
#
# Measured, against the flat 5,000-bar box this replaced: the multiple that
# marks an instrument's rarest 1% of hours varied 3.0x between calendar years
# for the equity block and now varies 2.0x; crypto gains six points of coverage
# during its worst weeks. Message volume moves by under half a message per
# instrument-year, and the printed multiple does not move at all - this setting
# decides WHEN you hear, not how often.
DAILY_SERIES = "daily"          # one bar a day: the VIX, not an instrument
SIGMA_LT_HALFLIFE_BARS: "dict[str, int]" = {
    "us_equity": 600,
    "fx_continuous": 1400,
    "crypto_24_7": 2000,
    DAILY_SERIES: 83,
}
# Six half-lives of span because that is where the measurement stops improving:
# at four the gain is a third of what it could be, at eight and twelve it is no
# better than at six. The oldest bar in the window then carries one sixty-fourth
# of the newest one's weight, against the box window's one.
SIGMA_LT_SPAN_HALFLIVES = 6
SIGMA_LT_MIN_BARS = 720


def sigma_lt_halflife(template: str) -> int:
    """Bars over which the long-run sigma's weights halve, for this calendar."""
    try:
        return SIGMA_LT_HALFLIFE_BARS[template]
    except KeyError:
        raise ValueError(
            f"No sigma_LT half-life for session template '{template}'. It is a "
            "formula input, so guessing one would put an unexplained number "
            "under every alert that instrument sends.") from None


def sigma_lt_span(template: "str | None" = None) -> int:
    """How far back the long-run sigma reaches, in bars.

    None means "whatever the longest is". A caller that does not know which
    instrument it is loading history for should load MORE than it needs, never
    less: extra lead-in costs a read, and missing lead-in costs exactness.
    """
    if template is None:
        return SIGMA_LT_SPAN_HALFLIVES * max(SIGMA_LT_HALFLIFE_BARS.values())
    return SIGMA_LT_SPAN_HALFLIVES * sigma_lt_halflife(template)

# Volume profile - 20 FULL trading days per local exchange hour; half
# sessions and holidays are excluded from the profile.
VOLUME_PROFILE_DAYS = 20

# Regression window on the block factor - in bars where the instrument
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

# How much trailing history reproduces a recent bar EXACTLY, so that the hourly
# run does not have to recompute twenty-three years to learn about one hour.
#
# Every per-bar quantity here depends on a bounded stretch of the past. The two
# that bind are the long-run sigma at SIGMA_LT_BARS, and the adaptive thresholds,
# whose window is w_asset counted over DEFINED values - which for the residual
# series, present only on reference hours, spans considerably more rows than
# that - followed by an EWMA smoother with its own tail.
#
# The multiplier is measured rather than reasoned. Taking the smallest trailing
# window whose last two hundred bars agree with a full run to one part in a
# billion:
#
#     SPY      41,573 bars   w_asset   840    8,000
#     XLF      41,558 bars   w_asset   840    6,000
#     HYG      33,979 bars   w_asset   840    8,000
#     GLD      38,241 bars   w_asset   840    6,000
#     BTC-USD  97,585 bars   w_asset 2,880   10,000
#     ETH-USD  90,176 bars   w_asset 2,880   10,000
#     EUR/USD 147,083 bars   w_asset 2,880   12,000
#     USD/JPY 147,031 bars   w_asset 2,880   12,000
#
# which is SIGMA_LT_BARS plus about two and a half w_asset. Four is used here,
# so an ETF takes 8,360 and a currency pair 16,520 - between a third and a half
# again more than anything measured needed. The cost of being generous is a few
# seconds; the cost of being tight is a bar that disagrees with the archive.
# The window is warm-up PLUS a usable span, and the two are kept apart because
# they answer different questions.
#
# The warm-up is what it costs to make a bar exact: the long-run sigma and four
# times the adaptive-threshold window, per the measurements above. Bars inside
# it are not wrong in an interesting way, they are simply not finished, and
# nothing downstream should look at them.
#
# NO CALLER PASSES `rate` ANY MORE, and the paragraph below is history. Since the
# record book (saed.RecordBook) a warm events run keeps each series' "biggest
# since" lookup between runs and publishes only the fortnight after its
# checkpoint, so its slice is the warm-up alone plus that fortnight - the
# drift measured below lived in the published years that were still inside the
# warm-up, and nothing is published from there now. The function keeps the
# argument because removing it would move config_version for no change.
#
# The usable span is how far back the run must still be RIGHT. A push says "the
# last one this big was 23 days ago", read off the event table, so the table has
# to be correct at least as far back as the deepest rung claims - six years -
# or a once-in-six-years move would name the wrong predecessor or none. It
# follows severity.RECORD_HORIZON_DAYS rather than a constant, so moving the
# horizon moves this with it: at six years an ETF must stay exact over 10,519
# bars where three asked for 5,259, and a warm run costs that much more of the
# archive. Sizing
# the window at warm-up alone was measured and rejected: tiers matched exactly
# within a year and then drifted, 50 of them across the whole window, with 31
# events appearing that a full run does not produce.
def trusted_bars(rate: float) -> int:
    """How many bars back a run must still be exact, at this instrument's rate."""
    from tremor.severity import RECORD_HORIZON_DAYS

    return int(RECORD_HORIZON_DAYS * 24 * rate)


def warm_bars(w_asset_bars: int, rate: float | None = None,
              template: "str | None" = None) -> int:
    window = sigma_lt_span(template) + 4 * int(w_asset_bars)
    return window + trusted_bars(rate) if rate else window


# THE PER-ASSET COOLDOWN IS NOT A NUMBER AND SO IS NOT HERE. The twelve bars
# are gone: an instrument and a block each report once per THEIR OWN TRADING DAY,
# which is a rule with no window to tune. See tremor.saed.build_events.

# Burn-in of an asset's EWMA state.
EWMA_BURN_IN_BARS = 500

# --- time of day, for the abnormal channel of a US fund ---------------------
#
# A fund's opening half-hour is not one of its ordinary hours, and the abnormal
# channel used to treat it as one. Its short yardstick (sigma_eff, a day or so
# of memory) is the same at every hour, so the 09:30 residual - about twice the
# midday one - was judged against yesterday's quiet afternoon. Measured: the
# abnormal rung was crossed on 0.61% of opening bars against 0.14-0.23% of
# every other hour, 36x for XLI and 29x for XLV, while the absolute channel
# found the open no busier than 10:00 (0.74% against 0.73%). "Unusual for this
# fund" had become "usual for this fund's opening".
#
# So the residual is scaled by the hour's own usual size relative to all hours
# (residuals.hour_scale) before it is standardised. The level still comes from
# every hour; only the SHAPE is per hour, and the shape is steady - XLI's
# opening ran 2.0-2.8x its other hours every year since 2008 - so it is learned
# over a long memory rather than cut to a seventh of the data. Three memories
# were measured: 86, 250 and 500 sessions all predict the next half-year's
# opening to within 14-15%, and differ in how much they wobble month to month
# (4.0%, 1.9%, 1.3%). 500 is the steadiest, and its cost - a longer warm slice,
# about ten seconds a run - was judged not to matter.
#
# US funds only. FX has a time-of-day pattern too, a 10x spread, but it peaks
# at the hour US data is released - there the busy hour IS the news.
HOUR_SCALE_TEMPLATES = ("us_equity",)
HOUR_SCALE_MEMORY_SESSIONS = 500
HOUR_SCALE_BARS_PER_SESSION = 7
HOUR_SCALE_MIN_SAME_HOUR = 100


# --- what kind of close came before a gap, for tremor.gaps ------------------
#
# A US fund's opening gap follows a weeknight, a weekend or a holiday, and they
# are not the same size. Measured on the stored bars across the 44 funds, the
# typical gap after a weekend is 1.17x a weeknight's (0.95x TIP to 1.60x UNG),
# after a holiday or a long weekend larger still - where one pooled yardstick
# makes every Monday look a little more unusual than it is and every weeknight
# a little less. Not three nights' worth of news, which would be 1.7x: most of
# what moves a price over a weekend is the same few headlines a weeknight has.
#
# So the gap is judged against the usual gap of ITS KIND of close, the same way
# a fund's opening hour is judged against other openings: the level from every
# gap, the shape - this kind's spread over all kinds' - learned per fund over a
# long memory, because the shape is a property of the calendar and not of the
# month. Two kinds, a weeknight and any longer close: some 1,000 weekends per
# fund steady the ratio, the 200-odd holidays alone would not, and a holiday
# gap behaves like a weekend one in having more than a night behind it. The
# memory is counted in sessions on the calendar every kind shares, so the
# weeknight and the weekend spread forget at the same pace; 100 closes of a
# kind - about two years - before that kind's ratio is used, and a ratio of one
# until then. A currency pair's gap is always the weekend, so its ratio is one
# by construction.
GAP_KIND_MEMORY_SESSIONS = 500
GAP_KIND_MIN_SAME_KIND = 100


def hour_scale_chain(template: "str | None") -> int:
    """Bars of history the hour scale needs behind the first bar it must get
    exactly right: the regression that makes the residual, then the scale's
    own reach over that residual, then the EWMA state it feeds. Zero for a
    template it does not apply to."""
    if template not in HOUR_SCALE_TEMPLATES:
        return 0
    span = 6 * HOUR_SCALE_MEMORY_SESSIONS * HOUR_SCALE_BARS_PER_SESSION
    return REGRESSION_WINDOW + REGRESSION_GAP_BARS + span + EWMA_BURN_IN_BARS

# --- cross-sectional windows, in reference-calendar hours -----------------

W_PCA = 120           # PCA window, one trading week
W_CS = 1200           # window for CSV_norm, PC1_ratio, sigma_M, k_t

# THREE OF THE FOUR BELOW HAVE NO CALLER, marked where they stand rather than
# removed: this file is a config input hashed as parsed code, so deleting three
# unread names - or even reordering them - moves config_version and rebuilds
# every metric cold, for no change in behaviour. Do not read a dead one as live
# tuning, and do not tune one expecting an effect.
CLUSTER_COOLDOWN = 72  # DEAD: the cluster detector's cooldown
VIX_WINDOW = 24        # VIX multiplier window - live, read by vix and delivery
ESCALATION_DEBOUNCE = 24  # DEAD: escalation debounce
TRUTH_HORIZON = 24     # DEAD: horizon of the retired truth-labelling protocol

# The percentile the basket's coherence must clear for the single-factor trigger.
# A starting value, calibrated on the training period: 0.90 fires in 1.12% of
# hours once the second leg - the basket must actually have shifted - is applied.
COHERENCE_QUANTILE = 0.90

# The matched-horizon channel (deviation). The triggers are all one-hour
# statistics, while calibration asks what happens over the following 24 hours; the same
# basket move accumulated over 24 reference hours is three times more precise
# than its one-hour form (80% against 25% on train). These two say how far past
# its own recent history that accumulation and the single-asset event count must
# reach. Both starred.
SUSTAINED_WINDOW = 24
SUSTAINED_QUANTILE = 0.99
SAED_BREADTH_QUANTILE = 0.98
REVERSAL_DELAY = 3     # delay before the vector-reversal branch

# Floor under the cluster detector's k_t: the empirical percentile alone would sink so low in a
# prolonged lull that any wobble would read as a reversal. A starting value; k_t
# among the parameters calibrated on train.
REVERSAL_K_MIN = 1.5
EXPORT_HALF_WINDOW = 12  # event export window around T0

# --- calendar hours: the single exception --------------------

# Hours before and after a release, by (tier, importance). All starting values:
# calibrates the multiplier's parameters on train.
#
# The first numbers tried were 6/3 for High and 4/2 for Medium with no tiering,
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
    """Window for an asset's Q95/Q99 thresholds: max(120 * B_asset, 720) bars.

    B_asset is the median number of valid bars in that asset's session. It
    cannot be a constant: a US ETF session yields 7 hourly bars, a currency pair
    24, and the same window expressed in bars would cover a different stretch of
    history for each. The floor of 720 bars keeps the window from collapsing on
    an instrument with a short session.
    """
    if bars_per_session <= 0:
        raise ValueError("B_asset must be positive")
    return max(int(120 * bars_per_session), 720)


def sigma_lt_bars(available_bars: int, template: "str | None" = None) -> int:
    """How many bars the long-run sigma reaches over, given what exists.

    On a young series the whole available history is used, and the value is
    undefined at all below 720 bars - the rule the box window kept, and one the
    exponential form needs just as much: XLP holds 7,246 bars against an equity
    span of 3,600, but there are thinner series than XLP.
    """
    if available_bars < SIGMA_LT_MIN_BARS:
        raise ValueError(
            f"Not enough history for sigma_LT: {available_bars} bars "
            f"against a minimum of {SIGMA_LT_MIN_BARS}")
    return min(sigma_lt_span(template), available_bars)


# --- absolute legs of the hybrid significance condition -----------
#
# Starting values, calibrated on the training period.
# The point of the second, absolute leg is that the relative one is misleading
# on its own. In a very quiet stretch an asset's own volatility collapses, and a
# move that is negligible in absolute terms honestly clears its percentile. The
# absolute leg demands that the move also be large by the standards of the whole
# available history.
ABS_LEG_Q99 = 6.0   # |r_t| >= 6.0 * sigma_LT  (*) calibrated on train, spec 3.0

# The absolute leg for the RESIDUAL series. It was always a separate number from
# the price series' leg, and both start at 3.0; this implementation shared one constant for both,
# which meant calibrating the price leg silently moved SAED's sensitivity too -
# and now that the SAED count feeds the cluster score, the search would have been
# optimising against a channel it was also disturbing without modelling it.
# Separate constants, one per series. Starting values.
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

# Volume confirmation threshold, also a starting value.
VOLUME_CONFIRM = 2.5

# Below this the scaled volume MAD counts as degenerate.
VOLUME_MAD_FLOOR = 1e-6

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

# Smoothing of the VIX spike threshold (tremor.vix, the fear-gauge line on the
# digest note). Period 120 bars.
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
# Every per-bar quantity depends on a bounded stretch of the past, and the
# longest chain is: the block regression (REGRESSION_WINDOW + REGRESSION_GAP_BARS
# bars behind each residual), then the long-run sigma of that residual (its span
# of residuals), then the short-memory state fed by it (EWMA_BURN_IN_BARS, after
# which the starting value weighs about 1e-18). The price metrics need a subset
# of the same chain - the span and the burn-in - so one number serves both.
#
# It used to add four windows of the Q95/Q99 thresholds, the one piece that
# needed more; those thresholds fed no decision and were removed, and the lead
# shrank with them - by more than half for a currency pair or a coin. A US fund's
# events slice is set by the longer hour-scale chain instead (hour_scale_chain).
def warm_bars(template: "str | None" = None) -> int:
    """Bars of lead-in that make every quantity of the newest bar exact."""
    return (sigma_lt_span(template) + REGRESSION_WINDOW + REGRESSION_GAP_BARS
            + EWMA_BURN_IN_BARS)


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
# A US fund's opening gap follows a weeknight or a weekend, and they are not the
# same size. Measured on the stored bars across the 44 funds, the typical gap
# after a weekend is 1.17x a weeknight's (0.95x TIP to 1.60x UNG) - where one
# pooled yardstick makes every Monday look a little more unusual than it is and
# every weeknight a little less. Not three nights' worth of news, which would be 1.7x: most of
# what moves a price over a weekend is the same few headlines a weeknight has.
#
# So the gap is judged against the usual gap of ITS KIND of close, the same way
# a fund's opening hour is judged against other openings: the level from every
# gap, the shape - this kind's spread over all kinds' - learned per fund over a
# long memory, because the shape is a property of the calendar and not of the
# month. Some 1,000 weekends per fund steady the ratio. A midweek holiday comes
# two or three times a year - far too few to learn its own - so its gap counts
# as a weeknight (tremor.gaps); a long weekend counts as a weekend. The
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

# How long a VIX spike keeps the fear-gauge line's "stress episode" open, in
# reference hours - read by tremor.vix and the delivery layer.
VIX_WINDOW = 24


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


# --- the VIX spike test ----------------------------------------------------
#
# A VIX rise counts as a spike only if it is also large against the series' own
# long-run sigma, not merely above its recent percentile: in a quiet stretch the
# percentile sinks and a negligible rise would clear it.
ABS_LEG_Q95 = 1.5   # |r_t| >= 1.5 * sigma_LT

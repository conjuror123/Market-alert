"""Registry of windows and constants.

Two users are left: the pipeline, which needs to know how much history
reproduces a recent metric row exactly (warm_bars), and the fear-gauge line on
the weekly note (jump.vix), which runs the daily VIX series through a
long-run sigma and a short-memory EWMA state. The jump detector keeps its own
settings in config/basket.yaml under `detector:`.

Constant names deliberately mirror the notation used in the formulas they feed,
so the two can be checked against each other by eye without holding a rename
table in your head.
"""
from __future__ import annotations

# --- how much history an extension recomputes -------------------------------
#
# How much trailing history reproduces a recent metric row EXACTLY, so that the
# hourly run does not have to recompute twenty-three years to learn about one
# hour. A row is the hour's move, which needs the bar before it, and the gap
# before a session, which needs the last close before it - a few sessions at
# most, a long weekend plus a holiday. Five hundred bars is ten weeks of a fund
# and three of a currency pair or a coin: far more than either needs, and cheap.
WARM_BARS = 500


def warm_bars(template: "str | None" = None) -> int:
    """Bars of lead-in that make every quantity of the newest metric row exact."""
    return WARM_BARS


# --- the VIX series (jump.vix, the fear-gauge line on the weekly note) -----

# EWMA of returns. Period 24 bars.
LAMBDA = 2 / (24 + 1)

# Winsorization window: median absolute deviation over the last 24 valid
# bars, EXCLUDING the current one.
MAD_WINDOW = 24

# Smoothing of the VIX spike threshold. Period 120 bars.
LAMBDA_Q = 2 / (120 + 1)

# Long-term sigma of the daily series. Exponentially weighted, not a box - see
# jump/ewma.py for the shape. 83 daily bars of half-life, the band the
# volatility-forecasting literature settles on (120-240 daily observations of
# span per half-life pair); six half-lives of span, past which the measurement
# stops improving.
DAILY_SERIES = "daily"          # one bar a day: the VIX, not an instrument
SIGMA_LT_HALFLIFE_BARS: "dict[str, int]" = {
    DAILY_SERIES: 83,
}
SIGMA_LT_SPAN_HALFLIVES = 6
SIGMA_LT_MIN_BARS = 720


def sigma_lt_halflife(template: str) -> int:
    """Bars over which the long-run sigma's weights halve, for this series."""
    try:
        return SIGMA_LT_HALFLIFE_BARS[template]
    except KeyError:
        raise ValueError(
            f"No sigma_LT half-life for '{template}'. It is a formula input, so "
            "guessing one would put an unexplained number under the line.") from None


def sigma_lt_span(template: "str | None" = None) -> int:
    """How far back the long-run sigma reaches, in bars."""
    if template is None:
        return SIGMA_LT_SPAN_HALFLIVES * max(SIGMA_LT_HALFLIFE_BARS.values())
    return SIGMA_LT_SPAN_HALFLIVES * sigma_lt_halflife(template)


# How long a VIX spike keeps the fear-gauge line's "stress episode" open, in
# reference hours - read by jump.vix and the delivery layer.
VIX_WINDOW = 24

# A VIX rise counts as a spike only if it is also large against the series' own
# long-run sigma, not merely above its recent percentile: in a quiet stretch the
# percentile sinks and a negligible rise would clear it.
ABS_LEG_Q95 = 1.5   # |r_t| >= 1.5 * sigma_LT

"""How big is big: severity tiers expressed as return periods.

The detector used to answer one question - did this hour exceed the critical
value - and every event it produced looked alike. That is the wrong shape for
the thing being built. A move worth interrupting someone for and a move worth
a line in Friday's digest are both "significant" against the same null
hypothesis; they differ in RARITY, and rarity is what the recipient actually
reasons about.

So the output is no longer a boolean, it is a tier, and the tiers are return
periods: how long you would ordinarily wait to see a move this large in THIS
instrument. "The largest idiosyncratic move in BTC since March 2023" needs no
calibration intuition to read. A 1-to-100 importance score does, and nobody
ever builds it.

Return periods also solve the comparison problem for free. A 1.5% day is
unremarkable for SOL and a once-a-year event for SHY; the same percentage
means different things per instrument, but "once a year" means the same thing
everywhere. That is the whole reason the hydrology and insurance literature
states extremes this way rather than in raw units.

THE ESTIMATOR. Return levels far out in the tail cannot be read off the
empirical distribution - a once-in-three-years level over five years of history
rests on one or two observations. The standard answer is peaks-over-threshold:
fit a Generalised Pareto distribution to the excesses above a high threshold
and extrapolate from the fitted tail (Coles 2001, ch. 4). The fit here uses
probability-weighted moments (Hosking & Wallis 1987) rather than maximum
likelihood - it is a closed form, so there is no optimiser to fail to converge,
and it is the better estimator for the small tail samples this actually has.

Below the POT threshold there is no need to extrapolate at all: the routine
tier sits where the empirical quantile has hundreds of observations behind it,
so that is what is used. The two meet by construction at the threshold, where
the expected exceedance count is one.

CAUSALITY. The levels are refitted on an expanding window and applied only
forward, never to the bars they were fitted on. This costs a warm-up period at
the start of history where no tier can be assigned, and it is worth it: a
full-sample fit would label a 2016 move using the knowledge that 2020 was
coming, which makes every backtested tier optimistic and makes the live system
behave differently from the tested one. The refit is periodic rather than
per-bar because a tail estimate does not meaningfully move in an hour, and
recalibrating monthly is what practitioners do.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# The ladder, in calendar days. These are the recipient's categories, not the
# statistician's: how often a person is willing to hear from the system at each
# level of seriousness. Everything rarer than the top tier is still the top
# tier - there is no fifth box, because at that point the message is the same.
TIER_DAYS: dict[str, float] = {
    "routine": 14.0,      # a fortnight   - digest only, roughly 26 a year
    "notable": 61.0,      # two months    - pushed, roughly 6 a year
    "major": 365.25,      # a year        - pushed, roughly 1 a year
    "extreme": 1095.75,   # three years   - pushed, roughly 1 every 3 years
}
TIERS: tuple[str, ...] = tuple(TIER_DAYS)

HOURS_PER_DAY = 24.0

# How many tail points to fit the GPD on. Too few and the shape parameter is
# noise; too many and the fit is dragged down by the body of the distribution,
# which is not Pareto and was never claimed to be. One percent of the sample is
# the usual starting point, held between these bounds so that a short history
# still gets a usable fit and a long one does not reach down into the body.
POT_MIN_EXCEEDANCES = 50
POT_MAX_EXCEEDANCES = 1000
POT_FRACTION = 0.01

# The upper bound started at 500 and was raised on measurement, against the
# intuition that a tail fit wants the deepest threshold it can get. Checked
# against Student-t(4) - a harder case than the GPD itself, since t only
# approaches Pareto asymptotically - the three-year return level came out 13%
# low at 200 tail points and within 2% at 600. Below a few hundred points the
# variance of the shape estimate dominates the asymptotic bias it was meant to
# remove, and a level that is 13% low fires close to twice as often as its
# nominal rate. One percent of the sample lands inside the flat part of that
# curve for every history length here, from 3000 bars to 48000.

# The tail index is clipped before it is used. Left unbounded, a fit on fifty
# points can return a shape above 1 - a distribution with no finite mean - and
# extrapolate it three years out, which produces a threshold no move will ever
# reach and silences the instrument completely. Financial tails sit well inside
# this range; the clip only ever catches a fit that has gone wrong.
SHAPE_MIN, SHAPE_MAX = -0.5, 0.5

# Two calendar years before the first tier is assigned, and a refit every 30
# calendar days after that. Both are expressed in calendar time and converted
# per asset, because an ETF and a currency pair reach two years of history at
# very different bar counts.
WARMUP_DAYS = 730.0
REFIT_DAYS = 30.0

# A tier is not assignable until the instrument has as much history as the tier
# claims. "The largest move in three years" cannot be said on two years of data,
# and the arithmetic agrees with the English: fitted at the two-year mark, the
# three-year level came out low enough to produce eight "extreme" events in one
# month across the basket - a burst that sat exactly on the warm-up boundary and
# nowhere else. Extrapolating a return level much past the sample length is
# where POT stops being reliable, and this is the natural place to draw it,
# because it is also the point past which the message would be a claim the data
# cannot support. Moves that clear an unavailable level are not lost: they land
# in the deepest tier the history does support.
EXTRAPOLATION_LIMIT = 1.0


def bar_rate(hour_utc: pd.Series | np.ndarray) -> float:
    """Bars per calendar hour for one instrument, measured rather than declared.

    This is what converts the ladder above - which is in calendar time, because
    that is the unit the recipient thinks in - into the bar counts the quantiles
    need. Round-the-clock crypto returns about 1.0, a US equity ETF about 0.2:
    seven bars a day, five days in seven. Measuring it beats deriving it from
    the session template, which would have to be right about holidays too.
    """
    hours = np.asarray(hour_utc, dtype="float64")
    if hours.size < 2:
        return 1.0
    span = (np.nanmax(hours) - np.nanmin(hours)) / 3600.0 + 1.0
    if not np.isfinite(span) or span <= 0:
        return 1.0
    return float(np.clip(hours.size / span, 1e-3, 1.0))


def fit_gpd(excesses: np.ndarray) -> tuple[float, float]:
    """Generalised Pareto (shape, scale) by probability-weighted moments.

    Hosking & Wallis (1987), in their parameterisation F(y) = 1 - (1 - ky/a)^(1/k),
    which relates to the usual extreme-value shape by xi = -k. Both estimators
    are closed forms of the first two PWMs, so this cannot fail to converge; it
    can still be handed a degenerate sample, and returns the exponential case
    (shape 0) when it is, which is the right conservative answer.
    """
    y = np.sort(np.asarray(excesses, dtype="float64"))
    y = y[np.isfinite(y)]
    n = y.size
    if n < 2:
        return 0.0, float(y.mean()) if n else 0.0

    plotting = (np.arange(1, n + 1) - 0.35) / n
    a0 = float(y.mean())
    a1 = float(np.mean(y * (1.0 - plotting)))
    denominator = a0 - 2.0 * a1
    if not np.isfinite(denominator) or abs(denominator) < 1e-12:
        return 0.0, max(a0, 1e-12)

    shape = float(np.clip(-(a0 / denominator - 2.0), SHAPE_MIN, SHAPE_MAX))

    # The scale is recovered from the mean excess and the CLIPPED shape rather
    # than taken from the second PWM directly. Algebraically the two are the
    # same thing - the PWM scale is exactly a0 * (1 - shape), since the GPD mean
    # is scale / (1 - shape) - so nothing changes on a well-behaved sample. What
    # it fixes is the clipped one: clipping the shape alone leaves a pair that
    # no longer describes any distribution, and a fit on near-identical excesses
    # then pairs a shape of -0.5 with a scale in the thousands and extrapolates
    # a level no move will ever reach. That does not fail loudly - it silences
    # the instrument. Recomputing keeps the pair consistent by construction.
    scale = a0 * (1.0 - shape)
    if not np.isfinite(scale) or scale <= 0:
        return 0.0, max(a0, 1e-12)
    return shape, float(scale)


def return_level(values: np.ndarray, m: float) -> float:
    """The level exceeded on average once every `m` bars.

    Two regimes, and the split is not a hedge. Out in the tail, where the
    expected number of exceedances of the POT threshold is below one, the
    empirical distribution has nothing to say and the fitted GPD extrapolates
    (Coles 2001, eq. 4.13). Closer in, the empirical quantile has hundreds of
    observations behind it and is simply better than a model. They agree where
    they meet, at m * zeta = 1, because that is the point at which the GPD
    formula returns the threshold itself.
    """
    x = np.asarray(values, dtype="float64")
    x = x[np.isfinite(x)]
    n = x.size
    if n == 0 or not np.isfinite(m) or m <= 1:
        return float("nan")

    k = int(np.clip(round(POT_FRACTION * n), POT_MIN_EXCEEDANCES, POT_MAX_EXCEEDANCES))
    k = min(k, n // 2)
    if k < 2:
        return float(np.quantile(x, 1.0 - 1.0 / m)) if n >= m else float("nan")

    ordered = np.sort(x)
    threshold = float(ordered[-k - 1])
    zeta = k / n

    if m * zeta < 1.0:
        # Inside the body: the empirical quantile is better than any fit.
        return float(np.quantile(x, 1.0 - 1.0 / m))

    shape, scale = fit_gpd(ordered[-k:] - threshold)
    if abs(shape) < 1e-6:
        return threshold + scale * float(np.log(m * zeta))
    return threshold + (scale / shape) * (float((m * zeta) ** shape) - 1.0)


def tier_levels(values: np.ndarray, rate: float,
                available_days: float = float("inf")) -> dict[str, float]:
    """One return level per tier, forced to be non-decreasing.

    The monotonicity is imposed rather than assumed. The routine tier comes
    from an empirical quantile and the rarer ones from a fitted tail; nothing
    in either guarantees they come out in order, and a "major" level below the
    "notable" one would let a move land in the higher box while failing the
    lower, which is not a thing the ladder is allowed to do.
    """
    levels: dict[str, float] = {}
    running = -np.inf
    for name, days in TIER_DAYS.items():
        if days > EXTRAPOLATION_LIMIT * available_days:
            levels[name] = float("nan")
            continue
        level = return_level(values, days * HOURS_PER_DAY * rate)
        if np.isfinite(level):
            running = max(running, level)
            levels[name] = running
        else:
            levels[name] = float("nan")
    return levels


def magnitudes(score: pd.Series, two_sided: bool = True) -> pd.Series:
    """The quantity the ladder is actually built on.

    A return or a standardised residual is two-sided: a large fall is as much
    an event as a large rise, so the ladder is built on the absolute value. A
    volatility LEVEL is not - only high is an event, and low is the calmest
    market on record. Taking the absolute value of one of those is not a
    conservative default, it is an inversion: the detector's forecast is a log
    volatility running from -8.26 to -4.98, and the ladder built on its
    magnitude ranked the quietest hours as the rarest.
    """
    return score.abs() if two_sided else score


def rolling_levels(score: pd.Series, rate: float | None = None,
                   hour_utc: pd.Series | None = None,
                   two_sided: bool = True) -> pd.DataFrame:
    """Per-bar tier levels, each fitted only on bars strictly before it.

    Nothing is assigned during the warm-up: the levels stay NaN until there is
    enough history for the fit to mean anything, and every downstream caller
    treats NaN as "no tier", not as "no event". Those are different, and the
    distinction matters when the evaluation harness asks why an instrument was
    silent in 2015.

    The deeper tiers stay NaN for longer still, until the instrument has as much
    history as the tier claims (EXTRAPOLATION_LIMIT). So an instrument's ladder
    grows a rung at a time as it ages, which is the honest behaviour: it can say
    "the largest in two months" long before it has earned the right to say "the
    largest in three years".
    """
    magnitude = magnitudes(score, two_sided).to_numpy(dtype="float64")
    n = magnitude.size
    if rate is None:
        rate = bar_rate(hour_utc) if hour_utc is not None else 1.0

    frame = pd.DataFrame({name: np.full(n, np.nan) for name in TIERS},
                         index=score.index)
    warmup = int(WARMUP_DAYS * HOURS_PER_DAY * rate)
    step = max(int(REFIT_DAYS * HOURS_PER_DAY * rate), 1)
    if n <= warmup:
        return frame

    for start in range(warmup, n, step):
        available_days = start / (rate * HOURS_PER_DAY)
        levels = tier_levels(magnitude[:start], rate, available_days)
        stop = min(start + step, n)
        for name, level in levels.items():
            frame.iloc[start:stop, frame.columns.get_loc(name)] = level
    return frame


def assign(score: pd.Series, levels: pd.DataFrame,
           two_sided: bool = True) -> pd.Series:
    """The tier of each bar: the rarest level it clears, or NA for none.

    NA covers both "quieter than the routine level" and "no level was fitted
    yet", which are deliberately the same answer here - in both cases there is
    nothing to say about this bar - and are told apart, when it matters, by
    whether the levels themselves are NaN.
    """
    magnitude = magnitudes(score, two_sided)
    tier = pd.Series(pd.NA, index=score.index, dtype="string")
    for name in TIERS:
        tier = tier.mask(magnitude > levels[name], name)
    return tier


def rank(tier: pd.Series) -> pd.Series:
    """Tier as an integer, 1 for routine up to 4, so it can be compared and maxed."""
    order = {name: i + 1 for i, name in enumerate(TIERS)}
    return tier.map(order).astype("Int64")


LEVEL_PREFIX = "level"
LEVEL_COLUMNS: tuple[str, ...] = tuple(f"{LEVEL_PREFIX}_{name}" for name in TIERS)


def level_columns(prefix: str = LEVEL_PREFIX) -> tuple[str, ...]:
    return tuple(f"{prefix}_{name}" for name in TIERS)


def annotate(frame: pd.DataFrame, column: str = "z_resid_bmp",
             prefix: str = LEVEL_PREFIX, tier_column: str = "tier",
             fallback: str | None = "z_resid",
             two_sided: bool = True) -> pd.DataFrame:
    """Adds the four fitted levels and the resulting tier to one asset's frame.

    The column is a parameter because the same question - how rare is this for
    this instrument - is worth asking of more than one quantity. Asked of the
    residual it means "the market did not explain this"; asked of the raw
    return it means "this was a big move". Those are different events and both
    are wanted, which is why nothing here is specific to either.

    The bar rate is measured from the frame's own hours, so an instrument that
    changed session length part-way through history - a venue extending its
    hours, an ETF that started trading pre-market - is described by its average
    rather than by whatever it does today. That is the conservative reading:
    the ladder is in calendar time, and the average is what actually maps a
    fortnight onto a bar count over the stretch being fitted.
    """
    if column in frame:
        score = frame[column]
    elif fallback is not None and fallback in frame:
        score = frame[fallback]
    else:
        raise KeyError(f"{column!r} not in frame and no usable fallback")

    rate = bar_rate(frame["hour_utc"]) if "hour_utc" in frame else 1.0
    levels = rolling_levels(score, rate, two_sided=two_sided)
    out = frame.copy()
    for name in TIERS:
        out[f"{prefix}_{name}"] = levels[name].to_numpy()
    out[tier_column] = assign(score, levels, two_sided).to_numpy()
    return out


def combine(frame: pd.DataFrame, sources: dict[str, str],
            tier_column: str = "tier", basis_column: str = "basis"
            ) -> pd.DataFrame:
    """Merges several tier columns into one, keeping the rarest and its origin.

    `sources` maps a basis name to the tier column that carries it. An hour that
    clears more than one is reported at its rarest tier and marked "both",
    because it is one event and it is delivered once - a move that was both
    enormous and unexplained does not become two messages.

    Which basis a tier came from is not cosmetic. It decides which retention
    series answers "did it hold" for that event (see meals.persistence), and it
    is the difference between "gold moved and nothing else did" and "everything
    moved, gold included", which read as entirely different news.
    """
    order = {name: i for i, name in enumerate(TIERS)}
    present = {basis: column for basis, column in sources.items() if column in frame}
    if not present:
        raise KeyError("none of the tier columns are present")

    ranks = pd.DataFrame({basis: frame[column].map(order)
                          for basis, column in present.items()}, index=frame.index)
    best = ranks.max(axis=1)
    fired = best.notna()

    out = frame.copy()
    out[tier_column] = pd.Series(
        [TIERS[int(v)] if pd.notna(v) else pd.NA for v in best],
        index=frame.index, dtype="string")
    hits = ranks.notna()
    names = pd.Series(
        ["+".join(sorted(b for b in present if hits.at[i, b])) if fired.at[i] else pd.NA
         for i in frame.index],
        index=frame.index, dtype="string")
    out[basis_column] = names.replace(
        {"+".join(sorted(present)): "both"} if len(present) > 1 else {})
    return out

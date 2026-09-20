"""How big is big: severity tiers expressed as return periods.

The output is a tier, and the tiers are return periods: how long you would
ordinarily wait to see a move this large in THIS instrument. A move worth
interrupting someone for and a move worth a line in the weekly note are both
"significant" against the same null hypothesis; what separates them is RARITY,
which is what the recipient actually reasons about. "The largest idiosyncratic
move in BTC since March 2023" needs no calibration intuition to read; a 1-to-100
importance score does.

Return periods also solve the comparison problem for free. A 1.5% day is
unremarkable for SOL and a once-a-year event for SHY; the same percentage
means different things per instrument, but "once a year" means the same thing
everywhere. That is the whole reason the hydrology and insurance literature
states extremes this way rather than in raw units.

THE RUNG IS A SIZE; THE MESSAGE IS A DATE. Two different questions, answered
separately - conflating them produces a message that contradicts itself.

  the rung   how big was this move, in the instrument's own terms - |r| over its
             own long-run sigma, against a threshold set per BLOCK. Monotone by
             construction: a bigger move can never be given a milder word.
  the date   when this instrument last moved at least this far, read straight off
             its record (see record_since). "The biggest since 3 March 2020" is a
             fact, not an estimate, and it is the half a reader actually uses.

WHAT THIS REPLACED, TWICE. The tiers were first return levels fitted by
peaks-over-threshold, which at this sample size could not answer the question:
the same instrument fitted on different six-year windows gave once-in-six-years
levels a factor of three apart, and the top rung fired 1.75x as often as its own
words promised.

They were then RANKS - "the biggest move in the trailing six years" - which is
exactly calibrated in frequency and, it turns out, wrong about severity. A rank
is relative to a window, so a move sits in the shadow of any bigger one still
inside it: after a crash, nothing can reach the top rung until that crash rolls
out, however violent the market gets. Measured on the record, 395 moves LARGER
than the typical `extreme` were reported as something milder - a 47x move in
Bitcoin Cash went out as `high`, digest-only, no push - and the dates were
March 2020, October 2008, the 2015 yuan devaluation. The rule demoted precisely
the episodes it exists for.

Size has neither failure. It is monotone, so the shadow cannot happen, and it
needs no tail fit, because sigma over five thousand bars is an ordinary standard
deviation rather than an extrapolation.

WHY THE THRESHOLD IS PER BLOCK. A flat threshold across the basket is a claim
that 5 sigma means the same thing in Solana and in utilities, and it does not:
measured, a flat ladder put crypto at 36-43 messages a year and XLU, XLRE and
XLB at about one, a 47x spread. Instruments in a block share a return SHAPE -
crypto is fat-tailed as a class, utilities are not - so setting the threshold per
block removes the systematic part of that difference and leaves the idiosyncratic
part standing. It brings the spread to about 7x: crypto to ~13 a year, the quiet
sector ETFs up to ~2.3, and the equity block still spans 6.5x internally, so XLU
and XLK are not flattened into each other.

Block SIZE is deliberately not used, and the measurement is why: the equity block
has the most members, sixteen, and the second-quietest assets - 70 messages a
year between them against crypto's 281 from nine. Dividing by headcount would
quiet the block that is already quiet and barely touch the one that floods.

CAUSALITY is unchanged and is now structural rather than maintained. A level is
the maximum over bars STRICTLY BEFORE the one being described, so no bar can be
labelled using knowledge of its own future. There is no refit schedule to get
wrong and no warm-up beyond the rung's own window.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# The ladder, in multiples of the instrument's own long-run sigma, per block.
# These are the recipient's categories, not the statistician's: how serious a
# move has to be before a person wants to hear about it. Everything rarer than
# the top tier is still the top tier - there is no fifth box, because at that
# point the message is the same.
#
# The values are a TRADER'S reading of what each word should mean, not a target
# alert count. They were set by the person who receives the messages, asked what
# he would expect each rung to mean for one instrument; the resulting rate per
# year is an output worth watching and has never been an input. Measured over
# the whole archive it is about every 2 months, 6 months, 17 months and 2.9
# years per instrument - roughly half the old wording's promise at the two quiet
# rungs and twice it at the two that interrupt. See docs/decisions.md: the
# frequency guarantee belonged to the rank rule this table replaced.
#
# THESE ARE PREFERENCES, NOT ESTIMATES, which is why they are written down rather
# than fitted. They were seeded from a measurement - the value that puts each
# block near ten messages per instrument-year at the shallowest rung - and then
# they stay put until a person moves them. A number that refits itself is a
# number nobody can reason about, and it was refitting that produced the two
# failures this file records above.
#
# The spacing between rungs is geometric and shared: each is about 1.5x the one
# below. What separates blocks is where the ladder STARTS, not how it climbs.
BLOCK_SIGMA: dict[str, tuple[float, float, float, float]] = {
    "crypto":            (7.2, 10.8, 15.9, 23.1),
    "FX":                (6.3,  9.4, 13.8, 20.0),
    "precious_metals":   (4.4,  6.6,  9.6, 14.0),
    "credit":            (4.3,  6.4,  9.4, 13.7),
    "energy":            (4.3,  6.4,  9.4, 13.7),
    "equity":            (4.2,  6.2,  9.1, 13.3),
    "rates":             (4.1,  6.2,  9.1, 13.2),
    "agriculture":       (3.8,  5.8,  8.4, 12.3),
    "industrial_metals": (3.8,  5.7,  8.4, 12.2),
}

# What an unlisted block falls back to.
DEFAULT_SIGMA: tuple[float, float, float, float] = (4.2, 6.2, 9.1, 13.3)

# And the ladder for a BLOCK'S OWN move, which is a different series and needs
# different numbers. A block move is the median across its members of each
# member's move over that member's own sigma - so it arrives standardised, but a
# median of eight FX pairs and a median of sixteen equity ETFs do not have the
# same shape at all: the equity median is the most diversified series in the
# basket and the quietest, the FX one moves when the dollar moves and is not.
#
# Giving blocks the member table was measured and wrong: at 9.1 for `major` the
# US equity complex fired 0.04 times a year - once in twenty-five years, which
# is silence - while FX ran at 1.9 and crypto at 1.7. The block channel exists
# because widening the blocks made every member's residual small on exactly the
# days the whole complex repriced, and a table that silences equity gives that
# back for the block a reader cares about most.
#
# Seeded the same way as the member table: the value putting each block near one
# `major` every two years and one `extreme` every seven.
# THE THIRD TABLE, and the one whose absence made a whole channel silent. The
# abnormal ladder scores z_resid_bmp, which is a BMP standardised residual - a
# t-statistic - and it was given the member table above. Those numbers were
# calibrated on |r| / sigma_lt, a raw return over its own sigma, and the two do
# not live on the same scale at all: measured over 2,595,077 bars the raw ratio
# reaches 112.9 and the t-statistic reaches 15.6. Standardising is precisely the
# operation that removes the fat tail, and the BMP correction deflates the
# statistic further exactly when the whole cross-section is moving, which is when
# the biggest moves happen.
#
# So the member table did not make the abnormal ladder strict, it made it
# IMPOSSIBLE. In seven blocks of nine the `major` rung sat above the highest
# value the statistic had ever taken, and in eight of nine `extreme` did: FX
# asked for 20.0 of a series that has never passed 10.9, credit for 9.4 of one
# that has never passed 7.2. Over the whole record the abnormal ladder produced
# 698 events and exactly ONE push.
#
# Seeded by rate rather than by eye: each rung is the quantile of that block's
# own |z_resid_bmp| that fires as often as the same rung on the absolute ladder,
# so "major" means the same rarity whichever question found it.
BLOCK_RESID_SIGMA: dict[str, tuple[float, float, float, float]] = {
    "crypto":            (5.1, 6.6, 7.7, 10.6),
    "FX":                (4.3, 5.3, 6.5,  7.3),
    "industrial_metals": (3.2, 4.4, 6.1,  6.6),
    "agriculture":       (3.4, 4.6, 5.6,  6.4),
    "energy":            (3.6, 4.6, 5.4,  6.4),
    "precious_metals":   (3.2, 4.2, 5.0,  6.1),
    "rates":             (3.1, 3.8, 4.6,  5.8),
    "equity":            (3.1, 3.8, 4.5,  5.2),
    "credit":            (3.2, 3.7, 4.0,  4.5),
}
DEFAULT_RESID_SIGMA = (3.2, 4.4, 5.4, 6.4)

BLOCK_MOVE_SIGMA: dict[str, tuple[float, float, float, float]] = {
    "crypto":            (7.1, 10.5, 15.5, 19.3),
    "FX":                (5.7,  8.4, 12.3, 14.7),
    "industrial_metals": (4.6,  6.8, 10.0, 18.0),
    "precious_metals":   (4.4,  6.5,  9.5, 10.8),
    "agriculture":       (4.0,  6.0,  8.8, 12.9),
    "rates":             (4.0,  5.8,  8.6, 12.0),
    "credit":            (3.6,  5.4,  7.9, 10.2),
    "energy":            (3.1,  4.6,  6.8,  8.4),
    "equity":            (2.9,  4.3,  6.3,  7.8),
}
DEFAULT_BLOCK_MOVE_SIGMA: tuple[float, float, float, float] = (4.0, 5.8, 8.6, 12.0)

TIERS: tuple[str, ...] = ("noticeable", "high", "major", "extreme")

# How far back the message may claim a record. Beyond it the archive is trimmed
# on a warm run, so "the biggest since" would be a statement about the slice
# rather than about the instrument - the message says "in at least six years"
# there instead, which is what the data actually supports.
RECORD_HORIZON_DAYS = 2191.5

HOURS_PER_DAY = 24.0
SECONDS_PER_DAY = 86400.0


# Which of the three scores a ladder is being asked about. A name rather than a
# flag because there are three, and because every one of them was added after
# something was measured firing at the wrong rate - or, in the residual's case,
# at no rate at all. Passing the wrong one is silent: the levels come out, the
# tiers come out, and only the count over twenty years says anything is wrong.
MEMBER = "member"          # |r| / sigma_lt, one instrument's raw move
BLOCK_OWN = "block"        # a block's own median series, already standardised
RESIDUAL = "residual"      # z_resid_bmp, the market-adjusted t-statistic


def tier_sigma(block: str | None = None, ladder: str = MEMBER) -> dict[str, float]:
    """The rungs for this block on this ladder, after the sensitivity knob.

    The three tables are not interchangeable and cannot be made so - see
    BLOCK_RESID_SIGMA and BLOCK_MOVE_SIGMA for what each one is measured against
    and what happened when they shared numbers.
    """
    from tremor.basket import load_tuning

    tuning = load_tuning()
    if ladder == BLOCK_OWN:
        rungs = BLOCK_MOVE_SIGMA.get(str(block), DEFAULT_BLOCK_MOVE_SIGMA)
    elif ladder == RESIDUAL:
        rungs = BLOCK_RESID_SIGMA.get(str(block), DEFAULT_RESID_SIGMA)
    elif ladder == MEMBER:
        rungs = tuning.sigma_for(block) if block else DEFAULT_SIGMA
    else:
        raise ValueError(f"unknown ladder {ladder!r}")
    return {name: value * tuning.sensitivity
            for name, value in zip(TIERS, rungs)}


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


SECONDS_PER_DAY = 86400.0


def sigma_levels(scale: "pd.Series | None", index, block: str | None = None,
                 ladder: str = MEMBER) -> pd.DataFrame:
    """The four levels a bar must clear, in the units its score is already in.

    `scale` is the instrument's own long-run sigma where the score is a RAW
    quantity (the return), and None where the score is already standardised (the
    BMP residual t-statistic, which is in sigma units by construction). Passing
    sigma for something already divided by it would square the normalisation and
    make every quiet hour look enormous.

    No lookback, no window, no warm-up beyond whatever sigma itself needs. A
    bigger move gets a rung at least as deep as a smaller one, always, which is
    the property the rank rule could not offer: there is nothing here for a move
    to sit in the shadow of.
    """
    levels = tier_sigma(block, ladder)
    frame = pd.DataFrame(index=index)
    for name in TIERS:
        if scale is None:
            frame[name] = float(levels[name])
        else:
            usual = pd.to_numeric(scale, errors="coerce").to_numpy(dtype="float64")
            usual = np.where(np.isfinite(usual) & (usual > 0), usual, np.nan)
            frame[name] = levels[name] * usual
    return frame


def record_since(score: pd.Series, hour_utc: pd.Series,
                 two_sided: bool = True) -> pd.Series:
    """For each bar, the hour of the last bar that was at least as big.

    This is the whole message: the difference between a bar's own hour and this
    one is how far back you must go to find a move to match it, which is what
    "the biggest since 3 March 2020" means. NA where there is no such bar - the
    move is the largest in the whole record - and the caller says so in words
    rather than inventing a date.

    A monotonic stack, so it costs one pass. The stack holds exactly the bars
    still visible from the present - those with nothing at least as large
    between them and now - and every bar is pushed and popped at most once.
    Computing it as four separate lookbacks would be four passes and would still
    only answer to the nearest rung.
    """
    magnitude = magnitudes(score, two_sided).to_numpy(dtype="float64")
    hours = np.asarray(hour_utc, dtype="int64")
    out = np.full(len(magnitude), -1, dtype="int64")
    stack: list[int] = []
    for i, value in enumerate(magnitude):
        if not np.isfinite(value):
            continue
        while stack and magnitude[stack[-1]] < value:
            stack.pop()
        if stack:
            out[i] = hours[stack[-1]]
        stack.append(i)
    return pd.Series(pd.array(np.where(out >= 0, out, None), dtype="Int64"),
                     index=score.index)


def assign(score: pd.Series, levels: pd.DataFrame,
           two_sided: bool = True) -> pd.Series:
    """The tier of each bar: the rarest level it clears, or NA for none.

    NA covers both "quieter than the noticeable level" and "no level was fitted
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
    """Tier as an integer, 1 for noticeable up to 4, so it can be compared and maxed."""
    order = {name: i + 1 for i, name in enumerate(TIERS)}
    return tier.map(order).astype("Int64")


LEVEL_PREFIX = "level"
LEVEL_COLUMNS: tuple[str, ...] = tuple(f"{LEVEL_PREFIX}_{name}" for name in TIERS)


def level_columns(prefix: str = LEVEL_PREFIX) -> tuple[str, ...]:
    return tuple(f"{prefix}_{name}" for name in TIERS)


def annotate(frame: pd.DataFrame, column: str = "z_resid_bmp",
             prefix: str = LEVEL_PREFIX, tier_column: str = "tier",
             fallback: str | None = "z_resid",
             two_sided: bool = True,
             scale_column: "str | None" = None,
             block: "str | None" = None,
             ladder: str = MEMBER,
             levels: "pd.DataFrame | None" = None) -> pd.DataFrame:
    """Adds the four levels, the resulting tier, and what the move beat.

    The column is a parameter because the same question - how big is this, for
    this instrument - is worth asking of more than one quantity. Asked of the
    residual it means "the market did not explain this"; asked of the raw return
    it means "this was a big move". Those are different events and both are
    wanted, which is why nothing here is specific to either.

    `scale_column` is what makes that work without a second ladder. A raw return
    is in price units and has to be divided by the instrument's own long-run
    sigma before any threshold can mean anything; the BMP residual is already a
    t-statistic and must NOT be, or the normalisation is applied twice and every
    quiet hour looks enormous. So the caller names the divisor, or names none.

    `block` picks which rung set to use - see BLOCK_SIGMA and the note above it
    on why the block and not the instrument.

    `levels` short-circuits the computation for a caller that already holds the
    answer. Nothing is cached between runs: a level is a threshold times a sigma
    the frame already carries, which is one multiplication.

    RECORD_SINCE RIDES ALONG because it costs one pass and the message cannot be
    written without it. The tier says how big the move was; this says when the
    instrument last went that far, which is the half the reader uses.
    """
    if column in frame:
        score = frame[column]
    elif fallback is not None and fallback in frame:
        score = frame[fallback]
    else:
        raise KeyError(f"{column!r} not in frame and no usable fallback")

    hours = frame["hour_utc"] if "hour_utc" in frame else None
    if levels is None:
        scale = frame[scale_column] if scale_column and scale_column in frame \
            else None
        if scale_column and scale is None:
            # Named a divisor the frame does not carry. Silently scoring the raw
            # quantity against a sigma threshold would call every bar extreme.
            raise KeyError(f"{scale_column!r} is needed to scale {column!r}")
        levels = sigma_levels(scale, frame.index, block, ladder)
    out = frame.copy()
    for name in TIERS:
        out[f"{prefix}_{name}"] = levels[name].to_numpy()
    out[tier_column] = assign(score, levels, two_sided).to_numpy()
    if hours is not None:
        out[f"{prefix}_since"] = record_since(score, hours, two_sided).to_numpy()
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
    series answers "did it hold" for that event (see tremor.persistence), and it
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
    # Both columns are built by indexing arrays rather than by looping over rows.
    # The row loop this replaces was the second-largest cost in the run after the
    # rolling MAD: two scalar .at[] lookups per row per basis, forty thousand
    # bars per instrument, to choose between four possible answers.
    codes = best.to_numpy(dtype="float64")
    known = np.isfinite(codes)
    tiers = np.full(len(frame), None, dtype=object)
    tiers[known] = np.array(TIERS, dtype=object)[codes[known].astype(int)]
    out[tier_column] = pd.array(tiers, dtype="string")

    hits = ranks.notna()
    # One name per combination of channels, looked up by the bit pattern of which
    # ones fired. With two channels that is four rows in the table, not forty
    # thousand string joins.
    order_of = sorted(present)
    bits = np.zeros(len(frame), dtype=int)
    for position, basis in enumerate(order_of):
        bits |= hits[basis].to_numpy(dtype=bool) << position
    every = "+".join(order_of)
    lookup = np.array(
        ["+".join(name for position, name in enumerate(order_of) if pattern >> position & 1)
         for pattern in range(1 << len(order_of))], dtype=object)
    if len(present) > 1:
        lookup[lookup == every] = "both"
    names = np.full(len(frame), None, dtype=object)
    chosen = fired.to_numpy(dtype=bool)
    names[chosen] = lookup[bits[chosen]]
    out[basis_column] = pd.array(names, dtype="string")
    return out

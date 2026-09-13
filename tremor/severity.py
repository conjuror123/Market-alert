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

THE RULE IS A RECORD, NOT A FIT, and that is a correction rather than a
simplification. A bar's tier is decided by ONE question asked of the instrument's
own past: how far back must you go to find a move at least this big? Six years
and it is `extreme`, three and it is `major`, and so on down. Nothing is
estimated, nothing is extrapolated, and the message writes itself - "the biggest
move in SPY since 3 March 2020" is a fact about the record rather than a claim
about a distribution.

WHAT THIS REPLACED AND WHY. The tiers used to be return levels fitted by
peaks-over-threshold: a Generalised Pareto tail above a high threshold,
extrapolated out to the rung (Coles 2001 ch. 4; Hosking & Wallis 1987 for the
probability-weighted moments). That is the standard method and it was correctly
implemented. It was also being asked a question it cannot answer at this sample
size. Measured on this archive: fitting the SAME instrument with the SAME code
on different six-year windows, the estimated once-in-six-years level for SPY
ranges from 2.22% to 6.78% - a factor of three, and a factor of four for XLF and
IWM - purely according to which six years the window happened to contain. The
result was printed as a bare phrase with no interval on it, and it was wrong in
a consistent direction: the top rung fired 1.75 times as often as its own words
promised.

WHY A RECORD IS EXACTLY CALIBRATED, which is the argument for the whole change.
For ANY distribution whatever, stationary or not, the probability that the
newest of N observations is the largest of those N is exactly 1/N. So "the
largest in the trailing six years" happens on average once every six years by
construction - not because a model was fitted well, but because there is no
model to fit. Measured against the archive the record rule delivers 1.21 per six
years where the fitted ladder delivered 1.75, and the residual excess is
volatility clustering rather than bias: 29 records against 23.8 expected is
within Poisson noise of exact.

It also makes overclaiming structurally impossible. An instrument cannot be "the
biggest in six years" until it has six years, because the answer is a lookback
into a record that does not exist yet. The old code needed an explicit
EXTRAPOLATION_LIMIT to stop the fit promising more than the data could support;
that rule is now the shape of the arithmetic and cannot be got wrong.

WHAT IS LOST. A record is coarser than a fitted level - it says a move beat
everything in six years but not by how far - so ordering two moves inside one
tier needs the magnitude alongside, which saed already carries. And records
cluster: a crisis produces several in a week where a fitted level would have
spread them. That is a true property of markets rather than an artefact, and
the reader is better served seeing the cluster.

CAUSALITY is unchanged and is now structural rather than maintained. A level is
the maximum over bars STRICTLY BEFORE the one being described, so no bar can be
labelled using knowledge of its own future. There is no refit schedule to get
wrong and no warm-up beyond the rung's own window.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# The ladder, in calendar days. These are the recipient's categories, not the
# statistician's: how often a person is willing to hear from the system at each
# level of seriousness. Everything rarer than the top tier is still the top
# tier - there is no fifth box, because at that point the message is the same.
#
# The values are a TRADER'S reading of what each word should mean, not a target
# alert count. They were set by the person who receives the messages, asked what
# he would expect each rung to mean for one instrument; the resulting rate per
# year is an output worth watching and has never been an input.
#
# WHY THE TOP RUNG IS SIX YEARS. Because six is inside the five-to-ten the
# recipient asked for, and for no other reason.
#
# It used to be chosen differently and the difference matters. The old note here
# argued for six because seven "would silence the top rung for eighteen
# instruments at once" - the boundary was set to fit the shape of the archive,
# so the meaning of the word depended on how much history had been downloaded,
# and deepening an instrument would have quietly renamed moves that had already
# been sent. A rung is a statement about what the reader wants to hear, and it
# must not be a statement about what happens to be on disk. An instrument that
# has not lived six years simply cannot claim the rung - that is now arithmetic
# rather than policy - and the honest answer there is silence at that level, not
# a redefinition of the level for everybody else.
TIER_DAYS: dict[str, float] = {
    "noticeable": 30.0,    # a month       - digest only
    "high": 105.0,         # three months and a half - digest only
    "major": 1095.75,      # three years   - pushed
    "extreme": 2191.5,     # six years     - pushed
}
TIERS: tuple[str, ...] = tuple(TIER_DAYS)

HOURS_PER_DAY = 24.0


def tier_days() -> dict[str, float]:
    """The rungs as they currently stand, after the sensitivity knob.

    TIER_DAYS above is the BASE - what the words mean at sensitivity 1.0 - and
    this is what everything downstream must read. `sensitivity` in
    config/basket.yaml scales all four together: 2.0 makes every rung twice as
    rare and the messages roughly half as many, 0.5 the other way.

    Scaling them TOGETHER is the point. Sensitivity is one question - how rare
    before I want to know - and answering it must not silently re-rank a move
    from `major` to `high`, which is what moving one rung alone would do. It is
    also not a per-instrument dial and must never become one: each instrument is
    still judged against its own history, so SHY may speak once a year and SOL a
    hundred times, and flattening that would throw away the only thing a return
    period buys.
    """
    from tremor.basket import load_tuning

    scale = load_tuning().sensitivity
    return {name: days * scale for name, days in TIER_DAYS.items()}


def period_phrase(days: float) -> str:
    """A return period as a person says it.

    Derived from the number rather than written beside it. The rungs move - a
    trader retuned them once and the knob moves them again - and a hard-coded
    "about once a fortnight" survives that silently, which turns every message
    into a lie about a number the reader cannot check.
    """
    if days < 10.5:
        return "about once a week"
    if days < 18:
        return "about once in 2 weeks"
    if days < 45:
        return "about once a month"
    if days < 75:
        return "about once in 2 months"
    if days < 135:
        return "about once a quarter"
    if days < 270:
        return "about once in 6 months"
    if days < 550:
        return "about once a year"
    years = round(days / 365.25)
    return f"about once in {years} years"

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


def rolling_levels(score: pd.Series, rate: float | None = None,
                   hour_utc: pd.Series | None = None,
                   two_sided: bool = True) -> pd.DataFrame:
    """Per-bar tier levels: the bar to beat, over each rung's own lookback.

    A rung's level at bar i is the largest magnitude among the bars STRICTLY
    BEFORE i and within that rung's window - so clearing it means exactly "this
    is the biggest move since at least that far back", which is the sentence the
    message prints. assign() below is unchanged by the switch away from a fitted
    tail, because a record is still expressed as a level to clear.

    THE WINDOWS ARE IN CALENDAR TIME, not in bars, and that is not a detail. The
    rungs are what a person means by six years, and an instrument's bars per year
    change with its venue's hours, its holidays, and whether it trades at all at
    the weekend. Counting bars would make the same rung mean six years in one
    instrument and four in another.

    A rung is NaN until the instrument has lived that long, which is what stops
    a two-year-old listing announcing the biggest move in six years. The old code
    needed a rule for that; here it falls out, because the answer is a lookback
    into a record that does not exist yet. Downstream treats NaN as "no tier",
    never as "no event" - the two differ, and the distinction is what lets the
    evaluation say why an instrument was silent rather than guessing.

    The rungs come out non-decreasing for free, the windows being nested: the
    largest move in six years is at least the largest in three. The old fit had
    to impose that by hand, because an extrapolated level and an empirical
    quantile had nothing keeping them in order.

    `rate` is accepted and ignored. It described bars per calendar hour, which a
    fit over a bar count needed and a calendar window does not; it stays in the
    signature because callers pass it positionally, and removing it would be a
    silent argument shift rather than an error. Without `hour_utc` there is no
    calendar to window on, and the levels are all NaN - no tier rather than a
    guessed one.
    """
    magnitude = magnitudes(score, two_sided)
    frame = pd.DataFrame({name: np.full(len(magnitude), np.nan) for name in TIERS},
                         index=score.index)
    if hour_utc is None or len(magnitude) == 0:
        return frame

    hours = np.asarray(hour_utc, dtype="float64")
    stamped = pd.Series(magnitude.to_numpy(dtype="float64"),
                        index=pd.to_datetime(hours, unit="s"))
    lived = hours - hours[0]
    for name, days in tier_days().items():
        # closed="left" is what makes the level a statement about the PAST: it
        # takes the window's left edge and drops its right, so the current bar
        # is never part of the record it is being measured against.
        window = stamped.rolling(f"{int(days * SECONDS_PER_DAY)}s",
                                 closed="left").max().to_numpy()
        frame[name] = np.where(lived >= days * SECONDS_PER_DAY, window, np.nan)
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
             levels: "pd.DataFrame | None" = None) -> pd.DataFrame:
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

    `levels` short-circuits the computation for a caller that already holds the
    answer - blocks, which score a series built elsewhere. It is no longer a
    cache: a record level moves with every bar rather than standing for a month,
    so there is nothing to remember between runs. What made a trailing slice of
    history enough is now simply that the slice covers the deepest rung's
    lookback, which saed checks outright.

    RECORD_SINCE RIDES ALONG because it costs one pass and the message cannot be
    written without it: the tier says which rung was cleared, and this says what
    the move was actually bigger than.
    """
    if column in frame:
        score = frame[column]
    elif fallback is not None and fallback in frame:
        score = frame[fallback]
    else:
        raise KeyError(f"{column!r} not in frame and no usable fallback")

    hours = frame["hour_utc"] if "hour_utc" in frame else None
    if levels is None:
        levels = rolling_levels(score, hour_utc=hours, two_sided=two_sided)
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

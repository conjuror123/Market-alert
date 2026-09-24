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
# the whole archive it is about every 2 months, 5 months, 14 months and 2.3
# years per instrument. See docs/decisions.md: the frequency guarantee belonged
# to the rank rule this table replaced.
#
# WHAT THE FIRST COLUMN ACTUALLY DECIDES IS THE LENGTH OF THE WEEKLY NOTE. The
# digest carries noticeable and high, 6.7 rows a week across two notes - about
# 3.3 rows a note - and THREE QUARTERS OF THEM ARE `noticeable`. So the shallowest
# rung sets how much there is to read on a Saturday almost by itself, and the
# three above it only decide which of those rows carries which word. That is the
# question to ask when moving the first column, because it is the one a person
# can answer by reading a note: too much, too little, about right. "Ten messages
# per instrument-year" is how the column was seeded, not what it is for.
#
# THESE ARE PREFERENCES, NOT ESTIMATES, which is why they are written down rather
# than fitted. They stay put until a person moves them. A number that refits
# itself is a number nobody can reason about, and it was refitting that produced
# the two failures this file records above.
#
# THE SPACING IS A FREQUENCY, NOT A SIZE, and that is the correction. The rungs
# used to climb a uniform 1.47x in size in every block, and how much RARER that
# made a rung depended on the block's tail, which differs: measured over the
# archive the tail exponent runs 2.75 in credit to 3.71 in energy, so one step
# was 2.96x rarer in credit and 4.64x in energy. It compounded - among the ten
# instruments with at least ten top-rung events, enough for the ratio to mean
# anything, `extreme` cost 2.5 `noticeable` events on EMB and 18.3 on USD/CAD,
# 7.2x apart, and the tail exponent predicted which (correlation +0.83).
#
# So the frequency step is what is held constant: EACH RUNG IS 3.162x RARER THAN
# THE ONE BELOW - half a magnitude unit, the Gutenberg-Richter construction - and
# the size that delivers that is read off each block's own history. Realised, the
# steps now sit between 2.88x and 3.29x across all nine blocks, against 2.96x to
# 7.00x before.
#
# The bottom rung is UNCHANGED from the hand-set table. It is the one rung a
# reader can check, because three quarters of the weekly note's rows are
# `noticeable` and it therefore sets how long the note is; everything above it is
# derived from it. See tools/ladder.py, which prints this table and the two below
# it, and docs/decisions.md for why a quantile beats extrapolating from the
# exponent.
#
# WHAT IT ACTUALLY BOUGHT, because the next reader will measure and should not
# conclude the table is broken. Two cold passes over the whole archive, the old
# table against this one:
#
#   per block, pooled             5.5 .. 44.5  ->  11.8 .. 17.1    8.2x -> 1.4x
#   does the block's tail predict it   +0.67   ->        +0.01
#   per instrument, >=10 extreme   2.5 .. 18.3 ->   3.6 .. 14.1    7.2x -> 3.9x
#   names that never reach the top rung    5   ->            1
#
# THAT RESTRICTION ON THE THIRD LINE IS LOAD-BEARING, and an earlier version of
# this comment lost it - which is how it came to publish "4.7 to 19.6, 4.2x
# apart", two endpoints taken from a different set than the one they were
# compared against. The median instrument records SIX top-rung events in
# twenty-three years, so its noticeable-per-extreme ratio has a denominator of
# six: across all sixty names that reach the rung at all it runs 3.6 to 50.3, and
# that 14.0x is mostly counting error rather than disagreement about what the
# word means - the spread falls monotonically as the minimum count rises, which
# is the signature. Quote the pooled line, or name the restriction.
#
# Every other published number held: 7.6 to 11.7 events per instrument-year (was
# 8.1 to 12.2), 3.8x between the quietest and loudest name, 94.4% of the obvious
# hours reached, 74.3% still standing at the next close.
#
# BUT THE 3.162x IS TRUE OF CROSSINGS, NOT OF DELIVERED EVENTS, and the gap is
# real: pooled, the event counts step 2.65x, 2.47x and 1.95x. The rungs are spaced
# correctly on the series they were derived against; a delivered event is a
# different object, because `combine` takes the MAX of the absolute and abnormal
# tiers - the maximum of two evenly spaced ladders is not evenly spaced, it
# concentrates upward - and the once-a-day rule then keeps each day's PEAK tier
# and concentrates it again. Closing that gap means deriving against event rates,
# which is an iteration rather than a formula. It is open, in
# docs/concerns-for-later.md.
BLOCK_SIGMA: dict[str, tuple[float, float, float, float]] = {
    "crypto":            (7.2, 9.9, 13.2, 18.3),
    "FX":                (6.3, 8.6, 11.1, 15.1),
    "precious_metals":   (4.4, 6.1,  7.8,  9.9),
    "credit":            (4.3, 6.5,  9.6, 13.5),
    "energy":            (4.3, 5.8,  7.9, 10.4),
    "equity":            (4.2, 5.7,  7.5, 10.2),
    "rates":             (4.1, 5.7,  7.7, 10.5),
    "agriculture":       (3.8, 5.5,  7.6, 10.6),
    "industrial_metals": (3.8, 5.5,  8.0, 10.0),
}

# What an unlisted block falls back to: the element-wise median of the nine
# above, a typical ladder rather than an extrapolation from a tail nobody has.
DEFAULT_SIGMA: tuple[float, float, float, float] = (4.3, 5.8, 7.9, 10.5)

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
# Anchored on `major`, NOT on the bottom rung, because the bottom rung cannot
# fire: blocks.BLOCK_TIERS drops a block at `noticeable`, so that rung computes a
# tier and produces nothing, and anchoring on it would be anchoring on nothing.
# `major` is held where it was - about 3.7 block events a year - and the rest is
# derived from it at the same 3.162x step the member table uses.
#
# `high` DOES fire now, into the note rather than onto the phone, so its rung has
# stopped being invisible: it is worth about 7.8 block rows a year and a reader
# can see each one. It is still derived rather than held, which is the honest
# state of it - nobody has yet read a season of notes and said whether that rung
# lands where a block row should start.
#
# AND IT DIVIDES BY A DIFFERENT SIGMA, which is easy to miss and silently wrong
# if it is. The member table scores |r| / sigma_LT, the long-run yardstick. This
# series is a median of r / sigma_eff - saed builds its sigma panel from the
# `sigma_eff` column - so it runs hotter in a calm stretch. Deriving it against
# sigma_lt by mistake put three blocks' top rung above anything their series had
# ever reached, while the live run has those blocks firing at `extreme` four
# times each.
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
    "crypto":            (5.3, 6.5, 7.5, 9.5),
    "FX":                (4.4, 5.1, 5.9, 6.9),
    "industrial_metals": (3.3, 4.4, 5.7, 6.9),
    "agriculture":       (3.4, 4.5, 5.5, 6.3),
    "energy":            (3.8, 4.7, 5.5, 6.3),
    "precious_metals":   (3.4, 4.2, 4.8, 5.7),
    "rates":             (3.3, 3.9, 4.7, 5.6),
    "equity":            (3.2, 3.9, 4.5, 5.2),
    "credit":            (3.3, 4.0, 4.7, 5.6),
}
DEFAULT_RESID_SIGMA = (3.4, 4.4, 5.5, 6.3)

BLOCK_MOVE_SIGMA: dict[str, tuple[float, float, float, float]] = {
    "crypto":            (8.1, 11.6, 15.5, 18.7),
    "FX":                (6.3,  9.2, 12.3, 14.3),
    "industrial_metals": (5.1,  7.2, 10.0, 16.3),
    "precious_metals":   (5.9,  7.5,  9.5, 10.8),
    "agriculture":       (4.0,  5.5,  8.8, 12.2),
    "rates":             (4.7,  6.5,  8.6, 11.3),
    "credit":            (3.7,  5.0,  7.9,  9.9),
    "energy":            (4.7,  6.1,  6.8,  7.8),
    "equity":            (3.6,  4.7,  6.3,  7.7),
}
DEFAULT_BLOCK_MOVE_SIGMA: tuple[float, float, float, float] = (4.7, 6.5, 8.8, 11.3)

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


def rungs_for(table: dict, block: "str | None", default: tuple,
              table_name: str) -> tuple:
    """This block's rungs from one ladder table, or the default for no block.

    A NAMED block absent from the table used to take the default silently. That
    is the worst shape a failure can have here: the levels come out, the tiers
    come out, every message reads normally, and only a count over twenty years
    says one complex has been calibrated on another's tail. BLOCKS, basket.yaml
    and the three tables are four halves of one thing, and this is what catches
    them coming apart - which is exactly what happens when a block is added and
    one of the tables is forgotten.

    `None` still takes the default, because "no block was named" is a different
    statement from "a block I have never heard of". Nothing on the live path
    passes it: saed resolves every asset_id through basket.instruments, which is
    assets + outside, so every scored instrument carries a real block.
    """
    if block is None:
        return default
    name = str(block)
    if name not in table:
        raise KeyError(
            f"block {name!r} has no rungs in severity.{table_name}. Every block "
            f"in tremor.basket.BLOCKS needs an entry in all three ladder tables "
            f"(BLOCK_SIGMA, BLOCK_RESID_SIGMA, BLOCK_MOVE_SIGMA); derive them "
            f"with tools/ladder.py rather than writing them by hand.")
    return table[name]


def tier_sigma(block: str | None = None, ladder: str = MEMBER) -> dict[str, float]:
    """The rungs for this block on this ladder, after the sensitivity knob.

    The three tables are not interchangeable and cannot be made so - see
    BLOCK_RESID_SIGMA and BLOCK_MOVE_SIGMA for what each one is measured against
    and what happened when they shared numbers.
    """
    from tremor.basket import load_tuning

    tuning = load_tuning()
    if ladder == BLOCK_OWN:
        rungs = rungs_for(BLOCK_MOVE_SIGMA, block, DEFAULT_BLOCK_MOVE_SIGMA,
                          "BLOCK_MOVE_SIGMA")
    elif ladder == RESIDUAL:
        rungs = rungs_for(BLOCK_RESID_SIGMA, block, DEFAULT_RESID_SIGMA,
                          "BLOCK_RESID_SIGMA")
    elif ladder == MEMBER:
        rungs = tuning.sigma_for(block) if block else DEFAULT_SIGMA
    else:
        raise ValueError(f"unknown ladder {ladder!r}")
    if len(rungs) != len(TIERS):
        # zip() would truncate here and hand back a short dict, and the caller
        # would either KeyError deep inside sigma_levels or - worse - quietly
        # lose the top rung. A table and the names it is zipped against are two
        # halves of one thing; say so rather than limp on.
        raise ValueError(
            f"{ladder} ladder for block {block!r}: {len(rungs)} rungs against "
            f"{len(TIERS)} tiers {TIERS}")
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


class RecordState:
    """What one series' record lookup carries from one run to the next.

    WHY THIS EXISTS. "The biggest since March 2020" is the last bar at least
    this big, and finding it used to mean holding six years of bars in every
    warm run - the whole reason a warm events slice was warm-up PLUS six years.
    But the answer for every future bar depends on only a handful of past bars:
    the ones nothing at least as big has come after since. That is the monotonic
    stack record_since already keeps, and at any moment it is a few dozen
    entries even over twenty years. So a run hands its stack, as of a checkpoint
    hour, to the next run (tremor.saed's record book), and the next run needs
    only the bars after the checkpoint - not six years of them.

      seed         the stack as of `after`, oldest first: (hour, magnitude)
      after        bars at or before this hour are already folded into `seed`
                   and are not read again; None means start from nothing
      snapshot_at  the hour to take the stack at for the next run
      snapshot     filled in: the stack as of `snapshot_at`
    """

    def __init__(self, seed=(), after: "int | None" = None,
                 snapshot_at: "int | None" = None):
        self.seed = [(int(h), float(m)) for h, m in seed]
        self.after = after
        self.snapshot_at = snapshot_at
        self.snapshot: "list[tuple[int, float]] | None" = None


def record_since(score: pd.Series, hour_utc: pd.Series,
                 two_sided: bool = True,
                 state: "RecordState | None" = None) -> pd.Series:
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

    With a `state`, the stack starts from the one a previous run saved, bars it
    already covers are skipped (their answer is NA here - they are not the
    bars this run is answering for), and the stack is snapshotted for the next
    run. See RecordState.
    """
    magnitude = magnitudes(score, two_sided).to_numpy(dtype="float64")
    hours = np.asarray(hour_utc, dtype="int64")
    out = np.full(len(magnitude), -1, dtype="int64")
    stack: list[tuple[int, float]] = list(state.seed) if state is not None else []
    after = state.after if state is not None else None
    snap_at = state.snapshot_at if state is not None else None
    snapped = False
    for i, value in enumerate(magnitude):
        if snap_at is not None and not snapped and hours[i] > snap_at:
            state.snapshot, snapped = list(stack), True
        if not np.isfinite(value) or (after is not None and hours[i] <= after):
            continue
        while stack and stack[-1][1] < value:
            stack.pop()
        if stack:
            out[i] = stack[-1][0]
        stack.append((int(hours[i]), float(value)))
    if state is not None and snap_at is not None and not snapped:
        state.snapshot = list(stack)
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
             levels: "pd.DataFrame | None" = None,
             record_state: "RecordState | None" = None) -> pd.DataFrame:
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
        out[f"{prefix}_since"] = record_since(score, hours, two_sided,
                                              record_state).to_numpy()
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

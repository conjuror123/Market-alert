"""Single-asset event detection, SAED.

Catches moves an instrument's own peers do not explain, and moves that are large
for the instrument whatever the peers did. It builds the block factors it needs
from the per-asset metrics itself, so it depends on nothing `cross_section`
writes to disk.

Three rules worth knowing before changing anything here.

AN INSTRUMENT SPEAKS ONCE PER TRADING DAY, and the day is its own: the exchange's
local day for a listed fund, the UTC day for everything else. A later firing the
same day does not open a second event and does not cancel the first - it merges
into it, raising the tier if it is rarer and incrementing repeat_count. "The move
stopped" and "the move continues, and we have already reported it" are different
things.

BLOCKS ARE THEIR OWN EVENTS. A block is ranked on its own median series against
its own ladder, one alert per block per day. Three instruments of one block
jerking in the same hour is one observation about the block, not three messages.

VERSIONING IS STAMPED HERE, on the table this module writes: config_version and
run_version go on every row.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from tremor import (atomic, blocks, cross_section, gaps, persistence, quality,
                    routing, sessions, severity, windows)
from tremor.basket import Asset, Basket, load_tuning


# The two questions worth asking of one instrument's hour, and the column each is
# asked of. "Was this explained by its peers" and "was this a big move" are
# different events, and a detector that asks only the first is blind to exactly
# the days when everything moves together - which is what a macro event is. On
# the SVB collapse, the August 2024 yen unwind and the 2024 US election the
# abnormal channel finds nothing at all; the absolute channel finds 11, 17 and 9.
#
# The absolute channel is also the only one of the two that breathes with the
# market: month to month it varies 33x against the abnormal channel's 1.8x,
# because the abnormal score has the market's volatility divided out of it twice
# - once by the instrument's own rolling sigma and once by the peer spread (BMP).
#
# It reads the RAW return, not the winsorized one: winsorizing caps the series at
# 0.096 where the raw reaches 0.199, which is precisely the population this
# channel exists to find.
TIER_SOURCES = {"abnormal": "tier_abnormal", "absolute": "tier_absolute"}
ABSOLUTE_COLUMN = "r"
ABSOLUTE_LEVEL_PREFIX = "abs_level"


@dataclass(frozen=True)
class SaedEvent:
    event_id: str
    asset_id: str
    block: str
    hour_utc: int          # T0_single - the hour of the first firing
    peak_hour_utc: int     # the hour the event reached the tier it is reported at
    # Whether the non-parametric rank test agrees that the bar is extreme. A
    # separate axis from `basis`, which says which channel CLAIMED the event:
    # this says whether a test sharing none of their assumptions concurs.
    rank_confirms: "bool | None"
    # Whether the residual mean-reverts fast enough for the move to be read as
    # idiosyncratic rather than as a factor the model does not have.
    ou_reverts: "bool | None"
    z_resid: float
    e_resid: float
    # The move, split into the two things it can be, summing back to r exactly
    # (see tremor.residuals). Carried onto the event because the message shows
    # them and the factor series they come from live only in the residual step:
    # "of that move, this much was its block moving and this much was the
    # instrument itself".
    co_block: float
    r: float
    beta_block: float
    repeat_count: int
    tier: str
    basis: str
    # The instrument's usual hour at the time of the event - sigma_LT, the
    # rolling standard deviation of its own returns over the previous 5000 bars,
    # so causal like everything else. Carried onto the event purely so the
    # message can say what the move was BIG COMPARED WITH: "+0.13%, biggest in
    # three years" is unreadable on its own and reads as a bug, and 45% of
    # pushes show a number under 1%.
    sigma_lt: float
    # The price at the end of the bar that earned the tier. Carried purely so the
    # message can say where the instrument actually IS: a percentage with no
    # level behind it makes a reader open a chart to place it.
    close: float
    # The hour of the last bar that was at least this big, on the SAME ladder
    # that claimed the event - which is the message. The tier says which rung was
    # cleared and this says what the move actually beat, so "biggest since 3
    # March 2020" is read off the record rather than inferred from a rung. NA
    # where nothing in the archive matches it: the move is the largest on record,
    # and the message says so rather than inventing a date.
    record_since: "int | None"
    # The overnight gap claimed this event rather than any hour's move: `r` is
    # then the gap as a price move, `sigma_lt` the fund's usual gap and the
    # record date the last gap this big. See tremor.gaps.
    overnight: bool = False


# "work the instrument's own day out from its session template", as distinct
# from a caller passing None to mean the UTC day. Only the tests pass one
# explicitly; the pipeline always wants the instrument's own answer.
_DERIVE = object()


def _opt_int(value) -> "int | None":
    """An Int64 cell as a plain int, or None where it is NA."""
    return None if value is None or pd.isna(value) else int(value)


def _record_since(frame: pd.DataFrame, basis) -> np.ndarray:
    """Per bar, the lookback belonging to the ladder that claimed it.

    The two ladders date a move differently and only one of them is true of a
    given event. The absolute one asks when the raw move was last matched; the
    abnormal one asks when the move LEFT AFTER the block was last matched, which
    can be years apart - an instrument carried by its sector may have had bigger
    hours last week and still never have moved this far on its own.
    """
    abnormal = frame.get(f"{severity.LEVEL_PREFIX}_since")
    absolute = frame.get(f"{ABSOLUTE_LEVEL_PREFIX}_since")
    if abnormal is None and absolute is None:
        return np.full(len(frame), None, dtype=object)
    blank = pd.Series(pd.array([pd.NA] * len(frame), dtype="Int64"),
                      index=frame.index)
    abnormal = blank if abnormal is None else abnormal
    absolute = blank if absolute is None else absolute
    # The choice has to match where the delivery layer PUTS the phrase, or the
    # message is a false statement rather than an ugly one: it prints the date
    # against the whole move only when the absolute ladder claimed the hour
    # alone, and against the "on its own" line otherwise. `both` therefore takes
    # the abnormal date - the line it is written on is the residual's.
    whole_move = pd.Series(basis, index=frame.index).eq("absolute")
    return abnormal.mask(whole_move, absolute).to_numpy(dtype=object)


def _flag(value) -> "bool | None":
    """pandas boolean NA is not False - an hour whose rank window has not filled
    has not disagreed, it has not spoken."""
    return None if value is None or value is pd.NA else bool(value)


def withdraw_unconfirmed(frame: pd.DataFrame,
                         sources: dict[str, str] = TIER_SOURCES) -> pd.DataFrame:
    """Drops an abnormal-only claim the non-parametric rank test contradicts.

    Practical significance beside statistical significance. The abnormal channel
    is a t-statistic, so it says "large RELATIVE TO the peers this hour" - and
    when the peers are asleep that ratio is large for a move of six basis points.
    67 of 581 pushes fired on a move below the instrument's own median hour, and
    65 of those 67 were abnormal-only.

    Corrado's rank test is the right second opinion because it shares none of
    that machinery: it ranks this bar against the instrument's own recent bars
    and never estimates a variance, which is exactly the quantity thin trading
    distorts (Campbell & Wasley 1993). So an abnormal-only hour that the ranks
    put outside the top 1% of its own window has its abnormal claim withdrawn
    and is re-combined; if the absolute channel also fired, the hour was never
    abnormal-only and this does not touch it.

    THAT LAST CLAUSE KEEPS THE RULE SAFE IN A CRISIS. The rank test is itself
    misspecified when variance jumps - precisely when the absolute channel fires
    - so the gate lifts exactly where the rank test stops being trustworthy. Of
    24 pushes in October 2008 it removes one, of 15 in March 2020 none, and it
    silences none of the 1,256 events in the top 0.1% of any instrument's hours.

    An hour whose rank window has not filled has NOT disagreed - `rank_confirms`
    is NA there, and NA is not a contradiction.
    """
    if "rank_confirms" not in frame or "basis" not in frame:
        return frame
    abnormal = sources.get("abnormal")
    if abnormal not in frame:
        return frame

    disagrees = frame["rank_confirms"].eq(False).fillna(False).to_numpy(dtype=bool)
    alone = frame["basis"].eq("abnormal").fillna(False).to_numpy(dtype=bool)
    withdraw = disagrees & alone
    if not withdraw.any():
        return frame

    out = frame.copy()
    out[abnormal] = out[abnormal].mask(withdraw, pd.NA)
    return severity.combine(out, sources)


def triggers(frame: pd.DataFrame) -> pd.Series:
    """The event-generation condition: the move cleared its own noticeable return level.

    This replaces a single critical value shared by every instrument. The
    critical value answered "is this distinguishable from noise", which is a
    question about the null hypothesis and not about the recipient - it fired
    2.1 times a week and every event it produced looked alike. The condition
    now is that the move is at least a once-a-month event FOR THIS
    INSTRUMENT, and what comes out with it is how rare it actually was, which
    is what decides whether the message interrupts anyone (see tremor.severity).

    THERE IS A SECOND FILTER ON RAW MAGNITUDE. The abnormal channel measures
    whether a move was UNEXPLAINED, never whether it was LARGE, and the two come
    apart at the bottom: an instrument that ticked +0.03% while its block went
    the other way has a residual its own history finds remarkable, and a reader
    does not. Every event below one times its own usual hour is abnormal-only -
    240 of 9,069, about eleven a year.

    So a move must also clear `min_move_sigma` times the instrument's own
    sigma_LT. This is separate from `sensitivity` on purpose: turning rarity down
    far enough to silence these would silence genuinely small instruments too. A
    bar with no sigma_LT yet is not filtered - it has not failed the test, it has
    not taken it.

    THE FLOOR IS PER INSTRUMENT, and has to be. A rung is the biggest move in its
    own lookback, so every instrument clears one about once per lookback whatever
    its market does - that is what makes the word mean one thing across a digest,
    and it is also why the rungs cannot be what separates a loan ETF from Solana.
    Under one shared floor the whole basket sits between 9.3 and 20.4 events a
    year, a 2.2x spread across instruments that differ by far more. An instrument
    producing lines the reader does not want has its own floor raised on its entry
    in config/basket.yaml; a block line is floored the same way under
    `block_min_move_sigma`, without copying onto its members.
    """
    if "tier" not in frame:
        raise KeyError("severity.annotate must run before triggers")

    # Assessed if EITHER ladder was fitted and had something to read. One
    # channel still warming up does not make the hour unassessed - the other
    # one answered - and only an hour where neither could speak is NULL.
    assessed = pd.Series(False, index=frame.index)
    for column, prefix in ((("z_resid_bmp" if "z_resid_bmp" in frame else "z_resid"),
                            severity.LEVEL_PREFIX),
                           (ABSOLUTE_COLUMN, ABSOLUTE_LEVEL_PREFIX)):
        first = severity.level_columns(prefix)[0]
        if column in frame and first in frame:
            assessed |= frame[column].notna() & frame[first].notna()

    fired = frame["tier"].notna()
    if "r" in frame and "sigma_lt" in frame:
        tuning = load_tuning()
        # Per row rather than once per frame. Frames are built per instrument, so
        # in practice this is one lookup repeated - but a frame that ever carried
        # two instruments would silently take the first one's floor for both, and
        # that is the kind of wrong that never raises.
        if "asset_id" in frame:
            floor = frame["asset_id"].map(tuning.floor_for).astype("float64")
        else:
            floor = pd.Series(tuning.min_move_sigma, index=frame.index)
        usual = frame["sigma_lt"]
        big_enough = frame["r"].abs() >= floor * usual
        # An unmeasured sigma_LT is not a failed test, so it does not filter.
        # Nor is a floor of zero, which is how an instrument opts out entirely.
        fired &= big_enough | usual.isna() | (usual <= 0) | (floor <= 0)
    return fired.where(assessed, pd.NA).astype("boolean")


def _exceedance(frame: pd.DataFrame, tier: np.ndarray) -> np.ndarray:
    """How far past its tier's threshold each bar cleared, on its own channel.

    Two bars in the same tier are not equally big, and the ladder alone cannot
    say which is bigger: one may have earned `extreme` on the abnormal channel
    and the other on the absolute one, and z and r are not the same quantity.
    Dividing each by the threshold IT had to clear makes them comparable - both
    become "times the bar it cleared" - and taking the larger of the two channels
    means a bar is credited with whichever way it was remarkable.

    Bars with no tier, or with a threshold that is missing or zero, score zero:
    they are never the more extreme of a pair, which is the safe direction, and
    a zero threshold would otherwise make an unassessed bar infinitely large.
    """
    out = np.zeros(len(frame))
    # tier is an object array that carries pandas NA, and `tier == name` on one
    # of those returns NA rather than False, which .any() then refuses to read.
    # Coercing to plain strings first keeps the comparison a boolean one.
    names = np.array([t if isinstance(t, str) else "" for t in tier], dtype=object)
    for column, prefix in ((("z_resid_bmp" if "z_resid_bmp" in frame else "z_resid"),
                            severity.LEVEL_PREFIX),
                           (ABSOLUTE_COLUMN, ABSOLUTE_LEVEL_PREFIX)):
        if column not in frame:
            continue
        value = np.abs(frame[column].to_numpy(dtype=float))
        for name in severity.TIERS:
            level = f"{prefix}_{name}"
            if level not in frame:
                continue
            here = names == name
            if not here.any():
                continue
            threshold = np.abs(frame[level].to_numpy(dtype=float))
            usable = here & np.isfinite(value) & np.isfinite(threshold) & (threshold > 0)
            if usable.any():
                out[usable] = np.maximum(out[usable],
                                         value[usable] / threshold[usable])
    return out


def build_events(asset: Asset, frame: pd.DataFrame,
                 day_tz: "str | None" = _DERIVE) -> list[SaedEvent]:
    """Runs the event automaton over the asset's bars.

    ONE EVENT PER INSTRUMENT PER TRADING DAY. Every firing after the first joins
    the open event - escalating it, if it is worse - until the instrument's day
    turns, and only then can a new one open. It replaces a twelve-bar pause, and
    the reason is the one thing a bar count could never give: a rule the reader
    can hold. "SPY has already been reported today" is a sentence; "SPY fired
    nine bars ago" is an implementation detail, and the two disagreed exactly
    where it mattered - sixty pairs of pushes on one instrument inside forty-
    eight hours, half of them the instrument thrashing back the way it came.

    The day is the INSTRUMENT'S, not the clock's: an exchange-listed fund's day
    is the exchange's local one, so an afternoon move and the following
    morning's are two days apart even though six hours separate them, while
    22:00 and 02:00 UTC are the same session and fire once. Round-the-clock
    instruments take the UTC day. Same rule as the retention horizons, and
    deliberately: the today-close reading lands exactly when the instrument
    becomes eligible to fire again.

    The sequential pass is layer B: whether a firing joins the current
    event or opens a new one is a path-dependent decision.
    """
    fired = triggers(frame).fillna(False).to_numpy(dtype=bool)
    # An hour whose price moved less than the instrument can resolve is not a
    # small event, it is an unobserved one - see quality.resolvable. Applied
    # here rather than inside triggers() because it needs the instrument's tick
    # size, and triggers() deliberately reads nothing but the frame.
    if "close" in frame and getattr(asset, "tick_size", 0):
        fired &= quality.resolvable(frame["close"].to_numpy(),
                                    frame["r"].to_numpy(), asset.tick_size)
    if not fired.any():
        return []

    hours = frame["hour_utc"].to_numpy()
    z = frame["z_resid"].to_numpy()
    e = frame["e_resid"].to_numpy()
    r = frame["r"].to_numpy()
    beta = (frame["beta_block"].to_numpy() if "beta_block" in frame
            else np.full(len(frame), np.nan))
    block_part = (frame["co_block"].to_numpy(dtype="float64")
                  if "co_block" in frame else np.full(len(frame), np.nan))
    level = (frame["close"].to_numpy(dtype="float64") if "close" in frame
             else np.full(len(frame), np.nan))
    usual = (frame["sigma_lt"].to_numpy() if "sigma_lt" in frame
             else np.full(len(frame), np.nan))
    tier = frame["tier"].to_numpy(dtype=object)
    confirms = (frame["rank_confirms"].to_numpy(dtype=object)
                if "rank_confirms" in frame else np.full(len(frame), None))
    reverts = (frame["ou_reverts"].to_numpy(dtype=object)
               if "ou_reverts" in frame else np.full(len(frame), None))
    basis = frame["basis"].to_numpy(dtype=object) if "basis" in frame \
        else np.full(len(frame), "abnormal", dtype=object)
    overnight = (frame["overnight"].fillna(False).to_numpy(dtype=bool)
                 if "overnight" in frame else np.zeros(len(frame), dtype=bool))
    # One lookback per ladder, picked per bar by the basis that claimed it. A
    # bar claimed on `both` is dated by the raw move, which is the one the reader
    # can see on a chart.
    since = _record_since(frame, basis)
    rank = {name: i for i, name in enumerate(severity.TIERS)}
    exceedance = _exceedance(frame, tier)
    day = persistence.day_codes(
        frame, sessions.day_tz(asset.session_template)
        if day_tz is _DERIVE else day_tz)

    events: list[SaedEvent] = []
    counts: list[int] = []
    _peak_at: list[int] = []     # index of the bar each event is REPORTED at
    open_at: int | None = None   # index of the bar on which the current event opened

    for i in np.flatnonzero(fired):
        if open_at is not None and day[i] == day[open_at]:
            # Still the same day: the same event continues, no notification - but
            # it can still get worse. A move that opens at the noticeable level and
            # reaches the major one an hour later is a major event; reporting
            # the tier it happened to start at would understate it purely
            # because of when the automaton opened. So the event keeps the
            # highest tier it reached, and only the notification is suppressed.
            counts[-1] += 1
            here = rank.get(tier[i], -1)
            there = rank.get(events[-1].tier, -1)
            # A higher tier always wins. So does a bigger move at the SAME
            # tier, and that second half is not a nicety: extreme is the top of
            # the ladder, so an event that opens there can never be escalated,
            # and without this the reported bar would stay wherever the
            # automaton happened to open - which, over a whole day, is usually
            # the smallest bar of the move rather than its peak. On 2015-01-15 the franc peg broke:
            # 09:00 was already extreme at -3.5%, 10:00 was -10.5%, and the
            # push described the first. Comparing exceedance rather than raw
            # magnitude keeps the two channels commensurable - each bar is
            # measured against its own tier's threshold on the channel that
            # earned it, so an absolute-basis bar and an abnormal-basis one can
            # be ranked without pretending z and r are the same quantity.
            if here > there or (here == there
                                and exceedance[i] > exceedance[_peak_at[-1]]):
                _peak_at[-1] = i
                # The whole bar moves with the tier, not the tier alone. The
                # tier is earned by THIS hour's move, so reporting it beside the
                # opening hour's magnitude describes two different bars as one
                # event - and the delivery layer prints that magnitude next to
                # the tier's own words. It produced pushes reading "biggest move
                # in 3 years, +0.01%": a fifth of a basis point on SHY, opened
                # at the noticeable level at 15:00, with the extreme belonging to
                # the +0.13% at 17:00. Identity stays at the opening hour, so
                # event_id is untouched and a push already sent is edited rather
                # than repeated; the description follows the bar that earned the
                # label.
                events[-1] = SaedEvent(**{**events[-1].__dict__,
                                          "tier": tier[i], "basis": str(basis[i]),
                                          "peak_hour_utc": int(hours[i]),
                                          "rank_confirms": _flag(confirms[i]),
                                          "ou_reverts": _flag(reverts[i]),
                                          "z_resid": float(z[i]),
                                          "e_resid": float(e[i]),
                                          "co_block": float(block_part[i]),
                                          "r": float(r[i]),
                                          "beta_block": float(beta[i]),
                                          "sigma_lt": float(usual[i]),
                                          "close": float(level[i]),
                                          "record_since": _opt_int(since[i]),
                                          "overnight": bool(overnight[i])})
            continue
        open_at = i
        events.append(SaedEvent(
            event_id=f"{asset.file_stem}:{int(hours[i])}",
            asset_id=asset.asset_id, block=asset.block, hour_utc=int(hours[i]),
            peak_hour_utc=int(hours[i]), rank_confirms=_flag(confirms[i]),
            ou_reverts=_flag(reverts[i]),
            z_resid=float(z[i]), e_resid=float(e[i]),
            co_block=float(block_part[i]), r=float(r[i]),
            beta_block=float(beta[i]), repeat_count=0, tier=str(tier[i]),
            basis=str(basis[i]), sigma_lt=float(usual[i]),
            close=float(level[i]), record_since=_opt_int(since[i]),
            overnight=bool(overnight[i]),
        ))
        counts.append(0)
        _peak_at.append(i)

    return [SaedEvent(**{**event.__dict__, "repeat_count": count})
            for event, count in zip(events, counts)]


def events_frame(events: list[SaedEvent]) -> pd.DataFrame:
    columns = ["event_id", "asset_id", "block", "hour_utc", "peak_hour_utc",
               "z_resid", "e_resid", "co_block",
               "r", "beta_block", "repeat_count", "tier", "basis",
               "sigma_lt", "close", "record_since",
               "rank_confirms", "ou_reverts", "overnight"]
    if not events:
        return pd.DataFrame({c: pd.Series(dtype="object" if c in
                                          ("event_id", "asset_id", "block",
                                           "tier", "basis")
                                          else "bool" if c == "overnight"
                                          else "float64") for c in columns})
    return pd.DataFrame([e.__dict__ for e in events])[columns]


DEFAULT_EVENTS_PATH = "data/tremor/saed_events.parquet"
DEFAULT_RESIDUALS_DIR = "data/tremor/residuals"
# Full-history events for message rates only. Detection still reads the
# six-year table at DEFAULT_EVENTS_PATH. A warm hourly run appends; --full
# replaces. Gitignored and restored the same way metrics parquet is.
DEFAULT_ARCHIVE_PATH = "data/tremor/saed_events_archive.parquet"
ARCHIVE_COLUMNS = ("event_id", "asset_id", "hour_utc", "tier")

# Residual series that are written to disk. They can be recomputed from the
# metrics, but that means a full run of the regressions over the whole history -
# minutes instead of seconds - and both the decision journal and the event
# export need them.
#
# Intermediate states are not stored: the winsorized residual and sigma_eff are
# recovered unambiguously from e_resid and sigma_LT by the same winsorization,
# yet they take as much space as everything else put together - they are series
# of random numbers, and nothing compresses them.
RESIDUAL_COLUMNS = ("hour_utc", "asset_id", "beta_block", "e_resid",
                    "co_block",
                    "sigma_lt_resid", "patell_scale", "t_rank", "rank_pct",
                    "rank_confirms", "ou_reversion_bars", "s_score", "ou_reverts",
                    "z_resid", "bmp_scale", "bmp_dof",
                    "t_resid", "z_resid_bmp",
                    "q95_resid", "q99_resid", "tier", "basis", "tier_abnormal",
                    "tier_absolute") + severity.LEVEL_COLUMNS \
                  + severity.level_columns(ABSOLUTE_LEVEL_PREFIX) \
                  + persistence.RETENTION_COLUMNS


def archive_frame(events: pd.DataFrame) -> pd.DataFrame:
    """The slim columns a rate line needs, one row per event_id."""
    if events is None or events.empty:
        return pd.DataFrame(columns=list(ARCHIVE_COLUMNS))
    out = events.copy()
    if "event_id" not in out.columns:
        out["event_id"] = (
            out["asset_id"].astype(str) + ":" + out["hour_utc"].astype("int64").astype(str)
        )
    keep = [c for c in ARCHIVE_COLUMNS if c in out.columns]
    slim = out[keep].copy()
    if "hour_utc" in slim.columns:
        slim["hour_utc"] = pd.to_numeric(slim["hour_utc"], errors="coerce")
    if "tier" in slim.columns:
        slim["tier"] = slim["tier"].astype("string")
    if "asset_id" in slim.columns:
        slim["asset_id"] = slim["asset_id"].astype("string")
    if "event_id" in slim.columns:
        slim["event_id"] = slim["event_id"].astype("string")
    return slim.dropna(subset=["event_id"]).drop_duplicates(
        subset=["event_id"], keep="last").reset_index(drop=True)


def merge_archive(existing: pd.DataFrame | None,
                  incoming: pd.DataFrame) -> pd.DataFrame:
    """Keeps older hours and refreshes overlapping event_ids from this run.

    A warm table only holds six years. Concatenating it onto the durable file
    must not drop 2002 just because 2020-2026 arrived again.
    """
    new = archive_frame(incoming)
    if existing is None or existing.empty:
        return new
    old = archive_frame(existing)
    if new.empty:
        return old
    return archive_frame(pd.concat([old, new], ignore_index=True))


def load_events_archive(path: str = DEFAULT_ARCHIVE_PATH) -> list[dict]:
    """Rows for rate lines. Empty if the file is missing or unreadable."""
    if not os.path.exists(path):
        return []
    try:
        frame = pd.read_parquet(path)
    except Exception:                            # pragma: no cover - defensive
        return []
    if frame.empty:
        return []
    return archive_frame(frame).to_dict("records")


def write_events_archive(path: str, incoming: pd.DataFrame, *,
                         replace: bool = False) -> int:
    """Writes the durable rate archive. Returns how many rows it now holds.

    `replace` is the --full path: the incoming table IS the history.
    Otherwise this run's rows are merged onto whatever is already stored.
    An empty incoming warm table does not wipe a populated archive.
    """
    if replace:
        if incoming is None or incoming.empty:
            if os.path.exists(path):
                try:
                    return len(pd.read_parquet(path))
                except Exception:                # pragma: no cover - defensive
                    return 0
            return 0
        frame = archive_frame(incoming)
    elif incoming is None or incoming.empty:
        if not os.path.exists(path):
            return 0
        try:
            return len(pd.read_parquet(path))
        except Exception:                        # pragma: no cover - defensive
            return 0
    else:
        existing = None
        if os.path.exists(path):
            try:
                existing = pd.read_parquet(path)
            except Exception:                    # pragma: no cover - defensive
                existing = None
        frame = merge_archive(existing, incoming)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    atomic.write_parquet(path, frame)
    return len(frame)


def save_residuals(scored: dict[str, pd.DataFrame],
                   out_dir: str = DEFAULT_RESIDUALS_DIR) -> None:
    import os

    from tremor.basket import load_basket

    os.makedirs(out_dir, exist_ok=True)
    stems = {a.asset_id: a.file_stem for a in load_basket().instruments}
    for asset_id, frame in scored.items():
        columns = [c for c in RESIDUAL_COLUMNS if c in frame.columns]
        atomic.write_parquet(
            os.path.join(out_dir, f"{stems[asset_id]}.parquet"),
            frame[columns])


def load_residuals(basket: Basket,
                   out_dir: str = DEFAULT_RESIDUALS_DIR) -> dict[str, pd.DataFrame]:
    import os

    out = {}
    for asset in basket.instruments:
        path = os.path.join(out_dir, f"{asset.file_stem}.parquet")
        if os.path.exists(path):
            out[asset.asset_id] = pd.read_parquet(path)
    return out


# Which ladders an instrument carries, and the column prefix each one writes.
LADDERS = (("abnormal", severity.LEVEL_PREFIX), ("absolute", ABSOLUTE_LEVEL_PREFIX))


def plan_frames(basket: Basket, metrics: "dict[str, pd.DataFrame]"
                ) -> "tuple[dict[str, pd.DataFrame], bool]":
    """Trim each instrument to a trailing window, or say that the run must be cold.

    Every per-bar quantity here rebuilds exactly from a bounded slice of the past
    (see windows.warm_bars). The RUNG needs no history at all beyond the sigma it
    divides by - it is a size, not a lookback - but the DATE the message prints
    does: "the biggest since March 2020" is a statement about the instrument's
    record, and a trimmed slice can only speak for the part it holds. So the
    slice must still reach back past RECORD_HORIZON_DAYS, and a warm run
    publishes only the stretch it is exact over.

    ALL OR NOTHING still, on purpose. A run in which half the basket is trimmed
    and half is not has a cross-section built from two different amounts of
    history, which is harder to reason about than doing the whole run cold. In
    practice every instrument clears the check - the warm window is already
    around eight to eleven years and the deepest rung is six - so this is a guard
    against a shortened archive rather than a routine cost: the warm window is
    already eight to eleven years and the record horizon is six.

    The first bar is checked from the UNTRIMMED frame because that is the only
    place it is still visible: once an instrument is cut to its slice, nothing in
    what remains records how far the archive really reaches.
    """
    from tremor import pipeline

    deepest = severity.RECORD_HORIZON_DAYS * severity.SECONDS_PER_DAY
    trimmed: dict[str, pd.DataFrame] = {}
    warm = True
    for asset in basket.instruments:
        frame = metrics.get(asset.asset_id)
        if frame is None or frame.empty:
            continue
        bars_per = pipeline.bars_per_session(asset, frame, basket.anchor_exchange_tz)
        keep = windows.warm_bars(windows.w_asset(bars_per),
                                 severity.bar_rate(frame["hour_utc"]),
                                 template=asset.session_template)
        if len(frame) <= keep:
            # Short enough that the whole history IS the window.
            trimmed[asset.asset_id] = frame
            continue
        tail = frame.tail(keep).reset_index(drop=True)
        trimmed[asset.asset_id] = tail
        span = int(tail["hour_utc"].iloc[-1]) - int(tail["hour_utc"].iloc[0])
        if span < deepest:
            warm = False
    return (trimmed, warm) if warm else (dict(metrics), False)


def score_tiers(scored: "dict[str, pd.DataFrame]",
                blocks_of: "dict[str, str]") -> "dict[str, pd.DataFrame]":
    """Both ladders, combined, with the rank test's veto - for any scored series.

    Its own function because two series go through it: every instrument's hours,
    and every US fund's overnight gaps (tremor.gaps). A gap is put to exactly the
    questions an hour is, and one chain is how that stays true.
    """
    # Severity is fitted per instrument on its own standardised score, after the
    # cross-sectional pass because that is the score the trigger reads. It is
    # deliberately NOT pooled across the basket: the whole point of a return
    # period is that it is the instrument's own history that says what is rare
    # for it, and pooling would put SHY and SOL back on one yardstick.
    #
    # A rung is a size: the move over the instrument's own long-run sigma,
    # against a threshold set per block. The abnormal channel scores the BMP
    # residual, which is ALREADY a t-statistic, so it takes no divisor - passing
    # sigma there would apply the normalisation twice.
    #
    # And it takes its OWN table, which is the other half of that sentence and
    # was missing for a long time. Not squaring the normalisation is not the same
    # as being on the same scale: a t-statistic has had its tail removed by
    # construction and tops out near 15 where the raw ratio reaches 113, so the
    # member rungs were not strict here but unreachable. See BLOCK_RESID_SIGMA.
    scored = {aid: severity.annotate(frame, tier_column=TIER_SOURCES["abnormal"],
                                     block=blocks_of.get(aid),
                                     ladder=severity.RESIDUAL)
              for aid, frame in scored.items()}
    scored = {aid: severity.annotate(frame, column=ABSOLUTE_COLUMN,
                                     prefix=ABSOLUTE_LEVEL_PREFIX,
                                     tier_column=TIER_SOURCES["absolute"],
                                     fallback=None, scale_column="sigma_lt",
                                     block=blocks_of.get(aid))
              for aid, frame in scored.items()}
    scored = {aid: severity.combine(frame, TIER_SOURCES)
              for aid, frame in scored.items()}
    return {aid: withdraw_unconfirmed(frame) for aid, frame in scored.items()}


def build_for_basket(basket: Basket, metrics: dict[str, pd.DataFrame],
                     block_factors: pd.DataFrame | None = None,
                     panel: pd.DataFrame | None = None,
                     sigma_panel: pd.DataFrame | None = None,
                     full_metrics: "dict[str, pd.DataFrame] | None" = None
                     ) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Computes residuals and events for every instrument, non-basket ones included.

    Non-basket instruments are modelled the same way and on the same
    beta-estimation scheme: they do not enter their block's factor, but they are
    explained by it just like the rest.

    `full_metrics` is the UNTRIMMED metrics, and passing it switches on the
    overnight gap (tremor.gaps). Separate from `metrics` because a warm run
    trims the hourly series to its trailing window and the gap pass must not be
    trimmed with it - it always scores the whole morning history, which is small
    enough to, and is exact only because it does. None leaves every event exactly
    as it was before the gap existed.
    """
    from tremor import pipeline, residuals, windows as w

    scored: dict[str, pd.DataFrame] = {}
    windows_by_asset: dict[str, int] = {}
    for asset in basket.instruments:
        frame = metrics.get(asset.asset_id)
        if frame is None or frame.empty:
            continue
        # An all-NaN column is NOT a factor. block_factors returns one for a
        # block whose only member is this instrument - the leave-one-out median
        # of nothing - and passing it on would make the regression fail to fit,
        # leaving e_resid as r minus NaN: the instrument goes silent, with no
        # error anywhere. None is the honest input, and residuals() answers it
        # with the drift-only model an instrument with no peers is entitled to.
        own_block = None
        if block_factors is not None and asset.asset_id in block_factors:
            column = block_factors[asset.asset_id]
            own_block = column if column.notna().any() else None
        with_residuals = residuals.residuals(asset, frame, own_block)
        b_asset = pipeline.bars_per_session(asset, frame, basket.anchor_exchange_tz)
        windows_by_asset[asset.asset_id] = w.w_asset(b_asset)
        scored[asset.asset_id] = residuals.score_residuals(
            with_residuals, windows_by_asset[asset.asset_id])

    # The cross-sectional pass needs every asset's Z at once, so it can only run
    # after the loop - and the thresholds have to be recomputed on the score the
    # trigger will actually read, or a standardised score would be compared
    # against an unstandardised yardstick.
    scored = residuals.standardise_cross_section(scored)
    scored = {aid: residuals.rescore_thresholds(frame, windows_by_asset[aid])
              for aid, frame in scored.items()}

    scored = score_tiers(scored, {a.asset_id: a.block for a in basket.instruments})
    # The settled retention lands at the close of the next trading day, so an
    # instrument whose day is a SESSION is measured in its exchange's local day
    # and a round-the-clock one in the UTC day. us_equity is the only template
    # with an authoritative calendar behind it.
    day_of = {a.asset_id: sessions.day_tz(a.session_template)
              for a in basket.instruments}
    # And whether the newest day in each frame has finished, which only the
    # calendar can say. The store ends with the hour this run is standing in, so
    # for twenty-three hours out of twenty-four that day is half a day - and a
    # close reading taken from it would divide by whatever bar happened to be
    # newest and print the answer under "this day's close".
    closed_of = {a.asset_id: _last_day_closed(scored.get(a.asset_id),
                                              a.session_template)
                 for a in basket.instruments}
    scored = {aid: persistence.annotate(frame, day_of.get(aid),
                                        closed_of.get(aid, False))
              for aid, frame in scored.items()}

    # The overnight gap, written onto a COPY of each frame - the first bar of a
    # session, where the gap out-ranks it, and nowhere else. `scored` itself is
    # what the residual store is written from and what every other event's
    # retention was summed over above, and neither may see the night.
    gap_pass = gaps.score(basket, full_metrics, score_tiers) \
        if full_metrics is not None else None
    judged = scored
    if gap_pass is not None:
        judged = {aid: gaps.overlay(frame, gap_pass.assets.get(aid),
                                    day_of.get(aid), closed_of.get(aid, False))
                  for aid, frame in scored.items()}

    all_events: list[SaedEvent] = []
    for asset in basket.instruments:
        if asset.asset_id in judged:
            all_events.extend(build_events(asset, judged[asset.asset_id]))

    # The blocks themselves, as rows in the same table. Widening the blocks made
    # every member's residual smaller on the days the whole block moves - which
    # is what it is for - and the cost of that is silence on exactly those days,
    # because then no member is abnormal. See tremor.blocks.
    block_scored: dict[str, pd.DataFrame] = {}
    block_rows = pd.DataFrame()
    if panel is not None and sigma_panel is not None:
        block_scored = blocks.frames(basket, panel, sigma_panel)
        if gap_pass is not None:
            members, _ = cross_section._block_members(panel, basket)
            block_scored = {
                name: gaps.overlay(
                    frame, gap_pass.blocks.get(name),
                    blocks._day_tz(basket, members.get(name, [])),
                    blocks._last_day_closed(basket, members.get(name, []), frame))
                for name, frame in block_scored.items()}
        block_rows = blocks.events_frame(
            block_scored, basket, panel,
            gap_pass.panel if gap_pass is not None else None)

    frame = events_frame(all_events)
    if not block_rows.empty:
        frame = pd.concat([frame, block_rows], ignore_index=True)
    retention_from = dict(judged)
    retention_from.update({blocks.block_id(name): f for name, f in block_scored.items()})

    return routing.route(persistence.attach(frame, retention_from)), scored


def _last_day_closed(frame: "pd.DataFrame | None", template: str) -> bool:
    """Whether a scored frame ends on the closing bar of its instrument's day.

    False whenever the calendar cannot answer - no session table, a template it
    does not know, an empty frame - because withholding a check-in shows it as
    still due, and that is recoverable in a way that a published number is not.
    """
    if frame is None or frame.empty:
        return False
    table = None
    if template == "us_equity":
        try:
            table = sessions.cached_sessions()
        except Exception as exc:                 # pragma: no cover - defensive
            logging.getLogger("tremor.saed").warning(
                "No session table, so no day can be called closed: %s", exc)
            return False
    try:
        return sessions.day_is_closed(int(frame["hour_utc"].max()), template, table)
    except ValueError as exc:                    # pragma: no cover - defensive
        logging.getLogger("tremor.saed").warning(
            "Cannot tell whether %s's day has closed: %s", template, exc)
        return False


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging
    import os

    from tremor import cross_section, pipeline, sessions
    from tremor.basket import load_basket

    parser = argparse.ArgumentParser(description="SAED events")
    parser.add_argument("--metrics-dir", default=pipeline.DEFAULT_METRICS_DIR)
    parser.add_argument("--events-out", default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--residuals-out", default=DEFAULT_RESIDUALS_DIR)
    parser.add_argument("--residuals-always", action="store_true",
                        help="write the residual series even on a warm run, "
                             "where it covers only the trailing window")
    parser.add_argument("--full", action="store_true",
                        help="score on all history rather than the trailing window")
    parser.add_argument("--archive-out", default=DEFAULT_ARCHIVE_PATH,
                        help="durable events archive for message rates")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("tremor.saed")

    from tremor import versioning

    basket = load_basket()
    metrics = pipeline.load_all(basket, args.metrics_dir)
    if not metrics:
        log.error("No per-asset metrics - run python -m tremor.pipeline first")
        return 2

    config, run_id = versioning.versions_for()
    frames, warm = (dict(metrics), False) if args.full else plan_frames(basket, metrics)

    def panels(source):
        panel = cross_section.build_panel(source, "r")
        hours = panel.index[sessions.reference_hours_mask(
            pd.Series(panel.index), basket.anchor_exchange_tz).to_numpy()]
        sigma = cross_section.build_panel(source, "sigma_eff").reindex(index=panel.index)
        return panel.loc[hours], sigma.loc[hours]

    panel, sigma_panel = panels(frames)
    # Blocks need no separate check: a block's series is built from the member
    # frames, so it reaches back exactly as far as they do, and plan_frames has
    # already refused any slice shorter than the deepest rung.

    kept = sum(len(f) for f in frames.values())
    whole = sum(len(f) for f in metrics.values())
    log.info("%s run: %d of %d bars (%.0f%%)",
             "warm" if warm else "cold", kept, whole, 100 * kept / max(whole, 1))

    # Built here from the per-asset panel, so SAED depends on nothing
    # cross_section writes to disk and the two can run in either order.
    block_factors = cross_section.block_factors(panel, basket, sigma_panel)

    events, scored = build_for_basket(
        basket, frames, block_factors, panel, sigma_panel, full_metrics=metrics)

    # Rate lines read the durable archive, not the six-year delivery table.
    # --full replaces it with this run's complete history; a warm run merges
    # so hours older than the trailing window stay put.
    archived = write_events_archive(args.archive_out, events, replace=args.full)
    log.info("rate archive %s: %d event(s)", args.archive_out, archived)

    if warm and not events.empty:
        # A warm run publishes only the span it is exact over. Beyond it the
        # trailing window is still warming up, and those rows differ from a full
        # run - not by much, but the event table is where "the last one this big
        # was" is read from, and a tier that is nearly right there names the
        # wrong date.
        from tremor.severity import RECORD_HORIZON_DAYS

        floor = int(events["hour_utc"].max()) - int(RECORD_HORIZON_DAYS * 86400)
        before = len(events)
        events = events[events["hour_utc"] >= floor].reset_index(drop=True)
        log.info("warm run publishes %d of %d events - the %d days it is exact over",
                 len(events), before, int(RECORD_HORIZON_DAYS))

    events = versioning.stamp(events, config, run_id)
    os.makedirs(os.path.dirname(args.events_out) or ".", exist_ok=True)
    atomic.write_parquet(args.events_out, events)
    # RESIDUALS ARE A BACKTEST ARTEFACT, not something the hourly run produces
    # for anyone. Nothing in the delivery path reads them: the only readers are
    # tremor.saed_score and tools/report_card.py, both run by hand. Writing them
    # every hour cost 268 MB and about thirteen seconds for nobody.
    #
    # And it was worse than waste. A warm run scores only the trailing window,
    # so the files it wrote held 57% of each series - and then the scorer and
    # the report card, which want the whole history, read that truncated series
    # and reported on it without any sign that most of it was missing. Written
    # on a cold run, which is the one whose residuals mean what they say.
    if warm and not args.residuals_always:
        log.info("warm run: residuals not written (they would hold only the "
                 "trailing window). Use --full, or --residuals-always, for a "
                 "set tremor.saed_score can score.")
    else:
        save_residuals(scored, args.residuals_out)

    log.info("events %d, later firings merged into their day %d",
             len(events),
             int(events["repeat_count"].sum()) if not events.empty else 0)
    if not events.empty:
        span = (events["hour_utc"].max() - events["hour_utc"].min()) / (3600 * 24 * 365.25)
        counts = events["tier"].value_counts()
        log.info("events by tier: %s", {t: int(counts.get(t, 0))
                                        for t in severity.TIERS})
        if span > 0:
            log.info("per year: %s", {t: round(int(counts.get(t, 0)) / span, 2)
                                      for t in severity.TIERS})
        by_channel = events["channel"].value_counts()
        log.info("by basis: %s", events["basis"].value_counts().to_dict())
        log.info("by channel: %s", {c: int(by_channel.get(c, 0)) for c in
                                    (routing.PUSH, routing.DIGEST)})
        if span > 0 and by_channel.get(routing.PUSH, 0):
            log.info("a push every %.0f days, %.1f items per digest",
                     365.25 / (by_channel[routing.PUSH] / span),
                     by_channel.get(routing.DIGEST, 0) / (span * 104))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

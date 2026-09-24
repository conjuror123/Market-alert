"""Block-level events: when a whole block moves, rather than one member of it.

WHY THIS EXISTS. Every instrument is modelled against the median of its own
block, so the better that block describes it, the smaller its residual on the
days the block moves together. That is the point - twenty members of one complex
moving as one is a single observation, and firing twenty times about it was the
original sin this system was built to fix. But it has a consequence that only
appears once the blocks are wide: on those days NO member is abnormal, so nothing
fires at all, and the biggest days in the record - the ones where a whole complex
reprices - become the quietest.

So the block gets a ladder of its own. The same peaks-over-threshold fit that
tells an instrument its move is a once-a-year move for it, asked of the block's
own series, tells the block the same thing about itself. A block event says "US
and global equities moved together, and a move this big for that block happens
about once a year", which is the observation the member events cannot make and
should not try to.

WHAT THE BLOCK'S MOVE IS. The median across its members of each member's return
divided by that member's own usual hour, sign-oriented first (see
cross_section.block_moves). Unitless, because a block has no units: "equities
moved 2%" is a claim about a median of percentages across instruments whose
volatilities differ by a factor of twenty-five. For the message it is converted
back with the median member's sigma, so the reported figure means "the typical
member moved this much", and the members that actually led are named beside it.

WHAT IT IS NOT. It is not an aggregation of member events, which would answer a
different question ("several members fired at once") and was tried: counting how
many members of a block fired in the same hour predicts nothing about whether the
move held, never once contradicted the block model in twenty-three years, and does
not flag a bad print. This fires on the block's own move whether or not any member
cleared its own threshold, which is exactly the case that was silent.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tremor import (cross_section, ewma, persistence, sessions, severity,
                    windows)
from tremor.basket import Basket

# The asset_id a block event carries. Prefixed rather than bare so that nothing
# downstream can confuse a block with an instrument by accident, and so that a
# glance at an event table says which kind of row it is looking at.
BLOCK_PREFIX = "block:"

# The basis a block event is reported under. The asset ladders have two -
# "abnormal" (its peers did not explain it) and "absolute" (it was simply big) -
# and a block has neither, because there is nothing above it to explain it with
# and no single price for "big" to be measured against. It has its own.
BLOCK_BASIS = "block"

# A block's move is scored against its own history like everything else, so it
# needs one before it can be scored at all.
MIN_BARS = windows.SIGMA_LT_MIN_BARS

# WHICH OF A BLOCK'S OWN TIERS ARE WORTH DELIVERING. Not the bottom one. There
# are nine blocks and in any hour one of them is the one that moved most, so a
# block at `noticeable` is the ordinary background of a market rather than news
# - 32.2 rows a year, five a week between them, and the sentence each one
# carries is "energy moved a bit more than the others".
#
# `high` is a different quantity and was measured separately: 11.4 rows a year,
# about one a month, spread evenly over the nine blocks at 1.0 to 1.6 each
# rather than piling into energy and equity the way the bottom rung does. It
# goes to the digest, not to a push, so it costs a line in the Saturday note and
# never a phone call. The filter used to be the two push tiers alone, on a
# measurement that pooled `noticeable` and `high` into one number - 52.7 rows a
# year - and so answered a question nobody had asked: whether to take BOTH.
BLOCK_TIERS = ("high", "major", "extreme")


def block_id(block: str) -> str:
    return f"{BLOCK_PREFIX}{block}"


def is_block(asset_id: str) -> bool:
    return str(asset_id).startswith(BLOCK_PREFIX)


def block_name(asset_id: str) -> str:
    return str(asset_id)[len(BLOCK_PREFIX):]


def _member_scale(sigma_panel: pd.DataFrame, columns: list[str],
                  index: pd.Index) -> pd.Series:
    """A typical member's usual hour, so the block's move can be quoted as one.

    The median rather than the mean: it is the same statistic the move itself is
    built on, and a block holding both SHY and TLT has a mean sigma that
    describes neither.
    """
    frame = sigma_panel.reindex(index=index, columns=columns)
    values = frame.to_numpy(dtype="float64")
    values = np.where(np.isfinite(values) & (values > 0), values, np.nan)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return pd.Series(np.nanmedian(values, axis=1), index=index)


def hours_by_block(basket: Basket, panel: pd.DataFrame,
                   sigma_panel: pd.DataFrame) -> "dict[str, pd.Index]":
    """The hours each block actually has a move on, for the blocks that have one.

    A block is absent entirely when fewer than two of its members have bars -
    which is the ordinary state of a block whose instruments are still being
    collected - and short blocks are absent too: a tail cannot be fitted to a
    history that does not reach the burn-in, and inventing one would make the
    first months of a new instrument the loudest thing in the basket.

    Shared with the coverage check in saed, deliberately. Asking that question
    against the panel's whole index instead - which is what it did first - counts
    every currency hour against a block that only trades the US session, and a
    block could then never be considered covered at all.
    """
    moves = cross_section.block_moves(panel, basket, sigma_panel)
    members, _ = cross_section._block_members(panel, basket)
    out = {}
    for block in members:
        hours = moves[block].dropna().index
        if len(hours) >= MIN_BARS:
            out[block] = hours
    return out


def frames(basket: Basket, panel: pd.DataFrame,
           sigma_panel: pd.DataFrame, template: "str | None" = None,
           retention: bool = True, records=None) -> "dict[str, pd.DataFrame]":
    """One scored frame per block, shaped like an instrument's.

    Shaped like an instrument's on purpose: it then goes through the same
    severity ladder, the same retention check and the same routing as everything
    else, and the delivery layer has one kind of row to render rather than two.

    `template` and `retention` are for tremor.gaps, which runs this over a panel
    of overnight gaps - one row per session. Its sigma has to be read in days
    (windows.DAILY_SERIES) rather than on the members' hourly calendar, and its
    retention is measured on the HOURLY block series the gap is overlaid onto,
    not on the next morning's gap.
    """
    moves = cross_section.block_moves(panel, basket, sigma_panel)
    members, _ = cross_section._block_members(panel, basket)

    out: dict[str, pd.DataFrame] = {}
    for block, columns in members.items():
        move = moves[block].dropna()
        if len(move) < MIN_BARS:
            continue
        scale = _member_scale(sigma_panel, columns, panel.index).reindex(move.index)
        frame = pd.DataFrame({
            "hour_utc": move.index.to_numpy(dtype="int64"),
            # The score the ladder is fitted to: the block's move in units of a
            # member's usual hour.
            "z_resid": move.to_numpy(),
            # The same move as a return, so the message can quote a percentage.
            # There is nothing above a block to explain its move, so its "own"
            # part is the whole of it and e_resid and r are the same number.
            "r": (move * scale).to_numpy(),
            "sigma_lt": scale.to_numpy(),
            "n_members": np.isfinite(
                panel.reindex(columns=columns).reindex(move.index).to_numpy(
                    dtype="float64")).sum(axis=1),
        })
        frame["e_resid"] = frame["r"]
        frame["co_block"] = 0.0
        # The denominator floor the retention check applies wants the series'
        # own long-run spread, on data strictly before the bar like everything
        # else here.
        frame["sigma_lt_resid"] = ewma.sigma_lt(
            frame["e_resid"], template or _template(basket, columns) or "us_equity")
        # A block's move is already a median of member moves each divided by its
        # own sigma, so it arrives standardised and takes no divisor. It gets
        # BLOCK_MOVE_SIGMA rather than the member table: a median of sixteen
        # equity ETFs is the most diversified series in the basket and a median
        # of eight FX pairs is not, and giving both the member numbers left the
        # US equity complex firing once in twenty-five years.
        frame = severity.annotate(frame, column="z_resid", fallback=None,
                                  tier_column="tier", block=block,
                                  ladder=severity.BLOCK_OWN,
                                  record_state=(records.state(
                                      block_id(block), severity.LEVEL_PREFIX)
                                      if records is not None else None))
        frame["basis"] = pd.Series(BLOCK_BASIS, index=frame.index,
                                   dtype="string").where(frame["tier"].notna())
        out[block] = (persistence.annotate(frame, _day_tz(basket, columns),
                                           _last_day_closed(basket, columns, frame))
                      if retention else frame)
    return out


def _template(basket: Basket, columns: "list[str]") -> "str | None":
    """The session template a block's members share, or None where they differ.

    A block is not an instrument and has no calendar of its own, so everything
    that needs one - which day it belongs to, how far its sigma reaches - reads
    it off the members. Mixed blocks do not occur: the configuration groups by
    what a thing IS, and what a thing is decides where it trades.
    """
    shared = {a.session_template for a in basket.assets if a.asset_id in columns}
    return shared.pop() if len(shared) == 1 else None


def _last_day_closed(basket: Basket, columns: "list[str]",
                     frame: pd.DataFrame) -> bool:
    """Whether a block's newest day has finished, on its members' calendar.

    A block is not an instrument and has no session table of its own, so the
    question is asked of the template its members share - the same one _day_tz
    reads the block's day off. Where they do not share one, or the calendar
    cannot answer, the block's newest day is treated as open: a block row's
    check-in then reads as due, which is what it is.
    """
    template = _template(basket, columns)
    if template is None or frame.empty:
        return False
    table = None
    if template == "us_equity":
        try:
            table = sessions.cached_sessions()
        except Exception:                        # pragma: no cover - defensive
            return False
    try:
        return sessions.day_is_closed(int(frame["hour_utc"].max()), template, table)
    except ValueError:                           # pragma: no cover - defensive
        return False


def _day_tz(basket: Basket, columns: "list[str]") -> "str | None":
    """The calendar a block's day follows: its members', where they agree.

    Mixed blocks do not occur - the configuration groups by what a thing is, and
    what a thing is decides where it trades - so one shared template means one
    answer. A block whose members somehow disagreed falls back to the UTC day,
    the only boundary that means something to all of them.
    """
    template = _template(basket, columns)
    return sessions.day_tz(template) if template else None


def _by_day(positions: np.ndarray, day: np.ndarray) -> "list[np.ndarray]":
    """The fired positions grouped into the trading days they fall in, in order.

    Split on the changes rather than grouped by value: the codes run in calendar
    order down a frame sorted by hour, so a run of equal codes IS a day and the
    split keeps the groups in the order they happened.
    """
    if positions.size == 0:
        return []
    codes = day[positions]
    return np.split(positions, np.flatnonzero(codes[1:] != codes[:-1]) + 1)


def events_frame(scored: "dict[str, pd.DataFrame]", basket: Basket,
                 panel: pd.DataFrame,
                 gap_panel: "pd.DataFrame | None" = None) -> pd.DataFrame:
    """The block events, in the same columns an asset event carries.

    Everything above the bottom rung - see BLOCK_TIERS for what that costs and
    why `noticeable` is the one that is dropped. `major` and `extreme` push;
    `high` goes into the note like any other digest row.

    And the same size floor an instrument has: `|r|` must clear
    `min_move_sigma` times the block's usual hour, with a per-block override
    that does not copy onto the members. Shared `min_move_sigma` is the default.

    ONE ROW PER BLOCK PER TRADING DAY, the same rule an instrument gets, and
    until now blocks had no such rule at all: every qualifying bar became its own
    push, so the day a complex repriced in three legs sent three alerts saying
    the same thing. A block's day is closed by its members' session where they
    share one and by the UTC clock where they do not.

    Within the day the row describes the bar that earned the tier, not the bar
    the day opened on - a whole complex usually starts moving before it has
    moved far, so the opening bar is typically the smallest of the run. Identity
    stays with the opening bar so that a push already sent is edited rather than
    repeated.

    A row the overnight gap claimed (tremor.gaps, the `overnight` column) names
    its leaders from `gap_panel` - who opened furthest from their close - since
    the first hour's own moves are not what earned it.
    """
    from tremor.basket import load_tuning

    members, _ = cross_section._block_members(panel, basket)
    order = {name: i for i, name in enumerate(severity.TIERS)}
    tuning = load_tuning()
    rows = []
    for block, frame in scored.items():
        floor = tuning.floor_for(block_id(block))
        usual = frame["sigma_lt"] if "sigma_lt" in frame else None
        sized = pd.Series(True, index=frame.index)
        if usual is not None and "r" in frame:
            sized = (frame["r"].abs() >= floor * usual)
            sized |= usual.isna() | (usual <= 0) | (floor <= 0)
        fired = frame[frame["tier"].isin(BLOCK_TIERS) & sized]
        columns = [c for c in members.get(block, []) if c in panel.columns]
        if fired.empty:
            continue
        day = persistence.day_codes(frame, _day_tz(basket, columns))
        size = frame["z_resid"].abs().to_numpy(dtype="float64")
        tiers = frame["tier"].to_numpy(dtype=object)
        for positions in _by_day(fired.index.to_numpy(), day):
            peak = positions[0]
            for i in positions[1:]:
                here, there = order.get(tiers[i], -1), order.get(tiers[peak], -1)
                # A higher tier always wins, and a bigger move at the SAME tier
                # wins too: extreme is the top of the ladder, so a day that opens
                # there can never escalate and would otherwise be described by
                # whichever bar happened to come first. One-directional, so an
                # equal bar leaves the description alone.
                if here > there or (here == there and size[i] > size[peak]):
                    peak = i
            row = frame.iloc[peak]
            flag = row.get("overnight", False)
            overnight = bool(flag) if pd.notna(flag) else False
            rows.append({
                "event_id": f"{block_id(block).replace(':', '_')}:"
                            f"{int(frame['hour_utc'].iat[positions[0]])}",
                "asset_id": block_id(block), "block": block,
                "hour_utc": int(frame["hour_utc"].iat[positions[0]]),
                "peak_hour_utc": int(row.hour_utc),
                "z_resid": float(row.z_resid), "e_resid": float(row.e_resid),
                "co_block": 0.0, "r": float(row.r), "beta_block": float("nan"),
                "repeat_count": len(positions) - 1, "tier": str(row.tier),
                "basis": BLOCK_BASIS,
                "sigma_lt": float(row.sigma_lt), "close": float("nan"),
                "n_members": int(row.n_members),
                # Carried on the row rather than looked up at delivery time: the
                # delivery layer reads the events table and nothing else, and a
                # block's own figure is a median, so it names nobody. The
                # reader's next question is always which members did it.
                "leaders": _leaders(gap_panel if overnight and gap_panel is not None
                                    else panel, columns, int(row.hour_utc)),
                "overnight": overnight,
                "gap_kind": (str(row.get("gap_kind")) if overnight
                             and pd.notna(row.get("gap_kind")) else None),
                # What the block move actually beat. A block has one ladder, so
                # there is no basis to choose between: the date is simply when
                # this complex last moved together this hard.
                "record_since": (None if pd.isna(getattr(row, "level_since", None))
                                 else int(row.level_since)),
                # A block move is not put to the two confirmations an asset move
                # is. Corrado's rank test and the OU fit both ask whether ONE
                # instrument's residual behaves idiosyncratically, and a block
                # factor is the thing they measure idiosyncrasy against - asking
                # them here would be asking whether the yardstick is unusual by
                # its own yardstick.
                "rank_confirms": None, "ou_reverts": None,
            })
    if not rows:
        return pd.DataFrame(columns=[
            "event_id", "asset_id", "block", "hour_utc", "peak_hour_utc",
            "z_resid", "e_resid", "co_block", "r", "beta_block", "repeat_count",
            "tier", "basis", "sigma_lt", "close", "n_members", "leaders",
            "overnight", "gap_kind", "record_since", "rank_confirms", "ou_reverts"])
    # Stable, and tie-broken by name. Pandas sorts with quicksort by default,
    # so two blocks firing in the same hour came out in an arbitrary order that
    # depended on the length of the input - and the collapse then took whichever
    # of them happened to land first as the day's anchor. Nothing about that was
    # visible until a run over a shorter history picked the other one.
    return pd.DataFrame(rows).sort_values(
        ["hour_utc", "asset_id"], kind="mergesort").reset_index(drop=True)


def _leaders(panel: pd.DataFrame, columns: list[str], hour_utc: int,
             limit: int = 4) -> str:
    """The members that moved most in the hour, biggest first, as tickers.

    In the only form that can be checked - a ticker and a percentage - because
    "the block moved" is a claim about a median and the reader is entitled to
    see the instruments it was taken across.
    """
    if not columns or hour_utc not in panel.index:
        return ""
    row = panel.loc[hour_utc, columns].dropna()
    biggest = sorted(((str(a), float(v)) for a, v in row.items()),
                     key=lambda pair: -abs(pair[1]))[:limit]
    return ", ".join(f"{asset_id.split(':')[-1]} {value * 100:+.2f}%"
                     for asset_id, value in biggest)

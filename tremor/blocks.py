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

WHAT IT IS NOT. It is not an aggregation of member alerts - that already exists
in saed.aggregate_block_alerts and answers a different question ("several
members fired at once"). This fires on the block's own move whether or not any
member cleared its own threshold, which is exactly the case that was silent.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tremor import cross_section, persistence, severity, windows
from tremor.basket import Basket
from tremor.sessions import EXCHANGE_TZ

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


def frames(basket: Basket, panel: pd.DataFrame, sigma_panel: pd.DataFrame,
           cache: "pd.DataFrame | None" = None) -> "dict[str, pd.DataFrame]":
    """One scored frame per block, shaped like an instrument's.

    Shaped like an instrument's on purpose: it then goes through the same
    severity ladder, the same retention check and the same routing as everything
    else, and the delivery layer has one kind of row to render rather than two.
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
        frame["sigma_lt_resid"] = (frame["e_resid"].shift(1)
                                   .rolling(windows.SIGMA_LT_BARS,
                                            min_periods=windows.SIGMA_LT_MIN_BARS)
                                   .std(ddof=1))
        levels = None
        if cache is not None and not cache.empty:
            from tremor import ladder

            if ladder.covers(cache, block_id(block), "block", frame["hour_utc"]):
                levels = ladder.levels_for(cache, block_id(block), "block",
                                           frame["hour_utc"])
        frame = severity.annotate(frame, column="z_resid", fallback=None,
                                  tier_column="tier", levels=levels)
        frame["basis"] = pd.Series(BLOCK_BASIS, index=frame.index,
                                   dtype="string").where(frame["tier"].notna())
        # A block whose members all keep the US session has its day closed by
        # that session; one that trades around the clock has a UTC day. Mixed
        # blocks do not occur - the configuration groups by what a thing is, and
        # what a thing is decides where it trades.
        tz = EXCHANGE_TZ if all(
            a.session_template == "us_equity"
            for a in basket.assets if a.asset_id in columns) else None
        out[block] = persistence.annotate(frame, tz)
    return out


def events_frame(scored: "dict[str, pd.DataFrame]", basket: Basket,
                 panel: pd.DataFrame) -> pd.DataFrame:
    """The block events, in the same columns an asset event carries.

    Only the push tiers. A block moving at the routine or notable level is the
    ordinary background of a market - some block is always the one that moved
    most - and a digest line for it every fortnight would say nothing. The two
    rare tiers are the ones that mean "this whole complex repriced".
    """
    from tremor.routing import PUSH_TIERS

    members, _ = cross_section._block_members(panel, basket)
    rows = []
    for block, frame in scored.items():
        fired = frame[frame["tier"].isin(PUSH_TIERS)]
        columns = [c for c in members.get(block, []) if c in panel.columns]
        for row in fired.itertuples(index=False):
            rows.append({
                "event_id": f"{block_id(block).replace(':', '_')}:{int(row.hour_utc)}",
                "asset_id": block_id(block), "block": block,
                "hour_utc": int(row.hour_utc), "peak_hour_utc": int(row.hour_utc),
                "z_resid": float(row.z_resid), "e_resid": float(row.e_resid),
                "co_block": 0.0, "r": float(row.r), "beta_block": float("nan"),
                "repeat_count": 0, "tier": str(row.tier), "basis": BLOCK_BASIS,
                "sigma_lt": float(row.sigma_lt), "close": float("nan"),
                "n_members": int(row.n_members),
                # Carried on the row rather than looked up at delivery time: the
                # delivery layer reads the events table and nothing else, and a
                # block's own figure is a median, so it names nobody. The
                # reader's next question is always which members did it.
                "leaders": _leaders(panel, columns, int(row.hour_utc)),
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
            "rank_confirms", "ou_reverts"])
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

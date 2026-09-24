"""The overnight gap, scored as its own reading.

A US fund stops at 16:00 New York time and opens again at 09:30, and whatever
the world did in between arrives as one jump: the first print against the last.
The hourly series measures its first bar from that bar's OWN open
(tremor.returns), so the jump used to be thrown away - and it is not small.
Measured on the stored bars, a median 43% of day-to-day price variance across
the US funds happens overnight, from 25% for XLU to 70% for CPER, and SPY's
biggest opening gaps are 2020-03-16, 2008-10-24, 2015-08-24 and 2024-08-05:
real events, mostly over before the first bar existed.

A currency pair has the same thing once a week: it trades from Sunday 17:00 to
Friday 17:00 New York time, and the weekend is its gap. Small most weeks and
not on the weekends that matter - 2025-02-02, the Canada tariffs, USD/CAD
+1.44%; 2020-03-15, the Sunday Fed cut; 2017-04-23, the French first round.
With history from 2003 each pair has some 1,200 of them, and on the daily
calendar below its scale exists from 2012-13 (USD/CNH, younger, from 2022).
Crypto never closes and has no gap to score.

WHY NOT FOLD IT INTO THE FIRST BAR'S r. The 09:00 New York bar already carries
31% of the US funds' events and 39% of their pushes. Any single number built
from the gap and the first hour together reshapes the busiest hour in the system,
and a first hour that undoes the gap cancels it. So the first bar is left exactly
as it was, and the gap is a SECOND reading on the same row: it may claim that
row's event when it is the rarer of the two, and otherwise changes nothing.

HOW IT IS SCORED - the same machinery as everything else, on a series of one row
per session per fund:

  - its own scale: the gap over this fund's own long-run sigma of gaps. A night
    is worth anything from 1.6 trading hours (XLU) to 18.8 (DBB), median 6.2,
    so no shared divisor could stand in for this;
  - its own block factor, because every US fund opens at the same instant and
    every pair on the same Sunday evening, so "did its peers gap too" is as
    clean a question then as at any hour;
  - the same residual, BMP standardisation and ladders an hour gets.

THE CALENDAR IS DAYS, AND THAT IS NOT A DETAIL. Every per-asset window is
written in bars for its template - `us_equity` remembers 600 bars and reaches
back 3,600, which is 86 trading days. The same numbers read as SESSIONS are 2.4
years and fourteen years, so a gap pass inheriting the fund's template would
learn "normal" from a decade and a half and could never be exact on a warm run.
It runs on windows.DAILY_SERIES instead, the 83-day memory the VIX already uses.

AND IT IS NEVER WARM. The whole morning history is some six thousand rows per
fund and a quarter of a million in total, which scores from scratch in about a
second. So this reads the UNTRIMMED metrics every run (saed.main hands them over
before plan_frames cuts the hourly series down) and is exact by construction:
there is no slice of it to keep in step with anything.

WHAT A GAP-CLAIMED EVENT SAYS. Its `r` is the gap as a price move, its
`sigma_lt` the fund's usual gap, its record date the last gap at least this big
- all read by the delivery layer through the `overnight` flag, never by
pretending the night was an hour. Its retention is the event study's own
definition with the night as interval zero: (gap + everything after it to the
horizon) / gap, so "held at the next close" means the same thing it means for
every other event.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from tremor import blocks, cross_section, ewma, persistence, residuals, severity, windows
from tremor.basket import Basket

log = logging.getLogger("tremor.gaps")

# One row a session. See the module docstring for why this and not the fund's own.
TEMPLATE = windows.DAILY_SERIES

# The adaptive thresholds are computed by score_residuals whatever it is given
# and read by nothing that decides an event. One trading day per row.
W_GAP = windows.w_asset(1)

# What a gap-claimed row takes from the gap reading. Everything the event
# automaton and the message read off a bar - and nothing the retention of OTHER
# events sums over, which is why the overlay is applied to a copy of the frame.
OVERLAID = (("r", "sigma_lt", "e_resid", "co_block", "beta_block", "z_resid",
             "z_resid_bmp", "tier", "basis", "rank_confirms", "ou_reverts",
             "n_members", f"{severity.LEVEL_PREFIX}_since")
            + severity.LEVEL_COLUMNS
            + severity.level_columns("abs_level") + ("abs_level_since",))


@dataclass(frozen=True)
class GapPass:
    assets: "dict[str, pd.DataFrame]"   # asset_id -> scored gap rows
    blocks: "dict[str, pd.DataFrame]"   # block -> scored block gap rows
    panel: pd.DataFrame                 # hour x asset, the gaps - for leaders


def mornings(basket: Basket, metrics: "dict[str, pd.DataFrame]"
             ) -> "dict[str, pd.DataFrame]":
    """One row per scored gap per fund or currency pair, shaped like an
    instrument's frame. Crypto never closes and has none.

    `r` is the gap, so the residual machinery reads it without knowing, and
    `sigma_lt` is the fund's long-run spread of gaps, causal like every other
    sigma here (ewma.long_run_sigma shifts inside).
    """
    out: dict[str, pd.DataFrame] = {}
    for asset in basket.instruments:
        if asset.session_template not in ("us_equity", "fx_continuous"):
            continue
        frame = metrics.get(asset.asset_id)
        if frame is None or frame.empty or "gap" not in frame:
            continue
        rows = frame.loc[frame["gap"].notna(), ["hour_utc", "close", "gap"]]
        if rows.empty:
            continue
        rows = rows.rename(columns={"gap": "r"}).reset_index(drop=True)
        rows["sigma_lt"] = ewma.sigma_lt(rows["r"], TEMPLATE).to_numpy()
        rows["asset_id"] = asset.asset_id
        out[asset.asset_id] = rows
    return out


def score(basket: Basket, metrics: "dict[str, pd.DataFrame]", tiers) -> GapPass:
    """The whole morning history, scored from scratch.

    `tiers` is saed's own ladder chain (abnormal and absolute, combined, with the
    rank test's veto), passed in rather than imported so the two cannot drift:
    a gap is put to exactly the questions an hour is.
    """
    started = time.monotonic()
    morning = mornings(basket, metrics)
    if not morning:
        return GapPass({}, {}, pd.DataFrame())

    panel = cross_section.build_panel(morning, "r")
    sigma_panel = cross_section.build_panel(morning, "sigma_lt")
    factors = cross_section.block_factors(panel, basket, sigma_panel)
    by_id = {a.asset_id: a for a in basket.instruments}

    scored: dict[str, pd.DataFrame] = {}
    for asset_id, frame in morning.items():
        own = None
        if asset_id in factors and factors[asset_id].notna().any():
            own = factors[asset_id]
        with_residuals = residuals.residuals(by_id[asset_id], frame, own,
                                             template=TEMPLATE)
        scored[asset_id] = residuals.score_residuals(with_residuals, W_GAP)
    scored = residuals.standardise_cross_section(scored)
    scored = tiers(scored, {a.asset_id: a.block for a in basket.instruments})
    # The OU fit asks whether a residual reverts within a few BARS, and its
    # windows are written for an hourly series. On one row a session it would
    # answer a different question under the same name, so it says nothing.
    scored = {aid: frame.assign(ou_reverts=pd.array([pd.NA] * len(frame),
                                                    dtype="boolean"))
              for aid, frame in scored.items()}

    block_scored = blocks.frames(basket, panel, sigma_panel, template=TEMPLATE,
                                 retention=False)
    log.info("overnight gaps: %d instruments, %d sessions scored in %.1fs",
             len(scored), sum(len(f) for f in scored.values()),
             time.monotonic() - started)
    return GapPass(scored, block_scored, panel)


def overlay(frame: pd.DataFrame, gap: "pd.DataFrame | None",
            tz_name: "str | None" = None,
            last_day_closed: bool = False) -> pd.DataFrame:
    """The hourly frame with each gap that out-ranks its first bar written onto it.

    Strictly out-ranks: at the same tier the first hour keeps the row, so a
    morning the hourly series already reported is not re-described. Every other
    row is untouched, and so is every row when the gap fired nothing.

    Retention on a claimed row is (gap + CAR over the hourly bars from the first
    one to the horizon) / gap - the night is interval zero. Raw on `r`, abnormal
    on `e_resid`, with the same too-small-to-divide floor, measured against the
    gap's own residual spread.
    """
    out = frame.copy()
    out["overnight"] = False
    if gap is None or gap.empty or frame.empty or "tier" not in gap:
        return out
    fired = gap[gap["tier"].notna()].set_index("hour_utc")
    if fired.empty:
        return out

    hours = frame["hour_utc"].to_numpy(dtype="int64")
    here = np.flatnonzero(np.isin(hours, fired.index.to_numpy(dtype="int64")))
    if not here.size:
        return out
    claim = fired.reindex(hours[here])
    own = severity.rank(frame["tier"].iloc[here].reset_index(drop=True)).fillna(0)
    theirs = severity.rank(claim["tier"].reset_index(drop=True)).fillna(0)
    wins = (theirs > own).to_numpy(dtype=bool)
    if not wins.any():
        return out
    rows = here[wins]
    labels = frame.index[rows]
    claim = claim.iloc[wins]

    for column in OVERLAID:
        if column in claim and column in out:
            out.loc[labels, column] = claim[column].to_numpy()
    out.loc[labels, "overnight"] = True

    for horizon in persistence.HORIZONS:
        offsets = (persistence.next_close_offsets if horizon == persistence.SETTLED
                   else persistence.today_close_offsets)(frame, tz_name, last_day_closed)
        for column, gap_column, name in (("e_resid", "e_resid", f"retention_{horizon}"),
                                         ("r", "r", f"retention_raw_{horizon}")):
            if column not in frame or gap_column not in claim:
                continue
            car = persistence.forward_car_variable(
                frame[column].to_numpy(dtype="float64"), offsets)[rows]
            night = claim[gap_column].to_numpy(dtype="float64")
            floor = (persistence.MIN_DENOMINATOR_SIGMAS
                     * claim["sigma_lt_resid"].to_numpy(dtype="float64")
                     if "sigma_lt_resid" in claim else np.zeros(len(rows)))
            usable = (np.isfinite(night) & np.isfinite(car)
                      & (np.abs(night) > np.fmax(np.nan_to_num(floor), 1e-12)))
            out.loc[labels, name] = np.divide(night + car, night,
                                              out=np.full(len(rows), np.nan),
                                              where=usable)
    return out

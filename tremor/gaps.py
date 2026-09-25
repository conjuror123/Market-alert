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

EACH KIND OF CLOSE HAS ITS OWN USUAL SIZE. A fund's gap follows a weeknight or
a weekend, and a Monday's is typically 1.17x a Tuesday's; one pooled yardstick
would make every Monday a little more unusual than it is. So each row carries
its `gap_kind`, and both readings are judged against their kind: the raw gap's
`sigma_lt` is the pooled level times this kind's long-run share of it
(residuals.kind_scale), and the residual takes the same ratio of its own into
its divisor. See windows.GAP_KIND_MEMORY_SESSIONS. A pair's gap is always the
weekend, so for FX the ratio is one.

A gap after a midweek holiday is not scored at all. There are two or three a
year, too few to learn what one usually is, and borrowing the weekend's
yardstick made them fire three times their share. That morning's first hour is
judged as it always was.

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
             "n_members", f"{severity.LEVEL_PREFIX}_since", "gap_kind")
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
    `sigma_lt` is the fund's long-run spread of gaps OF THIS KIND - the pooled
    spread times `gap_scale`, this kind's share of it - causal like every other
    sigma here (ewma.long_run_sigma shifts inside). `gap_kind` says what kind
    of close came before: "night" or "weekend". A gap after a holiday is left
    out - see the module docstring.
    """
    out: dict[str, pd.DataFrame] = {}
    for asset in basket.instruments:
        if asset.session_template not in ("us_equity", "fx_continuous"):
            continue
        frame = metrics.get(asset.asset_id)
        if frame is None or frame.empty or "gap" not in frame:
            continue
        scored = frame["gap"].notna().to_numpy()
        if not scored.any():
            continue
        # The bar before a scored gap is the last bar of the previous session -
        # returns.overnight_gaps leaves the gap unscored where it is not - so
        # its date is the date the market last closed.
        before = frame["hour_utc"].shift(1).to_numpy()[scored]
        rows = frame.loc[scored, ["hour_utc", "close", "gap"]]
        rows = rows.rename(columns={"gap": "r"}).reset_index(drop=True)
        rows["gap_kind"] = (gap_kinds(rows["hour_utc"].to_numpy(), before)
                            if asset.session_template == "us_equity"
                            else "weekend")
        rows = rows[rows["gap_kind"] != "holiday"].reset_index(drop=True)
        if rows.empty:
            continue
        rows["gap_scale"] = residuals.kind_scale(rows["r"], rows["gap_kind"])
        rows["sigma_lt"] = (ewma.sigma_lt(rows["r"], TEMPLATE).to_numpy()
                            * rows["gap_scale"].to_numpy())
        rows["asset_id"] = asset.asset_id
        out[asset.asset_id] = rows
    return out


def gap_kinds(hours, before) -> np.ndarray:
    """What kind of close each gap followed, from the New York dates of the
    session's first bar and of the bar before it: "night" when the sessions are
    consecutive days, "weekend" when a Saturday lies between (a long weekend
    too), "holiday" for any other longer close - Thanksgiving's Friday, a
    Wednesday Fourth of July. The last are not scored; see mornings."""
    def dates(values):
        return (pd.to_datetime(pd.Series(values, dtype="float64"), unit="s", utc=True)
                .dt.tz_convert("America/New_York").dt.tz_localize(None)
                .dt.normalize())

    today, last = dates(hours), dates(before)
    days = (today - last).dt.days.to_numpy()
    # Days from the last close to the next Saturday, and whether that lands
    # strictly before today.
    to_saturday = (5 - last.dt.weekday.to_numpy()) % 7
    weekend = (to_saturday >= 1) & (to_saturday < days)
    return np.where(days <= 1, "night", np.where(weekend, "weekend", "holiday"))


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
                                             template=TEMPLATE,
                                             kinds=frame["gap_kind"].to_numpy())
        scored[asset_id] = residuals.score_residuals(with_residuals, W_GAP)
    scored = residuals.standardise_cross_section(scored)
    scored = tiers(scored, {a.asset_id: a.block for a in basket.instruments})
    # The OU fit asks whether a residual reverts within a few BARS, and its
    # windows are written for an hourly series. On one row a session it would
    # answer a different question under the same name, so it says nothing.
    scored = {aid: frame.assign(ou_reverts=pd.array([pd.NA] * len(frame),
                                                    dtype="boolean"))
              for aid, frame in scored.items()}

    # A block's move is a median of member gaps each over its member's usual gap
    # of the kind, so it arrives already judged by kind; it only needs the kind
    # itself, for the message. Every member shares the calendar, so the hour says it.
    block_scored = blocks.frames(basket, panel, sigma_panel, template=TEMPLATE,
                                 retention=False)
    kind_at = pd.concat([f.set_index("hour_utc")["gap_kind"] for f in morning.values()])
    kind_at = kind_at[~kind_at.index.duplicated()]
    block_scored = {name: frame.assign(gap_kind=kind_at.reindex(
                        frame["hour_utc"]).to_numpy(dtype=object))
                    for name, frame in block_scored.items()}
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
    out["gap_kind"] = pd.array([pd.NA] * len(out), dtype="string")
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

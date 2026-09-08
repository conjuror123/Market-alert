"""Did the move stay? Permanent and transitory price impact.

A large move and a move worth hearing about are not the same thing, and the
difference is not size. Prices move for two reasons, and the literature
separates them cleanly: impact from someone needing liquidity is TRANSITORY
and reverses, impact from information is PERMANENT and continues. Measured,
the permanent component runs at roughly half to three-quarters of the peak
temporary one, so the split is large rather than a rounding effect - and
reversals are larger for less liquid instruments and for the biggest initial
moves, which is exactly the population a detector selects.

That makes "biggest" the wrong headline on its own. What this module adds is
the second half of the sentence: of the abnormal move the detector fired on,
how much of it was still there some hours later.

The measure is the event study's own. Retention at horizon h is the cumulative
abnormal return from the event bar through h bars later, divided by the
abnormal return on the event bar itself:

    retention(h) = CAR(0..h) / AR(0)

so 1.0 means the move held exactly, 0.0 means it gave everything back, above
1.0 means it kept going, and below 0.0 means it overshot the way back. Nothing
is being predicted here - it is measured after the fact, which is the whole
point of using it to gate the tiers that are not urgent. A move that has to
wait until Friday's digest can simply be looked at again first, and the ones
that reverted never make the page. Only the top tier, which interrupts
someone, has to be sent before the answer is known.

Two horizons, and both counted in THE ASSET'S OWN BARS rather than calendar
hours - the same reason the cooldown is (see tremor.saed). A closed market
cannot revert, and twenty-four bars is a comparable amount of trading in every
instrument where twenty-four hours is not.

Abnormal and raw are both recorded because they answer different questions.
The abnormal one is the honest test of what the detector claimed: it fired on
a move the market did not explain, so whether THAT survived is the thing to
check. The raw one is what a person sees on a chart, and it is the one to
quote when explaining the event to them. They disagree exactly when the market
moves the same way afterwards, which is worth being able to see.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# The two short check-ins, in the instrument's own bars. Two is "is it still
# there at all" and arrives while the move is still the thing you are thinking
# about; six is "did it survive the session".
BAR_HORIZONS: tuple[int, ...] = (2, 6)

# And the settled one, which is NOT a bar count. Twenty-four bars means one day
# in an instrument that trades round the clock and nearly four days in an ETF
# that trades six and a half hours - so the same number was asking a different
# question of each, and the answer arrived on a Thursday for a move that
# happened on Monday. The settled reading is now taken at the CLOSE OF THE NEXT
# TRADING DAY: one legible moment, the same sentence for every instrument, and
# its distance from the event depends on what time of day the event happened,
# which is the honest dependency rather than a hidden one.
SETTLED = "settled"
HORIZONS: tuple = BAR_HORIZONS + (SETTLED,)

# What counts as having held. Taken from the measured permanent share of price
# impact - roughly 51% to 73% of the peak - so a half is the bottom of the
# range at which the literature would still call the impact permanent, and it
# is a round enough number not to pretend to more precision than that.
HELD_MIN = 0.5

# Below this the denominator is too small to divide by and the ratio is noise
# rather than a measurement. Expressed against the instrument's own long-term
# sigma, not as an absolute return, because a hundredth of a percent means
# different things in SHY and in SOL.
MIN_DENOMINATOR_SIGMAS = 0.5


def forward_car(abnormal: np.ndarray, horizon: int) -> np.ndarray:
    """Cumulative abnormal return from each bar through `horizon` bars later.

    The last `horizon` bars get NaN rather than a truncated sum. A partial
    window would quietly answer a different question - "how much held over the
    three bars that happen to exist" - and at the end of history that is every
    event the live system has just produced.
    """
    values = np.asarray(abnormal, dtype="float64")
    running = np.concatenate([[0.0], np.nancumsum(np.nan_to_num(values))])
    n = values.size
    out = np.full(n, np.nan)
    reach = n - horizon
    if reach > 0:
        index = np.arange(reach)
        out[:reach] = running[index + horizon + 1] - running[index]
    return out


def next_close_offsets(frame: pd.DataFrame, tz_name: str | None = None) -> np.ndarray:
    """Bars from each bar to the LAST bar of the next day the instrument trades.

    "Next day" is the exchange's local day where there is an authoritative
    calendar for it, and the UTC day otherwise - a currency pair has no daily
    close to speak of, so its day is the one the clock gives. Either way the
    offset is read off the bars actually present, so a holiday, a half day or a
    weekend simply is not a day: the next one is whatever traded next.

    The final day of history gets -1, meaning no answer yet, which is the same
    thing the fixed-horizon path says by running off the end of the array.
    """
    hours = pd.to_datetime(frame["hour_utc"], unit="s", utc=True)
    if tz_name:
        hours = hours.dt.tz_convert(tz_name)
    days = hours.dt.date.to_numpy()

    # Positional index of the last bar of each day, and the order of the days.
    unique, first_index = np.unique(days, return_index=True)
    order = np.argsort(first_index)
    unique = unique[order]
    last_of_day = {}
    positions = np.arange(len(days))
    for day in unique:
        last_of_day[day] = int(positions[days == day].max())

    following = {day: unique[i + 1] if i + 1 < len(unique) else None
                 for i, day in enumerate(unique)}
    out = np.full(len(days), -1, dtype="int64")
    for i, day in enumerate(days):
        nxt = following.get(day)
        if nxt is not None:
            out[i] = last_of_day[nxt] - i
    return out


def forward_car_variable(abnormal: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    """forward_car with a per-bar horizon instead of one shared by every bar."""
    values = np.asarray(abnormal, dtype="float64")
    running = np.concatenate([[0.0], np.nancumsum(np.nan_to_num(values))])
    n = values.size
    out = np.full(n, np.nan)
    index = np.arange(n)
    reach = index + offsets
    usable = (offsets >= 0) & (reach < n)
    out[usable] = running[reach[usable] + 1] - running[index[usable]]
    return out


def retention(frame: pd.DataFrame, horizon, column: str = "e_resid",
              tz_name: str | None = None) -> pd.Series:
    """The share of the bar's move still standing `horizon` bars later.

    Undefined where the move being divided by is too small to be a move. That
    is not a hypothetical even among events: a bar can clear its return level
    on a small abnormal return when the peer spread that hour was tiny, and
    dividing by it would report a retention of forty rather than a reversal.
    """
    values = frame[column].to_numpy(dtype="float64")
    car = (forward_car_variable(values, next_close_offsets(frame, tz_name))
           if horizon == SETTLED else forward_car(values, int(horizon)))
    floor = MIN_DENOMINATOR_SIGMAS * frame["sigma_lt_resid"].to_numpy(dtype="float64") \
        if "sigma_lt_resid" in frame else np.zeros(values.size)
    usable = np.isfinite(values) & (np.abs(values) > np.maximum(floor, 1e-12))
    return pd.Series(np.divide(car, values, out=np.full(values.size, np.nan),
                               where=usable),
                     index=frame.index)


def annotate(frame: pd.DataFrame, tz_name: str | None = None) -> pd.DataFrame:
    """Adds retention at every horizon, abnormal and raw, to one asset's frame.

    `tz_name` is the exchange's timezone for an instrument whose day is a
    trading session rather than a clock day; leaving it None makes the settled
    horizon fall on the UTC day, which is what a round-the-clock instrument
    wants.
    """
    out = frame.copy()
    for horizon in HORIZONS:
        out[f"retention_{horizon}"] = retention(frame, horizon, "e_resid", tz_name)
        if "r" in frame:
            out[f"retention_raw_{horizon}"] = retention(frame, horizon, "r", tz_name)
    return out


RETENTION_COLUMNS: tuple[str, ...] = tuple(
    f"retention_{h}" for h in HORIZONS) + tuple(
    f"retention_raw_{h}" for h in HORIZONS)


def held(events: pd.DataFrame, horizon: int = HORIZONS[-1],
         minimum: float = HELD_MIN) -> pd.Series:
    """Whether each event's move was still standing, as a nullable boolean.

    Read off the series that matches what the event claimed. An event found
    because the market did not explain the move is tested on whether the
    ABNORMAL move survived; one found because the move was simply large is
    tested on the price itself. Using the abnormal series for both would
    reject a genuine market-wide move the instant the market came back with
    it - which, on a macro day, is most of them, and they are the events this
    channel exists to catch.

    NA is a third answer and not a quiet False. An event whose horizon has not
    elapsed yet - every event the live system just produced - has not failed
    the test, it has not taken it, and a caller that treats the two alike would
    drop exactly the newest events.
    """
    abnormal, raw = f"retention_{horizon}", f"retention_raw_{horizon}"
    if abnormal not in events and raw not in events:
        return pd.Series(pd.NA, index=events.index, dtype="boolean")

    value = events[abnormal] if abnormal in events \
        else pd.Series(np.nan, index=events.index)
    if "basis" in events and raw in events:
        value = value.where(events["basis"].ne("absolute"), events[raw])
    return (value >= minimum).where(value.notna(), pd.NA).astype("boolean")


def attach(events: pd.DataFrame, scored: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Copies each event's retention off the bar that earned its tier.

    Joined on (asset_id, hour) rather than by position: the events table is
    built from several assets' frames and has been through a groupby since, so
    its row order is not any single asset's bar order.

    The hour joined on is `peak_hour_utc`, not the opening hour, and the two
    differ whenever an event escalated inside its cooldown. Retention divides
    the forward move by the move it is retaining, so measuring it from an
    opening bar whose move was a fifth of a basis point - while the event is
    reported at the tier a later, far larger bar earned - yields ratios like
    12x that describe the mismatch rather than the market.
    """
    if events.empty:
        return events.assign(**{c: pd.Series(dtype="float64")
                                for c in RETENTION_COLUMNS})

    pieces = []
    for asset_id, frame in scored.items():
        annotated = frame if all(c in frame for c in RETENTION_COLUMNS) \
            else annotate(frame)
        keep = ["hour_utc"] + [c for c in RETENTION_COLUMNS if c in annotated]
        piece = annotated[keep].copy()
        piece["asset_id"] = asset_id
        pieces.append(piece)
    if not pieces:
        return events.assign(**{c: np.nan for c in RETENTION_COLUMNS})

    lookup = pd.concat(pieces, ignore_index=True)
    on = "peak_hour_utc" if "peak_hour_utc" in events else "hour_utc"
    merged = events.merge(lookup.rename(columns={"hour_utc": on}),
                          on=["asset_id", on], how="left",
                          validate="many_to_one")
    merged.index = events.index
    return merged

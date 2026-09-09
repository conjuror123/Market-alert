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

Three check-ins, and none of them is a number of hours. The two short ones are
counted in THE ASSET'S OWN BARS rather than calendar hours - the same reason the
cooldown is (see tremor.saed) - because a closed market cannot revert, and six
bars is a comparable amount of trading in every instrument where six hours is
not. The third is a moment rather than a distance: the close of the next day the
instrument trades.

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

# TWO CHECK-INS, AND NEITHER IS A NUMBER OF BARS. They used to be: two bars,
# six bars, and a settled reading. A bar count asks a different question of each
# instrument - six bars is most of a session in an ETF and a quarter of a day in
# crypto - and it gives the reader an appointment they cannot picture. Both are
# now moments on the instrument's own calendar, and both are sentences anyone
# can hold: how the move stood when THIS day closed, and how it stood when the
# NEXT one did.
TODAY = "today"
SETTLED = "settled"
HORIZONS: tuple = (TODAY, SETTLED)

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


def _day_bounds(frame: pd.DataFrame,
                tz_name: str | None = None) -> "tuple[np.ndarray, np.ndarray]":
    """Which day each bar belongs to, and the last bar of each day.

    Returned as (code per bar, last position per code), where the codes run in
    calendar order over the days actually present - so code + 1 is "the next day
    this instrument traded", holidays and weekends skipped by construction.

    Vectorised, and that is the whole point of the function existing. Both
    callers used to loop over the days and compare the entire day column against
    each one: at six thousand days and a hundred and forty-five thousand bars
    that is 870 million comparisons per call, four calls per instrument, and it
    was 96% of the cost of the hourly pipeline - eighteen seconds a call where
    this is under a tenth of one.

    The day itself is an integer, not a datetime.date. An object array of dates
    compares elementwise in Python; local midnight as nanoseconds since the
    epoch is one int64 per bar, unique per local day and ordered like the
    calendar, which is all either caller needs.
    """
    hours = pd.to_datetime(frame["hour_utc"], unit="s", utc=True)
    if tz_name:
        hours = hours.dt.tz_convert(tz_name)
    day = hours.dt.normalize().astype("int64").to_numpy()

    unique, codes = np.unique(day, return_inverse=True)
    positions = np.arange(len(day), dtype="int64")
    last = np.zeros(len(unique), dtype="int64")
    # Not simply "the last row of each run": the frame is sorted in practice but
    # nothing here requires it, and a maximum is right either way.
    np.maximum.at(last, codes, positions)
    return codes, last


def today_close_offsets(frame: pd.DataFrame, tz_name: str | None = None) -> np.ndarray:
    """Bars from each bar to the LAST bar of its own day.

    Zero on the closing bar itself, which is not a failure: a move made in the
    last hour of the day has nothing left of that day to hold through, and the
    message says so rather than reporting a ratio of one and calling it news.
    """
    if frame.empty:
        return np.zeros(0, dtype="int64")
    codes, last = _day_bounds(frame, tz_name)
    return last[codes] - np.arange(len(codes), dtype="int64")


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
    if frame.empty:
        return np.zeros(0, dtype="int64")
    codes, last = _day_bounds(frame, tz_name)
    positions = np.arange(len(codes), dtype="int64")
    out = np.full(len(codes), -1, dtype="int64")
    following = codes + 1
    known = following < len(last)
    out[known] = last[following[known]] - positions[known]
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
    if horizon == SETTLED:
        car = forward_car_variable(values, next_close_offsets(frame, tz_name))
    else:
        car = forward_car_variable(values, today_close_offsets(frame, tz_name))
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

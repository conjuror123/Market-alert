"""The jump detector: each reading against the half-year before it.

The jump test of Lee & Mykland (2008), "Jumps in Financial Markets: A New
Nonparametric Test and Jump Dynamics", Review of Financial Studies 21(6).
docs/manual.md, "The detector", says what each part does; docs/decisions.md why.

THE SCORE. z = r / sigma, sigma the instrument's bipower volatility over the
half-year BEFORE the reading (their eq. 8):

    sigma = sqrt(pi/2 * mean(|r_j| * |r_(j-1)|))

Products of neighbouring moves, so a jump in the window never multiplies with
itself and cannot inflate the yardstick it is later measured against.

THE WORD, from |z|: 6, 8.5, 12, 17 (LEVELS) - about sqrt(2) apart, each about
three times rarer than the one below.

THE WINDOW is half a year of calendar time for every instrument (a fund about
880 bars, a pair 3,130, a coin 4,380), inside the paper's valid range of
sqrt(252 n) to 252 n bars for n bars a day. A young series scores from the
paper's minimum and its rows are marked `young`.

THE GAP - a fund's night and weekend, a pair's weekend - is scored by the same
rules against the gaps of its own kind over the same half-year. A weekend is a
gap of 48 hours or more; a midweek holiday is a night.

BROKEN PRINTS. A reading beyond MISTAKE_SIGMA, and the stretch after it until
the price is back (STRETCH_BACK, STRETCH_BARS), is no reading at all. A move a
second source did not see (jump.verify) is left out too (without_unconfirmed).

EVENTS. An instrument's first flagged reading opens an event of 24 real hours
from when it was found (event_starts); its word is its rarest reading's, its
numbers its biggest's. Each reading carries what delivery reads: an id, its word
as `tier`, its channel, its event's start, `r`, `sigma_lt`, the rarest-since
reading (`since_utc`, `since_z`; rarest_since) and the check at the first NYSE
close after it was found (`check_utc`, `held`; held_at_close).

    python -m jump.jumps            reads data/jump/metrics, writes
                                      data/jump/jumps.parquet

The whole history is rescored every run, a few seconds, so the result is exact
by construction: there is no warm slice to keep in step.
"""
from __future__ import annotations

import argparse
import logging
import math
import os

import numpy as np
import pandas as pd
import yaml

from jump import atomic
from jump.basket import DEFAULT_BASKET_PATH, load_basket

log = logging.getLogger("jump.jumps")

DEFAULT_METRICS_DIR = os.path.join("data", "jump", "metrics")
DEFAULT_OUT = os.path.join("data", "jump", "jumps.parquet")

WORDS: tuple[str, ...] = ("noticeable", "high", "major", "extreme")

# The settings, overridable under `detector:` in config/basket.yaml.
WINDOW_DAYS = 182.6          # half a year of calendar time
# The four words' thresholds on |z|, in half-year sigmas: about sqrt(2) apart,
# each word about three times rarer than the one below, rounded.
LEVELS: "tuple[float, ...]" = (6.0, 8.5, 12.0, 17.0)

# Bars a day, per calendar, for the paper's minimum window.
BARS_PER_DAY: "dict[str, int]" = {"us_equity": 7, "fx_continuous": 24, "crypto_24_7": 24}


def bars_per_day(template: str) -> int:
    from jump.sessions import DAILY_SESSIONS, daily_bars_per_day

    if template in DAILY_SESSIONS:
        return daily_bars_per_day(template)
    return BARS_PER_DAY[template]

# The three readings, and each gap kind's minimum window from the paper's rule
# at one reading a day (nights: sqrt(252) -> 16) or a week (weekends: 7, their
# own recommendation for weekly data).
HOUR, NIGHT, WEEKEND = "hour", "night", "weekend"
GAP_MIN_COUNT: "dict[str, int]" = {NIGHT: 16, WEEKEND: 7}
WEEKEND_HOURS = 48.0     # a gap this long or longer spans a weekend

SECONDS_PER_DAY = 86400.0

# A reading this many sigmas out is a broken price, not a market: it is not a
# reading at all, and it never enters a yardstick. The biggest real ones in the
# history are PFF's open on 2015-08-24 (-207 as a weekend, +147 as an hour) and
# the Swiss franc's unpegging on 2015-01-15 (-118); the one above is Coinbase
# reopening on a $0.06 bitcoin print on 2017-04-15 (+1,526).
MISTAKE_SIGMA = 1000.0

# A broken price can STAY broken: USD/KRW quoted 2.3 instead of 1,294 for a
# stretch on 2024-01-01, and the hours inside it moved 30 sigma against a
# yardstick the stretch itself had not yet reached. So after an hour's reading
# beyond MISTAKE_SIGMA, the hours that follow are not readings either until the
# price is back within STRETCH_BACK of the break - the bar that brings it back
# included - for at most STRETCH_BARS. An isolated bad tick, in and straight
# out, takes nothing more with it. The second source does this better where
# there is one (jump.verify); this is all the futures and the LME's metals
# have.
STRETCH_BACK = 0.5
STRETCH_BARS = 24


def minimum_count(bars_per_day: int) -> int:
    """Lee & Mykland's smallest valid window: the smallest integer above
    sqrt(252 * bars a day). 42 for a seven-bar fund day, 78 for 24 bars."""
    return math.ceil(math.sqrt(252 * bars_per_day))


def settings(path: str = DEFAULT_BASKET_PATH) -> "tuple[float, tuple[float, ...]]":
    """(window_days, levels) from config/basket.yaml, or the defaults. The levels
    are the four words' thresholds on |z|, rising."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = (yaml.safe_load(f) or {}).get("detector") or {}
    except OSError:
        raw = {}
    window = float(raw.get("window_days", WINDOW_DAYS))
    found = tuple(float(x) for x in (raw.get("levels") or LEVELS))
    if not (window > 0 and len(found) == len(WORDS) and found[0] > 0
            and all(a < b for a, b in zip(found, found[1:]))):
        raise ValueError(f"detector settings out of range: window_days={window}, "
                         f"levels={list(found)}")
    return window, found


def levels() -> "tuple[float, ...]":
    """The four words' thresholds on |z|, as configured."""
    return settings()[1]


def half_year_sigma(hour_utc, values, window_days: float = WINDOW_DAYS,
                    min_count: int = 78) -> np.ndarray:
    """Per reading, the bipower volatility of the readings strictly before it
    within `window_days` of calendar time. NaN until `min_count` products are
    in the window, and on a reading whose own value is missing.

    A product pairs each reading with the one before it among the VALID
    readings, and is stamped at the later one. The product stamped at the
    reading being judged contains that reading, so the window is closed on the
    left: it holds every product stamped before this reading and none at it.
    """
    values = np.asarray(values, dtype="float64")
    hours = np.asarray(hour_utc, dtype="int64")
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(np.isfinite(values))
    if len(valid) < 2:
        return out
    v = np.abs(values[valid])
    products = pd.Series(v[1:] * v[:-1],
                         index=pd.to_datetime(hours[valid][1:], unit="s"))
    window = pd.Timedelta(seconds=window_days * SECONDS_PER_DAY)
    mean = products.rolling(window, min_periods=min_count, closed="left").mean()
    out[valid[1:]] = np.sqrt(np.pi / 2 * mean.to_numpy())
    return out


def trusted_sigma(hour_utc, values, window_days: float = WINDOW_DAYS,
                  min_count: int = 78,
                  stretch: bool = False) -> "tuple[np.ndarray, np.ndarray]":
    """half_year_sigma with the impossible readings taken out: (values, sigma),
    where a reading beyond MISTAKE_SIGMA is NaN in both and never reaches a
    later reading's yardstick either. Taking one out can only shrink the
    yardsticks after it, so the next pass may find another; it stops when a
    pass finds none. With `stretch` (consecutive hours, not gaps), the broken
    stretch after each one goes too (STRETCH_BARS)."""
    values = np.array(values, dtype="float64")
    while True:
        sigma = half_year_sigma(hour_utc, values, window_days, min_count)
        with np.errstate(divide="ignore", invalid="ignore"):
            wrong = np.abs(np.where(sigma > 0, values / sigma, np.nan)) > MISTAKE_SIGMA
        if not wrong.any():
            return values, sigma
        if stretch:
            wrong = wrong | broken_stretch(values, wrong)
        values[wrong] = np.nan


def broken_stretch(values: np.ndarray, breaks: np.ndarray) -> np.ndarray:
    """The readings after each break until the price is back within
    STRETCH_BACK of it (the returning one included), at most STRETCH_BARS."""
    out = np.zeros(len(values), dtype=bool)
    for k in np.flatnonzero(breaks):
        if out[k]:
            continue                 # the bar that comes back, or one inside
        size = abs(values[k])
        moved = values[k]
        for j in range(k + 1, min(k + 1 + STRETCH_BARS, len(values))):
            if np.isfinite(values[j]):
                moved += values[j]
            out[j] = True
            if abs(moved) <= STRETCH_BACK * size:
                break
    return out


def word_of(z, levels: "tuple[float, ...]" = LEVELS) -> np.ndarray:
    """The word each |z| reaches, or None below the bottom one."""
    magnitude = np.abs(np.asarray(z, dtype="float64"))
    out = np.full(len(magnitude), None, dtype=object)
    for name, level in zip(WORDS, levels):
        out[np.nan_to_num(magnitude, nan=0.0) >= level] = name
    return out


def score(frame: pd.DataFrame, template: str, window_days: float = WINDOW_DAYS,
          levels: "tuple[float, ...]" = LEVELS) -> pd.DataFrame:
    """Every hour of one instrument, with its sigma, z, word and whether its
    window was still shorter than `window_days` (`young`)."""
    frame = frame.sort_values("hour_utc").reset_index(drop=True)
    hours = frame["hour_utc"].to_numpy(dtype="int64")
    r, sigma = trusted_sigma(hours, frame["r"].to_numpy(dtype="float64"), window_days,
                             minimum_count(bars_per_day(template)), stretch=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(sigma > 0, r / sigma, np.nan)
    finite = np.isfinite(r)
    first = hours[finite][0] if finite.any() else 0
    young = (hours - first) < window_days * SECONDS_PER_DAY
    return pd.DataFrame({"hour_utc": hours, "reading": HOUR, "r": r, "sigma": sigma,
                         "z": z, "word": pd.array(word_of(z, levels), dtype="string"),
                         "young": young})


def gap_kinds(elapsed_hours, template: "str | None" = None) -> np.ndarray:
    """`weekend` for a gap spanning WEEKEND_HOURS or more, else `night`. A long
    weekend is a weekend; a midweek holiday (about 41.5 hours) is a night. A
    currency pair has no nights: its only midweek closures are Christmas and New
    Year's Day, two a year, and they are judged with its weekends."""
    elapsed = np.asarray(elapsed_hours, dtype="float64")
    if template == "fx_continuous":
        return np.full(elapsed.shape, WEEKEND, dtype=object)
    return np.where(elapsed >= WEEKEND_HOURS, WEEKEND, NIGHT)


def score_gaps(frame: pd.DataFrame, window_days: float = WINDOW_DAYS,
               levels: "tuple[float, ...]" = LEVELS,
               template: "str | None" = None) -> pd.DataFrame:
    """Every gap of one instrument, each judged against the earlier gaps of its
    own kind within `window_days`. `frame` holds the metrics' `hour_utc` and
    `gap`: the gap sits on the first bar after a close, NaN everywhere else and
    where it was left unscored (an unconfirmed dividend, a split, a missing bar
    at either end of the night)."""
    frame = frame.sort_values("hour_utc").reset_index(drop=True)
    hours = frame["hour_utc"].to_numpy(dtype="int64")
    gap = frame["gap"].to_numpy(dtype="float64")
    at = np.flatnonzero(np.isfinite(gap))
    at = at[at > 0]
    columns = ["hour_utc", "reading", "r", "sigma", "z", "word", "young"]
    if not len(at):
        return pd.DataFrame(columns=columns)
    when, move = hours[at], gap[at]
    kind = gap_kinds((hours[at] - hours[at - 1]) / 3600.0, template)
    sigma = np.full(len(at), np.nan)
    young = np.zeros(len(at), dtype=bool)
    for name, minimum in GAP_MIN_COUNT.items():
        mine = kind == name
        if not mine.any():
            continue
        move[mine], sigma[mine] = trusted_sigma(when[mine], move[mine], window_days, minimum)
        young[mine] = (when[mine] - when[mine][0]) < window_days * SECONDS_PER_DAY
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(sigma > 0, move / sigma, np.nan)
    kept = np.isfinite(move)
    when, kind, move, sigma, z, young = (x[kept] for x in (when, kind, move, sigma, z, young))
    return pd.DataFrame({"hour_utc": when, "reading": kind, "r": move, "sigma": sigma,
                         "z": z, "word": pd.array(word_of(z, levels), dtype="string"),
                         "young": young})[columns]


# An event is this many hours of real time from its first move being found. A
# move found later than that starts the next event.
EVENT_HOURS = 24


def event_starts(found, anchors=()) -> np.ndarray:
    """The start of the event each reading belongs to, for one instrument.

    ONE EVENT PER 24 HOURS OF REAL TIME, from the moment its first move was
    found - not per trading day, and not per candle: a fund's move at 15:00 New
    York and the next morning's gap are one event. A reading found inside an
    event's 24 hours joins it; the first one found after them starts the next.

    `anchors` are the starts of events already on the channel (delivery's
    state). They hold their 24 hours whatever the table now says - a first move
    corrected away does not slide the event later - and a reading that arrives
    late, found in the 24 hours BEFORE an anchored event, joins it rather than
    opening a second event that would overlap it."""
    found = np.asarray(found, dtype="int64")
    span = EVENT_HOURS * 3600
    anchors = np.sort(np.asarray(list(anchors), dtype="int64"))
    starts = np.full(len(found), -1, dtype="int64")
    for a in anchors:
        starts[(found >= a) & (found < a + span) & (starts < 0)] = a
    current = None
    for i in np.argsort(found, kind="stable"):
        if starts[i] >= 0:
            continue
        f = int(found[i])
        if current is not None and f < current + span:
            starts[i] = current
            continue
        ahead = anchors[(anchors > f) & (anchors - span <= f)]
        if len(ahead):
            starts[i] = int(ahead[0])
            continue
        current = f
        starts[i] = f
    return starts


# RAREST SINCE. How close an earlier move must come, as a share of this
# one's size in sigma, to count as at least as rare: a 5.0 sigma move is matched
# by 4.75 and up, and by anything bigger. The reader's choice: an exact record
# would pass over a 4.9 half a year ago to name a 5.0 two years ago, and "rarest
# in half a year" is the truer answer to "when did it last do this".
RARE_SHARE = 0.95


def rarest_since(scored: pd.DataFrame, bottom: float = LEVELS[0],
                 share: float = RARE_SHARE) -> pd.DataFrame:
    """Adds `since_utc` and `since_z` to one instrument's readings: the most
    recent EARLIER reading OF THE SAME KIND, in the same direction, at least
    `share` of this one's size in sigma - or NA where the whole record has
    none. Hours against hours, nights against nights, weekends against
    weekends: a reading's sigma is its own kind's, so only within a kind do two
    sizes in sigma describe comparable moves."""
    hours = scored["hour_utc"].to_numpy(dtype="int64")
    z = scored["z"].to_numpy(dtype="float64")
    kinds = scored["reading"].to_numpy()
    since_hour = np.full(len(z), -1, dtype="int64")
    since_z = np.full(len(z), np.nan)
    for kind in (HOUR, NIGHT, WEEKEND):
        at = np.flatnonzero(kinds == kind)
        at = at[np.argsort(hours[at], kind="stable")]
        since_hour[at], since_z[at] = matches(hours[at], z[at], share, bottom)
    return scored.assign(
        since_utc=pd.arrays.IntegerArray(since_hour, since_hour < 0), since_z=since_z)


def matches(hours, z, share: float = RARE_SHARE, bottom: float = LEVELS[0]
            ) -> "tuple[np.ndarray, np.ndarray]":
    """For each reading, in time order, the most recent EARLIER one in the same
    direction whose size is at least `share` of its own: (its hour, its z), or
    (-1, NaN) where there is none.

    Readings below the bottom word are left unanswered - no message quotes
    them - and nothing smaller than share x bottom can answer a flagged one, so
    only those are kept as candidates. A stack per direction holds the
    candidates nothing at least as big has come after since, so their sizes
    fall towards the top; the answer is the topmost one still big enough, found
    by bisection."""
    import bisect

    hours = np.asarray(hours, dtype="int64")
    z = np.asarray(z, dtype="float64")
    out_hour = np.full(len(z), -1, dtype="int64")
    out_z = np.full(len(z), np.nan)
    floor = share * bottom
    stacks = {1: ([], [], []), -1: ([], [], [])}      # -size, hour, z; oldest first
    with np.errstate(invalid="ignore"):
        candidates = np.flatnonzero(np.abs(z) >= floor)
    for i in candidates:
        value = z[i]
        size, sign = abs(value), (1 if value > 0 else -1)
        negs, hrs, zs = stacks[sign]
        if size >= bottom:
            at = bisect.bisect_right(negs, -share * size) - 1
            if at >= 0:
                out_hour[i], out_z[i] = hrs[at], zs[at]
        while negs and -negs[-1] <= size:
            negs.pop(), hrs.pop(), zs.pop()
        negs.append(-size), hrs.append(int(hours[i])), zs.append(float(value))
    return out_hour, out_z


def found_times(scored: pd.DataFrame, template: str) -> np.ndarray:
    """When each reading became judgeable: an hour once it has ended; a fund's
    gap with its first bar, once that bar has ended (the first half-hour, as
    the bar is stamped on the hour); a currency pair's weekend gap and a
    daily-session market's night at their open, which is the whole of them."""
    hours = scored["hour_utc"].astype("int64").to_numpy()
    fx_gap = (scored["reading"] != HOUR).to_numpy() & (template != "us_equity")
    return np.where(fx_gap, hours, hours + 3600)


def ended(scored: pd.DataFrame, template: str, now: int) -> pd.DataFrame:
    """Only the readings that can be judged at `now`, with `found_utc`, the
    moment each became judgeable (found_times). The run fires a few minutes
    past the hour, and the bar of the hour it is standing in holds those
    minutes only."""
    out = scored.assign(found_utc=found_times(scored, template))
    return out[out["found_utc"] <= int(now)].reset_index(drop=True)


def held_at_close(metrics: pd.DataFrame, flagged: pd.DataFrame, now: int
                  ) -> "tuple[np.ndarray, np.ndarray]":
    """HOW MUCH OF THE MOVE WAS STILL THERE AT THE FUNDS' CLOSE.

    Every reading is checked at the first NYSE close after it was found - for a
    coin and a currency pair too, so every check lands inside its week (the week
    turns just after its last close, jump.routing). A move found at the close,
    its closing hour, is checked at the next one. Returns (check_utc, held) per
    flagged reading: `held` is the share of the move still there - 1.0 held
    exactly, 1.2 kept going, -0.2 reversed past where it began - and NaN until
    the close has passed.

    Measured in the instrument's own prices, from the price before the move to
    the last bar ending at or before the close: an hour from its open, a gap
    from the last close before it."""
    from jump import routing

    frame = metrics.sort_values("hour_utc")
    hours = frame["hour_utc"].to_numpy(dtype="int64")
    r = np.nan_to_num(frame["r"].to_numpy(dtype="float64"))
    gap = np.nan_to_num(frame["gap"].to_numpy(dtype="float64"))
    # The move across a missing hour is never scored, but the price did move.
    hole = (np.nan_to_num(frame["hole"].to_numpy(dtype="float64"))
            if "hole" in frame else np.zeros(len(frame)))
    price = np.cumsum(r + gap + hole)              # log price, up to a constant
    ends = hours + 3600
    check = np.full(len(flagged), -1, dtype="int64")
    held = np.full(len(flagged), np.nan)
    for i, (hour, found, kind) in enumerate(zip(flagged["hour_utc"], flagged["found_utc"],
                                                flagged["reading"])):
        close = routing.next_close(int(found))
        if close is None:
            continue
        check[i] = close
        t = int(np.searchsorted(hours, int(hour)))
        # Only once the bars reach the close: a store that stops short of it -
        # a missed fetch - is not the price at the close, whatever time it is.
        if close > now or not len(ends) or ends[-1] < close:
            continue
        if t >= len(hours) or hours[t] != int(hour):
            continue
        move = r[t] if kind == HOUR else gap[t]
        base = price[t] - r[t] if kind == HOUR else price[t] - r[t] - gap[t]
        k = int(np.searchsorted(ends, close, side="right")) - 1
        if move and k >= t:
            held[i] = (price[k] - base) / move
    return check, held


# What decides the set of events, for detector_version: the score and the
# routing, and - as for the metrics' own version (jump.versioning.CONFIG_INPUTS)
# - which hours each market trades and which nights are contract rolls.
DETECTOR_CODE = ("jumps.py", "returns.py", "pipeline.py", "quality.py", "routing.py",
                 "windows.py", "sessions.py", "futures.py")
DETECTOR_DATA = (os.path.join("data", "jump", "rolls.csv"),)


def detector_version(root: "str | None" = None) -> str:
    """A hash of what decides the set of events: the detector's code, parsed
    with docstrings stripped, and the basket and its settings, parsed so that a
    comment does not count. A new value means the detector was updated, and
    delivery starts the current week over from that run (see
    price_monitor.jump_delivery)."""
    import hashlib
    import json

    from jump import versioning

    root = root or os.path.join(os.path.dirname(__file__), "..")
    digest = hashlib.sha256()
    for name in DETECTOR_CODE:
        with open(os.path.join(root, "jump", name), "rb") as f:
            digest.update(versioning._code(f.read()))
    for relative in DETECTOR_DATA:
        path = os.path.join(root, relative)
        if os.path.exists(path):
            with open(path, "rb") as f:
                digest.update(f.read())
    with open(os.path.join(root, "config", "basket.yaml"), encoding="utf-8") as f:
        basket = yaml.safe_load(f) or {}
    # Who serves an instrument and what it is called cannot change an event, so
    # moving a pair to another provider is not a detector update.
    for key in ("assets", "outside_basket"):
        basket[key] = [{k: v for k, v in item.items() if k not in NOT_DETECTOR}
                       for item in basket.get(key) or [] if isinstance(item, dict)]
    digest.update(json.dumps(basket, sort_keys=True, default=str).encode())
    return digest.hexdigest()[:12]


# Instrument fields detector_version leaves out: they decide where a bar comes
# from and how a message names it, never what the bar is judged to be.
NOT_DETECTOR = ("provider", "label")


BASIS = "jump"

# The hourly run fires at :05, so a move judgeable at :00 is found five
# minutes later - and a note opening at 00:05 takes the hour checked with it.
FOUND_TO_RUN = 300


def reading_ids(asset_id, reading, hour_utc) -> pd.Series:
    """jump:<asset_id>:<reading>:<hour> - the same id for the same reading on
    every run, whatever else changed."""
    return pd.Series("jump:" + pd.Series(asset_id).astype(str).to_numpy() + ":"
                     + pd.Series(reading).astype(str).to_numpy() + ":"
                     + pd.Series(hour_utc).astype("int64").astype(str).to_numpy(),
                     index=getattr(asset_id, "index", None))


def for_delivery(readings: pd.DataFrame) -> pd.DataFrame:
    """The columns the delivery layer reads, added to the flagged readings.

    `tier` is the word and `sigma_lt` the half-year sigma, so the message says
    "N×σ" with N = |z|. `event_start` groups the readings into 24-hour events
    from the table alone; delivery regroups them against the events already on
    the channel (event_starts' anchors). `since_utc`, `since_z` and
    `record_start` say how long it has been since a reading of its kind was at
    least this rare (rarest_since). `check_utc` and `held` say how much of the
    move was still there at the funds' close after it (held_at_close).
    """
    from jump import routing

    if readings.empty:
        return readings
    out = readings.copy()
    if "found_utc" not in out:
        out["found_utc"] = out["hour_utc"].astype("int64") + 3600
    if "event_start" not in out:
        out["event_start"] = 0
        for _, rows in out.groupby("asset_id"):
            out.loc[rows.index, "event_start"] = event_starts(rows["found_utc"])
    out["reading_id"] = reading_ids(out["asset_id"], out["reading"], out["hour_utc"])
    out["tier"] = out["word"].astype("string")
    out["basis"] = BASIS
    out["sigma_lt"] = out["sigma"]
    out["overnight"] = out["reading"] != HOUR
    out["gap_kind"] = out["reading"].where(out["overnight"])
    pushes = out["tier"].isin(routing.PUSH_TIERS).fillna(False).to_numpy(dtype=bool)
    out["channel"] = pd.array(np.where(pushes, routing.PUSH, routing.DIGEST), dtype="string")
    return out


def events(readings: pd.DataFrame) -> pd.DataFrame:
    """One row per 24-hour event: its biggest reading, whose word is the
    event's. For reports; delivery works from the readings."""
    if readings.empty:
        return readings
    size = readings["z"].abs()
    top = size.groupby([readings["asset_id"], readings["event_start"]]).idxmax()
    return readings.loc[top.to_numpy()].reset_index(drop=True)


def without_unconfirmed(metrics: pd.DataFrame, asset_id: str, doubts: dict) -> pd.DataFrame:
    """The metrics with the moves a second source did not see taken out: an
    hour's move (`close`) or a session's gap (`open`), NaN like a hole - not
    a reading, and not in any yardstick (jump.verify). The bar itself is
    untouched in the store."""
    hours = {check: {h for a, h, c in doubts if a == asset_id and c == check}
             for check in ("close", "open")}
    if not hours["close"] and not hours["open"]:
        return metrics
    out = metrics.copy()
    stamps = out["hour_utc"].astype("int64")
    out.loc[stamps.isin(hours["close"]), "r"] = np.nan
    out.loc[stamps.isin(hours["open"]), "gap"] = np.nan
    return out


def run(metrics_dir: str = DEFAULT_METRICS_DIR, basket_path: str = DEFAULT_BASKET_PATH,
        now: "int | None" = None, verified_path: "str | None" = None) -> pd.DataFrame:
    """Every instrument's flagged readings that can be judged at `now` - hour,
    night or weekend - each with the start of its 24-hour event."""
    import time

    from jump import verify

    now = int(time.time()) if now is None else int(now)
    basket = load_basket(basket_path)
    window, ladder = settings(basket_path)
    doubts = verify.unconfirmed(verified_path)
    parts = []
    for asset in basket.instruments:
        path = os.path.join(metrics_dir, f"{asset.file_stem}.parquet")
        if not os.path.exists(path):
            log.warning("no metrics for %s", asset.asset_id)
            continue
        stored = pd.read_parquet(path, columns=["hour_utc", "r", "hole", "gap"])
        metrics = without_unconfirmed(stored, asset.asset_id, doubts)
        readings = [score(metrics, asset.session_template, window, ladder),
                    score_gaps(metrics, window, ladder, asset.session_template)]
        scored = pd.concat([f for f in readings if not f.empty], ignore_index=True)
        if scored.empty:
            continue
        scored = rarest_since(ended(scored, asset.session_template, now), ladder[0])
        scored["record_start"] = int(metrics["hour_utc"].min())
        flagged = scored[scored["word"].notna()].sort_values("found_utc").reset_index(drop=True)
        flagged["event_start"] = event_starts(flagged["found_utc"])
        # The close check follows the price as stored: an unconfirmed move is
        # not a reading, but leaving it out of the path would shift every
        # price after it.
        check, held = held_at_close(stored, flagged, now)
        flagged["check_utc"] = pd.arrays.IntegerArray(check, check < 0)
        flagged["held"] = held
        flagged.insert(1, "asset_id", asset.asset_id)
        flagged.insert(2, "ticker", asset.ticker)
        flagged.insert(3, "block", asset.block)
        flagged.insert(4, "template", asset.session_template)
        parts.append(flagged)
    out = (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame())
    if out.empty:
        return out
    return for_delivery(out.sort_values(["hour_utc", "asset_id"]).reset_index(drop=True))


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="The Jump detector")
    parser.add_argument("--metrics-dir", default=DEFAULT_METRICS_DIR)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    flagged = run(args.metrics_dir)
    atomic.write_parquet(args.out, flagged, index=False)
    if flagged.empty:
        log.info("no flagged readings -> %s", args.out)
        return 0
    tops = events(flagged)
    log.info("%d events of 24 hours from %d flagged readings -> %s",
             len(tops), len(flagged), args.out)
    log.info("by reading: %s", tops["reading"].value_counts().to_dict())
    log.info("by word: %s", tops["word"].value_counts().to_dict())
    log.info("by channel: %s", tops["channel"].value_counts().to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

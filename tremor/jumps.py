"""The jump detector, stage 0: an hour's move against the half-year before it.

This is the jump test of Lee & Mykland (2008), "Jumps in Financial Markets: A New
Nonparametric Test and Jump Dynamics", Review of Financial Studies 21(6). Stage 0 is
exactly two rules and nothing else:

  1. THE SCORE. For each instrument and hour, z = r / sigma, where sigma is the
     instrument's bipower volatility over the half-year BEFORE this hour (their
     eq. 8):

         sigma = sqrt(pi/2 * mean(|r_j| * |r_(j-1)|))

     Products of neighbouring moves rather than squares, so one jump inside the
     window cannot inflate the yardstick it is later measured against: a jump
     multiplies with an ordinary move on either side of it, not with itself.

  2. THE WORD, from |z|: noticeable at 3.9, and each word above it sqrt(2) times
     bigger - 3.9, 5.5, 7.8, 11.0 - half an earthquake magnitude per word.

THE WINDOW IS CALENDAR TIME, THE SAME FOR EVERY INSTRUMENT. The paper counts
bars, because its statistics needs enough of them; what the window has to
follow is the volatility regime, which runs on the world's calendar and not on
a market's opening hours. Half a year sits inside the paper's valid range for
every calendar here: sqrt(252 n) to 252 n bars, n bars a day - a fund has about
880 bars in it (42 to 1,764), a currency pair about 3,130 and a coin about
4,380 (78 to 6,048).

A YOUNG SERIES IS SCORED FROM THE PAPER'S MINIMUM, not from a full half-year.
Thirteen funds' records begin on 2020-02-10, and a half-year warm-up would leave
them blind through March 2020. So scoring starts once the window holds the
paper's smallest valid count - the smallest integer above sqrt(252 n) - and the
window grows with the history until it is half a year long. Rows scored before
then are marked `young`, so a report can keep them apart.

THE GAP (stage 1b) is scored by the same two rules on its own readings. What
happens while a market is shut arrives as the jump from the last price before
the close to the first after it: a fund's night and weekend, a currency pair's
weekend. A night is judged against the nights of the half-year before it, a
weekend against the weekends: every reading of an instrument - hours, nights,
weekends - is read against the same half-year of the world's events. A weekend
is any gap spanning 48 hours or more (a long weekend included); a midweek
holiday is a night. The yardstick for weekends is the noisiest, 26 readings a
half-year, against the paper's minimum of 7 for once-a-week data; measured, it
is no worse than pooling them with the nights (see docs/decisions.md).

ONE EVENT PER INSTRUMENT PER DAY (stage 1), unless the day grows. The first flagged
reading of an instrument's day - its gap or an hour - opens the day. A later reading
the same day is kept only if it reaches a HIGHER word than anything kept before it
that day: a day that starts `noticeable` and turns `high` says so, and a second
`noticeable` - or a `high` after a `major` - is dropped. So a day holds at most four
events, each rarer than the last. The day is the fund's New York session date and
the UTC date for currency pairs and coins (sessions.day_tz, the same day the
running detector uses).

WHAT IS NOT HERE YET, deliberately: no channels, no
"biggest since" date, no held check, no time-of-day scale, no block or own-move
reading, no size floor. Each comes back as its own stage with its own logic.
The output is a table of every reading that reached `noticeable`.

    python -m tremor.jumps            reads data/tremor/metrics, writes
                                      data/tremor/jumps.parquet

Every instrument's whole history is scored on every run - a few seconds - so the
result is exact by construction: there is no warm slice to keep in step.
"""
from __future__ import annotations

import argparse
import logging
import math
import os

import numpy as np
import pandas as pd
import yaml

from tremor import atomic, sessions
from tremor.basket import DEFAULT_BASKET_PATH, load_basket

log = logging.getLogger("tremor.jumps")

DEFAULT_METRICS_DIR = os.path.join("data", "tremor", "metrics")
DEFAULT_OUT = os.path.join("data", "tremor", "jumps.parquet")

WORDS: tuple[str, ...] = ("noticeable", "high", "major", "extreme")

# The settings, overridable under `detector:` in config/basket.yaml.
WINDOW_DAYS = 182.6          # half a year of calendar time
NOTICEABLE_SIGMA = 3.9       # the bottom word, in half-year sigmas
STEP = math.sqrt(2)          # each word this many times bigger than the one below

# Bars a day, per calendar, for the paper's minimum window.
BARS_PER_DAY: "dict[str, int]" = {"us_equity": 7, "fx_continuous": 24, "crypto_24_7": 24}

# The three readings, and each gap kind's minimum window from the paper's rule
# at one reading a day (nights: sqrt(252) -> 16) or a week (weekends: 7, their
# own recommendation for weekly data).
HOUR, NIGHT, WEEKEND = "hour", "night", "weekend"
GAP_MIN_COUNT: "dict[str, int]" = {NIGHT: 16, WEEKEND: 7}
WEEKEND_HOURS = 48.0     # a gap this long or longer spans a weekend

SECONDS_PER_DAY = 86400.0


def minimum_count(bars_per_day: int) -> int:
    """Lee & Mykland's smallest valid window: the smallest integer above
    sqrt(252 * bars a day). 42 for a seven-bar fund day, 78 for 24 bars."""
    return math.ceil(math.sqrt(252 * bars_per_day))


def settings(path: str = DEFAULT_BASKET_PATH) -> "tuple[float, float, float]":
    """(window_days, noticeable_sigma, step) from config/basket.yaml, or the defaults."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = (yaml.safe_load(f) or {}).get("detector") or {}
    except OSError:
        raw = {}
    window = float(raw.get("window_days", WINDOW_DAYS))
    bottom = float(raw.get("noticeable_sigma", NOTICEABLE_SIGMA))
    step = float(raw.get("step", STEP))
    if not (window > 0 and bottom > 0 and step > 1):
        raise ValueError(f"detector settings out of range: window_days={window}, "
                         f"noticeable_sigma={bottom}, step={step}")
    return window, bottom, step


def levels(bottom: float = NOTICEABLE_SIGMA, step: float = STEP) -> "tuple[float, ...]":
    """The four words' thresholds on |z|: bottom * step**k."""
    return tuple(bottom * step ** k for k in range(len(WORDS)))


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


def word_of(z, bottom: float = NOTICEABLE_SIGMA, step: float = STEP) -> np.ndarray:
    """The word each |z| reaches, or None below the bottom one."""
    magnitude = np.abs(np.asarray(z, dtype="float64"))
    out = np.full(len(magnitude), None, dtype=object)
    for name, level in zip(WORDS, levels(bottom, step)):
        out[np.nan_to_num(magnitude, nan=0.0) >= level] = name
    return out


def score(frame: pd.DataFrame, template: str, window_days: float = WINDOW_DAYS,
          bottom: float = NOTICEABLE_SIGMA, step: float = STEP) -> pd.DataFrame:
    """Every hour of one instrument, with its sigma, z, word and whether its
    window was still shorter than `window_days` (`young`)."""
    frame = frame.sort_values("hour_utc").reset_index(drop=True)
    hours = frame["hour_utc"].to_numpy(dtype="int64")
    r = frame["r"].to_numpy(dtype="float64")
    sigma = half_year_sigma(hours, r, window_days, minimum_count(BARS_PER_DAY[template]))
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(sigma > 0, r / sigma, np.nan)
    finite = np.isfinite(r)
    first = hours[finite][0] if finite.any() else 0
    young = (hours - first) < window_days * SECONDS_PER_DAY
    return pd.DataFrame({"hour_utc": hours, "reading": HOUR, "r": r, "sigma": sigma,
                         "z": z, "word": pd.array(word_of(z, bottom, step), dtype="string"),
                         "young": young})


def gap_kinds(elapsed_hours) -> np.ndarray:
    """`weekend` for a gap spanning WEEKEND_HOURS or more, else `night`. A long
    weekend is a weekend; a midweek holiday (about 41.5 hours) is a night."""
    return np.where(np.asarray(elapsed_hours, dtype="float64") >= WEEKEND_HOURS,
                    WEEKEND, NIGHT)


def score_gaps(frame: pd.DataFrame, window_days: float = WINDOW_DAYS,
               bottom: float = NOTICEABLE_SIGMA, step: float = STEP) -> pd.DataFrame:
    """Every gap of one instrument, each judged against the earlier gaps of its
    own kind within `window_days`. `frame` holds the metrics' `hour_utc` and
    `gap`: the gap sits on the first bar after a close, NaN everywhere else and
    where it was left unscored (an unconfirmed dividend, a split, a missing bar
    before the close)."""
    frame = frame.sort_values("hour_utc").reset_index(drop=True)
    hours = frame["hour_utc"].to_numpy(dtype="int64")
    gap = frame["gap"].to_numpy(dtype="float64")
    at = np.flatnonzero(np.isfinite(gap))
    at = at[at > 0]
    columns = ["hour_utc", "reading", "r", "sigma", "z", "word", "young"]
    if not len(at):
        return pd.DataFrame(columns=columns)
    when, move = hours[at], gap[at]
    kind = gap_kinds((hours[at] - hours[at - 1]) / 3600.0)
    sigma = np.full(len(at), np.nan)
    young = np.zeros(len(at), dtype=bool)
    for name, minimum in GAP_MIN_COUNT.items():
        mine = kind == name
        if not mine.any():
            continue
        sigma[mine] = half_year_sigma(when[mine], move[mine], window_days, minimum)
        young[mine] = (when[mine] - when[mine][0]) < window_days * SECONDS_PER_DAY
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(sigma > 0, move / sigma, np.nan)
    return pd.DataFrame({"hour_utc": when, "reading": kind, "r": move, "sigma": sigma,
                         "z": z, "word": pd.array(word_of(z, bottom, step), dtype="string"),
                         "young": young})[columns]


def day_keys(hour_utc, tz_name: "str | None") -> np.ndarray:
    """The trading day each reading belongs to, as a sortable integer
    (days since 1970 in `tz_name`, or in UTC for None)."""
    moments = pd.to_datetime(np.asarray(hour_utc, dtype="int64"), unit="s", utc=True)
    if tz_name:
        moments = moments.tz_convert(tz_name)
    return (moments.tz_localize(None).normalize()
            - pd.Timestamp("1970-01-01")).days.to_numpy()


def one_a_day(flagged: pd.DataFrame, tz_name: "str | None") -> pd.DataFrame:
    """One instrument's flagged readings, cut to one event a day unless the day
    grows: a reading is kept only if its word is rarer than every reading kept
    before it that day. A gap is taken before the hour that shares its
    timestamp - the night happened first. Adds `day` and `escalation` (False
    for the day's first event, True for one that raised it)."""
    if flagged.empty:
        return flagged.assign(day=pd.Series(dtype="int64"),
                              escalation=pd.Series(dtype="bool"))
    order = flagged.assign(_after=(flagged["reading"] == HOUR).astype(int))
    order = order.sort_values(["hour_utc", "_after"]).drop(columns="_after")
    rank = order["word"].map({w: i for i, w in enumerate(WORDS)}).to_numpy()
    days = day_keys(order["hour_utc"], tz_name)
    keep = np.zeros(len(order), dtype=bool)
    escalation = np.zeros(len(order), dtype=bool)
    best, today = -1, None
    for i, (day, r) in enumerate(zip(days, rank)):
        if day != today:
            today, best = day, -1
        if r > best:
            keep[i], escalation[i], best = True, best >= 0, r
    return order.assign(day=days, escalation=escalation)[keep].reset_index(drop=True)


def run(metrics_dir: str = DEFAULT_METRICS_DIR, basket_path: str = DEFAULT_BASKET_PATH
        ) -> pd.DataFrame:
    """Every instrument's events: its flagged readings - hour, night or
    weekend - cut to one a day unless the day grows (one_a_day)."""
    basket = load_basket(basket_path)
    window, bottom, step = settings(basket_path)
    parts = []
    for asset in basket.instruments:
        path = os.path.join(metrics_dir, f"{asset.file_stem}.parquet")
        if not os.path.exists(path):
            log.warning("no metrics for %s", asset.asset_id)
            continue
        metrics = pd.read_parquet(path, columns=["hour_utc", "r", "gap"])
        readings = [score(metrics, asset.session_template, window, bottom, step),
                    score_gaps(metrics, window, bottom, step)]
        scored = pd.concat([f for f in readings if not f.empty], ignore_index=True)
        flagged = one_a_day(scored[scored["word"].notna()],
                            sessions.day_tz(asset.session_template))
        flagged.insert(1, "asset_id", asset.asset_id)
        flagged.insert(2, "ticker", asset.ticker)
        flagged.insert(3, "block", asset.block)
        flagged.insert(4, "template", asset.session_template)
        parts.append(flagged)
    out = (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame())
    return out.sort_values(["hour_utc", "asset_id"]).reset_index(drop=True) \
        if not out.empty else out


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 0 jump detector")
    parser.add_argument("--metrics-dir", default=DEFAULT_METRICS_DIR)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    flagged = run(args.metrics_dir)
    atomic.write_parquet(args.out, flagged, index=False)
    log.info("%d events (%d of them escalations) -> %s", len(flagged),
             int(flagged["escalation"].sum()) if not flagged.empty else 0, args.out)
    if not flagged.empty:
        log.info("by reading: %s", flagged["reading"].value_counts().to_dict())
        log.info("by word: %s", flagged["word"].value_counts().to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

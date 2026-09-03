"""MEALS hourly bar store: Parquet, one file per instrument.

Why Parquet rather than the NDJSON of the existing monitor: there a file is only
appended one row per hour and grows slowly, whereas here a basket of 23
instruments since 2021 amounts to roughly half a million bars, and they must be
read in full on every run of the PCA and the regressions. A typed columnar format
reads an order of magnitude faster and takes several times less space.

The time convention comes from §1.2 of the spec: hour_utc stores the bar's
OPENING moment, and the closing moment is t = hour_utc + 1 hour. This is the same
convention already used by the existing monitor's candle_store (open_time), so
the accumulated history imports without a shift.
"""
from __future__ import annotations

import os

import pandas as pd

from price_monitor.models import Candle

HOUR = 3600

# n_src - how many source bars folded into this hourly bar.
# Needed from phase 1 onward: for ETFs the first half hour of a session produces
# an hourly bar out of a single half-hourly one, and that is precisely the "first
# bar of the session" that §2.4 splits into the gap channel and the intra-hour
# return. Without this field it could not be told apart from a full hour.
SCHEMA = {
    "hour_utc": "int64",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
    "n_src": "int64",
}


def store_path(base_dir: str, file_stem: str) -> str:
    return os.path.join(base_dir, f"{file_stem}.parquet")


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series(dtype=dt) for name, dt in SCHEMA.items()})


def load(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return empty_frame()
    return pd.read_parquet(path).astype(SCHEMA).sort_values("hour_utc").reset_index(drop=True)


def write(path: str, frame: pd.DataFrame) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    frame.astype(SCHEMA).sort_values("hour_utc").reset_index(drop=True).to_parquet(
        path, index=False, compression="zstd")


def merge(path: str, frame: pd.DataFrame) -> int:
    """Idempotently brings the store to the union of what is already there and
    `frame`. On a matching hour_utc the new row wins: the source may have revised
    the bar, and the fresher version is more trustworthy. Returns the number of
    added rows (revisions of existing ones do not count).
    """
    if frame.empty:
        return 0
    existing = load(path)
    before = len(existing)
    combined = pd.concat([existing, frame.astype(SCHEMA)], ignore_index=True)
    combined = combined.drop_duplicates(subset="hour_utc", keep="last")
    write(path, combined)
    return len(combined) - before


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    if not candles:
        return empty_frame()
    return pd.DataFrame({
        "hour_utc": [c.open_time for c in candles],
        "open": [c.open for c in candles],
        "high": [c.high for c in candles],
        "low": [c.low for c in candles],
        "close": [c.close for c in candles],
        "volume": [c.volume for c in candles],
        "n_src": [1] * len(candles),
    }).astype(SCHEMA)


def to_hourly(frame: pd.DataFrame) -> pd.DataFrame:
    """Folds bars of an arbitrary intra-hour grid into hourly ones on the
    boundary of the round UTC hour.

    Needed because the sources' grids do not coincide: ETF bars run on the :30
    (09:30, 10:30, ...), currency pairs and crypto on the round hour. The
    cross-section - weighted median, CSV, PCA, correlation matrix - requires "the
    same hour" to mean the same thing for every series, otherwise synchrony is
    measured on series offset from one another. So the ETFs are requested as
    half-hourly bars and folded here.

    For series already sitting on the round hour the operation is the identity.
    """
    if frame.empty:
        return empty_frame()
    # Two bars with the SAME hour_utc are one and the same bar that landed in
    # the file twice, not two different ones. They must be dropped BEFORE
    # aggregation: volume is summed, and on a duplicate it would double. Exactly
    # that happened on the accumulated history, where a bad branch merge
    # duplicated a block of 299 hours. The last copy wins: it is either
    # equivalent or more complete - a later download finds the hour closed.
    df = (frame.astype(SCHEMA)
          .drop_duplicates(subset="hour_utc", keep="last")
          .sort_values("hour_utc"))
    grouped = df.groupby(df["hour_utc"] // HOUR * HOUR, sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        n_src=("close", "size"),
    )
    return grouped.reset_index(names="hour_utc").astype(SCHEMA)


# Store directories. Kept here rather than in each calling module so that the
# backfill and the audit cannot disagree about where the data lives.
DEFAULT_BARS_DIR = os.path.join("data", "meals", "bars")
DEFAULT_VIX_DIR = os.path.join("data", "meals", "vix")

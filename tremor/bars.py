"""Tremor hourly bar store: Parquet, one directory of per-year shards per
instrument.

Why Parquet rather than the NDJSON of the existing monitor: there a file is only
appended one row per hour and grows slowly, whereas here sixty-two instruments
back to 2003 amount to several million bars, and they must be read in full on
every run of the PCA and the regressions. A typed columnar format reads an order
of magnitude faster and takes several times less space.

Why sharded by year rather than one file per instrument: parquet rewrites a file
whole, so a settled year re-commits itself every time the current hour arrives.
The archive is committed daily and cannot be re-fetched from the free tier, so
dropping it from git is not an option - sharding is. See store_path.

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
    """Where one instrument's bars live: a DIRECTORY of per-year shards.

    It used to be a single file, and the reason it is not any more is git.
    Parquet rewrites a file whole, so a one-megabyte store re-commits a whole
    megabyte to say that one hour arrived; the archive is committed daily, and at
    sixty-two instruments that was ninety megabytes of new objects a day, about
    thirty gigabytes a year against a repository already at half a gigabyte and a
    five-gigabyte soft limit. Sharded by year, only the current year's file
    changes, and the daily commit is a few hundred kilobytes.

    The path is still handed around as one string, so nothing above this module
    has to know. `load` also reads the legacy single file where one is still
    lying about, and the next `write` folds it into the shards and deletes it -
    the migration is the ordinary write path, not a script somebody has to
    remember to run.
    """
    return os.path.join(base_dir, file_stem)


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series(dtype=dt) for name, dt in SCHEMA.items()})


def _legacy_path(store: str) -> str:
    return f"{store}.parquet"


def _year_of(hours: pd.Series) -> pd.Series:
    return pd.to_datetime(hours, unit="s", utc=True).dt.year


def _shards(store: str) -> list[str]:
    """Every file the store is spread across, oldest first.

    A path that already names a .parquet file is honoured as one - callers that
    hold an explicit file (the tests, and anything pointing at an old store)
    keep working unchanged.
    """
    if store.endswith(".parquet"):
        return [store] if os.path.exists(store) else []
    found = []
    legacy = _legacy_path(store)
    if os.path.exists(legacy):
        found.append(legacy)
    if os.path.isdir(store):
        found.extend(sorted(os.path.join(store, name) for name in os.listdir(store)
                            if name.endswith(".parquet")))
    return found


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.astype(SCHEMA).sort_values("hour_utc").reset_index(drop=True)


def load(store: str) -> pd.DataFrame:
    """The whole instrument, every shard concatenated.

    A legacy file is read FIRST so that a shard covering the same hour wins the
    de-duplication - during a migration the shard is the newer copy by
    construction, and reading it second would resurrect stale rows.
    """
    parts = [pd.read_parquet(path) for path in _shards(store)]
    if not parts:
        return empty_frame()
    combined = pd.concat(parts, ignore_index=True)
    combined = combined.drop_duplicates(subset="hour_utc", keep="last")
    return _normalise(combined)


def write(store: str, frame: pd.DataFrame) -> None:
    """Rewrites the store, touching only the shards whose contents changed.

    Not touching an unchanged shard is the entire point: a year whose bars are
    long settled must produce no file write at all, so that git sees one changed
    file a day rather than twenty-four.
    """
    frame = _normalise(frame)
    if store.endswith(".parquet"):
        os.makedirs(os.path.dirname(store) or ".", exist_ok=True)
        frame.to_parquet(store, index=False, compression="zstd")
        return

    os.makedirs(store, exist_ok=True)
    wanted = {str(year): part.reset_index(drop=True)
              for year, part in frame.groupby(_year_of(frame["hour_utc"]))}
    for year, part in wanted.items():
        path = os.path.join(store, f"{year}.parquet")
        if os.path.exists(path):
            try:
                if _normalise(pd.read_parquet(path)).equals(part):
                    continue
            except Exception:                    # pragma: no cover - defensive
                pass                             # unreadable shard: rewrite it
        part.to_parquet(path, index=False, compression="zstd")
    for name in os.listdir(store):
        # A year that no longer has bars in the frame. Only reachable when a
        # store is rebuilt from a shorter history, but leaving the file behind
        # would make load() return rows write() was told to drop.
        if name.endswith(".parquet") and name[:-len(".parquet")] not in wanted:
            os.remove(os.path.join(store, name))
    legacy = _legacy_path(store)
    if os.path.exists(legacy):
        # Everything it held is now in the shards - load() read it before this
        # write and write() has just laid the union back down.
        os.remove(legacy)


def merge(store: str, frame: pd.DataFrame) -> int:
    """Idempotently brings the store to the union of what is already there and
    `frame`. On a matching hour_utc the new row wins: the source may have revised
    the bar, and the fresher version is more trustworthy. Returns the number of
    added rows (revisions of existing ones do not count).
    """
    if frame.empty:
        return 0
    existing = load(store)
    before = len(existing)
    combined = pd.concat([existing, frame.astype(SCHEMA)], ignore_index=True)
    combined = combined.drop_duplicates(subset="hour_utc", keep="last")
    write(store, combined)
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
DEFAULT_BARS_DIR = os.path.join("data", "tremor", "bars")
DEFAULT_VIX_DIR = os.path.join("data", "tremor", "vix")

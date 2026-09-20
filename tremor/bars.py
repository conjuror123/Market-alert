"""Tremor hourly bar store: Parquet, one directory of shards per instrument.

Why Parquet rather than the NDJSON of the existing monitor: there a file is only
appended one row per hour and grows slowly, whereas here sixty-two instruments
back to 2003 amount to several million bars, and they must be read in full on
every run of the PCA and the regressions. A typed columnar format reads an order
of magnitude faster and takes several times less space.

Why sharded rather than one file per instrument: parquet rewrites a file whole,
so an unsharded store re-commits its entire history every time the current hour
arrives. The archive is committed and cannot be re-fetched from the free tier,
so dropping it from git is not an option - sharding is. See store_path.

The time convention is the one thing here that cannot be changed later:
hour_utc stores the bar's OPENING moment, and the closing moment is
t = hour_utc + 1 hour. This is the same
convention already used by the existing monitor's candle_store (open_time), so
the accumulated history imports without a shift.
"""
from __future__ import annotations

import os

from tremor import atomic

import pandas as pd

from price_monitor.models import Candle

HOUR = 3600

# n_src - how many source bars folded into this hourly bar.
# Needed from phase 1 onward: for ETFs the first half hour of a session produces
# an hourly bar out of a single half-hourly one, and that is precisely the "first
# bar of the session" that tremor.returns splits into the gap channel and the intra-hour
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
    """Where one instrument's bars live: a DIRECTORY of shards.

    Sharded because of git. Parquet rewrites a file whole and git cannot delta
    the result, so a commit stores every byte of whatever file changed. What
    that costs is set by how much history shares a shard with the hour being
    added, so the only number that matters is the size of the shard currently
    being appended to.

    SETTLED YEARS GET ONE SHARD EACH, THE YEAR BEING WRITTEN GETS TWELVE. A
    year-only scheme still re-commits the whole year to date on every write, and
    because that shard grows all year the annual cost is not 365 daily deltas
    but 183 times one complete year - 955 MiB to record the 5.2 MiB of bars a
    year actually contains. Splitting the live year by month divides that by
    twelve, and costs nothing anywhere else: an instrument holds at most
    twenty-odd yearly shards plus twelve monthly ones, so `load` still opens a
    few dozen files rather than a few hundred.

    Which year is "live" is read off the DATA, not off the clock: the newest
    year present is the one being appended to. So the first write of January
    folds the previous year's twelve months back into one shard by itself, and
    there is no January-only code path to get wrong once a year.

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


def _shard_of(hours: pd.Series) -> pd.Series:
    """Which shard each bar belongs in: "2019", or "2026-09" for the live year."""
    if hours.empty:
        return pd.Series(dtype="object")
    when = pd.to_datetime(hours, unit="s", utc=True)
    year, month = when.dt.year, when.dt.month
    live = int(year.max())
    return year.astype(str).where(
        year != live, year.astype(str) + "-" + month.map("{:02d}".format))


def _shard_key(path: str) -> "tuple[int, int]":
    """Chronological order, with a year's own shard BEFORE its months.

    The ordering is what makes `load`'s keep="last" pick the right copy. A
    yearly and a monthly shard for the same year coexist only if a write died
    between laying the months down and removing the year they replace, and in
    that case the months are the newer truth - so they must be read second.
    Sorting the names as strings gets this backwards, because "-" sorts before
    ".".
    """
    stem = os.path.basename(path)
    stem = stem[:-len(".parquet")] if stem.endswith(".parquet") else stem
    year, _, month = stem.partition("-")
    try:
        return (int(year), int(month) if month else 0)
    except ValueError:                       # pragma: no cover - defensive
        return (1 << 30, 0)                  # something else entirely: read last


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
        found.extend(sorted((os.path.join(store, name) for name in os.listdir(store)
                             if name.endswith(".parquet")), key=_shard_key))
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
        atomic.write_parquet(store, frame)
        return

    os.makedirs(store, exist_ok=True)
    wanted = {str(shard): part.reset_index(drop=True)
              for shard, part in frame.groupby(_shard_of(frame["hour_utc"]))}
    for shard, part in wanted.items():
        path = os.path.join(store, f"{shard}.parquet")
        if os.path.exists(path):
            try:
                if _normalise(pd.read_parquet(path)).equals(part):
                    continue
            except Exception:                    # pragma: no cover - defensive
                pass                             # unreadable shard: rewrite it
        atomic.write_parquet(path, part)
    for name in os.listdir(store):
        # A shard that no longer has bars in the frame: a year rebuilt from a
        # shorter history, or - every January - the twelve months of the year
        # that has just stopped being the live one. Leaving the file behind
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

"""Jump hourly bar store: one directory of shards per instrument - the open
months as CSV outside git, settled months as CSV, settled years as Parquet.

Why Parquet for the settled years: 173 instruments back to 2003 amount to
several million bars, read on every run. A typed columnar format reads an order
of magnitude faster and takes several times less space.

Why sharded rather than one file per instrument: parquet rewrites a file whole,
so an unsharded store re-commits its entire history every time the current hour
arrives. The archive is committed and cannot be re-fetched from the free tier,
so dropping it from git is not an option - sharding is. See store_path.

The time convention is the one thing here that cannot be changed later:
hour_utc stores the bar's OPENING moment, and the closing moment is
t = hour_utc + 1 hour, the same convention as every provider's candles
(open_time).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from jump import atomic

import pandas as pd

from price_monitor.models import Candle

HOUR = 3600

# n_src - how many source bars folded into this hourly bar. A fund's first hour
# of a session is a single half-hour bar (09:30-10:00); the coverage report
# (jump.audit) counts such hours apart from full ones.
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

    THE MONTH BEING WRITTEN IS NOT IN GIT. Git keeps every version of every
    file it is given: a commit that adds one line to forty files stores forty
    new objects and the folder listings pointing at them, and Parquet, being
    compressed, shares nothing with its previous version at all. Measured on a
    real week: a run's new bars are 650 bytes, the commit that recorded them in
    per-instrument files 14 KB - 124 MiB a year hourly. So the open months
    (`2026-10.open.csv`) are gitignored and kept between runs as one file on a
    GitHub release (tools/hot_bars.sh), where replacing it costs the
    repository nothing.

    A MONTH ENTERS GIT ONCE, when it is settled: SETTLE_DAYS after its end, so
    the holes a provider's outage left in its last days have been filled. It is
    plain CSV, `2026-09.csv`. Git compresses every file it stores, so a month
    costs it the same 1 MB (all instruments) plain or gzipped; as Parquet 1.9,
    because a month is too small for Parquet's per-file overhead to pay off,
    and no faster to read (measured on 2026-08). It is not rewritten after:
    `merge` fills hours it lacks but does not revise the ones it holds.

    A YEAR BECOMES ONE PARQUET FILE once its December has settled: the first
    write after that folds its twelve months into `2026.parquet`. A year is big
    enough for Parquet to be the smaller and faster form, and the store keeps a
    few dozen files rather than twelve more every year for `load` to open. The
    fold costs git the year once more, about 16 MB (2025 was 16.4), the months
    staying in its history: with the months, about 28 MB a year.

    What is settled is read off the DATA, not the clock: SETTLE_DAYS after the
    month's end, measured against the newest bar the store holds.

    The path is still handed around as one string, so nothing above this module
    has to know.
    """
    return os.path.join(base_dir, file_stem)


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series(dtype=dt) for name, dt in SCHEMA.items()})


# A week: what still reaches a closed month after its end is a hole filled late
# - a provider down over a long weekend, a market shut since the month's last
# day. The backfill re-asks only the last three hours of each store, so a bar
# revised later than that never arrives anyway. A fill after settling is not
# lost, it rewrites that one month of that one instrument (a few KB of git).
SETTLE_DAYS = 7


def _month_end(year: int, month: int) -> int:
    nxt = pd.Timestamp(year=year + month // 12, month=month % 12 + 1, day=1, tz="UTC")
    return int(nxt.timestamp())


def settled_before(newest_hour: int) -> int:
    """The first hour that is still open: the start of the oldest month that is
    not yet SETTLE_DAYS past its end, measured against `newest_hour`."""
    when = pd.Timestamp(int(newest_hour), unit="s", tz="UTC")
    year, month = when.year, when.month
    # The previous month is still open for its first SETTLE_DAYS.
    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    if newest_hour < _month_end(prev_y, prev_m) + SETTLE_DAYS * 86400:
        year, month = prev_y, prev_m
    return int(pd.Timestamp(year=year, month=month, day=1, tz="UTC").timestamp())


OPEN = ".open.csv"


def _layout(hours: pd.Series, existing: "set[str]") -> "tuple[pd.Series, dict]":
    """Each bar's shard, and each shard's file name.

    A month is open (`2026-10.open.csv`, gitignored) until settled, then
    `2026-09.csv`, committed once. A year is one Parquet shard once its
    December has settled: the first write after that folds its months into
    `2026.parquet`. What is in git already - a settled month's `.csv`, a month's
    or a year's `.parquet` - keeps its file whatever the clock says: a store
    that lost its open months (a run without the release) sees its newest
    committed month as its newest data, and read off the clock alone that month
    would be open again, pulled out of git."""
    if hours.empty:
        return pd.Series(dtype="object"), {}
    when = pd.to_datetime(hours, unit="s", utc=True)
    year, month = when.dt.year, when.dt.month
    cutoff = settled_before(int(hours.max()))
    year_end = pd.to_datetime((year + 1).astype(str) + "-01-01", utc=True)
    year_settled = (year_end.astype("int64") // 10**9) <= cutoff
    yearly = {int(n[:-len(".parquet")]) for n in existing
              if n.endswith(".parquet") and "-" not in n}
    by_year = year_settled | year.isin(yearly)
    shard = year.astype(str).where(by_year, year.astype(str) + "-" + month.map("{:02d}".format))
    names = {}
    for name in shard.unique():
        if "-" not in name:
            names[name] = f"{name}.parquet"
            continue
        committed = [f"{name}{ext}" for ext in (".csv", ".parquet") if f"{name}{ext}" in existing]
        if committed:                          # in git already: stays as it is
            names[name] = committed[0]
        elif _month_end(int(name[:4]), int(name[5:])) > cutoff:
            names[name] = f"{name}{OPEN}"     # open: the month the store is writing
        else:
            names[name] = f"{name}.csv"
    return shard, names


# The open month's suffix before the plain one: the stem of "2026-10.open.csv"
# is "2026-10".
SHARD_EXTENSIONS = (".parquet", OPEN, ".csv")


def _stem(name: str) -> str:
    for ext in SHARD_EXTENSIONS:
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _is_shard(name: str) -> bool:
    return name.endswith(SHARD_EXTENSIONS) and not name.startswith(".")


def _read_shard(path: str) -> pd.DataFrame:
    if path.endswith(".csv"):
        return pd.read_csv(path, dtype=SCHEMA, float_precision="round_trip")
    return pd.read_parquet(path)


def _shard_key(path: str) -> "tuple[int, int]":
    """Chronological order, with a year's own shard BEFORE its months.

    The ordering is what makes `load`'s keep="last" pick the right copy. A
    yearly and a monthly shard for the same year coexist only if a write died
    between laying the months down and removing the year they replace, and in
    that case the months are the newer truth - so they must be read second.
    Sorting the names as strings gets this backwards, because "-" sorts before
    ".".
    """
    year, _, month = _stem(os.path.basename(path)).partition("-")
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
    if os.path.isdir(store):
        found.extend(sorted((os.path.join(store, name) for name in os.listdir(store)
                             if _is_shard(name)), key=_shard_key))
    return found


def _reaches(path: str, year: int, month: int) -> bool:
    """Whether a shard can hold bars of that month or later: a year's shard
    through its December, a month's its own; anything else (a named
    parquet) is read."""
    shard_year, shard_month = _shard_key(path)
    return shard_year >= (1 << 30) or (shard_year, shard_month or 12) >= (year, month)


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.astype(SCHEMA).sort_values("hour_utc").reset_index(drop=True)


def load(store: str, since: "int | None" = None) -> pd.DataFrame:
    """The whole instrument, every shard concatenated - or, with `since`, its
    bars from that hour on, read from the shards that can hold them.
    """
    paths = _shards(store)
    if since is not None:
        first = datetime.fromtimestamp(int(since), tz=timezone.utc)
        paths = [p for p in paths if _reaches(p, first.year, first.month)]
    parts = [_read_shard(path) for path in paths]
    if not parts:
        return empty_frame()
    combined = pd.concat(parts, ignore_index=True)
    combined = combined.drop_duplicates(subset="hour_utc", keep="last")
    if since is not None:
        combined = combined[combined["hour_utc"] >= int(since)]
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
    existing = {n for n in os.listdir(store) if _is_shard(n)}
    shard, names = _layout(frame["hour_utc"], existing)
    files = {names[str(k)]: part.reset_index(drop=True) for k, part in frame.groupby(shard)}
    for name, part in files.items():
        path = os.path.join(store, name)
        if os.path.exists(path):
            try:
                if _normalise(_read_shard(path)).equals(part):
                    continue
            except Exception:                    # pragma: no cover - defensive
                pass                             # unreadable shard: rewrite it
        if name.endswith(".csv"):
            atomic.write_csv(path, part)
        else:
            atomic.write_parquet(path, part)
    for name in existing:
        # A shard that no longer has bars in the frame, or the same shard in
        # another form - an open month once it settles, a settled year's months
        # once they are folded into its Parquet.
        # Leaving the file behind would make load() return rows write() was
        # told to drop.
        if name not in files:
            os.remove(os.path.join(store, name))


def removed(frame: pd.DataFrame) -> pd.Series:
    """The rows that are bars removed on purpose: an hour kept with no price."""
    return frame["close"].isna()


def remove(store: str, hours) -> int:
    """Removes bars on purpose, each hour kept as a row with no price.

    Deleting the row would leave a hole that any fetch reaching that hour fills
    again - fill-gaps re-asks every session the calendar has and the store
    lacks, from the vendor that served the bad bar in the first place. Kept,
    the hour is held: merge never fills it, and the bar gate (jump.quality)
    makes it a hole for everything that scores. Returns the hours marked."""
    import numpy as np

    marks = sorted({int(h) for h in hours})
    frame = load(store)
    kept = frame[~frame["hour_utc"].isin(marks).to_numpy()]
    nothing = pd.DataFrame({"hour_utc": marks, "open": np.nan, "high": np.nan,
                            "low": np.nan, "close": np.nan, "volume": np.nan, "n_src": 0})
    write(store, pd.concat([kept, nothing], ignore_index=True))
    return len(marks)


def merge(store: str, frame: pd.DataFrame, revise_settled: bool = False) -> int:
    """Idempotently brings the store to the union of what is already there and
    `frame`. On a matching hour_utc in an OPEN month the new row wins: the
    source may have revised the bar, and the fresher version is more
    trustworthy. In a settled month (store_path) the stored row stands - a
    settled month is in git and is not rewritten for a provider's re-served
    copy - unless `revise_settled`, which the tape repair passes on purpose. A
    month already committed counts as settled whatever the clock says.
    Hours a settled month lacks are filled either way. A bar removed on purpose
    (remove) is never filled again, in any month, whatever is passed. Returns
    the number of added rows (revisions of existing ones do not count).
    """
    if frame.empty:
        return 0
    existing = load(store)
    before = len(existing)
    incoming = frame.astype(SCHEMA)
    if not existing.empty:
        gone = set(existing.loc[removed(existing), "hour_utc"].astype(int))
        if gone:
            incoming = incoming[~incoming["hour_utc"].isin(gone).to_numpy()]
    if not revise_settled and not existing.empty:
        cutoff = settled_before(int(max(existing["hour_utc"].max(), incoming["hour_utc"].max())))
        settled = incoming["hour_utc"] < cutoff
        if os.path.isdir(store):
            committed = {_stem(n) for n in os.listdir(store)
                         if n.endswith((".csv", ".parquet")) and not n.endswith(OPEN)}
            when = pd.to_datetime(incoming["hour_utc"], unit="s", utc=True)
            settled |= when.dt.strftime("%Y-%m").isin(committed)
            settled |= when.dt.strftime("%Y").isin(committed)
        held = incoming["hour_utc"].isin(set(existing["hour_utc"].astype(int)))
        incoming = incoming[(~(settled & held)).to_numpy()]
    combined = pd.concat([existing, incoming], ignore_index=True)
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
    (09:30, 10:30, ...), currency pairs and crypto on the round hour. "The same
    hour" must mean the same thing for every series - in the store, in the
    messages, in a second source's check - so the ETFs are requested as
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
DEFAULT_BARS_DIR = os.path.join("data", "jump", "bars")
DEFAULT_VIX_DIR = os.path.join("data", "jump", "vix")

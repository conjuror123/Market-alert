"""The fitted rarity ladder, kept so it does not have to be refitted every hour.

WHY THIS EXISTS. Everything else in the per-asset chain depends on a BOUNDED
stretch of history - the longest is the long-run sigma at five thousand bars,
and the adaptive thresholds at four times an instrument's own window - so a
recent bar can be computed exactly from a trailing slice of the archive rather
than from all twenty-three years of it. Measured: an ETF needs about eight
thousand bars and a currency pair about twelve before the last two hundred
agree with a full run to one part in a billion.

The ladder is the one exception. severity.rolling_levels fits a generalised
Pareto tail to EVERY bar before the one it is describing, refitting every thirty
days, so it needs the whole magnitude series and a trailing slice cannot produce
it. Caching the magnitude series instead would be sixty megabytes; caching what
the fits PRODUCED is about a megabyte, because a refit every thirty days over
twenty-three years is two hundred and eighty segments of four numbers.

WHAT MAKES IT SAFE TO CACHE. The fit at each boundary uses only bars strictly
before it and the level it produces applies FORWARD, to the thirty days after.
So a level, once fitted, never changes - it is not an estimate that improves
with more data, it is a statement about what was known at that moment. Caching
it is not an approximation; it is remembering an answer instead of recomputing
it.

WHEN IT IS NOT USED. The cache is keyed by the hour a segment opens, so the
lookup does not care how long the frame it is applied to is. When the newest
bars run past the last cached segment by a whole refit step, that instrument is
due a genuine refit and falls back to the full history - once every thirty days
per instrument, which across the basket is a couple of instruments a day. A
cache that is missing, unreadable or built under a different configuration is
simply not used, and the run behaves exactly as it did before this module
existed.
"""
from __future__ import annotations

import logging
import os

import numpy as np
import pandas as pd

from tremor import severity

log = logging.getLogger("tremor.ladder")

DEFAULT_LADDER_PATH = os.path.join("data", "tremor", "ladder.csv")

# CSV rather than parquet, and that is deliberate. This file is committed, it
# only ever grows at its end, and git deltas an appended text file to a few
# hundred bytes where it stores a rewritten parquet whole. Measured on the bar
# archive: 0.8 KB per append for csv against 2.3 KB for parquet, and parquet
# only does that well because its settled row groups happen to be byte-stable.
COLUMNS = ("config", "asset_id", "ladder", "from_hour", "rate", "step") + tuple(
    f"level_{name}" for name in severity.TIERS)


def empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in COLUMNS}).astype(
        {"config": "object", "asset_id": "object", "ladder": "object",
         "from_hour": "int64", "step": "int64"})


def load(path: str = DEFAULT_LADDER_PATH,
         config: str | None = None) -> pd.DataFrame:
    """The cache, or an empty frame shaped like it.

    `config` is the version hash of the code and configuration that produced a
    row. Rows stamped with anything else are dropped rather than used, because a
    cached level is only an answer to the question the code was asking when it
    was fitted - change the model and it is a number from a different system. A
    cache that is missing, unreadable, or entirely from another version simply
    leaves every instrument on the full-history path, which is what the run did
    before this module existed.
    """
    if not path or not os.path.exists(path):
        return empty()
    try:
        frame = pd.read_csv(path)
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("Ladder cache at %s is unreadable (%s); ignoring it", path, exc)
        return empty()
    missing = set(COLUMNS) - set(frame.columns)
    if missing:
        log.warning("Ladder cache at %s is missing %s; ignoring it", path, sorted(missing))
        return empty()
    if config is not None:
        stale = int((frame["config"].astype(str) != str(config)).sum())
        if stale:
            log.info("Ladder cache: dropping %d of %d rows fitted under another "
                     "configuration", stale, len(frame))
        frame = frame[frame["config"].astype(str) == str(config)]
    return frame.sort_values(["asset_id", "ladder", "from_hour"]).reset_index(drop=True)


def save(frame: pd.DataFrame, path: str = DEFAULT_LADDER_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    frame = frame.drop_duplicates(subset=["asset_id", "ladder", "from_hour"], keep="last")
    frame.sort_values(["asset_id", "ladder", "from_hour"]).to_csv(
        path, index=False, float_format="%.10g")


def segments(asset_id: str, ladder: str, hours: pd.Series, levels: pd.DataFrame,
             rate: float, step: int, config: str = "") -> pd.DataFrame:
    """The step function a fitted levels frame represents, one row per refit.

    The rows follow rolling_levels' own grid - the warm-up, then every `step`
    bars - rather than being inferred from where the numbers change. That
    matters for a ladder whose levels are ALL NaN, which happens to an
    instrument with no peers in its block: its abnormal residual is its raw move
    less a drift and the tail fit has nothing to bite on. Inferring segments from
    changes would record no rows for it, "fitted and empty" would be
    indistinguishable from "never fitted", and covers() would send every run to
    the full history for ever. Recording the grid says "this was fitted, and the
    answer is that there is no answer", which is a different and true thing.
    """
    names = list(severity.TIERS)
    n = len(levels)
    warmup = int(severity.WARMUP_DAYS * severity.HOURS_PER_DAY * rate)
    if n <= warmup or step <= 0:
        return pd.DataFrame(columns=list(COLUMNS))

    starts = np.arange(warmup, n, step, dtype="int64")
    values = levels[names].to_numpy(dtype="float64")[starts]
    out = pd.DataFrame({
        "config": str(config), "asset_id": asset_id, "ladder": ladder,
        "from_hour": hours.to_numpy()[starts].astype("int64"),
        "rate": float(rate), "step": int(step),
    })
    for position, name in enumerate(names):
        out[f"level_{name}"] = values[:, position]
    return out[list(COLUMNS)]


def _mine(cache: pd.DataFrame, asset_id: str, ladder: str) -> pd.DataFrame:
    if cache.empty:
        return cache
    mine = cache[(cache["asset_id"] == asset_id) & (cache["ladder"] == ladder)]
    return mine.sort_values("from_hour")


def covers(cache: pd.DataFrame, asset_id: str, ladder: str,
           hours: pd.Series) -> bool:
    """Whether the cache can answer for every bar in `hours` without a refit.

    Two ways it cannot. The frame may reach back before the first segment, which
    means the instrument's warm-up is inside it and the ladder genuinely has to
    be built; or the newest bars may run a whole refit step past the last
    segment, which means a refit is due.
    """
    mine = _mine(cache, asset_id, ladder)
    if mine.empty or hours.empty:
        return False
    last = mine.iloc[-1]
    beyond = int((hours.to_numpy() > int(last["from_hour"])).sum())
    return beyond < int(last["step"])


def levels_for(cache: pd.DataFrame, asset_id: str, ladder: str,
               hours: pd.Series) -> pd.DataFrame:
    """The cached levels expanded back onto a frame's bars.

    A bar before the first segment gets NaN on every rung, which is the same
    answer rolling_levels gives during the warm-up and means "no tier" rather
    than "no event".
    """
    names = list(severity.TIERS)
    mine = _mine(cache, asset_id, ladder)
    out = pd.DataFrame({name: np.full(len(hours), np.nan) for name in names},
                       index=hours.index)
    if mine.empty:
        return out
    edges = mine["from_hour"].to_numpy(dtype="int64")
    # Which segment each bar falls in: the last one that had opened by then.
    position = np.searchsorted(edges, hours.to_numpy(dtype="int64"), side="right") - 1
    inside = position >= 0
    for name in names:
        values = mine[f"level_{name}"].to_numpy(dtype="float64")
        column = np.full(len(hours), np.nan)
        column[inside] = values[position[inside]]
        out[name] = column
    return out

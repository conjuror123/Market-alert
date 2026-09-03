"""Configuration and run versions (spec §6.2, §6.3).

§6.3 forbids changing parameters retroactively without recomputing the history
under a new version. The prohibition is enforced not by discipline but by
construction: config_version is a hash of the content of everything that affects
the result. Change a threshold, a window, the basket composition or a formula and
the version changes by itself, while the old events already carry a different
one. A manual counter is useless here: it gets forgotten precisely when it
matters most.

run_version works differently. Per §6.2 a repeat run of the same hour under the
same version must not create duplicates, while late or revised data MUST enter
the recomputation under a NEW version. One and the same construction satisfies
both: run_version is a hash of config_version and a fingerprint of the input
data. A run over unchanged data yields the same version and is therefore
idempotent; the moment the source back-fills or corrects a bar, the fingerprint
changes and the recomputation gets a new version automatically.
"""
from __future__ import annotations

import hashlib
import os

# Everything that affects the result of the calculation. The list is deliberately
# explicit: a silent "hash the whole package" would break the version on a comment
# edit, while hashing only the configuration would miss a change of formula.
CONFIG_INPUTS = (
    os.path.join("config", "basket.yaml"),
    os.path.join("meals", "windows.py"),
    os.path.join("meals", "zscore.py"),
    os.path.join("meals", "returns.py"),
    os.path.join("meals", "volume.py"),
    os.path.join("meals", "quality.py"),
    os.path.join("meals", "residuals.py"),
    os.path.join("meals", "cross_section.py"),
    os.path.join("meals", "si_index.py"),
    os.path.join("meals", "cluster.py"),
    os.path.join("meals", "saed.py"),
    os.path.join("meals", "calendar_multiplier.py"),
    os.path.join("meals", "vix.py"),
)

# Raw inputs of the calculation: everything that arrives from outside and is not
# a product of the system itself. Derived files - asset metrics, basket metrics,
# residuals - are NOT included here, and that is essential. The basket metrics are
# both input and output for the cluster run: include them in the fingerprint and a
# repeat run over the same data would get a new run_version simply because the
# previous run rewrote the file. The idempotency of §6.2 rests on exactly this:
# the version depends only on the raw data and the configuration, and everything
# else is a function of those.
RAW_INPUTS = (
    os.path.join("data", "meals", "bars"),
    os.path.join("data", "meals", "vix"),
    os.path.join("data", "meals", "sessions"),
    os.path.join("data", "meals", "corporate_actions.csv"),
    os.path.join("data", "economic_calendar", "calendar.ndjson"),
)

VERSION_LENGTH = 12

# Chunk size when reading the data. The files are few and small, but there is no
# reason to read a seventeen-megabyte calendar as a single bytes object.
CHUNK = 1 << 20


def _digest(chunks) -> str:
    accumulator = hashlib.sha256()
    for chunk in chunks:
        accumulator.update(chunk)
        accumulator.update(b"\x00")
    return accumulator.hexdigest()[:VERSION_LENGTH]


def config_version(root: str = ".", inputs=CONFIG_INPUTS) -> str:
    """Configuration version: a hash of the content of the files that affect the
    calculation.

    A missing file is not grounds for an exception but part of the state: its
    absence changes the version too, and that is more correct than failing.
    """
    chunks = []
    for relative in sorted(inputs):
        chunks.append(relative.encode("utf-8"))
        path = os.path.join(root, relative)
        if os.path.exists(path):
            with open(path, "rb") as f:
                chunks.append(f.read())
        else:
            chunks.append(b"<absent>")
    return _digest(chunks)


def _expand(paths):
    """Directories expand into a list of files; files stay themselves.

    A directory as such cannot serve as a fingerprint: its own modification time
    changes only when an entry is added or removed, and a bar appended inside an
    already existing file does not touch it - so a recomputation over updated data
    would get the same run_version as the run before the update.
    """
    for path in paths:
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for name in sorted(files):
                    yield os.path.join(root, name)
        else:
            yield str(path)


def data_fingerprint(paths=RAW_INPUTS) -> str:
    """Fingerprint of the input data: a hash of its CONTENT.

    Size and modification time would be cheaper but are wrong in both directions.
    A backfill run rewrites a file with the same bars - same size, new time - and a
    recomputation over unchanged data would get a new version, meaning the
    idempotency of §6.2 would not exist at all. Conversely, a bar corrected by the
    vendor to the same length would leave the size unchanged, and the edit could
    slip through unnoticed if the file was rewritten within the same second.

    The price is about a hundred milliseconds: there are roughly thirty megabytes
    of raw data here, and derived files are not in the fingerprint (see
    RAW_INPUTS).
    """
    chunks = []
    for path in sorted(set(_expand(paths))):
        chunks.append(str(path).encode("utf-8"))
        if os.path.exists(path):
            with open(path, "rb") as f:
                while True:
                    block = f.read(CHUNK)
                    if not block:
                        break
                    chunks.append(block)
        else:
            chunks.append(b"<absent>")
    return _digest(chunks)


def run_version(config: str, fingerprint: str) -> str:
    """Run version. Identical input and configuration give an identical version -
    hence the idempotency of §6.2."""
    return _digest([config.encode("utf-8"), fingerprint.encode("utf-8")])


def versions_for(data_paths=RAW_INPUTS, root: str = ".") -> tuple[str, str]:
    config = config_version(root)
    return config, run_version(config, data_fingerprint(data_paths))


# --- stamping the versions onto what the run writes -----------------------

VERSION_COLUMNS = ("config_version", "run_version")

# Provenance of a row per §6.2 and §6.4: when it first appeared, and whether it
# has since been recomputed.
PROVENANCE_COLUMNS = ("recalculated", "created_at")


def stamp(frame, config: str, run: str):
    """Writes both versions into every row.

    Per §6.3 the versions belong in each event, in the metrics and in the
    decision journal - not in a file name. Events of different versions may be
    compared only with the versions stated explicitly, and a reader who has one
    table in front of them cannot state what is not in it.
    """
    return frame.assign(config_version=config, run_version=run)


def provenance(frame, previous, run: str, key: str = "event_id", now: int | None = None):
    """created_at and recalculated (§6.2, §6.4), carried across runs.

    created_at is the moment a row FIRST appeared, not the moment of the latest
    write. Taking the clock on every run would be easier and would be wrong twice
    over: it destroys the idempotency of §6.2 - a rerun over unchanged data would
    produce a different table byte for byte - and it answers a question
    run_version already answers better, since run_version says WHICH inputs
    produced the row while created_at is meant to say WHEN it first existed.

    So a row that was already there keeps its created_at, and only genuinely new
    rows get the clock. recalculated then means what §6.2 says it means: this row
    existed under an earlier run_version and has been recomputed under a new one -
    late or revised data reached it. A row seen for the first time is not
    recalculated, and neither is one whose run_version has not moved.

    `previous` is the table as the last run left it, or None when there is none -
    on the very first run nothing can be claimed about recomputation, and
    everything is simply new.
    """
    import time

    import pandas as pd

    stamp_now = int(time.time()) if now is None else int(now)
    if frame.empty:
        return frame.assign(recalculated=pd.Series(dtype="boolean"),
                            created_at=pd.Series(dtype="int64"))

    born, redone = {}, set()
    if previous is not None and not previous.empty and key in previous.columns:
        if "created_at" in previous.columns:
            born = dict(zip(previous[key], previous["created_at"]))
        if "run_version" in previous.columns:
            redone = {row_key for row_key, was in
                      zip(previous[key], previous["run_version"]) if was != run}

    keys = frame[key]
    return frame.assign(
        recalculated=[bool(k in redone) for k in keys],
        created_at=pd.array([born.get(k, stamp_now) for k in keys], dtype="int64"),
    )


def previous_table(path: str):
    """The table as the last run left it, or None if there is not one yet."""
    import pandas as pd

    if not os.path.exists(path):
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        # An unreadable leftover must not stop the run: the worst case is that
        # the rows count as new, which is exactly what an absent table means.
        return None

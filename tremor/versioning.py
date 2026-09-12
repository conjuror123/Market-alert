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
    os.path.join("tremor", "windows.py"),
    os.path.join("tremor", "zscore.py"),
    os.path.join("tremor", "returns.py"),
    os.path.join("tremor", "volume.py"),
    os.path.join("tremor", "quality.py"),
    os.path.join("tremor", "residuals.py"),
    # Added late, and the omission was a real gap rather than a tidy-up:
    # severity decides what tier every single event gets, persistence decides
    # whether it held, and blocks decides whether a whole complex moving is an
    # event at all. A change to any of them changes every number downstream and
    # has to change the configuration version with it. It matters more again now
    # that the fitted ladder is CACHED between runs - a cached level is only an
    # answer to the question the code was asking when it was fitted, and the
    # config hash is what stops one version's answers being reused by another.
    os.path.join("tremor", "severity.py"),
    os.path.join("tremor", "persistence.py"),
    os.path.join("tremor", "blocks.py"),
    os.path.join("tremor", "ladder.py"),
    os.path.join("tremor", "cross_section.py"),
    os.path.join("tremor", "si_index.py"),
    os.path.join("tremor", "cluster.py"),
    os.path.join("tremor", "saed.py"),
    os.path.join("tremor", "calendar_multiplier.py"),
    os.path.join("tremor", "vix.py"),
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
    os.path.join("data", "tremor", "bars"),
    os.path.join("data", "tremor", "vix"),
    os.path.join("data", "tremor", "sessions"),
    os.path.join("data", "tremor", "corporate_actions.csv"),
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


def stamps(data_paths=RAW_INPUTS, root: str = ".") -> tuple[str, str, str]:
    """config_version, run_version and the raw-data fingerprint, hashed once.

    The fingerprint is returned rather than thrown away because run_version
    cannot be taken apart again: it is a hash of the configuration AND the data,
    so a row carrying only run_version cannot say WHICH of the two moved. §6.2
    attaches recalculated to revised data specifically, and telling that from an
    edited threshold needs the two kept separately.
    """
    config = config_version(root)
    fingerprint = data_fingerprint(data_paths)
    return config, run_version(config, fingerprint), fingerprint


def versions_for(data_paths=RAW_INPUTS, root: str = ".") -> tuple[str, str]:
    config, run, _ = stamps(data_paths, root)
    return config, run


# --- stamping the versions onto what the run writes -----------------------

VERSION_COLUMNS = ("config_version", "run_version")

# Provenance of a row per §6.2 and §6.4: which raw data produced it, whether it
# has since been recomputed, and when it first appeared.
PROVENANCE_COLUMNS = ("data_fingerprint", "recalculated", "created_at")


def stamp(frame, config: str, run: str):
    """Writes both versions into every row.

    Per §6.3 the versions belong in each event, in the metrics and in the
    decision journal - not in a file name. Events of different versions may be
    compared only with the versions stated explicitly, and a reader who has one
    table in front of them cannot state what is not in it.
    """
    return frame.assign(config_version=config, run_version=run)


def provenance(frame, previous, fingerprint: str, key: str = "event_id",
               now: int | None = None):
    """data_fingerprint, recalculated and created_at (§6.2, §6.4).

    created_at is the moment a row FIRST appeared, not the moment of the latest
    write. Taking the clock on every run would be easier and would be wrong twice
    over: it destroys the idempotency of §6.2 - a rerun over unchanged data would
    produce a different table byte for byte - and it answers a question
    run_version already answers better, since run_version says WHICH inputs
    produced the row while created_at is meant to say WHEN it first existed.

    recalculated is judged on the RAW-DATA fingerprint, not on run_version. §6.2
    raises the flag for one situation - a bar arriving late or revised by the
    vendor - and run_version also moves when a threshold or a comment in the code
    changes, which is not that situation. Judging on run_version would raise the
    flag on every edit, and during calibration, when the thresholds move on every
    iteration, it would stand at True on every row and mean nothing. What the code
    changed is already what config_version is for.

    `previous` is the table as the last run left it, or None when there is none -
    on the very first run nothing can be claimed about recomputation, and
    everything is simply new.
    """
    import time

    import pandas as pd

    stamp_now = int(time.time()) if now is None else int(now)
    if frame.empty:
        return frame.assign(data_fingerprint=pd.Series(dtype="object"),
                            recalculated=pd.Series(dtype="boolean"),
                            created_at=pd.Series(dtype="int64"))

    born, redone = {}, set()
    if previous is not None and not previous.empty and key in previous.columns:
        if "created_at" in previous.columns:
            born = dict(zip(previous[key], previous["created_at"]))
        if "data_fingerprint" in previous.columns:
            redone = {row_key for row_key, was in
                      zip(previous[key], previous["data_fingerprint"])
                      if was != fingerprint}

    keys = frame[key]
    return frame.assign(
        data_fingerprint=fingerprint,
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

"""Configuration and run versions.

Changing parameters retroactively without recomputing the history is forbidden
under a new version. The prohibition is enforced not by discipline but by
construction: config_version is a hash of the content of everything that affects
the result. Change a threshold, a window, the basket composition or a formula and
the version changes by itself, while the old events already carry a different
one. A manual counter is useless here: it gets forgotten precisely when it
matters most.

run_version works differently. A repeat run of the same hour under the
same version must not create duplicates, while late or revised data MUST enter
the recomputation under a NEW version. One and the same construction satisfies
both: run_version is a hash of config_version and a fingerprint of the input
data. A run over unchanged data yields the same version and is therefore
idempotent; the moment the source back-fills or corrects a bar, the fingerprint
changes and the recomputation gets a new version automatically.
"""
from __future__ import annotations

import ast
import hashlib
import os

# Everything that affects the result of the calculation. The list is deliberately
# explicit: hashing the whole package would drag in files that cannot change a
# number, while hashing only the configuration would miss a change of formula.
CONFIG_INPUTS = (
    os.path.join("config", "basket.yaml"),
    # How far back an extension recomputes.
    os.path.join("tremor", "windows.py"),
    # What a metric row is: the quality gate, the move and the gap.
    os.path.join("tremor", "quality.py"),
    os.path.join("tremor", "returns.py"),
    os.path.join("tremor", "pipeline.py"),
    # tremor/jumps.py is not here: it rescores the whole history on every run
    # from the metrics, so nothing of its own is ever extended or cached.
)

# Raw inputs of the calculation: everything that arrives from outside and is not
# a product of the system itself. Derived files - asset metrics, basket metrics,
# residuals - are NOT included here, and that is essential. The basket metrics are
# both input and output for the cluster run: include them in the fingerprint and a
# repeat run over the same data would get a new run_version simply because the
# previous run rewrote the file. Idempotency rests on exactly this:
# the version depends only on the raw data and the configuration, and everything
# else is a function of those.
RAW_INPUTS = (
    os.path.join("data", "tremor", "bars"),
    os.path.join("data", "tremor", "sessions"),
    os.path.join("data", "tremor", "corporate_actions.csv"),
)

VERSION_LENGTH = 12

# Chunk size when reading the data. The files are few and small, but there is no
# reason to read a seventeen-megabyte calendar as a single bytes object.
CHUNK = 1 << 20


def _code(source: bytes) -> bytes:
    """A Python source file reduced to the code it runs.

    config_version must move when a formula moves and stay put otherwise, so what
    is hashed is the parsed tree with the docstrings taken out - not the bytes.
    Hashing the bytes makes every comment a formula change: it forces a cold
    rebuild of all 61 instruments, and the recomputed table can differ from the
    extended one it replaces, which reaches the reader as a burst of alerts for
    hours that were scored days ago.

    The tree is normalised by the running interpreter, so a Python upgrade can
    move the version by itself. That costs one cold rebuild and no wrong number,
    which is the right side of the trade to be on.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            node.body = node.body[1:]
    return ast.dump(tree).encode("utf-8")


def _content(path: str) -> bytes:
    """What a configuration input contributes to the hash.

    Python is hashed as code (see _code); anything else - config/basket.yaml -
    as its bytes, because a comment there sits beside the values it describes
    and there is no parse that separates the two cheaply.
    """
    with open(path, "rb") as f:
        raw = f.read()
    return _code(raw) if path.endswith(".py") else raw


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
            chunks.append(_content(path))
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
    idempotency would not exist at all. Conversely, a bar corrected by the
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
    hence idempotency."""
    return _digest([config.encode("utf-8"), fingerprint.encode("utf-8")])


def stamps(data_paths=RAW_INPUTS, root: str = ".") -> tuple[str, str, str]:
    """config_version, run_version and the raw-data fingerprint, hashed once.

    The fingerprint is returned rather than thrown away because run_version
    cannot be taken apart again: it is a hash of the configuration AND the data,
    so a row carrying only run_version cannot say WHICH of the two moved. The rule
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


def stamp(frame, config: str, run: str):
    """Writes both versions into every row.

    The versions belong in each event, in the metrics and in the
    decision journal - not in a file name. Events of different versions may be
    compared only with the versions stated explicitly, and a reader who has one
    table in front of them cannot state what is not in it.
    """
    return frame.assign(config_version=config, run_version=run)

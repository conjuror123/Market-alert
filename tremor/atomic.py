"""Write-then-replace so a killed run cannot leave a truncated file."""
from __future__ import annotations

import os


def write_replacing(path: str, writer) -> None:
    """Call ``writer(tmp_path)``, then ``os.replace`` onto ``path``.

    A crash during ``writer`` leaves ``path`` as it was. A leftover ``.tmp`` is
    disposable; the original file is the one that must stay readable.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    try:
        writer(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_parquet(path: str, frame, **kwargs) -> None:
    kwargs.setdefault("index", False)
    kwargs.setdefault("compression", "zstd")

    def _write(tmp: str) -> None:
        frame.to_parquet(tmp, **kwargs)

    write_replacing(path, _write)

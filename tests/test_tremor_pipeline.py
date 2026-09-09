"""The metrics stage: the same answer whether it runs on one core or four."""
import os
from dataclasses import replace

import pandas as pd
import pytest

from tremor import bars
from tremor.basket import load_basket


def test_the_parallel_path_writes_exactly_what_the_serial_one_does(tmp_path):
    # The instruments are independent - nothing in EUR/USD's metric chain looks
    # at SPY - so the loop parallelises, and the only thing that matters is that
    # it produces the same files. Run on two instruments so the pool has
    # something to distribute and the test still finishes quickly.
    from tremor import pipeline as pl

    basket = load_basket()
    picked = [a for a in basket.instruments if a.ticker in ("SPY", "GLD")]
    if len(picked) < 2 or not all(
            os.path.isdir(bars.store_path(bars.DEFAULT_BARS_DIR, a.file_stem))
            for a in picked):
        pytest.skip("needs the real bar store")
    small = replace(basket, assets=tuple(picked), outside=())

    serial, parallel = tmp_path / "serial", tmp_path / "parallel"
    versions = ("cfg", "run")
    pl.build_all(small, bars.DEFAULT_BARS_DIR, str(serial), versions)
    written = pl.write_all(small, bars.DEFAULT_BARS_DIR, str(parallel), versions,
                           workers=2)

    assert written == 2
    for asset in picked:
        one = pd.read_parquet(pl.metrics_path(str(serial), asset.file_stem))
        two = pd.read_parquet(pl.metrics_path(str(parallel), asset.file_stem))
        assert one.equals(two), asset.ticker


def test_a_single_worker_falls_back_to_the_serial_path(tmp_path, monkeypatch):
    # Not a special case to keep working by accident: it is how the command line
    # turns the pool off, and how anything without a usable multiprocessing
    # context still gets its metrics.
    from tremor import pipeline as pl

    called = []

    def serial(*args, **kwargs):
        called.append(args)
        return {"one": None, "two": None}

    monkeypatch.setattr(pl, "build_all", serial)
    assert pl.write_all(load_basket(), workers=1) == 2
    assert called

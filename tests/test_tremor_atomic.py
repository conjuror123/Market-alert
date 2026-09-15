import os

import pandas as pd
import pytest

from tremor import atomic


def test_a_failed_write_leaves_the_original_file(tmp_path):
    path = tmp_path / "actions.csv"
    path.write_text("ok\n", encoding="utf-8")

    def boom(_tmp):
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        atomic.write_replacing(str(path), boom)
    assert path.read_text(encoding="utf-8") == "ok\n"
    assert not os.path.exists(str(path) + ".tmp")


def test_write_parquet_replaces_and_leaves_no_tmp(tmp_path):
    path = str(tmp_path / "x.parquet")
    frame = pd.DataFrame({"hour_utc": [1], "close": [1.5]})
    atomic.write_parquet(path, frame)
    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")
    assert list(pd.read_parquet(path)["close"]) == [1.5]

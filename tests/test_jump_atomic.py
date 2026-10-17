import os

import pandas as pd
import pytest

from jump import atomic


def test_a_failed_write_leaves_the_original_file(tmp_path):
    path = tmp_path / "actions.csv"
    path.write_text("ok\n", encoding="utf-8")

    def boom(_tmp):
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        atomic.write_replacing(str(path), boom)
    assert path.read_text(encoding="utf-8") == "ok\n"
    assert not os.path.exists(str(path) + ".tmp")


def test_the_target_keeps_its_old_bytes_until_the_new_ones_are_complete(tmp_path):
    # A run killed mid-write must leave the last good file, not half of the
    # new one: the writer works on another path, and the target is only
    # swapped once it returns.
    path = tmp_path / "actions.csv"
    path.write_text("old\n", encoding="utf-8")
    seen = {}

    def write(tmp):
        seen["path"] = tmp
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("ne")
            seen["meanwhile"] = path.read_text(encoding="utf-8")
            f.write("w\n")

    atomic.write_replacing(str(path), write)
    assert seen["path"] != str(path)
    assert seen["meanwhile"] == "old\n"
    assert path.read_text(encoding="utf-8") == "new\n"


def test_a_write_that_dies_half_way_leaves_the_original(tmp_path):
    path = tmp_path / "actions.csv"
    path.write_text("ok\n", encoding="utf-8")

    def half(tmp):
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("trunc")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        atomic.write_replacing(str(path), half)
    assert path.read_text(encoding="utf-8") == "ok\n"


def test_write_parquet_replaces_and_leaves_no_tmp(tmp_path):
    path = str(tmp_path / "x.parquet")
    frame = pd.DataFrame({"hour_utc": [1], "close": [1.5]})
    atomic.write_parquet(path, frame)
    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")
    assert list(pd.read_parquet(path)["close"]) == [1.5]

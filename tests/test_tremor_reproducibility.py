"""Run reproducibility test.

The requirement sounds simple: a repeat run of the same period under the same
config_version must give an IDENTICAL set of events. It has to be checked on the
repeat rather than on a single run, because all three ways of breaking
reproducibility only show up the second time round.

The first is state in a file: the run appends its own columns to the same basket
metrics file it reads from, and the second run either fails on the overlapping
names or computes against its own previous result. The second is a version that
depends on its own output: take the fingerprint after the write and run_version
changes on every run, taking idempotency with it. The third is non-deterministic
iteration order over dicts and sets, which leaves the events the same but their
sequence different each time.
"""
import os

from tremor import versioning


# --- the set of events ------------------------------------------------------

def test_the_same_data_yields_the_same_run_version(tmp_path):
    (tmp_path / "bars").mkdir()
    (tmp_path / "bars" / "spy.parquet").write_bytes(b"bars")
    paths = [str(tmp_path / "bars")]
    config = "cfg"
    first = versioning.run_version(config, versioning.data_fingerprint(paths))
    second = versioning.run_version(config, versioning.data_fingerprint(paths))
    assert first == second


def test_a_revised_bar_yields_a_new_run_version(tmp_path):
    # Late or revised data must enter the recomputation under a NEW
    # version - otherwise a corrected bar quietly mixes with the old decisions.
    directory = tmp_path / "bars"
    directory.mkdir()
    bar = directory / "spy.parquet"
    bar.write_bytes(b"bars")
    before = versioning.data_fingerprint([str(directory)])
    bar.write_bytes(b"bars revised")
    assert versioning.data_fingerprint([str(directory)]) != before


def test_rewriting_a_file_with_the_same_bytes_keeps_the_version(tmp_path):
    # A backfill rewrites the file with the same bars. A fingerprint based on
    # modification time would declare that new data, and a recomputation over an
    # unchanged history would get a new run_version every time - meaning the
    # idempotency would not exist at all.
    directory = tmp_path / "bars"
    directory.mkdir()
    bar = directory / "spy.parquet"
    bar.write_bytes(b"bars")
    before = versioning.data_fingerprint([str(directory)])
    os.utime(bar, (0, 0))
    bar.write_bytes(b"bars")
    assert versioning.data_fingerprint([str(directory)]) == before


def test_derived_files_are_not_part_of_the_fingerprint():
    # Derived files are rewritten by every run. Were they in the fingerprint, a
    # repeat run over the same data would get a new version simply because the
    # previous one rewrote them.
    raw = set(versioning.RAW_INPUTS)
    for derived in ("data/tremor/metrics", "data/tremor/jumps.parquet"):
        assert derived not in raw


def test_a_file_added_inside_a_directory_changes_the_fingerprint(tmp_path):
    # A directory cannot serve as a fingerprint on its own: only its modification
    # time changes, and a file appended inside does not always touch it.
    directory = tmp_path / "bars"
    directory.mkdir()
    (directory / "spy.parquet").write_bytes(b"a")
    before = versioning.data_fingerprint([str(directory)])
    (directory / "qqq.parquet").write_bytes(b"b")
    assert versioning.data_fingerprint([str(directory)]) != before


def test_the_config_version_does_not_depend_on_the_run(tmp_path):
    # config_version reads only the configuration and the code: two consecutive
    # runs over different data must give one and the same configuration version.
    (tmp_path / "windows.py").write_text("W = 1", encoding="utf-8")
    inputs = ("windows.py",)
    assert versioning.config_version(str(tmp_path), inputs) \
        == versioning.config_version(str(tmp_path), inputs)

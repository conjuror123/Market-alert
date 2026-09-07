import os

import pytest

from tremor import versioning


def write(tmp_path, name, text):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_config_version_is_stable_for_identical_input(tmp_path):
    write(tmp_path, "config/basket.yaml", "assets: []")
    inputs = ("config/basket.yaml",)
    first = versioning.config_version(str(tmp_path), inputs)
    second = versioning.config_version(str(tmp_path), inputs)
    assert first == second


def test_changing_a_threshold_changes_the_version(tmp_path):
    # §6.3: changing parameters retroactively without a recomputation is
    # forbidden. The version must notice the edit by itself - a manual counter,
    # which people forget to increment, cannot be relied on.
    inputs = ("windows.py",)
    write(tmp_path, "windows.py", "THRESHOLD = 7")
    before = versioning.config_version(str(tmp_path), inputs)
    write(tmp_path, "windows.py", "THRESHOLD = 8")
    assert versioning.config_version(str(tmp_path), inputs) != before


def test_a_missing_file_is_part_of_the_state(tmp_path):
    inputs = ("missing.yaml",)
    absent = versioning.config_version(str(tmp_path), inputs)
    write(tmp_path, "missing.yaml", "x")
    assert versioning.config_version(str(tmp_path), inputs) != absent


def test_file_order_does_not_matter(tmp_path):
    write(tmp_path, "a.py", "1")
    write(tmp_path, "b.py", "2")
    assert (versioning.config_version(str(tmp_path), ("a.py", "b.py"))
            == versioning.config_version(str(tmp_path), ("b.py", "a.py")))


def test_run_version_is_idempotent_for_unchanged_data(tmp_path):
    # §6.2: a repeat run of the same hour under the same version creates no duplicates.
    data = write(tmp_path, "bars.parquet", "x" * 100)
    first = versioning.run_version("cfg", versioning.data_fingerprint([str(data)]))
    second = versioning.run_version("cfg", versioning.data_fingerprint([str(data)]))
    assert first == second


def test_late_data_produces_a_new_run_version(tmp_path):
    data = write(tmp_path, "bars.parquet", "x" * 100)
    before = versioning.run_version("cfg", versioning.data_fingerprint([str(data)]))
    data.write_text("x" * 200, encoding="utf-8")
    os.utime(data, (0, 0))
    assert versioning.run_version("cfg", versioning.data_fingerprint([str(data)])) != before


def test_a_config_change_alone_changes_the_run_version(tmp_path):
    data = write(tmp_path, "bars.parquet", "x")
    fingerprint = versioning.data_fingerprint([str(data)])
    assert (versioning.run_version("cfg-a", fingerprint)
            != versioning.run_version("cfg-b", fingerprint))


def test_real_config_version_is_a_short_hex_string():
    version = versioning.config_version()
    assert len(version) == versioning.VERSION_LENGTH
    assert all(c in "0123456789abcdef" for c in version)


def frame(*event_ids):
    import pandas as pd
    return pd.DataFrame({"event_id": list(event_ids), "value": range(len(event_ids))})


def test_stamp_writes_both_versions_into_every_row():
    stamped = versioning.stamp(frame("a", "b"), "cfg1", "run1")
    assert list(stamped["config_version"]) == ["cfg1", "cfg1"]
    assert list(stamped["run_version"]) == ["run1", "run1"]


def written(events, config="cfg", data="data1", previous=None, now=1000):
    """A table as a run would leave it on disk: stamped, then given provenance."""
    run = versioning.run_version(config, data)
    return versioning.provenance(versioning.stamp(frame(*events), config, run),
                                 previous, data, now=now)


def test_created_at_survives_a_rerun_over_unchanged_data():
    # §6.2: a rerun with the same version must be idempotent. Taking the clock
    # again would make the table differ byte for byte between two identical runs.
    first = written(["a"], now=1000)
    again = written(["a"], previous=first, now=2000)
    assert list(again["created_at"]) == [1000]
    assert list(again["recalculated"]) == [False]


def test_revised_data_marks_the_row_recalculated():
    first = written(["a"], data="data1", now=1000)
    revised = written(["a"], data="data2", previous=first, now=2000)
    # The row was rebuilt on different data, but it is the same event and keeps
    # the moment it first appeared.
    assert list(revised["recalculated"]) == [True]
    assert list(revised["created_at"]) == [1000]


def test_an_edit_to_the_code_alone_does_not_mark_the_row_recalculated():
    # §6.2 raises the flag for late or revised DATA. A moved threshold or a
    # reworded comment moves config_version and with it run_version, and during
    # calibration that happens on every iteration - a flag that stood at True on
    # every row would say nothing at all.
    first = written(["a"], config="cfg1", data="same", now=1000)
    recoded = written(["a"], config="cfg2", data="same", previous=first, now=2000)

    assert first["run_version"].iloc[0] != recoded["run_version"].iloc[0]
    assert list(recoded["recalculated"]) == [False]
    assert list(recoded["created_at"]) == [1000]


def test_a_row_seen_for_the_first_time_is_not_recalculated():
    first = written(["a"], data="data1", now=1000)
    grown = written(["a", "b"], data="data2", previous=first, now=2000)
    assert list(grown["recalculated"]) == [True, False]
    assert list(grown["created_at"]) == [1000, 2000]


def test_provenance_without_a_previous_table_claims_nothing():
    fresh = written(["a", "b"], now=1000)
    assert list(fresh["recalculated"]) == [False, False]


def test_the_fingerprint_is_stored_so_the_next_run_can_compare():
    stored = written(["a"], data="data1")
    assert list(stored["data_fingerprint"]) == ["data1"]


def test_stamps_returns_a_run_version_built_from_the_other_two(tmp_path):
    write(tmp_path, "config/basket.yaml", "assets: []")
    write(tmp_path, "raw.csv", "a,b\n1,2\n")
    config, run, fingerprint = versioning.stamps(
        (str(tmp_path / "raw.csv"),), str(tmp_path))
    assert run == versioning.run_version(config, fingerprint)


def test_previous_table_returns_none_when_there_is_no_file(tmp_path):
    assert versioning.previous_table(str(tmp_path / "absent.parquet")) is None


def test_an_unreadable_previous_table_does_not_stop_the_run(tmp_path):
    broken = tmp_path / "broken.parquet"
    broken.write_text("not a parquet file", encoding="utf-8")
    assert versioning.previous_table(str(broken)) is None

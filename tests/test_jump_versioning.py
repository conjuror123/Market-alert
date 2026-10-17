import os

import pytest

from jump import versioning


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
    # Changing parameters retroactively without a recomputation is
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
    # A repeat run of the same hour under the same version creates no duplicates.
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


def test_stamps_returns_a_run_version_built_from_the_other_two(tmp_path):
    write(tmp_path, "config/basket.yaml", "assets: []")
    write(tmp_path, "raw.csv", "a,b\n1,2\n")
    config, run, fingerprint = versioning.stamps(
        (str(tmp_path / "raw.csv"),), str(tmp_path))
    assert run == versioning.run_version(config, fingerprint)


def test_a_comment_does_not_change_the_version(tmp_path):
    # THE ONE THAT WENT WRONG. A commit that only rewrote comments moved
    # config_version, which forced a cold rebuild of all 61 instruments; the
    # recomputed table differed from the extended one it replaced, and the rows
    # it gained were posted to a note that had closed seven hours earlier. A
    # comment cannot change a number, so it must not change the version.
    inputs = ("severity.py",)
    write(tmp_path, "severity.py", '"""Tiers."""\n# how rare is rare\nMAJOR = 3.0\n')
    before = versioning.config_version(str(tmp_path), inputs)

    write(tmp_path, "severity.py",
          '"""Tiers, and why these four.\n\n    At length.\n    """\nMAJOR = 3.0\n')
    assert versioning.config_version(str(tmp_path), inputs) == before


def test_a_formula_still_changes_the_version_when_the_comment_stays(tmp_path):
    # The other half of the same guard: ignoring comments must not make the
    # version ignore the edit sitting next to one.
    inputs = ("severity.py",)
    write(tmp_path, "severity.py", '"""Tiers."""\nMAJOR = 3.0\n')
    before = versioning.config_version(str(tmp_path), inputs)
    write(tmp_path, "severity.py", '"""Tiers."""\nMAJOR = 3.5\n')
    assert versioning.config_version(str(tmp_path), inputs) != before


def test_a_string_the_code_uses_is_not_a_docstring(tmp_path):
    # Only the leading string of a module, class or function is dropped. A
    # string that is assigned, returned or compared is a value like any other.
    inputs = ("severity.py",)
    write(tmp_path, "severity.py", 'TIER = "major"\n')
    before = versioning.config_version(str(tmp_path), inputs)
    write(tmp_path, "severity.py", 'TIER = "extreme"\n')
    assert versioning.config_version(str(tmp_path), inputs) != before


def test_a_comment_in_the_basket_still_changes_the_version(tmp_path):
    # basket.yaml is hashed as bytes: there is no cheap parse that separates a
    # comment from the value beside it, and erring towards a needless rebuild is
    # the safe direction for the file that lists what is watched at all.
    inputs = ("config/basket.yaml",)
    write(tmp_path, "config/basket.yaml", "assets: []\n")
    before = versioning.config_version(str(tmp_path), inputs)
    write(tmp_path, "config/basket.yaml", "# sixty-one instruments\nassets: []\n")
    assert versioning.config_version(str(tmp_path), inputs) != before


def test_every_python_configuration_input_can_be_parsed():
    # config_version now parses each .py input, so a file it cannot parse would
    # fail the whole run rather than one hash. They all import at test time, but
    # this states the dependency where the list lives.
    for relative in versioning.CONFIG_INPUTS:
        if relative.endswith(".py"):
            assert versioning._content(relative)

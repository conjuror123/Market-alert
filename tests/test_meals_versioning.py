import os

import pytest

from meals import versioning


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
    # П.6.3: ретро-изменение параметров без пересчёта запрещено. Версия обязана
    # заметить правку сама - на ручной счётчик, который забывают увеличить,
    # полагаться нельзя.
    inputs = ("windows.py",)
    write(tmp_path, "windows.py", "THRESHOLD = 7")
    before = versioning.config_version(str(tmp_path), inputs)
    write(tmp_path, "windows.py", "THRESHOLD = 8")
    assert versioning.config_version(str(tmp_path), inputs) != before


def test_a_missing_file_is_part_of_the_state(tmp_path):
    inputs = ("нет.yaml",)
    absent = versioning.config_version(str(tmp_path), inputs)
    write(tmp_path, "нет.yaml", "x")
    assert versioning.config_version(str(tmp_path), inputs) != absent


def test_file_order_does_not_matter(tmp_path):
    write(tmp_path, "a.py", "1")
    write(tmp_path, "b.py", "2")
    assert (versioning.config_version(str(tmp_path), ("a.py", "b.py"))
            == versioning.config_version(str(tmp_path), ("b.py", "a.py")))


def test_run_version_is_idempotent_for_unchanged_data(tmp_path):
    # П.6.2: повторный прогон того же часа с той же версией не создаёт дублей.
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

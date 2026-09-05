"""Run reproducibility test (spec §6.7).

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
import json
import os

import numpy as np
import pandas as pd

from meals import cluster, export, journal, saed, severity, versioning
from meals.basket import Asset

HOUR = 3600


def basket_frame(n=400):
    """The frame cross_section produces: without cluster's derived columns."""
    index = pd.Index([(i + 1) * HOUR for i in range(n)], name="hour_utc")
    rng = np.random.default_rng(20260901)
    return pd.DataFrame({
        "quorum_ok": pd.array([True] * n, dtype="boolean"),
        "n_assets": np.full(n, 12),
        "csv_norm": rng.normal(1.0, 0.1, n),
        "m_weighted_median": rng.normal(0.0, 0.005, n),
        "pc1_ratio": rng.uniform(0.2, 0.8, n),
        "single_factor": pd.array([False] * n, dtype="boolean"),
    }, index=index)


def with_derived(frame):
    """What the cluster run itself appends to that same file."""
    return frame.assign(
        m_calendar=1.0, m_vix=1.0, si_total=0.0, sigma_m=0.01, k=2.0,
        decision="", base_points=0, breadth_q99=False,
        n_active_blocks=0, n_active_blocks_q99=0,
        trigger_price_shock=False, trigger_volume=False,
        trigger_cluster_shift=False, trigger_single_factor=False,
    ).astype({"base_points": int, "n_active_blocks": int})


def run_frame(n=400, fire_at=(50, 200, 300)):
    frame = basket_frame(n).assign(
        trigger_cluster_shift=False, si_total=0.0, base_points=0,
        breadth_q99=False, sigma_m=0.01, k=2.0)
    for position in fire_at:
        hour = frame.index[position]
        frame.loc[hour, ["trigger_cluster_shift", "si_total", "base_points"]] = \
            [True, 12.0, 8]
    return frame


# --- the set of events ------------------------------------------------------

def test_repeating_the_run_gives_the_identical_event_set():
    frame = run_frame()
    first, first_journal = cluster.run(frame)
    second, second_journal = cluster.run(frame)

    assert cluster.events_frame(first).equals(cluster.events_frame(second))
    assert cluster.escalations_frame(first).equals(cluster.escalations_frame(second))
    assert first_journal.equals(second_journal)


def test_the_event_order_is_stable_not_merely_the_set():
    # The set matched but the order diverged - that is no longer
    # reproducibility: events are numbered and linked by parent_event_id, and a
    # permutation breaks the references.
    frame = run_frame()
    first = [(e.event_id, e.t0_utc) for e in cluster.run(frame)[0]]
    second = [(e.event_id, e.t0_utc) for e in cluster.run(frame)[0]]
    assert first == second
    assert [t0 for _, t0 in first] == sorted(t0 for _, t0 in first)


def test_the_runner_does_not_trip_over_its_own_output():
    # The main source of divergence: the basket metrics file is both input and
    # output. The second run reads a frame that already has the derived columns.
    frame = basket_frame()
    reused = cluster.reset_derived(with_derived(frame))
    assert list(reused.columns) == list(frame.columns)
    assert reused.equals(frame)


def test_resetting_derived_columns_is_idempotent():
    frame = basket_frame()
    once = cluster.reset_derived(with_derived(frame))
    assert cluster.reset_derived(once).equals(once)


# --- versions ---------------------------------------------------------------

def test_the_same_data_yields_the_same_run_version(tmp_path):
    (tmp_path / "bars").mkdir()
    (tmp_path / "bars" / "spy.parquet").write_bytes(b"bars")
    paths = [str(tmp_path / "bars")]
    config = "cfg"
    first = versioning.run_version(config, versioning.data_fingerprint(paths))
    second = versioning.run_version(config, versioning.data_fingerprint(paths))
    assert first == second


def test_a_revised_bar_yields_a_new_run_version(tmp_path):
    # §6.2: late or revised data must enter the recomputation under a NEW
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
    # idempotency of §6.2 would not exist at all.
    directory = tmp_path / "bars"
    directory.mkdir()
    bar = directory / "spy.parquet"
    bar.write_bytes(b"bars")
    before = versioning.data_fingerprint([str(directory)])
    os.utime(bar, (0, 0))
    bar.write_bytes(b"bars")
    assert versioning.data_fingerprint([str(directory)]) == before


def test_derived_files_are_not_part_of_the_fingerprint():
    # The basket metrics are both input and output for the cluster run. Were they
    # in the fingerprint, a repeat run over the same data would get a new version
    # simply because the previous one rewrote the file.
    raw = set(versioning.RAW_INPUTS)
    for derived in ("data/meals/metrics_basket_hour.parquet", "data/meals/metrics",
                    "data/meals/residuals", "data/meals/decision_log.parquet"):
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


# --- journal and export -----------------------------------------------------

def test_the_journal_is_identical_between_runs():
    frame = with_derived(basket_frame()).assign(
        csv_norm_q10=0.85, pc1_threshold=0.6,
        csv_compression=pd.array([False] * 400, dtype="boolean"),
        pca_sync=pd.array([False] * 400, dtype="boolean"))
    first = journal.stamp(journal.basket_decisions(frame), "cfg", "run")
    second = journal.stamp(journal.basket_decisions(frame), "cfg", "run")
    assert first.equals(second)


def test_the_exported_json_is_byte_identical_between_runs(tmp_path):
    from tests.test_meals_export import (EMPTY_ESCALATIONS, EMPTY_SAED,
                                         asset_metrics, basket_frame as export_frame,
                                         event, residual_frame)

    def build():
        return export.build_event(
            event(30 * HOUR), export_frame(), {"twelvedata:SPY": asset_metrics()},
            {"twelvedata:SPY": residual_frame()}, EMPTY_SAED, EMPTY_ESCALATIONS,
            "cfg", "run")

    first = export.write_event(build(), str(tmp_path))
    text = open(first, encoding="utf-8").read()
    second = export.write_event(build(), str(tmp_path))
    assert first == second
    assert open(second, encoding="utf-8").read() == text
    assert json.loads(text)["event_id"] == f"cluster:{30 * HOUR}"


def test_saed_events_keep_their_order_across_runs():
    asset = Asset(ticker="SPY", source="twelvedata", tier=1, block="equity",
                  has_volume=True, session_template="us_equity",
                  fetch_interval="30min", label="S&P 500", in_basket=True,
                  tick_size=0.01)
    n = 200
    frame = pd.DataFrame({
        "hour_utc": [(i + 1) * HOUR for i in range(n)],
        "z_resid": np.zeros(n), "e_resid": np.zeros(n), "r": np.zeros(n),
        "beta": np.full(n, 0.9), "sigma_lt_resid": np.full(n, 0.01),
        "q99_resid": np.full(n, 3.0),
        "tier": pd.array([pd.NA] * n, dtype="string"),
    })
    for name in severity.TIERS:
        frame[f"level_{name}"] = 5.0
    for position in (20, 21, 100):
        frame.loc[position, ["z_resid", "e_resid", "r"]] = [6.0, 0.05, 0.06]
        frame.loc[position, "tier"] = "routine"

    first = saed.events_frame(saed.build_events(asset, frame))
    second = saed.events_frame(saed.build_events(asset, frame))
    assert first.equals(second)
    assert list(first["hour_utc"]) == sorted(first["hour_utc"])

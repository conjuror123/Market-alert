import json
import os

import numpy as np
import pandas as pd
import pytest

from meals import export

HOUR = 3600


def hours(n=60):
    return np.array([(i + 1) * HOUR for i in range(n)], dtype="int64")


def basket_frame(n=60):
    index = pd.Index(hours(n), name="hour_utc")
    return pd.DataFrame({
        "csv_norm": np.linspace(0.8, 1.2, n),
        "pc1_ratio": np.linspace(0.3, 0.7, n),
        "mean_pairwise_corr": np.linspace(0.1, 0.5, n),
        "m_weighted_median": np.linspace(-0.01, 0.01, n),
        "si_total": np.zeros(n),
        "base_points": np.zeros(n, dtype=int),
        "n_assets": np.full(n, 12),
        "quorum_ok": pd.array([True] * n, dtype="boolean"),
    }, index=index)


def asset_metrics(n=60, asset_id="twelvedata:SPY", block="equity"):
    return pd.DataFrame({
        "hour_utc": hours(n),
        "asset_id": asset_id,
        "block": block,
        "r": np.linspace(-0.02, 0.02, n),
        "z": np.linspace(-3.0, 3.0, n),
        "v_r": np.linspace(0.0, 2.0, n),
    })


def residual_frame(n=60, asset_id="twelvedata:SPY"):
    return pd.DataFrame({
        "hour_utc": hours(n),
        "asset_id": asset_id,
        "beta": np.full(n, 0.9),
        "e_resid": np.linspace(-0.01, 0.01, n),
        "z_resid": np.linspace(-2.0, 2.0, n),
    })


def event(t0, **overrides):
    row = {"event_id": f"cluster:{t0}", "t0_utc": t0, "status": "finished",
           "parent_event_id": None, "si_total_t0": 11.5, "base_points_t0": 7}
    row.update(overrides)
    return pd.Series(row)


EMPTY_SAED = pd.DataFrame(columns=["event_id", "asset_id", "block", "hour_utc",
                                   "z_resid", "e_resid", "r", "repeat_count",
                                   "tier"])
EMPTY_ESCALATIONS = pd.DataFrame(columns=["event_id", "seq", "hour_utc", "kind",
                                          "si_total", "reason"])


def build(t0, frame=None, metrics=None, residuals=None,
          saed=EMPTY_SAED, escalations=EMPTY_ESCALATIONS):
    return export.build_event(
        event(t0), frame if frame is not None else basket_frame(),
        metrics if metrics is not None else {"twelvedata:SPY": asset_metrics()},
        residuals if residuals is not None else {"twelvedata:SPY": residual_frame()},
        saed, escalations, "cfg", "run")


# --- the window -------------------------------------------------------------

def test_window_is_symmetric_and_includes_t0():
    window, left, right = export.window_hours(hours(60), 30 * HOUR)
    assert len(window) == 2 * export.WINDOW_HOURS + 1
    assert window[export.WINDOW_HOURS] == 30 * HOUR
    assert not left and not right


def test_window_is_counted_in_positions_not_in_seconds():
    # Reference-calendar hours have a break at the weekend: between Friday evening
    # and Sunday evening lies a two-day hole. The window must count twelve CALENDAR
    # HOURS, not twelve times 3600 seconds, or the right arm of a Friday event goes
    # into the void.
    gapped = np.concatenate([hours(20), hours(20) + 100 * HOUR])
    window, _, _ = export.window_hours(gapped, gapped[19], arm=3)
    assert list(window) == list(gapped[16:23])
    assert window[-1] - window[0] > len(window) * HOUR


def test_truncation_is_reported_on_both_edges():
    _, left, right = export.window_hours(hours(60), 2 * HOUR)
    assert left and not right
    _, left, right = export.window_hours(hours(60), 60 * HOUR)
    assert not left and right


def test_an_unknown_t0_yields_an_empty_window():
    window, left, right = export.window_hours(hours(60), 7 * HOUR + 1)
    assert len(window) == 0 and left and right


# --- contents ---------------------------------------------------------------

def test_every_series_has_the_length_of_the_window():
    payload = build(30 * HOUR)
    width = len(payload["window"]["hours_utc"])
    for values in payload["basket"].values():
        assert len(values) == width
    for entry in payload["assets"].values():
        for values in entry["series"].values():
            assert len(values) == width


def test_hours_without_data_become_null_rather_than_disappearing():
    metrics = asset_metrics()
    metrics = metrics[metrics["hour_utc"] != 30 * HOUR]
    payload = build(30 * HOUR, metrics={"twelvedata:SPY": metrics})
    series = payload["assets"]["twelvedata:SPY"]["series"]["r"]
    assert len(series) == 2 * export.WINDOW_HOURS + 1
    assert series[export.WINDOW_HOURS] is None


def test_an_asset_silent_through_the_whole_window_is_dropped():
    # A night-time event would otherwise drag along twelve closed ETFs with series
    # of nothing but null.
    closed = asset_metrics(asset_id="twelvedata:GLD", block="commodities")
    closed[["r", "z", "v_r"]] = np.nan
    payload = build(30 * HOUR,
                    metrics={"twelvedata:SPY": asset_metrics(),
                             "twelvedata:GLD": closed},
                    residuals={})
    assert "twelvedata:GLD" not in payload["assets"]
    assert "twelvedata:SPY" in payload["assets"]


def test_residual_series_is_joined_to_the_asset():
    payload = build(30 * HOUR)
    series = payload["assets"]["twelvedata:SPY"]["series"]
    expected = residual_frame().set_index("hour_utc")["z_resid"]
    assert series["z_resid"] == pytest.approx(
        expected.reindex(payload["window"]["hours_utc"]).tolist())


def test_only_saed_events_inside_the_window_are_listed():
    saed = pd.DataFrame({
        "event_id": ["a", "b"], "asset_id": ["twelvedata:SPY"] * 2,
        "block": ["equity"] * 2, "hour_utc": [30 * HOUR, 55 * HOUR],
        "z_resid": [5.0, 6.0], "e_resid": [0.01, 0.02], "r": [0.02, 0.03],
        "repeat_count": [0, 1], "tier": ["major", "routine"],
    })
    payload = build(30 * HOUR, saed=saed)
    assert [e["event_id"] for e in payload["saed_events"]] == ["a"]


def test_the_exported_event_carries_its_severity_tier():
    # The tier is the headline fact about a single-asset event - how rare the
    # move was for that instrument - so it travels with the export rather than
    # having to be recomputed from the residuals by whatever reads it.
    saed = pd.DataFrame({
        "event_id": ["a"], "asset_id": ["twelvedata:SPY"], "block": ["equity"],
        "hour_utc": [30 * HOUR], "z_resid": [9.0], "e_resid": [0.05],
        "r": [0.06], "repeat_count": [0], "tier": ["extreme"],
    })
    assert build(30 * HOUR, saed=saed)["saed_events"][0]["tier"] == "extreme"


def test_a_saed_event_without_a_tier_exports_null_rather_than_a_guess():
    saed = pd.DataFrame({
        "event_id": ["a"], "asset_id": ["twelvedata:SPY"], "block": ["equity"],
        "hour_utc": [30 * HOUR], "z_resid": [9.0], "e_resid": [0.05],
        "r": [0.06], "repeat_count": [0], "tier": [pd.NA],
    })
    assert build(30 * HOUR, saed=saed)["saed_events"][0]["tier"] is None


def test_only_the_events_own_escalations_are_listed():
    escalations = pd.DataFrame({
        "event_id": [f"cluster:{30 * HOUR}", "cluster:999"],
        "seq": [1, 1], "hour_utc": [33 * HOUR, 999],
        "kind": ["higher_order_shock"] * 2, "si_total": [20.0, 21.0],
        "reason": ["breadth_q99", "breadth_q99"],
    })
    payload = build(30 * HOUR, escalations=escalations)
    assert [e["hour_utc"] for e in payload["escalations"]] == [33 * HOUR]


def test_versions_are_carried_into_the_payload():
    payload = build(30 * HOUR)
    assert payload["config_version"] == "cfg" and payload["run_version"] == "run"


# --- serialisation ----------------------------------------------------------

def test_payload_is_strict_json_without_nan_literals():
    # json.dumps emits NaN as the literal NaN - not JSON, and a strict parser on
    # the other side chokes on it. A gap must become null.
    metrics = asset_metrics()
    metrics.loc[metrics.index[10], "z"] = np.nan
    payload = build(30 * HOUR, metrics={"twelvedata:SPY": metrics})
    text = json.dumps(payload, allow_nan=False)
    assert "NaN" not in text
    assert json.loads(text) == payload


def test_boolean_series_survives_the_round_trip():
    frame = basket_frame()
    frame.loc[30 * HOUR, "quorum_ok"] = pd.NA
    payload = build(30 * HOUR, frame=frame)
    quorum = payload["basket"]["quorum_ok"]
    assert quorum[export.WINDOW_HOURS] is None
    assert quorum[0] is True


def test_file_name_has_no_colon(tmp_path):
    # Windows does not accept a colon in a file name, and the export has to be
    # readable there too.
    path = export.write_event(build(30 * HOUR), str(tmp_path))
    assert ":" not in path.rsplit("/", 1)[-1]
    assert json.loads(open(path, encoding="utf-8").read())["event_id"] \
        == f"cluster:{30 * HOUR}"


def test_export_matches_its_published_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.load(open(export.SCHEMA_PATH, encoding="utf-8"))
    # Validated with the optional blocks populated, not just the skeleton: the
    # schema declares additionalProperties false, so a field added to the export
    # and not to the schema is a failure only if something actually fills it.
    saed = pd.DataFrame({
        "event_id": ["a"], "asset_id": ["twelvedata:SPY"], "block": ["equity"],
        "hour_utc": [30 * HOUR], "z_resid": [9.0], "e_resid": [0.05],
        "r": [0.06], "repeat_count": [0], "tier": ["extreme"],
    })
    jsonschema.validate(build(30 * HOUR, saed=saed), schema)
    jsonschema.validate(build(30 * HOUR), schema)


def test_stale_exports_are_removed(tmp_path):
    # An event that stops existing - the calendar archive was rebuilt, a
    # threshold moved - would otherwise leave its file behind looking exactly
    # like current output.
    kept = export.write_event(build(30 * HOUR), str(tmp_path))
    ghost = tmp_path / "cluster_1643223600.json"
    ghost.write_text('{"event_id": "cluster:1643223600"}\n', encoding="utf-8")

    removed = export.prune_stale(str(tmp_path), {kept})

    assert removed == [str(ghost)]
    assert sorted(p.name for p in tmp_path.iterdir()) == [os.path.basename(kept)]


def test_pruning_leaves_alone_what_is_not_json(tmp_path):
    # The directory may hold a note or an index next to the exports; only the
    # export files themselves are this run's to replace.
    note = tmp_path / "README.txt"
    note.write_text("hand-written\n", encoding="utf-8")

    assert export.prune_stale(str(tmp_path), set()) == []
    assert note.exists()

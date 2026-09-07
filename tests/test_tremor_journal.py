import numpy as np
import pandas as pd

from tremor import journal, si_index, windows
from tremor.cross_section import QUORUM_MIN_ASSETS

HOUR = 3600


def hours(n):
    return np.array([(i + 1) * HOUR for i in range(n)], dtype="int64")


def basket_frame(n=30):
    index = pd.Index(hours(n), name="hour_utc")
    return pd.DataFrame({
        "quorum_ok": pd.array([True] * n, dtype="boolean"),
        "n_assets": np.full(n, 12),
        "csv_norm": np.linspace(0.8, 1.2, n),
        "csv_norm_q10": np.full(n, 0.85),
        "csv_compression": pd.array([False] * n, dtype="boolean"),
        "pc1_ratio": np.linspace(0.3, 0.7, n),
        "pc1_threshold": np.full(n, 0.6),
        "pca_sync": pd.array([False] * n, dtype="boolean"),
        "single_factor": pd.array([False] * n, dtype="boolean"),
        "trigger_price_shock": pd.array([False] * n, dtype="boolean"),
        "trigger_volume": pd.array([False] * n, dtype="boolean"),
        "trigger_cluster_shift": pd.array([False] * n, dtype="boolean"),
        "base_points": np.zeros(n, dtype=int),
        "si_total": np.zeros(n),
    }, index=index)


def asset_metrics(n=30, asset_id="twelvedata:SPY"):
    return pd.DataFrame({
        "hour_utc": hours(n),
        "asset_id": asset_id,
        "z": np.linspace(0.0, 4.0, n),
        "q95": np.full(n, 2.0),
        "q99": np.full(n, 3.0),
        "breach_q95": pd.array([False] * n, dtype="boolean"),
        "breach_q99": pd.array([False] * n, dtype="boolean"),
        "v_r": np.full(n, 0.5),
    })


def test_a_row_carries_the_value_the_threshold_and_the_outcome():
    # The point of the §6.1 journal is neither the value nor the outcome on its
    # own but the pairing "what was it compared against". That cannot be
    # reconstructed afterwards from the metrics and the events.
    rows = journal.basket_decisions(basket_frame())
    quorum = rows[rows["trigger"] == "quorum"]
    assert len(quorum) == 30
    assert (quorum["value"] == 12).all()
    assert (quorum["threshold"] == QUORUM_MIN_ASSETS).all()
    assert quorum["result"].all()


def test_an_hour_without_quorum_is_absent_rather_than_false():
    # §2.3: without quorum the cluster triggers get NULL, not False. In the
    # journal that means no row at all: "not assessed" and "assessed and did not
    # fire" are different things, and confusing them in a backtest is expensive.
    frame = basket_frame()
    frame.loc[5 * HOUR, ["quorum_ok", "csv_compression", "pca_sync",
                         "single_factor"]] = pd.NA
    rows = journal.basket_decisions(frame)
    compression = rows[rows["trigger"] == "csv_compression"]
    assert 5 * HOUR not in set(compression["hour_utc"])
    assert len(compression) == 29


def test_the_gate_is_journalled_against_the_si_threshold():
    frame = basket_frame()
    frame.loc[7 * HOUR, "si_total"] = si_index.THRESHOLD + 1
    rows = journal.basket_decisions(frame)
    gate = rows[rows["trigger"] == "gate"].set_index("hour_utc")
    assert bool(gate.loc[7 * HOUR, "result"])
    assert not bool(gate.loc[6 * HOUR, "result"])
    assert (gate["threshold"] == si_index.THRESHOLD).all()


def test_asset_rows_are_written_only_where_a_trigger_fired():
    # Twenty-three instruments over thirty-five thousand hours would give millions
    # of "nothing happened" rows - while the values themselves already sit in
    # metrics_asset_hour.
    metrics = asset_metrics()
    metrics.loc[metrics.index[20], ["breach_q95", "breach_q99"]] = True
    rows = journal.asset_decisions({"twelvedata:SPY": metrics})
    assert set(rows["hour_utc"]) == {21 * HOUR}
    assert set(rows["trigger"]) == {"breach_q95", "breach_q99"}
    assert set(rows["scope"]) == {"twelvedata:SPY"}


def test_volume_confirmation_is_journalled_where_it_holds():
    metrics = asset_metrics()
    metrics.loc[metrics.index[3], "v_r"] = windows.VOLUME_CONFIRM + 1
    rows = journal.asset_decisions({"twelvedata:SPY": metrics})
    volume = rows[rows["trigger"] == "volume_confirm"]
    assert list(volume["hour_utc"]) == [4 * HOUR]
    assert volume["threshold"].iloc[0] == windows.VOLUME_CONFIRM


def test_an_asset_without_volume_produces_no_volume_rows():
    # §3.5: for an instrument without exchange volume V_R = NULL, and that is
    # "not assessed", not "did not confirm".
    metrics = asset_metrics(asset_id="twelvedata:EUR/USD")
    metrics["v_r"] = np.nan
    rows = journal.asset_decisions({"twelvedata:EUR/USD": metrics})
    assert rows[rows["trigger"] == "volume_confirm"].empty


def test_saed_rows_come_from_the_residual_frames():
    metrics = asset_metrics()
    residuals = pd.DataFrame({
        "hour_utc": hours(30),
        "z_resid": np.zeros(30),
        "e_resid": np.zeros(30),
        "sigma_lt_resid": np.full(30, 0.01),
        "q99_resid": np.full(30, 3.0),
    })
    residuals.loc[residuals.index[9], ["z_resid", "e_resid"]] = [6.0, 0.05]
    rows = journal.asset_decisions({"twelvedata:SPY": metrics},
                                   {"twelvedata:SPY": residuals})
    saed = rows[rows["trigger"] == "saed"]
    assert list(saed["hour_utc"]) == [10 * HOUR]
    assert saed["value"].iloc[0] == 6.0 and saed["threshold"].iloc[0] == 3.0


def test_stamp_puts_the_versions_on_every_row():
    # §6.3: decisions from different versions may be compared only with the
    # versions stated explicitly - so the version must live in the row, not in a
    # file name.
    rows = journal.stamp(journal.basket_decisions(basket_frame()), "cfg", "run")
    assert list(rows.columns) == journal.COLUMNS
    assert (rows["config_version"] == "cfg").all()
    assert (rows["run_version"] == "run").all()


def test_first_valid_hour_marks_where_the_warm_up_ends():
    metrics = asset_metrics()
    metrics.loc[metrics.index[:10], ["z", "breach_q95", "breach_q99"]] = None
    table = journal.first_valid_hour({"twelvedata:SPY": metrics}, basket_frame())
    row = table[(table["scope"] == "twelvedata:SPY") & (table["trigger"] == "z")].iloc[0]
    assert row["first_valid_hour"] == 11 * HOUR
    assert row["evaluated_hours"] == 20 and row["total_hours"] == 30


def test_first_valid_hour_covers_the_basket_triggers_too():
    table = journal.first_valid_hour({}, basket_frame())
    assert set(table["trigger"]) == {"quorum_ok", "csv_compression", "pca_sync",
                                     "single_factor"}
    assert (table["scope"] == "basket").all()

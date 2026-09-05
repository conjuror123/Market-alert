import numpy as np
import pandas as pd

from meals import persistence as ps

HOUR = 3600


def bars(abnormal, sigma=0.001, raw=None):
    n = len(abnormal)
    return pd.DataFrame({
        "hour_utc": [(i + 1) * HOUR for i in range(n)],
        "e_resid": abnormal,
        "r": raw if raw is not None else abnormal,
        "sigma_lt_resid": [sigma] * n,
    })


def test_forward_car_sums_the_bar_and_the_horizon_after_it():
    car = ps.forward_car(np.array([1.0, 2.0, 3.0, 4.0, 5.0]), 2)
    assert car[0] == 6.0        # 1 + 2 + 3
    assert car[1] == 9.0        # 2 + 3 + 4
    assert car[2] == 12.0       # 3 + 4 + 5


def test_the_last_bars_get_no_answer_rather_than_a_short_one():
    # A partial window quietly answers a different question - "how much held
    # over the three bars that happen to exist" - and at the end of history
    # that is every event the live system has just produced.
    car = ps.forward_car(np.array([1.0, 2.0, 3.0, 4.0, 5.0]), 2)
    assert np.isnan(car[3]) and np.isnan(car[4])


def test_a_move_that_holds_retains_all_of_itself():
    frame = bars([0.0, 0.05] + [0.0] * 10)
    assert abs(ps.retention(frame, 6).iloc[1] - 1.0) < 1e-12


def test_a_move_that_is_fully_given_back_retains_none_of_it():
    frame = bars([0.0, 0.05, -0.05] + [0.0] * 10)
    assert abs(ps.retention(frame, 6).iloc[1]) < 1e-12


def test_a_move_that_keeps_going_retains_more_than_all_of_it():
    frame = bars([0.0, 0.05, 0.05] + [0.0] * 10)
    assert abs(ps.retention(frame, 6).iloc[1] - 2.0) < 1e-12


def test_an_overshoot_back_past_the_start_is_negative():
    frame = bars([0.0, 0.05, -0.08] + [0.0] * 10)
    assert ps.retention(frame, 6).iloc[1] < 0


def test_the_ratio_is_undefined_where_the_move_is_too_small_to_divide_by():
    # A bar can clear its return level on a small abnormal return when the peer
    # spread that hour was tiny; dividing by it reports a retention of forty
    # rather than a reversal.
    frame = bars([0.0, 0.0001, 0.004] + [0.0] * 10, sigma=0.01)
    assert pd.isna(ps.retention(frame, 6).iloc[1])


def test_the_sign_of_the_move_does_not_change_the_reading():
    up = bars([0.0, 0.05, -0.025] + [0.0] * 10)
    down = bars([0.0, -0.05, 0.025] + [0.0] * 10)
    assert abs(ps.retention(up, 6).iloc[1] - ps.retention(down, 6).iloc[1]) < 1e-12


def test_annotate_records_the_abnormal_move_and_the_raw_one_separately():
    # They disagree exactly when the market moves the same way afterwards. The
    # abnormal one tests what the detector claimed; the raw one is what a
    # person sees on a chart.
    frame = bars([0.0, 0.05] + [0.0] * 10,
                 raw=[0.0, 0.05, 0.05] + [0.0] * 9)
    out = ps.annotate(frame)
    assert abs(out["retention_6"].iloc[1] - 1.0) < 1e-12
    assert abs(out["retention_raw_6"].iloc[1] - 2.0) < 1e-12
    assert set(ps.RETENTION_COLUMNS) <= set(out.columns)


def test_held_is_null_where_the_horizon_has_not_elapsed():
    # An event that has not taken the test has not failed it. A caller treating
    # the two alike would drop exactly the newest events.
    events = pd.DataFrame({"retention_24": [0.9, 0.1, np.nan]})
    assert list(ps.held(events)[:2]) == [True, False]
    assert pd.isna(ps.held(events).iloc[2])


def test_held_is_null_when_retention_was_never_computed():
    assert ps.held(pd.DataFrame({"hour_utc": [1, 2]})).isna().all()


def test_attach_joins_on_the_asset_and_hour_not_on_row_order():
    # The events table is built from several assets' frames and has been
    # through a groupby since, so its row order is not any single asset's.
    scored = {
        "a": bars([0.0, 0.05] + [0.0] * 30),
        "b": bars([0.0, 0.05, -0.05] + [0.0] * 29),
    }
    events = pd.DataFrame({"asset_id": ["b", "a"], "hour_utc": [2 * HOUR] * 2})
    out = ps.attach(events, scored)

    assert abs(out["retention_6"].iloc[0]) < 1e-12       # b gave it back
    assert abs(out["retention_6"].iloc[1] - 1.0) < 1e-12  # a held


def test_attach_keeps_the_schema_on_an_empty_table():
    out = ps.attach(pd.DataFrame({"asset_id": [], "hour_utc": []}), {})
    assert out.empty and set(ps.RETENTION_COLUMNS) <= set(out.columns)


def test_held_reads_the_series_that_matches_what_the_event_claimed():
    # An event found because the market did not explain the move is tested on
    # the abnormal move; one found because the move was simply large is tested
    # on the price. Using the abnormal series for both would reject a genuine
    # market-wide move the instant the market came back with it - which, on a
    # macro day, is most of them.
    events = pd.DataFrame({
        "basis": pd.array(["abnormal", "absolute"], dtype="string"),
        "retention_24": [0.9, 0.1],       # the residual gave it back
        "retention_raw_24": [0.1, 0.9],   # the price did not
    })
    assert list(ps.held(events)) == [True, True]


def test_an_event_on_both_bases_is_tested_on_the_abnormal_one():
    # "both" means the move was large AND unexplained; the stricter reading of
    # whether it held is the one the detector's own claim rests on.
    events = pd.DataFrame({
        "basis": pd.array(["both"], dtype="string"),
        "retention_24": [0.1], "retention_raw_24": [0.9],
    })
    assert list(ps.held(events)) == [False]


def test_held_without_a_basis_column_uses_the_abnormal_series():
    events = pd.DataFrame({"retention_24": [0.9, 0.1]})
    assert list(ps.held(events)) == [True, False]

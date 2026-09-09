from datetime import datetime, timezone

import numpy as np
import pandas as pd

from tremor import persistence as ps

HOUR = 3600


def bars(abnormal, sigma=0.001, raw=None):
    n = len(abnormal)
    return pd.DataFrame({
        "hour_utc": [(i + 1) * HOUR for i in range(n)],
        "e_resid": abnormal,
        "r": raw if raw is not None else abnormal,
        "sigma_lt_resid": [sigma] * n,
    })


def test_a_move_that_holds_retains_all_of_itself():
    frame = bars([0.0, 0.05] + [0.0] * 10)
    assert abs(ps.retention(frame, ps.TODAY).iloc[1] - 1.0) < 1e-12


def test_a_move_that_is_fully_given_back_retains_none_of_it():
    frame = bars([0.0, 0.05, -0.05] + [0.0] * 10)
    assert abs(ps.retention(frame, ps.TODAY).iloc[1]) < 1e-12


def test_a_move_that_keeps_going_retains_more_than_all_of_it():
    frame = bars([0.0, 0.05, 0.05] + [0.0] * 10)
    assert abs(ps.retention(frame, ps.TODAY).iloc[1] - 2.0) < 1e-12


def test_an_overshoot_back_past_the_start_is_negative():
    frame = bars([0.0, 0.05, -0.08] + [0.0] * 10)
    assert ps.retention(frame, ps.TODAY).iloc[1] < 0


def test_the_ratio_is_undefined_where_the_move_is_too_small_to_divide_by():
    # A bar can clear its return level on a small abnormal return when the peer
    # spread that hour was tiny; dividing by it reports a retention of forty
    # rather than a reversal.
    frame = bars([0.0, 0.0001, 0.004] + [0.0] * 10, sigma=0.01)
    assert pd.isna(ps.retention(frame, ps.TODAY).iloc[1])


def test_the_sign_of_the_move_does_not_change_the_reading():
    up = bars([0.0, 0.05, -0.025] + [0.0] * 10)
    down = bars([0.0, -0.05, 0.025] + [0.0] * 10)
    assert abs(ps.retention(up, ps.TODAY).iloc[1] - ps.retention(down, ps.TODAY).iloc[1]) < 1e-12


def test_annotate_records_the_abnormal_move_and_the_raw_one_separately():
    # They disagree exactly when the market moves the same way afterwards. The
    # abnormal one tests what the detector claimed; the raw one is what a
    # person sees on a chart.
    frame = bars([0.0, 0.05] + [0.0] * 10,
                 raw=[0.0, 0.05, 0.05] + [0.0] * 9)
    out = ps.annotate(frame)
    assert abs(out["retention_today"].iloc[1] - 1.0) < 1e-12
    assert abs(out["retention_raw_today"].iloc[1] - 2.0) < 1e-12
    assert set(ps.RETENTION_COLUMNS) <= set(out.columns)


def test_held_is_null_where_the_horizon_has_not_elapsed():
    # An event that has not taken the test has not failed it. A caller treating
    # the two alike would drop exactly the newest events.
    events = pd.DataFrame({"retention_settled": [0.9, 0.1, np.nan]})
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

    assert abs(out["retention_today"].iloc[0]) < 1e-12       # b gave it back
    assert abs(out["retention_today"].iloc[1] - 1.0) < 1e-12  # a held


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
        "retention_settled": [0.9, 0.1],       # the residual gave it back
        "retention_raw_settled": [0.1, 0.9],   # the price did not
    })
    assert list(ps.held(events)) == [True, True]


def test_an_event_on_both_bases_is_tested_on_the_abnormal_one():
    # "both" means the move was large AND unexplained; the stricter reading of
    # whether it held is the one the detector's own claim rests on.
    events = pd.DataFrame({
        "basis": pd.array(["both"], dtype="string"),
        "retention_settled": [0.1], "retention_raw_settled": [0.9],
    })
    assert list(ps.held(events)) == [False]


def test_held_without_a_basis_column_uses_the_abnormal_series():
    events = pd.DataFrame({"retention_settled": [0.9, 0.1]})
    assert list(ps.held(events)) == [True, False]


# --- the settled horizon lands at the next trading close --------------------

def _session_frame():
    """Two 4-hour equity days, then a third, on the round hour."""
    hours = []
    for day in (1, 2, 3):
        hours += [int(datetime(2026, 6, day, h, tzinfo=timezone.utc).timestamp())
                  for h in (14, 15, 16, 17)]
    return pd.DataFrame({"hour_utc": hours,
                         "e_resid": [0.01] * len(hours),
                         "r": [0.01] * len(hours),
                         "sigma_lt_resid": [0.001] * len(hours)})


def test_the_settled_check_lands_on_the_last_bar_of_the_next_day():
    # Not twenty-four bars. In a four-bar day that would be six days away; the
    # close of the next day is the point a person would actually look.
    frame = _session_frame()
    offsets = ps.next_close_offsets(frame)
    # First bar of day 1 (index 0) -> last bar of day 2 (index 7).
    assert offsets[0] == 7
    # Last bar of day 1 (index 3) -> still the last bar of day 2.
    assert offsets[3] == 4
    # Day 3 is the final day: no next day, so no answer.
    assert (offsets[8:] == -1).all()


def test_the_distance_depends_on_the_hour_the_move_happened():
    # Which is the point: an early move waits longer for the same close than a
    # late one, and that dependency is the honest one rather than a hidden one.
    offsets = ps.next_close_offsets(_session_frame())
    assert offsets[0] > offsets[1] > offsets[2] > offsets[3]


def test_a_weekend_or_holiday_is_simply_not_a_day():
    # The offset is read off the bars present, so a gap needs no special case.
    hours = [int(datetime(2026, 6, d, h, tzinfo=timezone.utc).timestamp())
             for d, h in [(5, 14), (5, 15), (8, 14), (8, 15)]]   # Friday, Monday
    frame = pd.DataFrame({"hour_utc": hours, "e_resid": [0.01] * 4,
                          "r": [0.01] * 4, "sigma_lt_resid": [0.001] * 4})
    offsets = ps.next_close_offsets(frame)
    assert offsets[0] == 3        # Friday's first bar -> Monday's last
    assert (offsets[2:] == -1).all()


def test_the_exchange_day_is_used_when_a_timezone_is_given():
    # 23:00 UTC is the evening in New York, not the next day, so two bars either
    # side of midnight UTC belong to ONE session and must not be split.
    hours = [int(datetime(2026, 6, 1, 23, tzinfo=timezone.utc).timestamp()),
             int(datetime(2026, 6, 2, 0, tzinfo=timezone.utc).timestamp()),
             int(datetime(2026, 6, 2, 23, tzinfo=timezone.utc).timestamp())]
    frame = pd.DataFrame({"hour_utc": hours, "e_resid": [0.01] * 3,
                          "r": [0.01] * 3, "sigma_lt_resid": [0.001] * 3})
    utc_days = ps.next_close_offsets(frame)
    ny_days = ps.next_close_offsets(frame, "America/New_York")
    # Bar 1 is the tell. By the clock it has already crossed into the last day
    # of the frame, so there is no next close for it and the answer is -1. In
    # New York it is still the evening of day one, so its next close is bar 2.
    assert utc_days[1] == -1
    assert ny_days[1] == 1


def test_the_day_close_reading_runs_to_the_last_bar_of_that_day():
    # Not a bar count: how the move stood when THIS day closed. Six bars is most
    # of a session in an ETF and a quarter of a day in crypto, and neither is a
    # moment a reader can picture.
    frame = bars([0.0, 0.05, -0.05, 0.05] + [0.0] * 8)
    offsets = ps.today_close_offsets(frame)
    assert offsets[-1] == 0                       # the closing bar itself
    assert list(offsets[:3]) == [len(frame) - 1, len(frame) - 2, len(frame) - 3]


def test_a_move_in_the_closing_hour_has_no_day_left_to_hold_through():
    # Zero is not a failure - there is nothing left of that day, and the message
    # says so rather than reporting a ratio of one and calling it news.
    frame = bars([0.0, 0.05] + [0.0] * 10)
    assert ps.today_close_offsets(frame)[-1] == 0


def test_the_two_horizons_are_the_two_day_closes():
    assert ps.HORIZONS == (ps.TODAY, ps.SETTLED)
    assert "retention_today" in ps.RETENTION_COLUMNS
    assert "retention_settled" in ps.RETENTION_COLUMNS


# --- the day boundaries, which used to be the whole cost of the pipeline -----

def _frame(hours):
    return pd.DataFrame({"hour_utc": hours})


def test_the_offsets_agree_with_the_obvious_slow_answer():
    # The fast path is a grouped maximum; this is the definition it replaced,
    # written out. Checked on a SHUFFLED frame as well, because an offset is a
    # distance in rows and nothing in the pipeline promises the rows are sorted.
    def reference(frame):
        days = pd.to_datetime(frame["hour_utc"], unit="s", utc=True).dt.date.to_numpy()
        positions = np.arange(len(days))
        return np.array([positions[days == day].max() - i
                         for i, day in enumerate(days)])

    day = int(datetime(2026, 3, 2, tzinfo=timezone.utc).timestamp())
    hours = [day + h * HOUR for h in range(6)] + [day + 86400 + h * HOUR for h in range(4)]
    assert list(ps.today_close_offsets(_frame(hours))) == [5, 4, 3, 2, 1, 0, 3, 2, 1, 0]

    for order in ([3, 0, 7, 1, 9, 2, 4, 8, 5, 6], list(reversed(range(10)))):
        frame = _frame([hours[i] for i in order])
        assert list(ps.today_close_offsets(frame)) == list(reference(frame))


def test_the_offsets_survive_an_empty_frame():
    assert len(ps.today_close_offsets(_frame([]))) == 0
    assert len(ps.next_close_offsets(_frame([]))) == 0


def test_the_day_boundaries_are_linear_in_the_number_of_bars():
    # This was O(bars x days): both functions looped over the days and compared
    # the whole day column against each one. At 145,000 bars and 6,000 days that
    # is 870 million comparisons per call, four calls per instrument, and it was
    # 96% of the cost of the hourly run - eighteen seconds a call. The bound
    # here is loose on purpose; the point is that it cannot be quadratic again.
    import time

    start = int(datetime(2015, 1, 1, tzinfo=timezone.utc).timestamp())
    hours = [start + d * 86400 + h * HOUR for d in range(2000) for h in range(24)]
    frame = _frame(hours)

    began = time.perf_counter()
    ps.today_close_offsets(frame)
    ps.next_close_offsets(frame)
    elapsed = time.perf_counter() - began

    assert elapsed < 5.0, f"{len(hours)} bars over 2000 days took {elapsed:.1f}s"

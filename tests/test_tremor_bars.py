import pandas as pd
import pytest

from tremor import bars
from price_monitor.models import Candle

HOUR = 3600


def frame(rows):
    """rows: (hour_utc, open, high, low, close, volume, n_src)"""
    return pd.DataFrame(rows, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)


def test_to_hourly_folds_half_hour_bars_onto_the_round_hour():
    # The ETF grid runs on the :30, so the two half-hourly bars 10:30 and 11:00
    # belong to DIFFERENT hours, while 11:00 and 11:30 belong to the same one.
    src = frame([
        (11 * HOUR + 1800, 1.0, 4.0, 0.5, 2.0, 10.0, 1),
        (12 * HOUR, 2.0, 3.0, 1.5, 2.5, 20.0, 1),
        (12 * HOUR + 1800, 2.5, 9.0, 2.0, 7.0, 30.0, 1),
    ])
    out = bars.to_hourly(src)

    assert list(out["hour_utc"]) == [11 * HOUR, 12 * HOUR]
    full = out.iloc[1]
    assert full["open"] == 2.0 and full["close"] == 7.0
    assert full["high"] == 9.0 and full["low"] == 1.5
    assert full["volume"] == 50.0
    assert full["n_src"] == 2
    # An hour assembled from a single half-hourly bar is the first bar of the
    # session (§2.4). It can only be told apart by n_src.
    assert out.iloc[0]["n_src"] == 1


def test_to_hourly_is_identity_for_series_already_on_the_hour():
    src = frame([(HOUR, 1.0, 2.0, 0.5, 1.5, 7.0, 1),
                 (2 * HOUR, 1.5, 2.5, 1.0, 2.0, 8.0, 1)])
    out = bars.to_hourly(src)
    pd.testing.assert_frame_equal(out, src)


def test_to_hourly_orders_by_time_not_by_input_order():
    # Sources serve bars newest first; open and close must be taken by time, not
    # by row order in the response.
    src = frame([(HOUR + 1800, 5.0, 6.0, 4.0, 5.5, 1.0, 1),
                 (HOUR, 1.0, 2.0, 0.5, 1.5, 1.0, 1)])
    out = bars.to_hourly(src)
    assert out.iloc[0]["open"] == 1.0
    assert out.iloc[0]["close"] == 5.5


def test_to_hourly_on_empty_input_returns_typed_empty_frame():
    out = bars.to_hourly(bars.empty_frame())
    assert out.empty
    assert list(out.columns) == list(bars.SCHEMA)


def test_merge_is_idempotent(tmp_path):
    path = bars.store_path(str(tmp_path), "x")
    data = frame([(HOUR, 1.0, 2.0, 0.5, 1.5, 7.0, 1)])

    assert bars.merge(path, data) == 1
    assert bars.merge(path, data) == 0
    assert len(bars.load(path)) == 1


def test_merge_lets_a_revised_bar_win(tmp_path):
    # The vendor may revise a bar - the fresher version is more trustworthy, but
    # it does not count as a new row.
    path = bars.store_path(str(tmp_path), "x")
    bars.merge(path, frame([(HOUR, 1.0, 2.0, 0.5, 1.5, 7.0, 1)]))
    added = bars.merge(path, frame([(HOUR, 1.0, 2.0, 0.5, 9.9, 7.0, 1)]))

    assert added == 0
    assert bars.load(path).iloc[0]["close"] == 9.9


def test_merge_deduplicates_a_repeated_block(tmp_path):
    # Exactly the situation found in the accumulated NDJSON history: a bad branch
    # merge duplicated a continuous block of hours.
    path = bars.store_path(str(tmp_path), "x")
    block = [(h * HOUR, 1.0, 2.0, 0.5, 1.5, 7.0, 1) for h in range(1, 6)]
    assert bars.merge(path, frame(block + block)) == 5
    assert list(bars.load(path)["hour_utc"]) == [h * HOUR for h in range(1, 6)]


def test_merge_keeps_the_store_sorted(tmp_path):
    path = bars.store_path(str(tmp_path), "x")
    bars.merge(path, frame([(3 * HOUR, 1.0, 1.0, 1.0, 1.0, 0.0, 1)]))
    bars.merge(path, frame([(HOUR, 1.0, 1.0, 1.0, 1.0, 0.0, 1)]))
    assert list(bars.load(path)["hour_utc"]) == [HOUR, 3 * HOUR]


def test_load_missing_file_returns_typed_empty_frame(tmp_path):
    out = bars.load(bars.store_path(str(tmp_path), "no-such-thing"))
    assert out.empty
    assert out["hour_utc"].dtype == "int64"


def test_candles_to_frame_preserves_the_open_time_convention():
    # hour_utc is the bar's OPENING moment (§1.2), the same convention as
    # open_time in candle_store, so the import needs no shift.
    candles = [Candle(open_time=HOUR, open=1.0, high=2.0, low=0.5, close=1.5,
                      volume=3.0, close_time=2 * HOUR)]
    out = bars.candles_to_frame(candles)
    assert out.iloc[0]["hour_utc"] == HOUR
    assert out.iloc[0]["close"] == 1.5


def test_candles_to_frame_on_empty_list():
    assert bars.candles_to_frame([]).empty


def test_to_hourly_does_not_double_volume_on_a_repeated_bar():
    # The same hour arriving twice is one bar, not two. Without dropping
    # duplicates before aggregation the volume would be summed and doubled:
    # exactly what would have happened on the history where a branch merge
    # duplicated a block of hours.
    src = frame([(HOUR, 1.0, 2.0, 0.5, 1.5, 453.89, 1),
                 (HOUR, 1.0, 2.0, 0.5, 1.5, 453.89, 1)])
    out = bars.to_hourly(src)

    assert len(out) == 1
    assert out.iloc[0]["volume"] == 453.89


def test_to_hourly_prefers_the_later_copy_of_a_repeated_bar():
    # The earlier copy may have caught the hour still open - narrower in both
    # volume and range. The later one is either equivalent or more complete.
    src = frame([(HOUR, 78175.92, 78179.52, 78091.18, 78118.19, 36.77, 1),
                 (HOUR, 78175.92, 78179.52, 77969.24, 78076.91, 174.29, 1)])
    out = bars.to_hourly(src)

    assert len(out) == 1
    assert out.iloc[0]["close"] == 78076.91
    assert out.iloc[0]["volume"] == 174.29
    assert out.iloc[0]["low"] == 77969.24

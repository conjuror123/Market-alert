import os
from datetime import datetime, timezone

from price_monitor.candle_store import (
    append_candles,
    deduplicate,
    daily_closes,
    load_candles,
    merge_history,
    safe_asset_filename,
    store_path,
)
from price_monitor.models import Candle


def candle(open_time, close=1.0, high=None, low=None, volume=0.0):
    return Candle(
        open_time=open_time, open=close, high=high if high is not None else close,
        low=low if low is not None else close, close=close, volume=volume,
        close_time=open_time + 3600,
    )


def test_safe_asset_filename_sanitizes_special_characters():
    assert safe_asset_filename("twelvedata", "EUR/USD") == "twelvedata_EUR_USD"
    assert safe_asset_filename("yahoo", "DX-Y.NYB") == "yahoo_DX-Y.NYB"


def test_store_path_uses_safe_filename(tmp_path):
    path = store_path(str(tmp_path), "coinbase", "BTC-USD")
    assert path == os.path.join(str(tmp_path), "coinbase_BTC-USD.ndjson")


def test_append_then_load_roundtrip(tmp_path):
    path = os.path.join(tmp_path, "asset.ndjson")
    candles = [candle(0, close=100.0), candle(3600, close=101.0)]
    append_candles(path, candles)
    assert load_candles(path) == candles


def test_append_is_pure_append_across_calls(tmp_path):
    path = os.path.join(tmp_path, "asset.ndjson")
    append_candles(path, [candle(0, close=100.0)])
    append_candles(path, [candle(3600, close=101.0)])
    loaded = load_candles(path)
    assert [c.open_time for c in loaded] == [0, 3600]


def test_load_missing_file_returns_empty_list(tmp_path):
    assert load_candles(os.path.join(tmp_path, "missing.ndjson")) == []


def test_merge_history_deduplicates_overlapping_fetches(tmp_path):
    path = os.path.join(tmp_path, "asset.ndjson")
    added_first = merge_history(path, [candle(0), candle(3600), candle(7200)])
    added_second = merge_history(path, [candle(3600), candle(7200), candle(10800)])
    assert added_first == 3
    assert added_second == 1
    loaded = load_candles(path)
    assert [c.open_time for c in loaded] == [0, 3600, 7200, 10800]


def test_merge_history_returns_zero_when_nothing_new(tmp_path):
    path = os.path.join(tmp_path, "asset.ndjson")
    merge_history(path, [candle(0)])
    assert merge_history(path, [candle(0)]) == 0


def test_daily_closes_groups_by_utc_calendar_day():
    day1 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    hour = 3600
    candles = [
        candle(day1, close=10.0, high=11.0, low=9.0, volume=5.0),
        candle(day1 + 23 * hour, close=12.0, high=13.0, low=8.0, volume=7.0),
        candle(day1 + 24 * hour, close=20.0, high=21.0, low=19.0, volume=2.0),
    ]
    now = datetime(2026, 1, 3, tzinfo=timezone.utc)
    result = daily_closes(candles, now=now)

    assert len(result) == 2
    assert result[0].close == 12.0  # last close of day 1
    assert result[0].high == 13.0
    assert result[0].low == 8.0
    assert result[0].volume == 12.0  # summed
    assert result[1].close == 20.0  # day 2's only candle


def test_daily_closes_drops_the_still_forming_day():
    today_start = int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp())
    candles = [candle(today_start, close=5.0)]
    now = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)  # same UTC day, not yet over
    assert daily_closes(candles, now=now) == []


def test_daily_closes_keeps_a_day_once_now_moves_past_it():
    day_start = int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp())
    candles = [candle(day_start, close=5.0)]
    now = datetime(2026, 1, 3, 0, 1, tzinfo=timezone.utc)
    result = daily_closes(candles, now=now)
    assert len(result) == 1
    assert result[0].close == 5.0


def test_deduplicate_removes_a_repeated_block(tmp_path):
    # Как выглядит след неудачного слияния: два одинаковых блока подряд.
    path = os.path.join(str(tmp_path), "x.ndjson")
    block = [candle(1000 + i * 3600) for i in range(5)]
    append_candles(path, block)
    append_candles(path, block)

    assert deduplicate(path) == 5
    stored = load_candles(path)
    assert [c.open_time for c in stored] == [1000 + i * 3600 for i in range(5)]


def test_deduplicate_keeps_the_later_version_of_a_conflicting_hour(tmp_path):
    # Ранняя копия застала час незакрытым: меньше объём, уже диапазон.
    path = os.path.join(str(tmp_path), "x.ndjson")
    partial = Candle(open_time=1000, open=10.0, high=11.0, low=9.9,
                     close=10.5, volume=36.7, close_time=4600)
    complete = Candle(open_time=1000, open=10.0, high=11.0, low=9.0,
                      close=10.1, volume=174.2, close_time=4600)
    append_candles(path, [partial])
    append_candles(path, [complete])

    assert deduplicate(path) == 1
    stored = load_candles(path)
    assert len(stored) == 1
    assert stored[0].volume == 174.2
    assert stored[0].low == 9.0


def test_deduplicate_leaves_a_clean_file_untouched(tmp_path):
    path = os.path.join(str(tmp_path), "x.ndjson")
    append_candles(path, [candle(1000), candle(4600)])
    before = open(path, encoding="utf-8").read()

    assert deduplicate(path) == 0
    assert open(path, encoding="utf-8").read() == before


def test_deduplicate_sorts_by_time(tmp_path):
    path = os.path.join(str(tmp_path), "x.ndjson")
    append_candles(path, [candle(8200)])
    append_candles(path, [candle(1000), candle(8200)])

    deduplicate(path)
    assert [c.open_time for c in load_candles(path)] == [1000, 8200]


def test_deduplicate_on_a_missing_file(tmp_path):
    assert deduplicate(os.path.join(str(tmp_path), "нет.ndjson")) == 0

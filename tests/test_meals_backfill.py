import os

import pandas as pd

from meals import bars
from meals.backfill import import_legacy
from meals.basket import Asset
from price_monitor import candle_store
from price_monitor.models import Candle

HOUR = 3600


def asset(**over):
    base = dict(ticker="EUR/USD", source="twelvedata", tier=1, block="FX",
                has_volume=False, tick_size=0.00001, session_template="fx_continuous",
                fetch_interval="1h", label="Евро / доллар", in_basket=True)
    return Asset(**(base | over))


def candle(hour, close=1.5):
    return Candle(open_time=hour * HOUR, open=1.0, high=2.0, low=0.5,
                  close=close, volume=0.0, close_time=(hour + 1) * HOUR)


def test_imports_the_accumulated_ndjson_history(tmp_path):
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    a = asset()
    candle_store.append_candles(
        candle_store.store_path(str(legacy_dir), a.source, a.ticker),
        [candle(1), candle(2), candle(3)])

    path = bars.store_path(str(tmp_path / "bars"), a.file_stem)
    assert import_legacy(a, path, str(legacy_dir)) == 3
    assert list(bars.load(path)["hour_utc"]) == [HOUR, 2 * HOUR, 3 * HOUR]


def test_import_deduplicates_the_repeated_block(tmp_path):
    # Накопленная история содержит блок из 299 часов, продублированный
    # неудачным слиянием веток. Хранилище обязано принять её один раз.
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    a = asset()
    block = [candle(h) for h in range(1, 6)]
    candle_store.append_candles(
        candle_store.store_path(str(legacy_dir), a.source, a.ticker), block + block)

    path = bars.store_path(str(tmp_path / "bars"), a.file_stem)
    assert import_legacy(a, path, str(legacy_dir)) == 5
    assert len(bars.load(path)) == 5


def test_import_is_idempotent(tmp_path):
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    a = asset()
    candle_store.append_candles(
        candle_store.store_path(str(legacy_dir), a.source, a.ticker), [candle(1)])

    path = bars.store_path(str(tmp_path / "bars"), a.file_stem)
    import_legacy(a, path, str(legacy_dir))
    assert import_legacy(a, path, str(legacy_dir)) == 0


def test_import_without_legacy_file_is_a_no_op(tmp_path):
    path = bars.store_path(str(tmp_path / "bars"), "x")
    assert import_legacy(asset(), path, str(tmp_path / "нет")) == 0
    assert not os.path.exists(path)

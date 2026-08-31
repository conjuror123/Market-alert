import pandas as pd
import pytest

from meals import bars
from price_monitor.models import Candle

HOUR = 3600


def frame(rows):
    """rows: (hour_utc, open, high, low, close, volume, n_src)"""
    return pd.DataFrame(rows, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)


def test_to_hourly_folds_half_hour_bars_onto_the_round_hour():
    # Сетка биржевых фондов идёт по :30, поэтому два получасовых бара
    # 10:30 и 11:00 относятся к РАЗНЫМ часам, а 11:00 и 11:30 - к одному.
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
    # Час, собранный из одного получасового бара - это первый бар сессии
    # (п.2.4). Отличить его можно только по n_src.
    assert out.iloc[0]["n_src"] == 1


def test_to_hourly_is_identity_for_series_already_on_the_hour():
    src = frame([(HOUR, 1.0, 2.0, 0.5, 1.5, 7.0, 1),
                 (2 * HOUR, 1.5, 2.5, 1.0, 2.0, 8.0, 1)])
    out = bars.to_hourly(src)
    pd.testing.assert_frame_equal(out, src)


def test_to_hourly_orders_by_time_not_by_input_order():
    # Источники отдают бары от новых к старым; open и close обязаны браться по
    # времени, а не по порядку строк в ответе.
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
    # Вендор может пересмотреть бар - свежая версия достовернее, но новой
    # строкой она не считается.
    path = bars.store_path(str(tmp_path), "x")
    bars.merge(path, frame([(HOUR, 1.0, 2.0, 0.5, 1.5, 7.0, 1)]))
    added = bars.merge(path, frame([(HOUR, 1.0, 2.0, 0.5, 9.9, 7.0, 1)]))

    assert added == 0
    assert bars.load(path).iloc[0]["close"] == 9.9


def test_merge_deduplicates_a_repeated_block(tmp_path):
    # Ровно та ситуация, что нашлась в накопленной NDJSON-истории: неудачное
    # слияние веток продублировало непрерывный блок часов.
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
    out = bars.load(bars.store_path(str(tmp_path), "нет-такого"))
    assert out.empty
    assert out["hour_utc"].dtype == "int64"


def test_candles_to_frame_preserves_the_open_time_convention():
    # hour_utc - момент ОТКРЫТИЯ бара (п.1.2), та же конвенция, что у
    # open_time в candle_store, поэтому импорт идёт без сдвига.
    candles = [Candle(open_time=HOUR, open=1.0, high=2.0, low=0.5, close=1.5,
                      volume=3.0, close_time=2 * HOUR)]
    out = bars.candles_to_frame(candles)
    assert out.iloc[0]["hour_utc"] == HOUR
    assert out.iloc[0]["close"] == 1.5


def test_candles_to_frame_on_empty_list():
    assert bars.candles_to_frame([]).empty


def test_to_hourly_does_not_double_volume_on_a_repeated_bar():
    # Тот же час, пришедший дважды - это один бар, а не два. Без отбрасывания
    # дублей до агрегации объём сложился бы суммой и удвоился: ровно это
    # произошло бы на истории, где слияние веток продублировало блок часов.
    src = frame([(HOUR, 1.0, 2.0, 0.5, 1.5, 453.89, 1),
                 (HOUR, 1.0, 2.0, 0.5, 1.5, 453.89, 1)])
    out = bars.to_hourly(src)

    assert len(out) == 1
    assert out.iloc[0]["volume"] == 453.89


def test_to_hourly_prefers_the_later_copy_of_a_repeated_bar():
    # Ранняя копия могла застать час незакрытым - у неё уже и объём, и
    # диапазон. Поздняя либо равнозначна, либо полнее.
    src = frame([(HOUR, 78175.92, 78179.52, 78091.18, 78118.19, 36.77, 1),
                 (HOUR, 78175.92, 78179.52, 77969.24, 78076.91, 174.29, 1)])
    out = bars.to_hourly(src)

    assert len(out) == 1
    assert out.iloc[0]["close"] == 78076.91
    assert out.iloc[0]["volume"] == 174.29
    assert out.iloc[0]["low"] == 77969.24

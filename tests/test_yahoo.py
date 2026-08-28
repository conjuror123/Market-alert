import pytest

from price_monitor.models import ExchangeError
from price_monitor.yahoo import _parse_chart_response


def make_raw(timestamps, opens, highs, lows, closes, volumes):
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {"open": opens, "high": highs, "low": lows,
                             "close": closes, "volume": volumes}
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def test_parses_candles_in_chronological_order():
    raw = make_raw(
        timestamps=[300, 100, 200],
        opens=[3.0, 1.0, 2.0],
        highs=[3.5, 1.5, 2.5],
        lows=[2.5, 0.5, 1.5],
        closes=[3.2, 1.2, 2.2],
        volumes=[30, 10, 20],
    )
    candles = _parse_chart_response(raw, granularity=3600, limit=10)
    assert [c.open_time for c in candles] == [100, 200, 300]
    assert candles[0].close == 1.2
    assert candles[0].close_time == 100 + 3600


def test_skips_rows_with_null_ohlc_gaps():
    raw = make_raw(
        timestamps=[100, 200, 300],
        opens=[1.0, None, 3.0],
        highs=[1.5, None, 3.5],
        lows=[0.5, None, 2.5],
        closes=[1.2, None, 3.2],
        volumes=[10, None, 30],
    )
    candles = _parse_chart_response(raw, granularity=3600, limit=10)
    assert [c.open_time for c in candles] == [100, 300]


def test_null_volume_becomes_zero_not_dropped():
    raw = make_raw(
        timestamps=[100],
        opens=[1.0], highs=[1.5], lows=[0.5], closes=[1.2],
        volumes=[None],
    )
    candles = _parse_chart_response(raw, granularity=3600, limit=10)
    assert len(candles) == 1
    assert candles[0].volume == 0.0


def test_limit_keeps_most_recent():
    raw = make_raw(
        timestamps=[100, 200, 300],
        opens=[1.0, 2.0, 3.0], highs=[1.0, 2.0, 3.0],
        lows=[1.0, 2.0, 3.0], closes=[1.0, 2.0, 3.0],
        volumes=[10, 20, 30],
    )
    candles = _parse_chart_response(raw, granularity=3600, limit=2)
    assert [c.open_time for c in candles] == [200, 300]


def test_malformed_response_raises_exchange_error():
    with pytest.raises(ExchangeError):
        _parse_chart_response({"chart": {"result": None, "error": {"description": "Not Found"}}},
                               granularity=3600, limit=10)

import pytest

from price_monitor import bitstamp
from price_monitor.models import ExchangeError


def _row(t, close, volume):
    return {"timestamp": str(t), "open": "1.0", "high": "1.1", "low": "0.9",
            "close": str(close), "volume": str(volume)}


def test_an_hour_without_a_trade_is_not_a_price():
    # Bitstamp serves an untraded hour at the last price with zero volume:
    # stored, it would be a run of zero returns that never happened.
    payload = {"data": {"pair": "XRP/USD", "ohlc": [
        _row(7200, 0.52, "10.5"), _row(3600, 0.51, "0.00000000"), _row(0, 0.50, "3")]}}
    out = bitstamp.parse(payload)
    assert [c.open_time for c in out] == [0, 7200]
    assert out[1].close == 0.52 and out[1].close_time == 10800


def test_an_answer_without_candles_raises():
    with pytest.raises(ExchangeError):
        bitstamp.parse({"errors": [{"message": "Invalid pair"}]})


def test_the_history_walk_steps_over_an_empty_stretch(monkeypatch):
    # Before a pair is listed the answer is empty; the walk moves on rather
    # than stopping there and returning nothing.
    asked = []

    def fake(session, pair, start):
        asked.append(start)
        if start < 5000 * 3600:
            return {"data": {"ohlc": []}}
        return {"data": {"ohlc": [_row(start, 1.0, "1"), _row(start + 3600, 1.0, "1")]}}

    from datetime import datetime, timezone
    monkeypatch.setattr(bitstamp, "_get", fake)
    out = bitstamp.fetch_history("xrpusd", datetime.fromtimestamp(0, timezone.utc),
                                 datetime.fromtimestamp(5003 * 3600, timezone.utc),
                                 request_delay_seconds=0)
    assert out and out[0].open_time == 5000 * 3600
    assert len(asked) < 10

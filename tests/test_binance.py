from datetime import datetime, timezone

from price_monitor import binance


def _k(t, close):
    return [t * 1000, "1.0", "1.2", "0.9", str(close), "123.5", t * 1000 + 3599999,
            "0", 10, "0", "0", "0"]


def test_ticker_names_the_binance_symbol():
    assert binance.symbol_for("ADA/USDT") == "ADAUSDT"


def test_klines_are_stamped_at_their_open_in_seconds():
    c = binance.parse([_k(3600, 1.1)])[0]
    assert (c.open_time, c.close, c.volume, c.close_time) == (3600, 1.1, 123.5, 7200)


def test_the_walk_pages_forward_until_a_short_page(monkeypatch):
    asked = []

    def fake(session, symbol, start):
        asked.append(start)
        if start >= 2000 * 3600:
            return [_k(start, 1.0)]
        return [_k(start + i * 3600, 1.0) for i in range(binance.LIMIT)]

    monkeypatch.setattr(binance, "_get", fake)
    out = binance.fetch_history("ADAUSDT", datetime.fromtimestamp(0, timezone.utc),
                                datetime.fromtimestamp(5000 * 3600, timezone.utc),
                                request_delay_seconds=0)
    assert asked == [0, 1000 * 3600, 2000 * 3600]
    assert len(out) == 2001 and out[-1].open_time == 2000 * 3600

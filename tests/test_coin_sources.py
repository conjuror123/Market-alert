"""The coins' second sources, Coinbase and Kraken: what an answer becomes."""
import pytest

from price_monitor import coinbase, kraken
from price_monitor.models import ExchangeError


def test_coinbase_rows_come_newest_first_and_an_untraded_hour_is_dropped():
    rows = [[7200, 9.0, 11.0, 10.0, 10.5, 3.0],      # [time, low, high, open, close, volume]
            [3600, 9.5, 10.5, 10.0, 10.0, 0.0],
            [0, 8.0, 10.0, 9.0, 10.0, 2.0]]
    out = coinbase.parse(rows)
    assert [c.open_time for c in out] == [0, 7200]
    assert (out[1].open, out[1].high, out[1].low, out[1].close) == (10.0, 11.0, 9.0, 10.5)


def test_coinbase_names_the_coin_against_the_dollar():
    assert coinbase.product_for("BTC/USDT") == "BTC-USD"
    assert coinbase.product_for("POL/USDT") == "POL-USD"


def test_coinbase_pages_by_three_hundred_hours(monkeypatch):
    from datetime import datetime, timezone

    asked = []
    monkeypatch.setattr(coinbase, "_get",
                        lambda session, product, start, end: asked.append((start, end)) or [])
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 31, tzinfo=timezone.utc)
    coinbase.fetch_history("BTC-USD", start, end, request_delay_seconds=0)
    assert asked[0][0] == int(start.timestamp())
    assert all(b - a == 299 * 3600 for a, b in asked[:-1])
    assert asked[-1][1] == int(end.timestamp())
    assert all(asked[k + 1][0] == asked[k][1] + 3600 for k in range(len(asked) - 1))


def test_kraken_names_bitcoin_and_dogecoin_its_own_way():
    assert kraken.pair_for("BTC/USDT") == "XBTUSD"
    assert kraken.pair_for("DOGE/USDT") == "XDGUSD"
    assert kraken.pair_for("ETH/USDT") == "ETHUSD"


def test_kraken_rows_and_an_untraded_hour():
    payload = {"error": [], "result": {
        "XXBTZUSD": [[0, "10", "11", "9", "10.5", "10.2", "3.0", 5],
                     [3600, "10.5", "10.5", "10.5", "10.5", "0", "0.0", 0]],
        "last": 3600}}
    out = kraken.parse(payload, "XBTUSD")
    assert [c.open_time for c in out] == [0]
    assert (out[0].open, out[0].close, out[0].volume) == (10.0, 10.5, 3.0)


def test_kraken_says_what_it_refused():
    with pytest.raises(ExchangeError, match="EQuery:Unknown asset pair"):
        kraken.parse({"error": ["EQuery:Unknown asset pair"]}, "NOPEUSD")

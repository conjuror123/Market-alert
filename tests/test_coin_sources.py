"""The coins' voters, Coinbase, Kraken and Alpaca: what an answer becomes."""
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


def test_kraken_names_bitcoin_its_own_way():
    assert kraken.pair_for("BTC/USDT") == "XBTUSD"
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


def test_alpacas_coin_bars_are_paged_and_asked_without_a_key(monkeypatch):
    # Rows of 2026-10-12's answer, the first page carrying a token for the next.
    from datetime import datetime, timezone

    from price_monitor import alpaca
    pages = [
        {"bars": {"BTC/USD": [
            {"c": 83858.1595, "h": 83892.895, "l": 83622.43, "n": 26, "o": 83675.3715,
             "t": "2026-10-12T00:00:00Z", "v": 0.031230259, "vw": 83765.624431},
            {"c": 83566.2755, "h": 83944.484, "l": 83354.125, "n": 27, "o": 83857.5025,
             "t": "2026-10-12T01:00:00Z", "v": 0.017849965, "vw": 83585.886878}]},
         "next_page_token": "QlRD"},
        {"bars": {"BTC/USD": [
            {"c": 83740.77, "h": 83917.8145, "l": 83522.4705, "n": 29, "o": 83577.203,
             "t": "2026-10-12T02:00:00Z", "v": 0.034733564, "vw": 83716.709874}]},
         "next_page_token": None}]
    asked = []

    def get(session, url, params, auth, symbol):
        asked.append((url, dict(params), auth))
        return pages[len(asked) - 1]

    monkeypatch.setattr(alpaca, "_get", get)
    out = alpaca.fetch_crypto("BTC/USD", datetime(2026, 10, 12, tzinfo=timezone.utc),
                              datetime(2026, 10, 12, 3, tzinfo=timezone.utc))
    assert [c.open_time for c in out] == [1791763200, 1791766800, 1791770400]
    assert (out[0].open, out[0].close, out[0].close_time) == (83675.3715, 83858.1595,
                                                              1791766800)
    assert asked[0][0].endswith("/v1beta3/crypto/us/bars") and asked[0][2] == {}
    assert asked[1][1]["page_token"] == "QlRD"

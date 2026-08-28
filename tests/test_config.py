import pytest

from price_monitor.config import _parse_assets


def test_parse_assets_fills_default_label():
    assets = _parse_assets([{"symbol": "BTC-USD", "source": "coinbase"}])
    assert assets[0].label == "BTC-USD"


def test_parse_assets_keeps_explicit_label():
    assets = _parse_assets([{"symbol": "GC=F", "source": "yahoo", "label": "Gold"}])
    assert assets[0].label == "Gold"


def test_parse_assets_rejects_unknown_source():
    with pytest.raises(ValueError):
        _parse_assets([{"symbol": "BTC-USD", "source": "binance"}])


def test_parse_assets_rejects_missing_keys():
    with pytest.raises(ValueError):
        _parse_assets([{"symbol": "BTC-USD"}])

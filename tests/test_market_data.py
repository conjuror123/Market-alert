from price_monitor import market_data
from price_monitor.config import AssetConfig, Config
from price_monitor.market_data import fetch_candles


def make_config(assets):
    return Config(assets=assets, twelvedata_api_key="secret-key")


def test_dispatches_to_coinbase_with_base_url(monkeypatch):
    captured = {}

    def fake_fetch_klines(**kwargs):
        captured.update(kwargs)
        return []

    _, extra_kwargs_fn = market_data.SOURCES["coinbase"]
    monkeypatch.setitem(market_data.SOURCES, "coinbase", (fake_fetch_klines, extra_kwargs_fn))
    asset = AssetConfig(symbol="BTC-USD", source="coinbase", label="Bitcoin")
    cfg = make_config([asset])

    fetch_candles(asset, cfg)

    assert captured["symbol"] == "BTC-USD"
    assert captured["base_url"] == cfg.coinbase_base_url
    assert "api_key" not in captured


def test_dispatches_to_twelvedata_with_api_key(monkeypatch):
    captured = {}

    def fake_fetch_klines(**kwargs):
        captured.update(kwargs)
        return []

    _, extra_kwargs_fn = market_data.SOURCES["twelvedata"]
    monkeypatch.setitem(market_data.SOURCES, "twelvedata", (fake_fetch_klines, extra_kwargs_fn))
    asset = AssetConfig(symbol="EUR/USD", source="twelvedata", label="EUR/USD")
    cfg = make_config([asset])

    fetch_candles(asset, cfg)

    assert captured["symbol"] == "EUR/USD"
    assert captured["base_url"] == cfg.twelvedata_base_url
    assert captured["api_key"] == "secret-key"


def test_unknown_source_raises():
    asset = AssetConfig(symbol="X", source="bogus", label="X")
    cfg = make_config([asset])
    try:
        fetch_candles(asset, cfg)
        assert False, "expected ExchangeError"
    except Exception as exc:
        assert "Unknown source" in str(exc)

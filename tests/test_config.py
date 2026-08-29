import pytest

from price_monitor.config import Config, _parse_assets


def test_parse_assets_fills_default_label():
    assets = _parse_assets([{"symbol": "BTC-USD", "source": "coinbase"}])
    assert assets[0].label == "BTC-USD"


def test_parse_assets_keeps_explicit_label():
    assets = _parse_assets([{"symbol": "GC=F", "source": "yahoo", "label": "Gold"}])
    assert assets[0].label == "Gold"


def test_parse_assets_news_query_defaults_to_label():
    assets = _parse_assets([{"symbol": "GC=F", "source": "yahoo", "label": "Золото (Gold futures)"}])
    assert assets[0].news_query == "Золото (Gold futures)"


def test_parse_assets_keeps_explicit_news_query():
    assets = _parse_assets([{
        "symbol": "GC=F", "source": "yahoo", "label": "Золото (Gold futures)", "news_query": "gold price",
    }])
    assert assets[0].news_query == "gold price"


def test_parse_assets_accepts_twelvedata_source():
    assets = _parse_assets([{"symbol": "EUR/USD", "source": "twelvedata", "label": "EUR/USD"}])
    assert assets[0].source == "twelvedata"


def test_parse_assets_rejects_unknown_source():
    with pytest.raises(ValueError):
        _parse_assets([{"symbol": "BTC-USD", "source": "binance"}])


def test_parse_assets_rejects_missing_keys():
    with pytest.raises(ValueError):
        _parse_assets([{"symbol": "BTC-USD"}])


def test_parse_assets_collects_known_overrides():
    assets = _parse_assets([{
        "symbol": "CNY=X", "source": "yahoo",
        "price_zscore_threshold": 4.0, "cooldown_minutes": 60,
    }])
    assert assets[0].overrides == {"price_zscore_threshold": 4.0, "cooldown_minutes": 60}


def test_parse_assets_ignores_non_override_keys():
    assets = _parse_assets([{"symbol": "BTC-USD", "source": "coinbase", "label": "Bitcoin"}])
    assert assets[0].overrides == {}


def test_parse_assets_rejects_invalid_override_type():
    with pytest.raises(ValueError):
        _parse_assets([{"symbol": "BTC-USD", "source": "coinbase", "mad_window": "lots"}])


def test_parse_assets_casts_override_to_declared_type():
    assets = _parse_assets([{"symbol": "BTC-USD", "source": "coinbase", "price_zscore_threshold": 4}])
    assert assets[0].overrides["price_zscore_threshold"] == 4.0
    assert isinstance(assets[0].overrides["price_zscore_threshold"], float)


def test_params_for_falls_back_to_global_defaults():
    cfg = Config(assets=_parse_assets([{"symbol": "BTC-USD", "source": "coinbase"}]),
                 price_zscore_threshold=3.0, volume_zscore_threshold=4.0)
    params = cfg.params_for(cfg.assets[0])
    assert params.price_zscore_threshold == 3.0
    assert params.volume_zscore_threshold == 4.0


def test_params_for_applies_asset_override_without_affecting_others():
    assets = _parse_assets([
        {"symbol": "BTC-USD", "source": "coinbase"},
        {"symbol": "CNY=X", "source": "yahoo", "price_zscore_threshold": 4.0},
    ])
    cfg = Config(assets=assets, price_zscore_threshold=3.0)
    btc_params = cfg.params_for(assets[0])
    cny_params = cfg.params_for(assets[1])
    assert btc_params.price_zscore_threshold == 3.0
    assert cny_params.price_zscore_threshold == 4.0
    # overriding one field leaves the asset's other resolved params at the globals
    assert cny_params.volume_zscore_threshold == cfg.volume_zscore_threshold

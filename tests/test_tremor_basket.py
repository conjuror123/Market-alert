import pytest
import yaml

from tremor.basket import BasketConfigError, load_basket

MINIMAL = {
    "anchor_exchange_tz": "America/New_York",
    "history_since": "2021-01-01",
    "session_templates": {"s": {"tz": "UTC"}},
    "volatility_index": {"series_id": "VIXCLS", "source": "fred", "interval": "1d"},
    "assets": [],
}


def asset(ticker, block, **over):
    return {"ticker": ticker, "source": "twelvedata", "block": block,
            "has_volume": True, "tick_size": 0.01, "session_template": "s",
            "fetch_interval": "1h"} | over


def write(tmp_path, raw):
    path = tmp_path / "basket.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return str(path)


def two_block_config(**extra):
    return MINIMAL | {"assets": [
        asset("A", "equity"), asset("B", "equity"),
        asset("C", "FX"), asset("D", "FX"),
    ]} | extra


def test_rejects_block_outside_the_taxonomy(tmp_path):
    raw = two_block_config()
    raw["assets"].append(asset("E", "livestock"))
    with pytest.raises(BasketConfigError, match="is not one of"):
        load_basket(write(tmp_path, raw))


def test_rejects_duplicate_instrument(tmp_path):
    raw = two_block_config()
    raw["assets"].append(asset("A", "FX"))
    with pytest.raises(BasketConfigError, match="listed twice"):
        load_basket(write(tmp_path, raw))


def test_rejects_unknown_session_template(tmp_path):
    raw = two_block_config()
    raw["assets"][0]["session_template"] = "no-such-thing"
    with pytest.raises(BasketConfigError, match="session template"):
        load_basket(write(tmp_path, raw))


def test_outside_basket_instruments_are_instruments_but_not_assets(tmp_path):
    raw = two_block_config(outside_basket=[asset("Z", "FX")])
    basket = load_basket(write(tmp_path, raw))
    assert "Z" not in [a.ticker for a in basket.assets]
    assert [a.ticker for a in basket.outside] == ["Z"]
    assert "twelvedata:Z" in {a.asset_id for a in basket.instruments}


def test_a_provider_change_keeps_the_instruments_identity(tmp_path):
    # source names the store, provider the server: moving a fund to another
    # provider must not orphan its bars or its verdicts.
    raw = two_block_config()
    first = load_basket(write(tmp_path, raw)).assets[0]
    raw["assets"][0]["provider"] = "yahoo"
    moved = load_basket(write(tmp_path, raw)).assets[0]
    assert moved.fetched_from == "yahoo" != first.fetched_from
    assert (moved.asset_id, moved.file_stem) == (first.asset_id, first.file_stem)


def test_file_stem_is_filesystem_safe(tmp_path):
    raw = two_block_config()
    raw["assets"][2]["ticker"] = "EUR/USD"
    basket = load_basket(write(tmp_path, raw))
    pair = next(a for a in basket.assets if a.ticker == "EUR/USD")
    assert pair.file_stem == "twelvedata_EUR_USD"
    assert pair.asset_id == "twelvedata:EUR/USD"


def test_a_one_member_block_does_not_load(tmp_path):
    # A block of one is a typo in basket.yaml, not a group.
    raw = two_block_config()
    raw["assets"].append(asset("Z", "crypto"))
    with pytest.raises(BasketConfigError, match="crypto"):
        load_basket(write(tmp_path, raw))


def test_the_thin_blocks_are_the_ones_we_know_about():
    # A tripwire: no block has fewer than six members, and this fails when
    # that changes, so a thin block is a decision rather than an accident.
    basket = load_basket()
    thin = {b for b, m in basket.by_block().items() if len(m) < 6}
    assert thin == set()


def test_real_basket_config_is_valid():
    basket = load_basket()
    assert set(basket.by_block()) == {
        "equity", "rates", "credit", "energy", "precious_metals",
        "industrial_metals", "agriculture", "FX", "crypto"}
    for block, members in basket.by_block().items():
        assert len(members) >= 2, block


def test_rejects_a_nonpositive_tick_size(tmp_path):
    # The price step feeds the winsorization floor; zero or a negative value would make the
    # floor meaningless rather than strict.
    raw = two_block_config()
    raw["assets"][0]["tick_size"] = 0
    with pytest.raises(BasketConfigError, match="tick_size"):
        load_basket(write(tmp_path, raw))


def test_rejects_a_tick_size_that_yaml_parsed_as_text(tmp_path):
    # YAML 1.1 reads 1e-05 as a STRING - the form 0.00001 is required. Letting
    # that through silently means breaking winsorization for no reason at all.
    raw = two_block_config()
    raw["assets"][0]["tick_size"] = "1e-05"
    with pytest.raises(BasketConfigError, match="tick_size"):
        load_basket(write(tmp_path, raw))


def test_real_config_tick_sizes_are_plausible(tmp_path):
    basket = load_basket()
    for a in basket.instruments:
        # Nickel is quoted in dollars a tonne and steps by the LME's $5; the
        # futures by their contracts' ticks (cocoa $1 a tonne); a coin by
        # Binance's (test_a_coins_tick_is_binances).
        limit = 0.01 if a.session_template in ("us_equity", "fx_continuous") else 10.0
        assert 0 < a.tick_size <= limit, a.ticker


# Binance's PRICE_FILTER tickSize for each USDT pair (exchangeInfo, 2026-10-04).
BINANCE_TICKS = {
    "BTC/USDT": 0.01, "ETH/USDT": 0.01, "SOL/USDT": 0.01, "LTC/USDT": 0.01,
    "BCH/USDT": 0.1, "LINK/USDT": 0.001, "ADA/USDT": 0.0001, "DOGE/USDT": 0.00001,
    "AVAX/USDT": 0.001, "XRP/USDT": 0.0001, "DOT/USDT": 0.001, "POL/USDT": 0.00001,
    "UNI/USDT": 0.001, "ATOM/USDT": 0.001, "FIL/USDT": 0.0001, "AAVE/USDT": 0.01,
}


def test_a_coins_tick_is_binances():
    coins = {a.ticker: a.tick_size for a in load_basket().instruments
             if a.source == "binance"}
    assert coins == BINANCE_TICKS

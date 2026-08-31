import pytest
import yaml

from meals.basket import BasketConfigError, load_basket

MINIMAL = {
    "anchor_exchange_tz": "America/New_York",
    "history_since": "2021-01-01",
    "session_templates": {"s": {"tz": "UTC"}},
    "volatility_index": {"series_id": "VIXCLS", "source": "fred", "interval": "1d"},
    "assets": [],
}


def asset(ticker, block, tier=1, **over):
    return {"ticker": ticker, "source": "twelvedata", "tier": tier, "block": block,
            "has_volume": True, "tick_size": 0.01, "session_template": "s",
            "fetch_interval": "1h"} | over


def write(tmp_path, raw):
    path = tmp_path / "basket.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return str(path)


def two_block_config(**extra):
    return MINIMAL | {"assets": [
        asset("A", "equity"), asset("B", "equity", tier=2),
        asset("C", "FX"), asset("D", "FX", tier=2),
    ]} | extra


def test_weights_follow_the_equal_weight_rule(tmp_path):
    # Три актива в equity против одного в FX: блоки всё равно весят поровну,
    # а внутри блока активы делят вес блока между собой (п.2.3).
    raw = MINIMAL | {"assets": [
        asset("A", "equity"), asset("B", "equity", tier=2), asset("C", "equity", tier=2),
        asset("D", "FX"), asset("E", "FX", tier=2),
    ]}
    basket = load_basket(write(tmp_path, raw))
    w = basket.weights()

    equity = [w["twelvedata:A"], w["twelvedata:B"], w["twelvedata:C"]]
    assert equity == pytest.approx([1 / 6] * 3)
    assert w["twelvedata:D"] == pytest.approx(1 / 4)
    assert sum(w.values()) == pytest.approx(1.0)
    # Блоки равновесны между собой - главное свойство правила.
    assert sum(equity) == pytest.approx(w["twelvedata:D"] + w["twelvedata:E"])


def test_weight_is_not_readable_from_the_file(tmp_path):
    # По п.2.3 вес производный. Даже если его дописать в конфигурацию, система
    # обязана считать по правилу, а не доверять записанному числу.
    raw = two_block_config()
    raw["assets"][0]["weight"] = 0.99
    basket = load_basket(write(tmp_path, raw))
    assert basket.weights()["twelvedata:A"] == pytest.approx(1 / 4)


def test_rejects_block_outside_the_taxonomy(tmp_path):
    raw = two_block_config()
    raw["assets"].append(asset("E", "energy"))
    with pytest.raises(BasketConfigError, match="не из перечня"):
        load_basket(write(tmp_path, raw))


def test_rejects_duplicate_instrument(tmp_path):
    raw = two_block_config()
    raw["assets"].append(asset("A", "FX", tier=2))
    with pytest.raises(BasketConfigError, match="дважды"):
        load_basket(write(tmp_path, raw))


def test_rejects_unknown_session_template(tmp_path):
    raw = two_block_config()
    raw["assets"][0]["session_template"] = "нет-такого"
    with pytest.raises(BasketConfigError, match="шаблон сессии"):
        load_basket(write(tmp_path, raw))


def test_rejects_basket_where_quorum_is_unreachable(tmp_path):
    # Один блок с двумя активами и три блока по одному: кворум часа требует
    # два блока по два актива, значит кластерные триггеры не сработают никогда.
    raw = MINIMAL | {"assets": [
        asset("A", "equity"), asset("B", "equity", tier=2),
        asset("C", "FX"), asset("D", "rates"), asset("E", "commodities"),
    ]}
    with pytest.raises(BasketConfigError, match="Кворум недостижим"):
        load_basket(write(tmp_path, raw))


def test_outside_basket_instruments_are_not_weighted(tmp_path):
    raw = two_block_config(outside_basket=[asset("Z", "FX", tier=2)])
    basket = load_basket(write(tmp_path, raw))
    assert "twelvedata:Z" not in basket.weights()
    assert [a.ticker for a in basket.outside] == ["Z"]
    assert "twelvedata:Z" in {a.asset_id for a in basket.instruments}


def test_file_stem_is_filesystem_safe(tmp_path):
    raw = two_block_config()
    raw["assets"][2]["ticker"] = "EUR/USD"
    basket = load_basket(write(tmp_path, raw))
    pair = next(a for a in basket.assets if a.ticker == "EUR/USD")
    assert pair.file_stem == "twelvedata_EUR_USD"
    assert pair.asset_id == "twelvedata:EUR/USD"


def test_real_basket_config_is_valid():
    basket = load_basket()
    assert len(basket.assets) == 21
    assert set(basket.by_block()) == {"equity", "rates", "FX", "commodities", "crypto"}
    assert sum(basket.weights().values()) == pytest.approx(1.0)
    # Ночью в сессии остаются только FX и crypto - вместе они обязаны набирать
    # кворум п.2.3, иначе система слепа вне американской сессии.
    night = [a for a in basket.assets if a.block in ("FX", "crypto")]
    assert len(night) >= 8
    assert sum(1 for a in night if a.tier == 1) >= 2


def test_rejects_a_nonpositive_tick_size(tmp_path):
    # Шаг цены участвует в нижней отсечке п.2.5; ноль или отрицательное
    # значение сделали бы отсечку бессмысленной, а не строгой.
    raw = two_block_config()
    raw["assets"][0]["tick_size"] = 0
    with pytest.raises(BasketConfigError, match="tick_size"):
        load_basket(write(tmp_path, raw))


def test_rejects_a_tick_size_that_yaml_parsed_as_text(tmp_path):
    # YAML 1.1 читает 1e-05 как СТРОКУ - нужна форма 0.00001. Молча пропустить
    # такое значит уронить винзоризацию на ровном месте.
    raw = two_block_config()
    raw["assets"][0]["tick_size"] = "1e-05"
    with pytest.raises(BasketConfigError, match="tick_size"):
        load_basket(write(tmp_path, raw))


def test_real_config_tick_sizes_are_plausible(tmp_path):
    basket = load_basket()
    for a in basket.instruments:
        assert 0 < a.tick_size <= 0.01, a.ticker

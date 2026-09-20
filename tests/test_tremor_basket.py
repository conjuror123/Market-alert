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
    # Three assets in equity against one in FX: the blocks still weigh the same,
    # and within a block the assets split the block's weight between them.
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
    # Blocks are equal to one another - the rule's main property.
    assert sum(equity) == pytest.approx(w["twelvedata:D"] + w["twelvedata:E"])


def test_weight_is_not_readable_from_the_file(tmp_path):
    # The weight is derived. Even if it is written into the
    # configuration, the system must compute by the rule rather than trust the
    # stored number.
    raw = two_block_config()
    raw["assets"][0]["weight"] = 0.99
    basket = load_basket(write(tmp_path, raw))
    assert basket.weights()["twelvedata:A"] == pytest.approx(1 / 4)


def test_rejects_block_outside_the_taxonomy(tmp_path):
    raw = two_block_config()
    raw["assets"].append(asset("E", "livestock"))
    with pytest.raises(BasketConfigError, match="is not one of"):
        load_basket(write(tmp_path, raw))


def test_rejects_duplicate_instrument(tmp_path):
    raw = two_block_config()
    raw["assets"].append(asset("A", "FX", tier=2))
    with pytest.raises(BasketConfigError, match="listed twice"):
        load_basket(write(tmp_path, raw))


def test_rejects_unknown_session_template(tmp_path):
    raw = two_block_config()
    raw["assets"][0]["session_template"] = "no-such-thing"
    with pytest.raises(BasketConfigError, match="session template"):
        load_basket(write(tmp_path, raw))


def test_rejects_basket_where_quorum_is_unreachable(tmp_path):
    # One block with two assets and three blocks with one each: the hourly quorum
    # requires two blocks of two, so the cluster triggers will never fire.
    raw = MINIMAL | {"assets": [
        asset("A", "equity"), asset("B", "equity", tier=2),
        asset("C", "FX"), asset("D", "rates"), asset("E", "credit"),
    ]}
    with pytest.raises(BasketConfigError, match="Quorum unreachable"):
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
    assert set(basket.by_block()) == {
        "equity", "rates", "credit", "energy", "precious_metals",
        "industrial_metals", "agriculture", "FX", "crypto"}
    # Every block needs two members or it can never be active (the
    # BLOCK_ACTIVE_MIN), and a block that can never be active contributes exactly
    # nothing to the quorum - measured: one crypto asset instead of two takes the
    # share of hours passing quorum from 70.6% to 19.9%, losing every overnight
    # hour. Two is also the floor at which the leave-one-out block factor exists
    # at all: drop a block to one member and that member has no peers to be
    # compared with and no model but its own drift.
    for block, members in basket.by_block().items():
        assert len(members) >= 2, block
    assert sum(basket.weights().values()) == pytest.approx(1.0)
    # At night only FX and crypto remain in session - together they must make the
    # quorum, or the system is blind outside the US session.
    night = [a for a in basket.assets if a.block in ("FX", "crypto")]
    assert len(night) >= 8
    assert sum(1 for a in night if a.tier == 1) >= 2


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
        assert 0 < a.tick_size <= 0.01, a.ticker


# --- the per-instrument size floor -------------------------------------------

def tuning_for(tmp_path, raw):
    from tremor.basket import load_tuning

    load_tuning.cache_clear()
    return load_tuning(write(tmp_path, raw))


def test_an_instrument_without_an_override_takes_the_shared_floor(tmp_path):
    t = tuning_for(tmp_path, two_block_config(min_move_sigma=1.5))
    assert t.floor_for("twelvedata:A") == 1.5
    assert t.floor_for("twelvedata:nothing-like-this") == 1.5


def test_an_instrument_with_an_override_takes_its_own(tmp_path):
    # The one lever that makes instruments speak at different rates. The rungs
    # cannot: every instrument clears a rung about once per its lookback,
    # whatever its market does.
    raw = MINIMAL | {"min_move_sigma": 1.0, "assets": [
        asset("A", "equity"), asset("B", "equity", tier=2, min_move_sigma=2.5),
    ]}
    t = tuning_for(tmp_path, raw)
    assert t.floor_for("twelvedata:B") == 2.5
    assert t.floor_for("twelvedata:A") == 1.0        # untouched


def test_the_committed_basket_keeps_shy_and_metals_on_the_shared_floor():
    # Live `/floor BKLN` writes that instrument's number on the default branch.
    # Do not pin it here: a feature branch sees the committed yaml, which may
    # still be 1.0, and CI must not fail for that. SHY and the metals block
    # stay on the shared 1.0 unless someone floors them on purpose.
    from tremor.basket import DEFAULT_BASKET_PATH, load_tuning

    load_tuning.cache_clear()
    t = load_tuning(DEFAULT_BASKET_PATH)
    assert t.min_move_sigma == 1.0
    assert t.floor_for("twelvedata:SHY") == t.min_move_sigma
    assert t.floor_for("block:industrial_metals") == t.min_move_sigma


def test_raising_one_instruments_floor_leaves_every_other_alone(tmp_path):
    # The whole reason this is per instrument. Raising the SHARED number high
    # enough to silence the one that annoyed the reader silences every quiet
    # instrument with it - point at one and lose the others.
    raw = MINIMAL | {"min_move_sigma": 1.0, "assets": [
        asset("A", "equity", min_move_sigma=4.0), asset("B", "equity", tier=2),
        asset("C", "FX"), asset("D", "FX", tier=2),
    ]}
    t = tuning_for(tmp_path, raw)
    assert t.floor_for("twelvedata:A") == 4.0
    assert [t.floor_for(f"twelvedata:{x}") for x in "BCD"] == [1.0, 1.0, 1.0]


def test_an_instrument_can_opt_out_of_the_floor_entirely(tmp_path):
    raw = MINIMAL | {"min_move_sigma": 2.0,
                     "assets": [asset("A", "equity", min_move_sigma=0)]}
    assert tuning_for(tmp_path, raw).floor_for("twelvedata:A") == 0.0


def test_a_non_basket_instrument_may_carry_a_floor_too(tmp_path):
    raw = MINIMAL | {"assets": [asset("A", "equity")],
                     "outside": [asset("Z", "equity", min_move_sigma=3.0)]}
    assert tuning_for(tmp_path, raw).floor_for("twelvedata:Z") == 3.0


def test_a_floor_that_is_not_a_number_is_refused_by_name(tmp_path):
    raw = MINIMAL | {"assets": [asset("A", "equity", min_move_sigma="loud")]}
    with pytest.raises(BasketConfigError, match="twelvedata:A"):
        tuning_for(tmp_path, raw)


def test_a_negative_floor_is_refused(tmp_path):
    raw = MINIMAL | {"assets": [asset("A", "equity", min_move_sigma=-1)]}
    with pytest.raises(BasketConfigError, match="negative"):
        tuning_for(tmp_path, raw)


def test_a_block_override_does_not_change_member_floors(tmp_path):
    raw = MINIMAL | {"min_move_sigma": 1.0,
                     "block_min_move_sigma": {"industrial_metals": 2.5},
                     "assets": [
                         asset("DBB", "industrial_metals", min_move_sigma=1.8),
                         asset("CPER", "industrial_metals"),
                     ]}
    t = tuning_for(tmp_path, raw)
    assert t.floor_for("block:industrial_metals") == 2.5
    assert t.floor_for("twelvedata:DBB") == 1.8
    assert t.floor_for("twelvedata:CPER") == 1.0


def test_a_block_without_an_override_takes_the_shared_floor(tmp_path):
    t = tuning_for(tmp_path, two_block_config(min_move_sigma=1.5))
    assert t.floor_for("block:equity") == 1.5
    assert t.floor_for("block:nothing") == 1.5


def test_the_tuning_stays_hashable_so_it_can_be_cached(tmp_path):
    # load_tuning is lru_cached and Tuning is frozen. A dict field would quietly
    # stop it being either, and the failure would be a TypeError deep in a run.
    t = tuning_for(tmp_path, two_block_config())
    assert hash(t) == hash(t)

"""Private-chat /floor writes min_move_sigma on the named instrument or block."""
from types import SimpleNamespace

import pytest
import yaml

from price_monitor import floor as fl
from tremor.basket import Asset, Basket, VolatilityIndex
from datetime import date


def _asset(ticker, block, label=None, **over):
    base = dict(ticker=ticker, source="twelvedata", provider="yahoo",
                tier=2, block=block, has_volume=True, tick_size=0.01,
                session_template="us_equity", fetch_interval="30min",
                label=label or ticker, in_basket=True)
    return Asset(**(base | over))


def basket():
    return Basket(
        assets=(
            _asset("BKLN", "credit", "Senior bank loans"),
            _asset("SHY", "rates", "1-3 year Treasuries"),
            _asset("DBB", "industrial_metals",
                   "Base metals (aluminium, zinc, copper)"),
            _asset("CPER", "industrial_metals", "Copper"),
        ),
        outside=(),
        volatility_index=VolatilityIndex(
            "VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York",
        history_since=date(2021, 1, 1), session_templates={},
    )


SAMPLE = """
min_move_sigma: 1.0
assets:
  - ticker: BKLN
    source: twelvedata
    block: credit
    label: "Senior bank loans"
    # keep me
  - ticker: SHY
    source: twelvedata
    block: rates
    min_move_sigma: 1.8
  - ticker: DBB
    source: twelvedata
    block: industrial_metals
    label: "Base metals (aluminium, zinc, copper)"
  - ticker: CPER
    source: twelvedata
    block: industrial_metals
    label: "Copper"
outside: []
"""


def test_a_floor_command_is_parsed_as_typed():
    assert fl.parse_command("/floor BKLN 2.5") == ("BKLN", 2.5)
    assert fl.parse_command("/floor BKLN 2.2") == ("BKLN", 2.2)
    assert fl.parse_command("/floor@TremorBot Base metals 2.5") == (
        "Base metals", 2.5)
    assert fl.parse_command("hello") is None
    assert fl.parse_command("/floor") is None


def test_a_ticker_and_a_block_name_both_resolve(monkeypatch):
    b = basket()
    kind, label, ids = fl.resolve_target("BKLN", b)
    assert kind == "instrument" and label == "BKLN"
    assert ids == ["twelvedata:BKLN"]

    kind, label, ids = fl.resolve_target("Base metals", b)
    assert kind == "block"
    assert set(ids) == {"twelvedata:DBB", "twelvedata:CPER"}

    kind, _, ids = fl.resolve_target("industrial metals", b)
    assert kind == "block" and len(ids) == 2

    with pytest.raises(ValueError):
        fl.resolve_target("NOPE", b)


def test_yaml_inserts_or_replaces_without_touching_neighbours(tmp_path):
    path = tmp_path / "basket.yaml"
    path.write_text(SAMPLE)
    fl.set_floors(["BKLN"], 2.5, str(path))
    fl.set_floors(["BKLN"], 2.2, str(path))
    text = path.read_text()
    assert "min_move_sigma: 1.0\n" in text
    assert "# keep me" in text
    raw = yaml.safe_load(text)
    by_ticker = {a["ticker"]: a for a in raw["assets"]}
    assert by_ticker["BKLN"]["min_move_sigma"] == 2.2
    assert by_ticker["SHY"]["min_move_sigma"] == 1.8
    assert "min_move_sigma" not in by_ticker["DBB"]


def test_a_block_floor_is_written_on_every_member(tmp_path):
    path = tmp_path / "basket.yaml"
    path.write_text(SAMPLE)
    b = basket()
    reply = fl.apply_command("Base metals", 2.5, str(path), b)
    raw = yaml.safe_load(path.read_text())
    by_ticker = {a["ticker"]: a for a in raw["assets"]}
    assert by_ticker["DBB"]["min_move_sigma"] == 2.5
    assert by_ticker["CPER"]["min_move_sigma"] == 2.5
    assert "min_move_sigma" not in by_ticker["BKLN"]
    assert "2.5" in reply and "DBB" in reply


def test_process_updates_applies_a_private_command(tmp_path, monkeypatch):
    path = tmp_path / "basket.yaml"
    path.write_text(SAMPLE)
    cfg = SimpleNamespace(
        telegram_bot_token="t", telegram_chat_id="99",
        telegram_health_chat_id="99",
    )
    sent = []
    monkeypatch.setattr(fl, "fetch_telegram_updates", lambda *a, **k: [{
        "update_id": 7,
        "message": {
            "chat": {"id": 99, "type": "private"},
            "text": "/floor BKLN 2.5",
        },
    }])
    monkeypatch.setattr(fl, "send_telegram_message",
                        lambda token, chat, text: sent.append((chat, text)) or 1)
    monkeypatch.setattr(fl, "load_basket", basket)
    state = {}
    assert fl.process_updates(cfg, state, str(path)) == 1
    assert state[fl.OFFSET_KEY] == 8
    assert sent and "BKLN" in sent[0][1]
    raw = yaml.safe_load(path.read_text())
    by_ticker = {a["ticker"]: a for a in raw["assets"]}
    assert by_ticker["BKLN"]["min_move_sigma"] == 2.5


def test_a_channel_command_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "basket.yaml"
    path.write_text(SAMPLE)
    cfg = SimpleNamespace(
        telegram_bot_token="t", telegram_chat_id="-1001",
        telegram_health_chat_id="99",
    )
    monkeypatch.setattr(fl, "fetch_telegram_updates", lambda *a, **k: [{
        "update_id": 3,
        "message": {
            "chat": {"id": -1001, "type": "channel"},
            "text": "/floor BKLN 9",
        },
    }])
    monkeypatch.setattr(fl, "send_telegram_message",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    state = {}
    assert fl.process_updates(cfg, state, str(path)) == 0
    assert "min_move_sigma: 9" not in path.read_text()
    assert state[fl.OFFSET_KEY] == 4

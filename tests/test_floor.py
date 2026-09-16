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
    kind, label, key = fl.resolve_target("BKLN", b)
    assert kind == "instrument" and label == "BKLN"
    assert key == "twelvedata:BKLN"

    kind, label, key = fl.resolve_target("Base metals", b)
    assert kind == "block" and key == "industrial_metals"

    kind, _, key = fl.resolve_target("industrial metals", b)
    assert kind == "block" and key == "industrial_metals"

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


def test_a_block_floor_does_not_copy_onto_members(tmp_path):
    path = tmp_path / "basket.yaml"
    path.write_text(SAMPLE)
    b = basket()
    payload = fl.apply_command("Base metals", 2.5, str(path), b)
    raw = yaml.safe_load(path.read_text())
    by_ticker = {a["ticker"]: a for a in raw["assets"]}
    assert "min_move_sigma" not in by_ticker["DBB"]
    assert "min_move_sigma" not in by_ticker["CPER"]
    assert "min_move_sigma" not in by_ticker["BKLN"]
    assert raw["block_min_move_sigma"]["industrial_metals"] == 2.5
    reply = payload["applied"]
    assert "2.5" in reply and "unchanged" in reply
    assert "DBB" not in reply


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
    events = tmp_path / "none.parquet"
    assert fl.process_updates(cfg, state, str(path), str(events)) == 1
    assert state[fl.OFFSET_KEY] == 8
    assert sent == []
    pending = state[fl.PENDING_KEY]
    assert pending[0]["asset_id"] == "twelvedata:BKLN"
    assert "2.5" in pending[0]["applied"]
    raw = yaml.safe_load(path.read_text())
    by_ticker = {a["ticker"]: a for a in raw["assets"]}
    assert by_ticker["BKLN"]["min_move_sigma"] == 2.5

    assert fl.send_pending_replies(cfg, state, str(events)) == 0
    assert state[fl.PENDING_KEY]

    day = 1_700_000_000
    _events_parquet(events, [
        {"asset_id": "twelvedata:BKLN", "hour_utc": day, "r": 0.03, "sigma_lt": 0.01},
        {"asset_id": "twelvedata:BKLN", "hour_utc": day + 400 * 86400, "r": 0.04,
         "sigma_lt": 0.01},
    ])
    assert fl.send_pending_replies(cfg, state, str(events)) == 1
    assert sent and "BKLN" in sent[0][1] and "2.5" in sent[0][1]
    assert "stored history" in sent[0][1]
    assert "No stored events yet" not in sent[0][1]
    assert not state[fl.PENDING_KEY]


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


def _events_parquet(path, rows):
    import pandas as pd

    frame = pd.DataFrame(rows)
    frame.to_parquet(path, index=False)
    return str(path)


def test_the_reply_counts_unique_days_not_hours(tmp_path):
    # Two hours the same UTC day, one a year later: that is two events, not three.
    path = tmp_path / "events.parquet"
    day = 1_700_000_000
    _events_parquet(path, [
        {"asset_id": "twelvedata:BKLN", "hour_utc": day, "r": 0.03, "sigma_lt": 0.01},
        {"asset_id": "twelvedata:BKLN", "hour_utc": day + 3600, "r": 0.04, "sigma_lt": 0.01},
        {"asset_id": "twelvedata:BKLN", "hour_utc": day + 365 * 86400, "r": 0.03,
         "sigma_lt": 0.01},
    ])
    text = fl.describe_rate("twelvedata:BKLN", 2.5, str(path), basket())
    assert "2 events" in text
    assert "pushes" not in text
    assert "block line" not in text


def test_a_block_rate_ignores_member_rows_and_hours_below_the_floor(tmp_path):
    path = tmp_path / "events.parquet"
    day = 1_700_000_000
    _events_parquet(path, [
        {"asset_id": "block:industrial_metals", "hour_utc": day,
         "r": 0.03, "sigma_lt": 0.01},          # 3x, kept
        {"asset_id": "block:industrial_metals", "hour_utc": day + 400 * 86400,
         "r": 0.02, "sigma_lt": 0.01},          # 2x, dropped at 2.5
        {"asset_id": "twelvedata:DBB", "hour_utc": day, "r": 0.05, "sigma_lt": 0.01},
    ])
    text = fl.describe_rate("block:industrial_metals", 2.5, str(path), basket())
    assert "1 event" in text
    assert "block line" in text
    assert "pushes" not in text


def test_a_lower_block_floor_replaces_the_yaml_value(tmp_path):
    path = tmp_path / "basket.yaml"
    path.write_text(SAMPLE)
    fl.set_block_floor("industrial_metals", 2.5, str(path))
    fl.set_block_floor("industrial_metals", 2.2, str(path))
    raw = yaml.safe_load(path.read_text())
    assert raw["block_min_move_sigma"]["industrial_metals"] == 2.2
    by_ticker = {a["ticker"]: a for a in raw["assets"]}
    assert "min_move_sigma" not in by_ticker["DBB"]

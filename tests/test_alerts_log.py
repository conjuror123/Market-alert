import os
from datetime import datetime, timezone

from price_monitor.alerts_log import (
    load_alerts_log,
    pending_entries,
    record_sent_alert,
    save_alerts_log,
)


def test_save_and_load_roundtrip(tmp_path):
    path = os.path.join(tmp_path, "alerts_log.json")
    entries = []
    record_sent_alert(
        entries,
        chat_id="@my_market_alerts",
        message_id=42,
        symbol="Ethereum",
        message_text="Ethereum - unusual market move",
        last_close=2473.66,
        last_return_pct=-1.45,
        ewma_z=-3.60,
        robust_z=-3.61,
        volume_z=0.70,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    save_alerts_log(path, entries)

    loaded = load_alerts_log(path)
    assert loaded == entries


def test_load_missing_file_returns_empty_list(tmp_path):
    path = os.path.join(tmp_path, "missing.json")
    assert load_alerts_log(path) == []


def test_load_corrupt_file_returns_empty_list(tmp_path):
    path = os.path.join(tmp_path, "alerts_log.json")
    with open(path, "w") as f:
        f.write("not json")
    assert load_alerts_log(path) == []


def test_record_sent_alert_defaults_to_unexplained():
    entries = []
    record_sent_alert(
        entries,
        chat_id="@chan",
        message_id=1,
        symbol="Gold",
        message_text="Gold - unusual market move",
        last_close=2000.0,
        last_return_pct=1.2,
        ewma_z=3.1,
        robust_z=3.2,
        volume_z=0.5,
    )
    assert entries[0]["explained"] is False


def test_record_sent_alert_defaults_signal_type_to_hourly():
    entries = []
    record_sent_alert(
        entries,
        chat_id="@chan",
        message_id=1,
        symbol="Gold",
        message_text="Gold - unusual market move",
        last_close=2000.0,
        last_return_pct=1.2,
        ewma_z=3.1,
        robust_z=3.2,
        volume_z=0.5,
    )
    assert entries[0]["signal_type"] == "hourly"


def test_record_sent_alert_records_daily_signal_type():
    entries = []
    record_sent_alert(
        entries,
        chat_id="@chan",
        message_id=1,
        symbol="EUR/USD",
        message_text="EUR/USD - unusual daily move",
        last_close=1.05,
        last_return_pct=0.8,
        ewma_z=4.5,
        robust_z=4.2,
        volume_z=0.0,
        signal_type="daily",
    )
    assert entries[0]["signal_type"] == "daily"


def test_pending_entries_filters_out_explained():
    entries = [
        {"symbol": "Gold", "explained": True},
        {"symbol": "Ethereum", "explained": False},
        {"symbol": "Oil"},
    ]
    assert [e["symbol"] for e in pending_entries(entries)] == ["Ethereum", "Oil"]

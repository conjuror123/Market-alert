import json
import os
from datetime import datetime, timezone

from price_monitor.analysis import Signal
from price_monitor.decision_log import append_decision, log_path


def make_signal(**overrides):
    defaults = dict(
        symbol="EUR/USD", last_close=1.05, last_return_pct=0.2, ewma_z=1.0,
        robust_z=1.1, volume_z=0.0, price_alert=False, volume_alert=False, reasons=[],
    )
    defaults.update(overrides)
    return Signal(**defaults)


def test_log_path_uses_safe_filename(tmp_path):
    path = log_path(str(tmp_path), "twelvedata", "EUR/USD")
    assert path == os.path.join(str(tmp_path), "twelvedata_EUR_USD.ndjson")


def test_append_decision_writes_one_json_line(tmp_path):
    path = os.path.join(tmp_path, "asset.ndjson")
    signal = make_signal(price_alert=True)
    append_decision(
        path, signal, signal_type="hourly", notified=True,
        price_zscore_threshold=7.0, price_zscore_override=22.0,
        volume_zscore_threshold=22.0, volume_zscore_override=22.0,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    with open(path) as f:
        lines = f.readlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["symbol"] == "EUR/USD"
    assert row["signal_type"] == "hourly"
    assert row["notified"] is True
    assert row["price_alert"] is True
    assert row["price_zscore_threshold"] == 7.0


def test_append_decision_is_append_only(tmp_path):
    path = os.path.join(tmp_path, "asset.ndjson")
    for i in range(3):
        append_decision(
            path, make_signal(), signal_type="daily", notified=False,
            price_zscore_threshold=4.0, price_zscore_override=8.0,
            volume_zscore_threshold=1e9, volume_zscore_override=1e9,
            now=datetime(2026, 1, 1 + i, tzinfo=timezone.utc),
        )
    with open(path) as f:
        lines = f.readlines()
    assert len(lines) == 3


def test_append_decision_records_both_signal_types_in_one_file(tmp_path):
    path = os.path.join(tmp_path, "asset.ndjson")
    append_decision(
        path, make_signal(), signal_type="hourly", notified=False,
        price_zscore_threshold=7.0, price_zscore_override=22.0,
        volume_zscore_threshold=22.0, volume_zscore_override=22.0,
    )
    append_decision(
        path, make_signal(), signal_type="daily", notified=False,
        price_zscore_threshold=4.0, price_zscore_override=8.0,
        volume_zscore_threshold=1e9, volume_zscore_override=1e9,
    )
    with open(path) as f:
        rows = [json.loads(line) for line in f]
    assert [r["signal_type"] for r in rows] == ["hourly", "daily"]

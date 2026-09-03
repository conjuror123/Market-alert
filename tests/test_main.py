import json
import os

from price_monitor import __main__ as main_module
from price_monitor.analysis import Signal
from price_monitor.candle_store import load_candles, store_path
from price_monitor.config import AssetConfig, Config
from price_monitor.decision_log import log_path
from price_monitor.models import Candle
from price_monitor.state import load_state


def make_signal(**overrides):
    defaults = dict(
        symbol="EUR/USD", last_close=1.05, last_return_pct=2.5, ewma_z=8.0,
        robust_z=8.0, volume_z=0.0, price_alert=True, volume_alert=False, reasons=["test"],
    )
    defaults.update(overrides)
    return Signal(**defaults)


def make_config(tmp_path):
    return Config(
        assets=[AssetConfig(symbol="EUR/USD", source="twelvedata", label="EUR/USD")],
        telegram_bot_token="bot-token",
        telegram_chat_id="@chan",
        decision_log_dir=os.path.join(tmp_path, "decision_log"),
        price_zscore_threshold=7.0, price_zscore_override=22.0,
        cooldown_minutes=20160, escalation_factor=1.5,
        daily_price_zscore_threshold=4.0, daily_price_zscore_override=8.0,
        daily_cooldown_minutes=43200, daily_escalation_factor=1.5,
    )


def test_format_alert_hourly_mentions_the_hour_and_signal_label():
    cfg = make_config("/tmp")
    params = cfg.params_for(cfg.assets[0])
    text = main_module.format_alert(make_signal(), params, "hourly")
    assert "the last hour" in text
    assert "hourly" in text
    assert "unusual market move" in text
    assert "Volume z-score" in text  # hourly still shows the volume line


def test_format_alert_daily_mentions_the_day_and_signal_label():
    cfg = make_config("/tmp")
    params = cfg.params_for(cfg.assets[0])
    text = main_module.format_alert(make_signal(), params, "daily")
    assert "the last day" in text
    assert "daily" in text
    assert "unusual daily move" in text
    assert "Volume z-score" not in text  # daily signal is price-only
    assert "4.0" in text  # uses the daily threshold, not the hourly one


def test_handle_signal_sends_and_logs_decision(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    asset = cfg.assets[0]
    params = cfg.params_for(asset)
    sent = []
    monkeypatch.setattr(main_module, "send_telegram_message", lambda *a, **k: sent.append(a) or 123)
    monkeypatch.setattr(main_module, "edit_telegram_message", lambda *a, **k: None)

    state, alerts_log = {}, []
    notified = main_module._handle_signal(
        cfg, asset, params, make_signal(), "hourly", "twelvedata:EUR/USD", state, alerts_log)

    assert notified is True
    assert len(sent) == 1
    assert alerts_log[0]["signal_type"] == "hourly"
    assert "twelvedata:EUR/USD" in state

    rows = [json.loads(l) for l in open(log_path(cfg.decision_log_dir, "twelvedata", "EUR/USD"))]
    assert rows[0]["notified"] is True
    assert rows[0]["signal_type"] == "hourly"


def test_handle_signal_logs_decision_even_without_alert(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    asset = cfg.assets[0]
    params = cfg.params_for(asset)

    def boom(*a, **k):
        raise AssertionError("should not send a Telegram message")

    monkeypatch.setattr(main_module, "send_telegram_message", boom)

    state, alerts_log = {}, []
    calm_signal = make_signal(price_alert=False, ewma_z=0.5, robust_z=0.5)
    notified = main_module._handle_signal(
        cfg, asset, params, calm_signal, "hourly", "twelvedata:EUR/USD", state, alerts_log)

    assert notified is False
    assert alerts_log == []
    rows = [json.loads(l) for l in open(log_path(cfg.decision_log_dir, "twelvedata", "EUR/USD"))]
    assert rows[0]["price_alert"] is False
    assert rows[0]["notified"] is False


def test_handle_signal_hourly_and_daily_cooldowns_are_independent(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    asset = cfg.assets[0]
    params = cfg.params_for(asset)
    monkeypatch.setattr(main_module, "send_telegram_message", lambda *a, **k: 1)
    monkeypatch.setattr(main_module, "edit_telegram_message", lambda *a, **k: None)

    state, alerts_log = {}, []
    # Hourly alert fires and sets hourly's own cooldown...
    main_module._handle_signal(
        cfg, asset, params, make_signal(), "hourly", "twelvedata:EUR/USD", state, alerts_log)
    # ...but the daily signal, on its own independent state key, is unaffected.
    daily_notified = main_module._handle_signal(
        cfg, asset, params, make_signal(), "daily", "twelvedata:EUR/USD:daily", state, alerts_log)

    assert daily_notified is True
    assert "twelvedata:EUR/USD" in state
    assert "twelvedata:EUR/USD:daily" in state


def test_handle_signal_respects_cooldown_on_repeat(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    asset = cfg.assets[0]
    params = cfg.params_for(asset)
    monkeypatch.setattr(main_module, "send_telegram_message", lambda *a, **k: 1)
    monkeypatch.setattr(main_module, "edit_telegram_message", lambda *a, **k: None)

    state, alerts_log = {}, []
    main_module._handle_signal(
        cfg, asset, params, make_signal(), "hourly", "twelvedata:EUR/USD", state, alerts_log)
    # Same severity, straight away - within cooldown, not an escalation.
    second = main_module._handle_signal(
        cfg, asset, params, make_signal(), "hourly", "twelvedata:EUR/USD", state, alerts_log)

    assert second is False
    assert len(alerts_log) == 1


def make_end_to_end_config(tmp_path):
    return Config(
        assets=[AssetConfig(symbol="EUR/USD", source="twelvedata", label="EUR/USD")],
        telegram_bot_token="bot-token",
        telegram_chat_id="@chan",
        state_path=os.path.join(tmp_path, "state.json"),
        alerts_log_path=os.path.join(tmp_path, "alerts_log.json"),
        candle_history_dir=os.path.join(tmp_path, "candle_history"),
        decision_log_dir=os.path.join(tmp_path, "decision_log"),
        min_history=2, mad_window=5,
        price_zscore_threshold=1000.0, price_zscore_override=1000.0,
        volume_zscore_threshold=1000.0, volume_zscore_override=1000.0,
        daily_min_history=1, daily_mad_window=5,
        daily_price_zscore_threshold=1000.0, daily_price_zscore_override=1000.0,
        twelvedata_api_key="secret",
    )


def hourly_candles(n, start=0):
    return [
        Candle(open_time=start + i * 3600, open=1.0, high=1.0, low=1.0, close=1.0 + i * 0.001,
               volume=0.0, close_time=start + i * 3600 + 3600)
        for i in range(n)
    ]


def test_main_seeds_local_candle_history_and_dedups_on_rerun(tmp_path, monkeypatch):
    cfg = make_end_to_end_config(tmp_path)
    monkeypatch.setattr(main_module, "load_config", lambda: cfg)

    first_batch = hourly_candles(5)
    monkeypatch.setattr(main_module, "fetch_candles", lambda asset, cfg, session=None: first_batch)
    assert main_module.main() == 0

    history_path = store_path(cfg.candle_history_dir, "twelvedata", "EUR/USD")
    assert [c.open_time for c in load_candles(history_path)] == [c.open_time for c in first_batch]

    state = load_state(cfg.state_path)
    assert state["twelvedata:EUR/USD:last_candle"]["open_time"] == first_batch[-1].open_time

    # Next run re-fetches an overlapping window plus one genuinely new hour -
    # only that one new candle should be appended, never a duplicate.
    second_batch = hourly_candles(6)
    monkeypatch.setattr(main_module, "fetch_candles", lambda asset, cfg, session=None: second_batch)
    assert main_module.main() == 0

    stored = load_candles(history_path)
    assert [c.open_time for c in stored] == [c.open_time for c in second_batch]
    assert len(stored) == 6  # not 5 + 6 = 11


def test_main_runs_daily_signal_from_seeded_history(tmp_path, monkeypatch):
    """The daily signal must work purely from the local store - no extra
    network fetch for it (see module docstring)."""
    cfg = make_end_to_end_config(tmp_path)
    monkeypatch.setattr(main_module, "load_config", lambda: cfg)

    # Pre-seed a few full days of quiet local history directly (as if a
    # backtest run had already populated it), then the day before "today".
    history_path = store_path(cfg.candle_history_dir, "twelvedata", "EUR/USD")
    from price_monitor.candle_store import append_candles
    day = 86400
    seeded = [
        Candle(open_time=i * day, open=1.0, high=1.0, low=1.0, close=1.0,
               volume=0.0, close_time=i * day + day)
        for i in range(3)
    ]
    append_candles(history_path, seeded)

    # New fetch lands on a later, non-overlapping day so it becomes its own
    # additional daily bucket once resampled.
    new_hours = hourly_candles(3, start=3 * day)
    monkeypatch.setattr(main_module, "fetch_candles", lambda asset, cfg, session=None: new_hours)
    assert main_module.main() == 0

    rows = [json.loads(l) for l in open(log_path(cfg.decision_log_dir, "twelvedata", "EUR/USD"))]
    signal_types = {r["signal_type"] for r in rows}
    assert "daily" in signal_types


def test_muted_alerts_are_not_sent_but_still_logged(tmp_path, monkeypatch):
    # Muted while MEALS takes over: the run proceeds as usual, decisions are
    # written, but the per-asset Telegram message does not go out.
    cfg = make_config(tmp_path)
    cfg.alerts_muted = True
    asset = cfg.assets[0]

    def boom(*a, **k):
        raise AssertionError("a muted alert must not go out to Telegram")

    monkeypatch.setattr(main_module, "send_telegram_message", boom)

    state, alerts_log = {}, []
    notified = main_module._handle_signal(
        cfg, asset, cfg.params_for(asset), make_signal(), "hourly",
        "twelvedata:EUR/USD", state, alerts_log)

    assert notified is False
    assert alerts_log == []
    rows = [json.loads(l) for l in open(log_path(cfg.decision_log_dir, "twelvedata", "EUR/USD"))]
    assert rows[0]["price_alert"] is True, "the signal happened - the journal shows it"
    assert rows[0]["notified"] is False


def test_muting_does_not_burn_the_cooldown(tmp_path, monkeypatch):
    # The state must stay as though there had been no signal: otherwise, once
    # the mute is lifted, the first genuine move would run into a pause
    # accumulated during the silence.
    cfg = make_config(tmp_path)
    cfg.alerts_muted = True
    asset = cfg.assets[0]
    monkeypatch.setattr(main_module, "send_telegram_message",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no send")))

    state, alerts_log = {}, []
    main_module._handle_signal(cfg, asset, cfg.params_for(asset), make_signal(),
                               "hourly", "twelvedata:EUR/USD", state, alerts_log)
    assert state == {}

    cfg.alerts_muted = False
    sent = []
    monkeypatch.setattr(main_module, "send_telegram_message",
                        lambda *a, **k: sent.append(a) or 123)
    monkeypatch.setattr(main_module, "edit_telegram_message", lambda *a, **k: None)
    assert main_module._handle_signal(
        cfg, asset, cfg.params_for(asset), make_signal(), "hourly",
        "twelvedata:EUR/USD", state, alerts_log) is True
    assert len(sent) == 1


def test_muting_is_off_by_default():
    assert make_config("/tmp").alerts_muted is False

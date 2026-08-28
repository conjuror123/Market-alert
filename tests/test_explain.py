import os

from price_monitor import explain
from price_monitor.alerts_log import load_alerts_log, record_sent_alert, save_alerts_log
from price_monitor.config import AssetConfig, Config
from price_monitor.llm import LLMError
from price_monitor.news import NewsError
from price_monitor.notifier import TelegramError


def make_config(tmp_path, llm_api_key="secret-key"):
    return Config(
        assets=[AssetConfig(symbol="ETH-USD", source="coinbase", label="Ethereum", news_query="Ethereum")],
        telegram_bot_token="bot-token",
        telegram_chat_id="@chan",
        alerts_log_path=os.path.join(tmp_path, "alerts_log.json"),
        llm_api_key=llm_api_key,
    )


def seed_pending_entry(path):
    entries = []
    record_sent_alert(
        entries,
        chat_id="@chan",
        message_id=42,
        symbol="Ethereum",
        message_text="Ethereum - необычное движение рынка",
        last_close=2473.66,
        last_return_pct=-1.45,
        ewma_z=-3.60,
        robust_z=-3.61,
        volume_z=0.70,
    )
    save_alerts_log(path, entries)


def test_missing_api_key_returns_error_without_any_calls(tmp_path, monkeypatch, capsys):
    cfg = make_config(tmp_path, llm_api_key="")
    monkeypatch.setattr(explain, "load_config", lambda: cfg)

    def boom(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(explain, "fetch_news", boom)
    monkeypatch.setattr(explain, "chat_completion", boom)
    monkeypatch.setattr(explain, "edit_telegram_message", boom)

    assert explain.main() == 1
    assert "LLM_API_KEY" in capsys.readouterr().err


def test_no_pending_entries_returns_0_and_does_nothing(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)

    def boom(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(explain, "chat_completion", boom)
    monkeypatch.setattr(explain, "edit_telegram_message", boom)

    assert explain.main() == 0


def test_full_flow_explains_and_edits_message(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [
        {"title": "Ethereum falls on macro selloff", "source": "Example", "published": None, "link": ""}
    ])
    monkeypatch.setattr(explain, "chat_completion", lambda **kwargs: "Падение связано с общей распродажей на рынке.")

    edits = []
    monkeypatch.setattr(
        explain, "edit_telegram_message",
        lambda token, chat_id, message_id, text: edits.append((token, chat_id, message_id, text)),
    )

    assert explain.main() == 0
    assert len(edits) == 1
    token, chat_id, message_id, text = edits[0]
    assert (chat_id, message_id) == ("@chan", 42)
    assert "Ethereum - необычное движение рынка" in text
    assert "Падение связано с общей распродажей на рынке." in text

    saved = load_alerts_log(cfg.alerts_log_path)
    assert saved[0]["explained"] is True
    assert saved[0]["explanation"] == "Падение связано с общей распродажей на рынке."


def test_news_fetch_failure_still_asks_llm(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)

    def failing_fetch(query, limit=6):
        raise NewsError("boom")

    monkeypatch.setattr(explain, "fetch_news", failing_fetch)
    monkeypatch.setattr(explain, "chat_completion", lambda **kwargs: "Явной причины в новостях не нашлось.")
    monkeypatch.setattr(explain, "edit_telegram_message", lambda *a, **k: None)

    assert explain.main() == 0
    saved = load_alerts_log(cfg.alerts_log_path)
    assert saved[0]["explained"] is True


def test_llm_error_leaves_entry_pending_for_retry(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [])

    def failing_llm(**kwargs):
        raise LLMError("rate limited")

    monkeypatch.setattr(explain, "chat_completion", failing_llm)
    monkeypatch.setattr(explain, "edit_telegram_message", lambda *a, **k: None)

    assert explain.main() == 1
    saved = load_alerts_log(cfg.alerts_log_path)
    assert saved[0]["explained"] is False


def test_telegram_edit_error_leaves_entry_pending_for_retry(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [])
    monkeypatch.setattr(explain, "chat_completion", lambda **kwargs: "explanation")

    def failing_edit(*args, **kwargs):
        raise TelegramError("edit failed")

    monkeypatch.setattr(explain, "edit_telegram_message", failing_edit)

    assert explain.main() == 1
    saved = load_alerts_log(cfg.alerts_log_path)
    assert saved[0]["explained"] is False


def test_explain_entry_passes_time_based_model_choice(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    cfg.llm_model_peak = "flash-x"
    cfg.llm_model_offpeak = "pro-x"
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [])
    monkeypatch.setattr(explain, "select_model", lambda peak, offpeak, now=None: f"{peak}/{offpeak}")

    captured = {}

    def fake_chat_completion(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(explain, "chat_completion", fake_chat_completion)

    entry = {
        "symbol": "Ethereum", "last_return_pct": -1.0, "last_close": 100.0,
        "sent_at": "2026-01-01T00:00:00+00:00",
    }
    explain.explain_entry(cfg, entry, "Ethereum")

    assert captured["model"] == "flash-x/pro-x"


def test_html_special_characters_in_explanation_are_escaped(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [])
    monkeypatch.setattr(explain, "chat_completion", lambda **kwargs: "Цена упала <5% при объёме > нормы & без явной причины")

    edits = []
    monkeypatch.setattr(
        explain, "edit_telegram_message",
        lambda token, chat_id, message_id, text: edits.append(text),
    )

    explain.main()
    assert "<5%" not in edits[0]
    assert "&lt;5%" in edits[0]
    assert "&gt;" in edits[0]
    assert "&amp;" in edits[0]

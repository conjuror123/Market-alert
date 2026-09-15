"""Operational Telegram: name who went dark, and keep it off the product channel."""
from tremor.backfill import format_provider_failure, send_ops_alert


def test_a_dead_instrument_is_named_with_its_provider():
    text = format_provider_failure(
        [("twelvedata:SPY", "yahoo", "HTTP 500")])
    assert "twelvedata:SPY (yahoo): HTTP 500" in text
    assert "went dark" in text


def test_tiingo_bucket_pressure_is_in_the_same_message():
    text = format_provider_failure(
        [], tiingo_gone=True, tiingo_skipped=12,
        tiingo_remaining="0", tiingo_trip="twelvedata:XLK")
    assert "remaining headroom 0" in text
    assert "12 remaining Tiingo" in text
    assert "twelvedata:XLK" in text
    assert "not switched automatically" in text


def test_nothing_to_report_is_an_empty_string():
    assert format_provider_failure([]) == ""


def test_ops_alert_uses_the_health_chat_not_the_product_one(monkeypatch):
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@public")
    monkeypatch.setenv("TELEGRAM_HEALTH_CHAT_ID", "12345")

    def fake_send(token, chat, text):
        sent.append((token, chat, text))
        return 1

    monkeypatch.setattr("tremor.backfill.send_telegram_message", fake_send)
    send_ops_alert("hello")
    assert sent == [("tok", "12345", "hello")]


def test_ops_alert_falls_back_to_the_product_chat(monkeypatch):
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@public")
    monkeypatch.delenv("TELEGRAM_HEALTH_CHAT_ID", raising=False)
    monkeypatch.setattr(
        "tremor.backfill.send_telegram_message",
        lambda token, chat, text: sent.append(chat) or 1)
    send_ops_alert("hello")
    assert sent == ["@public"]

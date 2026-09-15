import pytest
import requests

from price_monitor import notifier
from price_monitor.notifier import TelegramError, edit_telegram_message, send_telegram_message


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_send_returns_message_id(monkeypatch):
    def fake_post(url, json, timeout):
        return FakeResponse(200, {"ok": True, "result": {"message_id": 99}})

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    assert send_telegram_message("token", "@chan", "hello") == 99


def test_missing_credentials_raise_without_request(monkeypatch):
    def fake_post(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    with pytest.raises(TelegramError):
        send_telegram_message("", "@chan", "hello")


def test_non_200_status_raises(monkeypatch):
    def fake_post(url, json, timeout):
        return FakeResponse(400, {"ok": False, "description": "Bad Request"})

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    with pytest.raises(TelegramError):
        send_telegram_message("token", "@chan", "hello")


def test_edit_calls_edit_message_text_endpoint(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json))
        return FakeResponse(200, {"ok": True, "result": {}})

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    edit_telegram_message("token", "@chan", 99, "updated text")

    assert len(calls) == 1
    url, payload = calls[0]
    assert url.endswith("/bottoken/editMessageText")
    assert payload == {
        "chat_id": "@chan",
        "message_id": 99,
        "text": "updated text",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


def test_edit_missing_credentials_raise_without_request(monkeypatch):
    def fake_post(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    with pytest.raises(TelegramError):
        edit_telegram_message("", "@chan", 99, "text")


def test_a_request_error_does_not_contain_the_bot_token(monkeypatch):
    def fake_post(*args, **kwargs):
        raise requests.ConnectionError(
            "HTTPSConnectionPool(host='api.telegram.org', port=443): "
            "Failed to resolve 'api.telegram.org/bot123456:secret-token/sendMessage'")

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    with pytest.raises(TelegramError, match="Telegram request failed") as caught:
        send_telegram_message("123456:secret-token", "@chan", "hello")
    assert "secret-token" not in str(caught.value)
    assert "bot123456:secret-token" not in str(caught.value)


def test_redact_secrets_strips_token_apikey_and_authorization():
    text = notifier.redact_secrets(
        "https://api.telegram.org/bot999:AAA-bbb/sendMessage "
        "https://api.twelvedata.com/time_series?apikey=sk-live&symbol=SPY "
        "Authorization: Bearer tok")
    assert "bot999:AAA-bbb" not in text
    assert "sk-live" not in text
    assert "Bearer tok" not in text
    assert "bot<redacted>" in text
    assert "apikey=<redacted>" in text
    assert "Authorization: <redacted>" in text


def test_edit_non_200_status_raises(monkeypatch):
    def fake_post(url, json, timeout):
        return FakeResponse(400, {"ok": False, "description": "Bad Request"})

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    with pytest.raises(TelegramError):
        edit_telegram_message("token", "@chan", 99, "text")

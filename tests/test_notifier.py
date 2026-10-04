import pytest
import requests

from price_monitor import notifier
from price_monitor.notifier import (
    TelegramError, edit_telegram_message, fetch_telegram_updates,
    send_telegram_message,
)


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


def test_an_unmodified_edit_is_not_an_error(monkeypatch):
    def fake_post(url, json, timeout):
        return FakeResponse(400, {
            "ok": False,
            "description": "Bad Request: message is not modified",
        })

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    edit_telegram_message("token", "@chan", 99, "same text")


def test_fetch_updates_passes_the_offset(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params))
        return FakeResponse(200, {"ok": True, "result": [{"update_id": 4}]})

    monkeypatch.setattr(notifier.requests, "get", fake_get)
    assert fetch_telegram_updates("token", offset=5) == [{"update_id": 4}]
    assert calls[0][0].endswith("/bottoken/getUpdates")
    assert calls[0][1]["offset"] == 5


def test_a_429_is_waited_out_and_the_message_goes(monkeypatch):
    monkeypatch.setattr(notifier, "_waited", [0.0])
    answers = [FakeResponse(429, {"ok": False, "parameters": {"retry_after": 3}}),
               FakeResponse(200, {"ok": True, "result": {"message_id": 7}})]
    slept = []
    monkeypatch.setattr(notifier.requests, "post", lambda url, json, timeout: answers.pop(0))
    monkeypatch.setattr(notifier.time, "sleep", slept.append)
    assert send_telegram_message("token", "@chan", "hello") == 7
    assert slept == [3.5]


def test_a_429_past_the_cap_is_an_error_not_a_long_wait(monkeypatch):
    monkeypatch.setattr(notifier.requests, "post", lambda url, json, timeout: FakeResponse(
        429, {"ok": False, "parameters": {"retry_after": 600}}))
    monkeypatch.setattr(notifier.time, "sleep",
                        lambda s: (_ for _ in ()).throw(AssertionError("waited")))
    with pytest.raises(TelegramError):
        send_telegram_message("token", "@chan", "hello")


def test_429s_are_retried_a_bounded_number_of_times(monkeypatch):
    monkeypatch.setattr(notifier, "_waited", [0.0])
    calls = []

    def fake_post(url, json, timeout):
        calls.append(1)
        return FakeResponse(429, {"ok": False, "parameters": {"retry_after": 1}})

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)
    with pytest.raises(TelegramError):
        edit_telegram_message("token", "@chan", 5, "x")
    assert len(calls) == notifier.RETRIES_ON_429 + 1


def test_a_silent_send_asks_for_no_sound(monkeypatch):
    seen = []

    def fake_post(url, json, timeout):
        seen.append(json)
        return FakeResponse(200, {"ok": True, "result": {"message_id": 1}})

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    send_telegram_message("token", "@chan", "a")
    send_telegram_message("token", "@chan", "b", silent=True)
    assert "disable_notification" not in seen[0]
    assert seen[1]["disable_notification"] is True


def test_the_waiting_of_one_run_is_bounded(monkeypatch):
    monkeypatch.setattr(notifier, "_waited", [0.0])
    monkeypatch.setattr(notifier.requests, "post", lambda url, json, timeout: FakeResponse(
        429, {"ok": False, "parameters": {"retry_after": 50}}))
    slept = []
    monkeypatch.setattr(notifier.time, "sleep", slept.append)
    for _ in range(5):
        with pytest.raises(TelegramError):
            send_telegram_message("token", "@chan", "hello")
    assert sum(slept) <= notifier.WAIT_BUDGET_SECONDS
    assert len(slept) == 3                    # 3 x 50.5 s, then no more waiting

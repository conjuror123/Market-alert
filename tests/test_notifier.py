import pytest

from price_monitor import notifier
from price_monitor.notifier import TelegramError, send_telegram_message


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

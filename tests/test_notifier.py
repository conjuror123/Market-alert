import pytest
import requests

from price_monitor import notifier
from price_monitor.notifier import (
    TelegramError, edit_telegram_message, send_telegram_message,
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
    with pytest.raises(TelegramError, match="Telegram") as caught:
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


def test_a_fred_key_is_redacted_too():
    # FRED spells it api_key=; a FRED error in the health chat carried it whole.
    text = notifier.redact_secrets(
        "https://api.stlouisfed.org/fred/series/observations?series_id=VIXCLS"
        "&api_key=abc123&file_type=json")
    assert "abc123" not in text
    assert "api_key=<redacted>&file_type=json" in text


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


# What became of a request: only one that never reached Telegram is "not sent";
# any other failure may have been carried out (2026-11-03: two sends timed out
# and were published 33 minutes later).

def _raising(exc):
    def fake_post(*args, **kwargs):
        raise exc
    return fake_post


def test_a_read_timeout_is_unsure_and_a_connect_timeout_is_not(monkeypatch):
    monkeypatch.setattr(notifier.requests, "post", _raising(requests.ReadTimeout("slow")))
    with pytest.raises(notifier.TelegramUnsure):
        send_telegram_message("token", "@chan", "hello")
    monkeypatch.setattr(notifier.requests, "post", _raising(requests.ConnectTimeout("no")))
    with pytest.raises(TelegramError) as caught:
        send_telegram_message("token", "@chan", "hello")
    assert not isinstance(caught.value, notifier.TelegramUnsure)


def test_a_connection_never_made_is_not_sent_one_dropped_is_unsure(monkeypatch):
    from urllib3.exceptions import MaxRetryError, NewConnectionError
    never = requests.ConnectionError(MaxRetryError(None, "/", NewConnectionError(None, "refused")))
    monkeypatch.setattr(notifier.requests, "post", _raising(never))
    with pytest.raises(TelegramError) as caught:
        send_telegram_message("token", "@chan", "hello")
    assert not isinstance(caught.value, notifier.TelegramUnsure)
    monkeypatch.setattr(notifier.requests, "post",
                        _raising(requests.ConnectionError("Connection aborted.")))
    with pytest.raises(notifier.TelegramUnsure):
        send_telegram_message("token", "@chan", "hello")


def test_telegrams_own_trouble_is_unsure_a_refusal_is_not(monkeypatch):
    monkeypatch.setattr(notifier.requests, "post",
                        lambda url, json, timeout: FakeResponse(502, {"ok": False}))
    with pytest.raises(notifier.TelegramUnsure):
        send_telegram_message("token", "@chan", "hello")
    with pytest.raises(notifier.TelegramUnsure):
        edit_telegram_message("token", "@chan", 5, "hello")
    monkeypatch.setattr(notifier.requests, "post",
                        lambda url, json, timeout: FakeResponse(400, {"ok": False}))
    with pytest.raises(TelegramError) as caught:
        send_telegram_message("token", "@chan", "hello")
    assert not isinstance(caught.value, notifier.TelegramUnsure)


def test_a_sent_message_whose_answer_cannot_be_read_is_unsure(monkeypatch):
    monkeypatch.setattr(notifier.requests, "post",
                        lambda url, json, timeout: FakeResponse(200, {"ok": True}))
    with pytest.raises(notifier.TelegramUnsure):
        send_telegram_message("token", "@chan", "hello")


def test_the_channel_waits_a_minute_and_health_a_short_while(monkeypatch):
    seen = []
    monkeypatch.setattr(notifier.requests, "post", lambda url, json, timeout: seen.append(
        timeout) or FakeResponse(200, {"ok": True, "result": {"message_id": 1}}))
    send_telegram_message("token", "@chan", "hello")
    notifier.send_health("x", token="token", chat="@health")
    assert seen == [notifier.CHANNEL_TIMEOUT, notifier.SHORT_TIMEOUT]
    assert notifier.CHANNEL_TIMEOUT[1] >= 60


def test_a_delete_that_got_no_answer_raises_rather_than_reporting_a_refusal(monkeypatch):
    monkeypatch.setattr(notifier.requests, "post", _raising(requests.ReadTimeout("slow")))
    with pytest.raises(notifier.TelegramUnsure):
        notifier.delete_telegram_message("token", "@chan", 5)


def test_forwarding_reads_a_message_and_says_when_there_is_none(monkeypatch):
    asked = []

    def answer(url, json, timeout):
        asked.append((url.rsplit("/", 1)[1], json))
        if json["message_id"] == 137:
            return FakeResponse(200, {"ok": True, "result": {
                "message_id": 9, "text": "🟨 TTF · -7.11%",
                "forward_origin": {"type": "channel", "message_id": 137}}})
        if json["message_id"] == 135:
            return FakeResponse(400, {"ok": False,
                                      "description": "Bad Request: message can't be forwarded"})
        return FakeResponse(400, {"ok": False,
                                  "description": "Bad Request: message to forward not found"})

    monkeypatch.setattr(notifier.requests, "post", answer)
    got = notifier.forward_telegram_message("token", "@chan", 137, "@health")
    assert got == {"state": "found", "text": "🟨 TTF · -7.11%", "copy": 9}
    assert asked[0] == ("forwardMessage", {"chat_id": "@health", "from_chat_id": "@chan",
                                           "message_id": 137, "disable_notification": True})
    assert notifier.forward_telegram_message("token", "@chan", 135, "@health") == {
        "state": "unforwardable"}
    assert notifier.forward_telegram_message("token", "@chan", 140, "@health") == {
        "state": "missing"}

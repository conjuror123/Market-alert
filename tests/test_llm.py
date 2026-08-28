import pytest

from price_monitor import llm
from price_monitor.llm import LLMError, chat_completion


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_returns_message_content(monkeypatch):
    def fake_post(url, headers, json, timeout):
        assert url == "https://api.deepseek.com/chat/completions"
        assert headers["Authorization"] == "Bearer secret-key"
        assert json["model"] == "deepseek-chat"
        return FakeResponse(200, {"choices": [{"message": {"content": "  explanation text  "}}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    result = chat_completion(
        base_url="https://api.deepseek.com",
        api_key="secret-key",
        model="deepseek-chat",
        messages=[{"role": "user", "content": "why?"}],
    )
    assert result == "explanation text"


def test_strips_trailing_slash_from_base_url(monkeypatch):
    def fake_post(url, headers, json, timeout):
        assert url == "https://api.example.com/chat/completions"
        return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    chat_completion(base_url="https://api.example.com/", api_key="k", model="m", messages=[])


def test_missing_api_key_raises_without_request(monkeypatch):
    def fake_post(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(llm.requests, "post", fake_post)
    with pytest.raises(LLMError):
        chat_completion(base_url="https://api.deepseek.com", api_key="", model="m", messages=[])


def test_non_200_status_raises(monkeypatch):
    def fake_post(url, headers, json, timeout):
        return FakeResponse(500, {"error": "boom"})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    with pytest.raises(LLMError):
        chat_completion(base_url="https://api.deepseek.com", api_key="k", model="m", messages=[])


def test_unexpected_response_shape_raises(monkeypatch):
    def fake_post(url, headers, json, timeout):
        return FakeResponse(200, {"unexpected": "shape"})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    with pytest.raises(LLMError):
        chat_completion(base_url="https://api.deepseek.com", api_key="k", model="m", messages=[])

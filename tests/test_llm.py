from datetime import datetime, timezone

import pytest

from price_monitor import llm
from price_monitor.llm import LLMError, chat_completion, is_peak_hour, select_model


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


# Wednesday 2026-09-02, 02:00 UTC - inside the 01:00-04:00 peak window.
_PEAK_WEEKDAY = datetime(2026, 9, 2, 2, 0, tzinfo=timezone.utc)
# Same day, 05:00 UTC - the gap between the two peak windows.
_OFFPEAK_WEEKDAY_GAP = datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc)
# Same day, 8:00 UTC - inside the 06:00-10:00 peak window.
_PEAK_WEEKDAY_2 = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)
# Same day, 20:00 UTC - well outside both peak windows.
_OFFPEAK_WEEKDAY_NIGHT = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
# Saturday 2026-09-05, 02:00 UTC - would be peak hours, but it's a weekend.
_WEEKEND = datetime(2026, 9, 5, 2, 0, tzinfo=timezone.utc)
# Exactly the boundary where the first peak window ends (04:00 - end exclusive).
_PEAK_BOUNDARY_END = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("now", [_PEAK_WEEKDAY, _PEAK_WEEKDAY_2])
def test_is_peak_hour_true_inside_peak_windows(now):
    assert is_peak_hour(now) is True


@pytest.mark.parametrize("now", [_OFFPEAK_WEEKDAY_GAP, _OFFPEAK_WEEKDAY_NIGHT, _WEEKEND, _PEAK_BOUNDARY_END])
def test_is_peak_hour_false_outside_peak_windows(now):
    assert is_peak_hour(now) is False


def test_select_model_picks_peak_model_during_peak():
    assert select_model("flash", "pro", now=_PEAK_WEEKDAY) == "flash"


def test_select_model_picks_offpeak_model_outside_peak():
    assert select_model("flash", "pro", now=_OFFPEAK_WEEKDAY_NIGHT) == "pro"

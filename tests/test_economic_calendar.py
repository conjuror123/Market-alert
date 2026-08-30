import json

import pytest
import requests

from price_monitor import economic_calendar
from price_monitor.economic_calendar import CalendarError, fetch_calendar, merge_events, parse_event_time


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


SAMPLE_RAW = [
    {
        "title": "Non-Farm Payrolls",
        "country": "USD",
        "date": "2026-09-04T08:30:00-04:00",
        "impact": "High",
        "forecast": "180K",
        "previous": "150K",
    },
    {
        "title": "Bank Holiday",
        "country": "All",
        "date": "2026-08-31T00:00:00-04:00",
        "impact": "Holiday",
        "forecast": "",
        "previous": "",
    },
]


def test_fetch_calendar_parses_known_fields(monkeypatch):
    def fake_get(url, timeout, headers):
        assert url == economic_calendar.CALENDAR_URL
        return FakeResponse(200, SAMPLE_RAW)

    monkeypatch.setattr(economic_calendar.requests, "get", fake_get)
    events = fetch_calendar()

    assert len(events) == 2
    assert events[0] == {
        "title": "Non-Farm Payrolls", "country": "USD", "date": "2026-09-04T08:30:00-04:00",
        "impact": "High", "forecast": "180K", "previous": "150K",
    }


def test_fetch_calendar_skips_malformed_entries(monkeypatch):
    def fake_get(url, timeout, headers):
        return FakeResponse(200, [{"title": "missing fields"}])

    monkeypatch.setattr(economic_calendar.requests, "get", fake_get)
    assert fetch_calendar() == []


def test_fetch_calendar_raises_on_http_error(monkeypatch):
    def fake_get(url, timeout, headers):
        return FakeResponse(500, [])

    monkeypatch.setattr(economic_calendar.requests, "get", fake_get)
    with pytest.raises(CalendarError):
        fetch_calendar()


def test_fetch_calendar_raises_on_invalid_json(monkeypatch):
    class BrokenJsonResponse(FakeResponse):
        def json(self):
            raise ValueError("not json")

    def fake_get(url, timeout, headers):
        return BrokenJsonResponse(200, None)

    monkeypatch.setattr(economic_calendar.requests, "get", fake_get)
    with pytest.raises(CalendarError):
        fetch_calendar()


def test_fetch_calendar_uses_the_given_session(monkeypatch):
    calls = []

    class FakeSession:
        def get(self, url, timeout, headers):
            calls.append(url)
            return FakeResponse(200, SAMPLE_RAW)

    fetch_calendar(session=FakeSession())
    assert calls == [economic_calendar.CALENDAR_URL]


def test_parse_event_time_converts_to_utc():
    parsed = parse_event_time("2026-09-04T08:30:00-04:00")
    assert parsed.utcoffset().total_seconds() == 0
    assert parsed.hour == 12


def test_merge_events_is_idempotent_and_deduplicates(tmp_path):
    path = str(tmp_path / "calendar.ndjson")

    added_first = merge_events(path, SAMPLE_RAW)
    assert added_first == 2

    added_again = merge_events(path, SAMPLE_RAW)
    assert added_again == 0
    assert len(economic_calendar.load_events(path)) == 2


def test_merge_events_adds_only_new_rows(tmp_path):
    path = str(tmp_path / "calendar.ndjson")
    merge_events(path, [SAMPLE_RAW[0]])

    added = merge_events(path, SAMPLE_RAW)
    assert added == 1
    assert len(economic_calendar.load_events(path)) == 2


def test_load_events_returns_empty_list_when_file_missing(tmp_path):
    assert economic_calendar.load_events(str(tmp_path / "nope.ndjson")) == []


def test_merge_events_persists_valid_json_lines(tmp_path):
    path = str(tmp_path / "calendar.ndjson")
    merge_events(path, SAMPLE_RAW)
    with open(path, "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 2

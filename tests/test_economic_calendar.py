import json
import time
from datetime import datetime, timezone

import pytest
import requests

from price_monitor import economic_calendar
from price_monitor.economic_calendar import (
    CalendarError,
    fetch_calendar,
    fetch_fmp_history,
    fetch_fmp_range,
    merge_events,
    parse_event_time,
)


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


FMP_RAW_EVENT = {
    "event": "Non-Farm Payrolls",
    "date": "2023-06-02 12:30:00",
    "country": "US",
    "impact": "High",
    "estimate": "190K",
    "previous": "253K",
}


def test_fmp_date_to_iso_utc_parses_datetime_and_date_only():
    assert economic_calendar._fmp_date_to_iso_utc("2023-06-02 12:30:00") == \
        datetime(2023, 6, 2, 12, 30, tzinfo=timezone.utc).isoformat()
    assert economic_calendar._fmp_date_to_iso_utc("2023-06-02") == \
        datetime(2023, 6, 2, tzinfo=timezone.utc).isoformat()


def test_fmp_date_to_iso_utc_returns_none_for_unparseable_date():
    assert economic_calendar._fmp_date_to_iso_utc("not a date") is None


def test_normalize_fmp_event_maps_known_fields():
    normalized = economic_calendar._normalize_fmp_event(FMP_RAW_EVENT)
    assert normalized == {
        "title": "Non-Farm Payrolls", "country": "US",
        "date": datetime(2023, 6, 2, 12, 30, tzinfo=timezone.utc).isoformat(),
        "impact": "High", "forecast": "190K", "previous": "253K",
    }


def test_normalize_fmp_event_accepts_alternate_field_names():
    # In case the "stable" endpoint ever renames "event"/"estimate" to
    # "title"/"forecast" - tolerate either without erroring.
    item = {"title": "CPI m/m", "date": "2023-06-02 08:30:00", "country": "US",
            "impact": "Medium", "forecast": "0.3%", "previous": "0.4%"}
    normalized = economic_calendar._normalize_fmp_event(item)
    assert normalized["title"] == "CPI m/m"
    assert normalized["forecast"] == "0.3%"


def test_normalize_fmp_event_returns_none_when_title_or_date_missing():
    assert economic_calendar._normalize_fmp_event({"date": "2023-06-02 08:30:00"}) is None
    assert economic_calendar._normalize_fmp_event({"event": "CPI m/m"}) is None
    assert economic_calendar._normalize_fmp_event({"event": "CPI m/m", "date": "garbage"}) is None


def test_fetch_fmp_range_normalizes_events(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params))
        return FakeResponse(200, [FMP_RAW_EVENT])

    monkeypatch.setattr(economic_calendar.requests, "get", fake_get)
    events = fetch_fmp_range("key123", "2023-06-01", "2023-06-30")

    assert len(events) == 1
    assert events[0]["title"] == "Non-Farm Payrolls"
    assert calls == [(economic_calendar.FMP_CALENDAR_URL,
                       {"from": "2023-06-01", "to": "2023-06-30", "apikey": "key123"})]


def test_fetch_fmp_range_raises_on_error_object_response(monkeypatch):
    def fake_get(url, params, timeout):
        return FakeResponse(200, {"Error Message": "Invalid API KEY."})

    monkeypatch.setattr(economic_calendar.requests, "get", fake_get)
    with pytest.raises(CalendarError):
        fetch_fmp_range("bad-key", "2023-06-01", "2023-06-30")


def test_fetch_fmp_range_raises_on_http_error(monkeypatch):
    def fake_get(url, params, timeout):
        return FakeResponse(429, [])

    monkeypatch.setattr(economic_calendar.requests, "get", fake_get)
    with pytest.raises(CalendarError):
        fetch_fmp_range("key123", "2023-06-01", "2023-06-30")


def test_fetch_fmp_history_chunks_into_90_day_windows(monkeypatch):
    windows = []

    def fake_fetch_fmp_range(api_key, from_date, to_date, session=None):
        windows.append((from_date, to_date))
        return []

    monkeypatch.setattr(economic_calendar, "fetch_fmp_range", fake_fetch_fmp_range)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    since = datetime(2023, 1, 1, tzinfo=timezone.utc)
    until = datetime(2023, 5, 1, tzinfo=timezone.utc)  # 120 days -> 2 windows
    fetch_fmp_history("key123", since, until, delay=0.0)

    assert windows == [("2023-01-01", "2023-04-01"), ("2023-04-02", "2023-05-01")]


def test_fetch_fmp_history_continues_after_a_failed_window(monkeypatch):
    calls = []

    def flaky_fetch(api_key, from_date, to_date, session=None):
        calls.append(from_date)
        if len(calls) == 1:
            raise CalendarError("boom")
        return [dict(FMP_RAW_EVENT)]

    monkeypatch.setattr(economic_calendar, "fetch_fmp_range", flaky_fetch)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    since = datetime(2023, 1, 1, tzinfo=timezone.utc)
    until = datetime(2023, 5, 1, tzinfo=timezone.utc)
    events = fetch_fmp_history("key123", since, until, delay=0.0)

    assert len(calls) == 2
    assert len(events) == 1


def test_fetch_fmp_history_sleeps_between_windows_but_not_after_the_last(monkeypatch):
    sleeps = []
    monkeypatch.setattr(economic_calendar, "fetch_fmp_range", lambda *a, **k: [])
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    since = datetime(2023, 1, 1, tzinfo=timezone.utc)
    until = datetime(2023, 5, 1, tzinfo=timezone.utc)  # 2 windows
    fetch_fmp_history("key123", since, until, delay=2.5)

    assert sleeps == [2.5]

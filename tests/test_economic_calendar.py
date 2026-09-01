import json
from datetime import datetime, timezone

import pytest
import requests

from price_monitor import economic_calendar
from price_monitor.economic_calendar import (
    CalendarError,
    events_in_window,
    fetch_calendar,
    load_events,
    merge_events,
    parse_event_time,
    store_path,
)


class FakeResponse:
    def __init__(self, status_code, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text

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
        "actual": "190K",
    },
    {
        "title": "Bank Holiday",
        "country": "All",
        "date": "2026-08-31T00:00:00-04:00",
        "impact": "Holiday",
        "forecast": "",
        "previous": "",
        "actual": "",
    },
]


def test_fetch_calendar_parses_known_fields(monkeypatch):
    def fake_get(url, timeout, headers):
        assert url == economic_calendar.CALENDAR_URL
        return FakeResponse(200, SAMPLE_RAW)

    monkeypatch.setattr(economic_calendar.requests, "get", fake_get)
    events = fetch_calendar()

    assert len(events) == 2
    # Дата приводится к UTC: фид отдаёт фиксированное смещение -04:00, а
    # обе исторические ветки пишут в UTC, и хранить один момент в двух
    # упаковках значило бы завести сдвинутую копию.
    assert events[0] == {
        "title": "Non-Farm Payrolls", "country": "USD", "date": "2026-09-04T12:30:00+00:00",
        "impact": "High", "forecast": "180K", "previous": "150K", "actual": "190K",
    }


def test_fetch_calendar_normalizes_holiday_impact_to_low(monkeypatch):
    def fake_get(url, timeout, headers):
        return FakeResponse(200, SAMPLE_RAW)

    monkeypatch.setattr(economic_calendar.requests, "get", fake_get)
    events = fetch_calendar()

    holiday_event = next(e for e in events if e["title"] == "Bank Holiday")
    assert holiday_event["impact"] == "Low"


def test_normalize_impact_folds_holiday_and_non_economic_into_low():
    assert economic_calendar._normalize_impact("Holiday") == "Low"
    assert economic_calendar._normalize_impact("Non-economic") == "Low"
    assert economic_calendar._normalize_impact("Low") == "Low"
    assert economic_calendar._normalize_impact("Medium") == "Medium"
    assert economic_calendar._normalize_impact("High") == "High"


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


def test_events_in_window_keeps_only_events_inside_the_range_inclusive():
    events = [
        {"title": "before", "date": datetime(2024, 1, 1, 11, 59, tzinfo=timezone.utc).isoformat()},
        {"title": "lower bound", "date": datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc).isoformat()},
        {"title": "inside", "date": datetime(2024, 1, 1, 13, 0, tzinfo=timezone.utc).isoformat()},
        {"title": "upper bound", "date": datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc).isoformat()},
        {"title": "after", "date": datetime(2024, 1, 1, 14, 1, tzinfo=timezone.utc).isoformat()},
    ]
    matched = economic_calendar.events_in_window(
        events, datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc), datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc))
    assert [e["title"] for e in matched] == ["lower bound", "inside", "upper bound"]


def test_events_in_window_returns_events_sorted_by_date():
    events = [
        {"title": "second", "date": datetime(2024, 1, 1, 13, 0, tzinfo=timezone.utc).isoformat()},
        {"title": "first", "date": datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc).isoformat()},
    ]
    matched = economic_calendar.events_in_window(
        events, datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc), datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc))
    assert [e["title"] for e in matched] == ["first", "second"]


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



def test_kaggle_row_becomes_an_archive_event():
    from price_monitor.economic_calendar import _normalize_kaggle_row

    event = _normalize_kaggle_row({
        "date": "14/01/2020", "time": "13:30", "zone": "united states",
        "currency": "USD", "importance": "high", "event": "CPI (MoM)",
        "actual": "0.2%", "forecast": "0.3%", "previous": "0.1%",
    })

    assert event["date"] == "2020-01-14T13:30:00+00:00"
    assert event["country"] == "USD"
    assert event["impact"] == "High"
    assert event["forecast"] == "0.3%"


def test_kaggle_all_day_rows_are_dropped():
    # Выходные и праздники: ни момента публикации, ни влияния на рынок. В
    # исходном датасете таких строк 85%.
    from price_monitor.economic_calendar import _normalize_kaggle_row

    assert _normalize_kaggle_row({
        "date": "01/01/2020", "time": "All Day", "zone": "united states",
        "importance": "", "event": "New Year's Day",
    }) is None


def test_kaggle_rows_without_importance_are_dropped():
    from price_monitor.economic_calendar import _normalize_kaggle_row

    assert _normalize_kaggle_row({
        "date": "14/01/2020", "time": "13:30", "importance": None,
        "event": "x", "zone": "y",
    }) is None


def test_kaggle_time_is_read_as_utc():
    # Проверено на данных: у CPI США ровно два времени, 12:30 и 13:30 UTC, что
    # соответствует 08:30 по Нью-Йорку летом и зимой. Толковать это как местное
    # время значило бы повторить ошибку прежнего архива.
    from price_monitor.economic_calendar import _normalize_kaggle_row

    summer = _normalize_kaggle_row({"date": "11/06/2025", "time": "12:30",
                                    "importance": "high", "event": "CPI",
                                    "currency": "USD", "zone": "us"})
    assert summer["date"].endswith("+00:00")


def test_forexfactory_state_is_parsed_by_brace_matching():
    from price_monitor.economic_calendar import _extract_calendar_state

    html = ('junk calendarComponentStates[1] = {days: '
            '[{"date":"Mon","events":[{"name":"CPI m/m","dateline":1788102900,'
            '"currency":"USD","impactTitle":"High Impact Expected","actual":"",'
            '"forecast":"0.3%","previous":"0.2%"}]}], other: 1}; more junk')

    days = _extract_calendar_state(html)
    assert len(days) == 1
    assert days[0]["events"][0]["name"] == "CPI m/m"


def test_forexfactory_state_missing_is_an_error():
    from price_monitor.economic_calendar import CalendarError, _extract_calendar_state

    with pytest.raises(CalendarError, match="нет состояния"):
        _extract_calendar_state("<html>ничего похожего</html>")


def test_merge_key_still_separates_genuinely_different_events(tmp_path):
    # Ключ слияния включает дату, и именно поэтому сдвинутые копии в прежнем
    # архиве выглядели отдельными событиями. Сама по себе эта чувствительность
    # нужна: одна и та же публикация в разные месяцы - разные события.
    path = store_path(str(tmp_path))
    january = {"date": "2021-01-13T13:30:00+00:00", "country": "USD",
               "title": "CPI m/m", "impact": "High", "actual": "", "forecast": "",
               "previous": ""}
    february = dict(january, date="2021-02-10T13:30:00+00:00")

    assert merge_events(path, [january, february]) == 2
    assert merge_events(path, [january, february]) == 0

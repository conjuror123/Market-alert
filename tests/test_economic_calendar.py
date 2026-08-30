import json

import pytest
import requests

from price_monitor import economic_calendar
from price_monitor.economic_calendar import (
    CalendarError,
    fetch_calendar,
    fetch_spoluan_year,
    import_spoluan_years,
    merge_events,
    parse_event_time,
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
    assert events[0] == {
        "title": "Non-Farm Payrolls", "country": "USD", "date": "2026-09-04T08:30:00-04:00",
        "impact": "High", "forecast": "180K", "previous": "150K", "actual": "190K",
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


# --- Historical import from the spoluan/forex-factory-scraper GitHub CSVs ---

SPOLUAN_CSV = (
    "Date,Time,Currency,Event,Impact,Actual,Forecast,Previous,Combined DateTime\n"
    "2022-01-03,All Day,NZD,Bank Holiday,Low,,,,2022-01-03 00:00:00\n"
    "2022-01-27,3:00am,USD,FOMC Statement,High,,,,2022-01-27 03:00:00\n"
    "2022-01-07,9:30pm,USD,Non-Farm Employment Change,High,199K,426K,249K,2022-01-07 21:30:00\n"
    ",,,,,,,,\n"  # a fully blank row some scraper exports include
)


def test_normalize_spoluan_row_converts_timed_event_from_utc8_to_utc():
    # Real FOMC Statement release: 2022-01-26 14:00 US Eastern (EST, UTC-5) =
    # 2022-01-26 19:00 UTC. The scraper's own display offset is UTC+8 (see
    # _SPOLUAN_DISPLAY_UTC_OFFSET_HOURS), landing this at 2022-01-27 03:00 in
    # its "Combined DateTime" column - converting back should recover the
    # real UTC instant.
    row = {"Date": "2022-01-27", "Time": "3:00am", "Currency": "USD", "Event": "FOMC Statement",
           "Impact": "High", "Actual": "", "Forecast": "", "Previous": "",
           "Combined DateTime": "2022-01-27 03:00:00"}
    normalized = economic_calendar._normalize_spoluan_row(row)
    assert normalized["date"] == "2022-01-26T19:00:00+00:00"


def test_normalize_spoluan_row_anchors_all_day_events_to_utc_midnight_of_their_own_date():
    # "All Day" rows have no real time-of-day - the -8h correction used for
    # timed events would otherwise shift these into the previous UTC day.
    row = {"Date": "2022-01-03", "Time": "All Day", "Currency": "NZD", "Event": "Bank Holiday",
           "Impact": "Low", "Actual": "", "Forecast": "", "Previous": "",
           "Combined DateTime": "2022-01-03 00:00:00"}
    normalized = economic_calendar._normalize_spoluan_row(row)
    assert normalized["date"] == "2022-01-03T00:00:00+00:00"


def test_normalize_spoluan_row_maps_known_fields():
    row = {"Date": "2022-01-07", "Time": "9:30pm", "Currency": "USD",
           "Event": "Non-Farm Employment Change", "Impact": "High",
           "Actual": "199K", "Forecast": "426K", "Previous": "249K",
           "Combined DateTime": "2022-01-07 21:30:00"}
    normalized = economic_calendar._normalize_spoluan_row(row)
    assert normalized["title"] == "Non-Farm Employment Change"
    assert normalized["country"] == "USD"
    assert normalized["impact"] == "High"
    assert normalized["actual"] == "199K"
    assert normalized["forecast"] == "426K"
    assert normalized["previous"] == "249K"


def test_normalize_spoluan_row_returns_none_for_blank_or_malformed_rows():
    assert economic_calendar._normalize_spoluan_row(
        {"Date": "", "Time": "", "Currency": "", "Event": "", "Combined DateTime": ""}) is None
    assert economic_calendar._normalize_spoluan_row(
        {"Event": "CPI m/m", "Time": "9:30pm", "Combined DateTime": "not a datetime"}) is None


def test_fetch_spoluan_year_parses_csv_and_skips_blank_rows(monkeypatch):
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        return FakeResponse(200, text=SPOLUAN_CSV)

    monkeypatch.setattr(economic_calendar.requests, "get", fake_get)
    events = fetch_spoluan_year(2022)

    assert calls == [economic_calendar._SPOLUAN_CSV_URL.format(year=2022)]
    assert len(events) == 3
    assert {e["title"] for e in events} == {"Bank Holiday", "FOMC Statement", "Non-Farm Employment Change"}


def test_fetch_spoluan_year_raises_calendar_error_on_http_failure(monkeypatch):
    def fake_get(url, timeout):
        return FakeResponse(404, text="Not Found")

    monkeypatch.setattr(economic_calendar.requests, "get", fake_get)
    with pytest.raises(CalendarError):
        fetch_spoluan_year(1999)


def test_import_spoluan_years_continues_after_a_failed_year(monkeypatch):
    def fake_fetch(year, session=None):
        if year == 2019:
            raise CalendarError("no data for this year")
        return [{"title": f"event {year}"}]

    monkeypatch.setattr(economic_calendar, "fetch_spoluan_year", fake_fetch)
    events = economic_calendar.import_spoluan_years([2019, 2020, 2021])

    assert [e["title"] for e in events] == ["event 2020", "event 2021"]


def test_import_spoluan_years_aggregates_all_years(monkeypatch):
    monkeypatch.setattr(
        economic_calendar, "fetch_spoluan_year",
        lambda year, session=None: [{"title": f"event {year}"}])

    events = import_spoluan_years([2021, 2022, 2023])
    assert [e["title"] for e in events] == ["event 2021", "event 2022", "event 2023"]

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
    # The date is normalised to UTC: the feed serves a fixed -04:00 offset while
    # both historical branches write UTC, and keeping one moment in two packagings
    # would create a shifted copy.
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


@pytest.mark.parametrize("ordered", [False, True], ids=["list", "timeline"])
def test_events_in_window_keeps_only_events_inside_the_range_inclusive(ordered):
    events = [
        {"title": "before", "date": datetime(2024, 1, 1, 11, 59, tzinfo=timezone.utc).isoformat()},
        {"title": "lower bound", "date": datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc).isoformat()},
        {"title": "inside", "date": datetime(2024, 1, 1, 13, 0, tzinfo=timezone.utc).isoformat()},
        {"title": "upper bound", "date": datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc).isoformat()},
        {"title": "after", "date": datetime(2024, 1, 1, 14, 1, tzinfo=timezone.utc).isoformat()},
    ]
    if ordered:
        events = economic_calendar.Timeline(list(reversed(events)))
    matched = economic_calendar.events_in_window(
        events, datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc), datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc))
    assert [e["title"] for e in matched] == ["lower bound", "inside", "upper bound"]


@pytest.mark.parametrize("ordered", [False, True], ids=["list", "timeline"])
def test_events_in_window_returns_events_sorted_by_date(ordered):
    events = [
        {"title": "second", "date": datetime(2024, 1, 1, 13, 0, tzinfo=timezone.utc).isoformat()},
        {"title": "first", "date": datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc).isoformat()},
    ]
    if ordered:
        events = economic_calendar.Timeline(events)
    matched = economic_calendar.events_in_window(
        events, datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc), datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc))
    assert [e["title"] for e in matched] == ["first", "second"]


def test_a_timeline_orders_by_the_moment_not_the_written_offset():
    # 08:30 New York is 12:30 UTC, after 12:00 UTC, though "08:30" sorts first.
    events = [{"title": "new york", "date": "2026-09-03T08:30:00-04:00"},
              {"title": "utc", "date": "2026-09-03T12:00:00+00:00"},
              {"title": "tokyo", "date": "2026-09-03T23:50:00+09:00"}]
    timeline = economic_calendar.Timeline(events)
    found = timeline.between(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
                             datetime(2026, 9, 3, 12, 30, tzinfo=timezone.utc))
    assert [e["title"] for e in found] == ["utc", "new york"]
    assert len(timeline) == 3 and not economic_calendar.Timeline([])


def test_a_timeline_finds_what_a_scan_of_the_archive_finds():
    archive = load_events(store_path("data/economic_calendar"))
    if not archive:
        pytest.skip("no calendar archive in this checkout")
    timeline = economic_calendar.Timeline(archive)
    from datetime import timedelta
    for hour in range(int(datetime(2016, 1, 4, tzinfo=timezone.utc).timestamp()),
                      int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()),
                      997 * 3600):                 # about every six weeks, at every hour of day
        moment = datetime.fromtimestamp(hour, tz=timezone.utc)
        lower, upper = moment - timedelta(hours=2), moment + timedelta(hours=1)
        assert (economic_calendar.events_in_window(timeline, lower, upper)
                == economic_calendar.events_in_window(archive, lower, upper))


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


def test_a_write_that_dies_half_way_leaves_the_archive_as_it_was(tmp_path, monkeypatch):
    # Written in place, a run killed mid-write left the archive cut short.
    path = str(tmp_path / "calendar.ndjson")
    merge_events(path, SAMPLE_RAW)
    real, calls = economic_calendar.json.dumps, []

    def dies_on_the_second_row(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(*a, **k)

    monkeypatch.setattr(economic_calendar.json, "dumps", dies_on_the_second_row)
    new = dict(SAMPLE_RAW[0], title="CPI", date="2026-09-10T08:30:00-04:00")
    with pytest.raises(KeyboardInterrupt):
        merge_events(path, [new])
    monkeypatch.undo()
    assert len(economic_calendar.load_events(path)) == 2


def test_load_events_returns_empty_list_when_file_missing(tmp_path):
    assert economic_calendar.load_events(str(tmp_path / "nope.ndjson")) == []


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

    with pytest.raises(CalendarError, match="no calendar state"):
        _extract_calendar_state("<html>nothing of the sort</html>")


def test_merge_key_still_separates_genuinely_different_events(tmp_path):
    # The merge key includes the date, and that is exactly why shifted copies in
    # the old archive looked like separate events. The sensitivity itself is
    # wanted: the same release in different months is a different event.
    path = store_path(str(tmp_path))
    january = {"date": "2021-01-13T13:30:00+00:00", "country": "USD",
               "title": "CPI m/m", "impact": "High", "actual": "", "forecast": "",
               "previous": ""}
    february = dict(january, date="2021-02-10T13:30:00+00:00")

    assert merge_events(path, [january, february]) == 2
    assert merge_events(path, [january, february]) == 0

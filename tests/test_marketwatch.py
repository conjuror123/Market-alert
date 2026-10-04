"""MarketWatch's hourly bars (price_monitor.marketwatch): the parser."""
from datetime import datetime, timezone

import pytest

from price_monitor import marketwatch
from price_monitor.models import ExchangeError

HOUR = 3600


def ts(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def payload(rows):
    return {"TimeInfo": {"Ticks": [ts(t) * 1000 for t, _ in rows]},
            "Series": [{"DataPoints": [p for _, p in rows]}]}


def test_each_tick_is_the_start_of_its_hour_and_only_ended_bars_are_kept():
    rows = [("2026-10-02 18:00", [96.10, 96.20, 96.05, 96.15]),
            ("2026-10-02 19:00", [None, None, None, None]),      # no price that hour
            ("2026-10-02 20:00", [96.15, 96.30, 96.10, 96.25]),
            ("2026-10-02 21:00", [96.25, 96.26, 96.20, 96.21])]  # still in progress
    now = datetime.fromtimestamp(ts("2026-10-02 21:05"), timezone.utc)
    got = marketwatch.parse(payload(rows), "CURRENCY/US/XTUP/USDINR", now)
    assert [c.open_time for c in got] == [ts("2026-10-02 18:00"), ts("2026-10-02 20:00")]
    assert (got[1].open, got[1].high, got[1].low, got[1].close) == (96.15, 96.30, 96.10, 96.25)
    assert got[1].close_time == got[1].open_time + HOUR


def test_an_answer_without_a_series_raises():
    with pytest.raises(ExchangeError):
        marketwatch.parse({"error": "Unknown instrument"}, "CURRENCY/US/XTUP/USDXXX")


def test_a_refused_request_raises(monkeypatch):
    class Answer:
        status_code = 400
        text = '{"error":"Unknown instrument"}'

    class Session:
        def get(self, *a, **k):
            return Answer()

    with pytest.raises(ExchangeError, match="400"):
        marketwatch.fetch_hourly("CURRENCY/US/XTUP/USDXXX", Session())

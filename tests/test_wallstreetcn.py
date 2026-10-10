"""Wallstreetcn's hourly bars (price_monitor.wallstreetcn): the parser and the request."""
from datetime import datetime, timezone

import pytest

from price_monitor import wallstreetcn
from price_monitor.models import ExchangeError

HOUR = 3600


def ts(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def payload(code, lines):
    return {"code": 20000, "message": "OK", "data": {"candle": {code: {"lines": lines}}}}


def test_each_line_is_open_close_high_low_and_the_start_and_only_ended_bars_are_kept():
    lines = [[29870.0, 29905.0, 29930.0, 29850.0, ts("2026-10-09 15:00")],
             [29905.0, 29880.0, 29915.0, 29860.0, ts("2026-10-09 16:00")],
             [29880.0, 29890.0, 29895.0, 29875.0, ts("2026-10-09 17:00")]]   # still in progress
    now = datetime.fromtimestamp(ts("2026-10-09 17:05"), timezone.utc)
    got = wallstreetcn.parse(payload("UKSN.OTC", lines), "UKSN.OTC", now)
    assert [c.open_time for c in got] == [ts("2026-10-09 15:00"), ts("2026-10-09 16:00")]
    assert (got[1].open, got[1].high, got[1].low, got[1].close) == (29905.0, 29915.0, 29860.0, 29880.0)
    assert got[1].close_time == got[1].open_time + HOUR and got[1].volume == 0.0


def test_an_answer_without_the_code_raises():
    # What it answers for a code it does not carry.
    with pytest.raises(ExchangeError):
        wallstreetcn.parse({"code": 20000, "data": {"candle": {}, "fields": []}}, "XXXX.OTC")


class _Answer:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _Session:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.asked.append(params)
        return self.answer


def test_it_asks_for_the_days_wanted_and_never_more_than_one_answer_serves():
    session = _Session(_Answer(200, payload("UKNI.OTC", [])))
    wallstreetcn.fetch_hourly("UKNI.OTC", session, days=10)
    wallstreetcn.fetch_hourly("UKNI.OTC", session, days=400)
    assert [p["tick_count"] for p in session.asked] == [10 * 24 + 24, wallstreetcn.MAX_TICKS]
    assert session.asked[0]["period_type"] == HOUR and session.asked[0]["prod_code"] == "UKNI.OTC"


def test_a_refused_request_raises():
    with pytest.raises(ExchangeError, match="403"):
        wallstreetcn.fetch_hourly("UKAH.OTC", _Session(_Answer(403)))


def test_a_pair_is_named_by_its_six_letters_and_usd_krw_is_not_carried():
    assert wallstreetcn.pair_code("EUR/USD") == "EURUSD.OTC"
    assert wallstreetcn.pair_code("USD/BRL") == "USDBRL.OTC"
    assert wallstreetcn.pair_code("USD/KRW") is None
    assert "KC=F" not in wallstreetcn.SOFTS                 # no coffee

"""Tencent's half-hour bars (price_monitor.tencent): the parser and the request."""
from datetime import datetime, timezone

import pytest

from price_monitor import tencent
from price_monitor.models import ExchangeError, Unreachable

HALF_HOUR = 1800


def ts(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def payload(lines, code="sh000300"):
    """An answer as it comes: each line its end in Beijing, open, close, high, low,
    volume, and two more."""
    return {"code": 0, "msg": "", "data": {code: {"m30": lines, "qt": {}}}}


def test_each_line_is_stamped_by_its_start_in_utc_and_only_ended_bars_are_kept():
    # 2026-10-16 10:00 Beijing ends the 09:30 half hour: 01:30 UTC.
    lines = [["202610161000", "4166.01", "4180.20", "4189.26", "4160.10", "61234567.00", {}, "20.1"],
             ["202610161030", "4180.20", "4175.00", "4185.00", "4170.00", "31234567.00", {}, "10.2"],
             ["202610161100", "4175.00", "4178.00", "4179.00", "4172.00", "21234567.00", {}, "7.0"]]
    now = datetime.fromtimestamp(ts("2026-10-16 02:45"), timezone.utc)      # 10:45 Beijing
    got = tencent.parse(payload(lines), "sh000300", now)
    assert [c.open_time for c in got] == [ts("2026-10-16 01:30"), ts("2026-10-16 02:00")]
    first = got[0]
    assert (first.open, first.close, first.high, first.low) == (4166.01, 4180.20, 4189.26, 4160.10)
    assert first.close_time == first.open_time + HALF_HOUR and first.volume == 61234567.0


def test_a_line_without_a_price_is_skipped():
    lines = [["202610161000", "4166.01", "4180.20", "4189.26", "4160.10", "61234567.00"],
             ["202610161030", None, None, None, None, None]]
    now = datetime.fromtimestamp(ts("2026-10-16 08:00"), timezone.utc)
    assert len(tencent.parse(payload(lines), "sh000300", now)) == 1


def test_an_answer_without_the_code_raises():
    # What it answers for a market it does not serve here (Hong Kong's).
    with pytest.raises(ExchangeError):
        tencent.parse({"code": -1, "msg": "param error", "data": []}, "hkHSI")


class _Answer:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _Session:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.asked.append(params)
        return self.answers.pop(0)


def test_it_asks_for_the_most_bars_one_answer_serves(monkeypatch):
    monkeypatch.setattr(tencent.time, "sleep", lambda s: None)
    session = _Session([_Answer(503), _Answer(200, payload([]))])
    assert tencent.fetch_bars("sh000300", session) == []
    assert session.asked[-1] == {"param": "sh000300,m30,,800"}


def test_a_server_that_never_answers_is_unreachable_and_a_refusal_is_not(monkeypatch):
    monkeypatch.setattr(tencent.time, "sleep", lambda s: None)
    with pytest.raises(Unreachable):
        tencent.fetch_bars("sh000300", _Session([_Answer(502)] * tencent.MAX_ATTEMPTS))
    with pytest.raises(ExchangeError) as refused:
        tencent.fetch_bars("sh000300", _Session([_Answer(404)]))
    assert not isinstance(refused.value, Unreachable)

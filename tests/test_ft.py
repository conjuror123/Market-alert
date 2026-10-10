"""The FT's hourly bars (price_monitor.ft): the parser, the names and the request."""
import json
from datetime import datetime, timezone

import pytest

from price_monitor import ft
from price_monitor.models import ExchangeError

HOUR = 3600


def ts(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def payload(dates, opens, highs, lows, closes):
    return {"Dates": dates, "Elements": [{"ComponentSeries": [
        {"Type": "Open", "Values": opens}, {"Type": "High", "Values": highs},
        {"Type": "Low", "Values": lows}, {"Type": "Close", "Values": closes}]}]}


def test_each_bar_is_dated_by_its_end_and_stamped_here_by_its_start():
    answer = payload(["2026-10-09T15:00:00", "2026-10-09T16:00:00", "2026-10-09T17:00:00",
                      "2026-10-09T18:00:00"],
                     [240.1, 240.5, None, 241.0], [240.6, 241.0, None, 241.2],
                     [240.0, 240.4, None, 240.9], [240.5, 240.9, None, 241.1])
    now = datetime.fromtimestamp(ts("2026-10-09 17:30"), timezone.utc)
    got = ft.parse(answer, "LCZ26", now)
    # 14:00 and 15:00; 16:00 has no price, 17:00 has not ended.
    assert [c.open_time for c in got] == [ts("2026-10-09 14:00"), ts("2026-10-09 15:00")]
    assert (got[1].open, got[1].high, got[1].low, got[1].close) == (240.5, 241.0, 240.4, 240.9)
    assert got[1].close_time == ts("2026-10-09 16:00") and got[1].volume == 0.0
    with pytest.raises(ExchangeError):
        ft.parse({"Status": 404}, "LCZ26")


def test_a_cattle_contract_is_named_as_the_ft_names_it():
    assert ft.cattle_symbol("LEZ26.CME") == "LCZ26"
    assert ft.cattle_symbol("LEG27.CME") == "LCG27"
    assert ft.cattle_symbol(None) is None and ft.cattle_symbol("KCZ26.NYB") is None


class _Answer:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Session:
    def __init__(self, found, chart):
        self.found, self.chart, self.searched, self.posted = found, chart, [], []

    def get(self, url, params=None, headers=None, timeout=None):
        self.searched.append(params["query"])
        return self.found

    def post(self, url, data=None, headers=None, timeout=None):
        self.posted.append(json.loads(data))
        return self.chart


def test_a_contract_is_found_once_by_the_search_and_asked_in_trading_days(monkeypatch):
    monkeypatch.setattr(ft, "_found", {})
    found = _Answer(200, {"data": {"security": [
        {"symbol": "LCZ26:CME", "xid": "991714947", "assetClass": "Commodities"}]}})
    session = _Session(found, _Answer(200, payload([], [], [], [], [])))
    ft.fetch_hourly("LCZ26", session, days=9.2)
    ft.fetch_hourly("LCZ26", session, days=200)
    ft.fetch_hourly("KC.1", session, days=3)               # a continuous series: no search
    assert session.searched == ["LCZ26"]
    assert [(p["elements"][0]["Symbol"], p["days"]) for p in session.posted] == [
        ("991714947", 10), ("991714947", ft.MAX_DAYS), ("1046650", 3)]


def test_a_contract_the_search_does_not_find_and_a_refusal_raise(monkeypatch):
    monkeypatch.setattr(ft, "_found", {})
    nothing = _Answer(200, {"data": {"security": [
        {"symbol": "LEX:LSE", "xid": "270588", "assetClass": "Equities"}]}})
    with pytest.raises(ExchangeError, match="not found"):
        ft.fetch_hourly("LCZ26", _Session(nothing, None))
    with pytest.raises(ExchangeError, match="403"):
        ft.fetch_hourly("KC.1", _Session(None, _Answer(403)))

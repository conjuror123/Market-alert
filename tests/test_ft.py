"""The FT's hourly bars (price_monitor.ft): the parser, the names and the request."""
import json
from datetime import datetime, timezone

import pytest

from price_monitor import ft
from price_monitor.models import ExchangeError

HOUR = 3600


def ts(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def payload(dates, opens, highs, lows, closes, exchange="CME"):
    return {"Dates": dates, "Elements": [{"ExchangeId": exchange, "ComponentSeries": [
        {"Type": "Open", "Values": opens}, {"Type": "High", "Values": highs},
        {"Type": "Low", "Values": lows}, {"Type": "Close", "Values": closes}]}]}


def test_each_bar_is_dated_by_its_end_and_stamped_here_by_its_start():
    answer = payload(["2026-11-09T15:00:00", "2026-11-09T16:00:00", "2026-11-09T17:00:00",
                      "2026-11-09T18:00:00"],
                     [240.1, 240.5, None, 241.0], [240.6, 241.0, None, 241.2],
                     [240.0, 240.4, None, 240.9], [240.5, 240.9, None, 241.1])
    now = datetime.fromtimestamp(ts("2026-11-09 17:30"), timezone.utc)
    got = ft.parse(answer, "LCZ26", now)
    # 14:00 and 15:00; 16:00 has no price, 17:00 has not ended.
    assert [c.open_time for c in got] == [ts("2026-11-09 14:00"), ts("2026-11-09 15:00")]
    assert (got[1].open, got[1].high, got[1].low, got[1].close) == (240.5, 241.0, 240.4, 240.9)
    assert got[1].close_time == ts("2026-11-09 16:00") and got[1].volume == 0.0
    with pytest.raises(ExchangeError):
        ft.parse({"Status": 404}, "LCZ26")


def _ends(exchange, *dates):
    answer = payload(list(dates), *[[1.0] * len(dates)] * 4, exchange=exchange)
    now = datetime(2027, 1, 1, tzinfo=timezone.utc)
    return [datetime.fromtimestamp(c.close_time, timezone.utc).strftime("%m-%d %H:%M")
            for c in ft.parse(answer, "x", now)]


def test_a_date_is_an_hour_late_while_its_exchange_keeps_summer_time():
    # The FTSE's last bar of 2026-10-20 (summer) and of 11-03 (winter): the
    # session ends 16:30 London both days, 15:30 and 16:30 UTC. The FT says 17:00
    # both times.
    assert _ends("FSI", "2026-10-20T17:00:00", "2026-11-03T17:00:00") == [
        "10-20 16:00", "11-03 17:00"]
    # Coffee the same on New York's clock, which moved a week later: still an
    # hour late on 10-28, on time on 11-03.
    assert _ends("IUS", "2026-10-28T19:00:00", "2026-11-03T19:00:00") == [
        "10-28 18:00", "11-03 19:00"]
    # Sao Paulo keeps no summer time: as dated.
    assert _ends("SAO", "2026-10-20T21:00:00") == ["10-20 21:00"]


def test_a_listing_on_an_exchange_without_a_zone_is_an_outage():
    with pytest.raises(ExchangeError, match="no zone"):
        _ends("TYO", "2026-10-20T06:00:00")


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


def test_an_index_is_asked_by_its_xid_without_a_search(monkeypatch):
    monkeypatch.setattr(ft, "_found", {})
    session = _Session(None, _Answer(200, payload([], [], [], [], [])))
    ft.fetch_hourly(ft.INDICES["^FTSE"], session, days=3)
    assert session.searched == [] and session.posted[0]["elements"][0]["Symbol"] == "572009"

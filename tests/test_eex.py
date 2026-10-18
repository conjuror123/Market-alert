"""EEX's TTF month future (price_monitor.eex): trades into hourly bars, and the
requests, one a weekday."""
from datetime import datetime, timezone

import pytest

from price_monitor import eex
from price_monitor.models import ExchangeError, Unreachable

HOUR = 3600


def ts(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def payload(trades):
    """An answer as it comes: the trades' prices, volumes and lot sizes, each
    stamped to the nanosecond."""
    return {"lastUpdate": "2026-10-16T15:58:33.757904000Z", "currency": "EUR", "uOM": "MWh",
            "series": [{"serieName": "lastPx", "timeAndValue": trades},
                       {"serieName": "volume", "timeAndValue": []},
                       {"serieName": "lotSize", "timeAndValue": []}]}


def test_trades_fold_into_ended_hours_first_trade_open_last_trade_close():
    trades = eex.parse(payload([
        ["2026-10-16T06:00:51.017936000Z", 85.45],
        ["2026-10-16T06:31:56.394535000Z", 85.90],
        ["2026-10-16T06:58:31.243720000Z", 85.40],
        ["2026-10-16T07:02:31.245216000Z", 85.60],
        ["2026-10-16T08:10:00.000000000Z", 86.00]]), "202611")       # still in progress
    got = eex.hourly(trades, datetime.fromtimestamp(ts("2026-10-16 08:30"), timezone.utc))
    assert [c.open_time for c in got] == [ts("2026-10-16 06:00"), ts("2026-10-16 07:00")]
    first = got[0]
    assert (first.open, first.high, first.low, first.close) == (85.45, 85.90, 85.40, 85.40)
    assert first.close_time == first.open_time + HOUR and first.volume == 0.0
    assert (got[1].open, got[1].close) == (85.60, 85.60)


def test_a_day_without_trades_is_no_bars_and_an_answer_without_series_raises():
    assert eex.parse({"lastUpdate": None, "series": [
        {"serieName": "lastPx", "timeAndValue": []}]}, "202611") == []
    with pytest.raises(ExchangeError):
        eex.parse({"error": "bad"}, "202611")


class _Answer:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _Session:
    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.asked.append(params)
        return self.answer


def test_it_asks_one_request_a_weekday_of_the_month_the_store_holds():
    # Friday 2026-10-16 to Tuesday 10-20: Friday, Monday, Tuesday - not the weekend.
    session = _Session(_Answer(200, payload([])))
    now = datetime.fromtimestamp(ts("2026-10-20 12:00"), timezone.utc)
    assert eex.fetch_hourly("202611", session, now, days=4) == []
    assert [p["startDate"] for p in session.asked] == ["2026-10-16", "2026-10-19", "2026-10-20"]
    assert {(p["shortCode"], p["maturity"], p["area"]) for p in session.asked} == {
        ("G3BM", "202611", "TTF")}


def test_a_server_that_never_answers_is_unreachable_and_a_refusal_is_not(monkeypatch):
    monkeypatch.setattr(eex.time, "sleep", lambda s: None)
    now = datetime.fromtimestamp(ts("2026-10-16 12:00"), timezone.utc)
    with pytest.raises(Unreachable):
        eex.fetch_hourly("202611", _Session(_Answer(503)), now, days=1)
    with pytest.raises(ExchangeError) as refused:
        eex.fetch_hourly("202611", _Session(_Answer(400)), now, days=1)
    assert not isinstance(refused.value, Unreachable)


def test_a_thin_hour_is_left_out_on_request_and_the_store_names_its_month():
    trades = [(ts("2026-10-16 06:00") + 60 * k, 85.0 + k / 100) for k in range(5)] + \
             [(ts("2026-10-16 07:10"), 86.0), (ts("2026-10-16 07:20"), 86.1)]
    now = datetime.fromtimestamp(ts("2026-10-16 09:00"), timezone.utc)
    assert [c.open_time for c in eex.hourly(trades, now)] == [ts("2026-10-16 06:00"),
                                                              ts("2026-10-16 07:00")]
    assert [c.open_time for c in eex.hourly(trades, now, min_trades=5)] == [
        ts("2026-10-16 06:00")]
    from jump import futures
    assert eex.MONTH_CODES == futures.MONTH_CODES
    assert [eex.maturity_for(c) for c in ("TFMX26.NDX", "TFMZ26.NDX", "TFMF27.NDX")] == [
        "202611", "202612", "202701"]

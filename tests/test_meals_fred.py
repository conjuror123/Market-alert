from datetime import date, datetime, timezone

import pytest

from meals import fred


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, params, timeout, headers):
        self.calls.append({"url": url, "params": dict(params)})
        return self.response


def payload(rows):
    return {"observations": [{"date": d, "value": v} for d, v in rows]}


def at(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def test_available_at_is_the_next_morning():
    # Wednesday's value is published on Thursday morning.
    assert at(fred.available_at(date(2026, 8, 26))) == datetime(
        2026, 8, 27, fred.PUBLICATION_HOUR_UTC, tzinfo=timezone.utc)


def test_available_at_skips_the_weekend():
    # Friday's value becomes known only on Monday - without that the backtest
    # would apply the VIX multiplier at the weekend, when it did not yet exist.
    assert at(fred.available_at(date(2026, 8, 28))).date() == date(2026, 8, 31)


def test_available_at_is_always_after_the_observation():
    for day in (date(2021, 1, 1), date(2026, 8, 26), date(2026, 8, 29)):
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        assert fred.available_at(day) > midnight.timestamp()


def test_fetch_series_skips_missing_values():
    # FRED encodes weekends and holidays, when the index was not computed, with a dot.
    session = FakeSession(FakeResponse(200, payload([
        ("2026-08-26", "15.21"), ("2026-08-27", "."), ("2026-08-28", "14.51"),
    ])))
    out = fred.fetch_series("VIXCLS", "k", date(2026, 8, 1), session=session)

    assert list(out["close"]) == [15.21, 14.51]
    assert len(out) == 2


def test_fetch_series_maps_the_day_to_utc_midnight():
    session = FakeSession(FakeResponse(200, payload([("2026-08-26", "15.21")])))
    out = fred.fetch_series("VIXCLS", "k", date(2026, 8, 1), session=session)
    assert at(out.iloc[0]["day"]) == datetime(2026, 8, 26, tzinfo=timezone.utc)
    assert out.iloc[0]["available_at"] > out.iloc[0]["day"]


def test_fetch_series_passes_the_observation_start():
    session = FakeSession(FakeResponse(200, payload([("2021-01-04", "26.97")])))
    fred.fetch_series("VIXCLS", "key", date(2021, 1, 1), session=session)
    params = session.calls[0]["params"]
    assert params["observation_start"] == "2021-01-01"
    assert params["series_id"] == "VIXCLS"
    assert params["api_key"] == "key"


def test_fetch_series_requires_a_key():
    with pytest.raises(fred.FredError, match="FRED_API_KEY"):
        fred.fetch_series("VIXCLS", "", date(2021, 1, 1))


def test_fetch_series_reports_a_bad_status():
    session = FakeSession(FakeResponse(429, {"error": "too many"}))
    with pytest.raises(fred.FredError, match="429"):
        fred.fetch_series("VIXCLS", "k", date(2021, 1, 1), session=session)


def test_fetch_series_reports_an_empty_answer():
    session = FakeSession(FakeResponse(200, payload([("2026-08-26", ".")])))
    with pytest.raises(fred.FredError, match="no observations"):
        fred.fetch_series("VIXCLS", "k", date(2026, 8, 1), session=session)

import json
from datetime import datetime, timezone

import pytest

from price_monitor import twelvedata
from price_monitor.models import ExchangeError
from price_monitor.twelvedata import fetch_full_history, fetch_klines


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params, timeout, headers):
        self.calls.append(dict(params))
        status, payload = self.responses.pop(0)
        return FakeResponse(status, payload)


def ok_payload(rows):
    """rows: list of (datetime_str, open, high, low, close), newest first (as the real API returns)."""
    return {
        "status": "ok",
        "values": [
            {"datetime": dt, "open": str(o), "high": str(h), "low": str(l), "close": str(c)}
            for dt, o, h, l, c in rows
        ],
    }


def test_fetch_klines_requires_api_key():
    with pytest.raises(ExchangeError, match="API key"):
        fetch_klines("EUR/USD", "1h", limit=10, base_url="https://x", api_key="")


def test_fetch_klines_sorts_ascending_and_sets_utc_timestamp():
    payload = ok_payload([
        ("2026-08-30 09:00:00", 1.1, 1.2, 1.0, 1.15),
        ("2026-08-30 08:00:00", 1.0, 1.1, 0.9, 1.05),
    ])
    sess = FakeSession([(200, payload)])

    candles = fetch_klines("EUR/USD", "1h", limit=10, base_url="https://x", api_key="k", session=sess)

    assert [c.close for c in candles] == [1.05, 1.15]
    assert candles[0].open_time < candles[1].open_time
    assert candles[0].close_time - candles[0].open_time == 3600
    assert all(c.volume == 0.0 for c in candles)


def test_fetch_klines_requests_utc_timezone_explicitly():
    payload = ok_payload([("2026-08-30 09:00:00", 1.1, 1.2, 1.0, 1.15)])
    sess = FakeSession([(200, payload)])

    fetch_klines("EUR/USD", "1h", limit=10, base_url="https://x", api_key="k", session=sess)

    assert sess.calls[0]["timezone"] == "UTC"
    assert sess.calls[0]["symbol"] == "EUR/USD"
    assert sess.calls[0]["apikey"] == "k"


def test_fetch_klines_trims_to_limit():
    payload = ok_payload([
        ("2026-08-30 10:00:00", 1, 1, 1, 3),
        ("2026-08-30 09:00:00", 1, 1, 1, 2),
        ("2026-08-30 08:00:00", 1, 1, 1, 1),
    ])
    sess = FakeSession([(200, payload)])

    candles = fetch_klines("EUR/USD", "1h", limit=2, base_url="https://x", api_key="k", session=sess)

    assert [c.close for c in candles] == [2, 3]


def test_fetch_klines_raises_on_api_error_status():
    payload = {"status": "error", "code": 429, "message": "rate limited"}
    sess = FakeSession([(200, payload), (200, payload), (200, payload)])

    with pytest.raises(ExchangeError, match="rate limited"):
        fetch_klines("EUR/USD", "1h", limit=10, base_url="https://x", api_key="k",
                     session=sess, retries=3, backoff_seconds=0)


def test_fetch_klines_raises_on_non_200():
    sess = FakeSession([(500, "boom"), (500, "boom"), (500, "boom")])

    with pytest.raises(ExchangeError):
        fetch_klines("EUR/USD", "1h", limit=10, base_url="https://x", api_key="k",
                     session=sess, retries=3, backoff_seconds=0)


def test_fetch_full_history_requires_api_key():
    with pytest.raises(ExchangeError, match="API key"):
        fetch_full_history("EUR/USD", "1h", days=5, base_url="https://x", api_key="")


def test_fetch_full_history_paginates_by_date_range_and_merges(monkeypatch):
    chunk1 = ok_payload([("2026-06-01 00:00:00", 1, 1, 1, 1)])
    chunk2 = ok_payload([("2026-01-01 00:00:00", 1, 1, 1, 2)])
    sess = FakeSession([(200, chunk1), (200, chunk2)])

    candles = fetch_full_history(
        "EUR/USD", "1h", days=200, base_url="https://x", api_key="k",
        session=sess, request_delay_seconds=0,
    )

    assert len(sess.calls) == 2
    assert all("start_date" in c and "end_date" in c and c["timezone"] == "UTC" for c in sess.calls)
    assert [c.close for c in candles] == [2, 1]


def test_supports_the_half_hour_interval_used_by_etfs():
    # ETFs are pulled as half-hourly bars so their grid lines up with the round
    # hour of the currency pairs and crypto (see tremor/bars.to_hourly).
    from price_monitor.twelvedata import _granularity_seconds, _interval_code

    assert _interval_code("30min") == "30min"
    assert _granularity_seconds("30min") == 1800


def test_rejects_an_unknown_interval():
    from price_monitor.twelvedata import _granularity_seconds

    with pytest.raises(ExchangeError, match="Unsupported interval"):
        _granularity_seconds("5min")


def test_half_hour_candles_get_a_half_hour_close_time():
    payload = ok_payload([("2026-08-17 15:30:00", 1.0, 2.0, 0.5, 1.5)])
    session = FakeSession([(200, payload)])

    candles = fetch_klines("SPY", "30min", limit=1, base_url="https://x",
                           api_key="k", session=session)

    assert candles[0].close_time - candles[0].open_time == 1800


def test_chunk_days_controls_the_request_window():
    # An ETF has ~13 half-hourly bars per trading day, so a single 5000-row
    # answer holds more than a year - a large window saves credits.
    session = FakeSession([(200, ok_payload([("2026-08-17 15:30:00", 1.0, 2.0, 0.5, 1.5)]))] * 4)

    fetch_full_history("SPY", "30min", days=300, base_url="https://x", api_key="k",
                       session=session, request_delay_seconds=0, chunk_days=300)

    assert len(session.calls) == 1


def test_parses_volume_when_the_source_provides_it():
    # ETFs serve real hourly volume - §3.5 builds the volume profile on it, and
    # losing it is not an option.
    payload = {"status": "ok", "values": [{
        "datetime": "2026-08-17 15:30:00", "open": "1", "high": "2",
        "low": "0.5", "close": "1.5", "volume": "10553040"}]}
    session = FakeSession([(200, payload)])

    candles = fetch_klines("SPY", "30min", limit=1, base_url="https://x",
                           api_key="k", session=session)

    assert candles[0].volume == 10553040.0


def test_missing_volume_stays_zero_for_forex():
    # No provider has volume for spot FX, and that is not a defect.
    session = FakeSession([(200, ok_payload([("2026-08-17 15:00:00", 1.0, 2.0, 0.5, 1.5)]))])

    candles = fetch_klines("EUR/USD", "1h", limit=1, base_url="https://x",
                           api_key="k", session=session)

    assert candles[0].volume == 0.0


# --- reaching the edge of a symbol's history -------------------------------

class _Resp:
    def __init__(self, status, payload, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


class _Walk:
    """A session that serves `good` chunks and then says there is no more data."""

    def __init__(self, good, status=400, message=None):
        self.good, self.status, self.calls = good, status, 0
        self.message = message or ("No data is available on the specified dates. "
                                   "Try setting different start/end dates.")

    def get(self, url, params=None, **kw):
        self.calls += 1
        if self.calls <= self.good:
            start = 1_700_000_000 + self.calls * 86400
            return _Resp(200, {"values": [{
                "datetime": datetime.fromtimestamp(start, tz=timezone.utc)
                            .strftime("%Y-%m-%d %H:%M:%S"),
                "open": "1", "high": "2", "low": "0.5", "close": "1.5",
                "volume": "10"}]})
        return _Resp(self.status, {"code": self.status, "status": "error",
                                   "message": self.message})


def test_the_edge_of_history_is_not_an_error(monkeypatch):
    # Walking back past what the provider holds is the normal way this ends.
    monkeypatch.setattr(twelvedata.time, "sleep", lambda *_: None)
    session = _Walk(good=3)
    candles = twelvedata.fetch_full_history(
        symbol="SPY", interval="30min", days=4000, base_url="https://x",
        api_key="k", session=session, request_delay_seconds=0, chunk_days=300)

    assert len(candles) == 3          # every successful chunk is kept
    assert session.calls == 4         # and it stops asking once told there is no more


def test_the_no_data_answer_is_not_retried(monkeypatch):
    # Retrying it costs a credit and 8 seconds per attempt and cannot succeed.
    monkeypatch.setattr(twelvedata.time, "sleep", lambda *_: None)
    session = _Walk(good=0)
    twelvedata.fetch_full_history(
        symbol="SPY", interval="30min", days=4000, base_url="https://x",
        api_key="k", session=session, request_delay_seconds=0, chunk_days=300)
    assert session.calls == 1


def test_a_failure_part_way_back_keeps_what_was_already_paid_for(monkeypatch):
    # This used to propagate and take the whole dict with it: a walk that
    # succeeded for years and then hit one bad chunk returned NOTHING,
    # discarding every candle fetched and every credit spent on them.
    monkeypatch.setattr(twelvedata.time, "sleep", lambda *_: None)
    session = _Walk(good=2, status=503, message="upstream unavailable")
    candles = twelvedata.fetch_full_history(
        symbol="SPY", interval="30min", days=4000, base_url="https://x",
        api_key="k", session=session, request_delay_seconds=0, chunk_days=300)
    assert len(candles) == 2


def test_a_permanent_error_still_raises_for_a_single_fetch(monkeypatch):
    # fetch_klines is the live path, where a bad request must be loud.
    monkeypatch.setattr(twelvedata.time, "sleep", lambda *_: None)
    session = _Walk(good=0, status=400)
    with pytest.raises(twelvedata.NoDataInRange):
        twelvedata.fetch_klines(symbol="SPY", interval="30min", limit=10,
                                base_url="https://x", api_key="k", session=session)
    assert session.calls == 1


def test_the_no_data_message_alone_does_not_make_an_error_permanent(monkeypatch):
    # A rate limit dropped as if it were the end of history ends the walk early
    # and silently, which is the one failure here that looks like success.
    monkeypatch.setattr(twelvedata.time, "sleep", lambda *_: None)
    session = _Walk(good=0, status=429,
                    message="No data is available - rate limited")
    with pytest.raises(ExchangeError):
        twelvedata.fetch_klines(symbol="SPY", interval="30min", limit=10,
                                base_url="https://x", api_key="k", session=session,
                                retries=2, backoff_seconds=0)
    assert session.calls == 2


def test_a_rate_limit_is_still_retried(monkeypatch):
    monkeypatch.setattr(twelvedata.time, "sleep", lambda *_: None)
    session = _Walk(good=0, status=429, message="You have run out of API credits")
    with pytest.raises(ExchangeError):
        twelvedata.fetch_klines(symbol="SPY", interval="30min", limit=10,
                                base_url="https://x", api_key="k", session=session,
                                retries=3, backoff_seconds=0)
    assert session.calls == 3

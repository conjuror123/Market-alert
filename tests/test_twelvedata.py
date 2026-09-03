import pytest

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
    # hour of the currency pairs and crypto (see meals/bars.to_hourly).
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

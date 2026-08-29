import pytest

from price_monitor.coinbase import MAX_CANDLES_PER_REQUEST, fetch_klines
from price_monitor.models import ExchangeError


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def make_row(t, price):
    return [t, price - 0.5, price + 0.5, price - 0.1, price, 10.0]


class FakeSession:
    """Records every call and answers with rows built from `open_time_by_call`."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params, timeout, headers):
        self.calls.append(dict(params))
        status, rows = self.responses.pop(0)
        return FakeResponse(status, rows)


def test_single_request_when_limit_within_cap():
    rows = [make_row(t, 100.0 + t) for t in range(0, 5 * 3600, 3600)]
    sess = FakeSession([(200, rows)])

    candles = fetch_klines("BTC-USD", "1h", limit=5, base_url="https://x", session=sess)

    assert len(sess.calls) == 1
    assert "start" not in sess.calls[0]
    assert [c.open_time for c in candles] == [t for t in range(0, 5 * 3600, 3600)]


def test_limit_above_cap_pages_across_multiple_requests():
    # Two windows are enough to cover MAX_CANDLES_PER_REQUEST + 10 candles.
    chunk1 = [make_row(t, 100.0) for t in range(0, MAX_CANDLES_PER_REQUEST * 3600, 3600)]
    chunk2 = [make_row(t, 200.0) for t in range(-10 * 3600, 0, 3600)]
    sess = FakeSession([(200, chunk1), (200, chunk2)])

    limit = MAX_CANDLES_PER_REQUEST + 10
    candles = fetch_klines("BTC-USD", "1h", limit=limit, base_url="https://x",
                            session=sess, request_delay_seconds=0)

    assert len(sess.calls) == 2
    assert all("start" in call and "end" in call for call in sess.calls)
    assert len(candles) == limit
    assert candles == sorted(candles, key=lambda c: c.open_time)
    # No duplicate candles across the overlapping page boundary.
    assert len({c.open_time for c in candles}) == limit


def test_pagination_stops_once_enough_candles_collected():
    # A single oversized chunk already covers the requested limit - no second call needed.
    limit = MAX_CANDLES_PER_REQUEST + 1
    chunk1 = [make_row(t, 100.0) for t in range(0, (limit + 50) * 3600, 3600)]
    sess = FakeSession([(200, chunk1)])

    candles = fetch_klines("BTC-USD", "1h", limit=limit, base_url="https://x",
                            session=sess, request_delay_seconds=0)

    assert len(sess.calls) == 1
    assert len(candles) == limit


def test_non_200_status_raises_after_retries():
    sess = FakeSession([(500, "boom"), (500, "boom"), (500, "boom")])
    with pytest.raises(ExchangeError):
        fetch_klines("BTC-USD", "1h", limit=5, base_url="https://x",
                     session=sess, retries=3, backoff_seconds=0)

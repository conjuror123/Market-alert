"""Yahoo client: the shapes that would otherwise reach the store unnoticed."""
import json

import pytest
import requests

from price_monitor import yahoo
from price_monitor.models import ExchangeError


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload, self.status_code = payload, status
        self.text = text or json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Session:
    def __init__(self, *responses):
        self._responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _chart(stamps, opens, highs, lows, closes, volumes):
    return {"chart": {"error": None, "result": [{
        "timestamp": stamps,
        "indicators": {"quote": [{"open": opens, "high": highs, "low": lows,
                                  "close": closes, "volume": volumes}]}}]}}


def test_a_bar_is_parsed_onto_the_open_time():
    s = _Session(_Resp(_chart([1000], [1.0], [2.0], [0.5], [1.5], [10])))
    out = yahoo.fetch_full_history("SPY", "30min", days=1, session=s)
    assert len(out) == 1
    c = out[0]
    assert (c.open_time, c.open, c.high, c.low, c.close, c.volume) == (
        1000, 1.0, 2.0, 0.5, 1.5, 10.0)
    assert c.close_time == 1000 + 1800


def test_the_closing_stub_is_dropped():
    """Yahoo appends a zero-volume, zero-width bar at the 20:00 UTC close.

    Twelve Data does not produce it, so keeping it would open an hourly bucket
    on every ETF on every run that the session calendar does not expect.
    """
    payload = _chart([1000, 2800], [1.0, 1.5], [2.0, 1.5], [0.5, 1.5],
                     [1.5, 1.5], [10, 0])
    s = _Session(_Resp(payload))
    out = yahoo.fetch_full_history("SPY", "30min", days=1, session=s)
    assert [c.open_time for c in out] == [1000]


def test_a_flat_bar_that_actually_traded_is_kept():
    """Zero width alone is not the stub signature - a thin fund can print every
    trade of a half hour at one price. Volume is what separates them."""
    payload = _chart([1000], [7.0], [7.0], [7.0], [7.0], [250])
    s = _Session(_Resp(payload))
    out = yahoo.fetch_full_history("CPER", "30min", days=1, session=s)
    assert len(out) == 1 and out[0].volume == 250.0


def test_a_null_row_is_a_hole_not_a_failure():
    payload = _chart([1000, 2000, 3000], [1.0, None, 3.0], [2.0, None, 4.0],
                     [0.5, None, 2.0], [1.5, None, 3.5], [10, None, 30])
    s = _Session(_Resp(payload))
    out = yahoo.fetch_full_history("SPY", "30min", days=1, session=s)
    assert [c.open_time for c in out] == [1000, 3000]


def test_an_unknown_ticker_is_not_retried():
    s = _Session(_Resp({}, status=404, text="Not Found"))
    with pytest.raises(ExchangeError, match="unknown to Yahoo"):
        yahoo.fetch_full_history("NOPE", "30min", days=1, session=s)
    assert len(s.calls) == 1


def test_a_rate_limit_is_retried(monkeypatch):
    monkeypatch.setattr(yahoo.time, "sleep", lambda *_: None)
    good = _Resp(_chart([1000], [1.0], [2.0], [0.5], [1.5], [10]))
    s = _Session(_Resp({}, status=429, text="too many"), good)
    out = yahoo.fetch_full_history("SPY", "30min", days=1, session=s)
    assert len(out) == 1 and len(s.calls) == 2


def test_a_lookback_past_what_yahoo_serves_is_refused():
    """Asking beyond the interval's limit returns an EMPTY result, which looks
    exactly like a quiet market - so it is refused before it is sent."""
    s = _Session()
    with pytest.raises(ExchangeError, match="at most 55 days"):
        yahoo.fetch_full_history("SPY", "30min", days=400, session=s)
    assert s.calls == []


def test_the_window_is_sent_as_epoch_seconds():
    s = _Session(_Resp(_chart([], [], [], [], [], [])))
    yahoo.fetch_full_history("SPY", "1h", days=2, session=s)
    params = s.calls[0]["params"]
    assert params["interval"] == "1h"
    assert params["period2"] - params["period1"] == 2 * 86400


def test_a_charted_error_is_raised():
    s = _Session(_Resp({"chart": {"error": {"code": "Not Found",
                                            "description": "No data found"},
                                  "result": None}}))
    with pytest.raises(ExchangeError, match="No data found"):
        yahoo.fetch_full_history("SPY", "30min", days=1, session=s)


def test_an_unsupported_interval_is_rejected():
    with pytest.raises(ExchangeError, match="Unsupported Yahoo interval"):
        yahoo.fetch_full_history("SPY", "5min", days=1, session=_Session())

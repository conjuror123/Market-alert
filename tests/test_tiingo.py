"""Tiingo client: the request it sends, and the answers that must not be retried."""
import json

import pytest

from price_monitor import tiingo
from price_monitor.models import ExchangeError


class _Resp:
    def __init__(self, payload, status=200, text="", headers=None):
        self._payload, self.status_code = payload, status
        self.text = text or json.dumps(payload) if payload is not None else text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Session:
    def __init__(self, *responses):
        self._responses, self.calls = list(responses), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self._responses.pop(0)


ROW = {"date": "2026-09-14T13:30:00.000Z", "open": 1.0, "high": 2.0,
       "low": 0.5, "close": 1.5, "volume": 100}


def test_a_row_is_parsed_as_utc():
    s = _Session(_Resp([ROW]))
    out = tiingo.fetch_full_history("SPY", "30min", days=1, api_key="k", session=s)
    assert len(out) == 1
    # 2026-09-14T13:30:00Z
    assert out[0].open_time == 1789392600
    assert out[0].close_time == out[0].open_time + 1800
    assert out[0].volume == 100.0


def test_volume_is_asked_for_by_name():
    """The intraday response carries no volume field unless columns names it."""
    s = _Session(_Resp([ROW]))
    tiingo.fetch_full_history("SPY", "30min", days=1, api_key="k", session=s)
    assert s.calls[0]["params"]["columns"] == "open,high,low,close,volume"


def test_a_start_date_is_always_sent():
    """Without it the intraday endpoint answers with the current day only,
    which would silently truncate a gap fill."""
    s = _Session(_Resp([ROW]))
    tiingo.fetch_full_history("SPY", "30min", days=3, api_key="k", session=s)
    assert s.calls[0]["params"]["startDate"]


def test_a_pair_goes_to_the_fx_endpoint_in_its_own_spelling():
    s = _Session(_Resp([{"date": "2026-09-14T13:00:00.000Z", "open": 1.1,
                         "high": 1.2, "low": 1.0, "close": 1.15}]))
    out = tiingo.fetch_full_history("EUR/USD", "1h", days=1, api_key="k", session=s)
    assert "/tiingo/fx/eurusd/prices" in s.calls[0]["url"]
    assert s.calls[0]["params"]["resampleFreq"] == "1hour"
    # No volume for a currency pair, and that is expected rather than missing.
    assert out[0].volume == 0.0
    assert "columns" not in s.calls[0]["params"]


def test_an_etf_goes_to_the_iex_endpoint():
    s = _Session(_Resp([ROW]))
    tiingo.fetch_full_history("SPY", "30min", days=1, api_key="k", session=s)
    assert "/iex/SPY/prices" in s.calls[0]["url"]


def test_the_key_travels_in_the_header_not_the_query():
    s = _Session(_Resp([ROW]))
    tiingo.fetch_full_history("SPY", "30min", days=1, api_key="secret", session=s)
    assert s.calls[0]["headers"]["Authorization"] == "Token secret"
    assert "token" not in (s.calls[0]["params"] or {})


def test_a_rate_limit_stops_rather_than_retrying():
    """Both the 50-an-hour and the 1000-a-day limit answer 429, and neither
    clears inside a run - so retrying only spends round trips."""
    s = _Session(_Resp(None, status=429, text="too many requests"))
    with pytest.raises(tiingo.RateLimited):
        tiingo.fetch_full_history("SPY", "30min", days=1, api_key="k", session=s)
    assert len(s.calls) == 1


def test_a_rate_limit_carries_remaining_headroom_when_the_header_is_present():
    s = _Session(_Resp(None, status=429, text="too many requests",
                       headers={"X-RateLimit-Remaining": "0"}))
    with pytest.raises(tiingo.RateLimited) as caught:
        tiingo.fetch_full_history("SPY", "30min", days=1, api_key="k", session=s)
    assert caught.value.remaining == "0"


def test_a_missing_key_is_refused_before_the_request():
    s = _Session()
    with pytest.raises(ExchangeError, match="No Tiingo API key"):
        tiingo.fetch_full_history("SPY", "30min", days=1, api_key="", session=s)
    assert s.calls == []


def test_a_server_fault_is_retried(monkeypatch):
    monkeypatch.setattr(tiingo.time, "sleep", lambda *_: None)
    s = _Session(_Resp(None, status=503, text="nope"), _Resp([ROW]))
    out = tiingo.fetch_full_history("SPY", "30min", days=1, api_key="k", session=s)
    assert len(out) == 1 and len(s.calls) == 2


def test_a_row_missing_a_price_is_skipped():
    s = _Session(_Resp([ROW, {"date": "2026-09-14T14:00:00.000Z", "open": None,
                              "high": 1.0, "low": 1.0, "close": 1.0}]))
    out = tiingo.fetch_full_history("SPY", "30min", days=1, api_key="k", session=s)
    assert len(out) == 1


def test_fx_ticker_spelling():
    assert tiingo.fx_ticker("EUR/USD") == "eurusd"
    assert tiingo.fx_ticker("USD_JPY") == "usdjpy"
    assert tiingo.is_fx("EUR/USD") and not tiingo.is_fx("SPY")


DAILY_ROW = {
    "date": "2025-12-05T00:00:00.000Z",
    "close": 145.53,
    "adjClose": 145.53,
    "divCash": 0.0,
    "splitFactor": 2.0,
}


def test_daily_history_uses_the_daily_endpoint_and_does_not_ask_for_columns():
    from datetime import date

    s = _Session(_Resp([DAILY_ROW]))
    out = tiingo.fetch_daily_history("XLK", date(2025, 12, 1),
                                     api_key="k", session=s,
                                     end=date(2025, 12, 10))
    assert "/tiingo/daily/XLK/prices" in s.calls[0]["url"]
    assert "columns" not in s.calls[0]["params"]
    assert s.calls[0]["params"]["startDate"] == "2025-12-01"
    assert s.calls[0]["params"]["endDate"] == "2025-12-10"
    assert len(out) == 1
    assert out[0].day == date(2025, 12, 5)
    assert out[0].close == 145.53
    assert out[0].split_factor == 2.0
    assert out[0].div_cash == 0.0

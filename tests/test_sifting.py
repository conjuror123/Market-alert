"""SiftingIO client: the request it sends, the pages it follows, and the
answers that must not be retried."""
import json

import pytest

from price_monitor import sifting
from price_monitor.models import ExchangeError


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self._payload, self.status_code = payload, status
        self.text = json.dumps(payload) if payload is not None else ""
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Session:
    def __init__(self, *responses):
        self._responses, self.calls = list(responses), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": headers})
        return self._responses.pop(0)


# 2026-09-30T02:00:00Z, the OPEN of the bar
BAR = {"t": 1790733600000, "o": 17.1, "h": 17.3, "l": 17.0, "c": 17.2, "v": 0}


def page(rows, cursor=None):
    return _Resp({"data": rows, "meta": {"symbol": "USDMXN", "interval": "1h",
                                         "next_cursor": cursor}})


def test_a_pair_is_asked_in_its_own_spelling_with_the_key_and_gzip():
    s = _Session(page([BAR]))
    sifting.fetch_full_history("USD/MXN", "1h", days=1, api_key="k", session=s)
    call = s.calls[0]
    assert call["url"].endswith("/hist/forex/USDMXN/bars")
    assert call["headers"]["X-API-Key"] == "k"
    assert "gzip" in call["headers"]["Accept-Encoding"]      # 406 without it
    assert call["params"]["interval"] == "1h"
    assert call["params"]["start"].endswith("Z") and call["params"]["end"].endswith("Z")


def test_a_bar_is_stamped_at_its_open_in_seconds():
    out = sifting.fetch_full_history("USD/MXN", "1h", days=1, api_key="k",
                                     session=_Session(page([BAR])))
    assert out[0].open_time == 1790733600
    assert out[0].close_time == 1790737200
    assert out[0].close == 17.2


def test_the_pages_are_followed_to_the_end():
    s = _Session(page([BAR], cursor="abc"), page([dict(BAR, t=BAR["t"] + 3600000)]))
    out = sifting.fetch_full_history("USD/MXN", "1h", days=30, api_key="k", session=s)
    assert len(out) == 2
    assert s.calls[1]["params"]["cursor"] == "abc"


def test_a_fund_goes_to_the_stock_endpoint_at_thirty_minutes():
    s = _Session(page([BAR]))
    sifting.fetch_full_history("SPY", "30min", days=1, api_key="k", session=s)
    assert s.calls[0]["url"].endswith("/hist/stocks/SPY/bars")
    assert s.calls[0]["params"]["interval"] == "30m"


def test_a_spent_budget_stops_rather_than_retries():
    s = _Session(_Resp({"error": "quota"}, status=429, headers={"X-Quota-Remaining": "0"}))
    with pytest.raises(sifting.RateLimited) as caught:
        sifting.fetch_full_history("USD/MXN", "1h", days=1, api_key="k", session=s)
    assert caught.value.remaining == "0"
    assert len(s.calls) == 1


def test_a_rejected_request_is_not_retried():
    s = _Session(_Resp({"error": "no"}, status=406))
    with pytest.raises(ExchangeError):
        sifting.fetch_full_history("USD/MXN", "1h", days=1, api_key="k", session=s)
    assert len(s.calls) == 1


def test_no_key_is_an_error_not_a_quiet_market():
    with pytest.raises(ExchangeError):
        sifting.fetch_full_history("USD/MXN", "1h", days=1, api_key="")

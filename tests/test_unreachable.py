"""A provider that does not answer: what each client raises once its retries are
spent, so the run can stop asking it (tremor.backfill, tremor.verify)."""
import pytest
import requests

from price_monitor import alpaca, binance, google, marketwatch, sifting, sina, tiingo, yahoo
from price_monitor.models import ExchangeError, Unreachable


class Answer:
    def __init__(self, status):
        self.status_code, self.text, self.headers = status, "", {}

    def json(self):
        return {}


class Session:
    """Every request times out, or is answered with `status`."""

    def __init__(self, status=None):
        self.status, self.calls = status, 0

    def get(self, *a, **k):
        self.calls += 1
        if self.status is None:
            raise requests.ReadTimeout("Read timed out. (read timeout=30)")
        return Answer(self.status)


CALLS = {
    "yahoo": (yahoo, lambda s: yahoo._request(s, "u", {}, 3600, "X")),
    "tiingo": (tiingo, lambda s: tiingo._get_json(s, "u", {}, "k", "X")),
    "sifting": (sifting, lambda s: sifting._get(s, "u", {}, "k", "X")),
    "alpaca": (alpaca, lambda s: alpaca._get(s, "u", {}, {}, "X")),
    "binance": (binance, lambda s: binance._get(s, "X", 0)),
    "google": (google, lambda s: google.fetch_full_history("TUR", "30min", 1, session=s)),
    "sina global": (sina, lambda s: sina.fetch_bars("X", s)),
    "sina us": (sina, lambda s: sina.fetch_us_bars("X", s)),
    "marketwatch": (marketwatch, lambda s: marketwatch.fetch_hourly("X", s)),
}


@pytest.mark.parametrize("status", [None, 503], ids=["timeout", "503"])
@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_provider_that_never_answers_is_unreachable(name, status, monkeypatch):
    module, call = CALLS[name]
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    session = Session(status)
    with pytest.raises(Unreachable):
        call(session)
    assert session.calls == module.MAX_ATTEMPTS


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_refusal_is_an_answer(name, monkeypatch):
    # A 404 is the provider answering about one symbol; it says nothing about
    # whether the next instrument will be answered.
    module, call = CALLS[name]
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    with pytest.raises(ExchangeError) as caught:
        call(Session(404))
    assert not isinstance(caught.value, Unreachable)

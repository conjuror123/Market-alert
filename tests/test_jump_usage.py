"""jump.usage: what one fetch asked of each provider, counted on the session."""
import logging
from datetime import date

import requests
from requests.adapters import BaseAdapter

from jump.usage import Usage


class Answers(BaseAdapter):
    """Answers every request itself, with `status` and `headers`: no network."""

    def __init__(self, status=200, headers=None):
        super().__init__()
        self.status, self.headers = status, headers or {}

    def send(self, request, **kwargs):
        response = requests.Response()
        response.status_code, response.url = self.status, request.url
        response.headers.update(self.headers)
        response._content = b"{}"
        return response

    def close(self):
        pass


def _ask(session, url, **answer):
    session.mount("https://", Answers(**answer))
    return session.get(url)


def test_answers_are_counted_by_provider_with_the_quota_left():
    usage = Usage()
    s = usage.session()
    _ask(s, "https://api.tiingo.com/iex/spy", headers={"X-RateLimit-Remaining": "4973"})
    _ask(s, "https://api.tiingo.com/iex/qqq", headers={"X-RateLimit-Remaining": "4972"})
    _ask(s, "https://api.sifting.io/v1/fx", status=429, headers={"X-Quota-Remaining": "0"})
    _ask(s, "https://query1.finance.yahoo.com/v8/finance/chart/CT=F")
    _ask(s, "https://example.org/x")
    assert usage.line() == ("requests: tiingo 2 (left 4972), example.org 1, "
                            "sifting 1 (left 0), yahoo 1")


def test_a_thread_has_its_own_session_on_the_same_count():
    usage = Usage()
    _ask(usage.session(), "https://api.twelvedata.com/time_series",
         headers={"api-credits-left": "760"})
    _ask(usage.session(), "https://data-api.binance.vision/api/v3/klines")
    assert usage.line() == "requests: binance 1, twelvedata 1 (left 760)"


def test_nothing_asked_says_so():
    assert Usage().line() == "requests: none"


def test_the_hourly_pass_logs_what_its_session_asked_once(tmp_path, monkeypatch, caplog):
    # Through main's own session: a fetch on a plain requests.Session would
    # log "requests: none".
    from jump import backfill
    from jump.basket import Asset, Basket, VolatilityIndex

    def fake_backfill(asset, basket, bars_dir, api_key, session, *a, **k):
        _ask(session, "https://data-api.binance.vision/api/v3/klines")
        return {"asset_id": asset.asset_id, "rows": 1, "first": 1, "last": 1,
                "from_api": 1}

    basket = Basket(
        assets=(Asset(ticker="BTC/USDT", source="binance", block="crypto",
                      has_volume=True, tick_size=0.01, session_template="crypto_24_7",
                      fetch_interval="1h", label="Bitcoin", in_basket=True),),
        outside=(),
        volatility_index=VolatilityIndex(
            "VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York",
        history_since=date(2021, 1, 1), session_templates={"crypto_24_7": {}},
    )
    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    monkeypatch.setattr(backfill, "backfill_instrument", fake_backfill)
    monkeypatch.setattr(backfill._sessions, "load_sessions",
                        lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(backfill, "send_ops_alert", lambda text: None)

    with caplog.at_level(logging.INFO, logger="jump.backfill"):
        assert backfill.main(["--skip-vix", "--bars-dir", str(tmp_path)]) == 0

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("requests:")]
    assert lines == ["requests: binance 1"]

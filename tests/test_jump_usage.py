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


def test_both_cboe_hosts_are_cboe():
    usage = Usage()
    _ask(usage.session(), "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv")
    _ask(usage.session(), "https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/_VIX.json")
    assert usage.line() == "requests: cboe 2"


def test_wallstreetcns_host_is_wallstreetcn():
    usage = Usage()
    _ask(usage.session(), "https://api-ddc-wscn.awtmt.com/market/kline?prod_code=UKSN.OTC")
    assert usage.line() == "requests: wallstreetcn 1"


def test_the_fts_host_is_ft():
    usage = Usage()
    _ask(usage.session(), "https://markets.ft.com/data/chartapi/series")
    assert usage.line() == "requests: ft 1"


def test_calls_are_counted_by_purpose_and_twelve_datas_in_credits():
    # A batch of seven funds is one request and seven credits; the vote's calls are
    # its own, apart from the fetch's.
    usage = Usage()
    _ask(usage.session(), "https://api.twelvedata.com/time_series?symbol=A,B,C,D,E,F,G",
         headers={"api-credits-left": "793"})
    _ask(usage.session("votes"), "https://api.tiingo.com/iex/spy")
    _ask(usage.session("votes"), "https://api.tiingo.com/iex/qqq")
    _ask(usage.session(), "https://api.twelvedata.com/time_series?symbol=UNG",
         headers={"api-credits-used": "1"})
    assert usage.spent == {("twelvedata", "fetch"): 8, ("tiingo", "votes"): 2}
    assert usage.line() == "requests: tiingo 2, twelvedata 2 (left 793)"


def test_an_unknown_purpose_is_refused():
    import pytest
    with pytest.raises(ValueError):
        Usage().session("sightseeing")


def test_a_runs_count_is_added_to_its_days_rows(tmp_path):
    from datetime import datetime, timezone

    from jump import usage as u
    path = str(tmp_path / "hourly.csv")
    first = Usage()
    _ask(first.session(), "https://api.twelvedata.com/time_series?symbol=UNG",
         headers={"api-credits-left": "799"})
    first.record(path, now=datetime(2026, 10, 12, 9, 5, tzinfo=timezone.utc))
    second = Usage()
    _ask(second.session(), "https://api.twelvedata.com/time_series?symbol=UNG",
         headers={"api-credits-left": "798"})
    _ask(second.session("votes"), "https://api.sifting.io/v1/fx")
    second.record(path, now=datetime(2026, 10, 12, 10, 5, tzinfo=timezone.utc))
    third = Usage()
    _ask(third.session(), "https://api.twelvedata.com/time_series?symbol=UNG")
    third.record(path, now=datetime(2026, 10, 13, 0, 5, tzinfo=timezone.utc))
    table = u.read(path)
    rows = {(r.day, r.provider, r.purpose): (r.calls, r.left, r.left_at)
            for r in table.itertuples()}
    assert rows == {("2026-10-12", "twelvedata", "fetch"): (2, "798", "10:05"),
                    ("2026-10-12", "sifting", "votes"): (1, "", ""),
                    ("2026-10-13", "twelvedata", "fetch"): (1, "", "")}


def test_no_usage_file_named_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("USAGE_FILE", raising=False)
    usage = Usage()
    _ask(usage.session(), "https://api.tiingo.com/iex/spy")
    assert usage.record() is None and list(tmp_path.iterdir()) == []


def test_every_workflows_file_is_read_together(tmp_path):
    from jump import usage as u
    (tmp_path / "hourly.csv").write_text(
        "day,provider,purpose,calls,left,left_at\n2026-10-12,tiingo,fetch,90,,\n")
    (tmp_path / "manual.csv").write_text(
        "day,provider,purpose,calls,left,left_at\n2026-10-12,tiingo,manual,12,,\n")
    table = u.read_all(str(tmp_path))
    from datetime import datetime, timezone
    assert u.spent_in(table, "tiingo", "day", datetime(2026, 10, 12, 23, tzinfo=timezone.utc)) == 102


def test_the_limits_file_reads_an_undecided_share_as_none(tmp_path):
    from jump import usage as u
    table = u.limits()
    tiingo_day = table[(table.provider == "tiingo") & (table.window == "day")].iloc[0]
    assert tiingo_day.limit == 1000
    assert set(table.window) <= {"minute", "hour", "day", "month"}
    # Each window's shares fit its limit, but a minute's, which the hourly run,
    # a walk and the vote use in different minutes (config/provider_limits.csv).
    for row in table[table.window != "minute"].itertuples():
        assert row.live + row.manual + row.dashboard + row.votes <= row.limit, row
    path = tmp_path / "limits.csv"
    path.write_text("provider,window,limit,live,manual,dashboard,votes,note\n"
                    "tiingo,day,1000,240,200,0,,\n")
    assert u.limits(str(path)).iloc[0].votes == 0


def test_the_votes_left_are_each_windows_share_less_what_the_vote_spent(tmp_path):
    # Tiingo's day share of 100 with 95 voted today, its hour's 14 this run's
    # own: 5 left. SiftingIO's month share, less this month's votes only - not
    # its fetches, nor last month's. Alpaca's minute is a pace, not a count.
    from datetime import datetime, timezone
    from jump import usage as u
    limits = tmp_path / "limits.csv"
    limits.write_text("provider,window,limit,live,manual,dashboard,votes,note\n"
                      "tiingo,hour,50,10,15,11,14,\ntiingo,day,1000,240,200,110,100,\n"
                      "sifting,month,10000,4700,1000,300,300,\n"
                      "twelvedata,day,800,24,600,70,,\nalpaca,minute,200,0,100,50,50,\n")
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()
    (usage_dir / "hourly.csv").write_text(
        "day,provider,purpose,calls,reported,left,left_at\n"
        "2026-10-12,tiingo,votes,40,,,\n2026-10-13,tiingo,votes,95,,,\n"
        "2026-10-13,tiingo,fetch,200,,,\n2026-09-30,sifting,votes,250,,,\n"
        "2026-10-02,sifting,votes,20,,,\n2026-10-13,sifting,fetch,300,,,\n")
    left = u.votes_left(datetime(2026, 10, 13, 12, tzinfo=timezone.utc), str(limits),
                        str(usage_dir))
    assert left == {"tiingo": 5, "sifting": 280, "twelvedata": 0, "alpaca": float("inf")}


def _table(rows):
    import pandas as pd
    from jump import usage as u
    return pd.DataFrame(rows, columns=u.COLUMNS).astype({"calls": int})


def test_a_day_past_four_fifths_of_its_limit_is_named_once():
    from datetime import datetime, timezone

    import pandas as pd
    from jump import usage as u
    now = datetime(2026, 10, 12, 18, tzinfo=timezone.utc)
    limits = pd.DataFrame([{"provider": "tiingo", "window": "day", "limit": 1000, "live": 0,
                            "manual": 0, "dashboard": 0, "votes": 0}])
    before = _table([["2026-10-12", "tiingo", "fetch", 790, "", "", ""]])
    after = _table([["2026-10-12", "tiingo", "fetch", 810, "", "", ""]])
    later = _table([["2026-10-12", "tiingo", "fetch", 830, "", "", ""]])
    assert u.warnings(before, after, limits, now) == ["tiingo: 810 of 1,000 this day (81%)"]
    assert u.warnings(after, later, limits, now) == []


def test_sifting_is_counted_by_its_month():
    from datetime import datetime, timezone

    import pandas as pd
    from jump import usage as u
    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    limits = pd.DataFrame([{"provider": "sifting", "window": "month", "limit": 10000,
                            "live": 0, "manual": 0, "dashboard": 0, "votes": 0}])
    before = _table([["2026-09-30", "sifting", "fetch", 9000, "", "", ""],
                     ["2026-10-29", "sifting", "fetch", 7990, "", "", ""]])
    after = _table([["2026-09-30", "sifting", "fetch", 9000, "", "", ""],
                    ["2026-10-29", "sifting", "fetch", 7990, "", "", ""],
                    ["2026-10-30", "sifting", "fetch", 20, "", "", ""]])
    assert u.warnings(before, after, limits, now) == ["sifting: 8,010 of 10,000 this month (80%)"]


def test_twelve_datas_count_ahead_of_ours_is_named_once():
    # Its usage endpoint says 40 credits today when we counted 10: another caller on
    # the key, or one of ours not counted.
    from datetime import datetime, timezone

    import pandas as pd
    from jump import usage as u
    now = datetime(2026, 10, 12, 23, 5, tzinfo=timezone.utc)
    limits = pd.DataFrame([{"provider": "twelvedata", "window": "day", "limit": 800,
                            "live": 0, "manual": 0, "dashboard": 0, "votes": 0}])
    before = _table([["2026-10-12", "twelvedata", "fetch", 9, "", "", ""]])
    after = _table([["2026-10-12", "twelvedata", "fetch", 10, "40", "", ""]])
    assert u.warnings(before, after, limits, now) == [
        "twelvedata counts 40 today, we counted 10: another caller on the key, "
        "or one of ours not counted"]
    again = _table([["2026-10-12", "twelvedata", "fetch", 11, "41", "", ""]])
    assert u.warnings(after, again, limits, now) == []


def test_twelve_data_is_asked_its_day_once_at_the_report_hour(monkeypatch):
    # Its usage endpoint costs a credit: the 23:00 UTC run only.
    from datetime import datetime, timezone

    from jump import usage as u
    seen = []

    class Reply:
        def json(self):
            return {"daily_usage": 67, "plan_daily_limit": 800}

    def get(self, url, params=None, timeout=None):
        seen.append(url)
        return Reply()

    monkeypatch.setattr(requests.Session, "get", get)
    usage = Usage()
    u.ask_twelvedata(usage, "key", now=datetime(2026, 10, 12, 22, 5, tzinfo=timezone.utc))
    assert seen == [] and usage.reported == {}
    u.ask_twelvedata(usage, "key", now=datetime(2026, 10, 12, 23, 5, tzinfo=timezone.utc))
    assert usage.reported == {"twelvedata": 67}
    usage.spent[("twelvedata", "fetch")] += 7
    row = usage.rows(datetime(2026, 10, 12, 23, 6, tzinfo=timezone.utc)).iloc[0]
    assert row["reported"] == "67" and row["calls"] == 7


def test_the_hourly_run_records_its_count_and_names_what_it_crossed(tmp_path, monkeypatch):
    # Through main: the count lands in the workflow's usage file whatever the run
    # did, and what it crossed goes to the health chat.
    from jump import backfill
    from jump import usage as u

    usage_file = tmp_path / "usage" / "hourly.csv"
    usage_file.parent.mkdir()
    usage_file.write_text("day,provider,purpose,calls,left,left_at\n")
    monkeypatch.setenv("USAGE_FILE", str(usage_file))
    sent = []
    monkeypatch.setattr(backfill, "send_ops_alert", sent.append)
    monkeypatch.setattr(u, "limits", lambda path=None: __import__("pandas").DataFrame(
        [{"provider": "binance", "window": "day", "limit": 1, "live": 0, "manual": 0,
          "dashboard": 0, "votes": 0}]))

    def run(argv, usage):
        _ask(usage.session(), "https://data-api.binance.vision/api/v3/klines")
        return 0

    monkeypatch.setattr(backfill, "_main", run)
    assert backfill.main([]) == 0
    table = u.read(str(usage_file))
    assert list(table[["provider", "purpose", "calls"]].itertuples(index=False, name=None)) \
        == [("binance", "fetch", 1)]
    assert len(sent) == 1 and "binance: 1 of 1 this day (100%)" in sent[0]

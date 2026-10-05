import os
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from tremor import bars
from tremor.basket import Asset
from price_monitor.models import Candle

HOUR = 3600


def asset(**over):
    base = dict(ticker="EUR/USD", source="twelvedata", block="FX",
                has_volume=False, tick_size=0.00001, session_template="fx_continuous",
                fetch_interval="1h", label="Euro / dollar", in_basket=True)
    return Asset(**(base | over))


class _Rows:
    """What a provider's candles endpoint looks like to its client: status and json."""

    def __init__(self, rows):
        self.status_code = 200
        self._rows = rows
        self.text = str(rows)

    def json(self):
        return self._rows


def candle(hour, close=1.5):
    return Candle(open_time=hour * HOUR, open=1.0, high=2.0, low=0.5,
                  close=close, volume=0.0, close_time=(hour + 1) * HOUR)


def test_deepening_starts_at_the_oldest_stored_bar_not_at_today(tmp_path, monkeypatch):
    # The walk goes backwards, so starting at today spends a credit per chunk
    # re-fetching years already on disk before reaching any new ground. On the
    # free tier's 800 a day that is the difference between reaching 2015 and
    # running out somewhere in 2019.
    import pandas as pd

    from tremor import backfill

    stored_oldest = int(datetime(2021, 6, 1, tzinfo=timezone.utc).timestamp())
    path = tmp_path / "twelvedata_SPY.parquet"
    bars.merge(str(path), bars.to_hourly(bars.candles_to_frame([
        Candle(open_time=stored_oldest + i * HOUR, open=1.0, high=1.0, low=1.0,
               close=1.0, volume=0.0, close_time=stored_oldest + (i + 1) * HOUR)
        for i in range(2)])))

    seen = {}

    def fake_history(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history", fake_history)
    asset = Asset(ticker="SPY", source="twelvedata", block="equity",
                  has_volume=True, tick_size=0.01, session_template="us_equity",
                  fetch_interval="30min", label="S&P 500", in_basket=True)

    backfill.fetch_missing(asset, str(path), date(2015, 1, 1), "key", None,
                           extend_history=True)

    assert seen["end"] == datetime.fromtimestamp(stored_oldest, tz=timezone.utc)
    # and it asks for the span from `since` up to that bar, not up to today
    assert 2340 < seen["days"] < 2360        # 2015-01-01 to 2021-06-01


def test_a_first_ever_fetch_still_walks_back_from_today(tmp_path, monkeypatch):
    from tremor import backfill

    seen = {}

    def fake_history(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history", fake_history)
    asset = Asset(ticker="SPY", source="twelvedata", block="equity",
                  has_volume=True, tick_size=0.01, session_template="us_equity",
                  fetch_interval="30min", label="S&P 500", in_basket=True)

    backfill.fetch_missing(asset, str(tmp_path / "nope.parquet"), date(2015, 1, 1),
                           "key", None, extend_history=True)
    assert seen["end"] is None


def test_a_forward_fetch_asks_from_the_settle_window_not_an_extra_day(
        tmp_path, monkeypatch):
    from tremor import backfill

    asked = {}

    def fake_history(**kwargs):
        asked.update(kwargs)
        return []

    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history", fake_history)
    newest = datetime(2026, 4, 4, 10, tzinfo=timezone.utc)
    now = datetime(2026, 4, 4, 15, tzinfo=timezone.utc)
    path = tmp_path / "twelvedata_SPY.parquet"
    bars.merge(str(path), bars.to_hourly(bars.candles_to_frame([
        Candle(open_time=int(newest.timestamp()), open=1.0, high=1.0, low=1.0,
               close=1.0, volume=0.0,
               close_time=int(newest.timestamp()) + HOUR)])))
    asset = Asset(ticker="SPY", source="twelvedata", block="equity",
                  has_volume=True, tick_size=0.01, session_template="us_equity",
                  fetch_interval="30min", label="S&P 500", in_basket=True)

    backfill.fetch_missing(asset, str(path), date(2015, 1, 1), "key", None,
                           now=now)

    # Five hours after the newest bar, plus three hours of settle, not +1 day.
    assert asked["days"] == pytest.approx(
        (5 + backfill.SETTLE_HOURS) / 24.0, abs=1e-9)
    assert asked["days"] < 1.0


def test_the_hourly_crypto_fetch_survives_one_dropped_connection(
        tmp_path, monkeypatch):
    """A reset mid-fetch costs a retry, not the instrument.

    Nothing below fetch_missing is faked on purpose: what is tested is the
    wiring from the hourly fetch through binance.fetch_full_history to the
    client's retry.
    """
    from tremor import backfill
    from price_monitor import binance

    newest = datetime(2026, 9, 21, 1, tzinfo=timezone.utc)
    now = datetime(2026, 9, 21, 2, tzinfo=timezone.utc)
    path = tmp_path / "binance_BTC_USDT"
    bars.merge(str(path), bars.to_hourly(bars.candles_to_frame([
        Candle(open_time=int(newest.timestamp()), open=1.0, high=1.0, low=1.0,
               close=1.0, volume=0.0,
               close_time=int(newest.timestamp()) + HOUR)])))

    fresh = int(now.timestamp())
    calls = []

    class Socket:
        """The real client, a faked wire: the first call is reset, the retry answers."""

        def get(self, url, params, timeout):
            calls.append(dict(params))
            if len(calls) == 1:
                raise requests.exceptions.ConnectionError(
                    "('Connection aborted.', "
                    "ConnectionResetError(104, 'Connection reset by peer'))")
            return _Rows([[fresh * 1000, "1.0", "2.0", "1.5", "1.8", "3.0"]])

    monkeypatch.setattr(binance.time, "sleep", lambda *_: None)
    crypto = Asset(ticker="BTC/USDT", source="binance", block="crypto",
                   has_volume=True, tick_size=0.01,
                   session_template="crypto_24_7", fetch_interval="1h",
                   label="Bitcoin", in_basket=True)

    added = backfill.fetch_missing(crypto, str(path), date(2020, 1, 1), "key",
                                   Socket(), now=now)

    assert len(calls) == 2
    assert added == 1
    assert fresh in set(bars.load(str(path))["hour_utc"])


# --- filling sessions the calendar has and the store does not ---------------

def backfill_runs(days):
    from tremor import backfill
    return backfill._runs(days)


def _etf(**over):
    base = dict(ticker="SPY", source="twelvedata", block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="30min", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def _day(y, m, d):
    return int(datetime(y, m, d, 15, tzinfo=timezone.utc).timestamp())


def _table(days):
    """A calendar of ordinary 09:30-16:00 sessions for the given days."""
    from tremor.sessions import Session
    return {d: Session(day=d, local_open="09:30", local_close="16:00",
                       is_early_close=False)
            for d in days}


def _store_days(path, days):
    bars.merge(str(path), bars.to_hourly(bars.candles_to_frame([
        Candle(open_time=t, open=1.0, high=1.0, low=1.0, close=1.0,
               volume=1.0, close_time=t + HOUR) for t in days])))


def test_only_the_days_between_the_first_and_last_bar_count_as_gaps(tmp_path):
    # A day before the archive starts is not a hole, it is simply outside it.
    # Counting those would bury the real gaps under thousands.
    from tremor import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    _store_days(path, [_day(2024, 3, 4), _day(2024, 3, 6)])
    table = {date(2024, 3, i): object() for i in (1, 4, 5, 6, 7)}

    assert backfill.missing_sessions(str(path), table) == [date(2024, 3, 5)]


def test_no_gaps_when_the_store_matches_the_calendar(tmp_path):
    from tremor import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    _store_days(path, [_day(2024, 3, 4), _day(2024, 3, 5)])
    table = {date(2024, 3, i): object() for i in (4, 5)}
    assert backfill.missing_sessions(str(path), table) == []


def test_consecutive_missing_days_become_one_request():
    runs = backfill_runs([date(2020, 7, 1), date(2020, 7, 2),
                          date(2020, 12, 22)])
    assert runs == [(date(2020, 7, 1), date(2020, 7, 2)),
                    (date(2020, 12, 22), date(2020, 12, 22))]


def test_days_split_by_a_weekend_still_share_one_window():
    # Friday and the following Tuesday are three days apart; asking twice for a
    # span one request already covers is a wasted credit.
    runs = backfill_runs([date(2024, 3, 1), date(2024, 3, 4)])
    assert runs == [(date(2024, 3, 1), date(2024, 3, 4))]


def test_distant_gaps_stay_separate():
    runs = backfill_runs([date(2024, 3, 1), date(2024, 9, 19)])
    assert len(runs) == 2


def test_gap_filling_is_offered_only_where_the_calendar_applies(tmp_path):
    # A currency pair trading around the clock has no "missing Tuesday" to find
    # against the NYSE table.
    from tremor import backfill

    path = tmp_path / "x.parquet"
    out = backfill.fill_gaps(_etf(session_template="fx_continuous"), str(path),
                             {}, "key", None)
    assert out["added"] == 0 and "calendar" in out["skipped"]


def test_a_recovered_day_is_merged_and_no_longer_missing(tmp_path, monkeypatch):
    from tremor import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    _store_days(path, [_day(2024, 3, 4), _day(2024, 3, 6)])
    table = {date(2024, 3, i): object() for i in (4, 5, 6)}
    missing = _day(2024, 3, 5)

    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history",
                        lambda **k: [Candle(open_time=missing, open=1.0, high=1.0,
                                            low=1.0, close=1.0, volume=1.0,
                                            close_time=missing + HOUR)])
    monkeypatch.setattr(backfill.time, "sleep", lambda *_: None)
    out = backfill.fill_gaps(_etf(), str(path), table, "key", None)

    assert out["gaps"] == 1 and out["added"] == 1
    assert out["still_missing"] == []


def test_a_day_the_provider_does_not_hold_is_reported_not_hidden(tmp_path, monkeypatch):
    # An empty answer is the useful one: it confirms the hole is the
    # provider's, not something our own fetching skipped.
    from tremor import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    _store_days(path, [_day(2024, 3, 4), _day(2024, 3, 6)])
    table = {date(2024, 3, i): object() for i in (4, 5, 6)}

    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history", lambda **k: [])
    monkeypatch.setattr(backfill.time, "sleep", lambda *_: None)
    out = backfill.fill_gaps(_etf(), str(path), table, "key", None)

    assert out["gaps"] == 1 and out["added"] == 0
    assert out["still_missing"] == [date(2024, 3, 5)]


def test_a_failed_request_does_not_abandon_the_other_gaps(tmp_path, monkeypatch):
    from tremor import backfill
    from price_monitor.models import ExchangeError

    path = tmp_path / "twelvedata_SPY.parquet"
    # Far apart on purpose: days within three of each other fold into one
    # window by design, and this needs two separate requests to test.
    _store_days(path, [_day(2024, 3, 4), _day(2024, 9, 20)])
    table = {date(2024, 3, 4): object(), date(2024, 3, 5): object(),
             date(2024, 9, 19): object(), date(2024, 9, 20): object()}
    calls = []

    def flaky(**k):
        calls.append(k["end"])
        if len(calls) == 1:
            raise ExchangeError("nope")
        return []

    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history", flaky)
    monkeypatch.setattr(backfill.time, "sleep", lambda *_: None)
    backfill.fill_gaps(_etf(), str(path), table, "key", None)
    assert len(calls) == 2


def test_a_spent_share_stops_the_gap_walk_rather_than_warning_per_gap(tmp_path, monkeypatch):
    # Once the walk's share of the day is spent, every later request would be
    # refused the same way: the walk stops, and says so once (main).
    from tremor import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    _store_days(path, [_day(2024, 3, 4), _day(2024, 9, 20)])
    table = {date(2024, 3, 4): object(), date(2024, 3, 5): object(),
             date(2024, 9, 19): object(), date(2024, 9, 20): object()}
    calls = []

    def spent(**k):
        calls.append(k["end"])
        raise backfill.twelvedata.ArchiveShareSpent("SPY: share spent")

    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history", spent)
    monkeypatch.setattr(backfill.time, "sleep", lambda *_: None)
    with pytest.raises(backfill.twelvedata.DailyQuotaExhausted):
        backfill.fill_gaps(_etf(), str(path), table, "key", None)
    assert len(calls) == 1


def test_bars_fetched_before_the_share_ran_out_are_counted(tmp_path, monkeypatch, caplog):
    # The first gap comes back with a bar, the second meets the spent share:
    # the bar is stored, and the log says so rather than "+0".
    from tremor import backfill
    from tremor.basket import Basket, VolatilityIndex

    asset = _etf()
    path = bars.store_path(str(tmp_path), asset.file_stem)
    _store_days(path, [_day(2024, 3, 4), _day(2024, 3, 6), _day(2024, 9, 20)])
    table = {date(2024, 3, 4): object(), date(2024, 3, 5): object(),
             date(2024, 3, 6): object(), date(2024, 9, 19): object(),
             date(2024, 9, 20): object()}
    recovered = _day(2024, 3, 5)

    def fetch(**k):
        if k["end"].month == 3:
            return [Candle(open_time=recovered, open=1.0, high=1.0, low=1.0, close=1.0,
                           volume=1.0, close_time=recovered + HOUR)]
        raise backfill.twelvedata.ArchiveShareSpent("SPY: share spent")

    basket = Basket(
        assets=(asset,), outside=(),
        volatility_index=VolatilityIndex("VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York", history_since=date(2021, 1, 1),
        session_templates={"us_equity": {}})
    monkeypatch.setenv("TWELVEDATA_API_KEY", "k")
    monkeypatch.delenv("HFDATA_API_KEY", raising=False)
    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    monkeypatch.setattr(backfill._sessions, "load_sessions", lambda: table)
    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history", fetch)
    monkeypatch.setattr(backfill, "missing_hours", lambda path, table: [])
    monkeypatch.setattr(backfill.time, "sleep", lambda *_: None)
    monkeypatch.setattr(backfill.twelvedata, "archive_mode", False)

    with caplog.at_level("INFO", logger="tremor.backfill"):
        backfill.main(["--fill-gaps", "--bars-dir", str(tmp_path)])
    assert recovered in set(bars.load(path)["hour_utc"])
    assert "+1 bars from Twelve Data" in caplog.text


def test_a_gap_walk_told_not_to_ask_only_lists_the_gaps(tmp_path, monkeypatch):
    from tremor import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    _store_days(path, [_day(2024, 3, 4), _day(2024, 3, 6)])
    table = {date(2024, 3, i): object() for i in (4, 5, 6)}
    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history",
                        lambda **k: pytest.fail("asked Twelve Data"))
    out = backfill.fill_gaps(_etf(), str(path), table, "key", None, ask=False)
    assert out["gaps"] == 1 and out["added"] == 0
    assert out["still_missing"] == [date(2024, 3, 5)]


def test_after_the_share_is_spent_the_gaps_go_to_hf_data_only(tmp_path, monkeypatch):
    from tremor import backfill
    from tremor.basket import Basket, VolatilityIndex

    basket = Basket(
        assets=(_etf(ticker="SPY"), _etf(ticker="QQQ"), _etf(ticker="IWM")), outside=(),
        volatility_index=VolatilityIndex("VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York", history_since=date(2021, 1, 1),
        session_templates={"us_equity": {}})
    asked, hf = [], []

    def fake_fill(asset, path, table, api_key, session, ask=True):
        asked.append((asset.ticker, ask))
        if ask and asset.ticker == "QQQ":
            raise backfill.twelvedata.ArchiveShareSpent("QQQ: share spent")
        return {"skipped": None, "added": 0, "gaps": 1, "still_missing": [date(2024, 3, 5)]}

    def fake_hf(asset, path, table, key, session):
        hf.append(asset.ticker)
        return {"skipped": None, "added": 0, "still_missing": [], "still_missing_hours": 0}

    monkeypatch.setenv("TWELVEDATA_API_KEY", "k")
    monkeypatch.setenv("HFDATA_API_KEY", "h")
    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    monkeypatch.setattr(backfill._sessions, "load_sessions", lambda: {})
    monkeypatch.setattr(backfill, "fill_gaps", fake_fill)
    monkeypatch.setattr(backfill, "fill_gaps_from_hfdata", fake_hf)
    monkeypatch.setattr(backfill, "missing_hours", lambda path, table: [])
    monkeypatch.setattr(backfill.twelvedata, "archive_mode", False)

    assert backfill.main(["--fill-gaps", "--bars-dir", str(tmp_path)]) == 0
    assert asked == [("SPY", True), ("QQQ", True), ("QQQ", False), ("IWM", False)]
    assert hf == ["SPY", "QQQ", "IWM"]


def test_a_spent_share_is_reported_as_the_walks_not_the_days(tmp_path, monkeypatch, caplog):
    from tremor import backfill
    from tremor.basket import Basket, VolatilityIndex

    basket = Basket(
        assets=(_yahoo_asset("UGA"),), outside=(),
        volatility_index=VolatilityIndex("VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York", history_since=date(2021, 1, 1),
        session_templates={"us_equity": {}})

    def spent(asset, *a, **k):
        raise backfill.twelvedata.ArchiveShareSpent("UGA: share spent")

    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    monkeypatch.setattr(backfill, "backfill_instrument", spent)
    monkeypatch.setattr(backfill._sessions, "load_sessions",
                        lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(backfill, "send_ops_alert", lambda *a, **k: None)
    monkeypatch.setattr(backfill.twelvedata, "archive_mode", False)

    with caplog.at_level("WARNING", logger="tremor.backfill"):
        backfill.main(["--extend-history", "--skip-vix", "--bars-dir", str(tmp_path)])
    text = caplog.text
    assert "share" in text and "tomorrow" in text and "midnight" not in text


# --- the ETF import, and the check that gates it ---------------------------

def _hf_minutes(start_hour, n, prices=None, step=60):
    import numpy as np
    prices = prices if prices is not None else np.linspace(100.0, 101.0, n)
    return pd.DataFrame({
        "hour_utc": [start_hour + i * step for i in range(n)],
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": [10.0] * n, "n_src": [1] * n,
    })


def test_the_import_is_gated_on_agreeing_with_what_is_already_stored():
    # The years being imported have no second opinion at all. The two years of
    # overlap do, so they are made to earn the rest.
    from tremor import backfill

    base = int(datetime(2021, 1, 4, tzinfo=timezone.utc).timestamp())
    import numpy as np
    rng = np.random.default_rng(4)
    # Enough minutes to clear ALIGNMENT_MIN_HOURS once folded to the hourly grid.
    n = 60 * (backfill.ALIGNMENT_MIN_HOURS + 60)
    walk = 100 * np.exp(np.cumsum(rng.standard_normal(n) * 0.001))

    agreeing = _hf_minutes(base, n, walk)
    stored = bars.to_hourly(agreeing).rename(columns={"close": "close"})
    check = backfill.verify_alignment(agreeing, stored)
    assert check["ok"] and check["correlation"] > 0.99
    assert check["median_bp"] < 1e-6


def test_a_shifted_series_fails_the_check_rather_than_being_merged():
    # A timezone read wrong shifts four months of every year by an hour. It
    # does not look like an error, it looks like noise - which is precisely
    # why a threshold catches it and a human reading the log would not.
    from tremor import backfill

    import numpy as np
    base = int(datetime(2021, 1, 4, tzinfo=timezone.utc).timestamp())
    rng = np.random.default_rng(9)
    n = 60 * (backfill.ALIGNMENT_MIN_HOURS + 60)
    walk = 100 * np.exp(np.cumsum(rng.standard_normal(n) * 0.002))

    stored = bars.to_hourly(_hf_minutes(base, n, walk))
    shifted = _hf_minutes(base + 3600, n, walk)       # one hour out
    check = backfill.verify_alignment(shifted, stored)
    assert not check["ok"]
    assert "correlation" in check["why"]


def test_too_little_overlap_is_refused_rather_than_scored_on_noise():
    from tremor import backfill

    base = int(datetime(2021, 1, 4, tzinfo=timezone.utc).timestamp())
    tiny = _hf_minutes(base, 10)
    check = backfill.verify_alignment(tiny, bars.to_hourly(tiny))
    assert not check["ok"] and "overlapping hours" in check["why"]


def test_the_etf_import_refuses_to_run_without_a_timezone(tmp_path):
    # Set to None it must stop, because the one thing that cannot be recovered
    # later is a silently misaligned archive.
    from tremor import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    _store_days(path, [_day(2021, 1, 4)])
    out = backfill.deepen_from_hfdata(_etf(), str(path), date(2015, 1, 1),
                                      "key", None, timezone_name=None)
    assert out["added"] == 0 and "TIMEZONE" in out["skipped"]


def test_the_etf_import_only_touches_us_equity_instruments(tmp_path):
    from tremor import backfill

    out = backfill.deepen_from_hfdata(_etf(session_template="fx_continuous"),
                                      str(tmp_path / "x.parquet"),
                                      date(2015, 1, 1), "key", None)
    assert out["added"] == 0 and "US-equity" in out["skipped"]


def test_a_failed_check_merges_nothing(tmp_path, monkeypatch):
    from tremor import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    base = int(datetime(2021, 1, 4, tzinfo=timezone.utc).timestamp())
    _store_days(path, [base])
    before = len(bars.load(str(path)))

    monkeypatch.setattr(backfill.hfdata, "fetch_parquet", lambda *a, **k: b"x")
    monkeypatch.setattr(backfill.hfdata, "to_minute_frame",
                        lambda *a, **k: _hf_minutes(base - 400 * 3600, 50))
    out = backfill.deepen_from_hfdata(_etf(), str(path), date(2015, 1, 1),
                                      "key", None)

    assert out["added"] == 0 and "alignment check failed" in out["skipped"]
    assert len(bars.load(str(path))) == before


def test_a_dividend_adjusted_series_is_caught_even_though_returns_agree():
    # The hole the first version of this check had. Adjustment is
    # multiplicative, so it barely touches returns: the real import scored
    # 0.9959 on correlation while sitting 99bp below the stored prices, and
    # 30024 adjusted SPY bars went into the store because only the correlation
    # was asserted on.
    from tremor import backfill

    import numpy as np
    base = int(datetime(2021, 1, 4, tzinfo=timezone.utc).timestamp())
    rng = np.random.default_rng(17)
    n = 60 * (backfill.ALIGNMENT_MIN_HOURS + 60)
    walk = 100 * np.exp(np.cumsum(rng.standard_normal(n) * 0.001))

    stored = bars.to_hourly(_hf_minutes(base, n, walk))
    adjusted = _hf_minutes(base, n, walk * 0.99)      # one percent low, as SPY was

    check = backfill.verify_alignment(adjusted, stored)
    assert check["correlation"] > 0.99      # returns are untouched by the factor
    assert not check["ok"]
    assert "dividend adjustment" in check["why"]


def test_a_series_that_agrees_on_both_price_and_returns_passes():
    from tremor import backfill

    import numpy as np
    base = int(datetime(2021, 1, 4, tzinfo=timezone.utc).timestamp())
    rng = np.random.default_rng(18)
    n = 60 * (backfill.ALIGNMENT_MIN_HOURS + 60)
    walk = 100 * np.exp(np.cumsum(rng.standard_normal(n) * 0.001))

    stored = bars.to_hourly(_hf_minutes(base, n, walk))
    check = backfill.verify_alignment(_hf_minutes(base, n, walk), stored)
    assert check["ok"] and check["median_bp"] < backfill.ALIGNMENT_MAX_MEDIAN_BP


# --- undoing a vendor's dividend adjustment --------------------------------

def _adjusted(minutes, steps, reference, ratio):
    """The inverse of unadjust_to_store, for building a fixture."""
    from tremor import corporate_actions
    factor = corporate_actions.unadjust_factor(
        steps, minutes["hour_utc"].to_numpy(), reference, ratio)
    out = minutes.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = out[c].to_numpy() * factor
    return out


def test_unadjust_factor_round_trips_a_declared_dividend_series():
    # Prices are built FORWARD from declared cash dividends - multiply every
    # pre-ex bar by (1 - d), d = cash / previous true close - and only then
    # un-adjusted. `_adjusted` below is the inverse of unadjust_factor and so
    # cannot fail; this can.
    from datetime import date as _date

    from tremor import backfill

    import numpy as np
    base = int(datetime(2020, 2, 10, tzinfo=timezone.utc).timestamp())
    rng = np.random.default_rng(23)
    n = 60 * 24 * 400
    walk = 300 * np.exp(np.cumsum(rng.standard_normal(n) * 0.0002))
    truth = _hf_minutes(base, n, walk)
    stored = bars.to_hourly(truth)
    days = pd.to_datetime(truth["hour_utc"], unit="s", utc=True).dt.date.to_numpy()

    payouts = [(_date(2020, 3, 20), 1.50), (_date(2020, 6, 19), 1.50),
               (_date(2020, 9, 18), 1.50), (_date(2020, 12, 18), 1.50)]
    vendor = truth.copy()
    steps = []
    for ex, cash in payouts:
        prior = truth.loc[days < ex, "close"]
        prev_close = float(prior.iloc[-1])
        d = cash / prev_close
        steps.append((ex, d / (1.0 - d)))
        factor = np.where(days < ex, 1.0 - d, 1.0)
        for column in ("open", "high", "low", "close"):
            vendor[column] = vendor[column].to_numpy() * factor

    fixed, info = backfill.unadjust_to_store(vendor, stored, steps)
    assert info["calibrated"]
    check = backfill.verify_alignment(fixed, stored)
    assert check["ok"] and check["median_bp"] < 1.0, check


def test_the_adjustment_is_undone_and_the_result_matches_the_store():
    from datetime import date as _date

    from tremor import backfill

    import numpy as np
    base = int(datetime(2020, 2, 10, tzinfo=timezone.utc).timestamp())
    rng = np.random.default_rng(23)
    n = 60 * 24 * 400
    walk = 300 * np.exp(np.cumsum(rng.standard_normal(n) * 0.0002))
    truth = _hf_minutes(base, n, walk)
    stored = bars.to_hourly(truth)

    # A year of quarterly payouts, as a vendor would have applied them.
    steps = [(_date(2020, 3, 20), 0.005), (_date(2020, 6, 19), 0.005),
             (_date(2020, 9, 18), 0.005), (_date(2020, 12, 18), 0.005)]
    vendor = _adjusted(truth, steps, _date(2020, 2, 20), 0.98)

    fixed, info = backfill.unadjust_to_store(vendor, stored, steps)
    assert info["calibrated"]
    check = backfill.verify_alignment(fixed, stored)
    assert check["ok"], check["why"]
    assert check["median_bp"] < 5


def test_the_uncorrected_series_would_have_failed_the_same_check():
    # Which is what makes the correction worth doing rather than assumed.
    from datetime import date as _date

    from tremor import backfill

    import numpy as np
    base = int(datetime(2020, 2, 10, tzinfo=timezone.utc).timestamp())
    rng = np.random.default_rng(23)
    n = 60 * 24 * 400
    walk = 300 * np.exp(np.cumsum(rng.standard_normal(n) * 0.0002))
    truth = _hf_minutes(base, n, walk)
    stored = bars.to_hourly(truth)
    steps = [(_date(2020, 3, 20), 0.005), (_date(2020, 6, 19), 0.005),
             (_date(2020, 9, 18), 0.005), (_date(2020, 12, 18), 0.005)]

    vendor = _adjusted(truth, steps, _date(2020, 2, 20), 0.98)
    assert not backfill.verify_alignment(vendor, stored)["ok"]


def test_an_instrument_with_no_payouts_is_only_rescaled():
    # GLD, SLV and USO distribute nothing, so their factor is a flat number and
    # the ex-date list is legitimately empty.
    from tremor import backfill

    import numpy as np
    base = int(datetime(2020, 2, 10, tzinfo=timezone.utc).timestamp())
    rng = np.random.default_rng(31)
    n = 60 * 24 * 60
    # A real walk, not a flat line: a constant series has no variance and the
    # return correlation comes back NaN rather than 1.
    walk = 150 * np.exp(np.cumsum(rng.standard_normal(n) * 0.0002))
    truth = _hf_minutes(base, n, walk)
    stored = bars.to_hourly(truth)

    fixed, info = backfill.unadjust_to_store(_hf_minutes(base, n, walk * 0.9),
                                             stored, [])
    assert info["calibrated"] and abs(info["ratio"] - 0.9) < 1e-6
    assert backfill.verify_alignment(fixed, stored)["ok"]


def test_calibration_needs_enough_overlap_to_be_meaningful():
    from tremor import backfill

    base = int(datetime(2020, 2, 10, tzinfo=timezone.utc).timestamp())
    tiny = _hf_minutes(base, 120)
    _, info = backfill.unadjust_to_store(tiny, bars.to_hourly(tiny), [])
    assert not info["calibrated"]


def test_the_gap_fill_patches_only_the_missing_days(tmp_path, monkeypatch):
    # This writes INTO the middle of the stored series rather than under it,
    # so it must touch the missing days and nothing else - a patch that also
    # rewrote neighbouring hours would replace vendor A's bars with vendor B's
    # in places the store was already complete.
    from tremor import backfill

    import numpy as np
    path = tmp_path / "twelvedata_SPY.parquet"
    base = int(datetime(2021, 1, 4, tzinfo=timezone.utc).timestamp())
    n = 60 * 24 * 120
    rng = np.random.default_rng(41)
    walk = 300 * np.exp(np.cumsum(rng.standard_normal(n) * 0.0002))
    everything = _hf_minutes(base, n, walk)

    # The store holds all of it except one day.
    hole = date(2021, 2, 1)
    days = pd.to_datetime(everything["hour_utc"], unit="s", utc=True).dt.date
    bars.merge(str(path), bars.to_hourly(everything[(days != hole).to_numpy()]))
    before = bars.load(str(path))

    table = _table(set(days))
    monkeypatch.setattr(backfill.hfdata, "fetch_parquet", lambda *a, **k: b"x")
    monkeypatch.setattr(backfill.hfdata, "to_minute_frame",
                        lambda *a, **k: everything)
    monkeypatch.setattr(backfill.corporate_actions, "load_steps", lambda: {})

    out = backfill.fill_gaps_from_hfdata(_etf(), str(path), table, "key", None)

    assert out["gaps"] == 1 and out["added"] > 0
    assert out["still_missing"] == []
    after = bars.load(str(path))
    # every hour that was already there is untouched
    merged = before.merge(after, on="hour_utc", suffixes=("_before", "_after"))
    assert len(merged) == len(before)
    assert (merged["close_before"] == merged["close_after"]).all()


def test_the_gap_fill_writes_nothing_when_the_alignment_check_fails(tmp_path, monkeypatch):
    from tremor import backfill

    import numpy as np
    path = tmp_path / "twelvedata_SPY.parquet"
    base = int(datetime(2021, 1, 4, tzinfo=timezone.utc).timestamp())
    n = 60 * 24 * 120
    rng = np.random.default_rng(42)
    walk = 300 * np.exp(np.cumsum(rng.standard_normal(n) * 0.0002))
    everything = _hf_minutes(base, n, walk)
    hole = date(2021, 2, 1)
    days = pd.to_datetime(everything["hour_utc"], unit="s", utc=True).dt.date
    bars.merge(str(path), bars.to_hourly(everything[(days != hole).to_numpy()]))
    before = len(bars.load(str(path)))

    table = _table(set(days))
    monkeypatch.setattr(backfill.hfdata, "fetch_parquet", lambda *a, **k: b"x")
    # An hour out, which is what a timezone read wrong looks like.
    monkeypatch.setattr(backfill.hfdata, "to_minute_frame",
                        lambda *a, **k: _hf_minutes(base + 3600, n, walk))
    monkeypatch.setattr(backfill.corporate_actions, "load_steps", lambda: {})

    out = backfill.fill_gaps_from_hfdata(_etf(), str(path), table, "key", None)
    assert out["added"] == 0 and "alignment check failed" in out["skipped"]
    assert len(bars.load(str(path))) == before


def _full_session_hours(day):
    from tremor.sessions import Session, session_hours
    return session_hours(Session(day=day, local_open="09:30",
                                 local_close="16:00", is_early_close=False))


def test_a_day_missing_five_of_its_seven_bars_is_not_a_complete_day(tmp_path):
    # The hole that survived the day-level fill: 2020-02-19 was present with
    # two bars of seven, so nothing reported it as a gap.
    from tremor import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    days = [date(2024, 3, 4), date(2024, 3, 5)]
    hours = _full_session_hours(days[0]) + _full_session_hours(days[1])[-2:]
    _store_days(path, hours)
    table = _table(days)

    assert backfill.missing_sessions(str(path), table) == []
    missing = backfill.missing_hours(str(path), table)
    assert missing == _full_session_hours(days[1])[:-2]


def test_a_complete_store_has_no_missing_hours(tmp_path):
    from tremor import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    days = [date(2024, 3, 4), date(2024, 3, 5)]
    _store_days(path, _full_session_hours(days[0]) + _full_session_hours(days[1]))
    assert backfill.missing_hours(str(path), _table(days)) == []


def test_hours_outside_the_stored_range_are_not_holes(tmp_path):
    # Same bound as the day-level view: the archive simply has not reached
    # there yet, and counting those would bury the real holes.
    from tremor import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    days = [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)]
    _store_days(path, _full_session_hours(days[1]))
    assert backfill.missing_hours(str(path), _table(days)) == []


def test_the_gap_fill_recovers_hours_inside_a_day_that_is_already_present(
        tmp_path, monkeypatch):
    from tremor import backfill

    import numpy as np
    path = tmp_path / "twelvedata_SPY.parquet"
    base = int(datetime(2021, 1, 4, tzinfo=timezone.utc).timestamp())
    n = 60 * 24 * 120
    rng = np.random.default_rng(43)
    walk = 300 * np.exp(np.cumsum(rng.standard_normal(n) * 0.0002))
    everything = _hf_minutes(base, n, walk)

    days = pd.to_datetime(everything["hour_utc"], unit="s", utc=True).dt.date
    table = _table(set(days))
    # Every day is present; one of them keeps only its last two session hours.
    wounded = date(2021, 2, 1)
    keep = set(_full_session_hours(wounded)[-2:])
    hours = everything["hour_utc"] // 3600 * 3600
    drop = (days == wounded).to_numpy() & ~hours.isin(keep).to_numpy()
    bars.merge(str(path), bars.to_hourly(everything[~drop]))

    before = bars.load(str(path))
    assert backfill.missing_sessions(str(path), table) == []
    assert len(backfill.missing_hours(str(path), table)) == 5

    monkeypatch.setattr(backfill.hfdata, "fetch_parquet", lambda *a, **k: b"x")
    monkeypatch.setattr(backfill.hfdata, "to_minute_frame",
                        lambda *a, **k: everything)
    monkeypatch.setattr(backfill.corporate_actions, "load_steps", lambda: {})

    out = backfill.fill_gaps_from_hfdata(_etf(), str(path), table, "key", None)

    assert out["gaps"] == 0 and out["hours"] == 5 and out["added"] == 5
    assert out["still_missing_hours"] == 0
    # and the hours that were already there still carry vendor A's bars
    after = bars.load(str(path))
    merged = before.merge(after, on="hour_utc", suffixes=("_b", "_a"))
    assert len(merged) == len(before)
    assert (merged["close_b"] == merged["close_a"]).all()


def _fx_asset(ticker="EUR/USD"):
    return Asset(ticker=ticker, source="twelvedata", block="FX",
                 has_volume=False, tick_size=0.00001, session_template="fx",
                 fetch_interval="1h", label=ticker, in_basket=True)


def _fx_bar(hour, close):
    return Candle(open_time=hour, open=close, high=close, low=close,
                  close=close, volume=0.0, close_time=hour + HOUR)


def _fx_store(path, first_hour, closes):
    bars.merge(str(path), bars.to_hourly(bars.candles_to_frame(
        [_fx_bar(first_hour + i * HOUR, c) for i, c in enumerate(closes)])))


def test_dukascopy_deepening_needs_something_to_check_itself_against(tmp_path):
    # With nothing stored there is no overlap, and on this source a wrong point
    # size is a factor of a thousand - so an unverifiable first write is refused
    # rather than taken on trust.
    from tremor import backfill

    path = tmp_path / "twelvedata_EUR_USD.parquet"
    out = backfill.deepen_from_dukascopy(_fx_asset(), str(path), date(2003, 1, 1), None)
    assert out["skipped"] == "nothing stored yet" and out["added"] == 0


def test_dukascopy_deepening_skips_a_pair_the_archive_does_not_carry(tmp_path):
    from tremor import backfill

    path = tmp_path / "twelvedata_USD_CNY.parquet"
    out = backfill.deepen_from_dukascopy(_fx_asset("USD/CNY"), str(path),
                                         date(2003, 1, 1), None)
    assert out["skipped"] == "no Dukascopy symbol"


def test_dukascopy_deepening_does_nothing_when_the_store_reaches_back(tmp_path):
    from tremor import backfill

    path = tmp_path / "twelvedata_EUR_USD.parquet"
    _fx_store(path, int(datetime(2003, 1, 2, tzinfo=timezone.utc).timestamp()),
              [1.2] * 10)
    out = backfill.deepen_from_dukascopy(_fx_asset(), str(path),
                                         date(2004, 1, 1), None)
    assert out["skipped"] == "already reaches back far enough"


def _walk(n, seed, start=1.2):
    import numpy as np
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.standard_normal(n) * 0.0004))


def test_the_overlap_earns_the_right_to_write_the_years_below_it(tmp_path, monkeypatch):
    from tremor import backfill

    first = int(datetime(2012, 1, 2, tzinfo=timezone.utc).timestamp())
    prices = _walk(3000, 7)
    # The store holds the last 2000 hours; the archive holds all 3000.
    _fx_store(path := tmp_path / "twelvedata_EUR_USD.parquet",
              first + 1000 * HOUR, prices[1000:])
    archive = [_fx_bar(first + i * HOUR, p) for i, p in enumerate(prices)]
    monkeypatch.setattr(backfill.dukascopy, "fetch_history",
                        lambda *a, **k: archive)

    before = bars.load(str(path))
    out = backfill.deepen_from_dukascopy(_fx_asset(), str(path), date(2003, 1, 1), None)

    assert out["skipped"] is None
    assert out["added"] == 1000                     # only what was below
    assert out["check"]["ok"] and out["check"]["correlation"] > 0.99
    after = bars.load(str(path))
    # nothing at or above the old floor was touched
    merged = before.merge(after, on="hour_utc", suffixes=("_b", "_a"))
    assert len(merged) == len(before)
    assert (merged["close_b"] == merged["close_a"]).all()


def test_a_thousandfold_scale_error_is_caught_by_the_level_gate(tmp_path, monkeypatch):
    # What reading a yen pair with the five-decimal point actually looks like.
    from tremor import backfill

    first = int(datetime(2012, 1, 2, tzinfo=timezone.utc).timestamp())
    prices = _walk(3000, 8, start=100.0)
    _fx_store(path := tmp_path / "twelvedata_USD_JPY.parquet",
              first + 1000 * HOUR, prices[1000:])
    wrong = [_fx_bar(first + i * HOUR, p / 1000.0) for i, p in enumerate(prices)]
    monkeypatch.setattr(backfill.dukascopy, "fetch_history", lambda *a, **k: wrong)

    before = len(bars.load(str(path)))
    out = backfill.deepen_from_dukascopy(_fx_asset("USD/JPY"), str(path),
                                         date(2003, 1, 1), None)
    assert out["added"] == 0 and "alignment check failed" in out["skipped"]
    assert len(bars.load(str(path))) == before


def test_a_shifted_archive_fails_before_anything_is_written(tmp_path, monkeypatch):
    from tremor import backfill

    first = int(datetime(2012, 1, 2, tzinfo=timezone.utc).timestamp())
    prices = _walk(3000, 9)
    _fx_store(path := tmp_path / "twelvedata_EUR_USD.parquet",
              first + 1000 * HOUR, prices[1000:])
    shifted = [_fx_bar(first + (i + 1) * HOUR, p) for i, p in enumerate(prices)]
    monkeypatch.setattr(backfill.dukascopy, "fetch_history", lambda *a, **k: shifted)

    before = len(bars.load(str(path)))
    out = backfill.deepen_from_dukascopy(_fx_asset(), str(path), date(2003, 1, 1), None)
    assert out["added"] == 0 and "alignment check failed" in out["skipped"]
    assert len(bars.load(str(path))) == before


# --- the session-aware skip ------------------------------------------------
#
# Every test here is about the same failure: skipping a fetch that would have
# returned something. That is a hole in the history and a move the detector
# never sees, where the worst a needless request costs is one of 800 a day.

def _equity(**over):
    return asset(ticker="SPY", session_template="us_equity", tick_size=0.01,
                 label="S&P 500", **over)


def _equity_store(tmp_path, hour_utc):
    path = str(tmp_path / "spy.parquet")
    bars.merge(path, bars.to_hourly(bars.candles_to_frame(
        [Candle(open_time=hour_utc, open=1.0, high=2.0, low=0.5, close=1.5,
                volume=1.0, close_time=hour_utc + HOUR)])))
    return path


def test_a_closed_market_with_nothing_new_is_not_asked_for():
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        # Friday 20:00 UTC stored; it is now Saturday afternoon and the calendar
        # runs to Monday. Nothing can have traded in between.
        friday = date(2026, 4, 3)
        stored = int(datetime(2026, 4, 3, 20, tzinfo=timezone.utc).timestamp())
        path = _equity_store(pathlib.Path(tmp), stored)
        table = _table([friday, date(2026, 4, 6)])
        now = datetime(2026, 4, 4, 15, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(_equity(), path, table, now) is True


def test_an_open_market_is_always_asked_for():
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        # Stored up to 14:00 on a trading day, and the session runs to 20:00 UTC.
        stored = int(datetime(2026, 4, 3, 14, tzinfo=timezone.utc).timestamp())
        path = _equity_store(pathlib.Path(tmp), stored)
        table = _table([date(2026, 4, 3)])
        now = datetime(2026, 4, 3, 19, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(_equity(), path, table, now) is False


# Before the open nothing can exist yet, though the day's session hours are on
# the calendar. Asked anyway, every run from 00:05 UTC cost a request per
# instrument: SiftingIO's pairs past their monthly quota (~10,400 of 10,000).

def test_a_fund_is_not_asked_before_the_open_of_a_trading_day():
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        # Friday's closing hour stored; Monday 05:05 UTC, the open is 13:30.
        stored = int(datetime(2026, 4, 3, 19, tzinfo=timezone.utc).timestamp())
        path = _equity_store(pathlib.Path(tmp), stored)
        table = _table([date(2026, 4, 3), date(2026, 4, 6)])
        now = datetime(2026, 4, 6, 5, 5, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(_equity(), path, table, now) is True


def test_a_fund_is_asked_from_the_run_in_its_first_hour():
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        # 13:05 UTC: the hour holding the 09:30 New York open has begun.
        stored = int(datetime(2026, 4, 3, 19, tzinfo=timezone.utc).timestamp())
        path = _equity_store(pathlib.Path(tmp), stored)
        table = _table([date(2026, 4, 3), date(2026, 4, 6)])
        now = datetime(2026, 4, 6, 13, 5, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(_equity(), path, table, now) is False


def test_fx_is_not_asked_on_sunday_before_the_week_reopens():
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        # Sunday 12:05 UTC; the week reopens at 21:00 UTC (17:00 New York).
        stored = int(datetime(2026, 4, 3, 20, tzinfo=timezone.utc).timestamp())
        path = _equity_store(pathlib.Path(tmp), stored)
        table = _table([date(2026, 4, 3), date(2026, 4, 6)])
        now = datetime(2026, 4, 5, 12, 5, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(asset(), path, table, now) is True


def test_fx_is_skipped_when_the_reference_week_is_shut():
    # Saturday afternoon: the FX week is Sun 17:00 → Fri 17:00 New York.
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        stored = int(datetime(2026, 4, 3, 20, tzinfo=timezone.utc).timestamp())
        path = _equity_store(pathlib.Path(tmp), stored)
        table = _table([date(2026, 4, 3), date(2026, 4, 6)])
        now = datetime(2026, 4, 4, 15, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(asset(), path, table, now) is True


def test_fx_is_asked_when_the_week_has_reopened():
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        stored = int(datetime(2026, 4, 3, 20, tzinfo=timezone.utc).timestamp())
        path = _equity_store(pathlib.Path(tmp), stored)
        table = _table([date(2026, 4, 3), date(2026, 4, 6)])
        # Sunday 22:00 UTC is 18:00 EDT, after the 17:00 New York reopen.
        now = datetime(2026, 4, 5, 22, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(asset(), path, table, now) is False


def test_fx_weekend_bars_a_provider_serves_do_not_keep_the_pair_asked_for():
    # SiftingIO serves bars through the weekend, and they are stored (the quality
    # gate drops them later). Measured from them, the newest bar is always an
    # hour old and the pair was asked every weekend hour - 16 pairs x 48 hours,
    # past the provider's monthly quota.
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        friday = int(datetime(2026, 4, 3, 20, tzinfo=timezone.utc).timestamp())
        path = _equity_store(pathlib.Path(tmp), friday)
        saturday = int(datetime(2026, 4, 4, 14, tzinfo=timezone.utc).timestamp())
        bars.merge(path, bars.to_hourly(bars.candles_to_frame(
            [Candle(open_time=saturday, open=1.5, high=1.5, low=1.5, close=1.5,
                    volume=0.0, close_time=saturday + HOUR)])))
        table = _table([date(2026, 4, 3), date(2026, 4, 6)])
        now = datetime(2026, 4, 4, 15, 5, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(asset(), path, table, now) is True
        # ...and the week reopening is still seen.
        sunday = datetime(2026, 4, 5, 22, 5, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(asset(), path, table, sunday) is False


def test_crypto_is_never_skipped():
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        stored = int(datetime(2026, 4, 3, 20, tzinfo=timezone.utc).timestamp())
        path = _equity_store(pathlib.Path(tmp), stored)
        table = _table([date(2026, 4, 3), date(2026, 4, 6)])
        now = datetime(2026, 4, 4, 15, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(
            asset(source="binance", session_template="crypto_24_7"),
            path, table, now) is False


def test_a_calendar_that_stops_short_never_causes_a_skip():
    # A table that ends before today reports "no session" for every day past its
    # end. Read as a holiday that would silence the instrument permanently.
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        stored = int(datetime(2026, 4, 3, 20, tzinfo=timezone.utc).timestamp())
        path = _equity_store(pathlib.Path(tmp), stored)
        stale = _table([date(2026, 4, 3)])          # nothing after the stored bar
        now = datetime(2026, 4, 20, 15, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(_equity(), path, stale, now) is False
        assert nothing_can_have_appeared(_equity(), path, None, now) is False


def test_the_newest_bar_is_re_asked_for_once_before_skipping_starts():
    # The ordinary fetch asks for a day more than it needs, because the newest
    # stored bar may have been served while its hour was still open. Skipping on
    # the very next run would keep whatever was stored first.
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        stored = int(datetime(2026, 4, 3, 20, tzinfo=timezone.utc).timestamp())
        path = _equity_store(pathlib.Path(tmp), stored)
        table = _table([date(2026, 4, 3), date(2026, 4, 6)])
        just_after = datetime(2026, 4, 3, 21, 30, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(_equity(), path, table, just_after) is False
        settled = datetime(2026, 4, 3, 23, 30, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(_equity(), path, table, settled) is True


def test_an_empty_store_is_always_asked_for():
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        path = str(pathlib.Path(tmp) / "empty.parquet")
        table = _table([date(2026, 4, 3)])
        now = datetime(2026, 4, 4, 15, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(_equity(), path, table, now) is False


def test_a_store_that_has_fallen_days_behind_is_asked_for():
    # After an outage the calendar has plenty of hours past the newest bar, and
    # every one of them is a bar the detector is missing.
    from tremor.backfill import nothing_can_have_appeared
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        stored = int(datetime(2026, 4, 3, 20, tzinfo=timezone.utc).timestamp())
        path = _equity_store(pathlib.Path(tmp), stored)
        table = _table([date(2026, 4, 3), date(2026, 4, 6), date(2026, 4, 7)])
        now = datetime(2026, 4, 7, 18, tzinfo=timezone.utc)
        assert nothing_can_have_appeared(_equity(), path, table, now) is False


# --- a newly configured instrument does not turn the hourly run into a job ---

def test_an_empty_store_takes_one_chunk_not_the_whole_archive(monkeypatch, tmp_path):
    # At the acquisition floor of 2002 the whole archive is about thirty chunks
    # paced eight seconds apart: four minutes and thirty credits for ONE
    # instrument, inside a job that is meant to take ninety seconds.
    from tremor import backfill

    asked = {}

    def fake(symbol, interval, days, **kw):
        asked["days"] = days
        return []

    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history", fake)
    asset = Asset(ticker="XLK", source="twelvedata", block="equity",
                  has_volume=True, session_template="us_equity",
                  fetch_interval="30min", label="Technology", in_basket=True,
                  tick_size=0.01)
    store = backfill.bars.store_path(str(tmp_path), asset.file_stem)

    backfill.fetch_missing(asset, store, date(2002, 1, 1), "key", None)

    assert asked["days"] == backfill.CHUNK_DAYS["30min"]


def test_extending_history_still_asks_for_the_whole_archive(monkeypatch, tmp_path):
    # The deepening workflow is the deliberate act, and it must not be capped by
    # the guard that protects the hourly run.
    from tremor import backfill

    asked = {}

    def fake(symbol, interval, days, **kw):
        asked["days"] = days
        return []

    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history", fake)
    asset = Asset(ticker="XLK", source="twelvedata", block="equity",
                  has_volume=True, session_template="us_equity",
                  fetch_interval="30min", label="Technology", in_basket=True,
                  tick_size=0.01)
    store = backfill.bars.store_path(str(tmp_path), asset.file_stem)

    backfill.fetch_missing(asset, store, date(2002, 1, 1), "key", None,
                           extend_history=True)

    assert asked["days"] > 8000


# --- VIX skip-if-fresh -------------------------------------------------------

def _vix_basket():
    from tremor.basket import Basket, VolatilityIndex
    return Basket(
        assets=(), outside=(),
        volatility_index=VolatilityIndex(
            "VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York",
        history_since=date(2021, 1, 1), session_templates={},
    )


def _vix_frame(days, close=15.0):
    from tremor import cboe
    return pd.DataFrame([
        {"day": int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()),
         "close": close, "available_at": cboe.available_at(d)}
        for d in days
    ])


def test_vix_is_not_fetched_when_the_store_already_covers_what_can_exist(
        tmp_path, monkeypatch):
    from tremor import backfill

    calls = []
    monkeypatch.setattr(backfill.cboe, "fetch_vix_history",
                        lambda *a, **k: calls.append("cboe") or _vix_frame([]))
    monkeypatch.setattr(backfill.fred, "fetch_series",
                        lambda *a, **k: calls.append("fred") or _vix_frame([]))

    frame = _vix_frame([date(2026, 4, 2), date(2026, 4, 3)])
    frame.to_parquet(tmp_path / "fred_VIXCLS.parquet", index=False)
    # Saturday 15:00 UTC: CBOE's 16:30 Eastern gate for the 4th has not opened,
    # so the newest day that can exist is the 3rd, which the store already has.
    now = datetime(2026, 4, 4, 15, tzinfo=timezone.utc)
    out = backfill.backfill_vix(_vix_basket(), str(tmp_path), "key", None, now=now)

    assert calls == []
    assert out["sources"] == ["stored"]
    assert out["rows"] == 2


def test_vix_does_not_rewrite_parquet_when_the_merge_equals_the_store(
        tmp_path, monkeypatch):
    from tremor import backfill

    stored = _vix_frame([date(2026, 4, 2), date(2026, 4, 3)])
    path = tmp_path / "fred_VIXCLS.parquet"
    stored.to_parquet(path, index=False)
    before = path.read_bytes()

    monkeypatch.setattr(backfill.cboe, "fetch_vix_history",
                        lambda *a, **k: stored.copy())
    monkeypatch.setattr(backfill.fred, "fetch_series",
                        lambda *a, **k: stored.copy())

    # After 16:30 Eastern the 4th is available, so the skip-if-fresh path does
    # not fire and the fetch runs. The merge is identical to what is already stored.
    now = datetime(2026, 4, 4, 23, tzinfo=timezone.utc)
    out = backfill.backfill_vix(_vix_basket(), str(tmp_path), "key", None, now=now)

    assert path.read_bytes() == before
    assert "cboe" in out["sources"]


def test_vix_asks_fred_from_a_recent_start_not_from_nineteen_ninety(
        tmp_path, monkeypatch):
    from datetime import timedelta
    from tremor import backfill

    stored = _vix_frame([date(2026, 4, 2), date(2026, 4, 3)])
    stored.to_parquet(tmp_path / "fred_VIXCLS.parquet", index=False)
    seen = {}

    monkeypatch.setattr(backfill.cboe, "fetch_vix_history",
                        lambda *a, **k: stored.copy())

    def fake_fred(series_id, api_key, start, session=None):
        seen["start"] = start
        return stored.copy()

    monkeypatch.setattr(backfill.fred, "fetch_series", fake_fred)
    now = datetime(2026, 4, 4, 23, tzinfo=timezone.utc)
    backfill.backfill_vix(_vix_basket(), str(tmp_path), "key", None, now=now)

    assert seen["start"] == date(2026, 4, 3) - timedelta(days=21)


# --- Yahoo 429 skips the rest of that provider, like Tiingo -----------------

def _yahoo_asset(ticker):
    return Asset(ticker=ticker, source="twelvedata", provider="yahoo",
                 block="energy", has_volume=True, tick_size=0.01,
                 session_template="us_equity", fetch_interval="30min",
                 label=ticker, in_basket=True)


def test_a_yahoo_rate_limit_skips_remaining_yahoo_instruments(tmp_path, monkeypatch):
    from tremor import backfill
    from tremor.basket import Basket, VolatilityIndex
    from price_monitor import yahoo

    asked = []
    alerts = []

    def fake_backfill(asset, *a, **k):
        asked.append(asset.ticker)
        if asset.ticker == "UGA":
            raise yahoo.RateLimited("UGA: status 429")
        return {"asset_id": asset.asset_id, "rows": 1, "first": 1, "last": 1,
                "from_api": 1}

    basket = Basket(
        assets=(_yahoo_asset("UGA"), _yahoo_asset("UNG"), _yahoo_asset("CPER"),
                Asset(ticker="BTC/USDT", source="binance", block="crypto",
                      has_volume=True, tick_size=0.01, session_template="crypto_24_7",
                      fetch_interval="1h", label="Bitcoin", in_basket=True)),
        outside=(),
        volatility_index=VolatilityIndex(
            "VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York",
        history_since=date(2021, 1, 1), session_templates={
            "us_equity": {}, "crypto_24_7": {}},
    )
    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    monkeypatch.setattr(backfill, "backfill_instrument", fake_backfill)
    monkeypatch.setattr(backfill._sessions, "load_sessions",
                        lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(backfill, "send_ops_alert", alerts.append)

    rc = backfill.main(["--skip-vix", "--bars-dir", str(tmp_path)])

    assert rc == 0
    assert asked == ["UGA", "BTC/USDT"]
    assert "2 remaining Yahoo" in alerts[0]
    assert "UGA" in alerts[0]


def test_instruments_left_by_a_rate_limit_are_not_blamed_on_the_calendar(
        tmp_path, monkeypatch, caplog):
    from tremor import backfill
    from tremor.basket import Basket, VolatilityIndex
    from price_monitor import yahoo

    def fake_backfill(asset, *a, **k):
        if asset.ticker == "UGA":
            raise yahoo.RateLimited("UGA: status 429")
        return {"asset_id": asset.asset_id, "rows": 1, "first": 1, "last": 1,
                "from_api": 1}

    basket = Basket(
        assets=(_yahoo_asset("UGA"), _yahoo_asset("UNG"), _yahoo_asset("CPER")),
        outside=(),
        volatility_index=VolatilityIndex(
            "VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York",
        history_since=date(2021, 1, 1), session_templates={"us_equity": {}},
    )
    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    monkeypatch.setattr(backfill, "backfill_instrument", fake_backfill)
    monkeypatch.setattr(backfill._sessions, "load_sessions",
                        lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(backfill, "send_ops_alert", lambda *a, **k: None)

    with caplog.at_level("INFO", logger="tremor.backfill"):
        backfill.main(["--skip-vix", "--bars-dir", str(tmp_path)])

    assert "the calendar says nothing new can exist" not in caplog.text
    assert "2 instrument(s) not asked: their provider's rate limit is spent" \
        in caplog.text


def test_a_yahoo_404_stays_per_instrument_and_does_not_skip_the_rest(
        tmp_path, monkeypatch):
    from tremor import backfill
    from tremor.basket import Basket, VolatilityIndex
    from price_monitor.models import ExchangeError

    asked = []

    def fake_backfill(asset, *a, **k):
        asked.append(asset.ticker)
        if asset.ticker == "UGA":
            raise ExchangeError("UGA: unknown to Yahoo (404)")
        return {"asset_id": asset.asset_id, "rows": 1, "first": 1, "last": 1,
                "from_api": 1}

    basket = Basket(
        assets=(_yahoo_asset("UGA"), _yahoo_asset("UNG")),
        outside=(),
        volatility_index=VolatilityIndex(
            "VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York",
        history_since=date(2021, 1, 1), session_templates={"us_equity": {}},
    )
    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    monkeypatch.setattr(backfill, "backfill_instrument", fake_backfill)
    monkeypatch.setattr(backfill._sessions, "load_sessions",
                        lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(backfill, "send_ops_alert", lambda *_: None)

    rc = backfill.main(["--skip-vix", "--bars-dir", str(tmp_path)])

    assert rc == 1
    assert asked == ["UGA", "UNG"]


def _sifting_pair(ticker):
    return Asset(ticker=ticker, source="twelvedata", provider="sifting",
                 block="FX", has_volume=False, tick_size=0.00001,
                 session_template="fx_continuous", fetch_interval="1h",
                 label=ticker, in_basket=True)


def test_a_sifting_budget_skips_remaining_pairs_and_says_so(tmp_path, monkeypatch):
    from tremor import backfill
    from tremor.basket import Basket, VolatilityIndex
    from price_monitor import sifting

    asked, alerts = [], []

    def fake_backfill(asset, *a, **k):
        asked.append(asset.ticker)
        if asset.ticker == "EUR/USD":
            raise sifting.RateLimited("EUR/USD: budget", remaining="0")
        return {"asset_id": asset.asset_id, "rows": 1, "first": 1, "last": 1,
                "from_api": 1}

    basket = Basket(
        assets=(_sifting_pair("EUR/USD"), _sifting_pair("USD/JPY"), _yahoo_asset("UGA")),
        outside=(),
        volatility_index=VolatilityIndex("VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York",
        history_since=date(2021, 1, 1),
        session_templates={"fx_continuous": {}, "us_equity": {}},
    )
    monkeypatch.setenv("SIFTING_API_KEY", "k")
    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    monkeypatch.setattr(backfill, "backfill_instrument", fake_backfill)
    monkeypatch.setattr(backfill._sessions, "load_sessions",
                        lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(backfill, "send_ops_alert", alerts.append)

    assert backfill.main(["--skip-vix", "--bars-dir", str(tmp_path)]) == 0
    assert asked == ["EUR/USD", "UGA"]
    assert "SiftingIO request budget spent" in alerts[0]
    assert "monthly quota left 0" in alerts[0]


def test_a_sifting_pair_without_the_key_stops_the_run(tmp_path, monkeypatch):
    from tremor import backfill
    from tremor.basket import Basket, VolatilityIndex

    basket = Basket(
        assets=(_sifting_pair("EUR/USD"),), outside=(),
        volatility_index=VolatilityIndex("VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York", history_since=date(2021, 1, 1),
        session_templates={"fx_continuous": {}},
    )
    monkeypatch.delenv("SIFTING_API_KEY", raising=False)
    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    assert backfill.main(["--skip-vix", "--bars-dir", str(tmp_path)]) == 2


def test_the_fetch_asks_sifting_for_a_sifting_pair(tmp_path, monkeypatch):
    from tremor import backfill

    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(backfill.sifting, "fetch_full_history", fake)
    backfill.fetch_missing(_sifting_pair("USD/MXN"), str(tmp_path / "p"),
                           date(2021, 1, 1), "td", requests.Session(),
                           sifting_key="sk")
    assert seen["symbol"] == "USD/MXN" and seen["api_key"] == "sk"


def test_the_fetch_asks_google_for_a_google_fund(tmp_path, monkeypatch):
    from tremor import backfill

    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(backfill.google, "fetch_full_history", fake)
    asset = Asset(ticker="TUR", source="twelvedata", provider="google", 
                  block="equity", has_volume=True, tick_size=0.01,
                  session_template="us_equity", fetch_interval="30min",
                  label="TUR", in_basket=True)
    backfill.fetch_missing(asset, str(tmp_path / "p"), date(2021, 1, 1), "td",
                           requests.Session())
    assert seen["symbol"] == "TUR" and seen["interval"] == "30min"


# --- Alpaca deepening ---------------------------------------------------------

def _fund():
    return Asset(ticker="VCIT", source="twelvedata", provider="yahoo", 
                 block="credit", has_volume=True, tick_size=0.01,
                 session_template="us_equity", fetch_interval="30min",
                 label="VCIT", in_basket=True)


def _session_half_hours(days):
    """Regular-session half-hour stamps (New York) for consecutive weekdays."""
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    out, day = [], datetime(2019, 6, 3)
    while len(out) < days * 13:
        if day.weekday() < 5:
            for k in range(13):
                t = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ny) + timedelta(minutes=30 * k)
                out.append(int(t.timestamp()))
        day += timedelta(days=1)
    return out


def _alpaca_candles(stamps, prices):
    from price_monitor.models import Candle
    return [Candle(open_time=t, open=p, high=p, low=p, close=p, volume=100.0,
                   close_time=t + 1800) for t, p in zip(stamps, prices)]


def test_alpaca_writes_only_below_the_store_once_the_overlap_agrees(tmp_path, monkeypatch):
    from tremor import backfill
    stamps = _session_half_hours(200)
    prices = _walk(len(stamps), 11, start=80.0)
    archive = _alpaca_candles(stamps, prices)
    path = str(tmp_path / "twelvedata_VCIT.parquet")
    split = 100 * 13
    bars.merge(path, bars.to_hourly(bars.candles_to_frame(archive[split:])))
    before = bars.load(path)
    monkeypatch.setattr(backfill.alpaca, "fetch_history", lambda *a, **k: archive)

    out = backfill.deepen_from_alpaca(_fund(), path, date(2015, 1, 1), {}, None)

    assert out["skipped"] is None and out["check"]["ok"]
    after = bars.load(path)
    assert after["hour_utc"].min() < before["hour_utc"].min()
    kept = before.merge(after, on="hour_utc", suffixes=("_b", "_a"))
    assert (kept["close_b"] == kept["close_a"]).all()


def test_alpaca_with_dividends_taken_out_is_refused_on_level(tmp_path, monkeypatch):
    # A dividend-adjusted series agrees on returns and sits below on price.
    from tremor import backfill
    stamps = _session_half_hours(200)
    prices = _walk(len(stamps), 12, start=80.0)
    path = str(tmp_path / "twelvedata_VCIT.parquet")
    bars.merge(path, bars.to_hourly(bars.candles_to_frame(
        _alpaca_candles(stamps[1300:], prices[1300:]))))
    adjusted = _alpaca_candles(stamps, prices * 0.97)
    monkeypatch.setattr(backfill.alpaca, "fetch_history", lambda *a, **k: adjusted)
    before = len(bars.load(path))
    out = backfill.deepen_from_alpaca(_fund(), path, date(2015, 1, 1), {}, None)
    assert out["added"] == 0 and "median level gap" in out["skipped"]
    assert len(bars.load(path)) == before


def test_alpaca_keeps_only_the_regular_session():
    from zoneinfo import ZoneInfo
    from price_monitor import alpaca
    ny = ZoneInfo("America/New_York")
    at = lambda h, m: int(datetime(2019, 6, 3, h, m, tzinfo=ny).timestamp())  # noqa: E731
    assert [alpaca.regular_session(at(h, m)) for h, m in
            ((9, 0), (9, 30), (15, 30), (16, 0))] == [False, True, True, False]


def test_a_bad_stored_print_is_repaired_from_the_tape_when_asked(tmp_path, monkeypatch):
    from tremor import backfill
    stamps = _session_half_hours(200)
    prices = _walk(len(stamps), 13, start=80.0)
    archive = _alpaca_candles(stamps, prices)
    path = str(tmp_path / "twelvedata_EZU.parquet")
    stored = bars.to_hourly(bars.candles_to_frame(archive[1300:]))
    spikes = stored.index[10:60:5]                       # ten +12% prints
    stored.loc[spikes, ["open", "high", "low", "close"]] *= 1.12
    bars.merge(path, stored)
    monkeypatch.setattr(backfill.alpaca, "fetch_history", lambda *a, **k: archive)

    refused = backfill.deepen_from_alpaca(_fund(), path, date(2015, 1, 1), {}, None)
    assert refused["added"] == 0 and "correlation" in refused["skipped"]

    out = backfill.deepen_from_alpaca(_fund(), path, date(2015, 1, 1), {}, None,
                                      repair=True)
    assert out["skipped"] is None and len(out["repaired"]) == 10
    fixed = bars.load(path).set_index("hour_utc")["close"]
    tape = bars.to_hourly(bars.candles_to_frame(archive)).set_index("hour_utc")["close"]
    assert (fixed.loc[stored.loc[spikes, "hour_utc"]] ==
            tape.loc[stored.loc[spikes, "hour_utc"]]).all()


def test_the_fetch_asks_alpaca_iex_for_an_alpaca_fund(tmp_path, monkeypatch):
    from tremor import backfill
    seen = {}

    def fake(symbol, start, end, auth, session, feed="sip", **k):
        seen.update(symbol=symbol, feed=feed, span=end - start)
        return []

    monkeypatch.setattr(backfill.alpaca, "fetch_history", fake)
    asset = Asset(ticker="KRE", source="twelvedata", provider="alpaca", 
                  block="equity", has_volume=True, tick_size=0.01,
                  session_template="us_equity", fetch_interval="30min",
                  label="KRE", in_basket=True)
    backfill.fetch_missing(asset, str(tmp_path / "p"), date(2021, 1, 1), "td",
                           requests.Session())
    assert seen["symbol"] == "KRE" and seen["feed"] == "iex"


def test_alpaca_funds_need_the_alpaca_keys(tmp_path, monkeypatch):
    from tremor import backfill
    from tremor.basket import Basket, VolatilityIndex
    fund = Asset(ticker="KRE", source="twelvedata", provider="alpaca", 
                 block="equity", has_volume=True, tick_size=0.01,
                 session_template="us_equity", fetch_interval="30min",
                 label="KRE", in_basket=True)
    basket = Basket(assets=(fund,), outside=(),
                    volatility_index=VolatilityIndex("VIXCLS", "fred", "1d", "VIX",
                                                     date(1990, 1, 1)),
                    anchor_exchange_tz="America/New_York",
                    history_since=date(2021, 1, 1), session_templates={"us_equity": {}})
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    assert backfill.main(["--skip-vix", "--bars-dir", str(tmp_path)]) == 2


# --- Twelve Data live: batched, in the background -----------------------------

def _td_fund(t):
    return Asset(ticker=t, source="twelvedata", provider="twelvedata", 
                 block="energy", has_volume=True, tick_size=0.01,
                 session_template="us_equity", fetch_interval="30min",
                 label=t, in_basket=True)


def test_twelvedata_funds_go_eight_to_a_request_a_minute_apart(tmp_path, monkeypatch):
    from tremor import backfill
    funds = [_td_fund(f"F{i}") for i in range(10)]
    t0 = int(datetime(2026, 9, 30, 14, tzinfo=timezone.utc).timestamp())
    for a in funds:
        bars.merge(bars.store_path(str(tmp_path), a.file_stem),
                   bars.candles_to_frame([Candle(t0, 1, 1, 1, 1, 0, t0 + 1800)]))
    calls, slept = [], []

    def fake_batch(symbols, interval, start, end, *a, **k):
        calls.append(list(symbols))
        return {s: [Candle(t0 + 3600, 2, 2, 2, 2, 0, t0 + 5400)] for s in symbols}

    monkeypatch.setattr(backfill.twelvedata, "fetch_batch", fake_batch)
    out = backfill.fetch_twelvedata_live(funds, str(tmp_path), "key",
                                         now=datetime(2026, 9, 30, 17, tzinfo=timezone.utc),
                                         sleep=slept.append)
    assert [len(c) for c in calls] == [8, 2]
    assert slept == [backfill.TWELVEDATA_BATCH_GAP_SECONDS]
    assert all(isinstance(r, dict) and r["from_api"] == 1 for _, r in out)


def test_one_symbols_error_in_a_batch_is_that_symbols_alone(tmp_path, monkeypatch):
    from tremor import backfill
    from price_monitor.models import ExchangeError
    funds = [_td_fund("USO"), _td_fund("GLD")]
    t0 = int(datetime(2026, 9, 30, 14, tzinfo=timezone.utc).timestamp())
    for a in funds:
        bars.merge(bars.store_path(str(tmp_path), a.file_stem),
                   bars.candles_to_frame([Candle(t0, 1, 1, 1, 1, 0, t0 + 1800)]))
    monkeypatch.setattr(backfill.twelvedata, "fetch_batch", lambda symbols, *a, **k: {
        "USO": ExchangeError("USO: bad"), "GLD": [Candle(t0 + 3600, 2, 2, 2, 2, 0, t0 + 5400)]})
    out = dict((a.ticker, r) for a, r in backfill.fetch_twelvedata_live(
        funds, str(tmp_path), "key", sleep=lambda s: None))
    assert isinstance(out["USO"], ExchangeError) and out["GLD"]["from_api"] == 1


def test_a_batched_answer_is_split_by_symbol():
    from price_monitor import twelvedata

    class R:
        status_code = 200
        def json(self):
            return {"USO": {"status": "ok", "values": [
                        {"datetime": "2026-09-30 14:00:00", "open": "1", "high": "1",
                         "low": "1", "close": "1", "volume": "5"}]},
                    "GLD": {"status": "error", "code": 400, "message": "**symbol** not found"}}

    class S:
        def get(self, *a, **k):
            return R()

    got = twelvedata.fetch_batch(["USO", "GLD"], "30min",
                                 datetime(2026, 9, 30, tzinfo=timezone.utc),
                                 datetime(2026, 9, 30, 20, tzinfo=timezone.utc),
                                 "https://x", "key", S())
    assert len(got["USO"]) == 1 and got["USO"][0].volume == 5.0
    assert isinstance(got["GLD"], Exception)


def test_an_alpaca_rate_limit_skips_remaining_alpaca_instruments(tmp_path, monkeypatch):
    from tremor import backfill
    from tremor.basket import Basket, VolatilityIndex
    from price_monitor import alpaca

    asked = []
    alerts = []

    def fake_backfill(asset, *a, **k):
        asked.append(asset.ticker)
        if asset.ticker == "LQD":
            raise alpaca.RateLimited("LQD: Alpaca answered 429")
        return {"asset_id": asset.asset_id, "rows": 1, "first": 1, "last": 1,
                "from_api": 1}

    def fund(ticker):
        return Asset(ticker=ticker, source="twelvedata", provider="alpaca",
                     block="credit", has_volume=True, tick_size=0.01,
                     session_template="us_equity", fetch_interval="30min",
                     label=ticker, in_basket=True)

    basket = Basket(
        assets=(fund("LQD"), fund("HYG"), fund("JNK"),
                Asset(ticker="BTC/USDT", source="binance", block="crypto",
                      has_volume=True, tick_size=0.01, session_template="crypto_24_7",
                      fetch_interval="1h", label="Bitcoin", in_basket=True)),
        outside=(),
        volatility_index=VolatilityIndex(
            "VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York",
        history_since=date(2021, 1, 1), session_templates={
            "us_equity": {}, "crypto_24_7": {}},
    )
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    monkeypatch.setattr(backfill, "backfill_instrument", fake_backfill)
    monkeypatch.setattr(backfill._sessions, "load_sessions",
                        lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(backfill, "send_ops_alert", alerts.append)

    backfill.main(["--skip-vix", "--bars-dir", str(tmp_path)])

    # Asked once; HYG and JNK skipped rather than each waiting out Alpaca's retries.
    assert asked == ["LQD", "BTC/USDT"]
    assert any("alpaca" in a.lower() for a in alerts)


@pytest.mark.parametrize("mode, walk", [("--extend-history", True), ("--fill-gaps", True),
                                        ("--live-pass", False)])
def test_only_a_history_walk_takes_the_archive_share(mode, walk, monkeypatch):
    from price_monitor import twelvedata
    from tremor import backfill
    monkeypatch.setattr(twelvedata, "archive_mode", not walk)
    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    monkeypatch.setattr(backfill, "load_basket", lambda: (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        backfill.main([mode])
    assert twelvedata.archive_mode is walk


def test_a_run_seeds_at_most_four_new_instruments(tmp_path, monkeypatch):
    # Thirty-eight empty stores in one run would spend the day's credits and
    # overrun the job; new instruments are seeded a few a run.
    from tremor import backfill
    from tremor.basket import Basket, VolatilityIndex

    asked = []

    def fake_backfill(asset, *a, **k):
        asked.append(asset.ticker)
        return {"asset_id": asset.asset_id, "rows": 1, "first": 1, "last": 1,
                "from_api": 1}

    tickers = ["A", "B", "C", "D", "E", "F"]
    basket = Basket(
        assets=tuple(_yahoo_asset(t) for t in tickers), outside=(),
        volatility_index=VolatilityIndex("VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York", history_since=date(2021, 1, 1),
        session_templates={"us_equity": {}})
    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    monkeypatch.setattr(backfill, "backfill_instrument", fake_backfill)
    monkeypatch.setattr(backfill._sessions, "load_sessions",
                        lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(backfill, "send_ops_alert", lambda *a, **k: None)

    backfill.main(["--skip-vix", "--bars-dir", str(tmp_path)])

    assert asked == tickers[:backfill.SEED_PER_RUN] and backfill.SEED_PER_RUN == 4


def test_new_instruments_waiting_to_be_seeded_are_not_blamed_on_the_calendar(
        tmp_path, monkeypatch, caplog):
    from tremor import backfill
    from tremor.basket import Basket, VolatilityIndex

    def fake_backfill(asset, *a, **k):
        return {"asset_id": asset.asset_id, "rows": 1, "first": 1, "last": 1,
                "from_api": 1}

    basket = Basket(
        assets=tuple(_yahoo_asset(t) for t in "ABCDEF"), outside=(),
        volatility_index=VolatilityIndex("VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York", history_since=date(2021, 1, 1),
        session_templates={"us_equity": {}})
    monkeypatch.setattr(backfill, "load_basket", lambda: basket)
    monkeypatch.setattr(backfill, "backfill_instrument", fake_backfill)
    monkeypatch.setattr(backfill._sessions, "load_sessions",
                        lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(backfill, "send_ops_alert", lambda *a, **k: None)

    with caplog.at_level("INFO", logger="tremor.backfill"):
        backfill.main(["--skip-vix", "--bars-dir", str(tmp_path)])

    assert "the calendar says nothing new can exist" not in caplog.text
    assert "2 new instrument(s) waiting their turn to be seeded" in caplog.text

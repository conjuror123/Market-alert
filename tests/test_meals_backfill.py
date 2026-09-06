import os
from datetime import date, datetime, timezone

import pandas as pd

from meals import bars
from meals.backfill import import_legacy
from meals.basket import Asset
from price_monitor import candle_store
from price_monitor.models import Candle

HOUR = 3600


def asset(**over):
    base = dict(ticker="EUR/USD", source="twelvedata", tier=1, block="FX",
                has_volume=False, tick_size=0.00001, session_template="fx_continuous",
                fetch_interval="1h", label="Euro / dollar", in_basket=True)
    return Asset(**(base | over))


def candle(hour, close=1.5):
    return Candle(open_time=hour * HOUR, open=1.0, high=2.0, low=0.5,
                  close=close, volume=0.0, close_time=(hour + 1) * HOUR)


def test_imports_the_accumulated_ndjson_history(tmp_path):
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    a = asset()
    candle_store.append_candles(
        candle_store.store_path(str(legacy_dir), a.source, a.ticker),
        [candle(1), candle(2), candle(3)])

    path = bars.store_path(str(tmp_path / "bars"), a.file_stem)
    assert import_legacy(a, path, str(legacy_dir)) == 3
    assert list(bars.load(path)["hour_utc"]) == [HOUR, 2 * HOUR, 3 * HOUR]


def test_import_deduplicates_the_repeated_block(tmp_path):
    # The accumulated history contains a block of 299 hours duplicated by a bad
    # branch merge. The store must accept it exactly once.
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    a = asset()
    block = [candle(h) for h in range(1, 6)]
    candle_store.append_candles(
        candle_store.store_path(str(legacy_dir), a.source, a.ticker), block + block)

    path = bars.store_path(str(tmp_path / "bars"), a.file_stem)
    assert import_legacy(a, path, str(legacy_dir)) == 5
    assert len(bars.load(path)) == 5


def test_import_is_idempotent(tmp_path):
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    a = asset()
    candle_store.append_candles(
        candle_store.store_path(str(legacy_dir), a.source, a.ticker), [candle(1)])

    path = bars.store_path(str(tmp_path / "bars"), a.file_stem)
    import_legacy(a, path, str(legacy_dir))
    assert import_legacy(a, path, str(legacy_dir)) == 0


def test_import_without_legacy_file_is_a_no_op(tmp_path):
    path = bars.store_path(str(tmp_path / "bars"), "x")
    assert import_legacy(asset(), path, str(tmp_path / "missing")) == 0
    assert not os.path.exists(path)


def test_deepening_starts_at_the_oldest_stored_bar_not_at_today(tmp_path, monkeypatch):
    # The walk goes backwards, so starting at today spends a credit per chunk
    # re-fetching years already on disk before reaching any new ground. On the
    # free tier's 800 a day that is the difference between reaching 2015 and
    # running out somewhere in 2019.
    import pandas as pd

    from meals import backfill

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
    asset = Asset(ticker="SPY", source="twelvedata", tier=1, block="equity",
                  has_volume=True, tick_size=0.01, session_template="us_equity",
                  fetch_interval="30min", label="S&P 500", in_basket=True)

    backfill.fetch_missing(asset, str(path), date(2015, 1, 1), "key", None,
                           extend_history=True)

    assert seen["end"] == datetime.fromtimestamp(stored_oldest, tz=timezone.utc)
    # and it asks for the span from `since` up to that bar, not up to today
    assert 2340 < seen["days"] < 2360        # 2015-01-01 to 2021-06-01


def test_a_first_ever_fetch_still_walks_back_from_today(tmp_path, monkeypatch):
    from meals import backfill

    seen = {}

    def fake_history(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history", fake_history)
    asset = Asset(ticker="SPY", source="twelvedata", tier=1, block="equity",
                  has_volume=True, tick_size=0.01, session_template="us_equity",
                  fetch_interval="30min", label="S&P 500", in_basket=True)

    backfill.fetch_missing(asset, str(tmp_path / "nope.parquet"), date(2015, 1, 1),
                           "key", None, extend_history=True)
    assert seen["end"] is None


# --- deepening the FX history from FXCM ------------------------------------

def _stored(path, first_hour, count=4):
    return bars.merge(str(path), bars.to_hourly(bars.candles_to_frame([
        Candle(open_time=first_hour + i * HOUR, open=1.0, high=1.0, low=1.0,
               close=1.0, volume=0.0, close_time=first_hour + (i + 1) * HOUR)
        for i in range(count)])))


def _fx_asset(ticker="EUR/USD"):
    return Asset(ticker=ticker, source="twelvedata", tier=1, block="FX",
                 has_volume=False, tick_size=0.00001, session_template="fx",
                 fetch_interval="1h", label=ticker, in_basket=True)


def test_deepening_asks_only_for_the_stretch_below_what_is_stored(tmp_path, monkeypatch):
    # Twelve Data stays the live source for these pairs. FXCM reaches UNDER
    # what is stored and stops, so the two never compete for the same hour and
    # a merge cannot overwrite a live bar with an archived one.
    from meals import backfill

    path = tmp_path / "twelvedata_EUR_USD.parquet"
    oldest = int(datetime(2020, 1, 29, tzinfo=timezone.utc).timestamp())
    _stored(path, oldest)

    seen = {}

    def fake_history(symbol, start, end, session=None, base_url=None):
        seen.update(symbol=symbol, start=start, end=end)
        return []

    monkeypatch.setattr(backfill.fxcm, "fetch_history", fake_history)
    backfill.deepen_from_fxcm(_fx_asset(), str(path), date(2015, 1, 1), None)

    assert seen["symbol"] == "EURUSD"
    assert seen["start"] == date(2015, 1, 1)
    assert seen["end"] == date(2020, 1, 29)      # the oldest stored day, not today


def test_deepening_skips_a_pair_the_archive_does_not_carry(tmp_path):
    from meals import backfill

    path = tmp_path / "twelvedata_USD_CNY.parquet"
    _stored(path, int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()))
    out = backfill.deepen_from_fxcm(_fx_asset("USD/CNY"), str(path),
                                    date(2015, 1, 1), None)
    assert out["added"] == 0 and "no FXCM symbol" in out["skipped"]


def test_deepening_does_nothing_when_the_store_already_reaches_back(tmp_path):
    from meals import backfill

    path = tmp_path / "twelvedata_EUR_USD.parquet"
    _stored(path, int(datetime(2014, 1, 1, tzinfo=timezone.utc).timestamp()))
    out = backfill.deepen_from_fxcm(_fx_asset(), str(path), date(2015, 1, 1), None)
    assert out["added"] == 0 and "far enough" in out["skipped"]


def test_deepening_needs_something_to_deepen(tmp_path):
    # With nothing stored there is no "below" to fill, and the ordinary Twelve
    # Data backfill is what runs first.
    from meals import backfill

    out = backfill.deepen_from_fxcm(_fx_asset(), str(tmp_path / "none.parquet"),
                                    date(2015, 1, 1), None)
    assert out["added"] == 0 and "nothing stored" in out["skipped"]


def test_deepening_merges_the_archived_bars_into_the_store(tmp_path, monkeypatch):
    from meals import backfill

    path = tmp_path / "twelvedata_EUR_USD.parquet"
    oldest = int(datetime(2020, 1, 29, tzinfo=timezone.utc).timestamp())
    _stored(path, oldest)
    older = int(datetime(2019, 6, 1, tzinfo=timezone.utc).timestamp())

    monkeypatch.setattr(backfill.fxcm, "fetch_history",
                        lambda *a, **k: [Candle(open_time=older + i * HOUR,
                                                open=1.1, high=1.2, low=1.0,
                                                close=1.15, volume=0.0,
                                                close_time=older + (i + 1) * HOUR)
                                         for i in range(3)])
    out = backfill.deepen_from_fxcm(_fx_asset(), str(path), date(2015, 1, 1), None)

    assert out["added"] == 3
    frame = bars.load(str(path))
    assert int(frame["hour_utc"].min()) == older
    assert len(frame) == 7          # 4 already there plus 3 reached under them


# --- filling sessions the calendar has and the store does not ---------------

def backfill_runs(days):
    from meals import backfill
    return backfill._runs(days)


def _etf(**over):
    base = dict(ticker="SPY", source="twelvedata", tier=1, block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="30min", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def _day(y, m, d):
    return int(datetime(y, m, d, 15, tzinfo=timezone.utc).timestamp())


def _table(days):
    """A calendar of ordinary 09:30-16:00 sessions for the given days."""
    from meals.sessions import Session
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
    from meals import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    _store_days(path, [_day(2024, 3, 4), _day(2024, 3, 6)])
    table = {date(2024, 3, i): object() for i in (1, 4, 5, 6, 7)}

    assert backfill.missing_sessions(str(path), table) == [date(2024, 3, 5)]


def test_no_gaps_when_the_store_matches_the_calendar(tmp_path):
    from meals import backfill

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
    from meals import backfill

    path = tmp_path / "x.parquet"
    out = backfill.fill_gaps(_etf(session_template="fx_continuous"), str(path),
                             {}, "key", None)
    assert out["added"] == 0 and "calendar" in out["skipped"]


def test_a_recovered_day_is_merged_and_no_longer_missing(tmp_path, monkeypatch):
    from meals import backfill

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
    from meals import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    _store_days(path, [_day(2024, 3, 4), _day(2024, 3, 6)])
    table = {date(2024, 3, i): object() for i in (4, 5, 6)}

    monkeypatch.setattr(backfill.twelvedata, "fetch_full_history", lambda **k: [])
    monkeypatch.setattr(backfill.time, "sleep", lambda *_: None)
    out = backfill.fill_gaps(_etf(), str(path), table, "key", None)

    assert out["gaps"] == 1 and out["added"] == 0
    assert out["still_missing"] == [date(2024, 3, 5)]


def test_a_failed_request_does_not_abandon_the_other_gaps(tmp_path, monkeypatch):
    from meals import backfill
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
    from meals import backfill

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
    from meals import backfill

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
    from meals import backfill

    base = int(datetime(2021, 1, 4, tzinfo=timezone.utc).timestamp())
    tiny = _hf_minutes(base, 10)
    check = backfill.verify_alignment(tiny, bars.to_hourly(tiny))
    assert not check["ok"] and "overlapping hours" in check["why"]


def test_the_etf_import_refuses_to_run_without_a_timezone(tmp_path):
    # Set to None it must stop, because the one thing that cannot be recovered
    # later is a silently misaligned archive.
    from meals import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    _store_days(path, [_day(2021, 1, 4)])
    out = backfill.deepen_from_hfdata(_etf(), str(path), date(2015, 1, 1),
                                      "key", None, timezone_name=None)
    assert out["added"] == 0 and "TIMEZONE" in out["skipped"]


def test_the_etf_import_only_touches_us_equity_instruments(tmp_path):
    from meals import backfill

    out = backfill.deepen_from_hfdata(_etf(session_template="fx_continuous"),
                                      str(tmp_path / "x.parquet"),
                                      date(2015, 1, 1), "key", None)
    assert out["added"] == 0 and "US-equity" in out["skipped"]


def test_a_failed_check_merges_nothing(tmp_path, monkeypatch):
    from meals import backfill

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
    from meals import backfill

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
    from meals import backfill

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
    from meals import corporate_actions
    factor = corporate_actions.unadjust_factor(
        steps, minutes["hour_utc"].to_numpy(), reference, ratio)
    out = minutes.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = out[c].to_numpy() * factor
    return out


def test_the_adjustment_is_undone_and_the_result_matches_the_store():
    from datetime import date as _date

    from meals import backfill

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

    from meals import backfill

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
    from meals import backfill

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
    from meals import backfill

    base = int(datetime(2020, 2, 10, tzinfo=timezone.utc).timestamp())
    tiny = _hf_minutes(base, 120)
    _, info = backfill.unadjust_to_store(tiny, bars.to_hourly(tiny), [])
    assert not info["calibrated"]


def test_the_gap_fill_patches_only_the_missing_days(tmp_path, monkeypatch):
    # This writes INTO the middle of the stored series rather than under it,
    # so it must touch the missing days and nothing else - a patch that also
    # rewrote neighbouring hours would replace vendor A's bars with vendor B's
    # in places the store was already complete.
    from meals import backfill

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
    from meals import backfill

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
    from meals.sessions import Session, session_hours
    return session_hours(Session(day=day, local_open="09:30",
                                 local_close="16:00", is_early_close=False))


def test_a_day_missing_five_of_its_seven_bars_is_not_a_complete_day(tmp_path):
    # The hole that survived the day-level fill: 2020-02-19 was present with
    # two bars of seven, so nothing reported it as a gap.
    from meals import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    days = [date(2024, 3, 4), date(2024, 3, 5)]
    hours = _full_session_hours(days[0]) + _full_session_hours(days[1])[-2:]
    _store_days(path, hours)
    table = _table(days)

    assert backfill.missing_sessions(str(path), table) == []
    missing = backfill.missing_hours(str(path), table)
    assert missing == _full_session_hours(days[1])[:-2]


def test_a_complete_store_has_no_missing_hours(tmp_path):
    from meals import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    days = [date(2024, 3, 4), date(2024, 3, 5)]
    _store_days(path, _full_session_hours(days[0]) + _full_session_hours(days[1]))
    assert backfill.missing_hours(str(path), _table(days)) == []


def test_hours_outside_the_stored_range_are_not_holes(tmp_path):
    # Same bound as the day-level view: the archive simply has not reached
    # there yet, and counting those would bury the real holes.
    from meals import backfill

    path = tmp_path / "twelvedata_SPY.parquet"
    days = [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)]
    _store_days(path, _full_session_hours(days[1]))
    assert backfill.missing_hours(str(path), _table(days)) == []


def test_the_gap_fill_recovers_hours_inside_a_day_that_is_already_present(
        tmp_path, monkeypatch):
    from meals import backfill

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

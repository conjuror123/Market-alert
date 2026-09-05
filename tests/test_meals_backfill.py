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

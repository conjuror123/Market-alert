"""The metrics stage: the same answer whether it runs on one core or four."""
import os
from dataclasses import replace

import pandas as pd
import pytest

from tremor import bars
from tremor.basket import load_basket


def test_the_parallel_path_writes_exactly_what_the_serial_one_does(tmp_path):
    # The instruments are independent - nothing in EUR/USD's metric chain looks
    # at SPY - so the loop parallelises, and the only thing that matters is that
    # it produces the same files. Run on two instruments so the pool has
    # something to distribute and the test still finishes quickly.
    from tremor import pipeline as pl

    basket = load_basket()
    picked = [a for a in basket.instruments if a.ticker in ("SPY", "GLD")]
    if len(picked) < 2 or not all(
            os.path.isdir(bars.store_path(bars.DEFAULT_BARS_DIR, a.file_stem))
            for a in picked):
        pytest.skip("needs the real bar store")
    small = replace(basket, assets=tuple(picked), outside=())

    serial, parallel = tmp_path / "serial", tmp_path / "parallel"
    versions = ("cfg", "run")
    pl.build_all(small, bars.DEFAULT_BARS_DIR, str(serial), versions)
    written = pl.write_all(small, bars.DEFAULT_BARS_DIR, str(parallel), versions,
                           workers=2)

    assert written == 2
    for asset in picked:
        one = pd.read_parquet(pl.metrics_path(str(serial), asset.file_stem))
        two = pd.read_parquet(pl.metrics_path(str(parallel), asset.file_stem))
        assert one.equals(two), asset.ticker


def test_a_single_worker_falls_back_to_the_serial_path(tmp_path, monkeypatch):
    # Not a special case to keep working by accident: it is how the command line
    # turns the pool off, and how anything without a usable multiprocessing
    # context still gets its metrics.
    from tremor import pipeline as pl

    called = []

    def serial(*args, **kwargs):
        called.append(args)
        return {"one": None, "two": None}

    monkeypatch.setattr(pl, "build_all", serial)
    assert pl.write_all(load_basket(), workers=1) == 2
    assert called


# --- extending the stored metrics instead of recomputing them ---------------

def _small_basket(tickers=("SPY", "GLD")):
    basket = load_basket()
    picked = [a for a in basket.instruments if a.ticker in tickers]
    if len(picked) < len(tickers) or not all(
            os.path.isdir(bars.store_path(bars.DEFAULT_BARS_DIR, a.file_stem))
            for a in picked):
        pytest.skip("needs the real bar store")
    return replace(basket, assets=tuple(picked), outside=())


def test_extending_gives_the_same_answer_as_recomputing_everything(tmp_path):
    # THE WHOLE CLAIM. Every window in the chain is bounded, so recomputing only
    # windows.warm_bars of lead-in reproduces a full run. If this ever stops
    # being true the hourly metrics quietly drift from the backtest's.
    import numpy as np
    from tremor import pipeline as pl

    small = _small_basket()
    versions = ("cfg", "run")
    whole, part = tmp_path / "whole", tmp_path / "part"
    pl.write_all(small, bars.DEFAULT_BARS_DIR, str(whole), versions, workers=2, full=True)
    pl.write_all(small, bars.DEFAULT_BARS_DIR, str(part), versions, workers=2, full=True)
    for asset in small.instruments:                       # hold six bars back
        p = pl.metrics_path(str(part), asset.file_stem)
        pd.read_parquet(p).iloc[:-6].to_parquet(p, index=False, compression="zstd")
    pl.write_all(small, bars.DEFAULT_BARS_DIR, str(part), versions, workers=2)

    for asset in small.instruments:
        a = pd.read_parquet(pl.metrics_path(str(whole), asset.file_stem))
        b = pd.read_parquet(pl.metrics_path(str(part), asset.file_stem))
        assert a.shape == b.shape, asset.ticker
        assert a["hour_utc"].tolist() == b["hour_utc"].tolist(), asset.ticker
        for column in ("r", "sigma_lt", "z", "sigma_eff", "q99"):
            x, y = a[column].to_numpy(float), b[column].to_numpy(float)
            ok = np.isfinite(x) & np.isfinite(y)
            worst = np.abs(x[ok] - y[ok]) / np.maximum(np.abs(x[ok]), 1e-12)
            # float64 accumulation order, not a disagreement
            assert worst.max() < 1e-10, (asset.ticker, column, worst.max())


def test_a_different_configuration_forces_the_whole_thing_to_be_rebuilt(tmp_path):
    # The one guard that matters: a change to the CALCULATION means the stored
    # rows are answers to a different question, and extending them would leave
    # the file half one and half the other, with nothing saying so.
    from tremor import pipeline as pl

    small = _small_basket(("SPY",))
    out = tmp_path / "m"
    pl.write_all(small, bars.DEFAULT_BARS_DIR, str(out), ("cfg-one", "run"),
                 workers=2, full=True)
    before = pd.read_parquet(pl.metrics_path(str(out), small.instruments[0].file_stem))
    assert (before["config_version"] == "cfg-one").all()

    pl.write_all(small, bars.DEFAULT_BARS_DIR, str(out), ("cfg-two", "run"), workers=2)
    after = pd.read_parquet(pl.metrics_path(str(out), small.instruments[0].file_stem))
    assert (after["config_version"] == "cfg-two").all()


def test_a_store_too_short_to_lead_in_is_rebuilt_rather_than_extended():
    # Extending needs warm_bars of history BEHIND the new rows. With less than
    # that the windows are still filling, and the rows would come out different
    # from a full run - so the honest answer is to refuse the shortcut.
    from tremor import pipeline as pl

    from tremor import sessions

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
    table = sessions.load_sessions()
    full = pl.build_asset_metrics(asset, small, frame, table, None)
    # a store that stops 200 bars in: far less lead-in than warm_bars needs
    stumpy = full.head(200).assign(config_version="cfg")
    assert pl.extend_asset_metrics(asset, small, frame, table, None,
                                   stumpy, "cfg") is None


def test_nothing_new_returns_the_store_itself_so_the_write_is_skipped():
    # Identity, not equality: the caller skips the parquet write on `is`, which
    # is what stops a quiet hour costing a quarter of a gigabyte.
    #
    # A real store rather than a one-row stub, because the tail is re-scored now
    # and "nothing new" is no longer the same question as "no new bars" - the
    # tail has to come back unchanged as well.
    from tremor import corporate_actions, pipeline as pl, sessions

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    table = sessions.load_sessions()
    actions = corporate_actions.load_actions().get(asset.ticker)
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
    built = pl.build_asset_metrics(asset, small, frame, table, actions)
    stored = built[[c for c in pl.METRIC_COLUMNS if c in built]].assign(
        config_version="cfg", run_version="run")

    assert pl.extend_asset_metrics(asset, small, frame, table, actions,
                                   stored, "cfg", "run") is stored


def test_an_empty_or_absent_store_is_rebuilt():
    from tremor import pipeline as pl

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
    assert pl.extend_asset_metrics(asset, small, frame, {}, None, None, "cfg") is None
    assert pl.extend_asset_metrics(asset, small, frame, {}, None,
                                   pd.DataFrame(), "cfg") is None


def test_a_store_without_a_version_stamp_is_rebuilt():
    from tremor import pipeline as pl

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
    stored = pd.DataFrame({"hour_utc": [int(frame["hour_utc"].max())]})
    assert pl.extend_asset_metrics(asset, small, frame, {}, None, stored, "cfg") is None


def test_added_rows_are_stamped_before_they_are_concatenated(monkeypatch):
    # The tail is shortened to one bar so the arithmetic stays readable; what is
    # under test is the stamping, not how long the tail is.
    #
    # Hour 200 is in the tail, so it is RE-SCORED and carries the new run: a row
    # this run computed says so, whatever an earlier run wrote there. Only hour
    # 100, which was left alone, keeps the old stamp.
    from tremor import pipeline as pl

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    stored = pd.DataFrame({
        "hour_utc": [100, 200],
        "r": [0.0, 0.0],
        "config_version": ["cfg", "cfg"],
        "run_version": ["old", "old"],
    })
    frame = pd.DataFrame({"hour_utc": [100, 200, 300]})
    monkeypatch.setattr(pl, "RECOMPUTE_TAIL_BARS", 1)
    monkeypatch.setattr(pl, "bars_per_session", lambda *a, **k: 1)
    monkeypatch.setattr(pl.windows, "w_asset", lambda *_: 1)
    monkeypatch.setattr(pl.windows, "warm_bars", lambda *_: 1)
    monkeypatch.setattr(
        pl, "build_asset_metrics",
        lambda *a, **k: pd.DataFrame({"hour_utc": [200, 300], "r": [0.02, 0.1]}))
    out = pl.extend_asset_metrics(asset, small, frame, {}, None, stored, "cfg", "new")
    assert list(out["hour_utc"]) == [100, 200, 300]
    assert list(out["config_version"]) == ["cfg", "cfg", "cfg"]
    assert list(out["run_version"]) == ["old", "new", "new"]
    assert list(out["r"]) == [0.0, 0.02, 0.1]      # 200 re-scored, not kept


def test_an_hour_first_scored_part_way_through_is_rescored_when_it_closes():
    # THE ONE THAT WENT WRONG. The hourly run fires five minutes past the hour
    # and stores a bar for the hour it is standing in - two to thirteen per cent
    # of that hour's volume, measured on the committed store. The bars heal on
    # the next fetch; the metrics did not, because an extension only computed
    # hours newer than the store's last one, so the complete bar arrived to find
    # its hour already written. Every hour was judged on its first five minutes.
    #
    # Live: 2026-09-16 18:00 UTC, the FOMC statement. SHY closed the hour at
    # -0.19%, a six-sigma move; the store held the +0.02% it had made of the
    # first five minutes, and thirteen instruments' events went missing.
    from tremor import corporate_actions, pipeline as pl, sessions

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    table = sessions.load_sessions()
    actions = corporate_actions.load_actions().get(asset.ticker)
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))

    truth = pl.build_asset_metrics(asset, small, frame, table, actions)
    hour = int(truth["hour_utc"].iloc[-1])
    settled = float(truth.loc[truth.hour_utc == hour, "r"].iloc[0])

    # The hour as it looked five minutes in: barely off the open.
    partway = frame.copy()
    row = partway.index[partway.hour_utc == hour][0]
    opened = float(partway.at[row, "open"])
    partway.loc[row, ["high", "low", "close"]] = [opened * 1.0002, opened * 0.9998,
                                                  opened * 1.0001]

    # What the run at :05 wrote, and what every run after it extended from.
    early = pl.build_asset_metrics(asset, small, partway[partway.hour_utc <= hour],
                                   table, actions)
    stored = early[[c for c in pl.METRIC_COLUMNS if c in early]].assign(
        config_version="cfg", run_version="run")
    provisional = float(stored.loc[stored.hour_utc == hour, "r"].iloc[0])
    assert provisional != pytest.approx(settled), \
        "the fixture must actually differ, or this test proves nothing"

    out = pl.extend_asset_metrics(asset, small, frame, table, actions,
                                  stored, "cfg", "run")
    assert out is not None
    after = float(out.loc[out.hour_utc == hour, "r"].iloc[0])
    assert after == pytest.approx(settled, abs=1e-12), \
        "the complete bar must replace what five minutes of it said"


def test_the_recomputed_tail_does_not_duplicate_or_lose_an_hour():
    # Re-scoring the tail means dropping rows from the store and putting them
    # back. An off-by-one here would either double an hour or drop one, and
    # every window downstream is computed by position.
    from tremor import corporate_actions, pipeline as pl, sessions

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    table = sessions.load_sessions()
    actions = corporate_actions.load_actions().get(asset.ticker)
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))

    truth = pl.build_asset_metrics(asset, small, frame, table, actions)
    stored = truth[[c for c in pl.METRIC_COLUMNS if c in truth]].assign(
        config_version="cfg", run_version="run")

    out = pl.extend_asset_metrics(asset, small, frame, table, actions,
                                  stored, "cfg", "run")
    hours = out["hour_utc"].tolist()
    assert hours == sorted(hours)
    assert len(hours) == len(set(hours))
    assert hours == truth["hour_utc"].tolist()

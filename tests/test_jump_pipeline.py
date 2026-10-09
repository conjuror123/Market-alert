"""The metrics stage: the same answer whether it runs on one core or four."""
import os
from dataclasses import replace

import pandas as pd
import pytest

from jump import bars
from jump.basket import load_basket


def test_the_parallel_path_writes_exactly_what_the_serial_one_does(tmp_path):
    # The instruments are independent - nothing in EUR/USD's metric chain looks
    # at SPY - so the loop parallelises, and the only thing that matters is that
    # it produces the same files. Run on two instruments so the pool has
    # something to distribute and the test still finishes quickly.
    from jump import pipeline as pl

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
    from jump import pipeline as pl

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
    # THE WHOLE CLAIM. Nothing in the chain looks back further than a few
    # sessions, so recomputing windows.warm_bars of lead-in reproduces a full
    # run. If this ever stops being true the hourly metrics quietly drift from
    # the backtest's.
    import numpy as np
    from jump import pipeline as pl

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
        for column in ("r", "gap"):
            x, y = a[column].to_numpy(float), b[column].to_numpy(float)
            assert (np.isnan(x) == np.isnan(y)).all(), (asset.ticker, column)
            ok = np.isfinite(x) & np.isfinite(y)
            worst = np.abs(x[ok] - y[ok]) / np.maximum(np.abs(x[ok]), 1e-12)
            # float64 accumulation order, not a disagreement
            assert worst.max() < 1e-10, (asset.ticker, column, worst.max())


def test_a_different_configuration_forces_the_whole_thing_to_be_rebuilt(tmp_path):
    # The one guard that matters: a change to the CALCULATION means the stored
    # rows are answers to a different question, and extending them would leave
    # the file half one and half the other, with nothing saying so.
    from jump import pipeline as pl

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
    from jump import pipeline as pl

    from jump import sessions

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
    table = sessions.load_sessions()
    full = pl.build_asset_metrics(asset, small, frame, table)
    # a store that stops 200 bars in: far less lead-in than warm_bars needs
    stumpy = full.head(200).assign(config_version="cfg")
    assert pl.extend_asset_metrics(asset, small, frame, table,
                                   stumpy, "cfg") is None


def test_nothing_new_returns_the_store_itself_so_the_write_is_skipped():
    # Identity, not equality: the caller skips the parquet write on `is`, which
    # is what stops a quiet hour costing a quarter of a gigabyte.
    #
    # A real store rather than a one-row stub, because the tail is re-scored now
    # and "nothing new" is no longer the same question as "no new bars" - the
    # tail has to come back unchanged as well.
    from jump import pipeline as pl, sessions

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    table = sessions.load_sessions()
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
    built = pl.build_asset_metrics(asset, small, frame, table)
    stored = built[[c for c in pl.METRIC_COLUMNS if c in built]].assign(
        config_version="cfg", run_version="run")

    assert pl.extend_asset_metrics(asset, small, frame, table,
                                   stored, "cfg", "run") is stored


def test_an_empty_or_absent_store_is_rebuilt():
    from jump import pipeline as pl

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
    assert pl.extend_asset_metrics(asset, small, frame, {}, None, "cfg") is None
    assert pl.extend_asset_metrics(asset, small, frame, {},
                                   pd.DataFrame(), "cfg") is None


def test_a_store_without_a_version_stamp_is_rebuilt():
    from jump import pipeline as pl

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
    stored = pd.DataFrame({"hour_utc": [int(frame["hour_utc"].max())]})
    assert pl.extend_asset_metrics(asset, small, frame, {}, stored, "cfg") is None


def test_added_rows_are_stamped_before_they_are_concatenated(monkeypatch):
    # The tail is shortened to one bar so the arithmetic stays readable; what is
    # under test is the stamping, not how long the tail is.
    #
    # Hour 200 is in the tail, so it is RE-SCORED and carries the new run: a row
    # this run computed says so, whatever an earlier run wrote there. Only hour
    # 100, which was left alone, keeps the old stamp.
    from jump import pipeline as pl

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    stored = pd.DataFrame({
        "hour_utc": [100, 200],
        "r": [0.0, 0.0],
        "bars_upto": [1, 2],
        "config_version": ["cfg", "cfg"],
        "run_version": ["old", "old"],
    })
    frame = pd.DataFrame({"hour_utc": [100, 200, 300]})
    monkeypatch.setattr(pl, "RECOMPUTE_TAIL_BARS", 1)
    monkeypatch.setattr(pl.windows, "warm_bars", lambda *_, **__: 1)
    monkeypatch.setattr(
        pl, "build_asset_metrics",
        lambda *a, **k: pd.DataFrame({"hour_utc": [200, 300], "r": [0.02, 0.1]}))
    out = pl.extend_asset_metrics(asset, small, frame, {}, stored, "cfg", "new")
    assert list(out["hour_utc"]) == [100, 200, 300]
    assert list(out["config_version"]) == ["cfg", "cfg", "cfg"]
    assert list(out["run_version"]) == ["old", "new", "new"]
    assert list(out["r"]) == [0.0, 0.02, 0.1]      # 200 re-scored, not kept


def test_bars_written_among_the_settled_rows_force_a_rebuild():
    # A deepening or a hole filled from another source lands under or among
    # rows already scored, which were computed without it: extending would
    # leave those hours out of the metrics for good.
    from jump import pipeline as pl, sessions

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    table = sessions.load_sessions()
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
    cut = frame.iloc[200:]                                   # before the deepening
    built = pl.build_asset_metrics(asset, small, cut, table)
    stored = built[[c for c in pl.METRIC_COLUMNS if c in built]].assign(
        config_version="cfg", run_version="run")
    assert pl.extend_asset_metrics(asset, small, cut, table,
                                   stored, "cfg", "run") is stored
    assert pl.extend_asset_metrics(asset, small, frame, table,
                                   stored, "cfg", "run") is None
    # And a store from before the count was kept is rebuilt once.
    assert pl.extend_asset_metrics(asset, small, cut, table,
                                   stored.drop(columns="bars_upto"), "cfg", "run") is None


def test_bars_upto_counts_the_whole_store_on_an_extension():
    from jump import pipeline as pl, sessions

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    table = sessions.load_sessions()
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
    truth = pl.build_asset_metrics(asset, small, frame, table)
    early = pl.build_asset_metrics(asset, small, frame.iloc[:-300], table)
    stored = early[[c for c in pl.METRIC_COLUMNS if c in early]].assign(
        config_version="cfg", run_version="run")
    out = pl.extend_asset_metrics(asset, small, frame, table, stored, "cfg", "run")
    assert out is not None
    assert list(out["bars_upto"]) == list(truth["bars_upto"])


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
    from jump import pipeline as pl, sessions

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    table = sessions.load_sessions()
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))

    truth = pl.build_asset_metrics(asset, small, frame, table)
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
                                   table)
    stored = early[[c for c in pl.METRIC_COLUMNS if c in early]].assign(
        config_version="cfg", run_version="run")
    provisional = float(stored.loc[stored.hour_utc == hour, "r"].iloc[0])
    assert provisional != pytest.approx(settled), \
        "the fixture must actually differ, or this test proves nothing"

    out = pl.extend_asset_metrics(asset, small, frame, table,
                                  stored, "cfg", "run")
    assert out is not None
    after = float(out.loc[out.hour_utc == hour, "r"].iloc[0])
    assert after == pytest.approx(settled, abs=1e-12), \
        "the complete bar must replace what five minutes of it said"


def test_a_bar_revised_hours_back_is_rescored_not_only_the_last_one():
    # The tail is two days, not one hour: a provider that corrects a bar
    # several hours later, or a stretch of missed runs, still has its hour
    # re-scored. Here the bar ten hours from the end was stored wrong and the
    # store's metrics were built from it.
    from jump import pipeline as pl, sessions

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    table = sessions.load_sessions()
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))

    truth = pl.build_asset_metrics(asset, small, frame, table)
    hour = int(truth["hour_utc"].iloc[-10])
    settled = float(truth.loc[truth.hour_utc == hour, "r"].iloc[0])

    wrong = frame.copy()
    row = wrong.index[wrong.hour_utc == hour][0]
    opened = float(wrong.at[row, "open"])
    wrong.loc[row, ["high", "low", "close"]] = [opened * 1.0002, opened * 0.9998,
                                                opened * 1.0001]
    early = pl.build_asset_metrics(asset, small, wrong, table)
    stored = early[[c for c in pl.METRIC_COLUMNS if c in early]].assign(
        config_version="cfg", run_version="run")
    assert float(stored.loc[stored.hour_utc == hour, "r"].iloc[0]) != \
        pytest.approx(settled), "the fixture must actually differ"

    out = pl.extend_asset_metrics(asset, small, frame, table,
                                  stored, "cfg", "run")
    assert out is not None
    after = float(out.loc[out.hour_utc == hour, "r"].iloc[0])
    assert after == pytest.approx(settled, abs=1e-12), \
        "a corrected bar inside the last two days must replace what was stored"


def test_the_recomputed_tail_does_not_duplicate_or_lose_an_hour():
    # Re-scoring the tail means dropping rows from the store and putting them
    # back. An off-by-one here would either double an hour or drop one, and
    # every window downstream is computed by position.
    from jump import pipeline as pl, sessions

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    table = sessions.load_sessions()
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))

    truth = pl.build_asset_metrics(asset, small, frame, table)
    stored = truth[[c for c in pl.METRIC_COLUMNS if c in truth]].assign(
        config_version="cfg", run_version="run")

    out = pl.extend_asset_metrics(asset, small, frame, table,
                                  stored, "cfg", "run")
    hours = out["hour_utc"].tolist()
    assert hours == sorted(hours)
    assert len(hours) == len(set(hours))
    assert hours == truth["hour_utc"].tolist()


# --- one instrument's error is that instrument's (F8) -------------------------

def test_one_instruments_error_leaves_the_others_and_its_stored_metrics(tmp_path, monkeypatch):
    from jump import pipeline as pl

    small = _small_basket(("SPY", "GLD"))
    spy, gld = sorted(small.instruments, key=lambda a: a.ticker != "SPY")
    stale = tmp_path / f"{spy.file_stem}.parquet"
    pd.DataFrame({"hour_utc": [1]}).to_parquet(stale)
    real = pl.build_asset_metrics

    def broken_for_spy(asset, *a, **k):
        if asset.ticker == "SPY":
            raise ValueError("a bad bar")
        return real(asset, *a, **k)

    monkeypatch.setattr(pl, "build_asset_metrics", broken_for_spy)
    failures = []
    built = pl.build_all(small, bars.DEFAULT_BARS_DIR, str(tmp_path), ("cfg", "run"), failures)
    assert list(built) == [gld.asset_id]
    assert failures == [(spy.asset_id, "a bad bar")]
    assert pd.read_parquet(stale)["hour_utc"].tolist() == [1]     # kept as it was


def test_a_worker_returns_an_instruments_error_instead_of_raising(monkeypatch):
    from jump import pipeline as pl

    asset = load_basket().instruments[0]
    monkeypatch.setattr(pl, "_pool_compute",
                        lambda payload: (_ for _ in ()).throw(ValueError("a bad bar")))
    assert pl._pool_one((asset, None)) == (asset.asset_id, None, "a bad bar")


def test_a_split_or_payout_declared_for_an_old_date_forces_a_rebuild():
    # The hourly run recomputes only its last rows, so a split declared for
    # 2019 (USHY's 6-for-5) would never reach that day's gap. The metrics
    # carry what they were computed with; a fund whose payouts moved is
    # rebuilt, one whose payouts did not is extended as before.
    from jump import corporate_actions as ca, pipeline as pl, sessions

    small = _small_basket(("SPY",))
    asset = small.instruments[0]
    table = sessions.load_sessions()
    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem))
    before = ca.Dividends(steps={"SPY": {"2010-03-19": 0.005}}, splits={},
                          checked_through={"SPY": "2030-01-01"})
    built = pl.build_asset_metrics(asset, small, frame.iloc[:-300], table, before)
    stored = built[[c for c in pl.METRIC_COLUMNS if c in built]].assign(
        config_version="cfg", run_version="run",
        actions_version=ca.fingerprint(before, "SPY"))
    out = pl.extend_asset_metrics(asset, small, frame, table, stored, "cfg", "run", before)
    assert out is not None and set(out["actions_version"]) == {ca.fingerprint(before, "SPY")}
    later = ca.Dividends(steps=before.steps, splits={"SPY": frozenset({"2005-06-09"})},
                         checked_through={"SPY": "2031-01-01"})
    assert pl.extend_asset_metrics(asset, small, frame, table, stored, "cfg", "run",
                                   later) is None
    # The checked-through date alone moves every day, and is no reason.
    moved = ca.Dividends(steps=before.steps, splits={}, checked_through={"SPY": "2031-01-01"})
    assert pl.extend_asset_metrics(asset, small, frame, table, stored, "cfg", "run",
                                   moved) is not None

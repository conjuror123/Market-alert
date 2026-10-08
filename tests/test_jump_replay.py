"""The history replay (jump.replay): the live vote over all history."""
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from jump import bars, replay, verify
from jump.basket import load_basket

HOUR = 3600


def ts(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


@pytest.fixture(scope="module")
def basket():
    return {a.ticker: a for a in load_basket().instruments}


def _coin(tmp_path, basket, bad=(), lift=None):
    """BTC's hours over 200 days from 2026-01-01, quiet; `bad` hours are a
    bad print of ±4% in turn, `lift` a 6σ move. Returns the asset, the store
    and the market as another exchange saw it (no bad prints)."""
    asset = basket["BTC/USDT"]
    hours = ts("2026-01-01 00:00") + HOUR * np.arange(24 * 200)
    rng = np.random.default_rng(11)
    r = rng.normal(0, 0.001, len(hours))
    if lift is not None:
        r[lift] = 0.006
    clean = r.copy()
    for i, k in enumerate(bad):
        r[k] = 0.04 if i % 2 == 0 else -0.04

    def frame(moves):
        close = 100 * np.exp(np.cumsum(moves))
        opened = np.r_[100, close[:-1]]
        return pd.DataFrame({"hour_utc": hours, "open": opened, "close": close,
                             "high": np.maximum(opened, close), "low": np.minimum(opened, close),
                             "volume": 1.0, "n_src": 1})

    store = frame(r)
    bars.write(bars.store_path(str(tmp_path / "bars"), asset.file_stem), store)
    return asset, hours, store, frame(clean)


def test_a_source_votes_only_on_the_hours_its_bars_span():
    v = pd.DataFrame({"hour_utc": [ts("2026-03-01 00:00"), ts("2026-03-10 00:00")]})
    inside = {"prev_hour": ts("2026-03-05 10:00"), "hour": ts("2026-03-05 11:00")}
    before = {"prev_hour": ts("2026-02-01 10:00"), "hour": ts("2026-02-01 11:00")}
    assert replay.covers(v, inside)          # and there, no bars around it is an outage
    assert not replay.covers(v, before)      # a coin before its listing there: no voter
    assert not replay.covers(None, inside)


def test_the_dry_run_votes_on_history_and_leaves_the_record_alone(monkeypatch, tmp_path,
                                                                  basket):
    # Six bad hours in March, long before the coins' 29-day recount: the
    # other exchanges were flat, so they are not real - written to the dry
    # run, not to the record.
    bad = range(24 * 70, 24 * 70 + 6)
    asset, hours, store, market = _coin(tmp_path, basket, bad=bad)
    monkeypatch.setattr(replay, "fetch", lambda src, a, stored, session, now: market)
    record = str(tmp_path / "verified.csv")
    verify.write({}, 0, record)
    now = datetime.fromtimestamp(int(hours[-1]) + 2 * HOUR, timezone.utc)
    out = str(tmp_path / "out")
    replay.replay([asset], str(tmp_path / "bars"), None, out, now=now, record_path=record)
    votes = verify.load(os.path.join(out, "votes.csv"))
    first = (asset.asset_id, int(hours[bad[0]]), "close")
    assert votes[first]["verdict"] == verify.NOT_REAL
    assert votes[first]["verifier"] == "coinbase,kraken,bitstamp,bitfinex"
    assert verify.load(record) == {}
    changes = pd.read_csv(os.path.join(out, "changes.csv"))
    assert ((changes["before"] == "-") & (changes["after"] == verify.NOT_REAL)).sum() >= 6
    # The sources' bars around each move are kept for the separate check.
    assert os.path.exists(os.path.join(out, "bars", "coinbase", f"{asset.file_stem}.parquet"))


def test_apply_replaces_only_the_replayed_range(monkeypatch, tmp_path, basket):
    bad = range(24 * 70, 24 * 70 + 6)
    asset, hours, store, market = _coin(tmp_path, basket, bad=bad)
    monkeypatch.setattr(replay, "fetch", lambda src, a, stored, session, now: market)
    now = datetime.fromtimestamp(int(hours[-1]) + 2 * HOUR, timezone.utc)
    stale = (asset.asset_id, int(hours[24 * 40]), "close")      # an old vote, no move now
    recent = (asset.asset_id, int(hours[-5]), "close")         # inside the live recount
    record = str(tmp_path / "verified.csv")
    row = lambda k, v: {"asset_id": k[0], "hour_utc": k[1], "check": k[2], "verdict": v}
    verify.write({stale: row(stale, verify.NOT_REAL), recent: row(recent, verify.NOT_REAL)},
                 int(now.timestamp()), record)
    out = str(tmp_path / "out")
    replay.replay([asset], str(tmp_path / "bars"), None, out, now=now, record_path=record)
    replay.apply(out, record, now=now)
    after = verify.load(record)
    assert stale not in after                                   # replaced by the replay
    assert after[recent]["verdict"] == verify.NOT_REAL          # the live count's, kept
    assert after[(asset.asset_id, int(hours[bad[0]]), "close")]["verdict"] == verify.NOT_REAL


def test_a_move_lifted_once_a_bad_stretch_is_voted_out_is_voted_on_next_pass(
        monkeypatch, tmp_path, basket):
    # The bad stretch swells the yardstick; voted not real in the first pass,
    # it leaves it, and the 6σ move forty days on is asked about in the next.
    bad = range(24 * 100, 24 * 100 + 12)
    lift = 24 * 140 + 5
    asset, hours, store, market = _coin(tmp_path, basket, bad=bad, lift=lift)
    monkeypatch.setattr(replay, "fetch", lambda src, a, stored, session, now: market)
    record = str(tmp_path / "verified.csv")
    verify.write({}, 0, record)
    now = datetime.fromtimestamp(int(hours[-1]) + 2 * HOUR, timezone.utc)
    out = str(tmp_path / "out")
    replay.replay([asset], str(tmp_path / "bars"), None, out, now=now, record_path=record)
    votes = verify.load(os.path.join(out, "votes.csv"))
    assert votes[(asset.asset_id, int(hours[lift]), "close")]["verdict"] == verify.REAL


def test_the_softs_contract_series_never_uses_the_stores_front_contract(monkeypatch, basket):
    from jump import futures
    from price_monitor import yahoo
    from price_monitor.models import Candle

    asset = basket["KC=F"]
    hour = ts("2026-09-15 14:00")
    front = futures.front_contract("KC=F", datetime.fromtimestamp(hour, timezone.utc).date())[0]

    order = [sym for sym, _ in futures.roll_days("KC=F", 2023, 2028)]

    def history(symbol, interval, days, session=None, **k):
        # Yahoo forgets an expired contract: the front is the oldest it serves.
        if order.index(symbol) < order.index(front):
            raise yahoo.ExchangeError(f"{symbol}: unknown to Yahoo (404)")
        price = 100.0 if symbol == front else 200.0
        return [Candle(open_time=hour, open=price, high=price, low=price, close=price,
                       volume=1, close_time=hour + HOUR)]

    monkeypatch.setattr(yahoo, "fetch_full_history", history)
    series = replay._contracts(asset, None)
    assert list(series["close"]) == [200.0]

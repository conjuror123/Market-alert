"""tools/history_check.py: the whole history asked of a second source that reaches it."""
from datetime import datetime, timezone

import pandas as pd
import pytest

from jump import verify
from tools import history_check as hc

H = 3600


def ts(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def hourly(start, closes, opens=None):
    t0 = ts(start)
    opens = opens or closes
    return pd.DataFrame({"hour_utc": [t0 + k * H for k in range(len(closes))],
                         "open": opens, "close": closes})


def test_the_same_feed_is_told_from_another():
    store = hourly("2022-07-04 00:00", [4.60 + 0.001 * k for k in range(80)])
    assert hc.same_feed(store, store.copy()) is True
    other = store.assign(close=store["close"] * 1.0001)          # 1 bp apart
    assert hc.same_feed(store, other) is False
    assert hc.same_feed(store.head(10), store.head(10)) is None   # too few to tell


def test_a_flagged_hour_is_the_close_check_and_a_gap_the_open_one():
    table = pd.DataFrame({"asset_id": ["x", "x", "x", "y"],
                          "reading": ["hour", "weekend", "hour", "hour"],
                          "hour_utc": [1, 2, 3, 4],
                          "word": ["high", "extreme", None, "high"]})
    assert hc.flagged(table, "x") == {(1, verify.CLOSE), (2, verify.OPEN)}


def test_usd_pln_flat_bars_15_percent_off_are_not_seen_by_dukascopy():
    # 2022-07-31: the store's Sunday opened at 3.96835 after Friday's 4.63341
    # close - two flat bars, then back. Dukascopy kept trading at 4.63-4.64.
    c = {"hour": ts("2022-07-31 21:00"), "check": verify.OPEN, "from_open": False,
         "prev_hour": ts("2022-07-29 20:00"), "prev_close": 4.63341, "price": 3.96835}
    duka = pd.DataFrame({"hour_utc": [ts("2022-07-29 19:00"), ts("2022-07-29 20:00"),
                                      ts("2022-07-31 21:00"), ts("2022-07-31 22:00")],
                         "open": [4.6365, 4.6360, 4.6402, 4.6410],
                         "close": [4.6360, 4.6331, 4.6410, 4.6405]})
    verdict, stored, _, _ = verify.judge_all(c, [("dukascopy", duka)], ts("2022-08-02 00:00"))
    assert verdict == verify.UNCONFIRMED
    assert stored == pytest.approx(-0.1548, abs=1e-3)


def test_a_month_from_the_stores_own_feed_is_skipped_not_confirmed(monkeypatch):
    from jump.basket import load_basket

    store = hourly("2022-07-01 00:00", [4.60 + 0.0001 * k for k in range(24 * 40)])
    c = {"hour": ts("2022-07-20 12:00"), "check": verify.CLOSE, "from_open": False,
         "prev_hour": ts("2022-07-20 11:00"), "prev_close": 4.6, "price": 4.9}
    pln = next(a for a in load_basket().instruments if a.ticker == "USD/PLN")
    table = pd.DataFrame({"asset_id": [pln.asset_id], "reading": ["hour"],
                          "hour_utc": [c["hour"]], "word": ["extreme"]})
    monkeypatch.setattr(hc.verify, "candidates", lambda *a, **k: [dict(c)])
    monkeypatch.setattr(hc.bars, "load", lambda path, since=None: store)
    monkeypatch.setattr(hc, "_source", lambda kind, asset: (
        "dukascopy", "USDPLN", lambda y, m, sess: _candles(store)))
    record = {}
    rows = hc.check("pairs", table, datetime(2026, 10, 7, tzinfo=timezone.utc), record,
                    only={"USD/PLN"})
    assert rows == [] and record == {}


def _candles(frame):
    from price_monitor.models import Candle
    return [Candle(open_time=int(h), open=o, high=max(o, c), low=min(o, c), close=c,
                   volume=1.0, close_time=int(h) + H)
            for h, o, c in zip(frame["hour_utc"], frame["open"], frame["close"])]

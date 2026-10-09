"""tools/tape_bars.py: Alpaca's tape for every US fund, saved for a measurement."""
import os
from datetime import datetime, timezone

import pandas as pd

from jump.basket import load_basket
from price_monitor.models import Candle
from tools import tape_bars


def test_each_fund_is_saved_under_its_old_name_and_a_failure_is_listed(monkeypatch, tmp_path):
    from price_monitor import alpaca

    basket = {a.ticker: a for a in load_basket().instruments}
    funds = [basket["SPY"], basket["IGIB"], basket["QQQ"], basket["BTC/USDT"]]
    asked = []

    def fetch(symbol, start, end, auth, session=None, feed="sip"):
        asked.append((symbol, feed))
        if symbol == "QQQ":
            raise RuntimeError("status 503")
        return [Candle(open_time=1600000000, open=1.0, high=1.0, low=1.0, close=1.0,
                       volume=1.0, close_time=1600001800)]

    monkeypatch.setattr(alpaca, "fetch_history", fetch)
    start, end = (datetime(y, 1, 1, tzinfo=timezone.utc) for y in (2020, 2023))
    failed = tape_bars.dump(funds, str(tmp_path), start, end, {}, pause=0)

    # The coin is no fund; IGIB is asked as CIU, its name before 2018-07.
    assert asked == [("SPY", "sip"), ("CIU", "sip"), ("QQQ", "sip")]
    assert len(pd.read_parquet(tmp_path / f"{basket['SPY'].file_stem}.parquet")) == 1
    assert os.path.exists(tmp_path / f"{basket['IGIB'].file_stem}.parquet")
    assert not os.path.exists(tmp_path / f"{basket['QQQ'].file_stem}.parquet")
    assert failed == ["QQQ: status 503"]
    assert "QQQ: status 503" in (tmp_path / "failed.txt").read_text()

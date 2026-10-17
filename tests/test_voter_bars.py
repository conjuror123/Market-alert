"""tools/voter_bars.py: every live voter's bars, saved for a measurement."""
import os
from datetime import datetime, timezone

import pandas as pd

from jump import verify
from jump.basket import load_basket
from tools import voter_bars


def test_each_voter_is_saved_and_a_failure_is_listed(monkeypatch, tmp_path):
    asset = next(a for a in load_basket().instruments if a.ticker == "BTC/USDT")
    asked = []

    def fetch(name, symbol, interval, days, session, now):
        asked.append((name, days))
        if name == "kraken":
            raise RuntimeError("status 503")
        return pd.DataFrame({"hour_utc": [1], "open": [1.0], "high": [1.0], "low": [1.0],
                             "close": [1.0], "volume": [0.0]})

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    failed = voter_bars.dump([asset], str(tmp_path), 35, now=datetime(2026, 10, 8,
                                                                       tzinfo=timezone.utc))
    assert os.path.exists(tmp_path / "coinbase" / f"{asset.file_stem}.parquet")
    assert not os.path.exists(tmp_path / "kraken")
    assert failed == ["BTC/USDT kraken: status 503"]
    assert "BTC/USDT kraken: status 503" in (tmp_path / "failed.txt").read_text()
    # Each source over the shorter of the days asked and its own reach.
    assert dict(asked) == {"yahoo": 35, "coinbase": 35, "kraken": 29, "bitstamp": 35,
                           "bitfinex": 35, "alpaca_crypto": 35}


def test_the_recount_fetch_asks_each_instrument_back_to_the_bar_before_its_window(
        monkeypatch, tmp_path):
    # EUR/USD's window is nine days (MarketWatch's reach): from a Monday noon, it
    # starts on the Saturday before last. The store's last bar before it is that
    # Friday's 20:00: the earliest move it could vote, Sunday's open, starts there -
    # and the pass asks a day more. Not capped by a source's reach: the pass is not
    # either.
    from jump import bars

    asset = next(a for a in load_basket().instruments if a.ticker == "EUR/USD")
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)          # a Monday
    friday = int(datetime(2026, 9, 25, 20, tzinfo=timezone.utc).timestamp())
    sunday = int(datetime(2026, 9, 27, 21, tzinfo=timezone.utc).timestamp())
    hours = [friday - 3600 * k for k in range(48, -1, -1)] + \
        [sunday + 3600 * k for k in range(24 * 9)]
    frame = pd.DataFrame({"hour_utc": hours, "open": 1.1, "high": 1.1, "low": 1.1,
                          "close": 1.1, "volume": 0.0, "n_src": 1})
    bars.write(bars.store_path(str(tmp_path / "bars"), asset.file_stem), frame)
    asked = []

    def fetch(name, symbol, interval, days, session, now):
        asked.append((name, days))
        return pd.DataFrame({"hour_utc": [1], "open": [1.0], "high": [1.0], "low": [1.0],
                             "close": [1.0], "volume": [0.0]})

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    # One no source votes on has no window, and is passed by.
    from dataclasses import replace
    unvoted = replace(asset, ticker="NOK/SEK")
    assert not verify.sources_for(unvoted)
    voter_bars.dump([unvoted, asset], str(tmp_path / "out"), None, now=now,
                    bars_dir=str(tmp_path / "bars"))
    span = (now.timestamp() - friday) / 86400 + 1
    assert dict(asked) == {"yahoo": span, "marketwatch": span, "wallstreetcn": span}
    assert (tmp_path / "out" / "failed.txt").read_text().splitlines()[0] == \
        "fetched 2026-10-05 12:00 UTC, each instrument's recount window"

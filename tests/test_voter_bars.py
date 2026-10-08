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
    assert dict(asked) == {"coinbase": 35, "kraken": 29}

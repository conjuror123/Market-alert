"""The store builders in tools/ rewrite stores from the network, so each must be
told what to rebuild: run bare, or with an instrument it does not build, it
stops with a usage error before asking anything or writing anything."""
import importlib

import pytest
import requests

from tremor import bars


@pytest.fixture
def untouchable(monkeypatch):
    def touched(*a, **k):
        pytest.fail("a builder touched a store or the network")

    for name in ("write", "merge"):
        monkeypatch.setattr(bars, name, touched)
    monkeypatch.setattr(requests, "get", touched)
    monkeypatch.setattr(requests.Session, "get", touched)


@pytest.mark.parametrize("module, argv", [
    ("tools.futures_history", []),
    ("tools.futures_history", ["NOPE=F"]),
    ("tools.futures_history", ["KC=F"]),            # built from Dukascopy instead
    ("tools.binance_history", []),
    ("tools.binance_history", ["NOPE/USDT"]),
    ("tools.dukascopy_futures", []),
    ("tools.dukascopy_futures", ["NOPE=F", "dumps/"]),
    ("tools.futures_unmix", []),
    ("tools.futures_unmix", ["NOPE=F"]),
    ("tools.dukascopy_dump", []),
    ("tools.dukascopy_dump", ["COTTONCMDUSX", "not-a-year", "out/"]),
])
def test_a_builder_must_be_told_what_to_rebuild(module, argv, untouchable):
    tool = importlib.import_module(module)
    with pytest.raises(SystemExit) as stopped:
        tool.main(argv)
    assert stopped.value.code == 2

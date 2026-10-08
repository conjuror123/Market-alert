"""tools/recount.py: the separate check on a replay's dry run."""
import ast
import csv
import importlib.util
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from jump import bars, replay, verify
from jump.basket import load_basket

HOUR = 3600
TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "recount.py")


def _tool():
    spec = importlib.util.spec_from_file_location("recount", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_check_does_not_use_the_votes_code():
    # Apart on purpose: a mistake in the vote must not be repeated here.
    tree = ast.parse(open(TOOL, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names |= {f"{node.module}.{a.name}" for a in node.names}
    assert not {n for n in names if n.startswith(("jump.verify", "jump.replay"))}


def _dry_run(tmp_path, monkeypatch):
    asset = next(a for a in load_basket().instruments if a.ticker == "BTC/USDT")
    hours = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()) + HOUR * np.arange(24 * 200)
    rng = np.random.default_rng(11)
    r = rng.normal(0, 0.001, len(hours))
    r[24 * 90 + 3] = 0.015                                # a real move, on every exchange
    clean = r.copy()
    bad = range(24 * 70, 24 * 70 + 6)
    for i, k in enumerate(bad):
        r[k] = 0.04 if i % 2 == 0 else -0.04

    def frame(moves):
        close = 100 * np.exp(np.cumsum(moves))
        opened = np.r_[100, close[:-1]]
        return pd.DataFrame({"hour_utc": hours, "open": opened, "close": close,
                             "high": np.maximum(opened, close), "low": np.minimum(opened, close),
                             "volume": 1.0, "n_src": 1})

    bars.write(bars.store_path(str(tmp_path / "bars"), asset.file_stem), frame(r))
    market = frame(clean)
    monkeypatch.setattr(replay, "fetch", lambda src, a, stored, session, now: market)
    record = str(tmp_path / "verified.csv")
    verify.write({}, 0, record)
    out = str(tmp_path / "out")
    replay.replay([asset], str(tmp_path / "bars"), None, out,
                  now=datetime.fromtimestamp(int(hours[-1]) + 2 * HOUR, timezone.utc),
                  record_path=record)
    return out, asset, hours, bad


def test_the_check_agrees_with_a_sound_dry_run_and_flags_a_vote_the_bars_contradict(
        tmp_path, monkeypatch):
    out, asset, hours, bad = _dry_run(tmp_path, monkeypatch)
    tool = _tool()
    assert tool.recount(out, str(tmp_path / "bars")) == []
    # A real move written as not real: the sources' bars say otherwise.
    path = os.path.join(out, "votes.csv")
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    real = next(r for r in rows if r["verdict"] == "real")
    real["verdict"] = "not_real"
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    found = tool.recount(out, str(tmp_path / "bars"))
    assert len(found) == 1 and "the recount real" in found[0]

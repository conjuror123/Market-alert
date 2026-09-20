"""Declared corporate actions: the step is divCash over the raw previous close."""
from datetime import date

import pytest

from price_monitor import tiingo
from price_monitor.tiingo import DailyRow
from tremor import corporate_actions as ca
from tremor.basket import Asset, Basket, VolatilityIndex


def _etf(ticker):
    return Asset(ticker=ticker, source="twelvedata", tier=1, block="equity",
                 has_volume=True, tick_size=0.01, session_template="us_equity",
                 fetch_interval="1h", label=ticker, in_basket=True)


def _basket(assets):
    return Basket(
        assets=tuple(assets), outside=(),
        volatility_index=VolatilityIndex(
            "VIXCLS", "fred", "1d", "VIX", date(1990, 1, 1)),
        anchor_exchange_tz="America/New_York",
        history_since=date(2021, 1, 1), session_templates={},
        fetch_since=date(2002, 1, 1),
    )


def _ratio_method_steps(rows):
    """The old inferred-ratio deriver. Kept so the 2x bug is a failing test
    rather than a paragraph in decisions.md."""
    steps = []
    previous = None
    for row in sorted(rows, key=lambda r: r.day):
        if row.close <= 0:
            continue
        factor = row.adj_close / row.close
        if previous is not None and previous > 0:
            step = factor / previous - 1
            if abs(step) >= ca.STEP_THRESHOLD:
                steps.append(step)
        previous = factor
    return steps


def _split_crossing_rows():
    # Pre-split closes ~290, a 0.96 cash dividend, then a 2:1. adjClose is
    # the split-adjusted series a ratio method would have used: the 0.96
    # cash on a ~145 split-adjusted price is the 2x error.
    return [
        DailyRow(day=date(2025, 9, 22), close=290.0, adj_close=100.0,
                 div_cash=0.0, split_factor=1.0),
        DailyRow(day=date(2025, 9, 23), close=290.0, adj_close=100.662,
                 div_cash=0.96, split_factor=1.0),
        DailyRow(day=date(2025, 12, 4), close=290.0, adj_close=100.662,
                 div_cash=0.0, split_factor=1.0),
        DailyRow(day=date(2025, 12, 5), close=145.0, adj_close=100.662,
                 div_cash=0.0, split_factor=2.0),
        DailyRow(day=date(2025, 12, 8), close=146.0, adj_close=101.0,
                 div_cash=0.0, split_factor=1.0),
    ]


def test_a_nominal_dividend_before_a_split_is_not_doubled():
    rows = _split_crossing_rows()
    actions = ca.derive_actions_tiingo("XLK", rows)
    dividends = [a for a in actions if a.kind == "dividend"]
    assert len(dividends) == 1
    # 0.96 / 290 / (1 - 0.96/290) = 0.003321...  The doubled figure is 0.00662.
    assert dividends[0].factor_step == pytest.approx(0.00331, abs=2e-5)
    old = _ratio_method_steps(rows)
    assert old[0] == pytest.approx(0.00662, abs=2e-5)


def test_the_step_is_divcash_over_the_raw_previous_close():
    rows = [
        DailyRow(day=date(2020, 1, 2), close=100.0, adj_close=1.0,
                 div_cash=0.0, split_factor=1.0),
        DailyRow(day=date(2020, 1, 3), close=99.0, adj_close=1.0,
                 div_cash=1.0, split_factor=1.0),
    ]
    actions = ca.derive_actions_tiingo("SPY", rows)
    assert len(actions) == 1
    # 1 / 100 / (1 - 1/100) = 1/99.
    assert actions[0].factor_step == pytest.approx(0.010101010101010102, abs=1e-15)


def test_adjclose_is_not_used():
    base = [
        DailyRow(day=date(2020, 1, 2), close=100.0, adj_close=1.0,
                 div_cash=0.0, split_factor=1.0),
        DailyRow(day=date(2020, 1, 3), close=99.0, adj_close=1.0,
                 div_cash=1.0, split_factor=1.0),
    ]
    absurd = [
        DailyRow(day=date(2020, 1, 2), close=100.0, adj_close=1e9,
                 div_cash=0.0, split_factor=1.0),
        DailyRow(day=date(2020, 1, 3), close=99.0, adj_close=1e-9,
                 div_cash=1.0, split_factor=1.0),
    ]
    a = ca.derive_actions_tiingo("SPY", base)
    b = ca.derive_actions_tiingo("SPY", absurd)
    assert a == b


def test_a_split_is_emitted_as_a_split_row():
    rows = [
        DailyRow(day=date(2025, 12, 4), close=290.0, adj_close=145.0,
                 div_cash=0.0, split_factor=1.0),
        DailyRow(day=date(2025, 12, 5), close=145.0, adj_close=145.0,
                 div_cash=0.0, split_factor=2.0),
    ]
    actions = ca.derive_actions_tiingo("XLK", rows)
    assert len(actions) == 1
    assert actions[0].kind == "split"
    assert actions[0].factor_step == 1.0
    assert actions[0].day == date(2025, 12, 5)


def test_load_steps_excludes_splits_by_default(tmp_path):
    path = tmp_path / "actions.csv"
    ca.write_actions(str(path), [
        ca.CorporateAction("XLK", date(2024, 6, 24), "dividend", 0.001751),
        ca.CorporateAction("XLK", date(2025, 12, 5), "split", 1.0),
    ])
    steps = ca.load_steps(str(path))
    assert steps["XLK"] == [(date(2024, 6, 24), 0.001751)]
    # The split is still IN the table - what is recorded and what is used for
    # un-adjustment are different questions - it is just not loaded by default.
    both = ca.load_steps(str(path), kinds=("dividend", "split"))
    assert [k for k, _ in both["XLK"]] == [date(2024, 6, 24), date(2025, 12, 5)]
    assert not (tmp_path / "actions.csv.tmp").exists()


def test_a_zero_dividend_row_emits_nothing():
    rows = [
        DailyRow(day=date(2020, 1, 2), close=100.0, adj_close=100.0,
                 div_cash=0.0, split_factor=1.0),
        DailyRow(day=date(2020, 1, 3), close=100.0, adj_close=100.0,
                 div_cash=0.0, split_factor=1.0),
    ]
    assert ca.derive_actions_tiingo("SHY", rows) == []


def test_a_treasury_payout_survives_the_step_threshold():
    # Short Treasuries in 2021 paid about 1.5e-4 of price. That must survive
    # STEP_THRESHOLD (1e-5); rounding noise around 1e-8 must not.
    rows = [
        DailyRow(day=date(2021, 3, 31), close=100.0, adj_close=100.0,
                 div_cash=0.0, split_factor=1.0),
        DailyRow(day=date(2021, 4, 1), close=99.985, adj_close=99.985,
                 div_cash=0.015, split_factor=1.0),
        DailyRow(day=date(2021, 4, 2), close=99.985, adj_close=99.985,
                 div_cash=0.0000005, split_factor=1.0),
    ]
    actions = ca.derive_actions_tiingo("SHY", rows)
    assert len(actions) == 1
    assert actions[0].day == date(2021, 4, 1)
    assert actions[0].factor_step == pytest.approx(0.0001500225, rel=1e-6)


def test_no_previous_close_means_no_action():
    rows = [
        DailyRow(day=date(2020, 1, 2), close=100.0, adj_close=100.0,
                 div_cash=1.0, split_factor=1.0),
    ]
    assert ca.derive_actions_tiingo("SPY", rows) == []


def test_a_partial_fetch_does_not_write_a_truncated_table(tmp_path, monkeypatch):
    sentinel = "ticker,date,kind,factor_step\nKEEP,2000-01-01,dividend,0.00100000\n"
    out = tmp_path / "actions.csv"
    out.write_text(sentinel, encoding="utf-8")

    funds = [_etf(f"T{i:02d}") for i in range(20)]
    calls = {"n": 0}

    def fake_fetch(symbol, start, api_key="", session=None, end=None, base_url=""):
        calls["n"] += 1
        if calls["n"] >= 20:
            raise tiingo.RateLimited(f"{symbol}: Tiingo request budget spent")
        return []

    monkeypatch.setattr(ca.tiingo, "fetch_daily_history", fake_fetch)
    monkeypatch.setattr(ca.time, "sleep", lambda *_: None)
    monkeypatch.setattr("tremor.basket.load_basket", lambda: _basket(funds))
    monkeypatch.setenv("TIINGO_API_KEY", "k")

    assert ca.main(["--out", str(out)]) == 1
    assert out.read_text(encoding="utf-8") == sentinel
    assert calls["n"] == 20

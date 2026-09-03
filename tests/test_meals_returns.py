import math
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from meals import bars, returns
from meals.basket import Asset

HOUR = 3600


def asset(**over):
    base = dict(ticker="SPY", source="twelvedata", tier=1, block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="30min", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def frame(rows):
    return pd.DataFrame(rows, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)


def et(y, m, d, h):
    return int(datetime(y, m, d, h, tzinfo=ZoneInfo("America/New_York")).timestamp())


def two_days():
    # Two trading days of two hours: the first closes at 101, the second opens at 105.
    return frame([
        (et(2021, 3, 1, 10), 100.0, 101.0, 99.0, 100.5, 1.0, 2),
        (et(2021, 3, 1, 11), 100.5, 102.0, 100.0, 101.0, 1.0, 2),
        (et(2021, 3, 2, 10), 105.0, 106.0, 104.0, 106.0, 1.0, 2),
        (et(2021, 3, 2, 11), 106.0, 107.0, 105.0, 106.5, 1.0, 2),
    ])


def test_overnight_move_goes_to_the_gap_channel_not_to_the_return():
    # Between sessions the price moved from 101 to 105. Had that jump landed in
    # r, every morning would look like an anomaly.
    out = returns.split_channels(asset(), two_days())
    opening = out.iloc[2]

    assert opening["is_session_open"]
    assert opening["r_gap"] == pytest.approx(math.log(105.0 / 101.0))
    assert opening["r"] == pytest.approx(math.log(106.0 / 105.0))


def test_ordinary_bar_is_close_to_close_and_has_no_gap():
    out = returns.split_channels(asset(), two_days())
    ordinary = out.iloc[1]

    assert not ordinary["is_session_open"]
    assert ordinary["r"] == pytest.approx(math.log(101.0 / 100.5))
    assert np.isnan(ordinary["r_gap"])


def test_the_very_first_bar_has_neither_channel():
    # There is no previous close - both quantities are undefined, not zero.
    out = returns.split_channels(asset(), two_days())
    assert np.isnan(out.iloc[0]["r"])
    assert np.isnan(out.iloc[0]["r_gap"])


def test_ex_dividend_gap_is_masked_but_the_intraday_return_survives():
    # The price drop on the ex-date is mechanical. Only the gap is masked: the
    # intra-hour return has nothing to do with the payout.
    out = returns.split_channels(asset(), two_days(), action_days={date(2021, 3, 2)})
    opening = out.iloc[2]

    assert opening["gap_masked"]
    assert np.isnan(opening["r_gap"])
    assert opening["r"] == pytest.approx(math.log(106.0 / 105.0))


def test_crypto_has_no_session_boundaries():
    crypto = asset(ticker="BTC-USD", source="coinbase", block="crypto",
                   session_template="crypto_24_7", fetch_interval="1h")
    out = returns.split_channels(crypto, two_days())

    # Only the very first bar of history opens a session; after that the series is continuous.
    assert int(out["is_session_open"].sum()) == 1
    assert out["r_gap"].isna().all()


def test_forex_week_is_one_session():
    fx = asset(ticker="EUR/USD", block="FX", has_volume=False, tick_size=0.00001,
               session_template="fx_continuous", fetch_interval="1h")
    # Thursday and Friday of one week, then Monday of the next.
    data = frame([
        (int(datetime(2026, 8, 27, 12, tzinfo=timezone.utc).timestamp()),
         1.0, 1.1, 0.9, 1.05, 0.0, 1),
        (int(datetime(2026, 8, 28, 12, tzinfo=timezone.utc).timestamp()),
         1.05, 1.1, 1.0, 1.06, 0.0, 1),
        (int(datetime(2026, 8, 31, 12, tzinfo=timezone.utc).timestamp()),
         1.08, 1.1, 1.0, 1.09, 0.0, 1),
    ])
    out = returns.split_channels(fx, data)

    # There is no break inside the week, and Monday opens a new one.
    assert list(out["is_session_open"]) == [True, False, True]
    assert out.iloc[2]["r_gap"] == pytest.approx(math.log(1.08 / 1.06))


def test_winsorization_clips_only_the_state_input():
    # r_w goes into the EWMA update, r stays untouched: clipping the very thing
    # we want to detect is pointless (§2.5).
    calm = [(i * HOUR, 100.0, 100.1, 99.9, 100.0 + (i % 2) * 0.01, 1.0, 2)
            for i in range(1, 40)]
    spike = [(40 * HOUR, 100.0, 130.0, 99.9, 130.0, 1.0, 2)]
    out = returns.winsorize(asset(), returns.split_channels(
        asset(ticker="BTC-USD", source="coinbase", block="crypto",
              session_template="crypto_24_7", fetch_interval="1h"),
        frame(calm + spike)))

    last = out.iloc[-1]
    assert last["r"] > last["r_w"]           # the raw return exceeds the clipped one
    assert last["r_w"] == pytest.approx(5 * last["mad_eff"])


def test_winsorization_floor_saves_a_stuck_quote():
    # The quote stands still: MAD_24 is zero, and without the floor any move at
    # all would come out "larger than five MADs".
    flat = [(i * HOUR, 100.0, 100.0, 100.0, 100.0, 1.0, 2) for i in range(1, 40)]
    move = [(40 * HOUR, 100.0, 100.2, 100.0, 100.2, 1.0, 2)]
    crypto = asset(ticker="BTC-USD", source="coinbase", block="crypto",
                   session_template="crypto_24_7", fetch_interval="1h")
    out = returns.winsorize(crypto, returns.split_channels(crypto, frame(flat + move)))

    assert out.iloc[-1]["mad_eff"] > 0
    assert np.isfinite(out.iloc[-1]["r_w"])


def test_winsorization_leaves_returns_alone_before_the_window_fills():
    short = frame([(i * HOUR, 100.0, 101.0, 99.0, 100.0 + i, 1.0, 2) for i in range(1, 5)])
    crypto = asset(ticker="BTC-USD", source="coinbase", block="crypto",
                   session_template="crypto_24_7", fetch_interval="1h")
    out = returns.winsorize(crypto, returns.split_channels(crypto, short))

    filled = out["r"].notna()
    assert (out.loc[filled, "r_w"] == out.loc[filled, "r"]).all()


def test_empty_input_keeps_the_columns():
    out = returns.winsorize(asset(), returns.split_channels(asset(), bars.empty_frame()))
    assert out.empty
    assert "r_w" in out.columns and "r_gap" in out.columns

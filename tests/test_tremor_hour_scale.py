"""The abnormal channel judges a US fund's hour against that hour's own history."""
import numpy as np
import pandas as pd
import pytest

from tremor import residuals, saed, windows
from tremor.basket import Asset
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta

NY = ZoneInfo("America/New_York")


def asset(template="us_equity"):
    return Asset(ticker="XLI", source="twelvedata", tier=1, block="equity",
                 has_volume=True, tick_size=0.01, session_template=template,
                 fetch_interval="30min", label="XLI", in_basket=True)


def sessions(days, opening_size=2.0, seed=0):
    """Seven bars a day, the 09:00 one `opening_size` times the others."""
    rng = np.random.default_rng(seed)
    hours, e = [], []
    day = datetime(2010, 1, 4, tzinfo=NY)
    while len(hours) < days * 7:
        if day.weekday() < 5:
            for h in range(9, 16):
                hours.append(int(day.replace(hour=h).timestamp()))
                e.append(rng.normal(0, 0.001 * (opening_size if h == 9 else 1.0)))
        day += timedelta(days=1)
    return pd.DataFrame({"hour_utc": hours, "e_resid": e})


def test_the_opening_learns_that_it_is_the_busy_hour():
    frame = sessions(2000, opening_size=2.0)
    scale = residuals.hour_scale(frame, memory=200, minimum=50)
    hour = pd.to_datetime(frame["hour_utc"], unit="s", utc=True).dt.tz_convert(NY).dt.hour
    late = scale[-700:]
    opening, rest = late[hour[-700:] == 9], late[hour[-700:] != 9]
    # sqrt of (4 + 6) / 7 is the all-hours level against which each is measured.
    level = np.sqrt((4 + 6) / 7)
    assert np.median(opening) == pytest.approx(2.0 / level, rel=0.1)
    assert np.median(rest) == pytest.approx(1.0 / level, rel=0.1)


def test_hours_that_are_alike_are_left_alone():
    frame = sessions(2000, opening_size=1.0)
    scale = residuals.hour_scale(frame, memory=200, minimum=50)
    assert np.median(np.abs(scale[-700:] - 1.0)) < 0.05


def test_it_is_causal_a_bar_never_sees_itself_or_later():
    frame = sessions(800)
    before = residuals.hour_scale(frame, memory=100, minimum=30)
    shocked = frame.copy()
    shocked.loc[len(frame) - 7, "e_resid"] = 1.0       # a huge last opening
    after = residuals.hour_scale(shocked, memory=100, minimum=30)
    np.testing.assert_array_equal(before[:-7], after[:-7])
    assert before[-7] == after[-7]


def test_too_little_history_is_judged_as_before():
    frame = sessions(20)
    assert (residuals.hour_scale(frame) == 1.0).all()


def _frame(n=3000):
    frame = sessions(n // 7, opening_size=2.0)
    rng = np.random.default_rng(1)
    frame["r"] = frame["e_resid"] + rng.normal(0, 1e-4, len(frame))
    frame["close"] = 100.0
    return frame.drop(columns="e_resid")


def test_only_a_us_fund_gets_it_and_only_in_the_divisor():
    frame = _frame()
    us = residuals.residuals(asset(), frame)
    fx = residuals.residuals(asset("fx_continuous"), frame)
    gap_pass = residuals.residuals(asset(), frame, template=windows.DAILY_SERIES)

    assert (us["hour_scale"] != 1.0).any()
    assert (fx["hour_scale"] == 1.0).all()
    assert (gap_pass["hour_scale"] == 1.0).all()
    # The residual itself - what the message splits and retention sums - is
    # untouched; the scale lives in the standardisation's divisor only.
    np.testing.assert_allclose(us["e_resid"], fx["e_resid"])
    np.testing.assert_allclose(us["patell_scale"],
                               fx["patell_scale"] * us["hour_scale"])


def test_a_warm_slice_reaches_back_far_enough_for_the_hour_scale():
    # The scale's chain is longer than anything warm_bars counts for a fund.
    lead = windows.warm_bars(windows.w_asset(7), template="us_equity")
    assert windows.hour_scale_chain("us_equity") > lead
    assert windows.hour_scale_chain("fx_continuous") == 0

    fund = asset()
    hours = sessions(6000)["hour_utc"].to_numpy()        # 42,000 fund bars
    metrics = {fund.asset_id: pd.DataFrame({"hour_utc": hours, "r": 0.0})}

    class Basket:
        instruments = (fund,)
        anchor_exchange_tz = "America/New_York"

    import tremor.saed as s
    after = int(hours[-100])                              # 99 bars after it
    kept = len(s.plan_frames(Basket(), metrics, after)[fund.asset_id])
    assert kept == windows.hour_scale_chain("us_equity") + 99

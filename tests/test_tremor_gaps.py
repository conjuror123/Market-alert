"""The overnight gap: scored on its own calendar, written onto the first bar only
where it out-ranks it, and never trimmed with the hourly series."""
import numpy as np
import pandas as pd
import pytest

from tremor import ewma, gaps, residuals, saed, severity, windows
from tremor.basket import Asset

HOUR = 3600


def asset(**over):
    base = dict(ticker="SPY", source="twelvedata", tier=1, block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="1h", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def hourly(n=40, tiers=None):
    """An hourly frame shaped like saed's, quiet unless `tiers` says otherwise."""
    tiers = tiers or {}
    column = [pd.NA] * n
    for i, name in tiers.items():
        column[i] = name
    moved = [0.05 if t is not pd.NA else 0.001 for t in column]
    frame = pd.DataFrame({
        "hour_utc": [(i + 1) * HOUR for i in range(n)],
        "r": moved, "e_resid": [0.001] * n, "z_resid": [0.5] * n,
        "z_resid_bmp": [0.5] * n, "co_block": [0.0] * n, "beta_block": [1.0] * n,
        "sigma_lt": [0.01] * n, "sigma_lt_resid": [0.01] * n,
        "close": [100.0] * n,
        "tier": pd.array(column, dtype="string"),
        "basis": pd.array(["absolute" if t is not pd.NA else pd.NA for t in column],
                          dtype="string"),
    })
    for rank, name in enumerate(severity.TIERS):
        frame[f"level_{name}"] = 5.0 + rank
    return frame


def gap_row(hour, tier, r=-0.04, e=-0.03, sigma=0.008, kind="night"):
    frame = pd.DataFrame({
        "hour_utc": [hour], "r": [r], "e_resid": [e], "z_resid": [9.0],
        "gap_kind": [kind],
        "z_resid_bmp": [9.0], "co_block": [r - e], "beta_block": [1.0],
        "sigma_lt": [sigma], "sigma_lt_resid": [0.005],
        "tier": pd.array([tier], dtype="string"),
        "basis": pd.array(["absolute"], dtype="string"),
    })
    for rank, name in enumerate(severity.TIERS):
        frame[f"level_{name}"] = 5.0 + rank
    return frame


# --- the calendar ------------------------------------------------------------

def test_the_gap_learns_normal_in_days_not_in_hourly_bar_counts():
    # One row a session. Read on the fund's own us_equity template, 600 rows of
    # half-life would be 2.4 YEARS of mornings and the span fourteen; the gap
    # has to be read on the daily calendar the VIX already uses.
    assert gaps.TEMPLATE == windows.DAILY_SERIES
    assert windows.sigma_lt_span(gaps.TEMPLATE) < windows.sigma_lt_span("us_equity") / 5

    rng = np.random.default_rng(3)
    # A quiet regime then a loud one: the daily memory must have moved to it.
    values = np.concatenate([rng.normal(0, 0.002, 1500), rng.normal(0, 0.02, 300)])
    series = pd.Series(values)
    daily = ewma.sigma_lt(series, windows.DAILY_SERIES).iloc[-1]
    hourly_calendar = ewma.sigma_lt(series, "us_equity").iloc[-1]
    assert daily > 1.5 * hourly_calendar


def test_the_residual_takes_the_calendar_it_is_given():
    frame = pd.DataFrame({"hour_utc": np.arange(1500) * 86400,
                          "r": np.random.default_rng(1).normal(0, 0.01, 1500),
                          "close": 100.0})
    own = residuals.residuals(asset(), frame)
    daily = residuals.residuals(asset(), frame, template=windows.DAILY_SERIES)
    assert not np.allclose(own["sigma_lt_resid"].to_numpy(),
                           daily["sigma_lt_resid"].to_numpy(), equal_nan=True)
    pd.testing.assert_series_equal(
        daily["sigma_lt_resid"],
        ewma.sigma_lt(daily["e_resid"], windows.DAILY_SERIES).rename("sigma_lt_resid"),
        check_index=False)


def test_mornings_take_only_scored_gaps_of_us_funds():
    spy = asset()
    coin = asset(ticker="BTC-USD", source="coinbase", block="crypto",
                 session_template="crypto_24_7")

    class Basket:
        instruments = (spy, coin)

    metrics = {
        spy.asset_id: pd.DataFrame({"hour_utc": [1, 2, 3], "close": [1.0, 1.0, 1.0],
                                    "gap": [np.nan, -0.01, np.nan]}),
        coin.asset_id: pd.DataFrame({"hour_utc": [1], "close": [1.0], "gap": [0.5]}),
    }
    out = gaps.mornings(Basket(), metrics)
    assert list(out) == [spy.asset_id]
    assert out[spy.asset_id]["r"].tolist() == [-0.01]
    assert out[spy.asset_id]["hour_utc"].tolist() == [2]


# --- the kind of close --------------------------------------------------------

def _utc(stamp):
    return int(pd.Timestamp(stamp, tz="America/New_York").tz_convert("UTC").timestamp())


def test_a_gap_knows_what_kind_of_close_came_before_it():
    cases = [
        ("2026-09-22 09:30", "2026-09-21 15:30", "night"),      # Tuesday
        ("2026-09-21 09:30", "2026-09-18 15:30", "weekend"),    # Monday
        ("2026-09-08 09:30", "2026-09-04 15:30", "weekend"),    # after Labor Day
        ("2025-11-28 09:30", "2025-11-26 15:30", "holiday"),    # after Thanksgiving
        ("2024-07-05 09:30", "2024-07-03 15:30", "holiday"),    # after the Fourth
    ]
    hours = [_utc(today) for today, _, _ in cases]
    before = [_utc(last) for _, last, _ in cases]
    assert gaps.gap_kinds(np.array(hours), np.array(before)).tolist() == [
        kind for _, _, kind in cases]
    assert gaps.kind_groups(["night", "weekend", "holiday"]).tolist() == [
        "night", "closed", "closed"]


def test_the_kind_scale_is_one_where_there_is_one_kind():
    values = pd.Series(np.random.default_rng(4).normal(0, 0.01, 3000))
    out = residuals.kind_scale(values, np.full(3000, "closed"))
    assert (out == 1.0).all()


def test_the_kind_scale_learns_each_kind_s_share_causally():
    # Every fifth row is twice the size of the rest: the scale must find the
    # two-to-one shape, and must not read the row it is scaling.
    rng = np.random.default_rng(5)
    n = 4000
    kinds = np.where(np.arange(n) % 5 == 0, "closed", "night")
    values = rng.normal(0, 0.01, n) * np.where(kinds == "closed", 2.0, 1.0)
    out = residuals.kind_scale(pd.Series(values), kinds)

    late = np.arange(n) > 3000
    ratio = out[late & (kinds == "closed")].mean() / out[late & (kinds == "night")].mean()
    assert ratio == pytest.approx(2.0, rel=0.1)
    # Too few closes of a kind yet: judged as it always was.
    assert (out[:windows.GAP_KIND_MIN_SAME_KIND] == 1.0).all()
    # Causal: a huge last row does not move its own scale.
    bumped = values.copy()
    bumped[-1] *= 50
    assert residuals.kind_scale(pd.Series(bumped), kinds)[-1] == out[-1]


def _sessions(n, monday_size=1.5, fund=True):
    """A metrics frame of `n` business days, one closing bar and one opening bar
    each, the gap on the opening bar - Mondays' `monday_size` times the rest."""
    days = pd.bdate_range("2010-01-04", periods=n)
    rng = np.random.default_rng(6)
    rows = []
    for i, day in enumerate(days):
        opened = _utc(f"{day.date()} 09:30")
        gap = np.nan if i == 0 else rng.normal(0, 0.005) * (
            monday_size if day.weekday() == 0 else 1.0)
        rows.append((opened, gap))
        rows.append((_utc(f"{day.date()} 15:30"), np.nan))
    frame = pd.DataFrame(rows, columns=["hour_utc", "gap"])
    frame["close"] = 100.0
    return frame


def test_a_monday_is_judged_against_other_mondays():
    spy = asset()

    class Basket:
        instruments = (spy,)

    out = gaps.mornings(Basket(), {spy.asset_id: _sessions(3000)})[spy.asset_id]
    late = out.iloc[2000:]
    monday = late["gap_kind"] == "weekend"
    assert set(late["gap_kind"]) == {"night", "weekend"}
    # The usual gap on a Monday is about 1.5x the usual weeknight one...
    ratio = late.loc[monday, "sigma_lt"].mean() / late.loc[~monday, "sigma_lt"].mean()
    assert ratio == pytest.approx(1.5, rel=0.1)
    # ...so a Monday no bigger than Mondays usually are scores like any night.
    z = (late["r"] / late["sigma_lt"]).abs()
    assert z[monday].mean() == pytest.approx(z[~monday].mean(), rel=0.1)


def test_a_currency_pair_s_gap_is_always_the_weekend_and_unscaled():
    pair = asset(ticker="USD/CAD", block="FX", session_template="fx_continuous")

    class Basket:
        instruments = (pair,)

    out = gaps.mornings(Basket(), {pair.asset_id: _sessions(1500)})[pair.asset_id]
    assert (out["gap_kind"] == "weekend").all()
    assert (out["gap_scale"] == 1.0).all()


def test_the_residual_is_divided_by_its_own_kind_scale():
    frame = pd.DataFrame({"hour_utc": np.arange(1500) * 86400,
                          "r": np.random.default_rng(7).normal(0, 0.01, 1500),
                          "close": 100.0})
    kinds = np.where(np.arange(1500) % 5 == 0, "closed", "night")
    plain = residuals.residuals(asset(), frame, template=windows.DAILY_SERIES)
    kinded = residuals.residuals(asset(), frame, template=windows.DAILY_SERIES,
                                 kinds=kinds)
    assert "kind_scale" not in plain
    np.testing.assert_allclose(kinded["patell_scale"],
                               plain["patell_scale"] * kinded["kind_scale"])
    # The residual itself stays the size the price moved.
    np.testing.assert_allclose(kinded["e_resid"], plain["e_resid"])


# --- the overlay -------------------------------------------------------------

def test_a_gap_that_out_ranks_the_first_bar_claims_it():
    frame = hourly()
    out = gaps.overlay(frame, gap_row(6 * HOUR, "major"))

    row = out.loc[out["hour_utc"] == 6 * HOUR].iloc[0]
    assert row["overnight"]
    assert row["tier"] == "major"
    assert row["r"] == pytest.approx(-0.04)       # the gap, as a price move
    assert row["sigma_lt"] == pytest.approx(0.008)  # the fund's usual gap
    assert row["gap_kind"] == "night"
    # Every other row untouched, and the input not mutated.
    assert out["overnight"].sum() == 1
    assert frame["tier"].isna().all()
    assert out.drop(index=row.name)["r"].eq(0.001).all()


def test_the_first_hour_keeps_a_row_it_ranks_at_or_above():
    frame = hourly(tiers={5: "major"})
    same = gaps.overlay(frame, gap_row(6 * HOUR, "major"))
    below = gaps.overlay(frame, gap_row(6 * HOUR, "high"))
    for out in (same, below):
        assert not out["overnight"].any()
        assert out.loc[5, "r"] == pytest.approx(0.05)


def test_retention_counts_the_night_as_interval_zero():
    # Gap -4%, then the day gives back 1% of price over its bars: at the close
    # the move is -3% of the -4% - three quarters held.
    frame = hourly(n=10)
    frame["r"] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.004, 0.003, 0.003, 0.0, 0.0]
    frame["hour_utc"] = [(10 + i) * HOUR for i in range(10)]   # one UTC day
    out = gaps.overlay(frame, gap_row(15 * HOUR, "major", r=-0.04),
                       tz_name=None, last_day_closed=True)
    row = out.loc[out["hour_utc"] == 15 * HOUR].iloc[0]
    assert row["retention_raw_today"] == pytest.approx((-0.04 + 0.01) / -0.04)


def test_a_gap_event_and_a_first_hour_event_the_same_day_are_one_event():
    # The gap claims the 09:00 row; an hour later the day fires again. One day,
    # one event - enforced by the automaton, not filtered afterwards.
    frame = hourly(tiers={7: "noticeable"})
    out = gaps.overlay(frame, gap_row(6 * HOUR, "major", kind="weekend"))
    events = saed.build_events(asset(), out, day_tz=None)

    assert len(events) == 1
    assert events[0].overnight
    assert events[0].gap_kind == "weekend"
    assert saed.events_frame(events)["gap_kind"].tolist() == ["weekend"]
    assert events[0].tier == "major"
    assert events[0].repeat_count == 1


def test_no_gap_pass_means_no_overnight_column_change():
    frame = hourly(tiers={5: "high"})
    assert gaps.overlay(frame, None)["overnight"].eq(False).all()
    events = saed.build_events(asset(), hourly(tiers={5: "high"}), day_tz=None)
    assert len(events) == 1 and not events[0].overnight
    assert events[0].gap_kind is None


# --- never trimmed -----------------------------------------------------------

def test_saed_hands_the_gap_pass_the_untrimmed_metrics(monkeypatch, tmp_path):
    # A warm run trims the hourly series to its trailing window. The gap pass
    # must see every morning anyway, or a warm run and a cold one would score
    # the same gap against different histories.
    full = {"a": pd.DataFrame({"hour_utc": range(100), "r": 0.0})}
    trimmed = {"a": full["a"].tail(10)}
    seen = {}

    from tremor import basket as basket_module
    monkeypatch.setattr(basket_module, "load_basket", lambda: type("B", (), {
        "instruments": (), "anchor_exchange_tz": "America/New_York"})())
    from tremor import pipeline
    monkeypatch.setattr(pipeline, "load_all", lambda basket, d: full)
    monkeypatch.setattr(saed, "plan_frames", lambda basket, metrics, after: trimmed)
    monkeypatch.setattr(saed, "load_record_book", lambda path, config: ({}, 50))

    def fake_build(basket, frames, factors, panel, sigma, full_metrics=None,
                   records=None):
        seen["frames"], seen["full"] = frames, full_metrics
        raise SystemExit(0)

    monkeypatch.setattr(saed, "build_for_basket", fake_build)
    from tremor import cross_section
    monkeypatch.setattr(cross_section, "build_panel",
                        lambda source, column="r": pd.DataFrame(index=[1]))
    monkeypatch.setattr(cross_section, "block_factors", lambda *a, **k: pd.DataFrame())
    with pytest.raises(SystemExit):
        saed.main(["--metrics-dir", str(tmp_path)])

    assert seen["frames"] is trimmed
    assert seen["full"] is full

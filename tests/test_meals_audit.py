import pandas as pd

from meals import bars
from meals.audit import _flag, audit_instrument
from meals.basket import Asset

HOUR = 3600


def asset(**over):
    base = dict(ticker="SPY", source="twelvedata", tier=1, block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="30min", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def frame(rows):
    return pd.DataFrame(rows, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)


def healthy(n=48, volume=100.0, n_src=2):
    # Five decimals in the price - that is how the real source quotes; on round
    # values like 1.5 the measured precision would come out as 0.1, far coarser
    # than the real price step, and the step check would fire spuriously.
    return frame([(h * HOUR, 1.0, 2.0, 0.5, 1.50001, volume, n_src) for h in range(1, n + 1)])


def test_reports_period_and_bars_per_day():
    row = audit_instrument(asset(), healthy(n=48))
    assert row["rows"] == 48
    assert row["days"] == 3
    assert row["bars_per_day"] > 0
    assert row["first"] <= row["last"]


def test_empty_store_is_reported_not_crashed():
    row = audit_instrument(asset(), bars.empty_frame())
    assert row["rows"] == 0
    assert row["first"] is None
    assert _flag(row) == "no data"


def test_declared_volume_that_is_actually_empty_is_flagged():
    # Exactly what §2.1 is meant to catch: hourly volume is declared but the
    # source does not actually have it. Per §3.5 such an asset must be
    # has_volume = false, or the volume trigger silently never fires.
    row = audit_instrument(asset(), healthy(volume=0.0))
    assert row["has_volume_actual"] is False
    assert "volume declared but empty" in _flag(row)


def test_forex_without_volume_is_not_flagged():
    row = audit_instrument(asset(ticker="EUR/USD", block="FX", has_volume=False,
                                 fetch_interval="1h"), healthy(volume=0.0))
    assert _flag(row) == ""


def test_ohlc_violation_is_counted():
    bad = frame([(HOUR, 1.0, 2.0, 5.0, 1.5, 1.0, 1)])  # low above open and close
    row = audit_instrument(asset(), bad)
    assert row["ohlc_violations"] == 1
    assert "OHLC" in _flag(row)


def test_nonpositive_price_is_counted():
    # Protection against ln(0) in the return calculation (§2.6).
    row = audit_instrument(asset(), frame([(HOUR, 1.0, 2.0, 0.0, 1.5, 1.0, 1)]))
    assert row["nonpositive_prices"] == 1
    assert "prices<=0" in _flag(row)


def test_partial_hours_are_counted():
    # An ETF's first session hour is assembled from a single half-hourly bar.
    df = pd.concat([healthy(n=10, n_src=2), frame([(99 * HOUR, 1.0, 2.0, 0.5, 1.5, 1.0, 1)])],
                   ignore_index=True)
    assert audit_instrument(asset(), df)["partial_hours"] == 1


def test_max_gap_is_measured_in_hours():
    df = frame([(HOUR, 1.0, 2.0, 0.5, 1.5, 1.0, 1),
                (73 * HOUR, 1.0, 2.0, 0.5, 1.5, 1.0, 1)])
    assert audit_instrument(asset(), df)["max_gap_hours"] == 72


def test_precision_reflects_what_the_source_actually_emits():
    from meals.audit import measure_precision

    assert measure_precision(pd.Series([1.15876, 1.1589])) == 1e-5   # currency pair
    assert measure_precision(pd.Series([147.123, 147.125])) == 0.001
    # For ETFs the source serves a floating tail - 769.53992 instead of 769.54 -
    # so the measured precision is finer than the real step by two orders of
    # magnitude; that is why tick_size is stated explicitly in the configuration
    # rather than measured.
    assert measure_precision(pd.Series([769.53992, 768.67999])) == 1e-5


def test_precision_ignores_binary_representation_noise():
    from meals.audit import measure_precision

    # 0.1 + 0.2 in float64 gives 0.30000000000000004 - without discarding that
    # rubbish the precision would come out as 1e-17.
    assert measure_precision(pd.Series([0.1 + 0.2, 0.4])) == 0.1


def test_precision_of_an_empty_series_is_unknown():
    from meals.audit import measure_precision

    assert measure_precision(pd.Series([], dtype="float64")) is None


def test_tick_finer_than_the_source_precision_is_flagged():
    # The configuration promises a resolution the data does not have, and
    # half_tick_return in §2.5 would be computed against a step that does not exist.
    row = audit_instrument(asset(tick_size=1e-9), healthy())
    assert "finer than source precision" in _flag(row)


def test_report_renders_an_instrument_with_no_data():
    # An empty instrument is an audit result, not a reason to crash on
    # formatting missing numbers.
    from meals.audit import render

    report = render([audit_instrument(asset(), bars.empty_frame())], None)

    assert "no data" in report
    assert "`SPY`" in report


def test_report_includes_both_sections_and_the_vix_line():
    from meals.audit import render

    report = render(
        [audit_instrument(asset(), healthy()),
         audit_instrument(asset(ticker="NZD/USD", block="FX", has_volume=False,
                                tick_size=0.00001, fetch_interval="1h",
                                in_basket=False), healthy(volume=0.0))],
        {"series_id": "VIXCLS", "rows": 1400, "first": "2021-01-04",
         "last": "2026-08-27", "median_lag_hours": 39.0},
    )

    assert "## Basket" in report
    assert "## Outside the basket (SAED only)" in report
    assert "VIXCLS" in report


def test_ohlc_check_tolerates_vendor_rounding_below_one_tick():
    # A real case from the data: close 92.42 against high 92.415 - the source
    # rounded the bar's fields independently. The difference is under a tick, so
    # the bar is sound.
    almost = frame([(HOUR, 92.15, 92.415, 92.14, 92.42, 1.0, 2)])
    assert audit_instrument(asset(), almost)["ohlc_violations"] == 0


def test_ohlc_check_still_catches_a_genuinely_broken_bar():
    broken = frame([(HOUR, 92.15, 92.415, 92.14, 95.0, 1.0, 2)])
    assert audit_instrument(asset(), broken)["ohlc_violations"] == 1

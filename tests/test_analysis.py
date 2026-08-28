import math

from price_monitor.analysis import analyze, ewma_volatility, log_returns, robust_z_score
from price_monitor.models import Candle


def make_candles(closes, volumes=None):
    volumes = volumes or [100.0] * len(closes)
    return [
        Candle(open_time=i, open=c, high=c, low=c, close=c, volume=v, close_time=i)
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


def test_log_returns():
    returns = log_returns([100.0, 110.0, 99.0])
    assert math.isclose(returns[0], math.log(1.1))
    assert math.isclose(returns[1], math.log(99.0 / 110.0))


def test_ewma_volatility_matches_recursive_definition():
    returns = [0.01, -0.02, 0.015]
    lam = 0.9
    expected_var = returns[0] ** 2
    for r in returns[1:]:
        expected_var = lam * expected_var + (1 - lam) * r ** 2
    assert math.isclose(ewma_volatility(returns, lam), math.sqrt(expected_var))


def test_robust_z_score_flat_history_matching_value_is_zero():
    assert robust_z_score(1.0, [1.0, 1.0, 1.0, 1.0]) == 0.0


def test_robust_z_score_flat_history_deviating_value_is_flagged():
    # Zero MAD (perfectly flat history) but the new value clearly differs: this must
    # not silently report "no anomaly" just because MAD-based scaling breaks down.
    assert robust_z_score(5.0, [1.0, 1.0, 1.0, 1.0]) > 0


def test_robust_z_score_detects_outlier():
    history = [0.0, 0.1, -0.1, 0.05, -0.05] * 10
    z = robust_z_score(2.0, history)
    assert z > 5


def test_analyze_flags_price_spike():
    calm_closes = [100.0]
    for _ in range(120):
        calm_closes.append(calm_closes[-1] * 1.0005)
    spike_closes = calm_closes + [calm_closes[-1] * 1.15]
    candles = make_candles(spike_closes)

    signal = analyze(
        symbol="TESTUSDT",
        candles=candles,
        ewma_lambda=0.94,
        mad_window=100,
        price_zscore_threshold=3.0,
        volume_zscore_threshold=4.0,
        volume_min_price_move_z=1.5,
        min_history=60,
    )

    assert signal is not None
    assert signal.price_alert is True
    assert signal.is_alert is True


def test_analyze_no_alert_on_normal_move():
    closes = [100.0]
    for i in range(120):
        closes.append(closes[-1] * (1.0003 if i % 2 == 0 else 0.9997))
    candles = make_candles(closes)

    signal = analyze(
        symbol="TESTUSDT",
        candles=candles,
        ewma_lambda=0.94,
        mad_window=100,
        price_zscore_threshold=3.0,
        volume_zscore_threshold=4.0,
        volume_min_price_move_z=1.5,
        min_history=60,
    )

    assert signal is not None
    assert signal.is_alert is False


def test_analyze_returns_none_without_enough_history():
    candles = make_candles([100.0, 101.0, 102.0])
    signal = analyze(
        symbol="TESTUSDT",
        candles=candles,
        ewma_lambda=0.94,
        mad_window=100,
        price_zscore_threshold=3.0,
        volume_zscore_threshold=4.0,
        volume_min_price_move_z=1.5,
        min_history=60,
    )
    assert signal is None


def test_analyze_flags_volume_surge_with_moderate_price_move():
    closes = [100.0]
    for i in range(120):
        closes.append(closes[-1] * (1.0003 if i % 2 == 0 else 0.9997))
    # Moderate final move plus a huge volume outlier.
    closes.append(closes[-1] * 1.01)
    volumes = [100.0] * 120 + [100.0, 5000.0]
    candles = make_candles(closes, volumes)

    signal = analyze(
        symbol="TESTUSDT",
        candles=candles,
        ewma_lambda=0.94,
        mad_window=100,
        price_zscore_threshold=3.0,
        volume_zscore_threshold=4.0,
        volume_min_price_move_z=0.1,
        min_history=60,
    )

    assert signal is not None
    assert signal.volume_alert is True

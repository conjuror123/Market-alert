from price_monitor.backtest import _price_alert, _severity, _volume_alert, simulate_notifications
from price_monitor.config import EffectiveParams

BASE_PARAMS = EffectiveParams(
    interval="1h", lookback=300, mad_window=288, ewma_lambda=0.94,
    price_zscore_threshold=3.0, price_zscore_override=6.0,
    volume_zscore_threshold=4.0, volume_zscore_override=8.0,
    volume_min_price_move_z=1.5, cooldown_minutes=2880, escalation_factor=1.3,
    min_history=60,
)


def step(open_time=0, ewma_z=0.0, robust_z=0.0, volume_z=0.0, return_pct=0.0):
    return {"open_time": open_time, "ewma_z": ewma_z, "robust_z": robust_z,
            "volume_z": volume_z, "return_pct": return_pct}


def test_price_alert_needs_both_signals_under_threshold():
    assert not _price_alert(step(ewma_z=3.5, robust_z=1.0), BASE_PARAMS)
    assert _price_alert(step(ewma_z=3.5, robust_z=3.5), BASE_PARAMS)


def test_price_alert_override_bypasses_confirmation():
    assert _price_alert(step(ewma_z=1.0, robust_z=6.5), BASE_PARAMS)
    assert _price_alert(step(ewma_z=6.5, robust_z=0.0), BASE_PARAMS)


def test_volume_alert_needs_confirming_price_move():
    assert not _volume_alert(step(volume_z=5.0, ewma_z=0.1, robust_z=0.1), BASE_PARAMS)
    assert _volume_alert(step(volume_z=5.0, ewma_z=2.0, robust_z=0.1), BASE_PARAMS)


def test_volume_alert_override_needs_no_price_confirmation():
    assert _volume_alert(step(volume_z=8.5, ewma_z=0.0, robust_z=0.0), BASE_PARAMS)


def test_severity_is_max_of_three_signals():
    assert _severity(step(ewma_z=-4.0, robust_z=2.0, volume_z=1.0)) == 4.0
    assert _severity(step(ewma_z=1.0, robust_z=1.0, volume_z=9.0)) == 9.0


def test_simulate_notifications_suppresses_similar_repeat_within_cooldown():
    hour = 3600
    series = [
        step(open_time=0, ewma_z=4.0, robust_z=4.0),
        step(open_time=hour, ewma_z=4.1, robust_z=4.1),  # 1h later, barely bigger
    ]
    notified = simulate_notifications(series, BASE_PARAMS)
    assert notified == {0}


def test_simulate_notifications_escalation_breaks_through_cooldown():
    hour = 3600
    series = [
        step(open_time=0, ewma_z=4.0, robust_z=4.0),
        step(open_time=hour, ewma_z=7.0, robust_z=7.0),  # much bigger, still in cooldown
    ]
    notified = simulate_notifications(series, BASE_PARAMS)
    assert notified == {0, hour}


def test_simulate_notifications_override_tier_always_gets_through():
    # Two independently extreme (override-tier) events an hour apart - e.g. a fast
    # multi-hour crash - must both notify even though neither is a 1.3x escalation
    # over the other, because each clears price_zscore_override (6.0) on its own.
    hour = 3600
    series = [
        step(open_time=0, ewma_z=8.0, robust_z=8.0),
        step(open_time=hour, ewma_z=6.5, robust_z=6.5),
    ]
    notified = simulate_notifications(series, BASE_PARAMS)
    assert notified == {0, hour}


def test_simulate_notifications_allows_repeat_after_cooldown_expires():
    week = 7 * 24 * 3600
    series = [
        step(open_time=0, ewma_z=4.0, robust_z=4.0),
        step(open_time=week + 3600, ewma_z=4.0, robust_z=4.0),
    ]
    notified = simulate_notifications(series, BASE_PARAMS)
    assert notified == {0, week + 3600}


def test_simulate_notifications_ignores_non_alerting_steps():
    series = [step(open_time=0, ewma_z=0.5, robust_z=0.5, volume_z=0.5)]
    assert simulate_notifications(series, BASE_PARAMS) == set()

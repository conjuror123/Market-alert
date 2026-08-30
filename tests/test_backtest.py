import json
import os
from dataclasses import replace

from price_monitor import backtest as backtest_module
from price_monitor import yahoo
from price_monitor.backtest import (
    YAHOO_MAX_HOURLY_DAYS,
    _price_alert,
    _severity,
    _volume_alert,
    calibrate_recall_threshold,
    cluster_events,
    fetch_backtest_history,
    simulate_notifications,
)
from price_monitor.config import AssetConfig, Config, EffectiveParams
from price_monitor.models import Candle, ExchangeError

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


def test_fetch_backtest_history_caps_yahoo_days_at_the_hard_limit(monkeypatch):
    """Yahoo hard-rejects (HTTP 422) any 60m-interval request spanning more
    than 730 days - a deep --since backfill must not pass that straight
    through and crash the whole run."""
    captured = {}
    monkeypatch.setattr(
        yahoo, "fetch_klines",
        lambda symbol, interval, limit, base_url, session, range_: captured.setdefault("range_", range_) or [],
    )
    asset = AssetConfig(symbol="GC=F", source="yahoo", label="Золото")
    cfg = Config(assets=[asset])
    fetch_backtest_history(asset, cfg.params_for(asset), cfg, days=2000, session=None)
    assert captured["range_"] == f"{YAHOO_MAX_HOURLY_DAYS}d"


def test_fetch_backtest_history_does_not_cap_yahoo_days_under_the_limit(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        yahoo, "fetch_klines",
        lambda symbol, interval, limit, base_url, session, range_: captured.setdefault("range_", range_) or [],
    )
    asset = AssetConfig(symbol="GC=F", source="yahoo", label="Золото")
    cfg = Config(assets=[asset])
    fetch_backtest_history(asset, cfg.params_for(asset), cfg, days=365, session=None)
    assert captured["range_"] == "370d"


def test_main_skips_an_asset_that_fails_and_still_processes_the_rest(tmp_path, monkeypatch, caplog):
    """The real incident this guards against: a backfill run hit a Twelve
    Data 429 on the 6th of 8 forex pairs and, before this fix, that crashed
    the whole script - losing the report for (and, in the actual workflow,
    the commit of) every asset already successfully fetched before it."""
    cfg = Config(
        assets=[
            AssetConfig(symbol="BTC-USD", source="coinbase", label="Bitcoin"),
            AssetConfig(symbol="EUR/USD", source="twelvedata", label="EUR/USD"),
            AssetConfig(symbol="ETH-USD", source="coinbase", label="Ethereum"),
        ],
        candle_history_dir=os.path.join(tmp_path, "candle_history"),
        min_history=1, daily_min_history=1,
    )
    monkeypatch.setattr(backtest_module, "load_config", lambda path=None: cfg)

    def fake_fetch(asset, params, cfg, days, session):
        if asset.symbol == "EUR/USD":
            raise ExchangeError("rate limited")
        return [
            Candle(open_time=i * 3600, open=1.0, high=1.0, low=1.0, close=1.0, volume=0.0,
                   close_time=i * 3600 + 3600)
            for i in range(5)
        ]

    monkeypatch.setattr(backtest_module, "fetch_backtest_history", fake_fetch)
    monkeypatch.setattr(
        "sys.argv", ["backtest", "--out", os.path.join(tmp_path, "out.json"), "--days", "30"])

    assert backtest_module.main() == 0
    assert "EUR/USD" in caplog.text  # logged, not silently dropped

    with open(os.path.join(tmp_path, "out.json")) as f:
        out = json.load(f)
    labels = {a["label"] for a in out["assets"]}
    assert labels == {"Bitcoin", "Ethereum"}  # EUR/USD skipped, the rest still made it in


def test_months_covered_top_n_scales_with_history_length():
    assert backtest_module.months_covered_top_n(30) == 1
    assert backtest_module.months_covered_top_n(365) == 12
    assert backtest_module.months_covered_top_n(5) == 1  # floored at 1, never zero


def test_calibrate_recall_threshold_finds_threshold_for_target_recall():
    """Eight escalating-severity spikes among 128 quiet daily periods (top_n
    = round(128/30) = 4): the threshold search should land wherever recall on
    the top-4 by size is closest to 50% (2 of the 4 biggest caught)."""
    params = replace(
        BASE_PARAMS, interval="1d", cooldown_minutes=1, escalation_factor=1.0,
        volume_zscore_threshold=1e9, volume_zscore_override=1e9,
    )
    day = 86400
    series = [step(open_time=i * day) for i in range(128)]
    for idx, z in zip([10, 25, 40, 55, 70, 85, 100, 115], range(1, 9)):
        series[idx] = step(open_time=idx * day, ewma_z=float(z), robust_z=float(z), return_pct=float(z))

    tuned, top_n, recall = calibrate_recall_threshold(series, params, target_fraction=0.5)

    assert top_n == 4
    assert recall == 0.5
    assert 6.0 < tuned.price_zscore_threshold <= 7.0
    # Every spike here has ewma_z == robust_z, so the override ratio never
    # changes which spikes get caught (dual confirmation alone already
    # decides it) - every ratio ties, and 1.0 (tried first for each
    # threshold) wins those ties, landing override == threshold rather than
    # the old fixed 3x.
    assert tuned.price_zscore_override == tuned.price_zscore_threshold


def test_calibrate_recall_threshold_searches_override_to_rescue_capped_ewma_events():
    """Events at rank 3-4 have ewma_z capped at 2.0 (below any threshold that
    would still exclude the always-missed rank 5-6 events) but a high
    robust_z - only price_zscore_override can catch them, dual confirmation
    never will. A fixed 3x ratio (the old behavior) would set override to
    18.0 at threshold=6.0, missing both capped events entirely (2/6 recall,
    off target); searching the ratio finds override=6.0 (ratio 1.0) instead,
    which rescues rank 3 (robust_z=6.0 >= 6.0) but not rank 4
    (robust_z=5.5 < 6.0) - landing exactly on the 50% target via a
    real quality difference, not just a coincidental threshold split."""
    params = replace(
        BASE_PARAMS, interval="1d", cooldown_minutes=1, escalation_factor=1.0,
        volume_zscore_threshold=1e9, volume_zscore_override=1e9,
    )
    day = 86400
    series = [step(open_time=i * day) for i in range(180)]  # top_n = round(180/30) = 6
    events = [
        (10, 10.0, 10.0, 10.0),  # rank1 - always caught via dual
        (30, 9.0, 9.0, 9.0),     # rank2 - always caught via dual
        (50, 2.0, 6.0, 6.0),     # rank3 - capped ewma, only override can catch (needs override <= 6.0)
        (70, 2.0, 5.5, 5.5),     # rank4 - capped ewma, only override can catch (needs override <= 5.5)
        (90, 1.0, 1.0, 1.0),     # rank5 - always missed
        (110, 0.5, 0.5, 0.5),    # rank6 - always missed
    ]
    for idx, ewma_z, robust_z, ret in events:
        series[idx] = step(open_time=idx * day, ewma_z=ewma_z, robust_z=robust_z, return_pct=ret)

    tuned, top_n, recall = calibrate_recall_threshold(series, params, target_fraction=0.5)

    assert top_n == 6
    assert recall == 0.5
    assert tuned.price_zscore_threshold == 6.0
    assert tuned.price_zscore_override == 6.0


def test_calibrate_recall_threshold_grows_top_n_when_recall_is_stuck_at_a_ceiling():
    """The two biggest moves both have z-scores of 20 - past even the search's
    highest threshold candidate (hi=15.0 by default) - so with the initial
    top_n=2 (round(60/30)), recall is stuck at 100% for every single
    (threshold, override) combination the search tries; nothing can push it
    toward the 50% target. Real per-asset case this guards against: a
    calibration landing at 59/60 recall isn't "almost calibrated" - it means
    the top-N pool itself undercounted how many of this asset's moves are
    genuinely significant (see _RECALL_CEILING). Growing top_n to 4 adds two
    more, deliberately never-caught moves, settling recall exactly on 50%."""
    params = replace(
        BASE_PARAMS, interval="1d", cooldown_minutes=1, escalation_factor=1.0,
        volume_zscore_threshold=1e9, volume_zscore_override=1e9,
    )
    day = 86400
    series = [step(open_time=i * day) for i in range(60)]  # initial top_n = round(60/30) = 2
    events = [
        (10, 20.0, 20.0, 20.0),  # rank1 - z clears even the highest threshold candidate
        (20, 20.0, 20.0, 19.0),  # rank2 - same
        (30, 0.1, 0.1, 0.5),     # rank3 (only exists once top_n grows) - never caught
        (40, 0.1, 0.1, 0.4),     # rank4 (only exists once top_n grows) - never caught
    ]
    for idx, ewma_z, robust_z, ret in events:
        series[idx] = step(open_time=idx * day, ewma_z=ewma_z, robust_z=robust_z, return_pct=ret)

    tuned, top_n, recall = calibrate_recall_threshold(series, params, target_fraction=0.5)

    assert top_n == 4
    assert recall == 0.5


def test_calibrate_recall_threshold_never_grows_top_n_past_the_available_series_length():
    """Guards the growth loop's stopping condition: if even the full series
    isn't enough to bring recall down from its ceiling, top_n must stop at
    the series length rather than looping forever or requesting an N bigger
    than what actually exists."""
    params = replace(
        BASE_PARAMS, interval="1d", cooldown_minutes=1, escalation_factor=1.0,
        volume_zscore_threshold=1e9, volume_zscore_override=1e9,
    )
    day = 86400
    # Every single period is an unmissable spike - recall can never drop
    # below 100% no matter how large top_n grows, since there's nothing else
    # in the series to dilute it with.
    series = [step(open_time=i * day, ewma_z=20.0, robust_z=20.0, return_pct=20.0) for i in range(10)]

    tuned, top_n, recall = calibrate_recall_threshold(series, params, target_fraction=0.5)

    assert top_n == 10
    assert recall == 1.0


def test_cluster_events_splits_when_gap_exceeds_window():
    day = 86400
    events = [(0, "A"), (day, "B"), (10 * day, "C")]
    assert cluster_events(events, gap_seconds=2 * day) == [
        [(0, "A"), (day, "B")],
        [(10 * day, "C")],
    ]


def test_cluster_events_rolling_window_extends_chain():
    """Each event sits within the gap of the *previous* one, but the first
    and last are 6 days apart - still one cluster, since the merge window
    rolls forward with each new item rather than being fixed from the start."""
    day = 86400
    events = [(0, "A"), (2 * day, "B"), (4 * day, "C"), (6 * day, "D")]
    clusters = cluster_events(events, gap_seconds=2 * day)
    assert len(clusters) == 1
    assert len(clusters[0]) == 4


def test_cluster_events_handles_empty_input():
    assert cluster_events([], gap_seconds=86400) == []

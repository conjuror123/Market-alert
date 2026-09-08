import numpy as np
import pandas as pd

from tremor import market, routing, severity, windows

HOUR = 3600


def scores(excess, base=-6.0):
    """A detector-scores frame whose exceedance follows `excess`."""
    n = len(excess)
    threshold = np.full(n, base)
    return pd.DataFrame({
        "hour_utc": np.arange(n) * HOUR,
        "forecast": threshold + np.asarray(excess, dtype="float64"),
        "threshold": threshold,
    })


def test_the_ladder_reads_the_exceedance_not_the_level():
    # The forecast is not stationary - its level depends on which instruments
    # are in the basket, and this basket grew. Fitted on an expanding window a
    # ladder on the level never fires again: measured, the routine level
    # settled at -5.21 during the 2021-2022 warm-up and the forecast never
    # reached it afterwards. The exceedance is stationary by construction,
    # because the detector's own threshold is a rolling quantile.
    rng = np.random.default_rng(3)
    n = 8766 * 4
    drift = np.linspace(0.0, -1.5, n)              # the level wanders away
    frame = pd.DataFrame({
        "hour_utc": np.arange(n) * HOUR,
        "threshold": -6.0 + drift,
        "forecast": -6.0 + drift + np.abs(rng.standard_t(4, n)) * 0.1,
    })
    out = market.tiers(frame)
    assert out["tier"].notna().any()
    # and the firings are not all crowded into the start, as a ladder on the
    # drifting level would make them
    fired = out.loc[out["tier"].notna(), "hour_utc"]
    assert fired.max() > 0.75 * out["hour_utc"].max()


def test_only_a_loud_market_fires_never_a_quiet_one():
    rng = np.random.default_rng(5)
    n = 8766 * 4
    frame = scores(rng.standard_t(4, n) * 0.1)
    out = market.tiers(frame)
    fired = out.loc[out["tier"].notna(), "excess"]
    assert (fired > 0).all()


def test_consecutive_hours_are_one_event():
    # A disorderly market stays disorderly for hours, and reporting each of
    # those hours would be reporting one event many times.
    scored = pd.DataFrame({
        "hour_utc": np.arange(6) * HOUR,
        "excess": [0.1, 0.2, 0.3, 0.1, 0.1, 0.1],
        "tier": pd.array(["routine", "routine", "major", None, None, None],
                         dtype="string"),
    })
    out = market.events(scored, cooldown_bars=windows.CLUSTER_COOLDOWN)
    assert len(out) == 1
    assert out["repeat_count"].iloc[0] == 2
    # and it keeps the worst hour it reached, not the one it opened on
    assert out["tier"].iloc[0] == "major"
    assert out["excess"].iloc[0] == 0.3


def test_a_new_event_opens_after_the_cluster_cooldown():
    gap = windows.CLUSTER_COOLDOWN + 1
    scored = pd.DataFrame({
        "hour_utc": np.arange(gap + 1) * HOUR,
        "excess": 0.2,
        "tier": pd.array(["routine"] + [None] * (gap - 1) + ["routine"],
                         dtype="string"),
    })
    assert len(market.events(scored)) == 2


def test_a_quiet_history_produces_no_events_and_keeps_the_schema():
    scored = pd.DataFrame({"hour_utc": [0, HOUR], "excess": [0.0, 0.0],
                           "tier": pd.array([None, None], dtype="string")})
    out = market.events(scored)
    assert out.empty
    assert {"event_id", "hour_utc", "tier", "excess", "repeat_count"} <= set(out.columns)


def test_market_events_are_marked_as_such():
    # So they can be delivered in one stream with the instrument alerts and
    # still be told apart in the message.
    rng = np.random.default_rng(7)
    out = market.build(scores(np.abs(rng.standard_t(4, 8766 * 4)) * 0.1))
    assert (out["basis"] == "market").all()
    assert {"channel", "digest_slot"} <= set(out.columns)


def test_a_market_event_is_not_gated_on_a_retention_test_it_cannot_take():
    # A volatility regime is not a price move: a spike that subsided within the
    # day was still a real spike. Left on, the retention check would have
    # nothing to read and every market event would silently be DROPPED for
    # failing a test it was never given. Shown on a digest tier, because that is
    # where the drop-on-revert rule still lives - the push tiers no longer
    # consult retention at all, they are corrected after the fact instead.
    events = pd.DataFrame({"hour_utc": [HOUR], "tier": pd.array(["notable"], dtype="string"),
                           "retention_settled": [-0.4]})
    assert routing.route(events, require_retention=False)["channel"].iloc[0] == routing.DIGEST
    assert routing.route(events)["channel"].iloc[0] == routing.DROPPED


def test_without_retention_the_lower_tiers_still_digest():
    events = pd.DataFrame({"hour_utc": [HOUR, 2 * HOUR],
                           "tier": pd.array(["routine", "notable"], dtype="string")})
    routed = routing.route(events, require_retention=False)
    assert list(routed["channel"]) == [routing.DIGEST, routing.DIGEST]
    assert routed["digest_slot"].notna().all()


def test_the_market_ladder_uses_the_same_tier_names():
    # The whole point of routing these together is that "the market's most
    # disorderly hour in two years" and "gold's biggest move in two years" sit
    # on one scale rather than two parallel notions of importance.
    rng = np.random.default_rng(11)
    out = market.tiers(scores(np.abs(rng.standard_t(4, 8766 * 4)) * 0.1))
    assert set(out["tier"].dropna()) <= set(severity.TIERS)

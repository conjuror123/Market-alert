import numpy as np
import pandas as pd
import pytest

from meals import vix

DAY = 86400
HOUR = 3600


def series(closes, start=0):
    days = [start + i * DAY for i in range(len(closes))]
    return pd.DataFrame({"day": days, "close": closes,
                         "available_at": [d + DAY for d in days]})


def calm_then(final, n=1000, level=20.0):
    """The series has to be long: Z appears only after the 720-bar sigma_LT
    burn-in, and the thresholds need further observations on top of that. On the
    real series of 9262 daily values that is long past."""
    rng = np.random.default_rng(0)
    closes = list(level * np.exp(np.cumsum(rng.normal(0, 0.01, n))))
    return series(closes + [closes[-1] * final])


def test_a_fall_is_never_a_spike():
    # Fear and relief are not symmetric: a sharp rise in VIX means the market is
    # paying for protection, while an equal fall is merely a return to normal.
    scored = vix.score(calm_then(0.70), window=200)
    assert not bool(scored["is_spike"].iloc[-1])


def test_a_large_rise_is_a_spike():
    scored = vix.score(calm_then(1.60), window=200)
    assert bool(scored["is_spike"].iloc[-1])


def test_spike_needs_the_absolute_leg_too():
    # A tiny rise can clear the percentile in a very quiet period, but that does
    # not make it stress.
    scored = vix.score(calm_then(1.001), window=200)
    assert not bool(scored["is_spike"].iloc[-1])


def test_window_lasts_the_specified_reference_hours():
    reference = np.arange(0, 200 * HOUR, HOUR)
    scored = pd.DataFrame({"available_at": [10 * HOUR], "is_spike": [True]})
    built = vix.windows_from_spikes(scored, reference, window_hours=24)

    assert len(built) == 1
    assert built[0].opened_at == 10 * HOUR
    assert (built[0].closes_at - built[0].opened_at) / HOUR == 24


def test_repeat_spike_inside_a_window_does_not_extend_it():
    # Otherwise a prolonged period of high volatility would keep the multiplier on
    # for weeks, and it would stop distinguishing an acute moment from the
    # background.
    reference = np.arange(0, 200 * HOUR, HOUR)
    scored = pd.DataFrame({"available_at": [10 * HOUR, 20 * HOUR], "is_spike": [True, True]})
    built = vix.windows_from_spikes(scored, reference, window_hours=24)

    assert len(built) == 1
    assert built[0].spike_count == 2
    assert built[0].closes_at == 34 * HOUR


def test_a_new_window_opens_after_the_previous_closes():
    reference = np.arange(0, 200 * HOUR, HOUR)
    scored = pd.DataFrame({"available_at": [10 * HOUR, 40 * HOUR], "is_spike": [True, True]})
    built = vix.windows_from_spikes(scored, reference, window_hours=24)

    assert len(built) == 2
    assert all(w.spike_count == 1 for w in built)


def test_multiplier_is_one_outside_and_raised_inside():
    built = [vix.VixWindow(opened_at=10 * HOUR, closes_at=34 * HOUR, spike_count=1)]
    hours = [5 * HOUR, 10 * HOUR, 33 * HOUR, 34 * HOUR, 50 * HOUR]
    values = vix.multiplier_series(hours, built)

    assert values[5 * HOUR] == 1.0
    assert values[10 * HOUR] == vix.M_VIX
    assert values[33 * HOUR] == vix.M_VIX
    assert values[34 * HOUR] == 1.0     # the edge is exclusive
    assert values[50 * HOUR] == 1.0


def test_no_windows_means_no_multiplier():
    assert vix.multiplier_series([HOUR, 2 * HOUR], []) == {HOUR: 1.0, 2 * HOUR: 1.0}


def test_missing_series_says_how_to_get_it(tmp_path):
    with pytest.raises(FileNotFoundError, match="meals.backfill"):
        vix.load_series(str(tmp_path / "missing.parquet"))

import numpy as np
import pandas as pd

from tremor import vix, windows

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


def test_the_vectorised_mad_matches_a_per_window_median_exactly():
    # Vectorised for speed, so it has to be exact rather than close: it is
    # checked against the per-window form it replaced, NaNs and short series
    # included.
    def per_window(series, window):
        def mad(values):
            median = np.median(values)
            return float(np.median(np.abs(values - median)))
        return series.shift(1).rolling(window).apply(mad, raw=True)

    rng = np.random.default_rng(0)
    for n, holes in ((5000, 0), (5000, 200), (50, 0), (24, 0), (20, 0)):
        values = rng.normal(size=n)
        if holes:
            values[rng.choice(n, holes, replace=False)] = np.nan
        series = pd.Series(values)
        expected = per_window(series, windows.MAD_WINDOW)
        actual = vix._rolling_mad(series, windows.MAD_WINDOW)
        # NaN in the same places, and bit-identical where both are finite.
        assert expected.isna().equals(actual.isna())
        assert np.array_equal(expected.dropna().to_numpy(), actual.dropna().to_numpy())


def test_the_vectorised_mad_spans_more_than_one_chunk():
    # The |x - median| step materialises, so it runs in chunks; the seam between
    # two chunks must not drop or duplicate a window.
    rng = np.random.default_rng(1)
    series = pd.Series(rng.normal(size=3000))
    whole = vix._rolling_mad(series, windows.MAD_WINDOW)
    original = vix._MAD_CHUNK
    try:
        vix._MAD_CHUNK = 500
        chunked = vix._rolling_mad(series, windows.MAD_WINDOW)
    finally:
        vix._MAD_CHUNK = original
    assert whole.equals(chunked)


# --- the overnight gap, kept beside r ----------------------------------------

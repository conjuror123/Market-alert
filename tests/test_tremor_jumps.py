"""Stage 0 of the jump detector: the score and the word, and nothing else."""
import math

import numpy as np
import pandas as pd
import pytest

from tremor import jumps

HOUR = 3600
DAY = 86400


def test_the_yardstick_is_the_bipower_mean_of_the_moves_before():
    # Products of neighbouring moves, stamped at the later one: |-2*1| = 2 at
    # bar 1, |3*-2| = 6 at bar 2. Bar 3 is judged against both, bar 2 against
    # the first alone.
    sigma = jumps.half_year_sigma([0, HOUR, 2 * HOUR, 3 * HOUR], [1.0, -2.0, 3.0, -4.0],
                                  min_count=1)
    assert np.isnan(sigma[0]) and np.isnan(sigma[1])
    assert sigma[2] == pytest.approx(math.sqrt(math.pi / 2 * 2))
    assert sigma[3] == pytest.approx(math.sqrt(math.pi / 2 * 4))


def test_the_hour_being_judged_never_enters_its_own_yardstick():
    hours = np.arange(200) * HOUR
    rng = np.random.default_rng(1)
    calm = rng.normal(0, 0.001, 200)
    jumped = calm.copy()
    jumped[150] = 0.05
    before = jumps.half_year_sigma(hours, calm, min_count=10)
    after = jumps.half_year_sigma(hours, jumped, min_count=10)
    assert after[150] == before[150]
    assert after[151] != before[151]


def test_one_jump_in_the_window_barely_moves_the_yardstick():
    # The reason for products rather than squares: a jump pairs with ordinary
    # moves on either side, never with itself.
    hours = np.arange(1000) * HOUR
    moves = np.random.default_rng(2).normal(0, 0.001, 1000)
    quiet = jumps.half_year_sigma(hours, moves, min_count=10)[-1]
    moves[500] = 0.05
    bipower = jumps.half_year_sigma(hours, moves, min_count=10)[-1]
    squares = np.sqrt(np.mean(moves[:-1] ** 2))
    assert abs(bipower / quiet - 1) < 0.1
    assert squares / quiet > 1.5


def test_the_window_is_calendar_days_whatever_the_calendar():
    # Two readings a day apart, the second window_days + a day later: the old
    # product has left the window by then.
    window = 10.0
    hours = [0, DAY, 2 * DAY, 2 * DAY + int((window + 1) * DAY)]
    sigma = jumps.half_year_sigma(hours, [1.0, 1.0, 1.0, 1.0], window_days=window,
                                  min_count=1)
    assert np.isnan(sigma[3])
    sigma = jumps.half_year_sigma(hours[:3] + [2 * DAY + int((window - 1) * DAY)],
                                  [1.0, 1.0, 1.0, 1.0], window_days=window, min_count=1)
    assert np.isfinite(sigma[3])


def test_a_young_series_is_scored_from_the_papers_minimum():
    assert jumps.minimum_count(7) == 42
    assert jumps.minimum_count(24) == 78
    hours = np.arange(100) * HOUR
    sigma = jumps.half_year_sigma(hours, np.full(100, 0.001), min_count=42)
    first = int(np.flatnonzero(np.isfinite(sigma))[0])
    # 42 products need 43 readings before the one being judged.
    assert first == 43


def test_a_cut_history_scores_the_shared_hours_the_same():
    rng = np.random.default_rng(3)
    n = 24 * 400
    frame = pd.DataFrame({"hour_utc": np.arange(n) * HOUR,
                          "r": rng.standard_t(4, n) * 0.001})
    full = jumps.score(frame, "crypto_24_7")
    cut = jumps.score(frame.iloc[-24 * 250:], "crypto_24_7")
    shared = full.set_index("hour_utc").loc[cut["hour_utc"]]
    settled = cut["hour_utc"] >= cut["hour_utc"].iloc[0] + int(jumps.WINDOW_DAYS * DAY)
    # Equal to the last digit, not bit for bit: a rolling mean carries a running
    # sum, and where it started leaves a rounding trace of about 1e-16.
    np.testing.assert_allclose(shared["z"].to_numpy()[settled.to_numpy()],
                               cut["z"].to_numpy()[settled.to_numpy()], rtol=1e-12)


def test_each_word_is_root_two_bigger_than_the_one_below():
    assert jumps.levels(3.9, math.sqrt(2)) == pytest.approx((3.9, 5.515, 7.8, 11.03), abs=1e-3)
    words = jumps.word_of([3.8, -3.9, 5.6, -8.0, 12.0, np.nan])
    assert list(words) == [None, "noticeable", "high", "major", "extreme", None]


def test_the_young_stretch_is_marked():
    frame = pd.DataFrame({"hour_utc": np.arange(24 * 400) * HOUR,
                          "r": np.full(24 * 400, 0.001)})
    scored = jumps.score(frame, "crypto_24_7")
    boundary = int(jumps.WINDOW_DAYS * DAY)
    assert scored.loc[scored["hour_utc"] < boundary, "young"].all()
    assert not scored.loc[scored["hour_utc"] >= boundary, "young"].any()


def test_a_gap_of_48_hours_or_more_is_a_weekend():
    kinds = jumps.gap_kinds([17.5, 41.5, 48.0, 65.5, 89.5])
    assert list(kinds) == ["night", "night", "weekend", "weekend", "weekend"]


def _sessions(days=400):
    """A fund-like calendar: one bar per weekday, a gap on each, weekends quiet
    at one size and nights at another, so each kind's yardstick is known."""
    rows, t = [], 0
    rng = np.random.default_rng(4)
    day = pd.Timestamp("2021-01-04 14:30", tz="UTC")
    for i in range(days):
        d = day + pd.Timedelta(days=i)
        if d.weekday() >= 5:
            continue
        monday = d.weekday() == 0
        rows.append({"hour_utc": int(d.timestamp()), "r": rng.normal(0, 0.001),
                     "gap": rng.normal(0, 0.010 if monday else 0.002)})
    return pd.DataFrame(rows)


def test_a_weekend_is_judged_only_against_weekends():
    scored = jumps.score_gaps(_sessions())
    settled = scored[~scored["young"]]
    weekend = settled[settled["reading"] == "weekend"]["sigma"].median()
    night = settled[settled["reading"] == "night"]["sigma"].median()
    # Weekends were drawn five times bigger than nights; each kind's yardstick
    # must follow its own kind, not the pool.
    assert 3.5 < weekend / night < 7


def test_a_gap_left_unscored_is_not_a_reading():
    frame = _sessions()
    frame.loc[frame.index[200], "gap"] = np.nan
    scored = jumps.score_gaps(frame)
    assert frame.loc[frame.index[200], "hour_utc"] not in set(scored["hour_utc"])
    assert len(scored) == frame["gap"].notna().sum() - 1   # the first bar has no gap before it


def test_weekends_are_scored_from_seven():
    scored = jumps.score_gaps(_sessions())
    weekends = scored[scored["reading"] == "weekend"]
    first = int(np.flatnonzero(np.isfinite(weekends["sigma"].to_numpy(dtype=float)))[0])
    assert first == jumps.GAP_MIN_COUNT["weekend"] + 1

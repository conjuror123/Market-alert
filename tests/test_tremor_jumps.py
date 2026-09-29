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


def _flags(rows):
    return pd.DataFrame([{"hour_utc": int(pd.Timestamp(t, tz="UTC").timestamp()),
                          "reading": reading, "word": word} for t, reading, word in rows])


def test_an_event_is_24_real_hours_from_its_first_move():
    # Not a trading day and not a count of candles: a fund's 15:00 New York move
    # and the next morning's open (found 14:00 UTC) are one event.
    found = [0, 5 * HOUR, 23 * HOUR, 24 * HOUR, 30 * HOUR, 48 * HOUR]
    assert list(jumps.event_starts(found)) == [0, 0, 0, 24 * HOUR, 24 * HOUR, 48 * HOUR]


def test_a_later_move_does_not_stretch_the_event():
    # 24 hours from the FIRST move, whatever comes inside them.
    found = [0, 20 * HOUR, 26 * HOUR]
    assert list(jumps.event_starts(found)) == [0, 0, 26 * HOUR]


def test_an_event_on_the_channel_keeps_its_24_hours():
    # Its first move corrected away does not slide it later.
    assert list(jumps.event_starts([5 * HOUR, 25 * HOUR], anchors=[0])) == [0, 25 * HOUR]


def test_a_late_move_just_before_an_event_on_the_channel_joins_it():
    # A bar that arrives late, found three hours before the event on the
    # channel, would open an event overlapping it; it joins it instead.
    starts = jumps.event_starts([-3 * HOUR, -30 * HOUR], anchors=[0])
    assert list(starts) == [0, -30 * HOUR]


def test_an_event_is_worded_by_its_biggest_reading():
    readings = pd.DataFrame({"asset_id": ["a", "a", "a"], "event_start": [0, 0, 0],
                             "z": [4.0, -9.0, 6.0],
                             "word": ["noticeable", "major", "high"]})
    assert list(jumps.events(readings)["word"]) == ["major"]


def test_an_hour_is_found_once_it_has_ended_and_not_before():
    # The run at 10:05 stands in the 10:00 bar, which holds five minutes: only
    # 09:00 can be judged.
    flags = _flags([("2024-01-02 09:00", "hour", "noticeable"),
                    ("2024-01-02 10:00", "hour", "noticeable")])
    now = int(pd.Timestamp("2024-01-02 10:05", tz="UTC").timestamp())
    out = jumps.ended(flags, "crypto_24_7", now)
    assert list(out["hour_utc"]) == [int(flags["hour_utc"].iloc[0])]
    assert out["found_utc"].iloc[0] == int(flags["hour_utc"].iloc[0]) + HOUR


def test_a_funds_gap_is_found_with_its_first_bar_and_a_pairs_at_its_open():
    # A fund's gap is judged with its first bar (13:30-14:00, stamped 13:00),
    # once that bar has ended; a currency pair's weekend gap is its open.
    fund = _flags([("2024-01-02 13:00", "night", "noticeable")])
    pair = _flags([("2024-01-07 22:00", "weekend", "noticeable")])
    fund_open = int(fund["hour_utc"].iloc[0])
    pair_open = int(pair["hour_utc"].iloc[0])
    assert jumps.ended(fund, "us_equity", fund_open + 1800).empty
    assert jumps.ended(fund, "us_equity", fund_open + HOUR)["found_utc"].iloc[0] == fund_open + HOUR
    assert jumps.ended(pair, "fx_continuous", pair_open)["found_utc"].iloc[0] == pair_open


def test_the_detector_version_ignores_comments_and_follows_the_settings(tmp_path):
    import shutil

    root = tmp_path / "repo"
    (root / "tremor").mkdir(parents=True)
    (root / "config").mkdir()
    for name in ("jumps.py", "returns.py", "pipeline.py", "quality.py", "routing.py",
                 "windows.py"):
        shutil.copy(f"tremor/{name}", root / "tremor" / name)
    shutil.copy(jumps.DEFAULT_BASKET_PATH, root / "config" / "basket.yaml")
    before = jumps.detector_version(str(root))

    code = root / "tremor" / "routing.py"
    code.write_text(code.read_text() + "\n# a comment\n")
    basket = root / "config" / "basket.yaml"
    basket.write_text(basket.read_text() + "\n# a comment\n")
    assert jumps.detector_version(str(root)) == before
    assert before == jumps.detector_version()       # the real repository, from anywhere

    basket.write_text(basket.read_text().replace("noticeable_sigma: 3.9",
                                                 "noticeable_sigma: 4.0"))
    assert jumps.detector_version(str(root)) != before


def _events(words, reading="hour"):
    base = pd.Timestamp("2026-09-15 14:00", tz="UTC")
    return pd.DataFrame([{"asset_id": "twelvedata:GLD",
                          "hour_utc": int((base + pd.Timedelta(hours=i)).timestamp()),
                          "reading": reading, "word": w, "r": 0.02, "sigma": 0.002,
                          "z": 10.0}
                         for i, w in enumerate(words)])


def test_delivery_gets_each_readings_event():
    out = jumps.for_delivery(_events(["noticeable", "high"]))
    first = int(out["hour_utc"].iloc[0]) + HOUR
    assert list(out["event_start"]) == [first, first]


def test_high_and_up_push_and_noticeable_goes_into_the_weekly_note():
    out = jumps.for_delivery(_events(["noticeable", "high", "major", "extreme"]))
    assert list(out["channel"]) == ["digest", "push", "push", "push"]
    assert (out["basis"] == "jump").all()


def test_a_reading_is_named_by_its_instrument_reading_and_hour():
    out = jumps.for_delivery(_events(["high"], reading="weekend"))
    hour = int(out["hour_utc"].iloc[0])
    assert out["reading_id"].iloc[0] == f"jump:twelvedata:GLD:weekend:{hour}"
    assert bool(out["overnight"].iloc[0]) and out["gap_kind"].iloc[0] == "weekend"
    assert out["sigma_lt"].iloc[0] == 0.002


# --- stage 3: rarest since ---------------------------------------------------

def test_the_answer_is_the_most_recent_move_at_least_95_percent_as_big():
    hours = [0, 1, 2, 3]
    z = [7.0, 4.9, 3.0, 5.0]           # 4.9 >= 4.75: it answers the 5.0, not the older 7.0
    hour, match = jumps.matches(hours, z)
    assert hour[3] == 1 and match[3] == 4.9


def test_a_bigger_move_counts_when_nothing_close_is_more_recent():
    hour, match = jumps.matches([0, 1, 2], [7.0, 4.7, 5.0])   # 4.7 < 4.75
    assert hour[2] == 0 and match[2] == 7.0


def test_only_the_same_direction_counts():
    hour, _ = jumps.matches([0, 1], [-6.0, 5.0])
    assert hour[1] == -1


def test_nothing_at_least_as_rare_in_the_record_is_no_answer():
    hour, match = jumps.matches([0, 1], [4.0, 9.0])
    assert hour[1] == -1 and np.isnan(match[1])


def test_each_kind_is_read_against_its_own_kind():
    # A 6-sigma night is not matched by a 6-sigma hour: each is in its own sigma.
    scored = pd.DataFrame({"hour_utc": [0, HOUR, 2 * HOUR, 3 * HOUR],
                           "reading": ["hour", "night", "hour", "night"],
                           "z": [6.0, 6.5, 5.0, 6.2]})
    out = jumps.rarest_since(scored)
    assert out["since_utc"].tolist()[2] == 0
    assert out["since_utc"].tolist()[3] == HOUR
    assert pd.isna(out["since_utc"].iloc[1])


def test_a_smaller_move_in_between_does_not_hide_the_answer():
    # 9, then 5, then 8: for a 7.9 the answer is the 8, the most recent move
    # at least 7.5 - the 5 between them must not steer the search to the 9.
    hour, match = jumps.matches([0, 1, 2, 3], [9.0, 5.0, 8.0, 7.9])
    assert hour[3] == 2 and match[3] == 8.0


# --- stage 4: held at the funds' close ----------------------------------------

def _bars(start, moves, gaps=None):
    hours = [start + i * HOUR for i in range(len(moves))]
    return pd.DataFrame({"hour_utc": hours, "r": moves,
                         "gap": gaps if gaps is not None else [np.nan] * len(moves)})


def test_an_hour_is_checked_at_the_close_and_measured_from_its_own_open():
    # Tuesday 8 September 2026: bars from 14:00 UTC; the close is 20:00 UTC.
    start = int(pd.Timestamp("2026-09-08 14:00", tz="UTC").timestamp())
    bars = _bars(start, [0.0, 0.010, -0.002, -0.002, 0.0, 0.0])
    move = pd.DataFrame({"hour_utc": [start + HOUR], "found_utc": [start + 2 * HOUR],
                         "reading": ["hour"]})
    check, held = jumps.held_at_close(bars, move, now=start + 10 * HOUR)
    assert check[0] == start + 6 * HOUR
    assert held[0] == pytest.approx(0.6)             # 1.0% up, 0.4% given back


def test_a_gap_is_measured_from_the_close_before_it():
    start = int(pd.Timestamp("2026-09-08 14:00", tz="UTC").timestamp())
    bars = _bars(start, [0.001, -0.003, 0.0, 0.0, 0.0, 0.0],
                 gaps=[0.010, np.nan, np.nan, np.nan, np.nan, np.nan])
    gap = pd.DataFrame({"hour_utc": [start], "found_utc": [start + HOUR],
                        "reading": ["night"]})
    _, held = jumps.held_at_close(bars, gap, now=start + 10 * HOUR)
    assert held[0] == pytest.approx(0.8)             # 1.0 + 0.1 - 0.3


def test_the_closing_hour_is_checked_at_the_next_close():
    friday_close = int(pd.Timestamp("2026-09-04 20:00", tz="UTC").timestamp())
    bars = _bars(friday_close - HOUR, [0.01])
    move = pd.DataFrame({"hour_utc": [friday_close - HOUR], "found_utc": [friday_close],
                         "reading": ["hour"]})
    check, held = jumps.held_at_close(bars, move, now=friday_close + 300)
    assert check[0] == int(pd.Timestamp("2026-09-08 20:00", tz="UTC").timestamp())  # Labor Day
    assert np.isnan(held[0])


def test_the_answer_waits_for_the_bars_to_reach_the_close():
    start = int(pd.Timestamp("2026-09-08 14:00", tz="UTC").timestamp())
    bars = _bars(start, [0.0, 0.010, -0.002])        # the store stops at 17:00
    move = pd.DataFrame({"hour_utc": [start + HOUR], "found_utc": [start + 2 * HOUR],
                         "reading": ["hour"]})
    _, held = jumps.held_at_close(bars, move, now=start + 10 * HOUR)
    assert np.isnan(held[0])

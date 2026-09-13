"""The scorer for the detector that is actually delivered.

Built on synthetic series whose answer is known by construction rather than on
the store, because the whole point of this module is to be a yardstick: a
yardstick tested against the thing it measures cannot fail.
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from tremor import saed_score, severity

HOUR = 3600
START = int(datetime(2006, 1, 2, tzinfo=timezone.utc).timestamp())


def series(n, values=None, start=START, step=HOUR):
    hours = np.arange(n, dtype="int64") * step + start
    if values is None:
        values = background(n)
    return pd.DataFrame({"hour_utc": hours, "score": np.asarray(values, dtype=float)})


def background(n, spikes=None):
    """An ordinary-looking series with optional spikes at named indices.

    Deliberately not constant. A flat series has every quantile equal to its own
    value, so every bar clears every line and every test passes for a reason
    that has nothing to do with the code.
    """
    values = np.abs(np.sin(np.arange(n) * 0.7)) + 0.01
    for index, size in (spikes or {}).items():
        values[index] = size
    return values


def ladder_rows(assets, tiers=severity.TIERS, from_hour=START):
    rows = []
    for asset in assets:
        row = {"asset_id": asset, "ladder": "absolute", "from_hour": from_hour}
        for tier in severity.TIERS:
            row[f"level_{tier}"] = 1.0 if tier in tiers else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def events(rows):
    return pd.DataFrame(rows, columns=["asset_id", "hour_utc", "tier", "basis"])


# --- the frequency claim ------------------------------------------------------

def fired_over(years, per_year, tier="extreme", asset="src:A"):
    """`per_year` events a year for `years` years, the last one closing the span.

    The span is measured from the ladder's start to the LAST event, so the last
    one has to land on the end or the denominator is short and every ratio comes
    out high.
    """
    end = START + int(years * saed_score.SECONDS_PER_YEAR)
    count = max(int(round(years * per_year)), 1)
    hours = np.linspace(START, end, count).astype("int64")
    return events([(asset, int(h), tier, "absolute") for h in hours])


def test_a_tier_firing_at_its_claimed_rate_scores_one():
    # The whole point of the table: "about once in 6 years" is a falsifiable
    # statement and this is what it looks like when it holds.
    claimed = 365.25 / severity.tier_days()["extreme"]
    cal = saed_score.calibration(fired_over(12.0, claimed), ladder_rows(["src:A"]))
    row = cal[cal["tier"].eq("extreme")].iloc[0]
    assert row["ratio"] == pytest.approx(1.0, abs=0.05)


def test_a_tier_that_fires_twice_as_often_as_it_promises_scores_two():
    claimed = 365.25 / severity.tier_days()["extreme"]
    cal = saed_score.calibration(fired_over(12.0, 2 * claimed), ladder_rows(["src:A"]))
    row = cal[cal["tier"].eq("extreme")].iloc[0]
    assert row["ratio"] == pytest.approx(2.0, rel=0.1)
    # and the words the report prints move with it
    assert row["actual_days"] < severity.tier_days()["extreme"]


def test_an_instrument_with_no_level_is_left_out_of_the_denominator():
    # An instrument whose fit never reached `extreme` is silent there by
    # construction. Counting its years would report the ladder as well behaved
    # for the arithmetic reason that part of the watchlist cannot speak.
    end = START + int(10 * saed_score.SECONDS_PER_YEAR)
    ladder = pd.concat([ladder_rows(["src:A"]),
                        ladder_rows(["src:B"], tiers=("noticeable", "high", "major"))])
    rows = [("src:A", END, "extreme", "absolute")
            for END in (START, end)]
    cal = saed_score.calibration(events(rows), ladder)
    extreme = cal[cal["tier"].eq("extreme")].iloc[0]
    major = cal[cal["tier"].eq("major")].iloc[0]
    assert extreme["instruments"] == 1
    assert major["instruments"] == 2
    assert extreme["instrument_years"] < major["instrument_years"]


def test_the_instruments_with_no_level_are_named():
    ladder = pd.concat([ladder_rows(["src:A"]),
                        ladder_rows(["src:B"], tiers=("noticeable",))])
    missing = saed_score.unreachable_tiers(ladder)
    assert missing["extreme"] == ["src:B"]
    assert "noticeable" not in missing


# --- the hindsight label ------------------------------------------------------

def test_the_label_is_drawn_where_the_claimed_rate_falls_in_the_sample():
    # No fit and no extrapolation. Twelve years of hourly bars at one an hour is
    # about two `extreme` occurrences, so the line sits at the second largest.
    n = int(12 * saed_score.SECONDS_PER_YEAR / HOUR)
    values = background(n, {n - 1: 100.0, n - 2: 90.0, n - 3: 80.0})
    levels = saed_score.hindsight_levels({"src:A": series(n, values)})
    # Two `extreme` occurrences fit in twelve years, so the line sits between
    # the largest and the third - drawn off the sample, not extrapolated past it.
    assert 80.0 <= levels[("src:A", "extreme")] <= 100.0
    # and the rarest tier sits above the commoner one, which is the only
    # ordering the words permit
    assert levels[("src:A", "extreme")] >= levels[("src:A", "major")]
    assert levels[("src:A", "major")] >= levels[("src:A", "noticeable")]


def test_a_tier_too_rare_for_the_record_gets_no_label():
    # One year of history has no answer to "what does once in six years look
    # like", and extrapolating one would make the label a second model with no
    # way to tell whose error was being measured.
    n = int(1.0 * saed_score.SECONDS_PER_YEAR / HOUR)
    levels = saed_score.hindsight_levels({"src:A": series(n)})
    assert ("src:A", "extreme") not in levels
    assert ("src:A", "noticeable") in levels


def test_a_series_shorter_than_the_minimum_is_not_labelled(tmp_path):
    short = pd.DataFrame({"hour_utc": np.arange(10) * HOUR + START,
                          "asset_id": "src:A", "r": np.arange(10) * 0.01})
    short.to_parquet(tmp_path / "a.parquet", index=False)
    loaded = saed_score._load_series(str(tmp_path), "absolute",
                                     pd.Series({"src:A": START}))
    assert loaded == {}


# --- which yardstick each event is judged by ---------------------------------

def test_each_event_is_judged_on_the_quantity_its_own_ladder_scores():
    # The easiest way to produce a confidently meaningless table. An abnormal
    # event's raw move may be small - that is not what it claimed - and an
    # absolute event's residual may be nothing.
    n = 4000
    at = START
    # The instrument had a huge residual and an ordinary raw move at `at`.
    frames = {"abnormal": {"src:A": series(n, background(n, {0: 500.0}))},
              "absolute": {"src:A": series(n, background(n))}}
    out = saed_score.precision(
        events([("src:A", at, "noticeable", "abnormal")]), frames)
    assert out["precision"].iloc[0] == 1.0

    # The same hour called an ABSOLUTE event is judged on the raw move and misses.
    out = saed_score.precision(
        events([("src:A", at, "noticeable", "absolute")]), frames)
    assert out["precision"].iloc[0] == 0.0


def test_an_event_claiming_both_is_a_hit_when_either_holds():
    n = 4000
    frames = {"abnormal": {"src:A": series(n, background(n, {0: 500.0}))},
              "absolute": {"src:A": series(n, background(n))}}
    out = saed_score.precision(events([("src:A", START, "noticeable", "both")]), frames)
    assert out["precision"].iloc[0] == 1.0


def test_an_event_with_no_label_for_its_tier_is_not_counted_either_way():
    # A year of history and an `extreme` claim: the record cannot say whether it
    # was right, and a scorer that guesses is worse than one that abstains.
    n = int(1.0 * saed_score.SECONDS_PER_YEAR / HOUR)
    frames = {"abnormal": {"src:A": series(n)}, "absolute": {"src:A": series(n)}}
    out = saed_score.precision(events([("src:A", START, "extreme", "absolute")]), frames)
    assert out.empty


# --- what was missed ----------------------------------------------------------

def test_one_shock_spanning_several_bars_is_one_episode():
    # Per-hour recall would mostly measure the debounce: the detector reports
    # the peak of a shock and suppresses the bars around it deliberately.
    flags = np.array([False, True, True, True, False, True, False])
    assert saed_score._episodes(flags) == [(1, 4), (5, 6)]


def test_an_episode_is_caught_by_an_event_a_bar_off_its_edge():
    # A shock building over an hour can put the first clearing bar just outside
    # the run the label draws.
    n = 4000
    frames = {"absolute": {"src:A": series(n, background(n, {100: 500.0, 101: 490.0,
                                                            102: 480.0}))},
              "abnormal": {"src:A": series(n, background(n))}}
    # fired one bar before the run starts
    fired = events([("src:A", START + 99 * HOUR, "noticeable", "absolute")])
    out = saed_score.recall(fired, frames)
    every = out[out["label"].eq("every large move") & out["tier"].eq("noticeable")]
    assert every["caught"].iloc[0] >= 1


def test_a_large_move_the_block_explains_is_not_counted_as_an_unexplained_miss():
    # The entire purpose of the system. Twenty members of one complex moving
    # together is one observation; not firing on them is the fault this was
    # built to fix, not a miss.
    n = 4000
    frames = {"absolute": {"src:A": series(n, background(n, {100: 500.0, 101: 490.0,
                                                            102: 480.0}))},
              # residual stays ordinary throughout: the block explained it
              "abnormal": {"src:A": series(n, background(n))}}
    out = saed_score.recall(events([]), frames)
    every = out[out["label"].eq("every large move") & out["tier"].eq("noticeable")]
    unexplained = out[out["label"].eq("large and unexplained")
                      & out["tier"].eq("noticeable")]
    assert every["episodes"].iloc[0] >= 1
    assert unexplained["episodes"].iloc[0] < every["episodes"].iloc[0]


# --- the section as a whole ---------------------------------------------------

def test_a_missing_residuals_directory_costs_the_section_and_not_the_report(tmp_path):
    # It runs inside tremor.evaluate. A directory that is not there must not
    # take down the report that is.
    assert saed_score.build(events_path=str(tmp_path / "nope.parquet"),
                            ladder_path=str(tmp_path / "nope.csv")) == ""


def test_the_section_says_which_detector_it_is_about(tmp_path):
    e = events([("src:A", START + i * HOUR, "noticeable", "absolute")
                for i in range(20)])
    e.to_parquet(tmp_path / "events.parquet", index=False)
    ladder_rows(["src:A"]).to_csv(tmp_path / "ladder.csv", index=False)
    metrics = tmp_path / "metrics"
    metrics.mkdir()
    n = 4000
    pd.DataFrame({"hour_utc": np.arange(n, dtype="int64") * HOUR + START,
                  "asset_id": "src:A",
                  "r": np.sin(np.arange(n) * 0.7)}).to_parquet(
        metrics / "a.parquet", index=False)
    out = saed_score.build(str(tmp_path / "events.parquet"),
                           str(tmp_path / "ladder.csv"),
                           str(tmp_path / "missing_residuals"), str(metrics))
    assert "saed_events.parquet" in out
    assert "delivered to nobody" in out


def test_the_score_columns_match_what_the_detector_actually_reads():
    # If severity or saed changes which quantity a ladder scores, this table
    # silently starts judging every event by the wrong yardstick. Better a
    # failure here than a confident number in the report.
    from tremor import saed

    assert saed_score.LADDER_SCORE["absolute"][0] == saed.ABSOLUTE_COLUMN
    defaults = severity.annotate.__defaults__
    assert saed_score.LADDER_SCORE["abnormal"] == (defaults[0], defaults[3])

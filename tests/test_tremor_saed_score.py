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


# --- how much arrives -------------------------------------------------------

def spans(**years):
    return dict(years)


def volume_events(pairs):
    return pd.DataFrame(pairs, columns=["asset_id", "hour_utc"])


def test_volume_is_reported_per_instrument_year():
    ev = volume_events([("src:A", i) for i in range(20)]
                       + [("src:B", i) for i in range(5)])
    by_block, per_asset = saed_score.volume(
        ev, spans(**{"src:A": 2.0, "src:B": 5.0}),
        {"src:A": "equity", "src:B": "equity"})
    rates = per_asset.set_index("asset_id")["per_year"]
    assert rates["src:A"] == pytest.approx(10.0)
    assert rates["src:B"] == pytest.approx(1.0)


def test_the_block_table_totals_rather_than_averages_away_its_members():
    # The number that matters for an inbox is what a block sends ALTOGETHER, and
    # it is the one that made block size look like the culprit until it was
    # measured: sixteen quiet instruments can still out-send nine loud ones.
    ev = volume_events([("src:A", i) for i in range(20)]
                       + [("src:B", i) for i in range(20)]
                       + [("src:C", i) for i in range(30)])
    by_block, _ = saed_score.volume(
        ev, spans(**{"src:A": 1.0, "src:B": 1.0, "src:C": 1.0}),
        {"src:A": "equity", "src:B": "equity", "src:C": "crypto"})
    row = by_block.set_index("block")
    assert row.loc["equity", "members"] == 2
    assert row.loc["equity", "block_per_year"] == pytest.approx(40.0)
    assert row.loc["crypto", "block_per_year"] == pytest.approx(30.0)
    # equity sends more in total while every crypto asset is louder
    assert row.loc["crypto", "per_asset"] > row.loc["equity", "per_asset"]


def test_an_instrument_with_no_events_is_still_counted_as_quiet():
    # Dropping it would flatter the spread by hiding the quiet end.
    ev = volume_events([("src:A", 1)])
    _, per_asset = saed_score.volume(ev, spans(**{"src:A": 1.0, "src:B": 1.0}),
                                     {"src:A": "equity", "src:B": "equity"})
    assert set(per_asset["asset_id"]) == {"src:A", "src:B"}
    assert per_asset.set_index("asset_id").loc["src:B", "per_year"] == 0.0


def test_nothing_is_reported_without_spans():
    by_block, per_asset = saed_score.volume(volume_events([("src:A", 1)]), {}, {})
    assert by_block.empty and per_asset.empty


# --- the label for what counts as a large hour ------------------------------

def test_the_large_line_is_a_plain_quantile_of_the_instruments_own_hours():
    # Not tied to any rung: the rungs are sizes now and have no rate to match.
    n = 20000
    values = np.concatenate([np.full(n - 10, 1.0), np.linspace(50, 100, 10)])
    level = saed_score.hindsight_levels({"src:A": series(n, values)})["src:A"]
    assert 1.0 < level <= 100.0


def test_a_series_shorter_than_the_minimum_is_not_labelled(tmp_path):
    short = pd.DataFrame({"hour_utc": np.arange(10) * HOUR + START,
                          "asset_id": "src:A", "r": np.arange(10) * 0.01})
    short.to_parquet(tmp_path / "a.parquet", index=False)
    loaded = saed_score._load_series(str(tmp_path), "absolute",
                                     pd.Series({"src:A": START}))
    assert loaded == {}


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
    every = out[out["label"].eq("every large move")]
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
    every = out[out["label"].eq("every large move")]
    unexplained = out[out["label"].eq("large and unexplained")]
    assert every["episodes"].iloc[0] >= 1
    assert unexplained["episodes"].iloc[0] < every["episodes"].iloc[0]


# --- the section as a whole ---------------------------------------------------

def test_a_missing_residuals_directory_costs_the_section_and_not_the_report(tmp_path):
    # This is one section of a report run by hand. A directory that is not
    # there must not take down the sections that are.
    assert saed_score.build(events_path=str(tmp_path / "nope.parquet")) == ""


def test_the_section_says_which_detector_it_is_about(tmp_path):
    e = events([("src:A", START + i * HOUR, "noticeable", "absolute")
                for i in range(20)])
    e.to_parquet(tmp_path / "events.parquet", index=False)
    n = 4000
    hours = np.arange(n, dtype="int64") * HOUR + START
    residuals = tmp_path / "residuals"
    residuals.mkdir()
    pd.DataFrame({"hour_utc": hours, "asset_id": "src:A",
                  "z_resid_bmp": np.sin(np.arange(n) * 0.3),
                  "tier_absolute": pd.array([None] * n, dtype="string"),
                  "tier_abnormal": pd.array([None] * n, dtype="string")}
                 ).to_parquet(residuals / "a.parquet", index=False)
    metrics = tmp_path / "metrics"
    metrics.mkdir()
    pd.DataFrame({"hour_utc": hours, "asset_id": "src:A",
                  "r": np.sin(np.arange(n) * 0.7)}).to_parquet(
        metrics / "a.parquet", index=False)
    out = saed_score.build(str(tmp_path / "events.parquet"),
                           str(residuals), str(metrics))
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

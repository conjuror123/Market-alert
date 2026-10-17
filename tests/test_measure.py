"""tools/measure.py: the dashboard's numbers - windows, counts, the votes, the checks."""
import os

import numpy as np
import pandas as pd
import pytest

from tools import measure

DAY = 86400


def ts(text):
    return int(pd.Timestamp(text, tz="UTC").timestamp())


def _events(rows):
    """(ticker, block, event start, word) -> the event table's columns."""
    return pd.DataFrame([{"ticker": t, "block": b, "event_start": ts(when),
                          "hour_utc": ts(when) - 3600, "word": w,
                          "push": w in ("high", "major", "extreme"), "reading": "hour",
                          "z": 5.0, "r": 0.01} for t, b, when, w in rows])


def test_the_windows_are_the_365_days_to_the_end_and_the_365_before():
    end = ts("2026-10-12 06:00")
    first, second = measure.windows(end)
    assert (first["start"], first["end"]) == (end - 365 * DAY, end)
    assert (second["start"], second["end"]) == (end - 730 * DAY, end - 365 * DAY)
    assert first["name"] == "year to 2026-10-12 06:00" and first["weeks"] == 365 / 7


def test_volume_counts_events_by_when_they_started_a_week_and_the_busiest_week():
    win = measure.windows(ts("2026-10-01"))[0]
    ev = _events([("SPY", "equity", "2026-03-02 15:00", "high"),
                  ("SPY", "equity", "2026-03-03 15:00", "noticeable"),
                  ("BTC", "crypto", "2026-03-04 01:00", "extreme"),
                  ("BTC", "crypto", "2026-04-15 01:00", "major"),
                  ("BTC", "crypto", "2025-09-30 23:00", "extreme"),    # the year before
                  ("BTC", "crypto", "2026-10-01 00:00", "extreme")])   # after the end
    v = measure.volume(ev, win)
    assert (v["pushes"], v["rows"]) == (3, 1)
    assert v["pushes_a_week"] == round(3 / win["weeks"], 2)
    assert v["busiest_week"] == {"pushes": 2, "week_to": "2026-03-06"}
    assert v["by_word"] == {"noticeable": 1, "high": 1, "major": 1, "extreme": 1}
    assert measure.split(ev, win, "block") == {"crypto": {"pushes": 2, "rows": 0},
                                               "equity": {"pushes": 1, "rows": 1}}


def test_instrument_years_count_only_the_history_each_instrument_had():
    cover = [{"first": ts("2026-01-01"), "last": ts("2026-12-31 23:00")},
             {"first": ts("2026-07-02"), "last": ts("2026-12-31 23:00")},
             {"first": None, "last": None}]
    assert measure.instrument_years(cover, 2026, ts("2027-01-01")) == round(
        (365 + 183) / 365.25, 2)
    assert measure.instrument_years(cover, 2026, ts("2026-07-02")) == round(182 / 365.25, 2)


def test_the_archive_counts_each_year_and_marks_the_last_partial():
    ev = _events([("SPY", "equity", "2025-03-02 15:00", "high"),
                  ("SPY", "equity", "2026-03-03 15:00", "noticeable")])
    cover = [{"first": ts("2025-01-01"), "last": ts("2026-06-30 23:00")}]
    years = measure.archive(ev, cover, ts("2026-07-01"))
    assert [(y["year"], y["events"], y["pushes"], y["partial"]) for y in years] == [
        (2025, 1, 1, False), (2026, 1, 0, True)]


def test_the_fetch_line_gives_when_and_how_far_and_the_failures(tmp_path):
    (tmp_path / "failed.txt").write_text("fetched 2026-10-12 06:07 UTC, each instrument's "
                                         "recount window\nSPY yahoo: answered 429\n")
    fetch = measure.read_fetch(str(tmp_path))
    assert fetch == {"at": ts("2026-10-12 06:07"), "span": "each instrument's recount window",
                     "failed": ["SPY yahoo: answered 429"]}


def _eur_store(tmp_path, now, bad_days):
    """Three months of EUR/USD hours to `now`, a bad print (-0.5% for one bar) each
    `bad_days` before it, and the sources' bars, which never went there."""
    from jump import bars
    from jump.basket import load_basket

    asset = next(a for a in load_basket().instruments if a.ticker == "EUR/USD")
    hours = now - 90 * DAY + 3600 * np.arange(90 * 24)
    rng = np.random.default_rng(1)
    closes = 1.10 * np.exp(np.cumsum(rng.normal(0, 0.0003, len(hours))))
    market = pd.DataFrame({"hour_utc": hours, "open": closes, "close": closes.copy()})
    for days in bad_days:
        closes[np.searchsorted(hours, now - days * DAY)] *= 0.995
    frame = pd.DataFrame({"hour_utc": hours, "open": np.r_[closes[0], closes[:-1]],
                          "close": closes, "volume": 0.0, "n_src": 1})
    frame["high"] = frame[["open", "close"]].max(axis=1)
    frame["low"] = frame[["open", "close"]].min(axis=1)
    bars.write(bars.store_path(str(tmp_path / "bars"), asset.file_stem), frame)
    for name in ("yahoo", "marketwatch", "wallstreetcn"):
        (tmp_path / "voters" / name).mkdir(parents=True)
        market.to_parquet(tmp_path / "voters" / name / f"{asset.file_stem}.parquet")
    return asset


def test_a_move_is_voted_only_inside_its_instruments_recount_window(tmp_path):
    # The bot votes EUR/USD's moves for nine days (MarketWatch's reach) and never
    # again: a bad print 20 days back is not voted here either, though every
    # source's bars reach it; one 3 days back is voted not real, with its way back.
    from jump.basket import load_basket

    now = ts("2026-10-01 12:00")
    asset = _eur_store(tmp_path, now, bad_days=(20, 3))
    rows, record = measure.take_votes(load_basket(), str(tmp_path / "bars"),
                                      str(tmp_path / "metrics"), str(tmp_path / "voters"), now)
    assert rows and all(r["ticker"] == "EUR/USD" and r["window_days"] == 9 for r in rows)
    assert all(r["hour"] >= now - 9 * DAY for r in rows)
    bad = now - 3 * DAY
    assert {r["hour"] for r in rows if r["verdict"] == "not_real"} == {bad, bad + 3600}
    assert set(record) == {(asset.asset_id, r["hour"], r["check"]) for r in rows}


def test_the_votes_make_a_record_the_detector_reads(tmp_path):
    from jump import verify
    from jump.basket import load_basket

    spy = next(a for a in load_basket().instruments if a.ticker == "SPY")
    gone = {"hour": ts("2026-03-02 14:00"), "check": verify.CLOSE}
    night = {"hour": ts("2026-03-03 14:00"), "check": verify.CLOSE}
    record = {(spy.asset_id, gone["hour"], gone["check"]): verify._row(
                  spy, gone, verify.NOT_REAL, 0.02, ["yahoo", "sina"], [0.0, None], [], 1),
              (spy.asset_id, night["hour"], night["check"]): verify._row(
                  spy, night, verify.OVERNIGHT, 0.03, ["yahoo", "sina"], [0.025, 0.027],
                  ["yahoo", "sina"], 1)}
    path = str(tmp_path / "votes.csv")
    measure.write_record(record, path)
    assert set(verify.not_real(path)) == {(spy.asset_id, gone["hour"], "close")}
    assert verify.overnight(path) == {(spy.asset_id, night["hour"], "close"):
                                      pytest.approx(0.026)}


WINDOWS = {"SPY": 9.0, "BTC": 29.0, "GLD": 9.0}


def _vote(ticker, block, age, verdict, answers, z=5.0):
    return {"ticker": ticker, "block": block, "hour": 0, "check": "close", "z": z,
            "stored": 0.01, "verdict": verdict, "age_days": age,
            "window_days": WINDOWS[ticker],
            "answers": [{"source": s, "move": m, "saw": saw} for s, m, saw in answers]}


def test_the_votes_are_counted_by_verdict_window_source_and_block():
    votes = [_vote("SPY", "equity", 3, "real", [("yahoo", 0.01, True), ("sina", None, False)]),
             _vote("SPY", "equity", 8, "not_real", [("yahoo", 0.0, False), ("sina", 0.0, False)]),
             _vote("BTC", "crypto", 20, "uncertain", [("kraken", None, False)], z=4.2)]
    t = measure.vote_tables(votes, 4.5, WINDOWS)
    assert t["votes"] == 3 and t["by_verdict"] == {"real": 1, "uncertain": 1, "not_real": 1,
                                                    "overnight": 0}
    assert t["flagged"] == 2 and t["flagged_by_verdict"]["uncertain"] == 0
    assert [(w["days"], w["instruments"], w["votes"]) for w in t["by_window"]] == [
        (9.0, ["GLD", "SPY"], 2), (29.0, ["BTC"], 1)]
    assert t["by_window"][0]["answers_a_vote"] == 1.5 and t["by_window"][1]["answers_a_vote"] == 0
    assert t["by_source"] == {"yahoo": {"asked": 2, "saw": 1, "did_not": 1, "outage": 0},
                              "sina": {"asked": 2, "saw": 0, "did_not": 1, "outage": 1},
                              "kraken": {"asked": 1, "saw": 0, "did_not": 0, "outage": 1}}
    assert t["by_block"]["equity"] == {"real": 1, "uncertain": 0, "not_real": 1, "overnight": 0}


def test_the_votes_effect_is_counted_inside_each_window_and_nothing_else_moves():
    end = ts("2026-10-01")
    detector = _events([("SPY", "equity", "2026-09-28 15:00", "high"),     # inside SPY's 9 days
                        ("SPY", "equity", "2026-09-21 15:00", "high"),     # the day before them
                        ("SPY", "equity", "2026-09-10 15:00", "high"),     # before
                        ("BTC", "crypto", "2026-09-10 01:00", "noticeable")])  # inside BTC's 29
    voted = detector.drop(index=[0]).reset_index(drop=True)                  # voted not real
    e = measure.effect(detector, voted, WINDOWS, end)
    assert e["detector"] == {"pushes": 2, "rows": 1} and e["voted"] == {"pushes": 1, "rows": 1}
    assert e["by_instrument"]["SPY"] == {"window_days": 9.0, "detector": {"pushes": 2, "rows": 0},
                                         "voted": {"pushes": 1, "rows": 0}}
    assert e["outside"]["detector"] == e["outside"]["voted"] == {"pushes": 1, "rows": 0}


def test_the_instruments_the_live_run_does_not_carry_are_fetched_if_their_provider_is_free(
        tmp_path):
    from jump import bars
    from jump.basket import load_basket

    basket = load_basket()
    stem = {a.ticker: a.file_stem for a in basket.instruments}
    for ticker, name in (("^N225", "2026-09.parquet"), ("REMX", "2026-10.open.csv"),
                         ("SPY", "2026-09.parquet")):
        store = bars.store_path(str(tmp_path), stem[ticker])
        os.makedirs(store)
        open(os.path.join(store, name), "w").close()
    out = measure.not_carried(basket, str(tmp_path))
    assert "^N225" in out                       # Yahoo's, no open month: fetched
    assert "REMX" not in out                    # the live run's open month is there
    assert "SPY" not in out and "XAU/USD" not in out   # Tiingo's and SiftingIO's quotas
    # SiftingIO named, for the metals: Tiingo's stays the live run's.
    out = measure.not_carried(basket, str(tmp_path), keyed=("sifting",))
    assert "XAU/USD" in out and "XAG/USD" in out and "SPY" not in out


def test_a_store_behind_the_measurement_is_named():
    end = ts("2026-10-12 09:00")
    cover = [{"ticker": "^N225", "provider": "yahoo", "last": ts("2026-09-30 06:00")},
             {"ticker": "SPY", "provider": "tiingo", "last": ts("2026-10-09 19:00")}]
    assert [b["ticker"] for b in measure.behind(cover, end)] == ["^N225"]


def _result(ev, votes=None):
    wins = measure.windows(ts("2026-10-01"))
    out = {"meta": {"failures": []}, "windows": wins,
           "coverage": [{"ticker": "SPY", "first": 1, "last": 2}],
           "events": len(ev),
           "volume": {w["name"]: measure.volume(ev, w) for w in wins},
           "by_block": {w["name"]: measure.split(ev, w, "block") for w in wins},
           "by_instrument": {w["name"]: measure.split(ev, w, "ticker") for w in wins},
           "archive": measure.archive(ev, [{"first": ts("2024-10-01"), "last": ts("2026-09-30")}],
                                      ts("2026-10-01")),
           "votes": None}
    if votes is not None:
        out["votes"] = {"list": votes, "tables": measure.vote_tables(votes, 4.5, WINDOWS),
                        "effect": measure.effect(ev, ev, WINDOWS, ts("2026-10-01"))}
    return out


def test_the_checks_hold_on_consistent_counts_and_fail_on_a_broken_one():
    ev = _events([("SPY", "equity", "2026-03-02 15:00", "high"),
                  ("BTC", "crypto", "2025-03-04 01:00", "noticeable")])
    votes = [_vote("SPY", "equity", 3, "real", [("yahoo", 0.01, True)])]
    result = _result(ev, votes)
    assert all(c["ok"] for c in measure.checks(result))
    result["by_block"][result["windows"][0]["name"]]["equity"]["pushes"] += 1
    failed = [c["name"] for c in measure.checks(result) if not c["ok"]]
    assert failed == [f"{result['windows'][0]['name']}: pushes of the blocks and "
                      "instruments add up"]
    result = _result(ev, votes + [_vote("SPY", "equity", 20, "real", [("yahoo", 0.01, True)])])
    assert [c["name"] for c in measure.checks(result) if not c["ok"]] == [
        "every vote lies inside its instrument's recount window"]
    result = _result(ev, votes)
    result["votes"]["effect"]["outside"]["voted"]["pushes"] += 1
    assert [c["name"] for c in measure.checks(result) if not c["ok"]] == [
        "the votes change nothing outside their windows"]
    result = _result(ev, votes)
    result["votes"]["tables"]["by_source"]["yahoo"]["saw"] = 0
    assert [c["name"] for c in measure.checks(result) if not c["ok"]] == [
        "each source's answers add up to its asks"]
    result = _result(ev)
    result["coverage"].append({"ticker": "GLD", "first": None, "last": None})
    assert not measure.checks(result)[0]["ok"]


def test_provider_limits_set_each_window_against_what_was_asked(tmp_path):
    # Tiingo's day: the last 30 days, its latest and its peak; SiftingIO's month:
    # the last two months; Tiingo's hour: its shares only (its own 429 guards it).
    from datetime import datetime, timezone

    (tmp_path / "hourly.csv").write_text(
        "day,provider,purpose,calls,left,left_at\n"
        "2026-10-10,tiingo,fetch,120,,\n2026-10-11,tiingo,fetch,110,,\n"
        "2026-10-11,tiingo,votes,15,,\n2026-10-12,tiingo,fetch,40,,\n"
        "2026-09-30,sifting,fetch,300,,\n2026-10-12,sifting,fetch,150,,\n")
    (tmp_path / "manual.csv").write_text(
        "day,provider,purpose,calls,left,left_at\n2026-10-11,tiingo,manual,12,,\n")
    end = int(datetime(2026, 10, 12, 18, tzinfo=timezone.utc).timestamp())
    got = {(x["provider"], x["window"]): x for x in measure.provider_limits(str(tmp_path), end)}
    day = got[("tiingo", "day")]
    assert len(day["series"]) == 30 and day["periods_counted"] == 3
    assert day["latest"] == {"period": "2026-10-12", "calls": 40, "by_purpose": {"fetch": 40}}
    assert day["peak"]["period"] == "2026-10-11" and day["peak"]["calls"] == 137
    assert day["peak"]["by_purpose"] == {"fetch": 110, "manual": 12, "votes": 15}
    month = got[("sifting", "month")]
    assert [(x["period"], x["calls"]) for x in month["series"]] == [("2026-09", 300),
                                                                  ("2026-10", 150)]
    assert got[("tiingo", "hour")]["series"] == [] and got[("tiingo", "hour")]["limit"] == 50

"""The second source (tremor.verify): a move another feed did not see is not
scored, and its message says so. The verdicts are pinned on real cases,
checked by hand against Yahoo.
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from tremor import bars, jumps, verify
from tremor.basket import load_basket

HOUR = 3600


def ts(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


@pytest.fixture(scope="module")
def basket():
    return {a.ticker: a for a in load_basket().instruments}


def bars_of(rows):
    """The second source's hourly bars: {hour text: close} or {hour text: (open, close)}."""
    pairs = {h: (x if isinstance(x, tuple) else (x, x)) for h, x in rows.items()}
    return pd.DataFrame({"hour_utc": [ts(h) for h in pairs],
                         "open": [o for o, _ in pairs.values()],
                         "close": [c for _, c in pairs.values()]})


def judge(hour, prev, price, rows, now, *, check="close", prev_hour=None, from_open=False):
    c = {"hour": ts(hour), "check": check, "prev_close": prev, "price": price,
         "prev_hour": ts(hour) if from_open else ts(prev_hour) if prev_hour else ts(hour) - HOUR,
         "from_open": from_open}
    return verify.judge(c, bars_of(rows), ts(now))


# --- who checks whom ------------------------------------------------------------

def test_every_feed_with_a_free_second_source_is_checked_by_one(basket):
    assert verify.verifier_for(basket["USD/INR"]) == ("yahoo", "USDINR=X", "1h")
    assert verify.verifier_for(basket["USD/BRL"]) == ("yahoo", "USDBRL=X", "1h")
    for fund in (a for a in basket.values() if a.session_template == "us_equity"):
        name, symbol, interval = verify.verifier_for(fund)
        assert name != fund.fetched_from and symbol == fund.ticker and interval == "30min"
    # A coin's price is its exchange's own trades; the futures and the LME's
    # metals have no independent free feed.
    for asset in basket.values():
        if asset.session_template == "crypto_24_7" or asset.session_template == "lme" \
                or asset.session_template.startswith(("ice_", "cme_")):
            assert verify.verifier_for(asset) is None


# --- the verdict ----------------------------------------------------------------

def test_usd_inr_falling_while_yahoo_stood_still_is_unconfirmed():
    # 2024-12-17 09:00: SiftingIO -0.48%, Yahoo +0.03%.
    verdict, move, theirs = judge("2024-12-17 09:00", 84.9198, 84.51605,
                                  {"2024-12-17 08:00": 84.900, "2024-12-17 09:00": 84.9255},
                                  "2025-01-08 00:00")
    assert verdict == verify.UNCONFIRMED
    assert move == pytest.approx(-0.00477, abs=1e-5) and theirs == pytest.approx(0.0003, abs=1e-5)


def test_usd_try_taken_straight_back_but_seen_by_yahoo_is_confirmed():
    # 2025-03-14 20:00: SiftingIO -0.42%, Yahoo -0.58%. A move that reverts in
    # the next hour is not thereby a bad print.
    assert judge("2025-03-14 20:00", 36.6677, 36.5153,
                 {"2025-03-14 19:00": 36.700, "2025-03-14 20:00": 36.487},
                 "2025-03-15 00:00")[0] == verify.CONFIRMED


def test_the_fomc_hour_is_confirmed():
    assert judge("2024-12-18 19:00", 1.04874, 1.03774,
                 {"2024-12-18 18:00": 1.0488, "2024-12-18 19:00": 1.03780},
                 "2025-01-08 00:00")[0] == verify.CONFIRMED


def test_a_steady_offset_between_the_feeds_is_not_a_move():
    # Yahoo quotes 20bp under SiftingIO throughout; both moved 0.5%.
    assert judge("2024-12-17 09:00", 85.00, 85.425,
                 {"2024-12-17 08:00": 84.83, "2024-12-17 09:00": 85.254},
                 "2025-01-08 00:00")[0] == verify.CONFIRMED


def test_a_move_yahoo_prints_an_hour_later_is_confirmed():
    # USD/TRY 2025-03-14: both fell at 10:00; SiftingIO came back at 11:00,
    # Yahoo at 12:00.
    assert judge("2025-03-14 11:00", 36.5640, 36.6741,
                 {"2025-03-14 10:00": 36.5290, "2025-03-14 11:00": 36.5270,
                  "2025-03-14 12:00": 36.6420}, "2025-03-15 00:00")[0] == verify.CONFIRMED


def test_martial_law_in_seoul_is_confirmed_though_yahoo_has_no_bar_that_hour():
    # USD/KRW 2024-12-03 13:00-14:00 UTC: +2.5% in two hours; Yahoo has bars
    # only either side.
    rows = {"2024-12-03 07:00": 1403.10, "2024-12-03 15:00": 1428.64}
    assert judge("2024-12-03 14:00", 1421.00, 1437.45, rows,
                 "2025-01-08 00:00")[0] == verify.CONFIRMED
    assert judge("2024-12-03 13:00", 1402.97, 1421.00, rows,
                 "2025-01-08 00:00")[0] == verify.CONFIRMED


def test_with_no_bar_that_hour_the_bars_either_side_decide():
    # USD/INR 2024-12-17 22:00: stored 85.15; Yahoo 84.882 before, 84.884 after.
    assert judge("2024-12-17 22:00", 84.90735, 85.1495,
                 {"2024-12-17 20:00": 84.882, "2024-12-18 00:00": 84.884},
                 "2025-01-08 00:00")[0] == verify.UNCONFIRMED


def test_no_bar_after_yet_is_pending_then_unknown():
    rows = {"2024-12-17 21:00": 84.882}
    assert judge("2024-12-17 22:00", 84.90735, 85.1495, rows,
                 "2024-12-18 03:00")[0] == verify.PENDING
    # Scored as usual: nothing is taken out on a guess.
    assert judge("2024-12-17 22:00", 84.90735, 85.1495, rows,
                 "2024-12-19 00:00")[0] == verify.UNKNOWN


def test_a_second_source_silent_for_half_a_day_proves_nothing():
    # USD/JPY 2026-05-04 03:00, a Tokyo holiday, Yahoo's last bar the Friday.
    assert judge("2026-05-04 03:00", 157.1334, 156.1290,
                 {"2026-05-01 20:00": 157.20, "2026-05-04 04:00": 156.727},
                 "2026-05-05 00:00")[0] == verify.UNKNOWN


def test_a_bad_opening_print_is_unconfirmed_and_a_real_one_confirmed():
    # IGV on Alpaca's IEX: opened 3% under where the tape opened.
    rows = {"2026-02-03 20:00": 84.200, "2026-02-04 14:00": (84.21, 84.31)}
    assert judge("2026-02-04 14:00", 84.21, 81.67, rows, "2026-02-04 16:00",
                 check="open", prev_hour="2026-02-03 20:00")[0] == verify.UNCONFIRMED
    rows["2026-02-04 14:00"] = (81.70, 82.0)
    assert judge("2026-02-04 14:00", 84.21, 81.67, rows, "2026-02-04 16:00",
                 check="open", prev_hour="2026-02-03 20:00")[0] == verify.CONFIRMED


def test_an_hour_measured_from_its_own_open_is_judged_from_the_second_sources_open():
    # A fund's first hour: the detector reads open to close, so the night's
    # gap is not part of it.
    rows = {"2026-02-03 20:00": 84.20, "2026-02-04 14:00": (82.00, 82.85)}
    assert judge("2026-02-04 14:00", 82.0, 82.9, rows, "2026-02-04 16:00",
                 from_open=True)[0] == verify.CONFIRMED
    rows["2026-02-04 14:00"] = (82.00, 82.02)
    assert judge("2026-02-04 14:00", 82.0, 82.9, rows, "2026-02-04 16:00",
                 from_open=True)[0] == verify.UNCONFIRMED
    # No open of its own on the second source: nothing to compare with.
    assert judge("2026-02-04 14:00", 82.0, 82.9, {"2026-02-03 20:00": 84.2},
                 "2026-02-04 16:00", from_open=True)[0] == verify.UNKNOWN


def test_the_move_reported_is_the_second_sources_over_the_same_hours():
    # Within the lag window Yahoo's best move is its 08:00-10:00 one; the
    # message reports 08:00 to 09:00, the hours the stored move spans.
    verdict, _, theirs = judge("2024-12-17 09:00", 84.9198, 84.51605,
                               {"2024-12-17 07:00": 84.95, "2024-12-17 08:00": 84.900,
                                "2024-12-17 09:00": 84.9255, "2024-12-17 10:00": 84.95},
                               "2025-01-08 00:00")
    assert verdict == verify.UNCONFIRMED
    assert theirs == pytest.approx(np.log(84.9255 / 84.900))


# --- which bars -------------------------------------------------------------------

def _fund_store(basket, days=120, gap_day=None, gap=0.0, first_hour=0.0):
    """A Yahoo-served fund's bars, quiet: 0.1% hours, 0.3% nights."""
    asset = next(a for a in basket.values()
                 if a.session_template == "us_equity" and a.fetched_from == "alpaca")
    from tremor import sessions
    table = sessions.load_sessions()
    rng = np.random.default_rng(7)
    rows, price = [], 100.0
    for day in sorted(d for d in table if ts("2026-05-01 00:00") <= ts(f"{d} 00:00")
                      <= ts("2026-09-30 00:00"))[-days:]:
        hours = sessions.session_hours(table[day])
        night = gap if day == gap_day else rng.normal(0, 0.003)
        price *= np.exp(night)
        for k, h in enumerate(hours):
            o = price
            move = first_hour if (day == gap_day and k == 0) else rng.normal(0, 0.001)
            price *= np.exp(move)
            rows.append({"hour_utc": h, "open": o, "high": max(o, price), "low": min(o, price),
                         "close": price, "volume": 1000.0, "n_src": 1})
    return asset, table, pd.DataFrame(rows)


def test_an_ordinary_night_is_not_asked_about_and_a_far_one_is(basket):
    from datetime import date
    day = date(2026, 9, 29)
    asset, table, frame = _fund_store(basket)
    now = ts("2026-09-29 20:05")
    assert [c for c in verify.candidates(asset, frame, table, now) if c["check"] == "open"] == []
    asset, table, frame = _fund_store(basket, gap_day=day, gap=0.03)
    found = verify.candidates(asset, frame, table, now)
    assert [c["check"] for c in found] == ["open"]
    # The quiet first hour after it is not a far move: the detector reads it
    # open to close.
    asset, table, frame = _fund_store(basket, gap_day=day, gap=0.0, first_hour=0.02)
    hour = [c for c in verify.candidates(asset, frame, table, now) if c["check"] == "close"]
    assert len(hour) == 1 and hour[0]["from_open"] and hour[0]["prev_hour"] == hour[0]["hour"]


# --- a pass, and what it leaves out -----------------------------------------------

def test_a_pass_records_the_verdict_and_asks_once(monkeypatch, tmp_path, basket):
    asset = basket["USD/INR"]
    hours = ts("2026-09-28 00:00") + HOUR * np.arange(80)        # Monday on
    rng = np.random.default_rng(1)
    closes = 84.0 * np.exp(np.cumsum(rng.normal(0, 0.0003, 80)))
    closes[70] *= 0.995                                            # a bad print
    frame = pd.DataFrame({"hour_utc": hours, "open": np.r_[closes[0], closes[:-1]],
                          "close": closes, "volume": 0.0, "n_src": 1})
    frame["high"] = frame[["open", "close"]].max(axis=1)
    frame["low"] = frame[["open", "close"]].min(axis=1)
    bars.write(bars.store_path(str(tmp_path / "bars"), asset.file_stem), frame)
    market = pd.DataFrame({"hour_utc": hours, "open": closes, "close": closes})
    market.loc[70, "close"] = closes[70] / 0.995                   # the tape never went there
    asked = []

    def fetch(name, symbol, interval, days, session, now):
        asked.append((name, symbol))
        return market

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    now = datetime.fromtimestamp(int(hours[75]) + 300, timezone.utc)
    path = str(tmp_path / "verified.csv")
    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
    assert asked == [("yahoo", "USDINR=X")]
    # The bad print, and the hour back from it, which did not happen either.
    assert set(verify.unconfirmed(path)) == {(asset.asset_id, int(hours[70]), "close"),
                                             (asset.asset_id, int(hours[71]), "close")}
    asked.clear()
    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
    assert asked == []
    # The bar heals - a run that lost its fetch had judged the :05 snapshot -
    # into no far move at all: the verdict no longer applies, and it is scored.
    frame.loc[70, "close"] = closes[70] / 0.995
    frame.loc[71, "open"] = frame.loc[70, "close"]
    bars.write(bars.store_path(str(tmp_path / "bars"), asset.file_stem), frame)
    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
    assert verify.unconfirmed(path) == {}


def test_a_healed_bar_still_far_is_asked_again(monkeypatch, tmp_path, basket):
    c = {"hour": 100 * HOUR, "check": "close", "prev_hour": 99 * HOUR, "prev_close": 84.0,
         "price": 83.5, "from_open": False}
    record = {("x", c["hour"], "close"): {"verdict": verify.UNCONFIRMED,
                                          "prices": verify.prices(c)}}
    assert record[("x", c["hour"], "close")]["prices"] == verify.prices(dict(c))
    assert verify.prices(dict(c, price=83.4)) != verify.prices(c)


def test_an_unconfirmed_verdict_outlives_the_rest_of_the_record(tmp_path):
    path = str(tmp_path / "verified.csv")
    old, now = ts("2025-01-01 00:00"), ts("2026-10-01 00:00")
    rows = {(name, old, "close"): {"asset_id": name, "hour_utc": old, "check": "close",
                                   "verdict": verdict}
            for name, verdict in (("a", verify.UNCONFIRMED), ("b", verify.CONFIRMED),
                                  ("c", verify.PENDING), ("d", verify.UNKNOWN))}
    verify.write(rows, now, path)
    # Pending too goes: a bar older than its 24 hours is never asked again.
    assert set(verify.load(path)) == {("a", old, "close")}


def test_an_unconfirmed_move_is_not_scored_and_not_in_the_yardstick():
    start = ts("2026-01-05 00:00")
    rng = np.random.default_rng(3)
    r = rng.normal(0, 0.001, 3000)
    r[2000] = 0.02                                     # 20 sigma
    metrics = pd.DataFrame({"hour_utc": start + HOUR * np.arange(3000), "r": r,
                            "gap": np.nan, "hole": False})
    doubts = {("x:Y", int(metrics["hour_utc"][2000]), "close"): {}}
    kept = jumps.without_unconfirmed(metrics, "x:Y", doubts)
    assert np.isnan(kept["r"][2000]) and np.isfinite(metrics["r"][2000])
    scored = jumps.score(kept, "crypto_24_7")
    assert pd.isna(scored["word"][2000])
    clean = jumps.score(metrics.assign(r=np.where(np.arange(3000) == 2000, np.nan, r)),
                        "crypto_24_7")
    assert np.allclose(scored["sigma"], clean["sigma"], equal_nan=True)
    # Another instrument's verdict leaves this one alone.
    assert jumps.without_unconfirmed(metrics, "x:Z", doubts) is metrics

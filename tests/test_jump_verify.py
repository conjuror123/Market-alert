"""The second source (jump.verify): a move another feed did not see is not
scored, and its message says so. The verdicts are pinned on real cases,
checked by hand against Yahoo.
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from jump import bars, jumps, verify
from jump.basket import load_basket

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
    # The pairs: Yahoo, and MarketWatch for the hours Yahoo lacks.
    assert verify.verifiers_for(basket["USD/INR"]) == [
        ("yahoo", "USDINR=X", "1h"), ("marketwatch", "CURRENCY/US/XTUP/USDINR", "1h")]
    assert verify.verifiers_for(basket["USD/BRL"])[1] == (
        "marketwatch", "CURRENCY/US/XTUP/USDBRL", "1h")
    for fund in (a for a in basket.values() if a.session_template == "us_equity"):
        [(name, symbol, interval)] = verify.verifiers_for(fund)
        assert name != fund.fetched_from and symbol == fund.ticker and interval == "30min"
    # The softs Yahoo serves: Sina's global futures, hourly.
    assert verify.verifiers_for(basket["KC=F"]) == [("sina", "KC", "1h")]
    assert verify.verifiers_for(basket["CC=F"]) == [("sina", "CC", "1h")]
    assert verify.verifiers_for(basket["CT=F"]) == [("sina", "CT", "1h")]
    # Live cattle: MarketWatch's continuous contract.
    assert verify.verifiers_for(basket["LE=F"]) == [
        ("marketwatch", "FUTURE/US/XCME/LC00", "1h")]
    # A coin's price is its exchange's own trades; the LME's metals have no
    # independent free feed found.
    for asset in basket.values():
        if asset.session_template in ("crypto_24_7", "lme"):
            assert verify.verifiers_for(asset) == []


def test_sinas_global_futures_and_us_funds_are_different_feeds(monkeypatch):
    from price_monitor import sina
    calls = []
    monkeypatch.setattr(sina, "fetch_bars",
                        lambda symbol, session, now, url: calls.append((symbol, url)) or [])
    monkeypatch.setattr(sina, "fetch_us_bars",
                        lambda symbol, session, now: calls.append((symbol, "us")) or [])
    now = datetime(2026, 10, 5, 15, 5, tzinfo=timezone.utc)
    verify.fetch_verifier("sina", "KC", "1h", 2, None, now)
    verify.fetch_verifier("sina", "SLQD", "30min", 2, None, now)
    assert calls == [("KC", sina.GLOBAL_URL), ("SLQD", "us")]


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
    from jump import sessions
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

    from jump.corporate_actions import Dividends
    day = date(2026, 9, 29)
    asset, table, frame = _fund_store(basket)
    # The gap is the pipeline's: scored only on dates the fund's payouts are
    # confirmed through, as the detector scores it.
    paid = Dividends(steps={}, splits={}, checked_through={asset.ticker: "2026-12-31"})
    now = ts("2026-09-29 20:05")

    def asked(frame):
        return verify.candidates(asset, frame, table, now, basket=load_basket(), dividends=paid)

    assert [c for c in asked(frame) if c["check"] == "open"] == []
    asset, table, frame = _fund_store(basket, gap_day=day, gap=0.03)
    assert [c["check"] for c in asked(frame)] == ["open"]
    # The quiet first hour after it is not a far move: the detector reads it
    # open to close. A far first hour is, from its own open.
    asset, table, frame = _fund_store(basket, gap_day=day, gap=0.0, first_hour=0.02)
    hour = [c for c in asked(frame) if c["check"] == "close"]
    assert len(hour) == 1 and hour[0]["from_open"] and hour[0]["prev_hour"] == hour[0]["hour"]


def test_a_pairs_weekend_gap_is_asked_about_at_its_open_and_from_fridays_close(
        monkeypatch, tmp_path, basket):
    # The detector judges a pair's weekend gap at its open (jumps.found_times),
    # so it is asked about then - and the second source is fetched from before
    # Friday's close, the price the gap starts at.
    asset = basket["USD/INR"]
    hours = ts("2026-08-03 00:00") + HOUR * np.arange(24 * 7 * 9)
    rng = np.random.default_rng(2)
    closes = 84.0 * np.exp(np.cumsum(rng.normal(0, 0.0003, len(hours))))
    reopen = int(np.flatnonzero(hours == ts("2026-10-04 21:00"))[0])
    closes[reopen:] *= 1.02                                       # a 2% weekend gap
    frame = pd.DataFrame({"hour_utc": hours, "open": closes, "close": closes,
                          "volume": 0.0, "n_src": 1})
    frame.loc[reopen + 1:, "open"] = closes[reopen:-1]
    frame["high"] = frame[["open", "close"]].max(axis=1)
    frame["low"] = frame[["open", "close"]].min(axis=1)
    frame = frame[frame["hour_utc"] <= ts("2026-10-04 21:00")]    # the run at 21:05
    bars.write(bars.store_path(str(tmp_path / "bars"), asset.file_stem), frame)
    spans = []

    def fetch(name, symbol, interval, days, session, now):
        spans.append(days)
        return pd.DataFrame({"hour_utc": frame["hour_utc"], "open": frame["open"],
                             "close": frame["close"]})

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    now = datetime.fromtimestamp(ts("2026-10-04 21:05"), timezone.utc)
    path = str(tmp_path / "verified.csv")
    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
    record = verify.load(path)
    gap = record[(asset.asset_id, ts("2026-10-04 21:00"), "open")]
    assert gap["verdict"] == verify.CONFIRMED
    friday = int(frame["hour_utc"][frame["hour_utc"] < ts("2026-10-03 00:00")].max())
    assert spans[0] * 86400 >= ts("2026-10-04 21:05") - friday


# --- a pass, and what it leaves out -----------------------------------------------

def _inr_store(tmp_path, basket, bad_share=0.995):
    """Two months of USD/INR hours to a bad print's week - enough for the
    detector's smallest window; the gate drops the weekends - and a tape that
    never went there."""
    asset = basket["USD/INR"]
    hours = ts("2026-08-03 00:00") + HOUR * np.arange(24 * 7 * 8 + 80)
    rng = np.random.default_rng(1)
    closes = 84.0 * np.exp(np.cumsum(rng.normal(0, 0.0003, len(hours))))
    bad = len(hours) - 10                                    # a Wednesday, 22:00
    market = pd.DataFrame({"hour_utc": hours, "open": closes, "close": closes.copy()})
    closes[bad] *= bad_share
    frame = pd.DataFrame({"hour_utc": hours, "open": np.r_[closes[0], closes[:-1]],
                          "close": closes, "volume": 0.0, "n_src": 1})
    frame["high"] = frame[["open", "close"]].max(axis=1)
    frame["low"] = frame[["open", "close"]].min(axis=1)
    bars.write(bars.store_path(str(tmp_path / "bars"), asset.file_stem), frame)
    return asset, hours, bad, frame, market


def test_a_pass_judges_every_reading_in_its_day_again_each_run(monkeypatch, tmp_path, basket):
    asset, hours, bad, frame, market = _inr_store(tmp_path, basket)
    asked = []

    def fetch(name, symbol, interval, days, session, now):
        asked.append((name, symbol))
        return market

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    now = datetime.fromtimestamp(int(hours[bad + 5]) + 300, timezone.utc)
    path = str(tmp_path / "verified.csv")
    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
    assert asked == [("yahoo", "USDINR=X"), ("marketwatch", "CURRENCY/US/XTUP/USDINR")]
    # The bad print, and the hour back from it, which did not happen either.
    unseen = {(asset.asset_id, int(hours[bad]), "close"),
              (asset.asset_id, int(hours[bad + 1]), "close")}
    assert set(verify.unconfirmed(path)) == unseen
    # Asked again next run - and the same answer.
    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
    assert len(asked) == 4 and set(verify.unconfirmed(path)) == unseen
    # The bar heals into no far move (a run that lost its fetch had judged the
    # :05 snapshot): its verdict no longer applies, and it is scored.
    frame.loc[bad, "close"] = market.loc[bad, "close"]
    frame.loc[bad + 1, "open"] = frame.loc[bad, "close"]
    frame["high"] = frame[["open", "close"]].max(axis=1)
    frame["low"] = frame[["open", "close"]].min(axis=1)
    bars.write(bars.store_path(str(tmp_path / "bars"), asset.file_stem), frame)
    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
    assert verify.unconfirmed(path) == {}


def test_not_seen_waits_for_the_second_sources_next_bar(monkeypatch, tmp_path, basket):
    # USD/TRY 2025-03-14: SiftingIO came back at 11:00, Yahoo only at 12:00.
    rows = {"2025-03-14 10:00": 36.5290, "2025-03-14 11:00": 36.5270}
    assert judge("2025-03-14 11:00", 36.5640, 36.6741, rows,
                 "2025-03-14 12:05")[0] == verify.PENDING
    rows["2025-03-14 12:00"] = 36.6420
    assert judge("2025-03-14 11:00", 36.5640, 36.6741, rows,
                 "2025-03-14 13:05")[0] == verify.CONFIRMED
    # Had Yahoo stayed put, not seen - once its 12:00 bar has ended.
    rows["2025-03-14 12:00"] = 36.5280
    assert judge("2025-03-14 11:00", 36.5640, 36.6741, rows,
                 "2025-03-14 13:05")[0] == verify.UNCONFIRMED


def test_a_busy_hour_asks_the_not_yet_judged_first(monkeypatch, tmp_path, basket):
    asset, hours, bad, frame, market = _inr_store(tmp_path, basket)
    other = basket["USD/TRY"]
    bars.write(bars.store_path(str(tmp_path / "bars"), other.file_stem), frame)
    asked = []

    def fetch(name, symbol, interval, days, session, now):
        asked.append(symbol)
        return market

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    monkeypatch.setattr(verify, "MAX_REQUESTS", 1)
    now = datetime.fromtimestamp(int(hours[bad + 5]) + 300, timezone.utc)
    path = str(tmp_path / "verified.csv")
    verify.verify([asset, other], str(tmp_path / "bars"), None, now=now, path=path)
    verify.verify([asset, other], str(tmp_path / "bars"), None, now=now, path=path)
    # USD/INR comes first in the basket and was judged the first run: the
    # second run's one request goes to USD/TRY, which was not.
    assert asked == ["USDINR=X", "USDTRY=X"]


def test_two_sources_combine_any_seen_confirms():
    C, U, P, K = verify.CONFIRMED, verify.UNCONFIRMED, verify.PENDING, verify.UNKNOWN
    one = lambda name, verdict, move: (name, verdict, 0.005, move)
    assert verify.combine([one("yahoo", U, 0.0), one("marketwatch", C, 0.004)])[:3] == (
        C, 0.005, ["marketwatch"])
    # Still waiting on one of them: not a verdict yet.
    assert verify.combine([one("yahoo", P, 0.0), one("marketwatch", U, 0.0)])[0] == P
    verdict, _, names, moves = verify.combine([one("yahoo", U, 0.0003),
                                              one("marketwatch", U, 0.0001)])
    assert (verdict, names, moves) == (U, ["yahoo", "marketwatch"], [0.0003, 0.0001])
    # One with no bars around the move (unknown) and one that did not see it.
    assert verify.combine([one("yahoo", K, 0.0), one("marketwatch", U, 0.0)])[0] == U
    assert verify.combine([one("yahoo", K, 0.0)])[0] == K
    assert verify.combine([])[0] == K


def test_a_source_with_bars_around_the_move_outweighs_one_bridging_a_gap():
    # USD/INR's bad print at 22:00: Yahoo's last bar is 10:00, MarketWatch has
    # every hour and stayed flat. Yahoo would wait for its 03:00 bar and then
    # bridge seventeen hours; MarketWatch's own hours decide, at once.
    def bars_(hours, price=96.1):
        return pd.DataFrame({"hour_utc": [ts(h) for h in hours], "open": price, "close": price})
    c = {"hour": ts("2026-09-30 22:00"), "check": "close", "from_open": False,
         "prev_hour": ts("2026-09-30 21:00"), "prev_close": 96.10, "price": 96.40}
    yahoo = bars_(["2026-09-30 10:00"])
    mw = bars_([f"2026-09-30 {h}:00" for h in range(19, 24)])
    now = ts("2026-10-01 00:05")
    assert verify.judge_all(c, [("yahoo", yahoo), ("marketwatch", mw)], now)[0] == \
        verify.UNCONFIRMED
    # Yahoo's 03:00 bar, 0.3% up after the night's drift, does not turn it.
    yahoo = bars_(["2026-09-30 10:00"]).pipe(
        lambda f: pd.concat([f, bars_(["2026-10-01 03:00"], 96.40)], ignore_index=True))
    assert verify.judge_all(c, [("yahoo", yahoo), ("marketwatch", mw)],
                            ts("2026-10-01 03:05"))[0] == verify.UNCONFIRMED
    # With nobody's bars around the move, the bridge is all there is.
    assert verify.judge_all(c, [("yahoo", yahoo)], ts("2026-10-01 03:05"))[0] == \
        verify.CONFIRMED


def test_one_source_failing_leaves_the_other_to_answer(monkeypatch, tmp_path, basket):
    from price_monitor.models import ExchangeError
    asset, hours, bad, frame, market = _inr_store(tmp_path, basket)

    def fetch(name, symbol, interval, days, session, now):
        if name == "yahoo":
            raise ExchangeError("USDINR=X: no response")
        return market

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    now = datetime.fromtimestamp(int(hours[bad + 5]) + 300, timezone.utc)
    path = str(tmp_path / "verified.csv")
    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
    row = verify.unconfirmed(path)[(asset.asset_id, int(hours[bad]), "close")]
    assert row["verifier"] == "marketwatch"


def test_a_rate_limit_stops_that_source_only(monkeypatch, tmp_path, basket):
    from price_monitor.models import ExchangeError
    asset, hours, bad, frame, market = _inr_store(tmp_path, basket)
    other = basket["USD/TRY"]
    bars.write(bars.store_path(str(tmp_path / "bars"), other.file_stem), frame)
    asked = []

    def fetch(name, symbol, interval, days, session, now):
        asked.append(name)
        if name == "marketwatch":
            raise ExchangeError(f"{symbol}: MarketWatch answered 429")
        return market

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    now = datetime.fromtimestamp(int(hours[bad + 5]) + 300, timezone.utc)
    verify.verify([asset, other], str(tmp_path / "bars"), None, now=now,
                  path=str(tmp_path / "verified.csv"))
    assert asked == ["yahoo", "marketwatch", "yahoo"]


def test_a_source_that_does_not_answer_twice_in_a_row_is_stopped(monkeypatch, tmp_path,
                                                                  basket):
    from price_monitor.models import Unreachable
    asset, hours, bad, frame, market = _inr_store(tmp_path, basket)
    others = [basket["USD/TRY"], basket["USD/MXN"]]
    for other in others:
        bars.write(bars.store_path(str(tmp_path / "bars"), other.file_stem), frame)
    asked = []

    def fetch(name, symbol, interval, days, session, now):
        asked.append(name)
        if name == "marketwatch":
            raise Unreachable(f"{symbol}: Read timed out")
        return market

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    now = datetime.fromtimestamp(int(hours[bad + 5]) + 300, timezone.utc)
    r = verify.verify([asset, *others], str(tmp_path / "bars"), None, now=now,
                      path=str(tmp_path / "verified.csv"))
    assert asked == ["yahoo", "marketwatch", "yahoo", "marketwatch", "yahoo"]
    # Said, so the health chat can name it.
    assert r["stopped"] == ["marketwatch"]


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


def test_the_close_check_follows_the_price_as_stored(tmp_path, basket):
    # An unconfirmed move is not a reading, but the price did what the store
    # says; leaving it out of the path would misstate how much of a real move
    # held at the close.
    asset = basket["BTC/USDT"] if "BTC/USDT" in basket else next(
        a for a in basket.values() if a.session_template == "crypto_24_7")
    start = ts("2026-03-02 00:00")
    rng = np.random.default_rng(5)
    r = rng.normal(0, 0.001, 24 * 200)
    hours = start + HOUR * np.arange(len(r))
    k = len(r) - 30
    r[k] = 0.03                                         # a real 30-sigma hour
    r[k + 2] = 0.01                                     # then an unconfirmed step
    metrics = pd.DataFrame({"hour_utc": hours, "r": r, "hole": np.nan, "gap": np.nan})
    os_dir = tmp_path / "metrics"
    os_dir.mkdir()
    metrics.to_parquet(os_dir / f"{asset.file_stem}.parquet")
    path = str(tmp_path / "verified.csv")
    verify.write({(asset.asset_id, int(hours[k + 2]), "close"): {
        "asset_id": asset.asset_id, "hour_utc": int(hours[k + 2]), "check": "close",
        "verdict": verify.UNCONFIRMED}}, int(hours[-1]), path)
    flagged = jumps.run(str(os_dir), now=int(hours[-1]) + 3600, verified_path=path)
    mine = flagged[flagged["asset_id"] == asset.asset_id]
    assert int(hours[k + 2]) not in set(mine["hour_utc"])
    held = mine.loc[mine["hour_utc"] == int(hours[k]), "held"].iloc[0]
    from jump import routing
    close = routing.next_close(int(hours[k]) + 3600)
    upto = np.searchsorted(hours + 3600, close, side="right") - 1
    assert held == pytest.approx(np.sum(r[k:upto + 1]) / r[k])


def test_the_check_follows_the_detectors_settings(monkeypatch, basket):
    # basket.yaml's detector: window_days and levels are the detector's; the
    # check asks about everything the detector could flag under them.
    from datetime import date

    from jump.corporate_actions import Dividends
    day = date(2026, 9, 29)
    asset, table, frame = _fund_store(basket, gap_day=day, first_hour=0.0035)
    paid = Dividends(steps={}, splits={}, checked_through={asset.ticker: "2026-12-31"})
    now = ts("2026-09-29 20:05")

    def asked():
        return [c for c in verify.candidates(asset, frame, table, now, basket=load_basket(),
                                             dividends=paid) if c["check"] == "close"]

    assert asked() == []                    # about 3.5 sigma: below the 4-sigma line
    monkeypatch.setattr(jumps, "settings", lambda *a, **k: (182.6, (3.0, 8.5, 12.0, 17.0)))
    assert len(asked()) == 1                # a bottom of 3 is flagged, so it is asked about

    windows = []
    score = jumps.score
    monkeypatch.setattr(jumps, "score", lambda f, t, w=jumps.WINDOW_DAYS, l=jumps.LEVELS:
                        windows.append(w) or score(f, t, w, l))
    monkeypatch.setattr(jumps, "settings", lambda *a, **k: (365.0, jumps.LEVELS))
    asked()
    assert windows == [365.0]
    assert verify.tail_days(365.0) >= 365.0


def test_a_broken_bar_from_a_second_source_costs_only_its_instrument(
        monkeypatch, tmp_path, basket):
    # A second source's zero or missing price is not a price. It must not take
    # the pass down with it: every other instrument is still judged.
    asset, hours, bad, frame, market = _inr_store(tmp_path, basket)
    other = basket["USD/TRY"]
    bars.write(bars.store_path(str(tmp_path / "bars"), other.file_stem), frame)
    broken = market.copy()
    broken.loc[bad - 1, ["open", "close"]] = 0.0

    def fetch(name, symbol, interval, days, session, now):
        return broken if "INR" in symbol else market

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    now = datetime.fromtimestamp(int(hours[bad + 5]) + 300, timezone.utc)
    path = str(tmp_path / "verified.csv")
    verify.verify([asset, other], str(tmp_path / "bars"), None, now=now, path=path)
    doubted = verify.unconfirmed(path)
    assert (other.asset_id, int(hours[bad]), "close") in doubted
    # The broken bar is left out and the rest of the source's hours decide.
    assert (asset.asset_id, int(hours[bad]), "close") in doubted

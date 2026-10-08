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

def _asked(asset):
    return [(src.name, src.symbol(asset), src.interval) for src in verify.sources_for(asset)]


def test_every_instrument_is_asked_of_every_source_of_its_class_but_its_own(basket):
    # The pairs: Yahoo, and MarketWatch for the hours Yahoo lacks.
    assert _asked(basket["USD/INR"]) == [
        ("yahoo", "USDINR=X", "1h"), ("marketwatch", "CURRENCY/US/XTUP/USDINR", "1h")]
    assert _asked(basket["USD/BRL"])[1] == ("marketwatch", "CURRENCY/US/XTUP/USDBRL", "1h")
    # A fund: Yahoo's, Sina's and MarketWatch's bars, less its own provider.
    for fund in (a for a in basket.values() if a.session_template == "us_equity"):
        names = [n for n, _, _ in _asked(fund)]
        assert names == [n for n in ("yahoo", "sina", "marketwatch") if n != fund.fetched_from]
    assert _asked(basket["LMBS"]) == [("yahoo", "LMBS", "30min"), ("sina", "LMBS", "30min"),
                                      ("marketwatch", "FUND/US/XNAS/LMBS", "1h")]
    # The softs Yahoo serves: Sina's global futures, hourly, with their own rolls.
    assert _asked(basket["KC=F"]) == [("sina", "KC", "1h")]
    assert _asked(basket["CT=F"]) == [("sina", "CT", "1h")]
    assert verify.sources_for(basket["CC=F"])[0].own_rolls
    # Live cattle: MarketWatch's continuous contract.
    assert _asked(basket["LE=F"]) == [("marketwatch", "FUTURE/US/XCME/LC00", "1h")]
    # The coins: two other exchanges' dollar pairs, Kraken naming BTC and
    # DOGE its own way.
    assert _asked(basket["BTC/USDT"]) == [("coinbase", "BTC-USD", "1h"), ("kraken", "XBTUSD", "1h")]
    assert _asked(basket["DOGE/USDT"])[1] == ("kraken", "XDGUSD", "1h")
    for asset in basket.values():
        if asset.session_template == "crypto_24_7":
            assert [n for n, _, _ in _asked(asset)] == ["coinbase", "kraken"]
    # The LME's metals have no independent free feed found.
    for asset in basket.values():
        if asset.session_template == "lme":
            assert verify.sources_for(asset) == []


def test_no_source_spends_an_allowance_the_live_run_needs():
    names = {src.name for group in verify.SOURCES.values() for src in group}
    assert not names & {"tiingo", "sifting", "twelvedata", "google"}


def test_a_wick_on_binance_alone_is_unconfirmed():
    # FIL 2025-10-10 21:00, the night of the liquidations: Binance closed the
    # hour 29% down (low 0.32 from 2.09); Coinbase fell 2% that hour.
    assert judge("2025-10-10 21:00", 2.094, 1.561,
                 {"2025-10-10 19:00": 2.212, "2025-10-10 20:00": 2.107,
                  "2025-10-10 21:00": 2.064, "2025-10-10 22:00": 2.076},
                 "2025-10-11 06:00")[0] == verify.UNCONFIRMED
    # The same night's real fall, seen on both: BTC-like -7% against -6.8%.
    assert judge("2025-10-10 21:00", 100.0, 93.0,
                 {"2025-10-10 20:00": 100.0, "2025-10-10 21:00": 93.2,
                  "2025-10-10 22:00": 93.5},
                 "2025-10-11 06:00")[0] == verify.CONFIRMED


def test_the_coins_sources_give_only_hours_that_have_ended(monkeypatch):
    from price_monitor import coinbase, kraken
    from price_monitor.models import Candle

    asked = []

    def fake(symbol, start, end, session=None):
        asked.append((symbol, start, end))
        return [Candle(open_time=int(end.timestamp()) - 2 * HOUR, open=1, high=1, low=1,
                       close=1, volume=1, close_time=int(end.timestamp()) - HOUR)]

    monkeypatch.setattr(coinbase, "fetch_history", fake)
    monkeypatch.setattr(kraken, "fetch_history", fake)
    now = datetime(2026, 10, 6, 20, 5, tzinfo=timezone.utc)
    out = verify.fetch_verifier("coinbase", "BTC-USD", "1h", 2, None, now)
    verify.fetch_verifier("kraken", "XBTUSD", "1h", 2, None, now)
    assert [a[2] for a in asked] == [datetime(2026, 10, 6, 20, tzinfo=timezone.utc)] * 2
    assert list(out["hour_utc"]) == [ts("2026-10-06 18:00")]


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
    assert gap["verdict"] == verify.REAL
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
    assert set(verify.not_real(path)) == unseen
    # Asked again next run - and the same answer.
    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
    assert len(asked) == 4 and set(verify.not_real(path)) == unseen
    # The bar heals into no far move (a run that lost its fetch had judged the
    # :05 snapshot): its verdict no longer applies, and it is scored.
    frame.loc[bad, "close"] = market.loc[bad, "close"]
    frame.loc[bad + 1, "open"] = frame.loc[bad, "close"]
    frame["high"] = frame[["open", "close"]].max(axis=1)
    frame["low"] = frame[["open", "close"]].min(axis=1)
    bars.write(bars.store_path(str(tmp_path / "bars"), asset.file_stem), frame)
    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
    assert verify.not_real(path) == {}


def test_a_vote_is_counted_again_once_a_day_while_its_closest_source_serves_it(
        monkeypatch, tmp_path, basket):
    # USD/INR's closest source is MarketWatch, nine days deep: the bad print
    # is counted again once a day until then, and its vote stands after.
    asset, hours, bad, frame, market = _inr_store(tmp_path, basket)
    asked = []

    def fetch(name, symbol, interval, days, session, now):
        asked.append(name)
        return market

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    path = str(tmp_path / "verified.csv")
    key = (asset.asset_id, int(hours[bad]), "close")

    def run(after_hours):
        asked.clear()
        now = datetime.fromtimestamp(int(hours[bad]) + after_hours * HOUR + 300, timezone.utc)
        verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
        return list(asked)

    assert verify.recount_days(asset) == 9
    assert run(72) == ["yahoo", "marketwatch"]               # three days on: counted
    assert verify.load(path)[key]["verdict"] == verify.NOT_REAL
    assert run(73) == []                                     # an hour later: not again
    # ... nor its provider (jump.backfill): the same hours come due together.
    at = lambda after: int(hours[bad]) + after * HOUR + 300
    assert verify.recount_hours(asset, verify.load(path), at(73)) == []
    assert int(hours[bad]) in verify.recount_hours(asset, verify.load(path), at(96))
    assert run(96) == ["yahoo", "marketwatch"]               # a day later: again
    assert run(24 * 10) == []                                # past nine days: never
    assert verify.load(path)[key]["verdict"] == verify.NOT_REAL


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


def test_the_sources_vote_and_the_store_is_one_vote_that_saw_it():
    C, U, P, K = verify.CONFIRMED, verify.UNCONFIRMED, verify.PENDING, verify.UNKNOWN
    one = lambda name, verdict, move: (name, verdict, 0.005, move)
    # The store and MarketWatch saw it, Yahoo did not: real.
    assert verify.combine([one("yahoo", U, 0.0), one("marketwatch", C, 0.004)]) == (
        verify.REAL, 0.005, ["yahoo", "marketwatch"], [0.0, 0.004], ["marketwatch"])
    # Neither did: not real, one against two.
    assert verify.combine([one("yahoo", U, 0.0003), one("marketwatch", U, 0.0001)])[0] == \
        verify.NOT_REAL
    # One against one is a tie: uncertain - and a source without bars around
    # the move does not vote.
    assert verify.combine([one("yahoo", K, 0.0), one("marketwatch", U, 0.0)])[:3] == (
        verify.UNCERTAIN, 0.005, ["marketwatch"])
    # Two of four saw it, the store one of them: a tie.
    assert verify.combine([one("yahoo", C, 0.004), one("sina", U, 0.0),
                           one("marketwatch", U, 0.0)])[0] == verify.UNCERTAIN
    # Nobody else voted: the store's word alone.
    assert verify.combine([one("yahoo", K, 0.0)])[0] == verify.SINGLE
    assert verify.combine([])[0] == verify.SINGLE
    # Still waiting on a source whose answer could turn it: pending...
    assert verify.combine([one("yahoo", P, 0.0), one("marketwatch", U, 0.0)])[0] == P
    # ... but not when it could not: three of four saw it already.
    assert verify.combine([one("yahoo", C, 0.004), one("sina", C, 0.005),
                           one("marketwatch", P, 0.0)])[0] == verify.REAL


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
        verify.UNCERTAIN
    # Yahoo's 03:00 bar, 0.3% up after the night's drift, does not turn it.
    yahoo = bars_(["2026-09-30 10:00"]).pipe(
        lambda f: pd.concat([f, bars_(["2026-10-01 03:00"], 96.40)], ignore_index=True))
    assert verify.judge_all(c, [("yahoo", yahoo), ("marketwatch", mw)],
                            ts("2026-10-01 03:05"))[0] == verify.UNCERTAIN
    # With nobody's bars around the move, the bridge is all there is.
    assert verify.judge_all(c, [("yahoo", yahoo)], ts("2026-10-01 03:05"))[0] == \
        verify.REAL


def test_a_vendor_printing_the_stores_own_bars_still_votes(monkeypatch, tmp_path, basket):
    # Vendors of one exchange tape often print the store's very bars (Yahoo
    # against Sina's fund stores: 0.89 of hours): that is agreement, and it
    # counts. Here Yahoo has the store's bars, the far move included, and
    # MarketWatch stayed flat.
    asset, hours, bad, frame, market = _inr_store(tmp_path, basket)

    def fetch(name, symbol, interval, days, session, now):
        return frame.drop(columns="n_src") if name == "yahoo" else market

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    now = datetime.fromtimestamp(int(hours[bad + 5]) + 300, timezone.utc)
    path = str(tmp_path / "verified.csv")
    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
    row = verify.load(path)[(asset.asset_id, int(hours[bad]), "close")]
    assert (row["verdict"], row["seen"]) == (verify.REAL, "yahoo")


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
    row = verify.load(path)[(asset.asset_id, int(hours[bad]), "close")]
    assert (row["verdict"], row["verifier"]) == (verify.UNCERTAIN, "marketwatch")


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
            for name, verdict in (("a", verify.NOT_REAL), ("b", verify.REAL),
                                  ("c", verify.PENDING), ("d", verify.SINGLE),
                                  ("e", verify.UNCERTAIN))}
    verify.write(rows, now, path)
    # Pending too goes: a bar older than its 24 hours is never asked again.
    assert set(verify.load(path)) == {("a", old, "close")}


def test_a_record_from_before_the_vote_reads_in_its_words(tmp_path):
    # Its confirmed is real, its unknown single source, and its unconfirmed
    # not real until the replay counts it again.
    path = tmp_path / "verified.csv"
    path.write_text("asset_id,hour_utc,check,verdict,provider,verifier,stored_move,"
                    "verifier_move,checked_utc\n"
                    "a,1,close,confirmed,binance,coinbase,0.03,0.03,9\n"
                    "b,2,close,unconfirmed,binance,coinbase,0.05,0.004,9\n"
                    "c,3,open,unknown,sifting,yahoo,0.01,0.0,9\n")
    assert {k[0]: row["verdict"] for k, row in verify.load(str(path)).items()} == {
        "a": verify.REAL, "b": verify.NOT_REAL, "c": verify.SINGLE}
    assert set(verify.not_real(str(path))) == {("b", 2, "close")}


def test_every_other_source_down_leaves_the_stores_word_alone_and_says_so(
        monkeypatch, tmp_path, basket):
    from price_monitor.models import ExchangeError
    asset, hours, bad, frame, market = _inr_store(tmp_path, basket)

    def fetch(name, symbol, interval, days, session, now):
        raise ExchangeError(f"{symbol}: no response")

    monkeypatch.setattr(verify, "fetch_verifier", fetch)
    now = datetime.fromtimestamp(int(hours[bad + 5]) + 300, timezone.utc)
    path = str(tmp_path / "verified.csv")
    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)
    row = verify.load(path)[(asset.asset_id, int(hours[bad]), "close")]
    assert (row["verdict"], row["verifier"]) == (verify.SINGLE, "yahoo,marketwatch")
    assert verify.not_real(path) == {}


def test_an_unconfirmed_move_is_not_scored_and_not_in_the_yardstick():
    start = ts("2026-01-05 00:00")
    rng = np.random.default_rng(3)
    r = rng.normal(0, 0.001, 3000)
    r[2000] = 0.02                                     # 20 sigma
    metrics = pd.DataFrame({"hour_utc": start + HOUR * np.arange(3000), "r": r,
                            "gap": np.nan, "hole": False})
    doubts = {("x:Y", int(metrics["hour_utc"][2000]), "close"): {}}
    kept = jumps.without_not_real(metrics, "x:Y", doubts)
    assert np.isnan(kept["r"][2000]) and np.isfinite(metrics["r"][2000])
    scored = jumps.score(kept, "crypto_24_7")
    assert pd.isna(scored["word"][2000])
    clean = jumps.score(metrics.assign(r=np.where(np.arange(3000) == 2000, np.nan, r)),
                        "crypto_24_7")
    assert np.allclose(scored["sigma"], clean["sigma"], equal_nan=True)
    # Another instrument's verdict leaves this one alone.
    assert jumps.without_not_real(metrics, "x:Z", doubts) is metrics


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
        "verdict": verify.NOT_REAL}}, int(hours[-1]), path)
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
    doubted = verify.not_real(path)
    assert (other.asset_id, int(hours[bad]), "close") in doubted
    # The broken bar is left out and the rest of the source's hours decide.
    assert (asset.asset_id, int(hours[bad]), "close") in doubted


# --- the softs: Sina changes contract on its own days --------------------------

def _sessions_of_coffee(days, level):
    """Hourly closes 08:00-17:00 UTC (inside ICE coffee's session in July)
    on each day, at `level(day, hour)`."""
    rows = []
    for d in days:
        for hh in range(8, 18):
            rows.append({"hour_utc": ts(f"2026-07-{d:02d} {hh:02d}:00"),
                         "close": level(d, hh)})
    return pd.DataFrame(rows).assign(open=lambda f: f["close"])


def test_sinas_change_of_contract_is_found_and_a_bad_print_is_not():
    days = (6, 7, 8, 9)
    store = _sessions_of_coffee(days, lambda d, h: 300.0)
    # Sina on another month from the 8th, 5% lower; one bad print on the 7th.
    sina = _sessions_of_coffee(days, lambda d, h: 285.0 if d >= 8 else
                               (270.0 if (d, h) == (7, 12) else 300.0))
    assert verify.switches(store, sina, "ice_coffee") == [ts("2026-07-08 08:00")]


def test_a_move_across_the_change_is_not_judged_by_sina():
    at = [ts("2026-07-08 08:00")]
    across = {"hour": ts("2026-07-08 08:00"), "prev_hour": ts("2026-07-07 17:00")}
    inside = {"hour": ts("2026-07-08 12:00"), "prev_hour": ts("2026-07-08 11:00")}
    assert verify.crosses(across, at)
    assert not verify.crosses(inside, at)


def test_a_real_gap_across_sinas_change_is_left_unknown_not_rejected(monkeypatch, tmp_path,
                                                                      basket):
    # The session after Sina moved to another month: our +2% open, and Sina's
    # -5% that is only its spread between the two contracts. Judged across it,
    # a real move read as not seen and left scoring for good.
    asset = basket["KC=F"]
    days = (6, 7, 8, 9)
    store = _sessions_of_coffee(days, lambda d, h: 306.0 if d >= 8 else 300.0)
    sina = _sessions_of_coffee(days, lambda d, h: 291.0 if d >= 8 else 300.0)
    c = {"hour": ts("2026-07-08 08:00"), "check": "open", "from_open": False,
         "prev_hour": ts("2026-07-07 17:00"), "prev_close": 300.0, "price": 306.0}
    monkeypatch.setattr(verify, "candidates", lambda *a, **k: [dict(c)])
    monkeypatch.setattr(verify.bars, "load", lambda path, since=None: store)
    monkeypatch.setattr(verify, "fetch_verifier", lambda *a, **k: sina)
    path = str(tmp_path / "verified.csv")
    now = datetime(2026, 7, 9, 18, tzinfo=timezone.utc)

    verify.verify([asset], str(tmp_path / "bars"), None, now=now, path=path)

    row = verify.load(path)[(asset.asset_id, c["hour"], "open")]
    assert row["verdict"] == verify.SINGLE
    assert "changed contract" in row["verifier"]


# --- the night correction ---------------------------------------------------------

# TLH 2020-03-09: Friday closed at 166.83; the store's Monday opens at a stale
# 166.88 and closes its first hour at 173.29 - "+3.8% in the first hour". The
# tape opened at 174.83: the move happened overnight.
TLH_FIRST_HOUR = {"hour": ts("2020-03-09 13:00"), "check": "close", "from_open": True,
                  "prev_hour": ts("2020-03-09 13:00"), "prev_close": 166.88,
                  "price": 173.29, "night_hour": ts("2020-03-06 20:00"),
                  "night_close": 166.83}
TAPE = {"2020-03-06 19:00": 167.60, "2020-03-06 20:00": (167.70, 166.83),
        "2020-03-09 13:00": (174.83, 173.30), "2020-03-09 14:00": (173.30, 172.56)}


def test_a_first_hour_the_feeds_saw_happen_overnight_moves_into_the_night():
    verdict, stored, names, moves, _ = verify.judge_all(
        TLH_FIRST_HOUR, [("yahoo", bars_of(TAPE)), ("sina", bars_of(TAPE))],
        ts("2020-03-09 18:00"))
    assert verdict == verify.OVERNIGHT and names == ["yahoo", "sina"]
    assert moves == pytest.approx([np.log(174.83 / 166.83)] * 2)


def test_a_first_hour_moves_into_the_night_only_if_most_saw_the_whole_move():
    # Two feeds never saw the price get there at all: with the store and the
    # one that did, a tie - no correction, and the hour stays not real.
    other = dict(TAPE, **{"2020-03-09 13:00": (174.83, 167.00),
                          "2020-03-09 14:00": (167.00, 166.90)})
    three = [("yahoo", bars_of(TAPE)), ("sina", bars_of(other)),
             ("marketwatch", bars_of(other))]
    assert verify.judge_all(TLH_FIRST_HOUR, three, ts("2020-03-09 18:00"))[0] == \
        verify.NOT_REAL
    # One of them did after all: most saw it happen overnight.
    three[1] = ("sina", bars_of(TAPE))
    assert verify.judge_all(TLH_FIRST_HOUR, three, ts("2020-03-09 18:00"))[:3:2] == (
        verify.OVERNIGHT, ["yahoo", "sina"])


def test_one_feed_cannot_say_a_move_did_not_happen():
    seen = {"2026-10-05 13:00": 100.0, "2026-10-05 14:00": 101.0, "2026-10-05 15:00": 101.1}
    flat = {"2026-10-05 13:00": 100.0, "2026-10-05 14:00": 100.0, "2026-10-05 15:00": 100.0}
    c = {"hour": ts("2026-10-05 14:00"), "check": "close", "from_open": False,
         "prev_hour": ts("2026-10-05 13:00"), "prev_close": 100.0, "price": 101.0}
    assert verify.judge_all(c, [("sina", bars_of(flat)), ("marketwatch", bars_of(seen))],
                            ts("2026-10-05 18:00"))[0] == verify.REAL
    assert verify.judge_all(c, [("sina", bars_of(flat)), ("marketwatch", bars_of(flat))],
                            ts("2026-10-05 18:00"))[0] == verify.NOT_REAL


def test_the_detector_reads_the_night_there_and_keeps_the_store_s_total():
    payout = 0.002                       # the stored gap had a payout taken out
    stored_night = np.log(166.88 / 166.83)
    metrics = pd.DataFrame({"hour_utc": [ts("2020-03-06 20:00"), ts("2020-03-09 13:00")],
                            "close": [166.83, 173.29],
                            "r": [0.0001, np.log(173.29 / 166.88)],
                            "gap": [np.nan, stored_night + payout], "hole": np.nan})
    night = np.log(174.83 / 166.83)
    out = jumps.with_nights(metrics, "x:TLH", {("x:TLH", ts("2020-03-09 13:00"), "close"): night})
    assert out["gap"][1] == pytest.approx(night + payout)
    assert out["r"][1] == pytest.approx(np.log(173.29 / 166.83) - night)
    assert out["r"][1] + out["gap"][1] - payout == pytest.approx(np.log(173.29 / 166.83))
    # An unscored gap stays unscored; the hour is still corrected.
    unscored = jumps.with_nights(metrics.assign(gap=np.nan), "x:TLH",
                                 {("x:TLH", ts("2020-03-09 13:00"), "close"): night})
    assert np.isnan(unscored["gap"][1])
    assert unscored["r"][1] == pytest.approx(np.log(173.29 / 166.83) - night)


def test_an_overnight_verdict_is_kept_for_good_and_read_as_the_feeds_median(tmp_path):
    path = str(tmp_path / "verified.csv")
    old, now = ts("2020-03-09 13:00"), ts("2026-10-01 00:00")
    verify.write({("x:TLH", old, "close"): {
        "asset_id": "x:TLH", "hour_utc": old, "check": "close", "verdict": verify.OVERNIGHT,
        "verifier": "yahoo,sina", "verifier_move": "0.046000,0.048000"}}, now, path)
    assert verify.overnight(path) == {("x:TLH", old, "close"): pytest.approx(0.047)}
    assert verify.not_real(path) == {}

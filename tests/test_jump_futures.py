from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from jump import bars, futures, returns, sessions
from jump.basket import Asset

NY = ZoneInfo("America/New_York")
HOUR = 3600


def ny(*a):
    return int(datetime(*a, tzinfo=NY).timestamp())


# --- the roll calendar ---------------------------------------------------------

def test_last_trading_days_match_the_rolls_yahoo_made_in_2026():
    # The session after each is when Yahoo's series was seen on the next contract.
    assert date(2026, 9, 18) in futures.last_trading_days("KC=F", 2026, 2026)
    assert date(2026, 9, 15) in futures.last_trading_days("CC=F", 2026, 2026)
    assert date(2026, 7, 9) in futures.last_trading_days("CT=F", 2026, 2026)
    assert date(2026, 8, 31) in futures.last_trading_days("LE=F", 2026, 2026)


def test_the_live_contract_rolls_before_first_notice_and_skips_october_cotton():
    assert futures.front_contract("KC=F", date(2026, 10, 2)) == ("KCZ26.NYB", date(2026, 8, 14))
    assert futures.front_contract("CT=F", date(2026, 10, 2))[0] == "CTZ26.NYB"
    # Cattle rolls when its volume does: December passed October 2026-09-11..16.
    assert futures.front_contract("LE=F", date(2026, 10, 2)) == ("LEZ26.CME", date(2026, 9, 15))
    assert futures.front_contract("ALI=F", date(2026, 10, 2)) is None


def test_stage_twos_last_trading_days_follow_each_exchanges_rule():
    # Brent two months before delivery, sugar the month before, the 10-year seven
    # business days before its month's last, the grains the business day before
    # the 15th (soybeans' November 2026: the 15th a Sunday, so Friday the 13th).
    assert date(2026, 9, 30) in futures.last_trading_days("BZ=F", 2026, 2026)      # BZX26
    assert date(2026, 10, 30) in futures.last_trading_days("BZ=F", 2026, 2026)     # BZZ26
    assert date(2026, 9, 30) in futures.last_trading_days("SB=F", 2026, 2026)      # SBV26
    assert date(2026, 9, 21) in futures.last_trading_days("ZN=F", 2026, 2026)
    assert date(2026, 9, 30) in futures.last_trading_days("ZT=F", 2026, 2026)
    assert date(2026, 11, 13) in futures.last_trading_days("ZS=F", 2026, 2026)
    assert date(2026, 10, 28) in futures.last_trading_days("PL=F", 2026, 2026)


def test_stage_twos_contracts_roll_when_yahoos_series_did_in_2026():
    # Each onto the contract held now on the day Yahoo's own series came onto it;
    # corn and soybeans before July's first notice, past September (and August).
    assert futures.front_contract("BZ=F", date(2026, 10, 12)) == ("BZZ26.NYM", date(2026, 9, 28))
    assert futures.front_contract("ZN=F", date(2026, 10, 12)) == ("ZNZ26.CBT", date(2026, 8, 27))
    assert futures.front_contract("ZW=F", date(2026, 10, 12)) == ("ZWZ26.CBT", date(2026, 8, 12))
    assert futures.front_contract("SB=F", date(2026, 10, 12)) == ("SBH27.NYB", date(2026, 9, 9))
    assert futures.front_contract("PL=F", date(2026, 10, 12)) == ("PLF27.NYM", date(2026, 9, 28))
    assert futures.front_contract("ZC=F", date(2026, 7, 1))[0] == "ZCZ26.CBT"
    assert futures.front_contract("ZS=F", date(2026, 7, 1))[0] == "ZSX26.CBT"
    # Brent holds every month: November until three business days before 09-30.
    assert futures.front_contract("BZ=F", date(2026, 9, 25))[0] == "BZX26.NYM"


def test_both_roll_calendars_leave_their_nights_unscored():
    days = [str(d.date()) for d in pd.bdate_range("2026-08-01", "2026-10-01")]
    rolled = futures.roll_sessions("KC=F", days)
    assert {"2026-08-13", "2026-08-14"} <= rolled          # this series' own roll
    assert {"2026-09-18", "2026-09-21"} <= rolled          # Yahoo's, in the history
    assert "2026-09-01" not in rolled


# --- thin bars -----------------------------------------------------------------

def test_a_bar_without_trading_is_thin_and_the_window_ends_before_it():
    vol = pd.Series([1000.0] * 60 + [0.0, 20.0, 900.0])
    assert list(futures.thin(vol).iloc[-3:]) == [True, True, False]
    # A single huge bar does not make the next one look thin.
    vol = pd.Series([1000.0] * 60 + [10 ** 9, 1000.0])
    assert not futures.thin(vol).iloc[-1]


def _frame(closes, opens=None, start=ny(2026, 3, 2, 9), step=HOUR):
    opens = opens if opens is not None else closes
    return pd.DataFrame({"hour_utc": [start + i * step for i in range(len(closes))],
                         "open": opens, "high": np.maximum(opens, closes),
                         "low": np.minimum(opens, closes), "close": closes,
                         "volume": 100.0, "n_src": 1}).astype(bars.SCHEMA)


# --- daily sessions ---------------------------------------------------------------

def test_an_overnight_session_starts_the_evening_before():
    assert sessions.daily_session_of(ny(2026, 3, 1, 21), "ice_cotton") == date(2026, 3, 2)
    assert sessions.daily_session_of(ny(2026, 3, 2, 14), "ice_cotton") == date(2026, 3, 2)
    assert sessions.daily_session_of(ny(2026, 3, 2, 15), "ice_cotton") is None
    assert sessions.daily_session_of(ny(2026, 3, 6, 21), "ice_cotton") is None   # Friday night


def _future(ticker="KC=F", template="ice_coffee"):
    return Asset(ticker=ticker, source="yahoo", block="agriculture",
                 has_volume=True, tick_size=0.05, session_template=template,
                 fetch_interval="1h", label=ticker, in_basket=True)


def _dividends():
    from jump.corporate_actions import Dividends
    return Dividends(steps={}, splits={}, checked_through={})


def _two_sessions(first_day, second_day, last_hour=12):
    rows = [(ny(*first_day, h), 300, 301, 299, 300, 100.0, 1) for h in range(5, last_hour + 1)]
    # The second session from its first hour: coffee opens 04:15 New York.
    rows += [(ny(*second_day, 4), 309, 310, 308, 309, 100.0, 1),
             (ny(*second_day, 5), 309, 310, 308, 309.5, 100.0, 1)]
    return pd.DataFrame(rows, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)


def test_a_night_ending_within_an_hour_of_the_close_is_scored():
    # Coffee closes 13:30; Yahoo's 13:00 bar is a zero-volume marker.
    out = returns.split_channels(_future(), _two_sessions((2026, 3, 3), (2026, 3, 4)),
                                 dividends=_dividends())
    assert out["gap"].dropna().tolist() == pytest.approx([np.log(309 / 300)])


def test_a_night_that_lost_its_afternoon_is_not():
    out = returns.split_channels(_future(), _two_sessions((2026, 3, 3), (2026, 3, 4), 10),
                                 dividends=_dividends())
    assert out["gap"].isna().all()


def test_a_roll_night_is_not_scored():
    # Coffee's September 2026 contract last traded on the 18th.
    out = returns.split_channels(_future(), _two_sessions((2026, 9, 18), (2026, 9, 21)),
                                 dividends=_dividends())
    assert out["gap"].isna().all()


def test_a_thin_bar_is_invalid_for_a_continuous_future():
    from jump import quality
    frame = _frame([300.0] * 61).assign(volume=[1000.0] * 60 + [0.0])
    reasons = quality.invalid_reasons(_future(), frame)
    assert reasons.iloc[-1] == "volume too thin to be a trade" and (reasons.iloc[:-1] == "").all()


def test_a_history_sources_switch_night_is_a_roll(tmp_path, monkeypatch):
    # Dukascopy's CFD switches contract on days of its own; the list of them
    # leaves those nights unscored like the series' own rolls.
    rolls = tmp_path / "rolls.csv"
    rolls.write_text("ticker,date,source\nKC=F,2021-07-26,dukascopy\n")
    monkeypatch.setattr(futures, "ROLLS_PATH", str(rolls))
    days = ["2021-07-22", "2021-07-23", "2021-07-26", "2021-07-27", "2021-07-28"]
    marked = futures.roll_sessions("KC=F", days)
    monkeypatch.setattr(futures, "ROLLS_PATH", str(tmp_path / "none.csv"))
    without = futures.roll_sessions("KC=F", days)
    assert marked - without == {"2021-07-26", "2021-07-27"}


def test_a_night_ending_on_a_late_first_bar_is_not_scored():
    # F2: with the 04:00 hour missing, the 05:00 bar's open is the night and an
    # hour of trading.
    late = _two_sessions((2026, 3, 3), (2026, 3, 4))
    late = late[late["hour_utc"] != ny(2026, 3, 4, 4)].reset_index(drop=True)
    out = returns.split_channels(_future(), late, dividends=_dividends())
    assert out["gap"].isna().all()


def test_the_ft_names_stage_twos_contracts_as_it_lists_them():
    from price_monitor import ft
    assert ft.futures_symbol("ZCZ26.CBT") == "CZ26"
    assert ft.futures_symbol("ZSX26.CBT") == "SX26"
    assert ft.futures_symbol("PLF27.NYM") == "US@PLF272"
    assert ft.futures_symbol("ZNZ26.CBT") is None and ft.futures_symbol(None) is None



def test_the_bund_gilt_and_ttf_roll_by_their_exchanges_rules():
    # The Bund's last trading day two business days before the 10th (December
    # 2026's on Tuesday the 8th), rolled three before it; the gilt's two before
    # its month's last business day, rolled five before first notice (two before
    # the month); TTF's the second-last business day of the month before.
    assert date(2026, 12, 8) in futures.last_trading_days("FGBL=F", 2026, 2026)
    assert date(2026, 3, 6) in futures.last_trading_days("FGBL=F", 2026, 2026)   # 10th a Tuesday
    assert futures.front_contract("FGBL=F", date(2026, 12, 3))[0] == "FGBLZ26.EUX"
    assert futures.front_contract("FGBL=F", date(2026, 12, 4)) == ("FGBLH27.EUX", date(2026, 12, 4))
    assert date(2026, 12, 29) in futures.last_trading_days("RLI=F", 2026, 2026)
    assert futures.front_contract("RLI=F", date(2026, 11, 20)) == ("RLIH27.ICE", date(2026, 11, 20))
    assert date(2026, 10, 29) in futures.last_trading_days("TFM=F", 2026, 2026)   # TFMX26
    assert futures.front_contract("TFM=F", date(2026, 10, 30))[0] == "TFMZ26.NDX"


def test_ttfs_roll_nights_hold_marketwatchs_change_of_month():
    # MarketWatch's TTF is on the next month from the last trading day itself, and
    # a session earlier where a holiday moves that day: September 2026's last
    # traded on 08-27, London's bank holiday on 08-31 counted out, and its value
    # changed that day. The nights into 08-27, 08-28 and 08-31 are not scored.
    days = [date(2026, 8, 3) + timedelta(i) for i in range(40)]
    rolled = futures.roll_sessions("TFM=F", [d for d in days if d.weekday() < 5])
    assert {"2026-08-27", "2026-08-28", "2026-08-31"} <= rolled
    assert "2026-08-26" not in rolled


def test_a_future_without_volume_has_no_thin_bars():
    # MarketWatch serves the Bund without volume: every bar a zero, none thin.
    from jump import quality
    bund = _future("FGBL=F", "eurex_bund")
    from dataclasses import replace
    frame = _frame([120.0] * 61).assign(volume=0.0)
    assert (quality.invalid_reasons(replace(bund, has_volume=False), frame) == "").all()
    assert (quality.invalid_reasons(bund, frame) == "volume too thin to be a trade").all()

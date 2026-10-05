from datetime import date, datetime
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
    assert futures.front_contract("LE=F", date(2026, 10, 2)) == ("LEV26.CME", date(2026, 9, 1))
    assert futures.front_contract("ALI=F", date(2026, 10, 2)) is None


def test_both_roll_calendars_leave_their_nights_unscored():
    days = [str(d.date()) for d in pd.bdate_range("2026-08-01", "2026-10-01")]
    rolled = futures.roll_sessions("KC=F", days)
    assert {"2026-08-13", "2026-08-14"} <= rolled          # this series' own roll
    assert {"2026-09-18", "2026-09-21"} <= rolled          # Yahoo's, in the history
    assert "2026-09-01" not in rolled


def test_where_yahoo_lags_this_series_its_history_is_left_out():
    windows = futures.yahoo_lag_windows("KC=F", 2026, 2026)
    assert (date(2026, 8, 14), date(2026, 9, 21)) in windows
    assert futures.yahoo_lag_windows("LE=F", 2026, 2026) == []


# --- thin bars and the history cleaning -----------------------------------------

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


def test_an_other_contract_hour_is_dropped_and_a_stray_open_reset():
    closes = [300.0, 301.0, 285.0, 301.5, 302.0, 302.5]     # 285: there and back
    opens = [300.0, 300.0, 301.0, 301.0, 270.0, 302.0]     # 270: a stray open
    out, dropped, stray, _ = futures.clean_history(_frame(closes, opens))
    assert dropped == 1 and 285.0 not in list(out["close"])
    assert stray == 1 and out.loc[out["close"] == 302.0, "open"].item() == 301.5


def test_a_month_with_under_half_a_normal_months_hours_is_dropped():
    full = [ny(2026, m, d, 9) for m in (1, 2, 3, 4, 5) for d in range(2, 28)]
    sparse = [ny(2026, 6, d, 9) for d in (2, 3)]
    tail = [ny(2026, 7, d, 9) for d in range(2, 28)]
    stamps = full + sparse + tail
    frame = _frame([300.0] * len(stamps)).assign(hour_utc=stamps)
    out, _, _, months = futures.clean_history(frame)
    assert months == ["2026-06"] and len(out) == len(stamps) - 2


# --- daily sessions ---------------------------------------------------------------

def test_an_overnight_session_starts_the_evening_before():
    assert sessions.daily_session_of(ny(2026, 3, 1, 21), "ice_cotton") == date(2026, 3, 2)
    assert sessions.daily_session_of(ny(2026, 3, 2, 14), "ice_cotton") == date(2026, 3, 2)
    assert sessions.daily_session_of(ny(2026, 3, 2, 15), "ice_cotton") is None
    assert sessions.daily_session_of(ny(2026, 3, 6, 21), "ice_cotton") is None   # Friday night


def test_the_real_trades_twelve_to_twenty_one_utc():
    utc = lambda h: int(datetime(2026, 3, 4, h, tzinfo=ZoneInfo("UTC")).timestamp())  # noqa: E731
    inside = [h for h in range(24) if sessions.daily_session_of(utc(h), "b3_fx")]
    assert inside == list(range(12, 21))
    assert sessions.daily_bars_per_day("b3_fx") == 9


def _future(ticker="KC=F", template="ice_coffee"):
    return Asset(ticker=ticker, source="yahoo", block="agriculture",
                 has_volume=True, tick_size=0.05, session_template=template,
                 fetch_interval="1h", label=ticker, in_basket=True)


def _dividends():
    from jump.corporate_actions import Dividends
    return Dividends(steps={}, splits={}, checked_through={})


def _two_sessions(first_day, second_day, last_hour=12):
    rows = [(ny(*first_day, h), 300, 301, 299, 300, 100.0, 1) for h in range(5, last_hour + 1)]
    rows += [(ny(*second_day, 5), 309, 310, 308, 309, 100.0, 1),
             (ny(*second_day, 6), 309, 310, 308, 309.5, 100.0, 1)]
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


def _cattle_sessions(levels_by_day):
    """Bars for live cattle (13:00-18:00 UTC in summer), one list of closes a day."""
    from datetime import datetime, timezone
    import pandas as pd
    from datetime import timedelta
    rows = []
    day = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for closes in levels_by_day:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        for i, c in enumerate(closes):
            t = int(day.replace(hour=13 + i).timestamp())
            rows.append((t, c, c, c, c, 1000.0, 1))
        day += timedelta(days=1)
    return pd.DataFrame(rows, columns=["hour_utc", "open", "high", "low", "close", "volume", "n_src"])


def test_a_run_of_sessions_interleaving_two_months_is_dropped_and_one_reversal_kept():
    normal = [100.0] * 6
    mixed = [96.5, 96.5, 100.0, 100.0, 100.0, 100.0]     # opens on the other month
    reversal = [97.0, 99.5, 99.8, 99.9, 100.0, 100.0]     # once: a real day
    days = [normal] * 3 + [reversal] + [normal] * 16 + [mixed] * 4 + [normal] * 3
    frame = _cattle_sessions(days)
    kept, stretches = futures.drop_mixed(frame, "cme_cattle")
    assert len(stretches) == 1
    assert len(frame) - len(kept) == 4 * 6


def test_a_stray_close_taken_back_by_the_next_open_is_reset():
    import numpy as np
    from jump import futures
    hours = [3600 * i for i in range(40)]
    close = list(100.0 + 0.05 * np.sin(np.arange(40)))
    frame = pd.DataFrame({"hour_utc": hours, "open": close, "high": close, "low": close,
                          "close": close, "volume": 1.0, "n_src": 1})
    frame["open"] = frame["close"].shift(1).fillna(frame["close"])
    frame.loc[20, "close"] = 93.0              # -7%, and the next bar opens back
    frame.loc[30, "close"] = 103.0             # +3%, and the next bar goes on
    frame.loc[31, "open"] = 104.0
    out, n = futures.reset_stray_closes(frame)
    assert n == 1
    assert out.loc[20, "close"] == frame.loc[21, "open"]
    assert out.loc[20, "low"] <= out.loc[20, "close"]
    assert out.loc[30, "close"] == 103.0

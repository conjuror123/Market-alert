from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from price_monitor import sina
from tremor import bars, returns, sessions
from tremor.basket import Asset

BJ = ZoneInfo("Asia/Shanghai")


def bj(*a):
    return int(datetime(*a, tzinfo=BJ).timestamp())


def _answer(rows):
    body = ",".join('{"d":"%s","o":"%s","h":"%s","l":"%s","c":"%s","v":"%s","p":"1"}' % r
                    for r in rows)
    return "/*<script>location.href='//sina.com';</script>*/\nvar t=([" + body + "]);"


def test_bars_are_stamped_at_the_hour_their_first_minute_falls_in():
    text = _answer([("2026-09-29 22:00:00", 1, 2, 1, 2, 5),
                    ("2026-09-30 10:00:00", 2, 3, 2, 3, 5),
                    ("2026-09-30 11:15:00", 3, 4, 3, 4, 5),
                    ("2026-09-30 14:15:00", 4, 5, 4, 5, 5),
                    ("2026-09-30 15:00:00", 5, 6, 5, 6, 5)])
    candles = sina.parse(text, "SN0", now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert [c.open_time for c in candles] == [bj(2026, 9, 29, 21), bj(2026, 9, 30, 9),
                                              bj(2026, 9, 30, 10), bj(2026, 9, 30, 13),
                                              bj(2026, 9, 30, 14)]


def test_a_bar_still_running_is_left_for_the_next_run():
    text = _answer([("2026-09-30 10:00:00", 2, 3, 2, 3, 5), ("2026-09-30 11:15:00", 3, 4, 3, 4, 5)])
    now = datetime.fromtimestamp(bj(2026, 9, 30, 11, 5), timezone.utc)
    assert len(sina.parse(text, "SN0", now=now)) == 1


def test_an_answer_without_bars_raises_and_null_is_empty():
    from price_monitor.models import ExchangeError
    with pytest.raises(ExchangeError):
        sina.parse("<html>blocked</html>", "SN0")
    assert sina.parse("var t=(null);", "SN2801") == []


def test_shanghai_has_a_night_and_a_day_session():
    assert sessions.segment_of(bj(2026, 9, 29, 21), "shfe") == "2026-09-29 21:00"
    assert sessions.segment_of(bj(2026, 9, 30, 0), "shfe") == "2026-09-29 21:00"
    assert sessions.segment_of(bj(2026, 9, 30, 1), "shfe") is None
    assert sessions.segment_of(bj(2026, 9, 30, 12), "shfe") == "2026-09-30 09:00"
    assert sessions.segment_of(bj(2026, 10, 2, 21), "shfe") == "2026-10-02 21:00"   # Friday night
    assert sessions.segment_of(bj(2026, 10, 3, 9), "shfe") is None                   # Saturday


def _tin():
    return Asset(ticker="SN0", source="sina", tier=2, block="industrial_metals",
                 has_volume=True, tick_size=10.0, session_template="shfe",
                 fetch_interval="1h", label="Tin", in_basket=True)


def test_both_of_shanghais_pauses_are_scored_gaps_and_lunch_is_a_hole():
    from tremor.corporate_actions import Dividends
    rows = [(bj(2026, 9, 29, h), 400, 401, 399, p, 9.0, 1) for h, p in
            ((9, 400), (10, 400), (13, 401), (14, 402))]                 # day
    rows += [(bj(2026, 9, 29, 21), 404, 405, 403, 404, 9.0, 1),          # night
             (bj(2026, 9, 30, 0), 404, 405, 403, 405, 9.0, 1),
             (bj(2026, 9, 30, 9), 410, 411, 409, 410, 9.0, 1)]           # next day
    frame = pd.DataFrame(rows, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)
    out = returns.split_channels(_tin(), frame,
                                 dividends=Dividends(steps={}, splits={}, checked_through={}))
    gaps = out.set_index("hour_utc")["gap"].dropna()
    assert gaps.loc[bj(2026, 9, 29, 21)] == pytest.approx(np.log(404 / 402))
    assert gaps.loc[bj(2026, 9, 30, 9)] == pytest.approx(np.log(410 / 405))
    assert np.isfinite(out.set_index("hour_utc").loc[bj(2026, 9, 29, 13), "hole"])


NY = ZoneInfo("America/New_York")


def test_us_half_hours_are_stamped_at_their_start_inside_the_session():
    text = _answer([("2026-10-01 09:30:00", 1, 1, 1, 1, 5),     # pre-market
                    ("2026-10-01 10:00:00", 2, 2, 2, 2, 5),
                    ("2026-10-01 16:00:00", 3, 3, 3, 3, 5),
                    ("2026-10-01 16:30:00", 4, 4, 4, 4, 5)])    # after hours
    candles = sina.parse_us(text, "SPY", now=datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert [c.open_time for c in candles] == [
        int(datetime(2026, 10, 1, 9, 30, tzinfo=NY).timestamp()),
        int(datetime(2026, 10, 1, 15, 30, tzinfo=NY).timestamp())]


def test_a_us_half_hour_still_running_is_left_out():
    text = _answer([("2026-10-01 10:00:00", 2, 2, 2, 2, 5), ("2026-10-01 10:30:00", 3, 3, 3, 3, 5)])
    now = datetime(2026, 10, 1, 10, 5, tzinfo=NY).astimezone(timezone.utc)
    assert len(sina.parse_us(text, "SPY", now=now)) == 1


def test_the_fetch_asks_sina_for_a_sina_fund(tmp_path, monkeypatch):
    import requests
    from datetime import date
    from tremor import backfill
    seen = {}
    monkeypatch.setattr(backfill.sina, "fetch_us_bars",
                        lambda symbol, session=None, now=None: seen.setdefault("us", symbol) and [])
    monkeypatch.setattr(backfill.sina, "fetch_bars",
                        lambda symbol, session=None, now=None: seen.setdefault("fut", symbol) and [])
    fund = Asset(ticker="RWX", source="twelvedata", provider="sina", tier=2, block="equity",
                 has_volume=True, tick_size=0.01, session_template="us_equity",
                 fetch_interval="30min", label="RWX", in_basket=True)
    backfill.fetch_missing(fund, str(tmp_path / "a"), date(2021, 1, 1), "", requests.Session())
    backfill.fetch_missing(_tin(), str(tmp_path / "b"), date(2021, 1, 1), "", requests.Session())
    assert seen == {"us": "RWX", "fut": "SN0"}

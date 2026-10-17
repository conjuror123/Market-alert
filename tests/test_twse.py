"""price_monitor/twse.py: the Taiwan exchange's TAIEX every five seconds, folded
to hours."""
from datetime import datetime, timedelta, timezone

from price_monitor import twse

# Rows of 2026-10-12's report: the time, the TAIEX, the index without finance.
PAYLOAD = {"stat": "OK", "date": "20261012", "data": [
    ["09:00:00", "49,313.44", "44,161.08"], ["09:59:55", "49,077.38", "43,831.93"],
    ["10:00:00", "49,117.62", "43,871.05"], ["10:30:00", "49,045.74", "43,803.15"],
    ["10:59:55", "49,278.68", "44,010.65"], ["13:29:55", "49,293.41", "44,042.09"],
    ["13:30:00", "49,301.96", "44,038.33"]]}


def utc(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def test_each_hour_holds_its_prints_and_the_close_falls_in_the_13_oclock_hour():
    out = twse.parse(PAYLOAD, now=datetime(2026, 10, 12, 8, tzinfo=timezone.utc))
    # 09:00 Taipei is 01:00 UTC.
    assert [c.open_time for c in out] == [utc("2026-10-12 01:00"), utc("2026-10-12 02:00"),
                                          utc("2026-10-12 05:00")]
    first, second, last = out
    assert (first.open, first.close) == (49313.44, 49077.38)
    assert (second.open, second.high, second.low, second.close) == (
        49117.62, 49278.68, 49045.74, 49278.68)
    assert last.close == 49301.96


def test_a_day_without_trading_has_no_bars_and_a_weekend_is_not_asked(monkeypatch):
    assert twse.parse({"stat": "很抱歉，沒有符合條件的資料!", "date": "20261010"}) == []
    asked = []
    monkeypatch.setattr(twse, "_day", lambda session, day: asked.append(day.date()) or PAYLOAD)
    monkeypatch.setattr(twse.time, "sleep", lambda s: None)
    now = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)              # a Monday
    out = twse.fetch_hourly(None, now, days=3)
    assert asked == [now.date(), now.date() - timedelta(days=3)]      # Monday, Friday
    assert len(out) == 6

"""price_monitor/naver.py: Naver's KOSPI minutes, folded to hours."""
from datetime import datetime, timezone

from price_monitor import naver

# Rows of 2026-10-12's answer (Seoul time): the 09:00 hour's first and last
# minute, the 10:00 hour's, and the 15:30 closing auction.
ROWS = [
    {"localDateTime": "20261012090000", "currentPrice": 6576.79, "openPrice": 6578.17,
     "highPrice": 6578.17, "lowPrice": 6575.9, "accumulatedTradingVolume": 7241},
    {"localDateTime": "20261012095900", "currentPrice": 6472.84, "openPrice": 6471.23,
     "highPrice": 6473.53, "lowPrice": 6471.01, "accumulatedTradingVolume": 1043},
    {"localDateTime": "20261012100000", "currentPrice": 6468.21, "openPrice": 6471.08,
     "highPrice": 6473.81, "lowPrice": 6468.21, "accumulatedTradingVolume": 1740},
    {"localDateTime": "20261012105900", "currentPrice": 6510.72, "openPrice": 6511.25,
     "highPrice": 6512.72, "lowPrice": 6507.24, "accumulatedTradingVolume": 498},
    {"localDateTime": "20261012152900", "currentPrice": 6394.26, "openPrice": 6394.26,
     "highPrice": 6394.26, "lowPrice": 6394.26, "accumulatedTradingVolume": 0},
    {"localDateTime": "20261012153000", "currentPrice": 6384.24, "openPrice": 6394.26,
     "highPrice": 6394.26, "lowPrice": 6384.05, "accumulatedTradingVolume": 12764},
]


def utc(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def test_each_hour_is_its_minutes_first_open_and_last_price():
    out = naver.parse(list(reversed(ROWS)), now=datetime(2026, 10, 12, 8, tzinfo=timezone.utc))
    # 09:00 Seoul is 00:00 UTC; the 15:30 auction closes the 15:00 hour.
    assert [c.open_time for c in out] == [utc("2026-10-12 00:00"), utc("2026-10-12 01:00"),
                                          utc("2026-10-12 06:00")]
    first = out[0]
    assert (first.open, first.high, first.low, first.close) == (6578.17, 6578.17, 6471.01,
                                                                6472.84)
    assert out[2].close == 6384.24 and out[2].close_time == out[2].open_time + 3600


def test_an_hour_not_ended_is_left_out():
    out = naver.parse(ROWS, now=datetime(2026, 10, 12, 1, 30, tzinfo=timezone.utc))
    assert [c.open_time for c in out] == [utc("2026-10-12 00:00")]

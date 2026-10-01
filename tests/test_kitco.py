from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from price_monitor import kitco
from price_monitor.models import ExchangeError

LDN = ZoneInfo("Europe/London")


def at(d, h, m=0):
    return int(datetime(2026, 9, d, h, m, tzinfo=LDN).timestamp())


def test_a_london_day_that_never_moves_is_dropped_whole():
    quotes = [(at(28, 9), 15000.0), (at(28, 10), 15000.0), (at(28, 21), 15000.0),
              (at(29, 9), 15000.0), (at(29, 10), 15010.0)]
    assert kitco.drop_still_days(quotes) == quotes[3:]


def _plateau(step, back_after_minutes):
    q = [(at(29, 9, 5 * i), 15000.0 + i) for i in range(6)]
    t = q[-1][0]
    q += [(t + 300 * k, 15005 * step) for k in range(1, back_after_minutes // 5 + 1)]
    t = q[-1][0]
    q += [(t + 300 * k, 15020.0) for k in range(1, 4)]
    return q


def test_a_glitch_that_comes_back_is_dropped_from_the_step_to_the_return():
    q = _plateau(0.62, 60)
    out = kitco.drop_glitches(q, now=at(30, 12))
    assert [p for _, p in out] == [p for _, p in q[:6]] + [15020.0] * 3


def test_a_step_that_stays_for_a_day_is_a_market():
    q = [(at(29, 9), 15000.0), (at(29, 9, 5), 12000.0)]
    q += [(at(29, 9, 5) + 3600 * k, 12000.0 + k) for k in range(1, 26)]
    out = kitco.drop_glitches(q, now=at(30, 12))
    assert out == q


def test_a_step_not_yet_decided_is_held_back():
    q = [(at(29, 9), 15000.0), (at(29, 9, 5), 12000.0), (at(29, 9, 10), 12010.0)]
    assert kitco.drop_glitches(q, now=at(29, 12)) == q[:1]


def test_an_ordinary_move_passes_untouched():
    q = [(at(29, 9, 5 * i), 15000.0 * (1.01 ** i)) for i in range(10)]
    assert kitco.drop_glitches(q, now=at(30, 12)) == q


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Session:
    def __init__(self, body):
        self.body = body

    def post(self, *a, **k):
        return _Resp(200, self.body)


def test_quotes_come_back_as_five_minute_candles_of_the_mid_in_dollars_a_tonne():
    end = datetime(2026, 9, 29, 10, 15, tzinfo=LDN)
    rows = [{"bid": 7.0, "ask": 7.2, "timestamp": at(29, 10)},
            {"bid": 7.1, "ask": 7.3, "timestamp": at(29, 10, 5)}]
    session = _Session({"data": {"GetMetalHistoryV3": {"results": rows}}})
    candles = kitco.fetch_full_history("NI", "1h", 0.1, session=session, end=end)
    assert [c.open_time for c in candles] == [at(29, 10), at(29, 10, 5)]
    assert candles[0].close == pytest.approx(7.1 * kitco.POUNDS_PER_TONNE)
    assert candles[0].close_time - candles[0].open_time == 300


def test_an_answer_without_the_series_raises():
    session = _Session({"errors": [{"message": "invalid metal symbol"}], "data": None})
    with pytest.raises(ExchangeError, match="no series"):
        kitco.fetch_full_history("XX", "1h", 0.1, session=session,
                                 end=datetime.now(timezone.utc) - timedelta(days=1))

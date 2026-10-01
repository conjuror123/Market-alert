from datetime import datetime, timezone

import pytest

from price_monitor import google
from price_monitor.models import ExchangeError


def _block(key, rows):
    body = ",".join(f'[{o},{c},{h},{lo},"{s}",{v}]' for o, c, h, lo, s, v in rows)
    return (f"<script>AF_initDataCallback({{key: 'ds:{key}', hash: '1', "
            f'data:[[[["RWX","NYSEARCA"],"/g/x","USD",[[[1],null,[{body}]]]]]], '
            f"sideChannel: {{}}}});</script>")


# The month's daily rows come first on the real page too; they share the shape.
MONTH = _block(12, [(27.22, 27.23, 27.37, 27.11, "2026-09-01T16:00:00-04:00", 34587),
                    (27.28, 27.28, 27.29, 27.21, "2026-09-02T16:00:00-04:00", 12275)])
DAY = _block(10, [(25.7, 25.7, 25.7, 25.7, "2026-09-30T09:30:00-04:00", 750),
                  (25.56, 25.54, 25.56, 25.54, "2026-09-30T11:25:00-04:00", 400),
                  (25.37, 25.37, 25.37, 25.37, "2026-09-30T16:00:00-04:00", 106)])


def _utc(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp())


def test_bars_are_moved_back_to_their_start_and_the_opening_print_stays():
    candles = google.parse(MONTH + DAY, "RWX", "NYSEARCA")
    assert [c.open_time for c in candles] == [
        _utc(2026, 9, 30, 13, 30),   # 09:30 New York: the opening print, an instant
        _utc(2026, 9, 30, 15, 20),   # stamped 11:25, the five minutes from 11:20
        _utc(2026, 9, 30, 19, 55)]   # stamped 16:00, the close, inside the session
    assert [(c.open, c.close, c.high, c.low, c.volume) for c in candles][1] == \
        (25.56, 25.54, 25.56, 25.54, 400.0)


def test_rows_outside_the_regular_session_are_dropped():
    page = _block(10, [(25.6, 25.6, 25.6, 25.6, "2026-09-30T08:00:00-04:00", 10),
                       (25.7, 25.7, 25.7, 25.7, "2026-09-30T10:00:00-04:00", 20),
                       (25.8, 25.8, 25.8, 25.8, "2026-09-30T17:30:00-04:00", 30)])
    assert [c.close for c in google.parse(page, "RWX", "NYSEARCA")] == [25.7]


def test_a_session_no_trade_has_reached_is_empty_not_an_error():
    page = '<script>["RWX","NYSEARCA"]</script>' + MONTH
    assert google.parse(page, "RWX", "NYSEARCA") == []


def test_a_page_without_the_listing_raises():
    with pytest.raises(ExchangeError, match="no longer carries"):
        google.parse("<html>consent</html>", "RWX", "NYSEARCA")


def test_an_unconfigured_ticker_raises_before_asking():
    with pytest.raises(ExchangeError, match="EXCHANGES"):
        google.fetch_full_history("SPY", "30min", 1, session=object())


def test_the_hour_folds_like_any_finer_grid():
    from tremor import bars
    hourly = bars.to_hourly(bars.candles_to_frame(google.parse(DAY, "RWX", "NYSEARCA")))
    assert list(hourly["hour_utc"]) == [_utc(2026, 9, 30, 13), _utc(2026, 9, 30, 15),
                                        _utc(2026, 9, 30, 19)]
    assert list(hourly["close"]) == [25.7, 25.54, 25.37]


class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class _Session:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def get(self, *a, **k):
        self.calls += 1
        return self.answers.pop(0)


def _tur_page():
    return DAY.replace('["RWX","NYSEARCA"]', '["TUR","NASDAQ"]')


def test_a_server_error_is_retried_then_answered(monkeypatch):
    monkeypatch.setattr(google.time, "sleep", lambda s: None)
    session = _Session([_Resp(503), _Resp(200, _tur_page())])
    assert len(google.fetch_full_history("TUR", "30min", 1, session=session)) == 3
    assert session.calls == 2


def test_a_404_is_not_retried(monkeypatch):
    monkeypatch.setattr(google.time, "sleep", lambda s: None)
    session = _Session([_Resp(404)])
    with pytest.raises(ExchangeError, match="404"):
        google.fetch_full_history("TUR", "30min", 1, session=session)
    assert session.calls == 1

import lzma
import struct
from datetime import date, datetime, timezone

import pytest

from price_monitor import dukascopy
from price_monitor.models import ExchangeError


def encode(rows, level=1):
    """A .bi5 body: LZMA-alone over 24-byte big-endian records."""
    body = b"".join(dukascopy.RECORD.pack(*r) for r in rows)
    return lzma.compress(body, format=lzma.FORMAT_ALONE)


def month_start(y, m):
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())


class FakeResponse:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


class FakeSession:
    """Answers by URL suffix, and records what was asked for."""
    def __init__(self, table, default=404):
        self.table = table
        self.default = default
        self.urls = []

    def get(self, url, timeout=None, headers=None):
        self.urls.append(url)
        for key, value in self.table.items():
            if key in url:
                return value if isinstance(value, FakeResponse) \
                    else FakeResponse(200, value)
        return FakeResponse(self.default)


def test_the_yen_uses_a_different_point_than_everything_else():
    # Measured: in June 2013 USD/JPY's integers run 94085..100672, which is
    # 94.085 and not 0.94085. One scale for every pair puts the yen a thousand
    # times too low.
    assert dukascopy.point_for("USDJPY") == 1e-3
    assert dukascopy.point_for("EURUSD") == 1e-5
    assert dukascopy.point_for("USDCNH") == 1e-5


def test_a_yen_month_decodes_to_a_yen_price():
    payload = encode([(0, 100672, 100500, 100400, 100700, 12.0)])
    rows = dukascopy.parse_month(payload, 2013, 6, dukascopy.point_for("USDJPY"))
    assert rows[0][1] == pytest.approx(100.672)


def test_the_record_is_open_close_low_high_not_ohlc():
    # The archive's field order puts CLOSE second. Reading it as OHLC swaps
    # close and high, and every check that only asks whether high >= low
    # still passes.
    payload = encode([(0, 120000, 121000, 119000, 122000, 5.0)])
    stamp, o, h, l, c, v = dukascopy.parse_month(payload, 2015, 1, 1e-5)[0]
    assert (o, h, l, c) == pytest.approx((1.20, 1.22, 1.19, 1.21))


def test_the_month_in_the_url_is_zero_indexed():
    # January is 00. Off by one here reads January as February and shifts a
    # whole archive by a month while every file still parses.
    session = FakeSession({})
    dukascopy.fetch_month("EURUSD", 2015, 1, "BID", session)
    assert "/2015/00/BID_candles_hour_1.bi5" in session.urls[0]


def test_timestamps_are_counted_from_the_start_of_the_month():
    payload = encode([(0, 120000, 120000, 120000, 120000, 1.0),
                      (7200, 120000, 120000, 120000, 120000, 1.0)])
    rows = dukascopy.parse_month(payload, 2015, 3, 1e-5)
    assert rows[0][0] == month_start(2015, 3)
    assert rows[1][0] == month_start(2015, 3) + 7200


def test_a_truncated_file_is_an_error_rather_than_a_silent_short_read():
    body = dukascopy.RECORD.pack(0, 1, 1, 1, 1, 1.0)[:-3]
    with pytest.raises(ExchangeError):
        dukascopy.parse_month(lzma.compress(body, format=lzma.FORMAT_ALONE),
                              2015, 1, 1e-5)


def test_a_missing_month_is_none_and_an_empty_one_is_empty():
    assert dukascopy.fetch_month("EURUSD", 1999, 1, "BID",
                                 FakeSession({}, default=404)) is None
    empty = FakeSession({"1999": FakeResponse(200, b"")})
    assert dukascopy.fetch_month("EURUSD", 1999, 1, "BID", empty) == []


def test_rate_limiting_is_retried_and_not_read_as_absence(monkeypatch):
    # A 503 is the limiter, not the archive declining to hold the month.
    # Treating it as absence would truncate a pair's history at whatever month
    # the limiter first bit.
    monkeypatch.setattr(dukascopy.time, "sleep", lambda *_: None)
    calls = {"n": 0}
    payload = encode([(0, 120000, 120000, 120000, 120000, 3.0)])

    class Flaky:
        def get(self, url, timeout=None, headers=None):
            calls["n"] += 1
            if calls["n"] < 3:
                return FakeResponse(503)
            return FakeResponse(200, payload)

    rows = dukascopy.fetch_month("EURUSD", 2015, 1, "BID", Flaky())
    assert calls["n"] == 3 and len(rows) == 1


def test_it_gives_up_loudly_rather_than_returning_a_short_history(monkeypatch):
    monkeypatch.setattr(dukascopy.time, "sleep", lambda *_: None)

    class Always503:
        def get(self, url, timeout=None, headers=None):
            return FakeResponse(503)

    with pytest.raises(ExchangeError):
        dukascopy.fetch_month("EURUSD", 2015, 1, "BID", Always503())


def _sides(bid_rows, ask_rows):
    return FakeSession({"BID_candles": encode(bid_rows),
                        "ASK_candles": encode(ask_rows)})


def test_filler_hours_are_dropped_rather_than_stored_as_flat_bars(monkeypatch):
    # The archive emits a record for every hour of the month including the ones
    # the market was shut: volume 0, OHLC all the last traded price. Merging
    # those writes exactly-zero returns into the store, which DEFLATES the
    # volatility the severity ladder is fitted to.
    monkeypatch.setattr(dukascopy.time, "sleep", lambda *_: None)
    traded = (3600, 120000, 121000, 119500, 121500, 8.0)
    filler = (7200, 121000, 121000, 121000, 121000, 0.0)
    session = _sides([traded, filler], [traded, filler])
    candles = dukascopy.fetch_history("EURUSD", date(2015, 1, 1), date(2015, 2, 1),
                                      session, request_delay_seconds=0)
    assert len(candles) == 1
    assert candles[0].open_time == month_start(2015, 1) + 3600


def test_bid_and_ask_are_folded_to_the_mid(monkeypatch):
    # Bid alone sits half a spread low and puts a systematic step at the seam
    # where this archive meets what is already stored.
    monkeypatch.setattr(dukascopy.time, "sleep", lambda *_: None)
    session = _sides([(0, 120000, 120000, 120000, 120000, 1.0)],
                     [(0, 120060, 120060, 120060, 120060, 1.0)])
    candles = dukascopy.fetch_history("EURUSD", date(2015, 1, 1), date(2015, 2, 1),
                                      session, request_delay_seconds=0)
    assert candles[0].close == pytest.approx(1.2003)


def test_an_hour_only_one_side_holds_is_skipped(monkeypatch):
    monkeypatch.setattr(dukascopy.time, "sleep", lambda *_: None)
    session = _sides([(0, 120000, 120000, 120000, 120000, 1.0),
                      (3600, 120000, 120000, 120000, 120000, 1.0)],
                     [(0, 120060, 120060, 120060, 120060, 1.0)])
    candles = dukascopy.fetch_history("EURUSD", date(2015, 1, 1), date(2015, 2, 1),
                                      session, request_delay_seconds=0)
    assert len(candles) == 1


def test_volume_is_stored_as_zero_like_every_other_pair(monkeypatch):
    monkeypatch.setattr(dukascopy.time, "sleep", lambda *_: None)
    session = _sides([(0, 120000, 120000, 120000, 120000, 42.0)],
                     [(0, 120000, 120000, 120000, 120000, 42.0)])
    candles = dukascopy.fetch_history("EURUSD", date(2015, 1, 1), date(2015, 2, 1),
                                      session, request_delay_seconds=0)
    assert candles[0].volume == 0.0


def test_months_below_the_known_floor_are_not_asked_for(monkeypatch):
    # Two requests a month a pair, all of them 404s, for every month below the
    # first the symbol holds.
    monkeypatch.setattr(dukascopy.time, "sleep", lambda *_: None)
    session = _sides([(0, 620000, 620000, 620000, 620000, 1.0)],
                     [(0, 620000, 620000, 620000, 620000, 1.0)])
    dukascopy.fetch_history("USDCNH", date(2011, 1, 1), date(2012, 6, 1),
                            session, request_delay_seconds=0)
    asked = {u.split("/USDCNH/")[1].rsplit("/", 1)[0] for u in session.urls}
    assert asked == {"2012/03", "2012/04"}   # April and May 2012; floor is 2012-04
    assert not any("/2011/" in u for u in session.urls)


def test_the_range_end_is_exclusive(monkeypatch):
    monkeypatch.setattr(dukascopy.time, "sleep", lambda *_: None)
    rows = [(0, 120000, 120000, 120000, 120000, 1.0)]
    session = _sides(rows, rows)
    assert dukascopy.fetch_history("EURUSD", date(2015, 2, 1), date(2015, 2, 1),
                                   session, request_delay_seconds=0) == []


def test_the_month_the_exclusive_end_lands_on_is_not_fetched(monkeypatch):
    # Every row of it would be filtered out again; that is two requests a pair
    # for nothing, and it is the same trap the FXCM reader has for years.
    monkeypatch.setattr(dukascopy.time, "sleep", lambda *_: None)
    rows = [(0, 120000, 120000, 120000, 120000, 1.0)]
    session = _sides(rows, rows)
    dukascopy.fetch_history("EURUSD", date(2015, 1, 1), date(2015, 2, 1),
                            session, request_delay_seconds=0)
    assert not any("/2015/01/" in u for u in session.urls)


def test_every_basket_pair_has_a_symbol_and_a_probed_floor():
    for ticker, symbol in dukascopy.SYMBOLS.items():
        assert dukascopy.first_month(symbol) is not None, ticker

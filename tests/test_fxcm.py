import gzip
from datetime import date, datetime, timezone

import pytest

from price_monitor import fxcm
from price_monitor.models import ExchangeError

HEADER = ("DateTime,BidOpen,BidHigh,BidLow,BidClose,"
          "AskOpen,AskHigh,AskLow,AskClose")


def week_bytes(rows):
    return gzip.compress(("\n".join([HEADER] + rows) + "\n").encode())


SAMPLE = week_bytes([
    "03/08/2015 21:00:00.000,1.08300,1.08344,1.08265,1.08300,"
    "1.08320,1.08364,1.08285,1.08320",
    "03/08/2015 22:00:00.000,1.08300,1.08407,1.08226,1.08383,"
    "1.08320,1.08427,1.08246,1.08403",
])


class FakeSession:
    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def get(self, url, **kw):
        self.urls.append(url)
        body = self.pages.get(url)
        return type("R", (), {
            "status_code": 404 if body is None else 200,
            "content": body or b"",
        })()


def test_bid_and_ask_are_folded_to_the_mid():
    candles = fxcm.parse_week(SAMPLE)
    assert len(candles) == 2
    assert candles[0].open == pytest.approx((1.08300 + 1.08320) / 2)
    assert candles[0].high == pytest.approx((1.08344 + 1.08364) / 2)
    assert candles[0].low == pytest.approx((1.08265 + 1.08285) / 2)
    assert candles[0].close == pytest.approx((1.08300 + 1.08320) / 2)


def test_timestamps_are_read_as_utc():
    # Measured, not assumed: every offset from -6h to +6h was tried against the
    # bars already stored and zero won on all three pairs tested. Reading these
    # as a local time is the classic way to ruin an FX archive.
    first = fxcm.parse_week(SAMPLE)[0]
    assert first.open_time == int(
        datetime(2015, 3, 8, 21, tzinfo=timezone.utc).timestamp())
    assert first.close_time == first.open_time + 3600


def test_volume_stays_zero_for_spot_fx():
    # No provider has a consolidated exchange volume for spot FX, and the pairs
    # already stored carry 0.0. A different convention before 2020 than after
    # would be worse than no volume at all.
    assert all(c.volume == 0.0 for c in fxcm.parse_week(SAMPLE))


def test_unparsable_rows_are_skipped_not_fatal():
    payload = week_bytes([
        "not a timestamp,1,1,1,1,1,1,1,1",
        "03/08/2015 21:00:00.000,1.1,1.1,1.1,1.1,1.1,1.1,1.1,1.1",
        ",,,,,,,,",
    ])
    assert len(fxcm.parse_week(payload)) == 1


def test_candles_come_back_in_order():
    payload = week_bytes([
        "03/08/2015 23:00:00.000,1.2,1.2,1.2,1.2,1.2,1.2,1.2,1.2",
        "03/08/2015 21:00:00.000,1.1,1.1,1.1,1.1,1.1,1.1,1.1,1.1",
    ])
    times = [c.open_time for c in fxcm.parse_week(payload)]
    assert times == sorted(times)


def test_a_missing_week_is_none_rather_than_an_error():
    # Their week numbering is a Sunday-start trading week that does not follow
    # from a formula - 2015 has a week 1, 2020 does not - so a miss is ordinary
    # and must be distinguishable from an empty file.
    session = FakeSession({})
    assert fxcm.fetch_week("EURUSD", 2020, 1, session) is None


def test_a_real_failure_still_raises():
    class Broken:
        def get(self, url, **kw):
            return type("R", (), {"status_code": 500, "content": b""})()

    with pytest.raises(ExchangeError):
        fxcm.fetch_week("EURUSD", 2015, 1, Broken())


def test_history_walks_weeks_and_filters_to_the_range():
    url = f"{fxcm.BASE_URL}/H1/EURUSD/2015/10.csv.gz"
    session = FakeSession({url: SAMPLE})
    got = fxcm.fetch_history("EURUSD", date(2015, 1, 1), date(2016, 1, 1), session)

    assert len(got) == 2
    assert len(session.urls) == fxcm.MAX_WEEK       # every week of 2015, once
    assert not any("/2016/" in u for u in session.urls)


def test_an_exclusive_end_does_not_drag_in_a_whole_extra_year():
    # A range ending on the 1st of January otherwise walks a year that cannot
    # contribute a single bar - fifty-three requests for nothing, per pair.
    session = FakeSession({})
    fxcm.fetch_history("EURUSD", date(2015, 1, 1), date(2016, 1, 1), session)
    assert len(session.urls) == fxcm.MAX_WEEK

    session = FakeSession({})
    fxcm.fetch_history("EURUSD", date(2015, 1, 1), date(2016, 1, 2), session)
    assert len(session.urls) == 2 * fxcm.MAX_WEEK


def test_history_drops_bars_outside_the_window():
    url = f"{fxcm.BASE_URL}/H1/EURUSD/2015/10.csv.gz"
    session = FakeSession({url: SAMPLE})
    assert fxcm.fetch_history("EURUSD", date(2015, 6, 1), date(2015, 7, 1),
                              session) == []


def test_an_empty_or_backwards_window_asks_for_nothing():
    session = FakeSession({})
    assert fxcm.fetch_history("EURUSD", date(2016, 1, 1), date(2015, 1, 1),
                              session) == []
    assert session.urls == []


def test_only_the_pairs_the_archive_carries_are_mapped():
    assert fxcm.symbol_for("EUR/USD") == "EURUSD"
    # USD/CNY is absent on purpose: the archive does not carry it under any
    # spelling, and offshore USD/CNH is a different instrument, not a
    # substitute to be slipped in quietly.
    assert fxcm.symbol_for("USD/CNY") is None
    assert len(fxcm.SYMBOLS) == 7

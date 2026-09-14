"""The exchange's own VIX history, and the union of it with FRED's mirror."""
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from tremor import cboe, fred

DAY = 86400


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, timeout, headers):
        self.calls.append(url)
        return self.response


def csv(rows):
    body = "DATE,OPEN,HIGH,LOW,CLOSE\n"
    return body + "".join(f"{d},1,1,1,{c}\n" for d, c in rows)


def at(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


# --- when a close may be used -----------------------------------------------

def test_the_close_is_available_the_same_evening():
    # The whole reason this module exists. VIX settles at 16:15 in Chicago and
    # CBOE posts it minutes later, so Friday's close is Friday's news - where
    # FRED's mirror does not carry it until Monday.
    when = at(cboe.available_at(date(2026, 9, 11)))
    assert when.date() == date(2026, 9, 11)
    assert when.hour == cboe.PUBLICATION_HOUR_UTC


def test_the_close_is_not_available_before_it_settles():
    # Both sides of the change to daylight saving, because that is where an
    # off-by-one-hour gate hides: 16:15 Eastern is 20:15 UTC in July and 21:15
    # in January, and a gate written from the WRONG clock passes the summer test
    # and hands January a number nobody had. This caught exactly that.
    for day in (date(2026, 1, 15), date(2026, 7, 15)):
        settles = pd.Timestamp(day, tz="America/New_York") + pd.Timedelta(hours=16,
                                                                         minutes=15)
        assert at(cboe.available_at(day)) > settles


def test_it_is_earlier_than_the_mirror_on_every_day_of_the_week():
    for day in pd.date_range("2026-09-07", "2026-09-11").date:
        assert cboe.available_at(day) < fred.available_at(day)


# --- reading the file -------------------------------------------------------

def test_the_history_is_returned_in_freds_columns():
    # Nothing downstream should be able to tell which source it was handed.
    session = FakeSession(FakeResponse(200, csv([("09/10/2026", "17.84"),
                                                 ("09/11/2026", "15.84")])))
    out = cboe.fetch_vix_history(date(1990, 1, 1), session=session)

    assert list(out.columns) == ["day", "close", "available_at"]
    assert out["day"].dtype == "int64" and out["close"].dtype == "float64"
    assert out["close"].tolist() == [17.84, 15.84]
    assert out["day"].iloc[1] == epoch(date(2026, 9, 11))


def test_rows_before_the_start_are_dropped():
    # The endpoint serves 1990 onwards in one response and takes no range, so
    # the trim happens here or not at all.
    session = FakeSession(FakeResponse(200, csv([("01/02/1990", "17.24"),
                                                 ("09/10/2026", "17.84")])))
    out = cboe.fetch_vix_history(date(2020, 1, 1), session=session)
    assert len(out) == 1 and out["close"].iloc[0] == 17.84


def test_an_unparseable_row_is_skipped_rather_than_poisoning_the_series():
    session = FakeSession(FakeResponse(200, csv([("09/10/2026", "17.84"),
                                                 ("09/11/2026", "n/a"),
                                                 ("not a date", "1.0")])))
    out = cboe.fetch_vix_history(date(1990, 1, 1), session=session)
    assert out["close"].tolist() == [17.84]


def test_a_bad_status_is_an_error_and_not_an_empty_series():
    # An empty frame here would silently erase the gauge; the caller has a
    # second source and must be told to fall back on it.
    session = FakeSession(FakeResponse(503, "down"))
    with pytest.raises(cboe.CboeError):
        cboe.fetch_vix_history(date(1990, 1, 1), session=session)


def test_columns_that_are_not_the_expected_ones_are_an_error():
    session = FakeSession(FakeResponse(200, "a,b\n1,2\n"))
    with pytest.raises(cboe.CboeError):
        cboe.fetch_vix_history(date(1990, 1, 1), session=session)


def test_nothing_in_range_is_an_error():
    session = FakeSession(FakeResponse(200, csv([("01/02/1990", "17.24")])))
    with pytest.raises(cboe.CboeError):
        cboe.fetch_vix_history(date(2020, 1, 1), session=session)


# --- the union --------------------------------------------------------------

def frame(pairs):
    return pd.DataFrame([{"day": d, "close": c, "available_at": a}
                         for d, c, a in pairs]).astype(
        {"day": "int64", "close": "float64", "available_at": "int64"})


def test_a_day_only_one_source_has_is_kept():
    # CBOE's file omits 1999-12-31 and FRED carries it; FRED has not
    # republished the newest day and CBOE has. Either alone loses something.
    out = cboe.merge(frame([(DAY, 10.0, 5)]), frame([(2 * DAY, 11.0, 9)]))
    assert out["day"].tolist() == [DAY, 2 * DAY]


def test_availability_takes_the_earliest_of_the_sources():
    # THE POINT OF THE FUNCTION. FRED republishes a value CBOE already carried,
    # so letting the later source win would push a reading's availability
    # forward: a number the note had already shown would become one the system
    # does not yet know, and the line would disappear from a correct message.
    out = cboe.merge(frame([(DAY, 10.0, 100)]), frame([(DAY, 10.0, 400)]))
    assert len(out) == 1 and out["available_at"].iloc[0] == 100
    # and the same however they are ordered
    other = cboe.merge(frame([(DAY, 10.0, 400)]), frame([(DAY, 10.0, 100)]))
    assert other["available_at"].iloc[0] == 100


def test_the_result_is_sorted_by_day():
    out = cboe.merge(frame([(3 * DAY, 12.0, 1), (DAY, 10.0, 1)]),
                     frame([(2 * DAY, 11.0, 1)]))
    assert out["day"].tolist() == [DAY, 2 * DAY, 3 * DAY]


def test_a_source_that_failed_is_simply_absent():
    out = cboe.merge(frame([(DAY, 10.0, 5)]), None, pd.DataFrame())
    assert out["day"].tolist() == [DAY]


def test_no_source_at_all_gives_an_empty_frame_with_the_right_columns():
    out = cboe.merge()
    assert out.empty and list(out.columns) == ["day", "close", "available_at"]
    assert out["day"].dtype == "int64"

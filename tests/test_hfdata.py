import io

import pandas as pd
import pytest

from price_monitor import hfdata
from price_monitor.models import ExchangeError


def parquet(rows, columns=None):
    frame = pd.DataFrame(rows) if columns is None else pd.DataFrame(rows, columns=columns)
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    return buffer.getvalue()


def bars(n=3, source="pitrading", start="2015-03-02 09:30:00"):
    stamps = pd.date_range(start, periods=n, freq="1min")
    return parquet({
        "timestamp": stamps,
        "open": [10.0] * n, "high": [11.0] * n, "low": [9.0] * n,
        "close": [10.5] * n, "volume": [100.0] * n,
        "source": [source] * n,
    })


class FakeSession:
    def __init__(self, status=200, content=b"", text=""):
        self.status, self.content, self.text, self.calls = status, content, text, []

    def get(self, url, params=None, headers=None, **kw):
        self.calls.append((url, params, headers))
        return type("R", (), {"status_code": self.status,
                              "content": self.content, "text": self.text})()


def test_the_key_travels_in_the_header_not_the_query():
    session = FakeSession(content=b"x")
    hfdata.fetch_parquet("SPY", "secret", session)
    url, params, headers = session.calls[0]
    assert headers["X-API-Key"] == "secret"
    assert "secret" not in url and "secret" not in str(params)


def test_a_rejected_key_says_so():
    with pytest.raises(ExchangeError, match="rejected the API key"):
        hfdata.fetch_parquet("SPY", "bad", FakeSession(status=401))


def test_no_key_is_refused_before_any_request():
    session = FakeSession()
    with pytest.raises(ExchangeError, match="No HF Data API key"):
        hfdata.fetch_parquet("SPY", "", session)
    assert session.calls == []


def test_only_consolidated_tape_bars_survive():
    # The library switches to IEX-only in March 2022, about 2-3% of consolidated
    # volume. Splicing across that boundary would drop every ETF's volume by
    # ~97% on a fixed date, and the volume profile is built on that series.
    mixed = pd.concat([
        pd.read_parquet(io.BytesIO(bars(2, "pitrading"))),
        pd.read_parquet(io.BytesIO(bars(3, "iex", "2023-03-02 09:30:00"))),
    ])
    buffer = io.BytesIO()
    mixed.to_parquet(buffer, index=False)

    out = hfdata.to_minute_frame(buffer.getvalue(), "UTC")
    assert len(out) == 2


def test_a_file_without_a_source_column_is_an_error_not_a_guess():
    # Without it there is no way to tell full-tape bars from IEX ones, and
    # defaulting either way silently corrupts volume.
    payload = parquet({"timestamp": pd.date_range("2015-03-02", periods=2, freq="1min"),
                       "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
                       "close": [1.0, 1.0], "volume": [1.0, 1.0]})
    with pytest.raises(ExchangeError, match="no source column"):
        hfdata.to_minute_frame(payload, "UTC")


def test_a_missing_price_column_raises_rather_than_defaulting():
    payload = parquet({"timestamp": pd.date_range("2015-03-02", periods=2, freq="1min"),
                       "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
                       "close": [1.0, 1.0], "source": ["pitrading"] * 2})
    with pytest.raises(ExchangeError, match="no volume column"):
        hfdata.to_minute_frame(payload, "UTC")


def test_naive_timestamps_are_localised_to_the_given_zone():
    # Reading Eastern as UTC shifts four months of every year by an hour, which
    # looks like noise rather than like an error - the mistake that made
    # HistData's archive score 0.53 correlation instead of 0.94.
    utc = hfdata.to_minute_frame(bars(1), "UTC")["hour_utc"].iloc[0]
    eastern = hfdata.to_minute_frame(bars(1), "US/Eastern")["hour_utc"].iloc[0]
    assert eastern - utc == 5 * 3600        # 2015-03-02 is still EST


def test_the_frame_matches_the_store_schema():
    out = hfdata.to_minute_frame(bars(3), "UTC")
    assert list(out.columns) == ["hour_utc", "open", "high", "low", "close",
                                 "volume", "n_src"]
    # to_hourly sums n_src, so one row per minute bar is what it has to be
    assert (out["n_src"] == 1).all()


def test_minute_bars_fold_into_the_hourly_grid():
    from meals import bars as store

    minutes = hfdata.to_minute_frame(bars(90, start="2015-03-02 14:00:00"), "UTC")
    hourly = store.to_hourly(minutes)
    assert len(hourly) == 2                       # 14:00 and 15:00
    assert hourly["volume"].sum() == 90 * 100.0   # nothing lost or doubled


def test_describe_reports_what_the_file_actually_holds():
    # The schema is undocumented, and this project has been bitten twice this
    # week by a source whose shape was assumed rather than read.
    out = hfdata.describe(bars(4))
    assert out["rows"] == 4
    assert out["timestamp_column"] == "timestamp"
    assert out["source_column"] == "source"
    assert out["source_counts"] == {"pitrading": 4}
    assert "first_timestamp" in out and "last_timestamp" in out


def test_an_all_iex_file_yields_nothing_rather_than_bad_bars():
    assert hfdata.to_minute_frame(bars(5, "iex"), "UTC").empty

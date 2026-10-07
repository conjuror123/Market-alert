"""jump/opens.py: the official opens table."""
from datetime import date, datetime, timezone

import pytest

from jump import opens
from price_monitor.models import Candle


def daily(day, o, h, l, c):
    t = int(datetime.fromisoformat(f"{day} 13:30").replace(tzinfo=timezone.utc).timestamp())
    return Candle(open_time=t, open=o, high=h, low=l, close=c, volume=1.0, close_time=t + 86400)


def test_each_day_is_its_open_over_the_close_before_it_and_a_flat_day_is_no_open():
    rows = opens.from_candles("GLD", [daily("2021-08-04", 170, 171, 169, 169.5),
                                      daily("2021-08-05", 169.4, 169.4, 169.4, 169.4),
                                      daily("2021-08-06", 165.84, 166, 164.5, 165.0)])
    assert [r["day"] for r in rows] == ["2021-08-06"]      # 08-05 is flat
    assert rows[0]["open"] == 165.84 and rows[0]["prev_close"] == 169.4


def test_finished_years_are_parquet_this_years_months_csv_and_nothing_is_overwritten(tmp_path):
    d = str(tmp_path)
    today = date(2026, 10, 7)
    rows = [{"ticker": "GLD", "day": "2025-12-31", "open": 1.0, "prev_close": 1.0},
            {"ticker": "GLD", "day": "2026-10-06", "open": 2.0, "prev_close": 1.0}]
    assert opens.merge(rows, d, today) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2025.parquet", "2026-10.csv"]
    assert opens.merge([{"ticker": "GLD", "day": "2026-10-06", "open": 9.0,
                         "prev_close": 1.0}], d, today) == 0
    assert opens.load(d)["GLD"] == {"2025-12-31": 1.0, "2026-10-06": 2.0}
    # A new year folds the old one's months into its Parquet file.
    assert opens.merge([], d, date(2027, 1, 4)) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2025.parquet", "2026.parquet"]
    assert opens.load(d)["GLD"]["2026-10-06"] == pytest.approx(2.0)

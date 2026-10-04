import pandas as pd
import pytest

from tremor import bars
from price_monitor.models import Candle

HOUR = 3600


def frame(rows):
    """rows: (hour_utc, open, high, low, close, volume, n_src)"""
    return pd.DataFrame(rows, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)


def test_to_hourly_folds_half_hour_bars_onto_the_round_hour():
    # The ETF grid runs on the :30, so the two half-hourly bars 10:30 and 11:00
    # belong to DIFFERENT hours, while 11:00 and 11:30 belong to the same one.
    src = frame([
        (11 * HOUR + 1800, 1.0, 4.0, 0.5, 2.0, 10.0, 1),
        (12 * HOUR, 2.0, 3.0, 1.5, 2.5, 20.0, 1),
        (12 * HOUR + 1800, 2.5, 9.0, 2.0, 7.0, 30.0, 1),
    ])
    out = bars.to_hourly(src)

    assert list(out["hour_utc"]) == [11 * HOUR, 12 * HOUR]
    full = out.iloc[1]
    assert full["open"] == 2.0 and full["close"] == 7.0
    assert full["high"] == 9.0 and full["low"] == 1.5
    assert full["volume"] == 50.0
    assert full["n_src"] == 2
    # An hour assembled from a single half-hourly bar is the first bar of the
    # session. It can only be told apart by n_src.
    assert out.iloc[0]["n_src"] == 1


def test_to_hourly_is_identity_for_series_already_on_the_hour():
    src = frame([(HOUR, 1.0, 2.0, 0.5, 1.5, 7.0, 1),
                 (2 * HOUR, 1.5, 2.5, 1.0, 2.0, 8.0, 1)])
    out = bars.to_hourly(src)
    pd.testing.assert_frame_equal(out, src)


def test_to_hourly_orders_by_time_not_by_input_order():
    # Sources serve bars newest first; open and close must be taken by time, not
    # by row order in the response.
    src = frame([(HOUR + 1800, 5.0, 6.0, 4.0, 5.5, 1.0, 1),
                 (HOUR, 1.0, 2.0, 0.5, 1.5, 1.0, 1)])
    out = bars.to_hourly(src)
    assert out.iloc[0]["open"] == 1.0
    assert out.iloc[0]["close"] == 5.5


def test_to_hourly_on_empty_input_returns_typed_empty_frame():
    out = bars.to_hourly(bars.empty_frame())
    assert out.empty
    assert list(out.columns) == list(bars.SCHEMA)


def test_merge_is_idempotent(tmp_path):
    path = bars.store_path(str(tmp_path), "x")
    data = frame([(HOUR, 1.0, 2.0, 0.5, 1.5, 7.0, 1)])

    assert bars.merge(path, data) == 1
    assert bars.merge(path, data) == 0
    assert len(bars.load(path)) == 1


def test_merge_lets_a_revised_bar_win(tmp_path):
    # The vendor may revise a bar - the fresher version is more trustworthy, but
    # it does not count as a new row.
    path = bars.store_path(str(tmp_path), "x")
    bars.merge(path, frame([(HOUR, 1.0, 2.0, 0.5, 1.5, 7.0, 1)]))
    added = bars.merge(path, frame([(HOUR, 1.0, 2.0, 0.5, 9.9, 7.0, 1)]))

    assert added == 0
    assert bars.load(path).iloc[0]["close"] == 9.9


def test_merge_deduplicates_a_repeated_block(tmp_path):
    # Exactly the situation found in the accumulated NDJSON history: a bad branch
    # merge duplicated a continuous block of hours.
    path = bars.store_path(str(tmp_path), "x")
    block = [(h * HOUR, 1.0, 2.0, 0.5, 1.5, 7.0, 1) for h in range(1, 6)]
    assert bars.merge(path, frame(block + block)) == 5
    assert list(bars.load(path)["hour_utc"]) == [h * HOUR for h in range(1, 6)]


def test_merge_keeps_the_store_sorted(tmp_path):
    path = bars.store_path(str(tmp_path), "x")
    bars.merge(path, frame([(3 * HOUR, 1.0, 1.0, 1.0, 1.0, 0.0, 1)]))
    bars.merge(path, frame([(HOUR, 1.0, 1.0, 1.0, 1.0, 0.0, 1)]))
    assert list(bars.load(path)["hour_utc"]) == [HOUR, 3 * HOUR]


def test_load_missing_file_returns_typed_empty_frame(tmp_path):
    out = bars.load(bars.store_path(str(tmp_path), "no-such-thing"))
    assert out.empty
    assert out["hour_utc"].dtype == "int64"


def test_candles_to_frame_preserves_the_open_time_convention():
    # hour_utc is the bar's OPENING moment, the same convention as
    # open_time in candle_store, so the import needs no shift.
    candles = [Candle(open_time=HOUR, open=1.0, high=2.0, low=0.5, close=1.5,
                      volume=3.0, close_time=2 * HOUR)]
    out = bars.candles_to_frame(candles)
    assert out.iloc[0]["hour_utc"] == HOUR
    assert out.iloc[0]["close"] == 1.5


def test_candles_to_frame_on_empty_list():
    assert bars.candles_to_frame([]).empty


def test_to_hourly_does_not_double_volume_on_a_repeated_bar():
    # The same hour arriving twice is one bar, not two. Without dropping
    # duplicates before aggregation the volume would be summed and doubled:
    # exactly what would have happened on the history where a branch merge
    # duplicated a block of hours.
    src = frame([(HOUR, 1.0, 2.0, 0.5, 1.5, 453.89, 1),
                 (HOUR, 1.0, 2.0, 0.5, 1.5, 453.89, 1)])
    out = bars.to_hourly(src)

    assert len(out) == 1
    assert out.iloc[0]["volume"] == 453.89


def test_to_hourly_prefers_the_later_copy_of_a_repeated_bar():
    # The earlier copy may have caught the hour still open - narrower in both
    # volume and range. The later one is either equivalent or more complete.
    src = frame([(HOUR, 78175.92, 78179.52, 78091.18, 78118.19, 36.77, 1),
                 (HOUR, 78175.92, 78179.52, 77969.24, 78076.91, 174.29, 1)])
    out = bars.to_hourly(src)

    assert len(out) == 1
    assert out.iloc[0]["close"] == 78076.91
    assert out.iloc[0]["volume"] == 174.29
    assert out.iloc[0]["low"] == 77969.24


# --- the store is sharded by year ------------------------------------------

def _hour(year, month=1, day=1, hour=0):
    from datetime import datetime, timezone
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


def _rows(hours):
    return pd.DataFrame({
        "hour_utc": hours, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
        "volume": 10.0, "n_src": 2,
    })


def test_a_settled_year_lands_in_one_file_and_the_live_one_in_months(tmp_path):
    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    bars.write(store, _rows([_hour(2003), _hour(2003, 6), _hour(2004),
                             _hour(2026, 1), _hour(2026, 9)]))

    import os
    assert sorted(os.listdir(store)) == [
        "2003.parquet", "2004.parquet", "2026-01.csv", "2026-09.open.csv"]
    assert len(bars.load(store)) == 5
    assert not any(name.endswith(".tmp") for name in os.listdir(store))


def test_a_load_from_an_hour_on_reads_only_the_shards_that_can_hold_it(tmp_path, monkeypatch):
    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    hours = [_hour(2003), _hour(2025, 3), _hour(2025, 11), _hour(2026, 1), _hour(2026, 8, 15),
             _hour(2026, 9)]
    bars.write(store, _rows(hours))
    read = []
    real = bars._read_shard
    monkeypatch.setattr(bars, "_read_shard", lambda path: read.append(path) or real(path))
    got = bars.load(store, since=_hour(2026, 8, 10))
    assert list(got["hour_utc"]) == [_hour(2026, 8, 15), _hour(2026, 9)]
    assert sorted(p.rsplit("/", 1)[1] for p in read) == ["2026-08.open.csv", "2026-09.open.csv"]
    # A year's shard is read when the hour falls inside it.
    assert list(bars.load(store, since=_hour(2025, 6))["hour_utc"]) == hours[2:]


def test_a_settled_year_is_not_rewritten_when_a_new_hour_arrives(tmp_path):
    # The whole reason the store is sharded: parquet rewrites a file whole, and
    # settled months are committed. A year whose bars are long finished must
    # produce no file write at all, or git stores the entire history again to
    # record one hour.
    import os

    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    bars.write(store, _rows([_hour(2003), _hour(2026)]))
    settled = os.path.join(store, "2003.parquet")
    stamp = os.stat(settled).st_mtime_ns

    bars.merge(store, _rows([_hour(2026, 1, 1, 5)]))

    assert os.stat(settled).st_mtime_ns == stamp
    assert len(bars.load(store)) == 3


def test_a_legacy_single_file_is_read_and_then_folded_in(tmp_path):
    # The migration is the ordinary write path rather than a script somebody has
    # to remember to run: the first merge reads the old file, lays the union down
    # as shards and removes it.
    import os

    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    legacy = f"{store}.parquet"
    os.makedirs(str(tmp_path), exist_ok=True)
    bars.write(legacy, _rows([_hour(2003), _hour(2004)]))

    assert len(bars.load(store)) == 2
    bars.merge(store, _rows([_hour(2026)]))

    assert not os.path.exists(legacy)
    assert sorted(os.listdir(store)) == [
        "2003.parquet", "2004.parquet", "2026-01.open.csv"]
    assert sorted(bars.load(store)["hour_utc"]) == [_hour(2003), _hour(2004), _hour(2026)]


def test_a_revised_bar_wins_over_the_stored_one(tmp_path):
    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    bars.write(store, _rows([_hour(2026)]))
    revised = _rows([_hour(2026)])
    revised.loc[0, "close"] = 99.0

    added = bars.merge(store, revised)

    assert added == 0
    assert bars.load(store)["close"].iloc[0] == 99.0


def test_a_year_that_lost_its_bars_loses_its_shard(tmp_path):
    import os

    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    bars.write(store, _rows([_hour(2003), _hour(2026)]))
    bars.write(store, _rows([_hour(2026)]))

    assert os.listdir(store) == ["2026-01.open.csv"]


def test_appending_an_hour_rewrites_only_the_month_it_lands_in(tmp_path):
    # THE WHOLE POINT OF THE SHARD SIZE. Git cannot delta parquet, so a commit
    # stores every byte of whatever file changed, and the only number that
    # matters is how much history shares a shard with the new hour. A year-only
    # scheme re-commits the whole year to date on every write; because that
    # shard grows all year the annual cost is 183 times one complete year, not
    # 365 daily deltas. Measured on the real store, month shards cut the bytes
    # rewritten per append by 11.4x.
    import os

    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    bars.write(store, _rows([_hour(2025, 6), _hour(2026, 1), _hour(2026, 6),
                             _hour(2026, 9)]))
    stamps = {name: os.stat(os.path.join(store, name)).st_mtime_ns
              for name in os.listdir(store)}

    bars.merge(store, _rows([_hour(2026, 9, 2)]))

    touched = {name for name in os.listdir(store)
               if stamps.get(name) != os.stat(os.path.join(store, name)).st_mtime_ns}
    assert touched == {"2026-09.open.csv"}


def test_a_year_folds_into_parquet_once_its_december_has_settled(tmp_path):
    # Months while the year is written, one Parquet file once it is finished -
    # the form a year is smallest and fastest in, and a store that does not
    # gain twelve files a year for load() to open.
    import os

    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    bars.write(store, _rows([_hour(2025, 6), _hour(2026, 1), _hour(2026, 6),
                             _hour(2026, 11)]))
    assert sorted(os.listdir(store)) == [
        "2025.parquet", "2026-01.csv", "2026-06.csv", "2026-11.open.csv"]

    bars.merge(store, _rows([_hour(2027, 1, 1, 9)]))
    assert sorted(os.listdir(store)) == [
        "2025.parquet", "2026-01.csv", "2026-06.csv", "2026-11.csv", "2027-01.open.csv"]

    bars.merge(store, _rows([_hour(2027, 1, 8, 0)]))
    assert sorted(os.listdir(store)) == ["2025.parquet", "2026.parquet", "2027-01.open.csv"]
    assert sorted(bars.load(store)["hour_utc"]) == [
        _hour(2025, 6), _hour(2026, 1), _hour(2026, 6), _hour(2026, 11),
        _hour(2027, 1, 1, 9), _hour(2027, 1, 8, 0)]


def test_a_folded_year_is_never_split_back_into_months(tmp_path):
    # A store that lost its open months has the folded year as its newest data;
    # read off the clock alone, December would be open again.
    import os

    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    os.makedirs(store)
    bars.write(os.path.join(store, "2026.parquet"), _rows([_hour(2026, 6), _hour(2026, 12, 30)]))

    bars.merge(store, _rows([_hour(2026, 12, 31)]))

    assert os.listdir(store) == ["2026.parquet"]
    assert len(bars.load(store)) == 3


def test_a_month_shard_outranks_the_year_it_replaces(tmp_path):
    # Both exist only if a write died between laying the months down and
    # removing the year they supersede. load() keeps the LAST copy of an hour,
    # so the months have to be read second - and sorting the names as strings
    # puts them first, because "-" sorts before ".".
    import os

    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    os.makedirs(store)
    stale = _rows([_hour(2026, 3)])
    stale.loc[0, "close"] = 11.0
    bars.write(os.path.join(store, "2026.parquet"), stale)
    fresh = _rows([_hour(2026, 3)])
    fresh.loc[0, "close"] = 22.0
    bars.write(os.path.join(store, "2026-03.parquet"), fresh)

    assert bars.load(store)["close"].tolist() == [22.0]


def test_the_open_month_settles_a_week_after_it_ends(tmp_path):
    # Outside git while it is written; in git once, a week after its end, when
    # a hole an outage left in its last days has been filled.
    import os

    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    bars.write(store, _rows([_hour(2026, 9), _hour(2026, 9, 30, 20)]))
    assert sorted(os.listdir(store)) == ["2026-09.open.csv"]

    bars.merge(store, _rows([_hour(2026, 10, 1, 13)]))
    assert sorted(os.listdir(store)) == ["2026-09.open.csv", "2026-10.open.csv"]

    bars.merge(store, _rows([_hour(2026, 10, 7, 23)]))
    assert sorted(os.listdir(store)) == ["2026-09.open.csv", "2026-10.open.csv"]

    bars.merge(store, _rows([_hour(2026, 10, 8, 0)]))
    assert sorted(os.listdir(store)) == ["2026-09.csv", "2026-10.open.csv"]
    assert len(bars.load(store)) == 5


def test_settled_before_is_the_oldest_open_month():
    from datetime import datetime, timezone

    def at(*args):
        return int(datetime(*args, tzinfo=timezone.utc).timestamp())

    assert bars.settled_before(at(2026, 10, 7, 23)) == at(2026, 9, 1)
    assert bars.settled_before(at(2026, 10, 8)) == at(2026, 10, 1)
    assert bars.settled_before(at(2027, 1, 7)) == at(2026, 12, 1)
    assert bars.settled_before(at(2027, 1, 20)) == at(2027, 1, 1)


def test_a_settled_month_keeps_its_rows_and_fills_its_holes(tmp_path):
    # It is in git: a provider re-serving an old bar a hair different must not
    # rewrite it. An hour it lacks is still taken, and the tape repair, which
    # replaces bad prints on purpose, may revise.
    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    bars.write(store, _rows([_hour(2026, 8, 3), _hour(2026, 9, 10)]))
    revised = _rows([_hour(2026, 8, 3), _hour(2026, 8, 4), _hour(2026, 9, 10)])
    revised["close"] = 99.0

    added = bars.merge(store, revised)

    got = bars.load(store).set_index("hour_utc")["close"]
    assert added == 1
    assert got[_hour(2026, 8, 3)] == 1.5
    assert got[_hour(2026, 8, 4)] == 99.0
    assert got[_hour(2026, 9, 10)] == 99.0

    bars.merge(store, revised, revise_settled=True)
    assert bars.load(store).set_index("hour_utc")["close"][_hour(2026, 8, 3)] == 99.0


def test_a_committed_month_is_never_pulled_back_out_of_git(tmp_path):
    # A run that lost its open months (no release to restore them from) sees its
    # newest committed month as the newest data. Read off the clock alone, that
    # month would be open again, and the write would turn its .csv into an
    # untracked .open.csv - deleting it from git.
    import os

    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    bars.write(store, _rows([_hour(2026, 8, 3), _hour(2026, 9, 10)]))
    os.remove(os.path.join(store, "2026-09.open.csv"))

    bars.merge(store, _rows([_hour(2026, 8, 5)]))

    assert sorted(os.listdir(store)) == ["2026-08.csv"]
    assert len(bars.load(store)) == 2


def test_the_text_month_reads_back_exactly(tmp_path):
    # A float that came back a hair different would read as a move.
    import numpy as np

    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    rng = np.random.default_rng(7)
    rows = _rows([_hour(2026, 9, 1, h) for h in range(24)])
    for column in ("open", "high", "low", "close", "volume"):
        rows[column] = rng.random(24) * 10.0 ** rng.integers(-4, 6, 24)
    bars.write(store, rows)

    assert bars.load(store).equals(bars._normalise(rows))


def test_an_unchanged_text_month_is_not_rewritten(tmp_path):
    import os

    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    bars.write(store, _rows([_hour(2026, 9), _hour(2026, 9, 2)]))
    path = os.path.join(store, "2026-09.open.csv")
    stamp = os.stat(path).st_mtime_ns

    bars.merge(store, _rows([_hour(2026, 9)]))

    assert os.stat(path).st_mtime_ns == stamp


def test_a_year_already_in_parquet_keeps_its_file(tmp_path):
    import os

    store = bars.store_path(str(tmp_path), "twelvedata_SPY")
    os.makedirs(store)
    bars.write(os.path.join(store, "2025.parquet"), _rows([_hour(2025, 6)]))
    stamp = os.stat(os.path.join(store, "2025.parquet")).st_mtime_ns
    bars.merge(store, _rows([_hour(2026, 9)]))

    assert sorted(os.listdir(store)) == ["2025.parquet", "2026-09.open.csv"]
    assert os.stat(os.path.join(store, "2025.parquet")).st_mtime_ns == stamp

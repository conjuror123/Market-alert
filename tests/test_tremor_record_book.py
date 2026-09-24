"""The saved record book: trusted only when it matches, dropped when it cannot cover."""
import pandas as pd

from tremor import saed, severity


def test_a_book_round_trips(tmp_path):
    book = saed.RecordBook(None, None, snapshot_at=1000)
    state = book.state("twelvedata:SPY", severity.LEVEL_PREFIX)
    severity.record_since(pd.Series([3.0, 1.0, 2.0]), pd.Series([100, 200, 300]),
                          state=state)
    book.state("twelvedata:SPY", "abs_level").snapshot = []   # no records yet
    path = tmp_path / "book.parquet"
    book.frame("cfg").to_parquet(path)

    seeds, after = saed.load_record_book(str(path), "cfg")
    assert after == 1000
    assert seeds[("twelvedata:SPY", "level")] == [(100, 3.0), (300, 2.0)]
    assert seeds[("twelvedata:SPY", "abs_level")] == []


def test_a_book_from_another_configuration_is_not_trusted(tmp_path):
    book = saed.RecordBook(None, None, snapshot_at=1000)
    book.state("a", "level").snapshot = [(1, 1.0)]
    path = tmp_path / "book.parquet"
    book.frame("old").to_parquet(path)
    assert saed.load_record_book(str(path), "new") is None
    assert saed.load_record_book(str(tmp_path / "none.parquet"), "new") is None


def test_a_series_the_book_never_saw_is_flagged():
    book = saed.RecordBook({("a", "level"): []}, after=10, snapshot_at=20)
    assert book.state("a", "level").after == 10
    book.state("block:new", "level")
    assert book.missing == {("block:new", "level")}

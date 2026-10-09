"""tools/tape_mend.py: the funds' first bars mended from Alpaca's tape."""
from datetime import date, datetime, timezone

import pandas as pd

from jump import atomic, bars, sessions
from jump.basket import load_basket
from tools import tape_mend


def _hour(day: str, h: int) -> int:
    return int(datetime.fromisoformat(f"{day}T{h:02d}:00").replace(tzinfo=timezone.utc).timestamp())


def _day(day: str, first, rest: float) -> list:
    """A June day's session (13:30-20:00 UTC): the first bar, then flat at `rest`."""
    o, h, l, c = first
    rows = [(_hour(day, 13), o, h, l, c)]
    rows += [(_hour(day, k), rest, rest, rest, rest) for k in range(14, 20)]
    return rows


def _frame(rows) -> pd.DataFrame:
    f = pd.DataFrame(rows, columns=["hour_utc", "open", "high", "low", "close"])
    return f.assign(volume=1000.0, n_src=1).astype(bars.SCHEMA)


# (day, store's first bar, tape's first bar, the rest of the day, official open)
DAYS = [
    ("2021-06-07", (100.0, 100.0, 100.0, 100.0), (100.0, 100.0, 100.0, 100.0), 100.0, 100.0),
    # stale: opens at the previous close; the tape opened 1% lower -> mended
    ("2021-06-08", (100.0, 100.0, 98.9, 99.0), (99.0, 99.2, 98.9, 99.0), 99.0, 99.0),
    # off by 1.4%, not at the previous close -> mended, the range widened to the close
    ("2021-06-09", (99.5, 99.5, 98.0, 98.2), (98.1, 98.15, 98.0, 98.18), 98.2, 98.1),
    # a lone print the store never traded at, though the official open repeats it -> kept
    ("2021-06-10", (98.2, 98.2, 98.2, 98.2), (90.0, 98.2, 90.0, 98.2), 98.2, 90.0),
    # the first bar's closes disagree by 62 bp: not the same prices -> kept
    ("2021-06-11", (98.2, 98.2, 97.0, 97.1), (97.5, 97.6, 96.0, 96.5), 97.1, 97.5),
    # the official open sides with the store -> kept
    ("2021-06-14", (97.1, 97.1, 96.0, 96.2), (96.1, 96.3, 96.0, 96.2), 96.2, 97.1),
    # both opens ordinary trades of the half hour, 26 bp apart -> mended, but not a bond fund's
    ("2021-06-15", (96.5, 96.6, 96.0, 96.3), (96.25, 96.6, 96.0, 96.3), 96.3, 96.25),
]


def _inputs():
    store = _frame([r for d, s, _, rest, _ in DAYS for r in _day(d, s, rest)])
    tape = _frame([r for d, _, t, rest, _ in DAYS for r in _day(d, t, rest)])
    official = pd.DataFrame({"day": [date.fromisoformat(d) for d, *_ in DAYS],
                             "o": [o for *_, o in DAYS], "c": [rest for *_, rest, _ in DAYS]})
    return store, tape, official


def test_only_a_wrong_open_both_records_traded_at_is_mended():
    basket = {a.ticker: a for a in load_basket().instruments}
    store, tape, official = _inputs()
    table = sessions.load_sessions()
    mend = tape_mend.to_mend(basket["SPY"], store, tape, official, table)
    assert mend.hour_utc.tolist() == [_hour(d, 13) for d in ("2021-06-08", "2021-06-09", "2021-06-15")]
    assert mend.open.tolist() == [99.0, 98.1, 96.25]
    # A bond fund keeps an open that is an ordinary trade of the tape's half hour.
    assert basket["LQD"].block == "credit"
    mend = tape_mend.to_mend(basket["LQD"], store, tape, official, table)
    assert mend.hour_utc.tolist() == [_hour(d, 13) for d in ("2021-06-08", "2021-06-09")]


def test_the_mend_rewrites_only_the_year_it_holds_and_keeps_the_close(tmp_path):
    spy = next(a for a in load_basket().instruments if a.ticker == "SPY")
    store, tape, official = _inputs()
    path = str(tmp_path / spy.file_stem)
    tmp_path.joinpath(spy.file_stem).mkdir()
    atomic.write_parquet(f"{path}/2021.parquet", store)
    atomic.write_parquet(f"{path}/2020.parquet", _frame(_day("2020-06-08", (50.0,) * 4, 50.0)))
    (tmp_path / spy.file_stem / "2026-10.open.csv").write_text("left as it is\n")
    untouched = {n: (tmp_path / spy.file_stem / n).read_bytes() for n in ("2020.parquet", "2026-10.open.csv")}

    mend = tape_mend.to_mend(spy, store, tape, official, sessions.load_sessions())
    assert tape_mend.apply(path, mend) == 3

    for name, before in untouched.items():
        assert (tmp_path / spy.file_stem / name).read_bytes() == before
    after = pd.read_parquet(f"{path}/2021.parquet").set_index("hour_utc")
    assert after.loc[_hour("2021-06-08", 13), ["open", "high", "low", "close"]].tolist() == [99.0, 99.2, 98.9, 99.0]
    # The tape's high (98.15) is under the stored close (98.2): widened to hold it.
    assert after.loc[_hour("2021-06-09", 13), ["open", "high", "low", "close"]].tolist() == [98.1, 98.2, 98.0, 98.2]
    unchanged = store.set_index("hour_utc").drop(index=mend.hour_utc)
    pd.testing.assert_frame_equal(after.drop(index=mend.hour_utc), unchanged)

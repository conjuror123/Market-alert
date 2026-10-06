"""tools/fx_weekend_opens.py: a Sunday open stitched to Friday's close, mended."""
from datetime import datetime, timezone

import pandas as pd

from tools import fx_weekend_opens as fx

H = 3600


def ts(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def frame(rows):
    return pd.DataFrame([{"hour_utc": ts(t), "open": o, "high": max(o, c), "low": min(o, c),
                          "close": c, "volume": 0.0} for t, o, c in rows])


STORE = frame([
    ("2012-03-09 20:00", 1.3106, 1.3115),
    ("2012-03-09 21:00", 1.3115, 1.3122),     # Friday's last bar
    ("2012-03-11 21:00", 1.3122, 1.3114),     # Sunday: opens at Friday's close
    ("2012-03-11 22:00", 1.3114, 1.3117),
    ("2012-03-16 21:00", 1.3000, 1.3010),
    ("2012-03-18 21:00", 1.2990, 1.2995),     # a real gap: left alone
])


def test_only_a_sunday_opening_exactly_at_fridays_close_is_found():
    found = fx.stitched_weekends(STORE, 2012)
    assert list(found["sunday_hour"]) == [ts("2012-03-11 21:00")]
    assert list(found["friday_close"]) == [1.3122]


def test_the_mended_open_keeps_dukascopys_weekend_gap():
    duk = frame([("2012-03-09 21:00", 1.3115, 1.3124),
                 ("2012-03-11 21:00", 1.3101, 1.3115)])
    patch, refused = fx.mend(STORE, fx.stitched_weekends(STORE, 2012), duk)
    assert refused == 0 and len(patch) == 1
    bar = patch.iloc[0]
    # Dukascopy's gap, 1.3101 / 1.3124, put onto the stored Friday close.
    assert abs(bar["open"] - 1.3122 * 1.3101 / 1.3124) < 1e-12
    assert bar["close"] == 1.3114                       # the rest of the bar stays
    assert bar["low"] <= bar["open"] <= bar["high"]


def test_a_dukascopy_that_disagrees_either_side_is_refused():
    duk = frame([("2012-03-09 21:00", 1.3115, 1.3124),
                 ("2012-03-11 21:00", 1.3101, 1.3300)])  # 140 bp off the stored close
    patch, refused = fx.mend(STORE, fx.stitched_weekends(STORE, 2012), duk)
    assert refused == 1 and patch.empty

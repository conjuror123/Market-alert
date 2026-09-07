"""Asymmetric calendar multiplier (spec §4.3).

The same move means different things depending on when it happened. A jump ten
minutes before the inflation print and an identical jump on a quiet Tuesday are
different events, even though the numbers match. The multiplier raises the
weight of the hours around important releases.

The asymmetry is deliberate: the window BEFORE a release is wider than the one
AFTER. The market prepares for data in advance - positions move ahead of time -
whereas the reaction after publication settles quickly.

Releases are tiered by country, which the spec does not do and this calendar
requires. ForexFactory labels impact per country, so "High" means high FOR THAT
CURRENCY: a New Zealand rate decision carries the same label as an FOMC
decision. There are 823 High-impact releases a year, and at the spec's nine-hour
window each that is 84.5% of the clock. A multiplier that is on for most hours
raises most scores and therefore ranks nothing. So USD and EUR - and the handful
marked for every country at once - keep a wide window and the full peak, while
everything else is kept at a narrower window and a smaller peak: still present,
no longer dominant. Coverage falls from 59.5% of hours to 17.4%.

These windows are the spec's only exception to the units rule: they are measured
in CALENDAR hours and are not shortened even when they cross a market close or a
weekend. A macro release does not obey the exchange schedule.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from tremor import windows

DEFAULT_CALENDAR_PATH = os.path.join("data", "economic_calendar", "calendar.ndjson")

HOUR = 3600

# The countries whose releases move a global macro basket rather than one
# currency. "All" is ForexFactory's own marker for a release with no single
# country attached.
CORE_COUNTRIES = frozenset({"USD", "EUR", "All"})

IMPORTANCE = ("High", "Medium")

# Peak of the multiplier at the moment of publication, by (tier, importance).
# All starred: §7 calibrates them on train.
PEAK = {
    ("core", "High"): 1.8,
    ("core", "Medium"): 1.4,
    ("other", "High"): 1.3,
    ("other", "Medium"): 1.15,
}


def tier_of(country: str) -> str:
    return "core" if country in CORE_COUNTRIES else "other"


def profile(importance: str, country: str) -> tuple[float, float, float] | None:
    """(peak, hours before, hours after) for a release, or None if it has none."""
    key = (tier_of(country), importance)
    if key not in PEAK:
        return None
    before, after = windows.CALENDAR_WINDOWS[key]
    return PEAK[key], before, after


def multiplier_at(hours_from_event: float, peak: float, before: float,
                  after: float) -> float:
    """Multiplier for a moment `hours_from_event` hours away from the release
    (negative means before it).

    The function is piecewise-linear and continuous: at both window edges it
    equals one, so including or excluding the edge itself makes no difference.
    At the publication point both branches give the peak, so no separate
    "at T_event" branch is needed.
    """
    if -before <= hours_from_event <= 0:
        return 1.0 + (peak - 1.0) * (hours_from_event + before) / before
    if 0 < hours_from_event <= after:
        return peak - (peak - 1.0) * hours_from_event / after
    return 1.0


def load_events(path: str = DEFAULT_CALENDAR_PATH) -> list[tuple[int, str, str]]:
    """High- and Medium-impact releases: (epoch UTC moment, importance, country).

    Low takes no part in the multiplier - §4.3 counts only High and Medium.
    """
    if not os.path.exists(path):
        return []
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            importance = record.get("impact")
            if importance not in IMPORTANCE:
                continue
            moment = datetime.fromisoformat(record["date"])
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            events.append((int(moment.timestamp()), importance,
                           record.get("country", "")))
    return events


def multiplier_series(hours_utc, path: str = DEFAULT_CALENDAR_PATH) -> dict[int, float]:
    """Multiplier for every hour: the maximum over ALL releases covering it.

    The maximum, not the product: two important events in a row do not make the
    hour twice as significant, they merely both say the hour is significant.
    Multiplying the factors together would inflate the SI-Index on days with many
    releases - and most days have many.

    The function's argument is t, the moment a bar CLOSES (§1.2), so an hour is
    counted forward from hour_utc, which stores the opening moment.
    """
    events = load_events(path)
    result: dict[int, float] = defaultdict(lambda: 1.0)
    wanted = set(int(h) for h in hours_utc)
    if not wanted:
        return {}

    for moment, importance, country in events:
        shape = profile(importance, country)
        if shape is None:
            continue
        peak, before, after = shape
        # Hours whose CLOSE falls inside the event window.
        first_close = moment - int(before * HOUR)
        last_close = moment + int(after * HOUR)
        first_hour = (first_close // HOUR) * HOUR - HOUR
        for close in range(first_hour, last_close + HOUR, HOUR):
            hour_utc = close - HOUR
            if hour_utc not in wanted:
                continue
            value = multiplier_at((close - moment) / HOUR, peak, before, after)
            if value > result[hour_utc]:
                result[hour_utc] = value
    return {h: result[h] for h in wanted}

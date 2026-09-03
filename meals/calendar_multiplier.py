"""Asymmetric calendar multiplier (spec §4.3).

The same move means different things depending on when it happened. A jump ten
minutes before the inflation print and an identical jump on a quiet Tuesday are
different events, even though the numbers match. The multiplier raises the
weight of the hours around important releases.

The asymmetry is deliberate: the window BEFORE a release is wider than the one
AFTER (six hours against three for High-impact events). The market prepares for
data in advance - positions move ahead of time - whereas the reaction after
publication settles quickly.

These windows are the spec's only exception to the units rule: they are measured
in CALENDAR hours and are not shortened even when they cross a market close or a
weekend. A macro release does not obey the exchange schedule.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from meals import windows

DEFAULT_CALENDAR_PATH = os.path.join("data", "economic_calendar", "calendar.ndjson")

HOUR = 3600

# Peak of the multiplier at the moment of publication. Starred in the spec.
PEAK = {"High": 1.8, "Medium": 1.5}
BEFORE = {"High": windows.CALENDAR_HIGH_BEFORE, "Medium": windows.CALENDAR_MEDIUM_BEFORE}
AFTER = {"High": windows.CALENDAR_HIGH_AFTER, "Medium": windows.CALENDAR_MEDIUM_AFTER}


def multiplier_at(hours_from_event: float, importance: str) -> float:
    """Multiplier for a moment `hours_from_event` hours away from the release
    (negative means before it).

    The function is piecewise-linear and continuous: at both window edges it
    equals one, so including or excluding the edge itself makes no difference.
    At the publication point both branches give the peak, so no separate
    "at T_event" branch is needed.
    """
    if importance not in PEAK:
        return 1.0
    peak, before, after = PEAK[importance], BEFORE[importance], AFTER[importance]
    if -before <= hours_from_event <= 0:
        return 1.0 + (peak - 1.0) * (hours_from_event + before) / before
    if 0 < hours_from_event <= after:
        return peak - (peak - 1.0) * hours_from_event / after
    return 1.0


def load_events(path: str = DEFAULT_CALENDAR_PATH) -> list[tuple[int, str]]:
    """High- and Medium-impact releases: (epoch UTC moment, importance).

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
            if importance not in PEAK:
                continue
            moment = datetime.fromisoformat(record["date"])
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            events.append((int(moment.timestamp()), importance))
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

    for moment, importance in events:
        before, after = BEFORE[importance], AFTER[importance]
        # Hours whose CLOSE falls inside the event window.
        first_close = moment - int(before * HOUR)
        last_close = moment + int(after * HOUR)
        first_hour = (first_close // HOUR) * HOUR - HOUR
        for close in range(first_hour, last_close + HOUR, HOUR):
            hour_utc = close - HOUR
            if hour_utc not in wanted:
                continue
            value = multiplier_at((close - moment) / HOUR, importance)
            if value > result[hour_utc]:
                result[hour_utc] = value
    return {h: result[h] for h in wanted}

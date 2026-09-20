"""FRED client for the daily VIX series.

ONE OF TWO SOURCES for that series, and the slower one - see tremor.cboe, which
takes the same numbers from the exchange that computes them, the same evening
rather than the next business day. FRED is kept because it carries 1999-12-31,
which CBOE's file omits, and because two mirrors mean neither is a single point
of failure. tremor.backfill unions them.

Why either of these rather than an ETF on VIX futures: this is the real index
going back to 1990 from the official source, without the contango drift that
afflicts any futures ETF. The price of that choice is two peculiarities, both recorded as
departures worth recording:

1. The series is DAILY. FRED has no intraday VIX in any series - verified by
   searching every CBOE volatility series, all of them "Daily, Close". So a VIX
   spike is identified by the z-score machinery on daily bars, not hourly ones.

2. The value is published on the next business day, in the morning Chicago time.
   FRED's realtime_start field for this series is backdated (it equals the
   observation date itself), so it cannot be trusted as a publication date - the
   moment of availability is computed explicitly by available_at below. Otherwise
   the backtest would apply the multiplier in an hour when the value did not yet
   exist, that is, it would look ahead.

   That lag is FRED's alone and not the market's, which is what tremor.cboe is
   for. It measured, at 09:00 UTC on Monday 14 September 2026: FRED's newest
   value was Thursday's 17.84 while the index had closed Friday at 15.84, so the
   gauge read "higher than 52% of days" about a market that was in fact calmer
   than 62% of them. Where both sources carry a day, the EARLIER availability
   wins - see cboe.merge, and the reason it has to be the earlier one.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import requests

API_ROOT = "https://api.stlouisfed.org/fred"

# Publication is on the morning of the next business day, Chicago time. 14:00 UTC
# is 08:00 or 09:00 in Chicago depending on the season; we take an hour's margin
# beyond the observed update time so as never to treat a value as available
# earlier than it actually appeared.
PUBLICATION_HOUR_UTC = 15


class FredError(RuntimeError):
    pass


def available_at(observation_day: date) -> int:
    """The moment (epoch, UTC) from which the value for `observation_day` is known
    to the system. The next business day, PUBLICATION_HOUR_UTC.

    Weekends are skipped: Friday's value is published on Monday.
    """
    day = observation_day + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return int(datetime.combine(day, time(PUBLICATION_HOUR_UTC), tzinfo=timezone.utc).timestamp())


def fetch_series(
    series_id: str,
    api_key: str,
    start: date,
    session: requests.Session | None = None,
    timeout: int = 40,
) -> pd.DataFrame:
    """Fetches the series' observations starting from `start`.

    Returns the columns: day (epoch of the UTC midnight of the day the value
    belongs to), close, available_at (epoch of the moment from which the value may
    be used, see the module docstring).
    """
    if not api_key:
        raise FredError("FRED key is not set (FRED_API_KEY)")

    sess = session or requests
    resp = sess.get(
        f"{API_ROOT}/series_observations",
        params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start.isoformat(),
        },
        timeout=timeout,
        headers={"User-Agent": "market-alert-bot"},
    )
    if resp.status_code != 200:
        raise FredError(f"{series_id}: unexpected status {resp.status_code}: {resp.text[:200]}")

    rows = []
    for obs in resp.json().get("observations", []):
        # FRED encodes gaps with a dot - those are weekends and holidays when the
        # index was not computed, not lost data.
        if obs.get("value") in (None, "", "."):
            continue
        day = datetime.strptime(obs["date"], "%Y-%m-%d").date()
        rows.append({
            "day": int(datetime.combine(day, time(0), tzinfo=timezone.utc).timestamp()),
            "close": float(obs["value"]),
            "available_at": available_at(day),
        })
    if not rows:
        raise FredError(f"{series_id}: FRED returned no observations since {start}")
    return pd.DataFrame(rows).astype({"day": "int64", "close": "float64", "available_at": "int64"})

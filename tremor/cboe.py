"""CBOE client for the daily VIX history (spec §4.4).

The same series tremor.fred already fetches, from the exchange that computes it
rather than from a mirror. Kept alongside FRED rather than replacing it, because
the two differ in exactly two ways and each covers the other's gap.

FRESHNESS, which is why this module exists. FRED republishes VIXCLS on the next
business day, so over a weekend the gauge runs three calendar days behind: at
09:00 UTC on Monday 14 September 2026 the newest value FRED held was Thursday
the 10th, at 17.84, while the index had closed Friday at 15.84 - the message
said fear was rising on the day it had fallen back. CBOE posts the day's close
the same evening, shortly after dissemination stops at 16:15 Eastern.

AGREEMENT, which is why substituting one for the other is safe. Compared over
the whole record, the two sources share 9,270 days and the largest disagreement
between them is zero: they are the same numbers to the cent, 1990 to today. The
only day either has to itself is 1999-12-31, which CBOE's file omits and FRED
carries - so the two are unioned rather than one being chosen.

No key, and that is worth stating because everything else here needs one: this
is a plain CSV on a public CDN. A run with no FRED_API_KEY still has a VIX
series, which it did not before.
"""
from __future__ import annotations

import io
from datetime import date, datetime, time, timezone

import pandas as pd
import requests

HISTORY_URL = ("https://cdn.cboe.com/api/global/us_indices/daily_prices/"
               "VIX_History.csv")

# When the day's close may be used. CBOE disseminates VIX until 16:15 EASTERN -
# the exchange is in Chicago but the index keeps the New York clock - which is
# 20:15 UTC in summer and 21:15 in winter, and the file follows within minutes.
# 22:00 clears the later of the two by three quarters of an hour. Same-day rather
# than next-business-day: the FRED lag is a property of the mirror, not of what
# the market knew. The gate still matters, because the file may be fetched at any
# hour and a row read before settlement would be a value nobody had.
PUBLICATION_HOUR_UTC = 22


class CboeError(RuntimeError):
    pass


def available_at(observation_day: date) -> int:
    """The moment (epoch, UTC) from which the day's close may be used."""
    return int(datetime.combine(observation_day, time(PUBLICATION_HOUR_UTC),
                                tzinfo=timezone.utc).timestamp())


def fetch_vix_history(start: date, session: requests.Session | None = None,
                      timeout: int = 40) -> pd.DataFrame:
    """The whole daily history, in tremor.fred's columns: day, close, available_at.

    The endpoint serves 1990 to yesterday in one 400 KB response and takes no
    range parameters, so `start` trims rather than requests.
    """
    sess = session or requests
    resp = sess.get(HISTORY_URL, timeout=timeout,
                    headers={"User-Agent": "market-alert-bot"})
    if resp.status_code != 200:
        raise CboeError(f"VIX history: unexpected status {resp.status_code}: "
                        f"{resp.text[:200]}")
    try:
        frame = pd.read_csv(io.StringIO(resp.text))
    except Exception as exc:
        raise CboeError(f"VIX history: unreadable CSV - {exc}") from exc
    if not {"DATE", "CLOSE"} <= set(frame.columns):
        raise CboeError(f"VIX history: unexpected columns {list(frame.columns)}")

    days = pd.to_datetime(frame["DATE"], format="%m/%d/%Y", errors="coerce").dt.date
    close = pd.to_numeric(frame["CLOSE"], errors="coerce")
    keep = days.notna() & close.notna() & (close > 0)
    days, close = days[keep], close[keep]
    days_from = days[days >= start]
    if days_from.empty:
        raise CboeError(f"VIX history: nothing on or after {start}")

    out = pd.DataFrame({
        "day": [int(datetime.combine(d, time(0), tzinfo=timezone.utc).timestamp())
                for d in days_from],
        "close": close.loc[days_from.index].to_numpy(dtype="float64"),
        "available_at": [available_at(d) for d in days_from],
    })
    return out.astype({"day": "int64", "close": "float64", "available_at": "int64"})


def merge(*frames: pd.DataFrame) -> pd.DataFrame:
    """One series out of several sources, keeping the EARLIEST availability.

    Earliest rather than "whichever source won", and the difference is a bug
    rather than a preference: FRED republishes a value CBOE already carried, so
    letting a later source overwrite the column would push a reading's
    availability FORWARD in time. A number the note had already shown would
    become one the system does not yet know, and the line would vanish from a
    message that had been correct. Availability only ever moves earlier here.

    The close itself is taken from whichever frame is first, which is arbitrary
    and can afford to be: measured over the whole overlap the sources agree
    exactly.
    """
    usable = [f for f in frames if f is not None and not f.empty]
    if not usable:
        return pd.DataFrame(columns=["day", "close", "available_at"]).astype(
            {"day": "int64", "close": "float64", "available_at": "int64"})
    joined = pd.concat(usable, ignore_index=True)
    out = joined.groupby("day", as_index=False).agg(
        close=("close", "first"), available_at=("available_at", "min"))
    return out.sort_values("day").reset_index(drop=True)

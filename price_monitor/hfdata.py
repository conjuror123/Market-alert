"""HF Data Library: one-minute US equity bars, used to deepen the ETF history.

Twelve Data's plan stops at 2020-02 for every ETF at once, and no other free
source found carried US-equity INTRADAY history further back - Dukascopy's ETF
CFDs are patchy and volume-incompatible, FXCM has no equities at all. This one
does, for all twelve instruments in the basket including SHY, HYG and DBC,
which nothing else had.

THE PART THAT MATTERS MOST. The library changes source in March 2022:

    before  full consolidated tape, CTA and UTP - the same underlying data as
            CRSP and TAQ, every trade on every US venue
    after   IEX Exchange only, about 2-3% of consolidated volume

Their own note puts it plainly: daily volume for a stock like AAPL on IEX may
be 2-5 million shares against 50-80 million on the full tape. Splicing across
that boundary would drop every ETF's volume by ~97% on a fixed date, and the
volume profile of §3.5 is built on exactly that series - it would read the
change of vendor as the largest liquidity event in the basket's history.

We do not need to go near it: Twelve Data already holds everything from
2020-02, so the useful window ends twenty-six months BEFORE the break. But
"we do not need to" is not a safeguard, so this module refuses IEX bars
outright rather than trusting a date. Every bar carries a `source` column, and
CONSOLIDATED_SOURCE is what is kept; anything else is dropped and counted, and
a file with no source column at all is an error rather than a guess.

Extended hours are not served in either version, which suits a basket whose
ETFs are already defined on the regular session.

Prices are unadjusted, which is what this project wants (see
meals.corporate_actions on why an adjusted series cannot be appended to hour by
hour). It does mean the corporate-actions table has to reach as far back as the
bars do, or every pre-2021 ex-date arrives as an unexplained price drop.
"""
from __future__ import annotations

import io
import logging

import pandas as pd
import requests

from price_monitor.models import ExchangeError

log = logging.getLogger("price_monitor.hfdata")

BASE_URL = "https://api.hfdatalibrary.com/v1"

# The value of the `source` column that means "full consolidated tape". Anything
# else is a partial venue and is refused - see the module docstring.
CONSOLIDATED_SOURCE = "pitrading"

# Their cleaned version applies a nine-step outlier and quality filter. Taking
# it rather than "raw" because this project has no way to tell an erroneous
# print from a real once-a-decade move, and a bad print is exactly what the
# severity ladder would promote to an extreme alert.
DEFAULT_VERSION = "clean"

# The parquet's own column names are not documented, so each field is found by
# trying what it is plausibly called. Unknown beats assumed: a missing field
# raises rather than defaulting, because a silently absent volume or a
# misidentified timestamp is worse than a failed import.
_TIMESTAMP_NAMES = ("timestamp", "datetime", "date_time", "time", "dt", "date")
_FIELD_NAMES = {
    "open": ("open", "o", "open_price"),
    "high": ("high", "h", "high_price"),
    "low": ("low", "l", "low_price"),
    "close": ("close", "c", "close_price"),
    "volume": ("volume", "v", "vol", "size"),
}
_SOURCE_NAMES = ("source", "src", "feed", "venue")


def _column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    lowered = {str(c).lower(): c for c in frame.columns}
    for name in candidates:
        if name in lowered:
            return lowered[name]
    return None


def fetch_parquet(ticker: str, api_key: str,
                  session: requests.Session | None = None,
                  version: str = DEFAULT_VERSION,
                  base_url: str = BASE_URL, timeout: int = 300) -> bytes:
    """The ticker's whole one-minute history, as the parquet bytes they serve."""
    if not api_key:
        raise ExchangeError("No HF Data API key configured (HFDATA_API_KEY)")
    sess = session or requests
    response = sess.get(f"{base_url}/bars/{ticker}", params={"version": version},
                        headers={"X-API-Key": api_key,
                                 "User-Agent": "market-alert-bot"},
                        timeout=timeout)
    if response.status_code == 401:
        raise ExchangeError(f"{ticker}: HF Data rejected the API key")
    if response.status_code != 200:
        raise ExchangeError(
            f"{ticker}: unexpected status {response.status_code}: "
            f"{response.text[:200]}")
    return response.content


def describe(payload: bytes, rows: int = 3) -> dict:
    """What the file actually contains, for looking before parsing.

    The schema is not documented anywhere, and this project has been bitten
    twice this week by a source whose shape was assumed rather than read - once
    by a log volatility read through its magnitude, once by an archive whose
    timestamps observed daylight saving. So the first thing the importer can do
    is print what it was given.
    """
    frame = pd.read_parquet(io.BytesIO(payload))
    stamp = _column(frame, _TIMESTAMP_NAMES)
    source = _column(frame, _SOURCE_NAMES)
    out = {
        "rows": len(frame),
        "columns": list(frame.columns)[:40],
        "dtypes": {str(c): str(frame[c].dtype) for c in list(frame.columns)[:40]},
        "timestamp_column": stamp,
        "source_column": source,
        "head": frame.head(rows).to_dict("records"),
    }
    if source is not None:
        out["source_counts"] = frame[source].value_counts().head(5).to_dict()
    if stamp is not None:
        out["first_timestamp"] = str(frame[stamp].min())
        out["last_timestamp"] = str(frame[stamp].max())
    return out


def to_minute_frame(payload: bytes, timezone: str) -> pd.DataFrame:
    """Consolidated-tape minute bars only, on the store's schema and in UTC.

    `timezone` is required and has no default on purpose. Their pages say the
    session runs 09:30-15:59 ET, which is a statement about the market and not
    necessarily about how the file encodes it, and guessing between "already
    UTC" and "Eastern, with daylight saving" is the single most expensive
    mistake available here: read wrong, four months of every year shift by an
    hour and the series looks like noise rather than like an error.
    """
    frame = pd.read_parquet(io.BytesIO(payload))
    if frame.empty:
        return frame

    stamp = _column(frame, _TIMESTAMP_NAMES)
    if stamp is None:
        raise ExchangeError(f"no timestamp column among {list(frame.columns)[:20]}")
    source = _column(frame, _SOURCE_NAMES)
    if source is None:
        raise ExchangeError(
            "no source column: cannot tell consolidated-tape bars from "
            "IEX-only ones, and mixing them collapses volume by ~97%")

    kept = frame[frame[source].astype(str).str.lower() == CONSOLIDATED_SOURCE]
    dropped = len(frame) - len(kept)
    if dropped:
        log.info("dropped %d bar(s) not from the consolidated tape", dropped)
    if kept.empty:
        return kept.iloc[0:0]

    moments = pd.to_datetime(kept[stamp], errors="coerce")
    if moments.dt.tz is None:
        moments = moments.dt.tz_localize(timezone, ambiguous="NaT",
                                         nonexistent="NaT")
    moments = moments.dt.tz_convert("UTC")

    fields = {}
    for name, candidates in _FIELD_NAMES.items():
        column = _column(kept, candidates)
        if column is None:
            raise ExchangeError(
                f"no {name} column among {list(kept.columns)[:20]}")
        fields[name] = pd.to_numeric(kept[column], errors="coerce").to_numpy()

    out = pd.DataFrame({
        "hour_utc": (moments.astype("int64") // 10**9).to_numpy(),
        **fields,
        # to_hourly sums this, so one row per minute bar is what it must be.
        "n_src": 1,
    })
    out = out[moments.notna().to_numpy()]
    return out.dropna().sort_values("hour_utc").reset_index(drop=True)

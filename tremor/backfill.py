"""Phase 0: filling the store with hourly bars from 2021 (spec §2.1).

A one-off tool, not part of the hourly run.

Two thirds of the basket need not be downloaded again: the currency pairs and
crypto already sit in data/candle_history/ - the existing monitor accumulated
them from 2021-01-01. They are imported from NDJSON and only the missing tail is
fetched; the only instruments pulled from the network in full are the ETFs, which
the old basket did not contain.

The ETFs are requested as HALF-HOURLY bars and folded into hourly ones on the
round UTC hour boundary (see bars.to_hourly): their own hourly grid runs on the
:30 and would not line up with the currency pairs and crypto. This costs no
credits - the Twelve Data plan counts requests, not rows - but it does require a
larger window per request: an ETF has about 13 half-hourly bars per trading day,
so a single 5000-row answer holds more than a year.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

from tremor import bars, cboe, corporate_actions, fred
from tremor import sessions as _sessions
from tremor.basket import Asset, Basket, load_basket
from price_monitor import (candle_store, coinbase, dukascopy, fxcm, hfdata,
                           tiingo, twelvedata, yahoo)
from price_monitor.models import ExchangeError
from price_monitor.notifier import TelegramError, send_telegram_message

log = logging.getLogger("tremor.backfill")

LEGACY_HISTORY_DIR = os.path.join("data", "candle_history")

COINBASE_BASE_URL = "https://api.exchange.coinbase.com"
TWELVEDATA_BASE_URL = "https://api.twelvedata.com"
TIINGO_BASE_URL = tiingo.BASE_URL
YAHOO_BASE_URL = yahoo.BASE_URL

# Pause between Twelve Data requests. The free plan allows 8 requests a minute,
# and 8 seconds hold exactly that boundary with a small margin. The pause is held
# between instruments too, not only within one: the limit is per key, and the
# running hourly monitor is spending it at the same time.
TWELVEDATA_DELAY_SECONDS = 8.0

# How many calendar days to request at once. Half-hourly bars give about 13 rows
# per trading day, so 300 days is ~3900 rows against a ceiling of 5000. Hourly
# currency pairs give 24 rows a day, and there the client's own, more cautious
# default applies.
CHUNK_DAYS = {"30min": 300, "1h": 150}

# How far back each provider will answer, by interval. Twelve Data and Coinbase
# page backwards without a wall and are absent on purpose; the two recent-end
# providers are not, and a request past their reach returns an empty result,
# which is indistinguishable from a quiet market.
_PROVIDER_REACH = {
    "yahoo": yahoo.MAX_LOOKBACK_DAYS,
    "tiingo": tiingo.MAX_LOOKBACK_DAYS,
}

# How many instruments with an EMPTY store one ordinary run will fetch. See the
# loop in main() - this is the guard that keeps a batch of newly configured
# tickers from turning the hourly job into a backfill.
SEED_PER_RUN = 4


def format_provider_failure(dark: list[tuple[str, str, str]],
                            tiingo_gone: bool = False,
                            tiingo_skipped: int = 0,
                            tiingo_remaining: str | None = None,
                            tiingo_trip: str | None = None) -> str:
    """One operational message naming who went dark. Does not switch provider."""
    lines = []
    if dark:
        lines.append(
            f"⚠️ <b>Backfill: {len(dark)} instrument(s) went dark</b>")
        for asset_id, provider, err in dark[:20]:
            lines.append(f"• {asset_id} ({provider}): {err}")
        if len(dark) > 20:
            lines.append(f"• …and {len(dark) - 20} more")
    if tiingo_gone:
        if lines:
            lines.append("")
        who = f" after {tiingo_trip}" if tiingo_trip else ""
        head = (f"remaining headroom {tiingo_remaining}"
                if tiingo_remaining is not None else
                "remaining headroom not in the 429")
        lines.append(f"⚠️ <b>Tiingo request budget spent{who}</b>")
        lines.append(f"{head}; {tiingo_skipped} remaining Tiingo instrument(s) skipped.")
        lines.append("Provider was not switched automatically.")
    return "\n".join(lines)


def send_ops_alert(text: str) -> None:
    """Private Telegram if configured, otherwise the product chat, otherwise log."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = (os.environ.get("TELEGRAM_HEALTH_CHAT_ID")
            or os.environ.get("TELEGRAM_CHAT_ID", ""))
    if not token or not chat:
        log.warning("No operational Telegram destination configured")
        return
    try:
        send_telegram_message(token, chat, text)
    except TelegramError as exc:
        log.error("Failed to send operational alert: %s", exc)


def _days_since(start: date) -> float:
    return max(1.0, (datetime.now(timezone.utc) - datetime.combine(
        start, datetime.min.time(), tzinfo=timezone.utc)).total_seconds() / 86400)


def import_legacy(asset: Asset, path: str, legacy_dir: str = LEGACY_HISTORY_DIR) -> int:
    """Moves the already accumulated NDJSON history into Parquet. The file-naming
    scheme is the same in candle_store and in Tremor, so the mapping is direct.
    """
    legacy_path = candle_store.store_path(legacy_dir, asset.source, asset.ticker)
    if not os.path.exists(legacy_path):
        return 0
    candles = candle_store.load_candles(legacy_path)
    if not candles:
        return 0
    return bars.merge(path, bars.to_hourly(bars.candles_to_frame(candles)))


def fetch_missing(asset: Asset, path: str, since: date, api_key: str,
                  session: requests.Session, extend_history: bool = False,
                  tiingo_key: str = "") -> int:
    """Fetches whatever the store does not have yet: from the last saved bar up
    to now, or from `since` when the store is empty.

    It asks for a day more than strictly needed: the last saved bar may have been
    incomplete when it was stored, and the overlap gives the source a chance to
    serve its corrected version (merge keeps the new one).

    `extend_history` asks from `since` even when the store already has data. The
    ordinary path only ever reaches FORWARD from the last saved bar, which is
    right for a daily top-up and useless for deepening the archive: moving
    history_since earlier changes nothing without it, because the store is not
    empty and the window is measured from its newest bar rather than its oldest.
    bars.merge takes the union, so the old rows survive and only genuinely new
    ones are added.
    """
    stored = bars.load(path)
    end: datetime | None = None
    if stored.empty:
        # A never-seen instrument. The ordinary hourly run takes ONE chunk of it
        # - a single credit - and leaves the archive to the deepening workflow.
        # Asking for the whole history here is what an empty store used to mean,
        # and at the acquisition floor of 2002 that is about thirty chunks paced
        # eight seconds apart: four minutes and thirty credits per instrument,
        # which for a batch of new tickers is hours of wall clock and more than
        # a day's free-tier budget spent inside a job that is supposed to take
        # ninety seconds. One chunk gets the instrument producing bars now; depth
        # is a separate, deliberate act.
        days = float(CHUNK_DAYS[asset.fetch_interval]) if not extend_history \
            else _days_since(since)
    elif extend_history:
        # Deepening: the walk goes backwards, so it starts at the oldest bar
        # already held rather than at today. Starting at today would spend a
        # credit per chunk re-fetching years that are already on disk before
        # reaching any new ground, and the free tier's 800 a day is the real
        # ceiling on how far back a run gets.
        end = datetime.fromtimestamp(int(stored["hour_utc"].min()), tz=timezone.utc)
        days = max(1.0, (end - datetime.combine(
            since, datetime.min.time(), tzinfo=timezone.utc)).total_seconds() / 86400)
    else:
        last = datetime.fromtimestamp(int(stored["hour_utc"].max()), tz=timezone.utc)
        days = max(1.0, (datetime.now(timezone.utc) - last).total_seconds() / 86400 + 1)

    # WHICH PROVIDER ANSWERS, and it is not always the fast one. Deepening walks
    # BACKWARDS through history, and only the archive provider holds it: Yahoo
    # serves at most 55 days of 30-minute bars and Tiingo caps a response at
    # 10000 rows. `source` is the provider the archive came from, so that is who
    # a deepening run asks, however the hourly top-up is routed.
    provider = asset.source if extend_history else asset.fetched_from
    # A first fetch of an empty store asks for a whole chunk - 300 days at 30
    # minutes - which is more than the recent-end providers will answer. Clamp
    # it rather than fail: the point of the seeding path is to get an instrument
    # producing bars now, and depth is a separate, deliberate act either way.
    reach = _PROVIDER_REACH.get(provider, {}).get(asset.fetch_interval)
    if reach is not None and days > reach:
        log.info("%s: %s serves %d days at %s, not %.0f - asking for what it has",
                 asset.asset_id, provider, reach, asset.fetch_interval, days)
        days = float(reach)

    if provider == "twelvedata":
        candles = twelvedata.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            base_url=TWELVEDATA_BASE_URL, api_key=api_key, session=session,
            request_delay_seconds=TWELVEDATA_DELAY_SECONDS,
            chunk_days=CHUNK_DAYS[asset.fetch_interval], end=end,
        )
    elif provider == "coinbase":
        candles = coinbase.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            base_url=COINBASE_BASE_URL, session=session,
        )
    elif provider == "tiingo":
        candles = tiingo.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            base_url=TIINGO_BASE_URL, api_key=tiingo_key, session=session, end=end,
        )
    elif provider == "yahoo":
        candles = yahoo.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            base_url=YAHOO_BASE_URL, session=session, end=end,
        )
    else:
        raise ExchangeError(f"{asset.asset_id}: unknown provider '{provider}'")

    return bars.merge(path, bars.to_hourly(bars.candles_to_frame(candles)))


# How recently a stored bar must have arrived for the top-up to run anyway. The
# ordinary fetch deliberately asks for a day more than it needs, because the
# newest stored bar may have been served while its hour was still open and the
# source will hand back a corrected version later. Skipping on the very next run
# would keep whatever was stored first. Three hours means the run right after a
# close still re-asks, and only the quiet hours afterwards are skipped.
SETTLE_HOURS = 3


def nothing_can_have_appeared(asset: Asset, path: str,
                              table: "dict | None",
                              now: datetime | None = None) -> bool:
    """True when the calendar says no new bar can exist for this instrument yet.

    EVERY GUARD HERE IS AGAINST THE SAME MISTAKE - skipping a fetch that would
    have returned something. A missed bar is a hole in the history and a move
    the detector never sees, which is far worse than a wasted request, so each
    condition below refuses to skip unless it is certain:

      - crypto_24_7 is never skipped: it genuinely trades every hour.
      - fx_continuous uses the same Sun 17:00 → Fri 17:00 New York week as the
        bar walk (`instrument_day_hours`), so the skip cannot disagree with
        what the walk considers a bar.
      - us_equity still needs the session table; a missing or short table never
        causes a skip.
      - never on an empty store, where there is no newest bar to reason from.
      - never within SETTLE_HOURS of the newest stored bar, so a bar served
        while its hour was still open is re-asked for.
      - and finally, only when no expected hour at all sits between the newest
        stored bar and now.
    """
    template = asset.session_template
    if template == "crypto_24_7":
        return False
    if template == "us_equity" and not table:
        return False
    if template not in ("us_equity", "fx_continuous"):
        return False

    stored = bars.load(path)
    if stored.empty:
        return False

    now = now or datetime.now(timezone.utc)
    newest = int(stored["hour_utc"].max())
    if now.timestamp() - newest < SETTLE_HOURS * 3600:
        return False
    if template == "us_equity" and max(table) < now.date():
        return False

    since = datetime.fromtimestamp(newest, tz=timezone.utc).date()
    expected: list[int] = []
    day = since
    while day <= now.date():
        expected.extend(_sessions.instrument_day_hours(day, template, table))
        day += timedelta(days=1)
    return not any(hour > newest for hour in expected)


def backfill_instrument(asset: Asset, basket: Basket, bars_dir: str, api_key: str,
                        session: requests.Session, legacy_dir: str = LEGACY_HISTORY_DIR,
                        extend_history: bool = False, tiingo_key: str = "") -> dict:
    path = bars.store_path(bars_dir, asset.file_stem)
    from_legacy = import_legacy(asset, path, legacy_dir)
    from_api = fetch_missing(asset, path, basket.acquire_since, api_key, session,
                             extend_history, tiingo_key)
    stored = bars.load(path)
    return {
        "asset_id": asset.asset_id,
        "from_legacy": from_legacy,
        "from_api": from_api,
        "rows": len(stored),
        "first": int(stored["hour_utc"].min()) if not stored.empty else None,
        "last": int(stored["hour_utc"].max()) if not stored.empty else None,
    }


def _vix_day(epoch: int) -> date:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).date()


def _latest_vix_day_available(now: datetime) -> date | None:
    """Newest observation day whose CBOE or FRED gate has already opened."""
    d = now.date()
    now_ts = now.timestamp()
    for _ in range(21):
        if now_ts >= cboe.available_at(d) or now_ts >= fred.available_at(d):
            return d
        d -= timedelta(days=1)
    return None


def _vix_frames_equal(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    cols = ["day", "close", "available_at"]
    if left.empty and right.empty:
        return True
    if len(left) != len(right) or left.empty or right.empty:
        return False
    a = left[cols].sort_values("day").reset_index(drop=True)
    b = right[cols].sort_values("day").reset_index(drop=True)
    return a.equals(b)


def backfill_vix(basket: Basket, vix_dir: str, api_key: str,
                 session: requests.Session,
                 now: datetime | None = None) -> dict:
    """The daily VIX series, from both sources that serve it.

    CBOE computes the index and posts the close the same evening; FRED
    republishes it on the next business day. Taking both costs one extra HTTP
    call and buys two things: the gauge stops running up to three calendar days
    behind over a weekend, and neither source is a single point of failure -
    a run with no FRED key still has a series, which it did not before.

    Neither is trusted over the other, because measured over the whole record
    they agree to the cent on all 9,270 days they share. They are unioned: CBOE
    carries the newest day, FRED carries 1999-12-31, which CBOE's file omits.

    The series gains at most one value a day. CBOE's endpoint takes no range
    parameters (~400 KB every time), so the only saving is not calling it.
    When the store already covers what the availability gates say can exist,
    both fetches and the parquet write are skipped.
    """
    vix = basket.volatility_index
    path = os.path.join(vix_dir, f"{vix.file_stem}.parquet")
    stored = pd.DataFrame()
    if os.path.exists(path):
        stored = pd.read_parquet(path).sort_values("day").reset_index(drop=True)

    now = now or datetime.now(timezone.utc)
    latest = _latest_vix_day_available(now)
    if not stored.empty and latest is not None:
        newest = _vix_day(int(stored["day"].max()))
        if newest >= latest:
            log.info("VIX: stored through %s already covers what can exist; not fetched",
                     newest)
            return {"asset_id": vix.series_id, "rows": len(stored),
                    "first": int(stored["day"].min()), "last": int(stored["day"].max()),
                    "sources": ["stored"], "trouble": []}

    pieces, sources, trouble = [], [], []
    try:
        pieces.append(cboe.fetch_vix_history(vix.history_since, session=session))
        sources.append("cboe")
    except Exception as exc:
        trouble.append(f"cboe: {exc}")

    fred_start = vix.history_since
    if not stored.empty:
        newest = _vix_day(int(stored["day"].max()))
        # Wide enough to span a holiday stretch; empty FRED raises.
        fred_start = max(vix.history_since, newest - timedelta(days=21))

    if api_key:
        try:
            pieces.append(fred.fetch_series(vix.series_id, api_key,
                                            fred_start, session=session))
            sources.append("fred")
        except Exception as exc:
            trouble.append(f"fred: {exc}")
    else:
        trouble.append("fred: FRED_API_KEY is not set")

    if not stored.empty:
        pieces.append(stored)

    frame = cboe.merge(*pieces)
    if frame.empty:
        raise RuntimeError("no VIX source answered - " + "; ".join(trouble))

    if _vix_frames_equal(frame, stored):
        log.info("VIX: merged frame equals the store; parquet not rewritten")
        return {"asset_id": vix.series_id, "rows": len(frame),
                "first": int(frame["day"].min()), "last": int(frame["day"].max()),
                "sources": sources, "trouble": trouble}

    os.makedirs(vix_dir, exist_ok=True)
    frame.to_parquet(path, index=False, compression="zstd")
    return {"asset_id": vix.series_id, "rows": len(frame),
            "first": int(frame["day"].min()), "last": int(frame["day"].max()),
            "sources": sources, "trouble": trouble}


def _fmt(epoch: int | None) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d") if epoch else "-"


# The NYSE table describes the US equity session and nothing else. A currency
# pair trading around the clock has no "missing Tuesday" to find against it, so
# gap filling is offered only where the calendar is authoritative.
CALENDAR_TEMPLATE = "us_equity"

# How many days either side of a run of missing sessions to ask for. A window
# rather than the exact day because a request for a single date can land on the
# wrong side of the provider's own boundary handling, and the extra rows merge
# away for free.
GAP_PADDING_DAYS = 1


def missing_sessions(path: str, table: dict) -> list[date]:
    """Trading days the calendar has and the store does not.

    Bounded by what is stored: a day before the first bar or after the last is
    not a gap, it is simply outside the archive, and reporting those would bury
    the real holes under thousands of them.
    """
    stored = bars.load(path)
    if stored.empty:
        return []
    days = set(pd.to_datetime(stored["hour_utc"], unit="s", utc=True).dt.date)
    lo, hi = min(days), max(days)
    return sorted({d for d in table if lo <= d <= hi} - days)


def missing_hours(path: str, table: dict) -> list[int]:
    """Hours the calendar has and the store does not, inside the stored range.

    The day-level view above cannot see these. A day that holds three of its
    seven bars is present, so it is not a gap by that measure, and every hole
    inside a day stayed invisible for as long as whole days were the unit of
    counting - which is how a five-hour hole on 2020-02-19 survived a gap fill
    that reported itself complete.

    That matters more than the count suggests. An hourly return is taken
    between consecutive STORED bars, so a missing hour does not shrink the
    series, it silently turns a one-hour return into a two-hour one - a larger
    move measured against a one-hour scale. The holes are rare, but they
    inflate exactly the quantity the ladder ranks.
    """
    stored = bars.load(path)
    if stored.empty:
        return []
    have = set(stored["hour_utc"].astype(int))
    days = pd.to_datetime(stored["hour_utc"], unit="s", utc=True).dt.date
    want = _sessions.expected_hours(table, min(days), max(days))
    return sorted(want - have)


def _runs(days: list[date]) -> list[tuple[date, date]]:
    """Consecutive missing days folded into single spans, to save requests."""
    spans: list[tuple[date, date]] = []
    for day in days:
        if spans and (day - spans[-1][1]).days <= 3:
            spans[-1] = (spans[-1][0], day)
        else:
            spans.append((day, day))
    return spans


def fill_gaps(asset: Asset, path: str, table: dict, api_key: str,
              session: requests.Session) -> dict:
    """Re-asks the provider for the sessions the store is missing.

    Whether the day is recoverable at all is the point of running this: the
    provider may simply not hold it, in which case the request comes back empty
    and the gap is confirmed as theirs rather than ours. Either answer is worth
    having, and only one of them costs a credit.
    """
    if asset.session_template != CALENDAR_TEMPLATE:
        return {"skipped": "no authoritative calendar", "added": 0, "gaps": 0}
    if asset.source != "twelvedata":
        # Deliberately `source`, not `fetched_from`. This walks backwards
        # through history, which Twelve Data holds and the fast providers do
        # not: Yahoo serves at most 55 days of 30-minute bars and Tiingo caps a
        # response at 10000 rows. An instrument moved to another provider for
        # its hourly top-up is still filled from Twelve Data here.
        return {"skipped": f"source {asset.source} not supported here",
                "added": 0, "gaps": 0}

    gaps = missing_sessions(path, table)
    if not gaps:
        return {"skipped": None, "added": 0, "gaps": 0, "still_missing": []}

    added = 0
    for start, end in _runs(gaps):
        window_end = datetime.combine(end, datetime.min.time(),
                                      tzinfo=timezone.utc) + timedelta(
            days=GAP_PADDING_DAYS + 1)
        span = (end - start).days + 2 * GAP_PADDING_DAYS + 1
        try:
            candles = twelvedata.fetch_full_history(
                symbol=asset.ticker, interval=asset.fetch_interval, days=span,
                base_url=TWELVEDATA_BASE_URL, api_key=api_key, session=session,
                request_delay_seconds=TWELVEDATA_DELAY_SECONDS,
                chunk_days=CHUNK_DAYS[asset.fetch_interval], end=window_end)
        except ExchangeError as exc:
            log.warning("%s: %s..%s could not be re-fetched: %s",
                        asset.asset_id, start, end, exc)
            continue
        if candles:
            added += bars.merge(path, bars.to_hourly(bars.candles_to_frame(candles)))
        time.sleep(TWELVEDATA_DELAY_SECONDS)

    left = missing_sessions(path, table)
    return {"skipped": None, "added": added, "gaps": len(gaps),
            "still_missing": left}


# What HF Data's minute timestamps mean, read off the file rather than assumed.
# The probe (run 34014690848) returned naive datetime64 values whose last row
# is 2026-09-04 15:59 - a September date, in daylight-saving season, ending at
# 15:59. Under a fixed EST encoding that day's last bar would read 14:59, so
# the file is Eastern wall-clock and observes DST.
#
# That reasoning is sound and is still not trusted on its own: it is checked
# against the two years of overlap the store already holds, and the import
# refuses to merge anything if the check fails. See verify_alignment.
HFDATA_TIMEZONE = "US/Eastern"

# How closely the re-derived hourly bars must track what is already stored
# before any of them are kept. A timezone read wrong shifts four months of
# every year by an hour, which does not look like an error - it looks like
# noise, and scores about 0.5 where the truth scores 0.99. HistData, tested
# the same way, gave 0.94 correctly localised and 0.53 read as UTC, so the
# gap between right and wrong is wide and the bar can sit well inside it.
ALIGNMENT_MIN_CORRELATION = 0.90
ALIGNMENT_MIN_HOURS = 200

# And how far apart the LEVELS may be. This is a separate question from the one
# above and the first version of this check did not ask it, which let 30024
# dividend-adjusted SPY bars into the store.
#
# Adjustment is multiplicative, so it barely touches returns: that import
# scored 0.9959 on correlation while sitting 99 basis points below the stored
# prices, and the gap grew the further back it went - 0.972x of the true close
# at the end of 2019, 0.885x in 2015, 0.733x in 2005, which is SPY's dividends
# compounded. Spliced under an unadjusted store it put a 3.07% step across one
# weekend at the seam.
#
# Two sources reporting the same consolidated tape should agree on the price of
# SPY to a basis point or two, so 25 is loose enough for timing differences
# inside a minute and nowhere near loose enough to admit a payout.
ALIGNMENT_MAX_MEDIAN_BP = 25.0


def verify_alignment(minutes: "pd.DataFrame", stored: "pd.DataFrame") -> dict:
    """Checks re-derived bars against the ones already held, over their overlap.

    The overlap exists because the archive runs to the present while the store
    starts in 2020, so roughly two years of consolidated-tape bars cover hours
    we can already price independently. Nothing else in this import has that
    luxury; the years being imported have no second opinion at all, which is
    exactly why the years that do have one are made to earn the rest.
    """
    import numpy as np

    hourly = bars.to_hourly(minutes)
    joined = hourly.merge(stored[["hour_utc", "close"]], on="hour_utc",
                          how="inner", suffixes=("_new", "_stored"))
    if len(joined) < ALIGNMENT_MIN_HOURS:
        return {"hours": len(joined), "correlation": float("nan"),
                "median_bp": float("nan"), "ok": False,
                "why": f"only {len(joined)} overlapping hours"}

    new = np.log(joined["close_new"].to_numpy())
    old = np.log(joined["close_stored"].to_numpy())
    returns_new, returns_old = np.diff(new), np.diff(old)
    good = np.isfinite(returns_new) & np.isfinite(returns_old)
    correlation = float(np.corrcoef(returns_new[good], returns_old[good])[0, 1])
    median_bp = float(np.median(
        np.abs(joined["close_new"] - joined["close_stored"])
        / joined["close_stored"]) * 1e4)
    reasons = []
    if not correlation >= ALIGNMENT_MIN_CORRELATION:
        reasons.append(f"correlation {correlation:.4f} below "
                       f"{ALIGNMENT_MIN_CORRELATION}")
    if not median_bp <= ALIGNMENT_MAX_MEDIAN_BP:
        reasons.append(f"median level gap {median_bp:.1f}bp above "
                       f"{ALIGNMENT_MAX_MEDIAN_BP}bp - the series disagree on "
                       f"PRICE while agreeing on returns, which is what a "
                       f"dividend adjustment looks like")
    return {"hours": len(joined), "correlation": correlation,
            "median_bp": median_bp, "ok": not reasons,
            "why": "; ".join(reasons)}


# How much of the overlap is spent pinning the vendor's adjustment factor. The
# rest of it - about two years - is then an honest test of the reconstruction,
# because nothing in those months was used to build it.
CALIBRATION_DAYS = 30


def unadjust_to_store(minutes: "pd.DataFrame", stored: "pd.DataFrame",
                      steps: list) -> tuple["pd.DataFrame", dict]:
    """Turns a vendor's adjusted prices into the store's unadjusted convention.

    Two things make this checkable rather than hopeful. The anchor is measured,
    not guessed: the factor is pinned where the store already knows the true
    price, so the ex-dates only have to explain the change from there. And the
    pinning uses the first month of the overlap while the check that follows
    uses all of it, so roughly two years of the test never touched the fit.

    Volume is left alone. Splits are recorded in the table but excluded from
    the steps used here (`load_steps` defaults to dividends); a dividend does
    not restate share counts, and the store is already split-adjusted.
    """
    import numpy as np

    hourly = bars.to_hourly(minutes)
    joined = hourly.merge(stored[["hour_utc", "close"]], on="hour_utc",
                          how="inner", suffixes=("_hf", "_store"))
    if joined.empty:
        return minutes, {"calibrated": False, "why": "no overlap to calibrate on"}

    joined = joined.sort_values("hour_utc")
    cutoff = int(joined["hour_utc"].iloc[0]) + CALIBRATION_DAYS * 24 * 3600
    window = joined[joined["hour_utc"] <= cutoff]
    if len(window) < 50:
        return minutes, {"calibrated": False,
                         "why": f"only {len(window)} hours to calibrate on"}

    ratio = float(np.median(window["close_hf"] / window["close_store"]))
    reference = datetime.fromtimestamp(int(window["hour_utc"].median()),
                                       tz=timezone.utc).date()
    factor = corporate_actions.unadjust_factor(
        steps, minutes["hour_utc"].to_numpy(), reference, ratio)

    out = minutes.copy()
    for column in ("open", "high", "low", "close"):
        out[column] = out[column].to_numpy() / factor
    return out, {"calibrated": True, "ratio": ratio, "reference": reference,
                 "ex_dates": len(steps), "hours_used": len(window)}


def deepen_from_hfdata(asset: Asset, path: str, since: date, api_key: str,
                       session: requests.Session,
                       timezone_name: str | None = HFDATA_TIMEZONE) -> dict:
    """Fills a US-equity instrument's history below what is already stored.

    Same shape as the FX deepening: Twelve Data stays the live source and this
    reaches under it, so the two never compete for an hour. The minute bars are
    folded to the store's hourly grid by bars.to_hourly, which sums volume - so
    the consolidated-tape filter in hfdata.to_minute_frame has to have run
    first, or an hour would mix full-tape and IEX volume in one figure.
    """
    if asset.session_template != CALENDAR_TEMPLATE:
        return {"skipped": "not a US-equity instrument", "added": 0}
    if timezone_name is None:
        return {"skipped": "HFDATA_TIMEZONE is unset - run --probe-hfdata first",
                "added": 0}

    stored = bars.load(path)
    if stored.empty:
        return {"skipped": "nothing stored yet", "added": 0}
    oldest = datetime.fromtimestamp(int(stored["hour_utc"].min()), tz=timezone.utc)
    if oldest.date() <= since:
        return {"skipped": "already reaches back far enough", "added": 0}

    payload = hfdata.fetch_parquet(asset.ticker, api_key, session)
    minutes = hfdata.to_minute_frame(payload, timezone_name)
    if minutes.empty:
        return {"skipped": "no consolidated-tape bars returned", "added": 0}

    # Their prices are dividend-adjusted, measured: 0.972x of SPY's real close
    # at the end of 2019 falling to 0.733x in 2005, in both the raw and the
    # clean version. This store is deliberately unadjusted, so the factor is
    # taken back out before anything is compared or merged.
    steps = corporate_actions.load_steps().get(asset.ticker, [])
    minutes, adjustment = unadjust_to_store(minutes, stored, steps)

    # Earn the un-checkable years with the checkable ones before merging.
    check = verify_alignment(minutes, stored)
    if not check["ok"]:
        return {"skipped": f"alignment check failed: {check['why']}",
                "added": 0, "check": check, "adjustment": adjustment}

    lo = int(datetime.combine(since, datetime.min.time(),
                              tzinfo=timezone.utc).timestamp())
    hi = int(oldest.timestamp())
    window = minutes[(minutes["hour_utc"] >= lo) & (minutes["hour_utc"] < hi)]
    if window.empty:
        return {"skipped": "nothing in the window below the store", "added": 0}

    added = bars.merge(path, bars.to_hourly(window))
    return {"skipped": None, "added": added, "minutes": len(window),
            "from": since, "to": oldest.date(), "check": check,
            "adjustment": adjustment}


def fill_gaps_from_hfdata(asset: Asset, path: str, table: dict, api_key: str,
                          session: requests.Session,
                          timezone_name: str | None = HFDATA_TIMEZONE) -> dict:
    """Fills the HOURS Twelve Data does not hold, from HF Data's archive.

    The unit here is the hour, not the day. Twelve Data's 2020-21 damage is not
    only whole sessions: 2020-02-19 is present with two of its seven bars, and
    a day-level fill declares it healthy because something is there. Asking for
    every calendar hour the store lacks covers the whole sessions as a special
    case and the partial ones as well.

    These years are inside HF Data's consolidated-tape era - the same full
    CTA/UTP feed the surrounding Twelve Data bars come from, not the IEX subset
    that starts in March 2022 - so the hours are recoverable from a second
    source of the same kind, which is exactly what a second source is for.

    This writes INTO the middle of the stored series rather than under it, so
    the adjustment has to be right to the basis point or the patch shows up as
    a step where the store was continuous. The same calibration and the same
    two gates apply, and nothing is written unless both pass. Hours the store
    already holds are removed from the patch before the merge: bars.merge lets
    the incoming row win on a collision, and the live source keeps its own bars.
    """
    if asset.session_template != CALENDAR_TEMPLATE:
        return {"skipped": "no authoritative calendar", "added": 0, "gaps": 0}
    if timezone_name is None:
        return {"skipped": "HFDATA_TIMEZONE is unset", "added": 0, "gaps": 0}

    wanted = set(missing_hours(path, table))
    if not wanted:
        return {"skipped": None, "added": 0, "gaps": 0, "hours": 0,
                "still_missing": [], "still_missing_hours": 0}

    gaps = missing_sessions(path, table)
    stored = bars.load(path)
    payload = hfdata.fetch_parquet(asset.ticker, api_key, session)
    minutes = hfdata.to_minute_frame(payload, timezone_name)
    if minutes.empty:
        return {"skipped": "no consolidated-tape bars returned", "added": 0,
                "gaps": len(gaps), "hours": len(wanted)}

    steps = corporate_actions.load_steps().get(asset.ticker, [])
    minutes, adjustment = unadjust_to_store(minutes, stored, steps)
    check = verify_alignment(minutes, stored)
    if not check["ok"]:
        return {"skipped": f"alignment check failed: {check['why']}",
                "added": 0, "gaps": len(gaps), "hours": len(wanted),
                "check": check}

    # Fold first, then select. The hole is an hour of the store's grid, and
    # only after folding does a minute bar carry the stamp that can be compared
    # against it.
    hourly = bars.to_hourly(minutes)
    patch = hourly[hourly["hour_utc"].isin(wanted).to_numpy()]
    if patch.empty:
        return {"skipped": "the archive does not hold those hours either",
                "added": 0, "gaps": len(gaps), "hours": len(wanted),
                "still_missing": gaps, "still_missing_hours": len(wanted)}

    added = bars.merge(path, patch)
    left = missing_hours(path, table)
    return {"skipped": None, "added": added, "gaps": len(gaps),
            "hours": len(wanted), "still_missing": missing_sessions(path, table),
            "still_missing_hours": len(left),
            "check": check, "adjustment": adjustment}


def deepen_from_fxcm(asset: Asset, path: str, since: date,
                     session: requests.Session) -> dict:
    """Fills an FX pair's history BELOW what is already stored, from FXCM.

    Only the stretch older than the oldest stored bar is asked for. Twelve Data
    stays the live source for these pairs and keeps collecting the recent end;
    this reaches under it and stops, so the two never compete for the same hour
    and the merge cannot overwrite a live bar with an archived one.

    Nothing happens for a pair the archive does not carry - USD/CNY - or where
    the store already reaches back past `since`. Both are ordinary outcomes and
    are reported as zero rather than raised.
    """
    symbol = fxcm.symbol_for(asset.ticker)
    if symbol is None:
        return {"skipped": "no FXCM symbol", "added": 0}

    stored = bars.load(path)
    if stored.empty:
        # Deepening is defined against something. With nothing stored there is
        # no "below" to fill, and the ordinary Twelve Data backfill runs first.
        return {"skipped": "nothing stored yet", "added": 0}

    oldest = datetime.fromtimestamp(int(stored["hour_utc"].min()), tz=timezone.utc)
    if oldest.date() <= since:
        return {"skipped": "already reaches back far enough", "added": 0}

    candles = fxcm.fetch_history(symbol, since, oldest.date(), session)
    if not candles:
        return {"skipped": "archive returned nothing", "added": 0}

    added = bars.merge(path, bars.to_hourly(bars.candles_to_frame(candles)))
    return {"skipped": None, "added": added, "fetched": len(candles),
            "from": since, "to": oldest.date()}


# How far ABOVE the oldest stored bar to fetch before writing anything below it.
# Unlike the FXCM archive, which starts where the store already had bars and had
# to be spliced blind, Dukascopy covers the whole stored range - so an overlap
# can be bought for six extra requests a pair and the splice can be gated on it
# instead of trusted. Three months of a 24/5 pair is about 1500 hours against
# the 200 the check needs.
DUKASCOPY_OVERLAP_DAYS = 93


def deepen_from_dukascopy(asset: Asset, path: str, since: date,
                          session: requests.Session) -> dict:
    """Fills an FX pair's history BELOW what is already stored, from Dukascopy.

    The overlap is fetched but NOT written. Its whole purpose is to earn the
    right to write the years underneath it: those years have no second opinion
    anywhere, and the only evidence available that this archive can be trusted
    to sit beside the stored bars is that where the two do meet, they agree.
    The same two gates as everywhere else - returns must correlate and the
    LEVELS must match, because a series that agrees on returns while disagreeing
    on price is what a bad scale or a stale rate looks like, and on this source
    a wrong point size is a factor of a thousand.

    Nothing is written unless both pass, and nothing is written at or above the
    oldest stored bar even then: Twelve Data stays the live source, and a merge
    lets the incoming row win.
    """
    symbol = dukascopy.symbol_for(asset.ticker)
    if symbol is None:
        return {"skipped": "no Dukascopy symbol", "added": 0}

    stored = bars.load(path)
    if stored.empty:
        # Deepening is defined against something, and with nothing stored there
        # is also nothing to check the archive against - which for this source
        # matters more than usual.
        return {"skipped": "nothing stored yet", "added": 0}

    floor = stored["hour_utc"].min()
    oldest = datetime.fromtimestamp(int(floor), tz=timezone.utc)
    if oldest.date() <= since:
        return {"skipped": "already reaches back far enough", "added": 0}

    end = oldest.date() + timedelta(days=DUKASCOPY_OVERLAP_DAYS)
    candles = dukascopy.fetch_history(symbol, since, end, session)
    if not candles:
        return {"skipped": "archive returned nothing", "added": 0}

    frame = bars.to_hourly(bars.candles_to_frame(candles))
    check = verify_alignment(frame, stored)
    if not check["ok"]:
        return {"skipped": f"alignment check failed: {check['why']}",
                "added": 0, "check": check}

    below = frame[frame["hour_utc"] < int(floor)]
    if below.empty:
        return {"skipped": "nothing below the oldest stored bar", "added": 0,
                "check": check}
    added = bars.merge(path, below)
    return {"skipped": None, "added": added, "fetched": len(candles),
            "from": since, "to": oldest.date(), "check": check}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 0: backfill of Tremor hourly history")
    parser.add_argument("--instruments", default="",
                        help="Comma-separated tickers; the whole basket by default")
    parser.add_argument("--bars-dir", default=bars.DEFAULT_BARS_DIR)
    parser.add_argument("--vix-dir", default=bars.DEFAULT_VIX_DIR)
    parser.add_argument("--legacy-dir", default=LEGACY_HISTORY_DIR)
    parser.add_argument("--skip-vix", action="store_true")
    parser.add_argument("--extend-history", action="store_true",
                        help="ask from basket.history_since even where the store "
                             "already has bars, to deepen the archive backwards")
    parser.add_argument("--probe-hfdata", default="",
                        help="print the schema, source values and first "
                             "timestamps of one HF Data ticker, then stop. "
                             "Their column names and timezone are undocumented "
                             "and must be read rather than assumed.")
    parser.add_argument("--deepen-etfs", action="store_true",
                        help="fill the US-equity instruments' history below "
                             "what Twelve Data's plan serves, from HF Data's "
                             "consolidated-tape minute bars.")
    parser.add_argument("--fill-gaps", action="store_true",
                        help="re-ask Twelve Data for trading days the NYSE "
                             "calendar has and the store does not. Whether a "
                             "day is recoverable is the point: an empty answer "
                             "confirms the hole is the provider's.")
    parser.add_argument("--deepen-fx", action="store_true",
                        help="fill the FX pairs' history below what is stored "
                             "from FXCM's public archive, which reaches 2012 "
                             "where Twelve Data's plan stops at 2020. Needs no "
                             "key and touches no other instrument.")
    parser.add_argument("--deepen-dukascopy", action="store_true",
                        help="fill the FX pairs' history below what is stored "
                             "from Dukascopy's public archive, which reaches "
                             "2003 where FXCM's stops at 2012, and which also "
                             "carries USD/CNH. Needs no key. Unlike the FXCM "
                             "pass this one overlaps the store and is gated on "
                             "agreeing with it.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    basket = load_basket()
    wanted = {t.strip() for t in args.instruments.split(",") if t.strip()}
    instruments = [a for a in basket.instruments if not wanted or a.ticker in wanted]
    if wanted and not instruments:
        log.error("No instrument matched --instruments %s", args.instruments)
        return 2

    if args.probe_hfdata:
        import json

        key = os.environ.get("HFDATA_API_KEY", "")
        if not key:
            log.error("HFDATA_API_KEY is not set")
            return 2
        session = requests.Session()
        # Both versions in one dispatch. "clean" turned out to carry a
        # cumulative dividend factor - 0.733x of SPY's actual close in 2005 -
        # and whether "raw" does too is the whole question, so asking one at a
        # time would just cost a round trip to learn half the answer.
        for version in ("clean", "raw"):
            try:
                payload = hfdata.fetch_parquet(args.probe_hfdata, key, session,
                                               version=version)
            except Exception as exc:
                log.error("%s (%s): %s", args.probe_hfdata, version, exc)
                continue
            log.info("%s (%s): %d bytes", args.probe_hfdata, version, len(payload))
            report = hfdata.describe(payload)
            if args.probe_hfdata.upper() == "SPY":
                report["reference_closes"] = hfdata.reference_check(
                    payload, hfdata.SPY_REFERENCE_CLOSES)
            print(f"--- {version} ---")
            print(json.dumps(report, indent=2, default=str))
        return 0

    if args.deepen_etfs:
        key = os.environ.get("HFDATA_API_KEY", "")
        if not key:
            log.error("HFDATA_API_KEY is not set")
            return 2
        session = requests.Session()
        total = 0
        for asset in instruments:
            path = bars.store_path(args.bars_dir, asset.file_stem)
            try:
                out = deepen_from_hfdata(asset, path, basket.acquire_since,
                                         key, session)
            except Exception as exc:
                log.error("%s: HF Data deepening failed - %s", asset.asset_id, exc)
                continue
            if out["skipped"]:
                log.info("%s: skipped (%s)", asset.asset_id, out["skipped"])
                continue
            total += out["added"]
            check, adj = out["check"], out.get("adjustment", {})
            log.info("%s: +%d bars from HF Data (%s .. %s, %d minute bars); "
                     "un-adjusted by %.4f at %s over %d ex-dates; "
                     "overlap check %d hours, corr %.4f, median %.2fbp",
                     asset.asset_id, out["added"], out["from"], out["to"],
                     out["minutes"], adj.get("ratio", float("nan")),
                     adj.get("reference"), adj.get("ex_dates", 0),
                     check["hours"], check["correlation"], check["median_bp"])
        log.info("HF Data deepening added %d bars", total)
        return 0

    if args.fill_gaps:

        api_key = os.environ.get("TWELVEDATA_API_KEY", "")
        hf_key = os.environ.get("HFDATA_API_KEY", "")
        if not api_key:
            log.error("TWELVEDATA_API_KEY is not set")
            return 2
        table = _sessions.load_sessions()
        session = requests.Session()
        filled = unfilled = 0
        for asset in instruments:
            path = bars.store_path(args.bars_dir, asset.file_stem)
            try:
                out = fill_gaps(asset, path, table, api_key, session)
            except Exception as exc:
                log.error("%s: gap fill failed - %s", asset.asset_id, exc)
                continue
            if out["skipped"]:
                continue
            left = out["still_missing"] if out["gaps"] else []
            if out["gaps"]:
                log.info("%s: %d gap(s), +%d bars from Twelve Data, %d still "
                         "missing%s", asset.asset_id, out["gaps"], out["added"],
                         len(left), f" {[str(d) for d in left]}" if left else "")

            # A day is not a unit of completeness. The store can hold a session
            # and still be missing five of its seven bars, and asking only "is
            # the day there" walks past that. The second source is offered when
            # EITHER measure finds something wrong.
            hours_left = missing_hours(path, table)
            hours_left_before = list(hours_left)
            if not left and not hours_left:
                log.info("%s: no gaps", asset.asset_id)
                continue
            if hours_left and not left:
                log.info("%s: whole sessions complete, %d hour(s) missing "
                         "inside them", asset.asset_id, len(hours_left))

            # Twelve Data does not hold them - proven, 0 of 21 recovered in run
            # 33994110137 - so anything still missing goes to the second source
            # of the same kind. Tried in this order because a day recovered from
            # the vendor the surrounding bars already come from needs no
            # adjustment and no calibration to sit correctly beside them.
            if (left or hours_left) and hf_key:
                try:
                    out = fill_gaps_from_hfdata(asset, path, table, hf_key,
                                                session)
                except Exception as exc:
                    log.error("%s: HF Data gap fill failed - %s",
                              asset.asset_id, exc)
                    out = {"skipped": str(exc), "added": 0,
                           "still_missing": left}
                if out.get("skipped"):
                    log.info("%s: HF Data skipped (%s)", asset.asset_id,
                             out["skipped"])
                else:
                    left = out["still_missing"]
                    log.info("%s: +%d bars from HF Data, %d hour(s) and %d "
                             "session(s) still missing%s", asset.asset_id,
                             out["added"], out.get("still_missing_hours", 0),
                             len(left),
                             f" {[str(d) for d in left]}" if left else "")
                    hours_left = [None] * out.get("still_missing_hours", 0)

            filled += len(hours_left_before) - len(hours_left)
            unfilled += len(hours_left)
        log.info("gap fill: %d hour(s) recovered, %d confirmed missing at "
                 "the source", filled, unfilled)
        return 0

    if args.deepen_fx:
        # Its own mode rather than a step inside the usual pass: it needs no
        # API key, spends no Twelve Data credits, and touches only the pairs
        # the archive carries. Mixing it in would make a run that fails for
        # want of a key also fail to do the part that never needed one.
        session = requests.Session()
        total = 0
        for asset in instruments:
            path = bars.store_path(args.bars_dir, asset.file_stem)
            try:
                result = deepen_from_fxcm(asset, path, basket.acquire_since, session)
            except Exception as exc:
                log.error("%s: FXCM deepening failed - %s", asset.asset_id, exc)
                continue
            if result["skipped"]:
                log.info("%s: skipped (%s)", asset.asset_id, result["skipped"])
                continue
            total += result["added"]
            log.info("%s: +%d bars from FXCM (%s .. %s, %d fetched)",
                     asset.asset_id, result["added"], result["from"],
                     result["to"], result["fetched"])
        log.info("FXCM deepening added %d bars", total)
        return 0

    if args.deepen_dukascopy:
        # Same reasoning as the FXCM mode for being its own: no key, no credits,
        # and only the pairs the archive carries. Run per pair from the workflow
        # - each is a few hundred requests, and a failure part way through then
        # costs one pair rather than all eight.
        session = requests.Session()
        total = 0
        for asset in instruments:
            path = bars.store_path(args.bars_dir, asset.file_stem)
            try:
                result = deepen_from_dukascopy(asset, path, basket.acquire_since,
                                               session)
            except Exception as exc:
                log.error("%s: Dukascopy deepening failed - %s",
                          asset.asset_id, exc)
                continue
            if result["skipped"]:
                log.info("%s: skipped (%s)", asset.asset_id, result["skipped"])
                continue
            total += result["added"]
            check = result["check"]
            log.info("%s: +%d bars from Dukascopy (%s .. %s, %d fetched); "
                     "overlap check %d hours, corr %.4f, median %.2fbp",
                     asset.asset_id, result["added"], result["from"],
                     result["to"], result["fetched"], check["hours"],
                     check["correlation"], check["median_bp"])
        log.info("Dukascopy deepening added %d bars", total)
        return 0

    api_key = os.environ.get("TWELVEDATA_API_KEY", "")
    if not api_key and any(a.fetched_from == "twelvedata" for a in instruments):
        log.error("TWELVEDATA_API_KEY is not set, and the list contains Twelve Data instruments")
        return 2

    tiingo_key = os.environ.get("TIINGO_API_KEY", "")
    if not tiingo_key and any(a.fetched_from == "tiingo" for a in instruments):
        log.error("TIINGO_API_KEY is not set, and the list contains Tiingo instruments")
        return 2

    session = requests.Session()
    # Loaded once. A missing table is not an error here - it only means no
    # instrument can be skipped, which is the safe direction.
    try:
        session_table = _sessions.load_sessions()
    except FileNotFoundError:
        log.warning("No session table; every instrument will be asked for")
        session_table = None

    failures = 0
    skipped = 0
    seeded = 0
    quota_gone = False
    tiingo_gone = False
    tiingo_skipped = 0
    tiingo_trip = None
    tiingo_remaining = None
    dark: list[tuple[str, str, str]] = []
    for i, asset in enumerate(instruments):
        path = bars.store_path(args.bars_dir, asset.file_stem)
        if not args.extend_history and bars.load(path).empty:
            # Only a few never-seen instruments per run. Adding a block of new
            # tickers to the configuration otherwise turns the next hourly run
            # into a batch job: the free tier is 800 credits a day and eight a
            # minute, so thirty-eight empty stores would spend the budget and
            # overrun the job. They are seeded a handful at a time and are all
            # producing bars within a few hours.
            if seeded >= SEED_PER_RUN:
                skipped += 1
                log.info("%s: new instrument, waiting its turn to be seeded",
                         asset.asset_id)
                continue
            seeded += 1
        elif not args.extend_history and nothing_can_have_appeared(
                asset, path, session_table):
            skipped += 1
            log.info("%s: market closed since the newest stored bar, not asked for",
                     asset.asset_id)
            continue
        if tiingo_gone and asset.fetched_from == "tiingo":
            skipped += 1
            tiingo_skipped += 1
            continue
        try:
            r = backfill_instrument(asset, basket, args.bars_dir, api_key, session,
                                    args.legacy_dir, args.extend_history, tiingo_key)
            log.info("%s: %d bars (%s .. %s), from local history %d, from network %d",
                     r["asset_id"], r["rows"], _fmt(r["first"]), _fmt(r["last"]),
                     r["from_legacy"], r["from_api"])
        except twelvedata.DailyQuotaExhausted as exc:
            # STOP THE WHOLE LOOP, and this is the difference between a run that
            # delivers on slightly stale bars and a run that delivers nothing.
            # The budget is per key and per day, so every instrument after this
            # one fails too; asking them anyway cost 32 seconds each and pushed
            # the job past its timeout, which skipped delivery entirely.
            quota_gone = True
            log.error("Twelve Data daily credits are gone - %s", exc)
            log.error("Stopping the fetch here. %d instrument(s) not asked for; "
                      "the pipeline continues on the bars already stored.",
                      len(instruments) - i - 1)
            break
        except tiingo.RateLimited as exc:
            # Tiingo's 50-an-hour bucket, or its 1000-a-day one. Neither clears
            # inside a run, so every later Tiingo instrument would spend a
            # round trip to be told the same thing. The others carry on: this
            # is one provider being out, not the run failing. Provider is not
            # switched automatically - a silent fall back to slower or
            # different-priced data cannot happen unnoticed.
            tiingo_gone = True
            tiingo_trip = asset.asset_id
            tiingo_remaining = getattr(exc, "remaining", None)
            log.error("Tiingo's request budget is spent - %s", exc)
            log.error("Skipping the remaining Tiingo instruments; the other "
                      "providers continue.")
        except Exception as exc:
            failures += 1
            dark.append((asset.asset_id, asset.fetched_from, str(exc)))
            log.error("%s: failed - %s", asset.asset_id, exc)
        # The 8-requests-per-minute limit is per key, and the running hourly
        # monitor spends it too - the pause is needed between instruments as well.
        # Only Twelve Data is paced: Coinbase, Tiingo and Yahoo have no enforced
        # rate, and pausing after them would spend the wait twice over.
        if asset.fetched_from == "twelvedata" and i < len(instruments) - 1:
            time.sleep(TWELVEDATA_DELAY_SECONDS)

    if skipped:
        log.info("%d instrument(s) skipped: the calendar says nothing new can exist",
                 skipped)
    # Not counted as a failure: a spent budget is a known limit being reached,
    # not a breakage, and failing the run would turn a daily certainty into a
    # daily red cross. It is logged loudly instead - and the run still delivers.
    if quota_gone:
        log.warning("The Twelve Data budget is spent for the UTC day. "
                    "Bars will resume at midnight.")
    if tiingo_gone:
        log.warning("The Tiingo request budget is spent. The hourly bucket "
                    "refills within the hour; the daily one at midnight UTC.")

    if not args.skip_vix:
        try:
            r = backfill_vix(basket, args.vix_dir,
                             os.environ.get("FRED_API_KEY", ""), session)
            log.info("%s: %d daily values (%s .. %s) from %s",
                     r["asset_id"], r["rows"], _fmt(r["first"]), _fmt(r["last"]),
                     " + ".join(r["sources"]))
            # Warned rather than failed: one source is enough to deliver, and a
            # run that goes red over a mirror being down would cry wolf. The
            # message names which one so a silent degradation is still visible.
            for note in r["trouble"]:
                log.warning("VIX source unavailable - %s", note)
        except Exception as exc:
            failures += 1
            dark.append(("VIX", "cboe/fred", str(exc)))
            log.error("VIX: failed - %s", exc)

    text = format_provider_failure(
        dark, tiingo_gone=tiingo_gone, tiingo_skipped=tiingo_skipped,
        tiingo_remaining=tiingo_remaining, tiingo_trip=tiingo_trip)
    if text:
        send_ops_alert(text)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

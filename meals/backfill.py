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

from meals import bars, fred
from meals.basket import Asset, Basket, load_basket
from price_monitor import candle_store, coinbase, fxcm, hfdata, twelvedata
from price_monitor.models import ExchangeError

log = logging.getLogger("meals.backfill")

LEGACY_HISTORY_DIR = os.path.join("data", "candle_history")

COINBASE_BASE_URL = "https://api.exchange.coinbase.com"
TWELVEDATA_BASE_URL = "https://api.twelvedata.com"

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


def _days_since(start: date) -> float:
    return max(1.0, (datetime.now(timezone.utc) - datetime.combine(
        start, datetime.min.time(), tzinfo=timezone.utc)).total_seconds() / 86400)


def import_legacy(asset: Asset, path: str, legacy_dir: str = LEGACY_HISTORY_DIR) -> int:
    """Moves the already accumulated NDJSON history into Parquet. The file-naming
    scheme is the same in candle_store and in MEALS, so the mapping is direct.
    """
    legacy_path = candle_store.store_path(legacy_dir, asset.source, asset.ticker)
    if not os.path.exists(legacy_path):
        return 0
    candles = candle_store.load_candles(legacy_path)
    if not candles:
        return 0
    return bars.merge(path, bars.to_hourly(bars.candles_to_frame(candles)))


def fetch_missing(asset: Asset, path: str, since: date, api_key: str,
                  session: requests.Session, extend_history: bool = False) -> int:
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
        days = _days_since(since)
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

    if asset.source == "twelvedata":
        candles = twelvedata.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            base_url=TWELVEDATA_BASE_URL, api_key=api_key, session=session,
            request_delay_seconds=TWELVEDATA_DELAY_SECONDS,
            chunk_days=CHUNK_DAYS[asset.fetch_interval], end=end,
        )
    elif asset.source == "coinbase":
        candles = coinbase.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            base_url=COINBASE_BASE_URL, session=session,
        )
    else:
        raise ExchangeError(f"{asset.asset_id}: unknown source '{asset.source}'")

    return bars.merge(path, bars.to_hourly(bars.candles_to_frame(candles)))


def backfill_instrument(asset: Asset, basket: Basket, bars_dir: str, api_key: str,
                        session: requests.Session, legacy_dir: str = LEGACY_HISTORY_DIR,
                        extend_history: bool = False) -> dict:
    path = bars.store_path(bars_dir, asset.file_stem)
    from_legacy = import_legacy(asset, path, legacy_dir)
    from_api = fetch_missing(asset, path, basket.acquire_since, api_key, session,
                             extend_history)
    stored = bars.load(path)
    return {
        "asset_id": asset.asset_id,
        "from_legacy": from_legacy,
        "from_api": from_api,
        "rows": len(stored),
        "first": int(stored["hour_utc"].min()) if not stored.empty else None,
        "last": int(stored["hour_utc"].max()) if not stored.empty else None,
    }


def backfill_vix(basket: Basket, vix_dir: str, api_key: str,
                 session: requests.Session) -> dict:
    vix = basket.volatility_index
    frame = fred.fetch_series(vix.series_id, api_key, vix.history_since, session=session)
    path = os.path.join(vix_dir, f"{vix.file_stem}.parquet")
    os.makedirs(vix_dir, exist_ok=True)
    frame.sort_values("day").reset_index(drop=True).to_parquet(
        path, index=False, compression="zstd")
    return {"asset_id": vix.series_id, "rows": len(frame),
            "first": int(frame["day"].min()), "last": int(frame["day"].max())}


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

    return {"skipped": None, "added": added, "gaps": len(gaps),
            "still_missing": missing_sessions(path, table)}


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

    # Earn the un-checkable years with the checkable ones before merging.
    check = verify_alignment(minutes, stored)
    if not check["ok"]:
        return {"skipped": f"alignment check failed: {check['why']}",
                "added": 0, "check": check}

    lo = int(datetime.combine(since, datetime.min.time(),
                              tzinfo=timezone.utc).timestamp())
    hi = int(oldest.timestamp())
    window = minutes[(minutes["hour_utc"] >= lo) & (minutes["hour_utc"] < hi)]
    if window.empty:
        return {"skipped": "nothing in the window below the store", "added": 0}

    added = bars.merge(path, bars.to_hourly(window))
    return {"skipped": None, "added": added, "minutes": len(window),
            "from": since, "to": oldest.date(), "check": check}


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 0: backfill of MEALS hourly history")
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
            check = out["check"]
            log.info("%s: +%d bars from HF Data (%s .. %s, %d minute bars); "
                     "overlap check %d hours, corr %.4f, median %.2fbp",
                     asset.asset_id, out["added"], out["from"], out["to"],
                     out["minutes"], check["hours"], check["correlation"],
                     check["median_bp"])
        log.info("HF Data deepening added %d bars", total)
        return 0

    if args.fill_gaps:
        from meals import sessions as _sessions

        api_key = os.environ.get("TWELVEDATA_API_KEY", "")
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
            if not out["gaps"]:
                log.info("%s: no gaps", asset.asset_id)
                continue
            left = out["still_missing"]
            filled += out["gaps"] - len(left)
            unfilled += len(left)
            log.info("%s: %d gap(s), +%d bars, %d still missing%s",
                     asset.asset_id, out["gaps"], out["added"], len(left),
                     f" {[str(d) for d in left]}" if left else "")
        log.info("gap fill: %d recovered, %d confirmed missing at the source",
                 filled, unfilled)
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

    api_key = os.environ.get("TWELVEDATA_API_KEY", "")
    if not api_key and any(a.source == "twelvedata" for a in instruments):
        log.error("TWELVEDATA_API_KEY is not set, and the list contains Twelve Data instruments")
        return 2

    session = requests.Session()
    failures = 0
    for i, asset in enumerate(instruments):
        try:
            r = backfill_instrument(asset, basket, args.bars_dir, api_key, session,
                                    args.legacy_dir, args.extend_history)
            log.info("%s: %d bars (%s .. %s), from local history %d, from network %d",
                     r["asset_id"], r["rows"], _fmt(r["first"]), _fmt(r["last"]),
                     r["from_legacy"], r["from_api"])
        except Exception as exc:
            failures += 1
            log.error("%s: failed - %s", asset.asset_id, exc)
        # The 8-requests-per-minute limit is per key, and the running hourly
        # monitor spends it too - the pause is needed between instruments as well.
        if asset.source == "twelvedata" and i < len(instruments) - 1:
            time.sleep(TWELVEDATA_DELAY_SECONDS)

    if not args.skip_vix:
        fred_key = os.environ.get("FRED_API_KEY", "")
        if not fred_key:
            log.error("FRED_API_KEY is not set - the VIX series was skipped")
            failures += 1
        else:
            try:
                r = backfill_vix(basket, args.vix_dir, fred_key, session)
                log.info("%s: %d daily values (%s .. %s)",
                         r["asset_id"], r["rows"], _fmt(r["first"]), _fmt(r["last"]))
            except Exception as exc:
                failures += 1
                log.error("VIX: failed - %s", exc)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

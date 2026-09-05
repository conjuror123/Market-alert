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
from datetime import date, datetime, timezone

import requests

from meals import bars, fred
from meals.basket import Asset, Basket, load_basket
from price_monitor import candle_store, coinbase, fxcm, twelvedata
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
    from_api = fetch_missing(asset, path, basket.history_since, api_key, session,
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
                result = deepen_from_fxcm(asset, path, basket.history_since, session)
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

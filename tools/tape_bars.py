"""Alpaca's consolidated tape (SIP) for every US fund over a span of dates, saved
for a measurement: the thirty-minute regular-session bars as Alpaca serves them.

    python -m tools.tape_bars OUT [--start 2016-01-01] [--end 2023-01-01] [--instruments A,B]

Needs ALPACA_KEY_ID and ALPACA_SECRET_KEY. A fund renamed since is asked under
its old name (jump.backfill.FORMER_TICKERS). Writes OUT/<file_stem>.parquet; a
fund that fails is listed in OUT/failed.txt, and a run with lines there is
partial. The bot never runs this: it writes nothing to the store.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timezone

from jump import bars
from jump.basket import load_basket

log = logging.getLogger(__name__)

# Alpaca's free plan allows 200 requests a minute; a fund is two to four pages.
PAUSE_SECONDS = 0.4


def dump(instruments, out_dir: str, start: datetime, end: datetime, auth: dict,
         session=None, pause: float = PAUSE_SECONDS) -> "list[str]":
    """Every US fund's tape bars into `out_dir`; returns the failures, one line each."""
    from jump.backfill import CALENDAR_TEMPLATE, FORMER_TICKERS
    from price_monitor import alpaca

    os.makedirs(out_dir, exist_ok=True)
    failed = []
    for asset in instruments:
        if asset.session_template != CALENDAR_TEMPLATE:
            continue
        symbol = FORMER_TICKERS.get(asset.ticker, asset.ticker)
        try:
            candles = alpaca.fetch_history(symbol, start, end, auth, session, feed="sip")
        except Exception as exc:
            log.warning("%s failed - %s", asset.ticker, exc)
            failed.append(f"{asset.ticker}: {exc}")
            continue
        bars.candles_to_frame(candles).to_parquet(
            os.path.join(out_dir, f"{asset.file_stem}.parquet"), index=False)
        log.info("%s: %d bars", asset.ticker, len(candles))
        time.sleep(pause)
    with open(os.path.join(out_dir, "failed.txt"), "w", encoding="utf-8") as f:
        f.write(f"{start:%Y-%m-%d} .. {end:%Y-%m-%d}\n")
        f.writelines(f"{line}\n" for line in failed)
    return failed


def main(argv: "list[str] | None" = None) -> int:
    import requests

    from price_monitor import alpaca

    parser = argparse.ArgumentParser(description="Alpaca's tape for every US fund, for a measurement")
    parser.add_argument("out_dir")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2023-01-01")
    parser.add_argument("--instruments", default="", help="comma-separated tickers")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    auth = alpaca.headers(os.environ.get("ALPACA_KEY_ID", "").strip(),
                          os.environ.get("ALPACA_SECRET_KEY", "").strip())
    wanted = {t.strip() for t in args.instruments.split(",") if t.strip()}
    instruments = [a for a in load_basket().instruments if not wanted or a.ticker in wanted]
    start, end = (datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
                  for d in (args.start, args.end))
    failed = dump(instruments, args.out_dir, start, end, auth, requests.Session())
    print(f"{len(failed)} fund(s) failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

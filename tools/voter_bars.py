"""Every live voter's bars of every instrument, saved for a measurement: what a
change to the vote would decide, on the same bars for both sides.

    python -m tools.voter_bars OUT [--days 35] [--instruments A,B]

Each source the live vote asks (jump.verify.sources_for - never Tiingo,
SiftingIO, Twelve Data or Google; Alpaca's tape only where its keys are set)
is fetched as the vote fetches it (verify.fetch_verifier), over `days` or its
own reach if shorter, and written to OUT/<source>/<file_stem>.parquet. A source
that fails is listed in OUT/failed.txt; a run with lines there is partial. The
bot never runs this: it writes nothing to the store or the record.
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone

from jump import verify
from jump.basket import load_basket

log = logging.getLogger(__name__)


def dump(instruments, out_dir: str, days: float, session=None,
         now: "datetime | None" = None) -> "list[str]":
    """Every voter's bars into `out_dir`; returns the failures, one line each."""
    now = now or datetime.now(timezone.utc)
    failed = []
    for asset in instruments:
        for src in verify.sources_for(asset):
            try:
                bars = verify.fetch_verifier(src.name, src.symbol(asset), src.interval,
                                             min(days, src.days), session, now)
            except Exception as exc:
                log.warning("%s from %s failed - %s", asset.ticker, src.name, exc)
                failed.append(f"{asset.ticker} {src.name}: {exc}")
                continue
            path = os.path.join(out_dir, src.name)
            os.makedirs(path, exist_ok=True)
            bars.to_parquet(os.path.join(path, f"{asset.file_stem}.parquet"), index=False)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "failed.txt"), "w", encoding="utf-8") as f:
        f.write(f"fetched {now:%Y-%m-%d %H:%M} UTC, {days:g} days\n")
        f.writelines(f"{line}\n" for line in failed)
    return failed


def main(argv: "list[str] | None" = None) -> int:
    import requests

    parser = argparse.ArgumentParser(description="Every live voter's bars, for a measurement")
    parser.add_argument("out_dir")
    parser.add_argument("--days", type=float, default=35.0)
    parser.add_argument("--instruments", default="", help="comma-separated tickers")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    wanted = {t.strip() for t in args.instruments.split(",") if t.strip()}
    instruments = [a for a in load_basket().instruments if not wanted or a.ticker in wanted]
    failed = dump(instruments, args.out_dir, args.days, requests.Session())
    print(f"{len(failed)} source(s) failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

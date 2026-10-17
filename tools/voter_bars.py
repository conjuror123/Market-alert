"""Every live voter's bars of every instrument, saved for a measurement.

    python -m tools.voter_bars OUT [--days 35 | --recount [--bars-dir DIR]] [--instruments A,B]

Each source the live vote asks (jump.verify.sources_for - the keyed ones
only where their keys are set, each costing one call of the dashboard's share
of config/provider_limits.csv per instrument it votes on)
is fetched as the vote fetches it (verify.fetch_verifier) and written to
OUT/<source>/<file_stem>.parquet. A source that fails is listed in
OUT/failed.txt; a run with lines there is partial. The bot never runs this: it
writes nothing to the store or the record.

HOW FAR BACK. `--days`: that many days, or the source's own reach if shorter.
`--recount` (the dashboard, tools/measure.py): what the live pass would ask for
the moves it votes - each instrument's recount window (verify.recount_days: 9
days for the funds and pairs, 29 for the coins, 79 for the LME), reaching back
to the store's last bar before the window, where the earliest move it could
vote starts (a Monday gap starts at Friday's close), and a day more, as the
pass asks (verify.verify).
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone

from jump import bars, verify
from jump.basket import load_basket

log = logging.getLogger(__name__)

DAY = 86400


def recount_span(asset, bars_dir: str, now: int) -> float:
    """Days back the live pass would ask at most for the instrument's moves
    inside its recount window: from the store's last bar before the window,
    and a day more."""
    since = now - int(verify.recount_days(asset) * DAY)
    frame = bars.load(bars.store_path(bars_dir, asset.file_stem), since - 60 * DAY)
    before = frame.loc[frame["hour_utc"] < since, "hour_utc"]
    start = int(before.max()) if len(before) else since
    return (now - start) / DAY + 1


def dump(instruments, out_dir: str, days: "float | None", session=None,
         now: "datetime | None" = None, bars_dir: "str | None" = None) -> "list[str]":
    """Every voter's bars into `out_dir`; returns the failures, one line each.
    `days` None: each instrument over its recount window (recount_span), as the
    live pass asks, not capped by the source's reach."""
    now = now or datetime.now(timezone.utc)
    failed = []
    for asset in instruments:
        sources = verify.sources_for(asset)
        if not sources:                     # nobody votes on it
            continue
        span = recount_span(asset, bars_dir or bars.DEFAULT_BARS_DIR, int(now.timestamp())) \
            if days is None else None
        for src in sources:
            try:
                got = verify.fetch_verifier(src.name, src.symbol(asset), src.interval,
                                              span if days is None else min(days, src.days),
                                              session, now)
            except Exception as exc:
                log.warning("%s from %s failed - %s", asset.ticker, src.name, exc)
                failed.append(f"{asset.ticker} {src.name}: {exc}")
                continue
            path = os.path.join(out_dir, src.name)
            os.makedirs(path, exist_ok=True)
            got.to_parquet(os.path.join(path, f"{asset.file_stem}.parquet"), index=False)
    os.makedirs(out_dir, exist_ok=True)
    how = "each instrument's recount window" if days is None else f"{days:g} days"
    with open(os.path.join(out_dir, "failed.txt"), "w", encoding="utf-8") as f:
        f.write(f"fetched {now:%Y-%m-%d %H:%M} UTC, {how}\n")
        f.writelines(f"{line}\n" for line in failed)
    return failed


def main(argv: "list[str] | None" = None) -> int:
    from jump.backfill import record_usage
    from jump.usage import Usage

    parser = argparse.ArgumentParser(description="Every live voter's bars, for a measurement")
    parser.add_argument("out_dir")
    how = parser.add_mutually_exclusive_group()
    how.add_argument("--days", type=float, default=35.0)
    how.add_argument("--recount", action="store_true",
                     help="each instrument's recount window, as the live pass asks")
    parser.add_argument("--bars-dir", default=bars.DEFAULT_BARS_DIR)
    parser.add_argument("--instruments", default="", help="comma-separated tickers")
    parser.add_argument("--purpose", choices=("manual", "dashboard"), default="manual",
                        help="what its calls are counted as (jump.usage)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    wanted = {t.strip() for t in args.instruments.split(",") if t.strip()}
    instruments = [a for a in load_basket().instruments if not wanted or a.ticker in wanted]
    usage = Usage()
    try:
        failed = dump(instruments, args.out_dir, None if args.recount else args.days,
                      usage.session(args.purpose), bars_dir=args.bars_dir)
    finally:
        log.info(usage.line())
        record_usage(usage)
    print(f"{len(failed)} source(s) failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

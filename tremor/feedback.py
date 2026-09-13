"""What the reader thought of a message, and what the knobs should be given that.

THE PROBLEM THIS EXISTS FOR. Both knobs - `sensitivity` and `min_move_sigma` -
are numbers somebody has to choose, and nothing in the data says what they
should be. Rarity is measurable; whether a line was worth reading is not. Only
the person being interrupted knows that, and until now there was nowhere for
them to say so, so the numbers were set by argument and left.

The asymmetry that decides the design: a reader can point at a message and say
it was not worth sending, but cannot point at a message that never arrived. So
the system should err loud - send slightly too much - and be turned down from
recorded judgements, rather than err quiet and never learn what it swallowed.
`missed` exists for the other direction and is rarer by nature: it can only be
used for something noticed elsewhere.

KEYED BY WHAT THE MESSAGE ALREADY SHOWS. A verdict is (ticker, the hour in the
message), so nothing has to be added to the message itself and nothing has to be
kept in sync with it. Messages quote a bar by its CLOSING moment - "in the hour
to 13:00" - and the store keys bars by their opening, so a quoted time is
matched against the nearest bar rather than assumed to be either; the match is
printed, so a wrong one is visible rather than silent.

WHAT IT DOES NOT DO. It does not turn the knobs. It says what they would have to
be and what that would cost elsewhere, and a person decides - a loop that
retunes itself from a handful of judgements would chase the last thing that
annoyed anyone.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("tremor.feedback")

DEFAULT_FEEDBACK_PATH = os.path.join("data", "tremor", "feedback.csv")
DEFAULT_EVENTS_PATH = os.path.join("data", "tremor", "saed_events.parquet")
COLUMNS = ("asset_id", "hour_utc", "verdict", "noted_at")

BORING = "boring"      # this arrived and was not worth reading
MISSED = "missed"      # this did not arrive and should have
VERDICTS = (BORING, MISSED)

# How far from the quoted hour to look for the bar meant. A message names a bar
# by its closing moment and the store names it by its opening, so one hour is
# already expected; two covers a reader rounding to the nearest hour.
MATCH_TOLERANCE_HOURS = 2


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_when(text: str) -> int:
    """A time as it appears in a message, to an epoch second."""
    text = text.strip().replace("UTC", "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt)
                       .replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    raise ValueError(f"cannot read a time from {text!r} - expected "
                     f"'2026-09-09 13:00'")


def resolve_asset(ticker: str, basket) -> str:
    """A ticker as a person types it, to an asset_id."""
    want = ticker.strip().upper()
    for a in basket.instruments:
        if a.asset_id.upper() == want or a.ticker.upper() == want:
            return a.asset_id
    # A slash is easy to drop: EURUSD for EUR/USD.
    squashed = want.replace("/", "")
    for a in basket.instruments:
        if a.ticker.upper().replace("/", "") == squashed:
            return a.asset_id
    raise ValueError(f"{ticker!r} is not an instrument in the basket")


def load(path: str = DEFAULT_FEEDBACK_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return [row for row in csv.DictReader(f) if row.get("verdict") in VERDICTS]


def record(asset_id: str, hour_utc: int, verdict: str,
           path: str = DEFAULT_FEEDBACK_PATH) -> None:
    """Appends one judgement. Re-judging the same bar replaces the old verdict."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    rows = [r for r in load(path)
            if not (r["asset_id"] == asset_id and int(r["hour_utc"]) == hour_utc)]
    rows.append({"asset_id": asset_id, "hour_utc": str(hour_utc),
                 "verdict": verdict, "noted_at": _now()})
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(COLUMNS), lineterminator="\n")
        writer.writeheader()
        for r in sorted(rows, key=lambda r: (r["asset_id"], int(r["hour_utc"]))):
            writer.writerow({k: r[k] for k in COLUMNS})


def attach(rows: list[dict], events, tolerance_hours: int = MATCH_TOLERANCE_HOURS):
    """Each judgement beside the event it refers to, or None where none is near."""
    out = []
    for r in rows:
        aid, hour = r["asset_id"], int(r["hour_utc"])
        mine = events[events["asset_id"] == aid]
        match = None
        if len(mine):
            gap = (mine["hour_utc"] - hour).abs()
            near = gap <= tolerance_hours * 3600
            if near.any():
                match = mine.loc[gap.where(near).idxmin()]
        out.append((r, match))
    return out


def suggest(rows: list[dict], events) -> list[str]:
    """What the knobs would have to be, and what that would cost elsewhere."""
    import numpy as np

    lines: list[str] = []
    paired = attach(rows, events)
    size = (events["r"].abs() / events["sigma_lt"]).replace(
        [np.inf, -np.inf], np.nan)

    boring = [(r, m) for r, m in paired if r["verdict"] == BORING and m is not None]
    missed = [(r, m) for r, m in paired if r["verdict"] == MISSED and m is not None]
    unmatched = [r for r, m in paired if m is None]

    for r in unmatched:
        lines.append(f"  no event within {MATCH_TOLERANCE_HOURS}h of "
                     f"{r['asset_id']} {r['hour_utc']} - nothing to learn from it")

    if boring:
        sizes = [abs(m["r"]) / m["sigma_lt"] if m["sigma_lt"] else np.nan
                 for _, m in boring]
        sizes = [s for s in sizes if np.isfinite(s)]
        lines.append(f"{len(boring)} judged not worth reading:")
        for (_, m), s in zip(boring, sizes):
            lines.append(f"  {m['asset_id']:<22} {m['tier']:<11} {m['basis']:<9}"
                         f" {abs(m['r'])*100:6.2f}%  {s:5.2f}x its usual hour")
        if sizes:
            from tremor.basket import load_tuning

            tuning = load_tuning()
            lines.append("")
            # PER INSTRUMENT, and that is the whole change. One shared floor
            # raised high enough to silence the instrument that annoyed the
            # reader silences every quiet instrument with it - the reader
            # pointed at BKLN and lost SHY. The verdict names an instrument, so
            # the answer should too.
            lines.append("  the floor each of these implies, for that instrument "
                         "alone:")
            by_asset: dict[str, list[float]] = {}
            for (_, m), s in zip(boring, sizes):
                by_asset.setdefault(str(m["asset_id"]), []).append(s)
            for asset_id, seen in sorted(by_asset.items()):
                need = max(seen)
                current = tuning.floor_for(asset_id)
                mine = events[events["asset_id"].eq(asset_id)]
                cost = int((size[mine.index] < need).sum()) if len(mine) else 0
                lines.append(f"    {asset_id:<22} {current:g} -> {need:.2f}x  "
                             f"(takes {cost} of this instrument's {len(mine)} "
                             f"events, and none of anyone else's)")
                if current >= need:
                    lines.append(f"      already at {current:g} - these predate it")
                if need > 3.0:
                    lines.append("      but that is a large floor and these were "
                                 "not small moves: the floor is the wrong lever "
                                 "here, and sensitivity is the one")
            lines.append("")
            lines.append("  in config/basket.yaml, on that instrument's own entry:")
            for asset_id, seen in sorted(by_asset.items()):
                lines.append(f"    - ticker: {asset_id.split(':')[-1]}"
                             f"    # ... its existing fields")
                lines.append(f"      min_move_sigma: {max(seen):.2f}")

    if missed:
        lines.append("")
        lines.append(f"{len(missed)} judged missed, but an event exists for each - "
                     f"so they were found and not delivered. That is a routing "
                     f"question (which tiers push), not a sensitivity one:")
        for _, m in missed:
            lines.append(f"  {m['asset_id']:<22} {m['tier']:<11} "
                         f"{m['channel']:<7} {abs(m['r'])*100:6.2f}%")

    if not rows:
        lines.append("  nothing recorded yet - mark a message with --boring or --missed")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record what a message was worth, and what the knobs imply")
    parser.add_argument("--boring", metavar="'TICKER 2026-09-09 13:00'",
                        help="this arrived and was not worth reading")
    parser.add_argument("--missed", metavar="'TICKER 2026-09-09 13:00'",
                        help="this did not arrive and should have")
    parser.add_argument("--events", default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--out", default=DEFAULT_FEEDBACK_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from tremor.basket import load_basket

    for verdict, text in ((BORING, args.boring), (MISSED, args.missed)):
        if not text:
            continue
        ticker, _, when = text.strip().partition(" ")
        asset_id = resolve_asset(ticker, load_basket())
        hour = parse_when(when)
        record(asset_id, hour, verdict, args.out)
        log.info("recorded %s: %s at %s", verdict, asset_id,
                 datetime.fromtimestamp(hour, tz=timezone.utc))

    rows = load(args.out)
    if not os.path.exists(args.events):
        log.info("%d judgement(s) recorded; %s not present, so nothing to weigh "
                 "them against yet", len(rows), args.events)
        return 0

    import pandas as pd

    events = pd.read_parquet(args.events)
    log.info("")
    for line in suggest(rows, events):
        log.info(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

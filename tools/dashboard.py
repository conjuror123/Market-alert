"""The report card's data, as one JSON payload the page renders itself from.

WHY THIS EXISTS AS A FILE. The first report card was assembled by hand, which
made it impossible to answer the only question anyone asks of a dashboard - is
this still true? Everything below is derived from the store by the code running
now, so the answer is "re-run it and see".

It reads the FULL backtest rather than the warm table the hourly run writes.
That distinction is not cosmetic: the warm table covers the trailing six years,
while the hours it is scored against come from the whole archive, so mixing them
reports most of history as missed. Pass --events from a `tremor.saed --full` run.

TWO THINGS ARE NOT DERIVED HERE and are passed in instead: run health, which
comes from the Actions API and needs a token this has no business holding, and
the four sample messages, which the delivery code renders. Everything else on
the page is measured from the store by this file, so a figure that has drifted
shows up as a different number rather than as nothing at all.

THE PAGE IT FILLS IS report_card.html, committed beside this with a __PAYLOAD__
placeholder where the numbers go. The page is a SNAPSHOT and says so: it carries
the date, the time and the commit it was measured on, because it stops being
recomputed the moment it is written and a reader cannot otherwise tell a figure
that still holds from one that stopped holding weeks ago.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import report_card                                                  # noqa: E402
from tremor import bars as bars_mod                                 # noqa: E402
from tremor.basket import load_basket                               # noqa: E402

PUSH_TIERS = ("major", "extreme")


def _commit() -> str:
    """The commit the figures were measured on.

    A published page is a SNAPSHOT: it stops being recomputed the moment it is
    written, and a reader has no way to tell a figure that still holds from one
    that stopped holding weeks ago. A date says when; the commit says what from,
    which is the half that lets somebody check.
    """
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):       # pragma: no cover
        return "unknown"
    if out.returncode:                                   # pragma: no cover
        return "unknown"
    return out.stdout.strip() + (" (with uncommitted changes)"
                                 if dirty.stdout.strip() else "")


def _coverage(basket) -> list[dict]:
    out = []
    for asset in basket.instruments:
        frame = bars_mod.load(bars_mod.store_path(bars_mod.DEFAULT_BARS_DIR,
                                                  asset.file_stem))
        if frame.empty:
            continue
        first, last = int(frame.hour_utc.min()), int(frame.hour_utc.max())
        out.append({
            "id": asset.asset_id, "ticker": asset.ticker, "label": asset.label,
            "block": asset.block, "provider": asset.provider or asset.source,
            "bars": int(len(frame)),
            "first": str(pd.to_datetime(first, unit="s").date()),
            "last": str(pd.to_datetime(last, unit="s").date()),
            "years": round((last - first) / (365.25 * 86400), 1),
        })
    return sorted(out, key=lambda r: r["years"])


def _inbox(push: pd.DataFrame, years: float) -> tuple[dict, list, list]:
    when = pd.to_datetime(push.hour_utc, unit="s")
    days = when.dt.date
    per_day = days.value_counts()
    gaps = np.diff(np.sort(push.hour_utc.unique())) / 86400.0
    day_gaps = np.diff(np.sort(pd.unique(days.map(pd.Timestamp).astype("int64")))) / 86400e9

    def bucket(values, edges, labels):
        counts = []
        for lo, hi, label in zip(edges[:-1], edges[1:], labels):
            counts.append({"label": label,
                           "count": int(((values >= lo) & (values < hi)).sum())})
        return counts

    gap_rows = bucket(day_gaps, [0, 1, 2, 4, 8, 16, 1e9],
                      ["same or next day", "2 days", "3-4 days", "5-8 days",
                       "9-16 days", "over 16 days"])
    clust = bucket(per_day.to_numpy(), [1, 2, 3, 5, 9, 1e9],
                   ["one that day", "two", "three or four", "five to eight",
                    "nine or more"])
    inbox = {
        "per_year": round(len(push) / years, 1),
        "days_with_any": int(per_day.size),
        "days_per_year": round(per_day.size / years, 1),
        "median_gap_days": round(float(np.median(day_gaps)), 1),
        "longest_quiet_days": int(day_gaps.max()),
        "worst_day": int(per_day.max()),
        "worst_day_when": str(per_day.idxmax()),
        "digest_rows_per_note": None,
    }
    return inbox, gap_rows, clust


def build(events: pd.DataFrame, ops: dict | None) -> dict:
    basket = load_basket()
    label = {a.asset_id: a.label for a in basket.instruments}
    block_of = {a.asset_id: a.block for a in basket.instruments}
    push = events[events.channel == "push"]
    first, last = int(events.hour_utc.min()), int(events.hour_utc.max())
    years = (last - first) / (365.25 * 86400)

    coverage = _coverage(basket)
    bars_total = sum(c["bars"] for c in coverage)

    # How often a tier fires FOR ONE INSTRUMENT, which is the number a reader is
    # actually asking about when they ask how often they will hear from it.
    #
    # The denominator is the sum of every instrument's own span, not the span of
    # the archive times the headcount: half the basket is younger than the
    # archive - crypto starts in 2015 at the earliest and XLP in 2022 - and
    # dividing by 61 full spans would report every tier as rarer than it is.
    #
    # Block rows are excluded from the numerator. A block is not an instrument;
    # counting its events here would attribute a whole complex's move to each of
    # the members that did not individually move.
    instrument_years = sum(c["years"] for c in coverage) or 1.0
    block_ids = {b for b in events.asset_id.unique() if str(b).startswith("block:")}

    # Per-instrument recall, straight from the report card so the page and the
    # command line cannot drift apart - same function, same constant.
    per = {}
    for asset_id, hours, move, pct, covered, fired, fitted in report_card.per_asset(events):
        top = pct >= report_card.OBVIOUS_PERCENTILE
        quiet = move < np.median(move)
        per[asset_id] = {
            "push": int((top & (covered == report_card.RANK["push"])).sum()),
            "digest": int((top & (covered == report_card.RANK["digest"])).sum()),
            "median_hour": round(float(np.median(move)) * 100, 4),
            "top": int(top.sum()),
            "silent": int((top & (covered == 0)).sum()),
            "quiet_hours": int(quiet.sum()),
            "quiet_fired": int((quiet & fired).sum()),
            "bars": int(len(hours)),
        }

    instruments = []
    for c in coverage:
        mine = events[events.asset_id == c["id"]]
        row = {k: c[k] for k in ("id", "ticker", "label", "block", "provider",
                                 "years", "bars")}
        row.update({
            "pushes": int((mine.channel == "push").sum()),
            "events": int(len(mine)),
            "push_per_year": round((mine.channel == "push").sum() / max(c["years"], .1), 1),
            "extreme": int((mine.tier == "extreme").sum()),
        })
        row.update(per.get(c["id"], {"median_hour": None, "top": 0, "silent": 0,
                                     "quiet_hours": 0, "quiet_fired": 0}))
        instruments.append(row)

    obvious = {"n": 0, "push": 0, "digest": 0, "silent": 0}
    quiet_n = quiet_fired = 0
    for asset_id, stat in per.items():
        if "coinbase" in asset_id:
            # Excluded from the headline, not out of squeamishness: one coin
            # contributes thousands of 2% hours and would set the bar alone.
            continue
        obvious["n"] += stat["top"]
        obvious["silent"] += stat["silent"]
        obvious["push"] += stat["push"]
        obvious["digest"] += stat["digest"]
        quiet_n += stat["quiet_hours"]
        quiet_fired += stat["quiet_fired"]

    # SORTED BY TIME, and the page depends on it: it takes the first and last
    # tick as the ends of the axis and places every mark as a percentage
    # between them. Unsorted, those two are whichever rows happened to come
    # first, and marks outside that span land past 100% - off the chart and
    # wide enough to scroll the whole page sideways on a phone.
    ticks = []
    med = {i["id"]: (i["median_hour"] or 0) / 100 for i in instruments}
    for r in push.sort_values("hour_utc").itertuples():
        m = med.get(r.asset_id) or 0
        ticks.append({"t": int(r.hour_utc),
                      "y": round(abs(float(r.r)) / m, 2) if m else 0,
                      "k": str(r.tier),
                      "a": str(r.leaders) if str(r.basis) == "block"
                           else str(r.asset_id).split(":")[-1]})
    ys = [t["y"] for t in ticks if t["y"]]

    inbox, gap_rows, clusters = _inbox(push, years)
    inbox["digest_rows_per_note"] = round(
        int((events.channel == "digest").sum()) / max(years * 104, 1), 1)

    blocks = []
    for blk, g in events.assign(b=events.asset_id.map(block_of)).groupby("b"):
        gp = g[g.channel == "push"]
        blocks.append({
            "block": str(blk),
            "members": sum(1 for a in basket.instruments if a.block == blk),
            "pushes": int(len(gp)), "per_year": round(len(gp) / years, 1),
            "block_level": int((gp.basis == "block").sum()),
            "extreme": int((gp.tier == "extreme").sum()),
        })

    def ret_rows(col):
        """How the pushes answered at one horizon.

        Counts, not buckets: the page reads held/reverted/kept_going against the
        total, so that an unanswered push - a horizon that has not elapsed yet -
        reads as a test not yet taken rather than as a failure.
        """
        vals = pd.to_numeric(push[col], errors="coerce").dropna()
        n = len(vals)
        return {
            "of": int(len(push)), "answered": n,
            "held": int((vals >= .5).sum()),
            "held_pct": round(float((vals >= .5).mean() * 100), 1) if n else 0.0,
            "reverted": int((vals <= 0).sum()),
            "reverted_pct": round(float((vals <= 0).mean() * 100), 1) if n else 0.0,
            "kept_going": int((vals > 1).sum()),
            "kept_going_pct": round(float((vals > 1).mean() * 100), 1) if n else 0.0,
            "median": round(float(vals.median()), 2) if n else 0.0,
        }

    retention_by = []
    for key, sub in [("major", push[push.tier == "major"]),
                     ("extreme", push[push.tier == "extreme"]),
                     ("absolute", push[push.basis == "absolute"]),
                     ("abnormal", push[push.basis == "abnormal"]),
                     ("both", push[push.basis == "both"]),
                     ("block", push[push.basis == "block"])]:
        answered = pd.to_numeric(sub.retention_settled, errors="coerce").dropna()
        retention_by.append({
            "key": key, "n": int(len(sub)), "answered": int(len(answered)),
            "held_pct": round(float((answered >= .5).mean() * 100), 1) if len(answered) else None,
            "median": round(float(answered.median()), 2) if len(answered) else None,
        })

    day = pd.to_datetime(events.hour_utc, unit="s").dt.date
    per_day = events.assign(d=day).groupby(["asset_id", "d"]).size()

    payload = {
        "meta": {
            "generated": str(pd.Timestamp.utcnow().date()),
            "generated_at": pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "commit": _commit(),
            "first": str(pd.to_datetime(first, unit="s").date()),
            "last": str(pd.to_datetime(last, unit="s").date()),
            "years": round(years, 1), "instruments": len(basket.instruments),
            "blocks": len(blocks), "bars": int(bars_total),
            "events": int(len(events)), "pushes": int(len(push)),
            "digest": int((events.channel == "digest").sum()),
        },
        "coverage": coverage,
        "tiers": [{"tier": t, "events": int((events.tier == t).sum()),
                   "per_year": round(float((events.tier == t).sum() / years), 1),
                   "per_instrument_year": round(
                       float((events[~events.asset_id.isin(block_ids)].tier == t).sum()
                             / instrument_years), 2),
                   "channel": "push" if t in PUSH_TIERS else "digest"}
                  for t in ("noticeable", "high", "major", "extreme")],
        "basis": [{"basis": b, "events": int((events.basis == b).sum()),
                   "pushes": int(((events.basis == b) & (events.channel == "push")).sum())}
                  for b in ("absolute", "abnormal", "both", "block")],
        "by_year": [{"year": int(y), "events": int(len(g)),
                     "pushes": int((g.channel == "push").sum())}
                    for y, g in events.groupby(
                        pd.to_datetime(events.hour_utc, unit="s").dt.year)],
        "ticks": ticks,
        "mult": {"median": round(float(np.median(ys)), 1) if ys else 0,
                 "max": round(float(max(ys)), 1) if ys else 0},
        "gaps": gap_rows, "clusters": clusters, "inbox": inbox,
        "dayrule": {"asset_days": int(per_day.size),
                    "multi": int((per_day > 1).sum())},
        "blocks": sorted(blocks, key=lambda b: -b["pushes"]),
        "retention": {"today": ret_rows("retention_today"),
                      "settled": ret_rows("retention_settled")},
        "retention_by": retention_by,
        "instruments": instruments,
        "quality": {
            "obvious_hours": obvious["n"], "unfitted": 0, "fitted": obvious["n"],
            "push": obvious["push"], "digest": obvious["digest"],
            "silent": obvious["silent"],
            "push_pct": round(obvious["push"] / max(obvious["n"], 1) * 100, 1),
            "digest_pct": round(obvious["digest"] / max(obvious["n"], 1) * 100, 1),
            "silent_pct": round(obvious["silent"] / max(obvious["n"], 1) * 100, 1),
            "reached_pct": round((1 - obvious["silent"] / max(obvious["n"], 1)) * 100, 1),
            "quiet_hours": quiet_n, "quiet_fired": quiet_fired,
            "quiet_pct": round(quiet_fired / max(quiet_n, 1) * 100, 3),
        },
    }
    if ops:
        payload["ops"] = ops
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--events", required=True,
                        help="events table from `tremor.saed --full`")
    parser.add_argument("--residuals", default=None,
                        help="residual directory from the same run")
    parser.add_argument("--ops", default=None,
                        help="run-health JSON, from the Actions API")
    parser.add_argument("--messages", default=None,
                        help="the sample messages, rendered by the delivery code")
    parser.add_argument("--out", required=True,
                        help="where to write; .html renders the page, .json the "
                             "payload on its own")
    parser.add_argument("--template", default=str(Path(__file__).with_name("report_card.html")),
                        help="the page to inject the payload into")
    args = parser.parse_args(argv)

    if args.residuals:
        report_card.RESIDUALS_DIR = Path(args.residuals)
    ops = json.loads(Path(args.ops).read_text()) if args.ops else None
    payload = build(pd.read_parquet(args.events), ops)
    if args.messages:
        payload["messages"] = json.loads(Path(args.messages).read_text())
    blob = json.dumps(payload, separators=(",", ":"))
    out = Path(args.out)
    if out.suffix == ".html":
        # The template carries the page and a placeholder; the payload is the
        # only thing that changes between snapshots, which is why it is kept out
        # of the committed file - a hundred kilobytes of numbers rewritten on
        # every rebuild is the same mistake as committing the parquet.
        template = Path(args.template).read_text()
        if "__PAYLOAD__" not in template:
            raise SystemExit(f"{args.template} has no __PAYLOAD__ placeholder")
        out.write_text(template.replace("__PAYLOAD__", blob))
    else:
        out.write_text(blob)
    m = payload["meta"]
    print(f"{m['events']} events, {m['pushes']} pushes, {m['instruments']} "
          f"instruments, {m['bars']} bars, measured {m['generated_at']} on "
          f"{m['commit']} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

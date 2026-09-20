"""Scores the detector that is actually delivered.

WHY THIS EXISTS AND WHAT IT REPLACES. tremor.evaluate scores the SI-Index
cluster channel against the calibration yardstick, and that number has been read for a
long time as "how good is the bot". It is not. price_monitor reads
saed_events.parquet: the 8,838 single-instrument and block events are what
reaches a phone, and the 139 cluster events are a channel that is computed,
written, and delivered to nobody. Every judgement made about this project on the
strength of "we barely beat the SPY rule" was made about a component the reader
has never seen.

WHAT A FAIR YARDSTICK IS HERE, which is the whole difficulty. That yardstick asks whether a
big move followed in the next 24 hours - a FORECASTING question. SAED does not
forecast. It says "what just happened in this instrument was unusual for this
instrument", which is a claim about the present and about a distribution, and
scoring it against a forecast target would produce another number that means
nothing and would be quoted anyway.

WHAT IS AND IS NOT WORTH SCORING NOW. A rung is a size - the move over the
instrument's own long-run sigma, against a threshold set per block - so there is
nothing to grade about whether the tier is "right". It is arithmetic, the way a
thermometer is not graded on whether it agrees that 30 degrees is hot. Two
earlier versions of this report scored the ladder as a frequency claim, because
two earlier versions of the ladder made one; neither does now.

What is left is the pair of questions a person actually has.

1. HOW MUCH ARRIVES, and from whom. Not a target to hit - the reader was explicit
   that alerts per year is "a resulting statistic for human analysis only" and
   never an input - but the thing they look at to decide whether to turn
   `sensitivity`, or to raise one instrument's floor. Reported per block and per
   instrument, with the spread, because the spread is the property the ladder was
   rebuilt to get: a flat threshold gave 47x between the quietest and loudest
   instrument, per-block thresholds give about 7x.

2. WHAT IT MISSED, per EPISODE rather than per hour, for the reason
   tremor.evaluate already gives: one shock spans several bars, the detector
   reports the peak and suppresses the repeats, and per-hour recall would mostly
   measure the debounce.

   Reported twice, because the plain number is not the honest one. Recall against
   every large raw move counts as misses the moves an instrument's own block
   explains - and not firing on those is the entire purpose of the system, since
   twenty members of one complex moving together is one observation. So the
   second figure keeps only the episodes that were large AND unexplained, which
   are the misses with nothing to be said for them.

WHAT IS NOT SCORED HERE. Whether the reader wanted the message. No statistic can
answer that and none here pretends to: the feedback loop in tremor.feedback is
where that lives.
"""
from __future__ import annotations

import glob
import logging
import os

import numpy as np
import pandas as pd

from tremor import severity

log = logging.getLogger("tremor.saed_score")

DEFAULT_EVENTS_PATH = os.path.join("data", "tremor", "saed_events.parquet")
DEFAULT_RESIDUALS_DIR = os.path.join("data", "tremor", "residuals")
DEFAULT_METRICS_DIR = os.path.join("data", "tremor", "metrics")

SECONDS_PER_YEAR = 365.25 * 24 * 3600

# The quantity each ladder scores, and the fallback severity.annotate itself
# uses. Kept here rather than imported so that a change to either side shows up
# as a test failure instead of as a silently reversed table.
LADDER_SCORE = {"abnormal": ("z_resid_bmp", "z_resid"), "absolute": ("r",)}

# A series shorter than this is not described by a quantile at the rates these
# tiers ask about. It is the same instinct as the ladder's own warm-up: with a
# few hundred bars the "once in six years" line is an artefact of which bars
# happened to be in the sample.
MIN_BARS = 500

# How far either side of a large episode an event still counts as having caught
# it. The detector times an event at the hour it first clears a level, and a
# shock building over an hour or ebbing over one can put that a bar off the
# episode the label draws. Two bars, not a licence: at twenty-four bars this
# would be scoring the day rather than the hour.
CATCH_SLACK_BARS = 2


def _score_column(frame: pd.DataFrame, ladder: str) -> "pd.Series | None":
    for name in LADDER_SCORE[ladder]:
        if name in frame:
            column = frame[name]
            if column.notna().any():
                return column
    return None


def _load_series(directory: str, ladder: str,
                 coverage: "pd.Series") -> "dict[str, pd.DataFrame]":
    """Each instrument's scored quantity, from the hour its ladder first covers.

    Cut at the ladder's own start deliberately. Before it there is no level to
    clear, so an hour there cannot produce an event however large it was, and
    counting those hours as misses would score the warm-up rather than the
    detector.
    """
    out: dict[str, pd.DataFrame] = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.parquet"))):
        frame = pd.read_parquet(path)
        if "asset_id" not in frame or frame.empty:
            continue
        score = _score_column(frame, ladder)
        if score is None:
            continue
        asset_id = str(frame["asset_id"].iloc[0])
        if asset_id not in coverage.index:
            continue
        kept = pd.DataFrame({"hour_utc": frame["hour_utc"].astype("int64"),
                             "score": score.abs().to_numpy(dtype="float64")})
        kept = kept[kept["score"].notna()
                    & (kept["hour_utc"] >= int(coverage[asset_id]))]
        kept = kept.sort_values("hour_utc").reset_index(drop=True)
        if len(kept) >= MIN_BARS:
            out[asset_id] = kept
    return out


# The share of an instrument's hours a move has to be in to count as "large" for
# the miss table. Not tied to any rung - the rungs are sizes now and have no rate
# to match - just a plain statement of what a person means by one of the biggest
# hours this instrument has had. One in two thousand is about twice a year for a
# round-the-clock market and about once for an ETF.
LARGE_QUANTILE = 1.0 - 1.0 / 2000.0


def hindsight_levels(series: "dict[str, pd.DataFrame]") -> "dict[str, float]":
    """What counts as a large hour for each instrument, read off the sample.

    A plain full-sample quantile: no fit, no extrapolation, and deliberately not
    the detector's own machinery, so what is being measured is whether the
    delivered system found the instrument's big hours rather than whether it
    agrees with itself.
    """
    return {asset: float(np.quantile(frame["score"].to_numpy(), LARGE_QUANTILE))
            for asset, frame in series.items() if len(frame) >= MIN_BARS}


def volume(events: pd.DataFrame, spans: "dict[str, float]",
           blocks: "dict[str, str]") -> "tuple[pd.DataFrame, pd.DataFrame]":
    """Messages per instrument-year, by block and by instrument.

    A REPORT, not a test. There is no target rate to miss: the rungs are sizes,
    and how many of them a market produces is a fact about that market. What the
    reader does with it is decide whether to turn `sensitivity` for everything or
    a single instrument's floor for one thing.

    Block size is deliberately absent from the arithmetic and present in the
    table, because the measurement that settled it is counter-intuitive: the
    equity block has the most members and the second-quietest assets - sixteen
    instruments producing 70 messages a year between them against crypto's 281
    from nine - so dividing by headcount would quiet the block that is already
    quiet.
    """
    if events.empty or not spans:
        return pd.DataFrame(), pd.DataFrame()
    counts = events["asset_id"].value_counts()
    rows = [{"asset_id": asset, "block": blocks.get(asset, "?"),
             "years": years, "events": int(counts.get(asset, 0)),
             "per_year": counts.get(asset, 0) / years if years else float("nan")}
            for asset, years in spans.items() if years > 0]
    per_asset = pd.DataFrame(rows).sort_values("per_year")
    by_block = per_asset.groupby("block").agg(
        members=("asset_id", "size"), per_asset=("per_year", "mean"),
        block_per_year=("per_year", "sum")).sort_values(
            "block_per_year", ascending=False).reset_index()
    return by_block, per_asset


def _episodes(flags: np.ndarray) -> "list[tuple[int, int]]":
    """Contiguous runs of True as (start, stop) index pairs."""
    out, start = [], None
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(flags)))
    return out


def recall(events: pd.DataFrame,
           series: "dict[str, dict[str, pd.DataFrame]]") -> pd.DataFrame:
    """Of an instrument's largest hours, how many produced a message.

    Twice over. `every large move` counts an episode whenever the raw return
    clears the line, which includes the ones the instrument's block fully
    explains - those are supposed to be silent, so the figure is a floor rather
    than a verdict. `large and unexplained` keeps only the episodes where the
    residual clears its line in the same hours, and those are misses with nothing
    to be said for them.
    """
    fired = {asset: set(group["hour_utc"].astype("int64"))
             for asset, group in events.groupby("asset_id")}
    size, abnormal = series["absolute"], series["abnormal"]
    size_levels = hindsight_levels(size)
    abnormal_levels = hindsight_levels(abnormal)

    rows = []
    for asset_id, frame in size.items():
        line = size_levels.get(asset_id)
        if line is None:
            continue
        hours = frame["hour_utc"].to_numpy()
        big = frame["score"].to_numpy() >= line
        seen = fired.get(asset_id, set())
        resid = abnormal.get(asset_id)
        unexplained = None
        resid_line = abnormal_levels.get(asset_id)
        if resid is not None and resid_line is not None:
            joined = frame.merge(resid, on="hour_utc", how="left",
                                 suffixes=("", "_resid"))
            unexplained = joined["score_resid"].to_numpy() >= resid_line

        for label, mask in (("every large move", big),
                            ("large and unexplained",
                             big & unexplained if unexplained is not None else None)):
            if mask is None:
                continue
            spans = _episodes(mask)
            caught = sum(
                any(int(h) in seen
                    for h in hours[max(0, a - CATCH_SLACK_BARS):
                                   min(len(hours), b + CATCH_SLACK_BARS)])
                for a, b in spans)
            rows.append({"label": label, "episodes": len(spans), "caught": caught})
    if not rows:
        return pd.DataFrame()
    grouped = pd.DataFrame(rows).groupby("label")[["episodes", "caught"]].sum()
    grouped["recall"] = grouped["caught"] / grouped["episodes"].replace(0, np.nan)
    return grouped.reset_index()


def render(by_block: pd.DataFrame, per_asset: pd.DataFrame,
           rec: pd.DataFrame, events: pd.DataFrame) -> str:
    lines = ["## The detector that is delivered", ""]
    lines.append(
        f"`saed_events.parquet` — {len(events):,} events, which is what "
        "`price_monitor` reads and what reaches a phone. The SI-Index table "
        "further down scores a different channel that is written and delivered "
        "to nobody; the two are not comparable and the numbers below are the "
        "ones that describe the product.")
    lines.append("")
    lines.append(
        "A rung is a SIZE — the move over the instrument's own long-run sigma, "
        "against a threshold set per block — so there is nothing here grading "
        "whether a tier is correct. It is arithmetic. What follows is how much "
        "arrives and what was missed.")
    lines.append("")

    if not by_block.empty:
        lines += ["### How much arrives, and from whom", "",
                  "Not a target. Alerts per year is a resulting statistic, never "
                  "an input — it is what you read to decide whether to turn "
                  "`sensitivity` for everything or one instrument's "
                  "`min_move_sigma` for one thing.", "",
                  "| Block | Members | Per asset/yr | Block total/yr |",
                  "|---|---:|---:|---:|"]
        for row in by_block.itertuples(index=False):
            lines.append(f"| {row.block} | {row.members} | {row.per_asset:.1f} | "
                         f"{row.block_per_year:.0f} |")
        lines.append("")
        lines.append("Block SIZE is not used to set anything, and this table is "
                     "why: the block with the most members is among the quietest "
                     "per asset, so dividing by headcount would quiet what is "
                     "already quiet and barely touch what floods.")
        lines.append("")

    if not per_asset.empty and len(per_asset) > 6:
        quiet = per_asset.head(3)
        loud = per_asset.tail(3)
        spread = (loud["per_year"].iloc[-1] / quiet["per_year"].iloc[0]
                  if quiet["per_year"].iloc[0] else float("nan"))
        def names(part):
            return ", ".join(f"{r.asset_id.split(':')[-1]} {r.per_year:.1f}"
                             for r in part.itertuples())
        lines += [f"Quietest: {names(quiet)}. Loudest: {names(loud)}. "
                  f"Spread across the basket **{spread:.1f}×** — a flat threshold "
                  "gave 47×, a rank-based ladder 2.2×.", ""]

    if not rec.empty:
        lines += ["### What did it miss?", "",
                  "Per episode, not per hour: one shock spans several bars and "
                  "the detector reports the peak and suppresses the repeats, so "
                  "a per-hour figure would mostly measure the debounce.", "",
                  "| Label | Episodes | Caught | Recall |", "|---|---:|---:|---:|"]
        for label in ("every large move", "large and unexplained"):
            part = rec[rec["label"].eq(label)]
            for row in part.itertuples(index=False):
                lines.append(f"| {row.label} | {row.episodes} | {row.caught} | "
                             f"{row.recall * 100:.1f}% |")
        lines.append("")
        lines.append("`every large move` counts the moves a block fully explains, "
                     "which are meant to be silent — twenty members of one complex "
                     "moving together is one observation, and firing twenty times "
                     "about it is the fault this system was built to fix. So that "
                     "row is a floor. `large and unexplained` is the one to read.")
        lines.append("")
    return "\n".join(lines)


def spans_and_blocks(residuals_dir: str) -> "tuple[dict, dict]":
    """Each instrument's span in years, and which block it sits in."""
    from tremor.basket import load_basket

    try:
        blocks = {a.asset_id: a.block for a in load_basket().instruments}
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("could not read the basket: %s", exc)
        blocks = {}
    spans: dict[str, float] = {}
    for path in sorted(glob.glob(os.path.join(residuals_dir, "*.parquet"))):
        frame = pd.read_parquet(path, columns=["hour_utc", "asset_id"])
        if frame.empty or len(frame) < MIN_BARS:
            continue
        asset_id = str(frame["asset_id"].iloc[0])
        spans[asset_id] = float((frame["hour_utc"].max() - frame["hour_utc"].min())
                                / SECONDS_PER_YEAR)
    return spans, blocks


def build(events_path: str = DEFAULT_EVENTS_PATH,
          residuals_dir: str = DEFAULT_RESIDUALS_DIR,
          metrics_dir: str = DEFAULT_METRICS_DIR) -> str:
    """The whole section, or an empty string if the inputs are not there.

    Empty rather than an exception: this runs inside tremor.evaluate, and a
    missing residuals directory must not cost the report that does exist.
    """
    if not os.path.exists(events_path):
        log.warning("no SAED events - section skipped")
        return ""
    events = pd.read_parquet(events_path)
    if events.empty:
        return ""

    spans, blocks = spans_and_blocks(residuals_dir)
    if not spans:
        log.warning("no residuals - section skipped")
        return ""
    coverage = pd.Series({asset: 0 for asset in spans})
    series = {"abnormal": _load_series(residuals_dir, "abnormal", coverage),
              "absolute": _load_series(metrics_dir, "absolute", coverage)}

    single = events[~events["asset_id"].astype(str).str.startswith("block:")]
    by_block, per_asset = volume(single, spans, blocks)
    rec = recall(single, series) if series["absolute"] else pd.DataFrame()
    return render(by_block, per_asset, rec, events)


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Score the delivered detector")
    parser.add_argument("--events", default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--residuals-dir", default=DEFAULT_RESIDUALS_DIR)
    parser.add_argument("--metrics-dir", default=DEFAULT_METRICS_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    section = build(args.events, args.residuals_dir, args.metrics_dir)
    if not section:
        return 2
    print(section)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

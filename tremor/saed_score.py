"""Scores the detector that is actually delivered.

WHY THIS EXISTS AND WHAT IT REPLACES. tremor.evaluate scores the SI-Index
cluster channel against the §7 yardstick, and that number has been read for a
long time as "how good is the bot". It is not. price_monitor reads
saed_events.parquet: the 8,838 single-instrument and block events are what
reaches a phone, and the 139 cluster events are a channel that is computed,
written, and delivered to nobody. Every judgement made about this project on the
strength of "we barely beat the SPY rule" was made about a component the reader
has never seen.

WHAT A FAIR YARDSTICK IS HERE, which is the whole difficulty. §7 asks whether a
big move followed in the next 24 hours - a FORECASTING question. SAED does not
forecast. It says "what just happened in this instrument was unusual for this
instrument", which is a claim about the present and about a distribution, and
scoring it against a forecast target would produce another number that means
nothing and would be quoted anyway.

So it is scored on the claim it actually makes, in three parts.

1. CALIBRATION, which is the part that matters most and needs no labels at all.
   Every message carries a return period - "about once in 6 years" - and that is
   a falsifiable statement about frequency. Count how often each tier really
   fires per instrument-year and compare. The ladder is fitted causally on an
   expanding window and applied forward only, so the realised rate is genuinely
   out of sample: this is a fair test, not a fit being graded on its own data.

2. PRECISION, on a label that is deliberately NOT the detector's own machinery.
   For each instrument the label is a plain full-sample empirical quantile of
   the same quantity the ladder scores, set at the rate the tier claims. The
   detector is a causal, model-based (peaks-over-threshold) extrapolation into
   the tail; the label is model-free hindsight. Both look at the same series,
   which is unavoidable - any label about whether a move was big must look at
   the move - but the label borrows none of the fitting, so what is being tested
   is the extrapolation rather than the definition.

   EACH EVENT IS JUDGED ON THE LADDER THAT FIRED IT, and getting this wrong is
   the easiest way to produce a confidently meaningless table. The two ladders
   score different quantities: `absolute` scores the raw return r, `abnormal`
   scores the BMP-standardised residual z_resid_bmp. Judging abnormal events on
   raw size scores them at 3.5%, and judging absolute events on the residual
   scores them at 1.1% - both figures say only that the yardstick was swapped.
   On their own quantities they are 76% and 84%.

3. RECALL, per EPISODE rather than per hour, for the reason tremor.evaluate
   already gives: one shock spans several bars, the detector deliberately
   reports the peak and suppresses the repeats, and per-hour recall would mostly
   measure the debounce.

   Reported twice, because the plain number is not the honest one. Recall
   against every large raw move counts as misses the moves that the instrument's
   own block explains - and not firing on those is the entire purpose of the
   system, since twenty members of one complex moving together is one
   observation and firing twenty times about it is the fault this was built to
   fix. So the second figure keeps only the episodes that were large AND
   unexplained by the block, which are the misses with nothing to be said for
   them.

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
DEFAULT_LADDER_PATH = os.path.join("data", "tremor", "ladder.csv")
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


def hindsight_levels(series: "dict[str, pd.DataFrame]") -> "dict[tuple[str, str], float]":
    """The level a tier's claimed rate implies, read straight off the sample.

    No fit and no extrapolation: if a tier says once in six years and the record
    holds nineteen, the line is drawn where the third-largest observation sits.
    That is exactly why it is usable as a label for a model that DOES
    extrapolate - it cannot inherit the model's error, only the sample's.

    A tier whose claimed rate implies fewer than one occurrence in the record
    gets no level: there is no sample answer to give, and inventing one by
    extrapolating would turn the label into a second model with no way to tell
    whose error was being measured.
    """
    days = severity.tier_days()
    out: dict[tuple[str, str], float] = {}
    for asset_id, frame in series.items():
        span = (int(frame["hour_utc"].iloc[-1])
                - int(frame["hour_utc"].iloc[0])) / SECONDS_PER_YEAR
        values = frame["score"].to_numpy()
        for tier in severity.TIERS:
            expected = span / (days[tier] / 365.25)
            if expected < 1.0:
                continue
            out[(asset_id, tier)] = float(
                np.quantile(values, 1.0 - expected / len(values)))
    return out


def calibration(events: pd.DataFrame, ladder: pd.DataFrame) -> pd.DataFrame:
    """Claimed frequency against realised, per tier.

    The denominator is instrument-YEARS and only those in which the tier could
    have fired at all. Five instruments have no `extreme` level - the fit never
    reached that far out - and folding their years into the total would report
    the ladder as well behaved for the arithmetic reason that part of the
    watchlist is unable to speak.
    """
    days = severity.tier_days()
    end = int(events["hour_utc"].max())
    rows = []
    for tier in severity.TIERS:
        column = f"level_{tier}"
        if column not in ladder:
            continue
        reachable = ladder[ladder[column].notna()]
        first = reachable.groupby("asset_id")["from_hour"].min()
        if first.empty:
            continue
        years = ((end - first) / SECONDS_PER_YEAR).clip(lower=0)
        fired = int(events["tier"].eq(tier).sum())
        claimed = 365.25 / days[tier]
        actual = fired / years.sum() if years.sum() else float("nan")
        rows.append({
            "tier": tier,
            "instruments": int(len(first)),
            "instrument_years": float(years.sum()),
            "events": fired,
            "claimed_per_year": claimed,
            "actual_per_year": actual,
            "ratio": actual / claimed if claimed else float("nan"),
            "claimed_period": severity.period_phrase(days[tier]),
            "actual_days": 365.25 / actual if actual else float("nan"),
        })
    return pd.DataFrame(rows)


def unreachable_tiers(ladder: pd.DataFrame) -> "dict[str, list[str]]":
    """Instruments whose ladder never produced a level for a tier.

    Not a scoring result but a precondition for reading one: an instrument with
    no `extreme` line is silent at that tier by construction, and that is a fact
    about the fit rather than about the market.
    """
    everything = set(ladder["asset_id"].unique())
    out = {}
    for tier in severity.TIERS:
        column = f"level_{tier}"
        if column not in ladder:
            continue
        have = set(ladder[ladder[column].notna()]["asset_id"].unique())
        missing = sorted(everything - have)
        if missing:
            out[tier] = missing
    return out


def _event_score(events: pd.DataFrame, ladder: str,
                 series: "dict[str, pd.DataFrame]") -> np.ndarray:
    """Each event's value of one ladder's quantity, looked up by hour."""
    lookup = {asset: dict(zip(frame["hour_utc"].to_numpy(),
                              frame["score"].to_numpy()))
              for asset, frame in series.items()}
    return np.array([lookup.get(a, {}).get(int(h), np.nan)
                     for a, h in zip(events["asset_id"], events["hour_utc"])])


def precision(events: pd.DataFrame,
              series: "dict[str, dict[str, pd.DataFrame]]") -> pd.DataFrame:
    """Of the events sent, how many clear the hindsight line for their tier.

    An event of basis `both` cleared both ladders and is counted a hit if either
    quantity clears its own line - the message it produced claimed both things,
    so either being true makes the message true.
    """
    levels = {name: hindsight_levels(frames) for name, frames in series.items()}
    scores = {name: _event_score(events, name, frames)
              for name, frames in series.items()}

    hit = np.zeros(len(events), dtype=bool)
    judged = np.zeros(len(events), dtype=bool)
    basis = events["basis"].astype("string").to_numpy()
    tiers = events["tier"].astype("string").to_numpy()
    assets = events["asset_id"].to_numpy()
    for name in series:
        applies = (basis == name) | (basis == "both")
        line = np.array([levels[name].get((a, t), np.nan)
                         for a, t in zip(assets, tiers)])
        usable = applies & np.isfinite(line) & np.isfinite(scores[name])
        judged |= usable
        hit |= usable & (scores[name] >= line)

    frame = events.assign(_hit=hit, _judged=judged)
    frame = frame[frame["_judged"]]
    if frame.empty:
        return pd.DataFrame()
    grouped = frame.groupby(["basis", "tier"])["_hit"].agg(["size", "sum"])
    grouped = grouped.rename(columns={"size": "events", "sum": "on_target"})
    grouped["precision"] = grouped["on_target"] / grouped["events"]
    return grouped.reset_index()


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
    """Of the largest moves in hindsight, how many produced a message.

    Twice over. `every large move` counts an episode whenever the raw return
    clears the line, which includes the ones the instrument's block fully
    explains - those are supposed to be silent, so the figure is a floor rather
    than a verdict. `large and unexplained` keeps only the episodes where the
    residual clears its line in the same hours, and those are misses with
    nothing to be said for them.
    """
    fired = {asset: set(group["hour_utc"].astype("int64"))
             for asset, group in events.groupby("asset_id")}
    size, abnormal = series["absolute"], series["abnormal"]
    size_levels = hindsight_levels(size)
    abnormal_levels = hindsight_levels(abnormal)

    rows = []
    for asset_id, frame in size.items():
        hours = frame["hour_utc"].to_numpy()
        values = frame["score"].to_numpy()
        seen = fired.get(asset_id, set())
        resid = abnormal.get(asset_id)
        unexplained = None
        if resid is not None:
            joined = frame.merge(resid, on="hour_utc", how="left",
                                 suffixes=("", "_resid"))
            unexplained = joined["score_resid"].to_numpy()

        for tier in severity.TIERS:
            line = size_levels.get((asset_id, tier))
            if line is None:
                continue
            big = values >= line
            resid_line = abnormal_levels.get((asset_id, tier))
            for label, mask in (("every large move", big),
                                ("large and unexplained",
                                 big & (unexplained >= resid_line)
                                 if unexplained is not None and resid_line is not None
                                 else None)):
                if mask is None:
                    continue
                spans = _episodes(mask)
                caught = sum(
                    any(int(h) in seen for h in hours[max(0, a - CATCH_SLACK_BARS):
                                                      min(len(hours), b + CATCH_SLACK_BARS)])
                    for a, b in spans)
                rows.append({"label": label, "tier": tier,
                             "episodes": len(spans), "caught": caught})
    if not rows:
        return pd.DataFrame()
    grouped = pd.DataFrame(rows).groupby(["label", "tier"])[
        ["episodes", "caught"]].sum().reset_index()
    grouped["recall"] = grouped["caught"] / grouped["episodes"].replace(0, np.nan)
    return grouped


def _order(frame: pd.DataFrame) -> pd.DataFrame:
    rank = {tier: i for i, tier in enumerate(severity.TIERS)}
    return frame.assign(_o=frame["tier"].map(rank)).sort_values("_o").drop(columns="_o")


def render(cal: pd.DataFrame, prec: pd.DataFrame, rec: pd.DataFrame,
           unreachable: dict, events: pd.DataFrame) -> str:
    lines = ["## The detector that is delivered", ""]
    lines.append(
        f"`saed_events.parquet` — {len(events):,} events, which is what "
        "`price_monitor` reads and what reaches a phone. The SI-Index table "
        "further down scores a different channel that is written and delivered "
        "to nobody; the two are not comparable and the numbers below are the "
        "ones that describe the product.")
    lines.append("")

    lines += ["### Does a tier fire as often as it claims?", "",
              "Every message states a return period. That is a falsifiable claim "
              "about frequency and this is the test of it. Levels are fitted on "
              "an expanding window and applied forward only, so the realised "
              "rate is out of sample.", "",
              "| Tier | Says | Actually | Instruments | Instrument-years | Events | Ratio |",
              "|---|---|---|---:|---:|---:|---:|"]
    for row in _order(cal).itertuples(index=False):
        actual = (severity.period_phrase(row.actual_days)
                  if np.isfinite(row.actual_days) else "—")
        lines.append(
            f"| {row.tier} | {row.claimed_period} | {actual} | "
            f"{row.instruments} | {row.instrument_years:,.0f} | {row.events} | "
            f"{row.ratio:.2f}× |")
    lines.append("")
    lines.append("A ratio above 1 means the tier fires more often than its words "
                 "promise, and the message overstates the rarity by that factor.")
    lines.append("")

    if unreachable:
        lines.append("**Tiers no level was ever fitted for**, which are silent by "
                     "construction rather than because the market was quiet:")
        lines.append("")
        for tier, assets in unreachable.items():
            lines.append(f"- `{tier}`: {len(assets)} instruments — "
                         + ", ".join(a.split(":")[-1] for a in assets[:12])
                         + ("…" if len(assets) > 12 else ""))
        lines.append("")

    if not prec.empty:
        lines += ["### Were the moves it sent actually rare?", "",
                  "Each event judged against a plain full-sample quantile of the "
                  "quantity ITS OWN ladder scores — `absolute` against the raw "
                  "return, `abnormal` against the standardised residual — drawn "
                  "at the rate its tier claims. Swapping the two yardsticks "
                  "scores them at 3.5% and 1.1% and means nothing except that "
                  "they were swapped.", "",
                  "| Basis | Tier | Events | On target | Precision |",
                  "|---|---|---:|---:|---:|"]
        for row in prec.sort_values(["basis", "tier"]).itertuples(index=False):
            lines.append(f"| {row.basis} | {row.tier} | {row.events} | "
                         f"{row.on_target} | {row.precision * 100:.1f}% |")
        lines.append("")

    if not rec.empty:
        lines += ["### What did it miss?", "",
                  "Per episode, not per hour: one shock spans several bars and "
                  "the detector reports the peak and suppresses the repeats, so "
                  "a per-hour figure would mostly measure the debounce.", "",
                  "| Label | Tier | Episodes | Caught | Recall |",
                  "|---|---|---:|---:|---:|"]
        for label in ("every large move", "large and unexplained"):
            part = rec[rec["label"].eq(label)]
            for row in _order(part).itertuples(index=False):
                lines.append(f"| {row.label} | {row.tier} | {row.episodes} | "
                             f"{row.caught} | {row.recall * 100:.1f}% |")
        lines.append("")
        lines.append("`every large move` counts the moves a block fully explains, "
                     "which are meant to be silent — twenty members of one complex "
                     "moving together is one observation, and firing twenty times "
                     "about it is the fault this system was built to fix. So that "
                     "row is a floor. `large and unexplained` is the one to read.")
        lines.append("")
    return "\n".join(lines)


def build(events_path: str = DEFAULT_EVENTS_PATH,
          ladder_path: str = DEFAULT_LADDER_PATH,
          residuals_dir: str = DEFAULT_RESIDUALS_DIR,
          metrics_dir: str = DEFAULT_METRICS_DIR) -> str:
    """The whole section, or an empty string if the inputs are not there.

    Empty rather than an exception: this runs inside tremor.evaluate, and a
    missing residuals directory must not cost the report that does exist.
    """
    if not (os.path.exists(events_path) and os.path.exists(ladder_path)):
        log.warning("no SAED events or ladder cache - section skipped")
        return ""
    events = pd.read_parquet(events_path)
    ladder = pd.read_csv(ladder_path)
    if events.empty or ladder.empty:
        return ""

    coverage = ladder.groupby("asset_id")["from_hour"].min()
    series = {"abnormal": _load_series(residuals_dir, "abnormal", coverage),
              "absolute": _load_series(metrics_dir, "absolute", coverage)}
    if not series["absolute"]:
        log.warning("no per-instrument metrics - section skipped")
        return ""

    # Blocks are scored by the same ladder machinery but have no per-instrument
    # series here to label them against, so they are left out of precision and
    # recall. They stay in calibration, where the question is only how often the
    # tier fires and the answer does not need a label.
    single = events[~events["asset_id"].astype(str).str.startswith("block:")]
    return render(calibration(events, ladder),
                  precision(single, series),
                  recall(single, series),
                  unreachable_tiers(ladder), events)


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Score the delivered detector")
    parser.add_argument("--events", default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--ladder", default=DEFAULT_LADDER_PATH)
    parser.add_argument("--residuals-dir", default=DEFAULT_RESIDUALS_DIR)
    parser.add_argument("--metrics-dir", default=DEFAULT_METRICS_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    section = build(args.events, args.ladder, args.residuals_dir, args.metrics_dir)
    if not section:
        return 2
    print(section)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

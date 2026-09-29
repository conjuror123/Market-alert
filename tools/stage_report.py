"""What the jump detector flags, stage by stage: the numbers a reader checks.

    PYTHONPATH=. python tools/stage_report.py [--metrics-dir DIR] [--seed N]

Scores every instrument's whole history with tremor.jumps (a second or two) and
prints, as Markdown:

  - flags a week for the whole basket, by word;
  - per instrument a year, by block: median and the 10-90% range;
  - the biggest hours of each instrument's record (its top 0.01% of |move|):
    how many were flagged, and at which word;
  - the gaps (nights and weekends) by kind: per instrument a year and by word;
  - the events: an instrument's flags grouped into 24 hours of real time from
    the first one found, each worded by its rarest flag (tremor.jumps.event_starts);
  - the events by channel: pushed, or a row in the weekly note;
  - ten flagged hours picked at random, for a sanity read.

Rates count only SETTLED hours - those whose window already spans the full
half-year. Hours scored while a series was still young are counted apart, so
the early, noisier yardstick cannot hide inside the totals.
"""
from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd

from tremor import jumps, routing
from tremor.basket import load_basket

YEAR = 365.25 * 86400
BIGGEST_SHARE = 1e-4          # the top 0.01% of an instrument's hours


def scored_basket(metrics_dir: str) -> pd.DataFrame:
    basket = load_basket()
    window, bottom, step = jumps.settings()
    parts = []
    for asset in basket.instruments:
        path = os.path.join(metrics_dir, f"{asset.file_stem}.parquet")
        if not os.path.exists(path):
            continue
        metrics = pd.read_parquet(path, columns=["hour_utc", "r", "gap"])
        parts_one = [jumps.score(metrics, asset.session_template, window, bottom, step),
                     jumps.score_gaps(metrics, window, bottom, step)]
        frame = pd.concat([f for f in parts_one if not f.empty], ignore_index=True)
        frame = frame[np.isfinite(frame["z"].astype("float64"))]
        frame.insert(2, "template", asset.session_template)
        frame.insert(0, "ticker", asset.ticker)
        frame.insert(1, "block", asset.block)
        parts.append(frame)
    return pd.concat(parts, ignore_index=True)


def gap_section(scored: pd.DataFrame) -> "list[str]":
    """The gaps: a fund's nights and weekends, a currency pair's weekends."""
    lines = ["## The gaps", "",
             "Each gap judged against the earlier gaps of its own kind over the half-year "
             "before it. Settled readings only.", "",
             "| instruments | reading | per instrument a year (median) | noticeable | high | "
             "major | extreme | whole basket a week |", "|---|---|---|---|---|---|---|---|"]
    gaps = scored[(scored["reading"] != jumps.HOUR) & ~scored["young"].astype(bool)]
    names = {"us_equity": "funds", "fx_continuous": "currency pairs"}
    for (template, reading), rows in gaps.groupby(["template", "reading"]):
        spans = rows.groupby("ticker")["hour_utc"].agg(lambda h: (h.max() - h.min()) / YEAR)
        flagged = rows[rows["word"].notna()]
        per = flagged.groupby("ticker").size().reindex(spans.index, fill_value=0) / spans
        by_word = [(flagged[flagged["word"] == w].groupby("ticker").size()
                    .reindex(spans.index, fill_value=0) / spans).median() for w in jumps.WORDS]
        lines.append(f"| {names.get(template, template)} | {reading} | {per.median():.2f} | "
                     + " | ".join(f"{v:.2f}" for v in by_word)
                     + f" | {per.sum() / 52.18:.1f} |")
    return lines + [""]


def event_section(scored: pd.DataFrame) -> "list[str]":
    """Flags grouped into 24-hour events, each worded by its rarest flag."""
    settled = scored[~scored["young"].astype(bool)]
    spans = settled.groupby("ticker")["hour_utc"].agg(lambda h: (h.max() - h.min()) / YEAR)
    parts = []
    for ticker, rows in settled[settled["word"].notna()].groupby("ticker"):
        rows = rows.assign(found_utc=jumps.found_times(rows, rows["template"].iloc[0]))
        rows = rows.sort_values("found_utc").reset_index(drop=True)
        parts.append(rows.assign(event_start=jumps.event_starts(rows["found_utc"]),
                                 asset_id=ticker))
    events = jumps.events(pd.concat(parts, ignore_index=True))
    flags = int(settled["word"].notna().sum())
    lines = ["## Events: 24 hours from an instrument's first flag", "",
             f"{flags:,} flags become {len(events):,} events.", "",
             "| word | events | a year, today's basket | a week, today's basket |",
             "|---|---|---|---|"]
    total = 0.0
    for word in jumps.WORDS:
        mine = events[events["word"] == word]
        rate = (mine.groupby("ticker").size().reindex(spans.index, fill_value=0) / spans).sum()
        total += rate
        lines.append(f"| {word} | {len(mine):,} | {rate:,.0f} | {rate / 52.18:,.1f} |")
    lines.append(f"| **all** | {len(events):,} | {total:,.0f} | {total / 52.18:,.1f} |")
    per = events.groupby("ticker").size().reindex(spans.index, fill_value=0) / spans
    block_of = settled.groupby("ticker")["block"].first()
    lines += ["", "| block | events per instrument a year (median) | 10–90% |", "|---|---|---|"]
    for block, names in block_of.groupby(block_of):
        v = per[names.index]
        lines.append(f"| {block} | {v.median():.1f} | {v.quantile(.1):.1f}–{v.quantile(.9):.1f} |")
    lines.append(f"| **all** | {per.median():.1f} | {per.quantile(.1):.1f}–{per.quantile(.9):.1f} |")

    # The channels (stage 2): which of those events interrupt at once and which
    # go into the weekly note, each row with its own small ping.
    pushed = events["word"].isin(routing.PUSH_TIERS)
    lines += ["", "## Channels", "",
              f"Pushed: {', '.join(routing.PUSH_TIERS)}. The rest go into the one weekly note.", "",
              "| channel | events | a week, today's basket | busiest week in the last year |",
              "|---|---|---|---|"]
    last_year = events["hour_utc"] >= events["hour_utc"].max() - YEAR
    for name, mask in (("push", pushed), ("weekly note (and a ping each)", ~pushed)):
        rate = (events[mask].groupby("ticker").size().reindex(spans.index, fill_value=0)
                / spans).sum() / 52.18
        weeks = (pd.to_datetime(events.loc[mask & last_year, "hour_utc"], unit="s")
                 .dt.to_period("W-FRI").value_counts())
        lines.append(f"| {name} | {int(mask.sum()):,} | {rate:.1f} | "
                     f"{int(weeks.max()) if len(weeks) else 0} |")
    return lines + [""]


def report(scored: pd.DataFrame, seed: int = 0) -> str:
    events = event_section(scored)
    gaps = gap_section(scored)
    scored = scored[scored["reading"] == jumps.HOUR]
    lines = []
    settled = scored[~scored["young"]]
    spans = settled.groupby("ticker")["hour_utc"].agg(lambda h: (h.max() - h.min()) / YEAR)
    years = spans.sum()
    flagged = settled[settled["word"].notna()]
    young_flags = scored[scored["young"] & scored["word"].notna()]

    lines += ["## Flags, whole basket", "",
              f"{len(spans)} instruments, {years:.0f} instrument-years of settled hours. "
              f"{len(young_flags)} more flags came from young stretches (window under half "
              f"a year) and are left out below.", "",
              "Each instrument's own rate, summed: what today's basket produces.", "",
              "| word | flags in the record | a year, today's basket | a week, today's basket |",
              "|---|---|---|---|"]
    total = 0.0
    for word in jumps.WORDS:
        mine = flagged[flagged["word"] == word]
        rate = (mine.groupby("ticker").size().reindex(spans.index, fill_value=0) / spans).sum()
        total += rate
        lines.append(f"| {word} | {len(mine):,} | {rate:,.0f} | {rate / 52.18:,.1f} |")
    lines.append(f"| **all** | {len(flagged):,} | {total:,.0f} | {total / 52.18:,.1f} |")
    lines.append("")

    per_inst = (flagged.groupby("ticker").size().reindex(spans.index, fill_value=0) / spans)
    block_of = settled.groupby("ticker")["block"].first()
    lines += ["## Per instrument a year, by block", "",
              "| block | instruments | median | 10–90% | major or rarer, median |",
              "|---|---|---|---|---|"]
    rare = (flagged[flagged["word"].isin(["major", "extreme"])].groupby("ticker").size()
            .reindex(spans.index, fill_value=0) / spans)
    for block, names in block_of.groupby(block_of):
        v = per_inst[names.index]
        lines.append(f"| {block} | {len(v)} | {v.median():.1f} | "
                     f"{v.quantile(.1):.1f}–{v.quantile(.9):.1f} | "
                     f"{rare[names.index].median():.2f} |")
    v = per_inst
    lines.append(f"| **all** | {len(v)} | {v.median():.1f} | "
                 f"{v.quantile(.1):.1f}–{v.quantile(.9):.1f} | {rare.median():.2f} |")
    lines.append("")

    counts = {w: 0 for w in jumps.WORDS}
    counts["not flagged"] = 0
    total_big = 0
    for ticker, rows in settled.groupby("ticker"):
        size = rows["r"].abs()
        big = rows[size >= size.quantile(1 - BIGGEST_SHARE)]
        total_big += len(big)
        for word, n in big["word"].fillna("not flagged").value_counts().items():
            counts[word] += int(n)
    lines += ["## The biggest hours of each record", "",
              f"Each instrument's top 0.01% of hours by size of move, {total_big} in all.", "",
              "| word | hours | share |", "|---|---|---|"]
    for word in list(jumps.WORDS) + ["not flagged"]:
        lines.append(f"| {word} | {counts[word]} | {counts[word] / max(total_big, 1):.0%} |")
    lines.append("")

    lines += gaps
    lines += events
    sample = flagged.sample(min(10, len(flagged)), random_state=seed).sort_values("hour_utc")
    lines += ["## Ten flagged hours, at random", "",
              "| hour (UTC) | instrument | move | half-year σ | z | word |",
              "|---|---|---|---|---|---|"]
    for row in sample.itertuples():
        when = pd.to_datetime(row.hour_utc, unit="s").strftime("%Y-%m-%d %H:%M")
        lines.append(f"| {when} | {row.ticker} | {row.r * 100:+.2f}% | "
                     f"{row.sigma * 100:.3f}% | {row.z:+.1f} | {row.word} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--metrics-dir", default=jumps.DEFAULT_METRICS_DIR)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    print(report(scored_basket(args.metrics_dir), args.seed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

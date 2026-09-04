"""Backtest metrics against the §7 yardstick.

Scores what the detector produced against the labels of meals.truth: precision,
recall, F1 and lead time, overall and per block, next to the baselines. Then the
diagnostics §7 asks for by name - events by month, which trigger produced them,
how often the debounce bit, what the calendar multiplier contributed.

Three decisions the spec does not make, all of which change the numbers, so they
are stated here and in docs/meals-deviations.md.

WHERE SCORING STARTS. §6.6 keeps an hour out of backtest statistics while its
triggers are still warming up. The basket is not fully warm until 2021-11-22,
when the last ETF's Q95 window fills; before that the detector can only see
crypto and FX, and three events were in fact created from those two blocks alone.
Scoring them would measure a handicapped detector. The truth thresholds are a
different matter and still use the whole train period: they are built from
returns, which are valid from the first bar, and nothing about the detector's
warm-up enters them.

RECALL IS PER EPISODE, NOT PER HOUR. A cluster event holds a 72-hour cooldown, so
it cannot fire on most hours of a long significant stretch - and a 24-hour forward
label turns one shock into some two dozen consecutive significant hours. Per-hour
recall would mostly measure the cooldown. So contiguous significant hours are
collapsed into one episode and the question becomes whether the episode was caught
at all, which is what a person receiving the alerts would care about.

THE BASELINE IS GIVEN THE SAME COOLDOWN. The trailing SPY rule fires on 972 hours
against the SI-Index's 188 events; comparing recall between them as they stand
would reward the baseline for being allowed to shout. It is therefore also run
through the 72-hour cooldown, so both produce a comparable number of alerts, and
both counts are reported.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from meals import si_index, truth, windows

log = logging.getLogger("meals.evaluate")

# A detection is credited to an episode if it lands no earlier than this many
# reference hours before the episode starts. Matched to §7's own horizon.
LEAD_MAX = truth.HORIZON

DEFAULT_REPORT_PATH = os.path.join("data", "meals", "evaluation.md")


def evaluation_start(warmup: pd.DataFrame) -> int:
    """The first hour at which the whole basket is warm (§6.6).

    The maximum over the basket-wide triggers and over every asset's Q95 breach,
    which is what the cluster shift is built from. Not a per-asset filter: the
    detector is one instrument, and it is only itself once all of its inputs are.
    """
    relevant = warmup[(warmup["scope"] == "basket")
                      | (warmup["trigger"].isin(("breach_q95", "breach_q99")))]
    return int(relevant["first_valid_hour"].max())


def episodes(significant: np.ndarray) -> list[tuple[int, int]]:
    """Maximal runs of consecutive significant hours, as (start, end) positions.

    Positions in the reference-hour grid, both ends inclusive. NaN counts as not
    significant here - an hour whose forward window is missing cannot open or
    extend an episode, and truth.label has already marked it NULL.
    """
    flags = np.asarray(significant, dtype=bool)
    if not flags.any():
        return []
    edges = np.diff(np.concatenate([[0], flags.view(np.int8), [0]]))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1) - 1
    return list(zip(starts.tolist(), ends.tolist()))


def apply_cooldown(positions: np.ndarray, cooldown: int = windows.CLUSTER_COOLDOWN
                   ) -> np.ndarray:
    """Thins firings the way §5.1 thins events: keep one, then stay quiet.

    Used on the baselines so their alert count is comparable with the detector's.
    """
    kept, last = [], -10 ** 9
    for position in np.sort(np.asarray(positions)):
        if position - last >= cooldown:
            kept.append(int(position))
            last = position
    return np.array(kept, dtype=int)


def score(detections: np.ndarray, runs: list[tuple[int, int]],
          lead_max: int = LEAD_MAX) -> dict:
    """Precision, recall, F1 and lead time for one detector against one label."""
    detections = np.asarray(detections, dtype=int)
    if not runs:
        return {"detections": len(detections), "episodes": 0, "precision": np.nan,
                "recall": np.nan, "f1": np.nan, "median_lead": np.nan,
                "caught": 0, "ahead": 0}

    matched = np.zeros(len(detections), dtype=bool)
    caught, leads = 0, []
    for start, end in runs:
        window = (detections >= start - lead_max) & (detections <= end)
        if window.any():
            caught += 1
            # Lead is measured from the EARLIEST alert credited to the episode:
            # the first warning is the one that would have reached a person.
            leads.append(start - int(detections[window].min()))
        matched |= window

    precision = float(matched.mean()) if len(detections) else np.nan
    recall = caught / len(runs)
    # A detector that fired and hit nothing scores zero, not "undefined": both
    # rates are known, they are simply both zero. F1 is undefined only when
    # precision is - that is, when nothing fired at all and there is no rate to
    # take.
    if precision != precision:
        f1 = np.nan
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "detections": len(detections), "episodes": len(runs), "caught": caught,
        "precision": precision, "recall": recall, "f1": f1,
        "median_lead": float(np.median(leads)) if leads else np.nan,
        "ahead": int(sum(1 for lead in leads if lead > 0)),
    }


def _rate(mask: pd.Series) -> float:
    return float(mask.mean()) if len(mask) else np.nan


def diagnostics(basket: pd.DataFrame, events: pd.DataFrame,
                escalations: pd.DataFrame, labels: pd.DataFrame) -> dict:
    """The list §7 names explicitly, each measured on the scored window."""
    at_events = basket.reindex(events["t0_utc"]).dropna(how="all")
    out = {}

    out["events_by_month"] = (
        pd.to_datetime(events["t0_utc"], unit="s", utc=True)
        .dt.tz_localize(None).dt.to_period("M").value_counts().sort_index())

    triggers = [c for c in basket.columns if c.startswith("trigger_")]
    out["trigger_share"] = {
        t[len("trigger_"):]: _rate(at_events[t].fillna(False).astype(bool))
        for t in triggers}

    # The live sub-condition is basket_coherence; csv_compression is still
    # computed and reported because its emptiness is the evidence for §21.
    live = "basket_coherence" if "basket_coherence" in basket else "csv_compression"
    for name in ("csv_compression", "basket_coherence", "pca_sync"):
        out[f"{name}_rate"] = (_rate(basket[name].fillna(False).astype(bool))
                               if name in basket else np.nan)
    # §3.4 wants this correlation measured, and with a sub-condition that never
    # fired it was undefined. It is computable now that coherence replaced it.
    pair = basket[[live, "pca_sync"]].dropna().astype(float)
    varies = (len(pair) > 1 and pair[live].nunique() > 1
              and pair["pca_sync"].nunique() > 1)
    out["subcondition_corr"] = (float(pair[live].corr(pair["pca_sync"]))
                                if varies else np.nan)

    decisions = basket["decision"] if "decision" in basket else pd.Series(dtype=object)
    counts = decisions.value_counts()
    out["no_quorum_share"] = _rate(basket["quorum_ok"].fillna(False).astype(bool).eq(False))
    out["escalations"] = int(counts.get("escalation", 0))
    out["debounced"] = int(counts.get("branch_a_debounced", 0)
                           + counts.get("branch_b_debounced", 0))
    out["suppressed"] = int(counts.get("suppressed_by_cooldown", 0))

    # Firings near an important publication: the calendar multiplier is above one
    # exactly inside a High/Medium window (§4.3).
    near = basket["m_calendar"] > 1
    out["calendar_base_rate"] = _rate(near)
    out["calendar_at_events"] = _rate(at_events["m_calendar"] > 1)

    if not escalations.empty:
        hours = escalations["hour_utc"]
        rows = basket.reindex(hours).dropna(how="all")
        out["escalations_by_reason"] = escalations["reason"].value_counts().to_dict()
        out["escalations_near_calendar"] = _rate(rows["m_calendar"] > 1)
        # Would the escalation have cleared its threshold without the calendar?
        without = rows["base_points"] * rows["m_vix"]
        decisive = ((rows["si_total"] >= si_index.ESCALATION_THRESHOLD)
                    & (without < si_index.ESCALATION_THRESHOLD))
        out["calendar_decisive_for_escalation"] = _rate(decisive)
    else:
        out["escalations_by_reason"] = {}
        out["escalations_near_calendar"] = np.nan
        out["calendar_decisive_for_escalation"] = np.nan

    out["baseline_fires"] = int(labels["baseline_spy_trailing"].fillna(False).sum())
    return out


def _pct(value) -> str:
    return "—" if value != value else f"{100 * value:.1f}%"


def _num(value, digits=1) -> str:
    return "—" if value != value else f"{value:.{digits}f}"


def render(scores: dict, per_block: dict, diag: dict, start: int, end: int,
           thresholds: pd.DataFrame, periods: dict | None = None) -> str:
    span = (f"{datetime.fromtimestamp(start, tz=timezone.utc):%Y-%m-%d} .. "
            f"{datetime.fromtimestamp(end, tz=timezone.utc):%Y-%m-%d}")
    out = ["# MEALS backtest against the §7 yardstick", "",
           f"Scored window {span} — from the hour the whole basket is warm (§6.6). "
           "Recall is per episode, not per hour, and the baselines carry the same "
           "72-hour cooldown as the detector; see the module docstring for why.", ""]

    out += ["## Detectors", "",
            "| Detector | Alerts | Episodes | Caught | Precision | Recall | F1 | "
            "Median lead, h |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, s in scores.items():
        out.append(f"| {name} | {s['detections']} | {s['episodes']} | {s['caught']} | "
                   f"{_pct(s['precision'])} | {_pct(s['recall'])} | {_pct(s['f1'])} | "
                   f"{_num(s['median_lead'], 0)} |")
    out.append("")

    if periods:
        out += ["## Train and test (§7)", "",
                "Calibration happens on train alone; test is reported so the gap is "
                "visible, not so it can be tuned against.", "",
                "| Period | Alerts | Episodes | Caught | Precision | Recall | F1 |",
                "|---|---:|---:|---:|---:|---:|---:|"]
        for name, s_ in periods.items():
            out.append(f"| {name} | {s_['detections']} | {s_['episodes']} | "
                       f"{s_['caught']} | {_pct(s_['precision'])} | "
                       f"{_pct(s_['recall'])} | {_pct(s_['f1'])} |")
        out.append("")

    out += ["## By block", "",
            "| Block | Q99 of a 24h move | Episodes | Caught | Precision | Recall | "
            "Median lead, h |", "|---|---:|---:|---:|---:|---:|---:|"]
    limits = thresholds.set_index("block")["q99_abs_move"]
    for block, s in sorted(per_block.items()):
        out.append(f"| {block} | {limits.get(block, float('nan')):.4f} | "
                   f"{s['episodes']} | {s['caught']} | {_pct(s['precision'])} | "
                   f"{_pct(s['recall'])} | {_num(s['median_lead'], 0)} |")
    out.append("")

    out += ["## Diagnostics (§7)", "",
            "| Quantity | Value |", "|---|---|",
            f"| Hours without quorum | {_pct(diag['no_quorum_share'])} |",
            f"| Escalations | {diag['escalations']} |",
            f"| Hours suppressed by the cooldown | {diag['suppressed']} |",
            f"| Early breaks refused by the debounce | {diag['debounced']} |",
            f"| basket_coherence fires | {_pct(diag['basket_coherence_rate'])} |",
            f"| pca_sync fires | {_pct(diag['pca_sync_rate'])} |",
            f"| Correlation of the two (§3.4, drop one above 0.7) | "
            f"{_num(diag['subcondition_corr'], 3)} |",
            f"| csv_compression fires (retired, §21) | "
            f"{_pct(diag['csv_compression_rate'])} |",
            f"| Calendar multiplier above 1, all hours | "
            f"{_pct(diag['calendar_base_rate'])} |",
            f"| Calendar multiplier above 1, at events | "
            f"{_pct(diag['calendar_at_events'])} |",
            f"| Escalations inside a calendar window | "
            f"{_pct(diag['escalations_near_calendar'])} |",
            f"| Escalations the calendar multiplier decided | "
            f"{_pct(diag['calendar_decisive_for_escalation'])} |", ""]

    out += ["Escalations by reason: "
            + (", ".join(f"`{k}` {v}" for k, v in diag["escalations_by_reason"].items())
               or "none") + "", ""]

    out += ["Share of events on which each trigger was true: "
            + ", ".join(f"`{k}` {_pct(v)}" for k, v in sorted(diag["trigger_share"].items())),
            ""]

    out += ["## Events by month", "", "```"]
    out += [f"{str(period)}  {'#' * int(count)} {count}"
            for period, count in diag["events_by_month"].items()]
    out += ["```", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from meals import cluster, cross_section, journal

    parser = argparse.ArgumentParser(description="Backtest metrics (§7)")
    parser.add_argument("--labels", default=truth.DEFAULT_LABELS_PATH)
    parser.add_argument("--thresholds", default=truth.DEFAULT_THRESHOLDS_PATH)
    parser.add_argument("--basket-metrics", default=cross_section.DEFAULT_BASKET_METRICS_PATH)
    parser.add_argument("--events", default=cluster.DEFAULT_EVENTS_PATH)
    parser.add_argument("--escalations", default=cluster.DEFAULT_ESCALATIONS_PATH)
    parser.add_argument("--warmup", default=journal.DEFAULT_WARMUP_PATH)
    parser.add_argument("--out", default=DEFAULT_REPORT_PATH)
    parser.add_argument("--from-hour", type=int, default=None,
                        help="override the start of the scored window")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    labels = pd.read_parquet(args.labels).set_index("hour_utc").sort_index()
    thresholds = pd.read_parquet(args.thresholds)
    basket = pd.read_parquet(args.basket_metrics).set_index("hour_utc").sort_index()
    events = pd.read_parquet(args.events)
    escalations = pd.read_parquet(args.escalations)

    start = (args.from_hour if args.from_hour is not None
             else evaluation_start(pd.read_parquet(args.warmup)))
    scored = labels[(labels.index >= start) & labels["significant"].notna()]
    if scored.empty:
        log.error("nothing to score")
        return 2
    position = {hour: i for i, hour in enumerate(scored.index)}

    runs = episodes(scored["significant"].to_numpy(dtype=bool))
    detections = np.array([position[h] for h in events["t0_utc"] if h in position])

    scores = {"SI-Index cluster events": score(detections, runs)}
    for column, name in (("baseline_spy_trailing", "SPY trailing 24h (runnable)"),
                         ("baseline_spy", "SPY forward 24h (§7 as written)")):
        fires = np.array([position[h] for h in
                          scored.index[scored[column].fillna(False)]])
        scores[name] = score(apply_cooldown(fires), runs)
        scores[f"{name}, no cooldown"] = score(fires, runs)

    limits = thresholds.set_index("block")["q99_abs_move"]
    per_block = {}
    for block in limits.index:
        column = f"acc_{block}"
        if column not in scored:
            continue
        block_significant = scored[column].abs() > limits[block]
        per_block[block] = score(detections, episodes(block_significant.to_numpy(dtype=bool)))

    boundary = int(truth.TRAIN_END.timestamp())
    periods = {}
    for name, mask in (("train", scored.index < boundary),
                       ("test", scored.index >= boundary)):
        part = scored[mask]
        if part.empty:
            continue
        local = {hour: i for i, hour in enumerate(part.index)}
        periods[name] = score(
            np.array([local[h] for h in events["t0_utc"] if h in local]),
            episodes(part["significant"].to_numpy(dtype=bool)))

    diag = diagnostics(basket.loc[basket.index >= start],
                       events[events["t0_utc"] >= start],
                       escalations[escalations["hour_utc"] >= start], scored)

    report = render(scores, per_block, diag, int(scored.index.min()),
                    int(scored.index.max()), thresholds, periods)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

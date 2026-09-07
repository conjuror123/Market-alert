"""Precision at a matched alert budget, for MEALS against the runnable baseline.

The comparison evaluate.md makes is unfair in a way that matters: it scores
detectors that fire 106 and 940 times as though they cost the same. F1 has no
term for how often a person is interrupted, and for an alerting system with a
fixed budget that is the whole question.

This asks the question the budget makes: GIVEN N alerts a year, how many of the
episodes does each detector catch, both ranked most-confident-first and both
carrying the same 72-hour cooldown.

    python -m tools.budget_curve
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SCORED_FROM = "2021-11-14"
COOLDOWN = 72 * 3600
LEAD = 24 * 3600
TIER_ORDER = {"routine": 0, "notable": 1, "major": 2, "extreme": 3}


def episodes(labels: pd.DataFrame, start: int) -> list[tuple[int, int]]:
    """Contiguous significant hours collapsed into one episode, as evaluate does."""
    sig = labels["significant"].astype("boolean").fillna(False).astype(bool)
    sig = sig[sig.index >= start]
    runs = (sig != sig.shift()).cumsum()
    return [(g.index.min(), g.index.max())
            for _, g in sig.groupby(runs) if bool(g.iloc[0])]


def cooldown(hours, gap: int = COOLDOWN) -> list[int]:
    kept, last = [], -10 ** 9
    for hour in sorted(hours):
        if hour - last >= gap:
            kept.append(hour)
            last = hour
    return kept


def caught(alerts, spans) -> int:
    a = np.sort(np.unique(np.asarray(alerts)))
    return sum(1 for lo, hi in spans
               if ((a >= lo - LEAD) & (a <= hi)).any())


def main() -> int:
    labels = pd.read_parquet("data/meals/truth_labels.parquet") \
        .set_index("hour_utc").sort_index()
    events = pd.read_parquet("data/meals/saed_events.parquet")
    start = int(pd.Timestamp(SCORED_FROM, tz="UTC").timestamp())
    spans = episodes(labels, start)
    years = (labels.index.max() - start) / (365.25 * 86400)

    pushes = events[(events["channel"] == "push")
                    & (events["hour_utc"] >= start)].copy()
    pushes["confidence"] = (pushes["tier"].map(TIER_ORDER).fillna(0) * 1e6
                            + pushes["z_resid"].abs().fillna(0))
    ours = cooldown(pushes.sort_values("confidence", ascending=False)["hour_utc"])

    fires = labels["baseline_spy_trailing"].astype("boolean").fillna(False).astype(bool)
    hours = np.array([h for h in fires[fires].index if h >= start])
    theirs = cooldown(labels.loc[hours, "acc_baseline_trailing"].abs()
                      .sort_values(ascending=False).index)

    print(f"{len(spans)} episodes, {years:.1f} years, both ranked most-confident "
          f"first with the same {COOLDOWN // 3600}h cooldown\n")
    print(f"{'alerts/yr':>9s} {'budget':>7s} | {'MEALS':>18s} | {'SPY trailing 24h':>18s}")
    for n in (25, 50, 75, 105, 150):
        a, b = ours[:n], theirs[:n]
        if not a or not b:
            continue
        ca, cb = caught(a, spans), caught(b, spans)
        print(f"{n / years:>9.0f} {n:>7d} | {ca:>4d}/{len(spans)}  P {ca / len(a) * 100:5.1f}%"
              f" | {cb:>4d}/{len(spans)}  P {cb / len(b) * 100:5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

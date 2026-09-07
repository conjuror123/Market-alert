"""Precision at a matched alert budget, for Tremor against the runnable baseline.

WHAT PRECISION MEANS HERE, because it is easy to read as more than it is: the
share of alerts landing within 24 hours BEFORE an episode starts, or DURING it.
It therefore credits coincidence as much as prediction, and the proof is that
delaying every alert by the six hours a `major` push really waits RAISES it -
from 52.0% to 56.0% at a budget of 25 - while cutting the median lead from 6
hours to 4. A predictive measure cannot behave that way. Read precision as "was
the alert about something real", and read the median lead as "how much warning",
and never read the first as though it were the second.

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

from tremor import routing

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
    labels = pd.read_parquet("data/tremor/truth_labels.parquet") \
        .set_index("hour_utc").sort_index()
    events = pd.read_parquet("data/tremor/saed_events.parquet")
    start = int(pd.Timestamp(SCORED_FROM, tz="UTC").timestamp())
    spans = episodes(labels, start)
    years = (labels.index.max() - start) / (365.25 * 86400)

    pushes = events[(events["channel"] == "push")
                    & (events["hour_utc"] >= start)].copy()
    pushes["confidence"] = (pushes["tier"].map(TIER_ORDER).fillna(0) * 1e6
                            + pushes["z_resid"].abs().fillna(0))
    # WHEN THE ALERT COULD ACTUALLY HAVE BEEN SENT, which is not the hour the
    # event opened. Two delays are real and were both being ignored:
    #   - an event's tier is not known until the bar that earned it, which for
    #     an escalating event is later than its opening hour;
    #   - a `major` push waits DELAY_HORIZON hours for the retention check, by
    #     construction (routing.PUSH_DELAYED_TIER). It cannot be sent before.
    # Scoring at the opening hour credits the detector with a lead it does not
    # have, which is look-ahead however small it turns out to be.
    delay = np.where(pushes["tier"].to_numpy() == routing.PUSH_DELAYED_TIER,
                     routing.DELAY_HORIZON * 3600, 0)
    pushes["sent_at"] = pushes["peak_hour_utc"].to_numpy() + delay
    ours = cooldown(pushes.sort_values("confidence", ascending=False)["sent_at"])

    fires = labels["baseline_spy_trailing"].astype("boolean").fillna(False).astype(bool)
    hours = np.array([h for h in fires[fires].index if h >= start])
    theirs = cooldown(labels.loc[hours, "acc_baseline_trailing"].abs()
                      .sort_values(ascending=False).index)

    print(f"{len(spans)} episodes, {years:.1f} years, both ranked most-confident "
          f"first with the same {COOLDOWN // 3600}h cooldown.")
    print("Tremor alerts are timed at the moment they could actually have been "
          "sent.\n")
    print(f"{'alerts/yr':>9s} {'budget':>7s} | {'Tremor':>18s} | {'SPY trailing 24h':>18s}")
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

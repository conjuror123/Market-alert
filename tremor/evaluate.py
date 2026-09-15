"""Scoring helpers, and the report on the detector that is actually delivered.

WHAT LEFT THIS FILE. It used to be the evaluation of the SI-Index cluster
detector: episode labelling from tremor.truth, a per-block precision/recall
table, a diagnostics section on the basket's own derived columns, and a
walk-forward comparison against baselines. Every one of those described a
detector whose output no module ever read - see the deletion that removed
tremor.cluster and tremor.si_index - so the report was a careful measurement
of something nobody received.

WHAT STAYED. Two things. The episode and cooldown helpers below, which are
about how you score ANY detector of bursty events and are used by the scorer
for the live one; and the entry point, which now renders that scorer's report
and nothing else.

RECALL IS PER EPISODE, NOT PER HOUR, and that is the reason these helpers are
worth keeping. A detector holds a cooldown after it fires, so per-hour recall
would mostly measure the cooldown. Contiguous significant hours are collapsed
into one episode and an episode counts as caught if any alert lands in it.
"""
from __future__ import annotations

import logging
import os

import numpy as np

from tremor import saed_score, windows

DEFAULT_REPORT_PATH = os.path.join("data", "tremor", "evaluation.md")

# How far AHEAD of an episode an alert may land and still be credited with it,
# in reference-calendar hours. It was tremor.truth.HORIZON, which was the
# labelling protocol's horizon and is stated here now that the protocol is gone;
# the number is unchanged and windows.TRUTH_HORIZON is still where it lives.
LEAD_MAX = windows.TRUTH_HORIZON


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


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Report on the delivered detector (spec section 7). "
                    "data/tremor/evaluation.md is a frozen artifact; this "
                    "entry point refuses to overwrite it unless --force.")
    parser.add_argument("--out", default=DEFAULT_REPORT_PATH)
    parser.add_argument("--force", action="store_true",
                        help="overwrite the frozen tracked report. That file "
                             "records a frequency claim the live detector no "
                             "longer makes (docs/decisions.md).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("tremor.evaluate")

    if (os.path.abspath(args.out) == os.path.abspath(DEFAULT_REPORT_PATH)
            and not args.force):
        log.error("%s is a frozen artifact of a frequency claim the live "
                  "detector no longer makes (see docs/decisions.md). Pass "
                  "--force to overwrite it, or --out elsewhere.", args.out)
        return 2

    report = saed_score.build()
    if not report:
        log.error("No SAED events to score - run python -m tremor.saed first")
        return 2
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as handle:
        handle.write("# Tremor evaluation\n\n" + report + "\n")
    log.info("Wrote %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

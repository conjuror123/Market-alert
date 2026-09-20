"""Scoring helpers for a detector of bursty events.

NOTHING IN THE PIPELINE CALLS THESE TODAY. There was an entry point here that
rendered a report to data/tremor/evaluation.md; it is gone, because that report
scored the SI-Index cluster channel rather than the detector that is delivered,
against a forecasting label neither of them claims to answer. tremor.saed_score
scores the live detector and carries its own episode helper. These are kept
because the reasoning below is the part that was hard to get right, and the
tests pin it; they are a library, not a live path.

RECALL IS PER EPISODE, NOT PER HOUR, and that is the reason these helpers are
worth keeping. A detector holds a cooldown after it fires, so per-hour recall
would mostly measure the cooldown. Contiguous significant hours are collapsed
into one episode and an episode counts as caught if any alert lands in it.
"""
from __future__ import annotations

import numpy as np

from tremor import windows

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
    """Thins firings the way the cluster detector thins events: keep one, then stay quiet.

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

"""Calibration of the starred constants on train (spec §7).

§7 names what may be fitted - THRESHOLD, ESCALATION_THRESHOLD, the trigger
weights, the V_R threshold, the coefficients of the absolute legs, k_t and the
multiplier parameters - and it names where: train alone, 2021-01-01 to
2023-12-31, with the result frozen under a config_version before test is run at
all. This module does the fitting and nothing else. It never reads a test hour,
and it writes its answer to disk so the freeze can be checked afterwards rather
than trusted.

WHAT IS OPTIMISED. Not plain train F1. Train is cut into three folds holding an
equal number of episodes each, and the objective is the MEAN of the per-fold F1
LESS their standard deviation, subject to one operational constraint stated in
advance: between 0.2 and 3.0 alerts a week, the band this project has always
worked to. Without the band the search buys precision with silence - a detector
that fires four times in two years scores wonderfully and is useless.

The reason for the fold objective is measured, not theoretical. Optimising plain
train F1 took it from 0.2725 to 0.3426 and left five of the thirteen parameters
sitting on the edges of their grids, with the VIX multiplier switched off
entirely and the escalation threshold pushed high enough to disable escalations -
while the gap between the two halves of train WIDENED, 0.383 against 0.2243. That
is the signature of fitting the noise between 111 episodes with thirteen
parameters. Penalising the spread across folds selects for a configuration that
works in more than one regime, which is the thing the test period will ask for.
Folds are cut by episode count rather than by hours because the train period is
front-loaded: split down the middle by time, one half holds 92 episodes and the
other 19.

HOW IT IS SEARCHED. Coordinate descent over coarse grids: one parameter at a
time, cycling until nothing improves. Not because it is the best optimiser but
because it is legible - every step is a one-parameter sweep whose whole curve can
be printed and argued with, which matters more here than squeezing the last
percent out of 111 training episodes.

WHY THE GRIDS ARE COARSE. There are 111 episodes on train and a dozen
parameters. A fine grid would fit the noise between them and the test period
would say so. The stability check at the end splits train in half by time and
reports F1 on each; if the two disagree sharply the fit is not to be believed
whatever the headline number says.

WHAT IS CHEAP AND WHAT IS NOT. Both multipliers are linear in their peak, so
m(s) = 1 + s * (m(1) - 1) and one precomputed series covers the whole grid. The
absolute legs, the volume threshold and the coherence quantile change which
triggers fire, so they need the trigger frame rebuilt - cached by their own key,
since coordinate descent revisits combinations. The weights and thresholds are
arithmetic on top and cost nothing.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace

import numpy as np
import pandas as pd

from meals import cluster, evaluate, si_index, truth, windows

log = logging.getLogger("meals.calibrate")

DEFAULT_RESULT_PATH = os.path.join("data", "meals", "calibration.json")

# The operational band, in alerts per week on train. Declared before the search,
# not chosen after seeing the answers.
MIN_ALERTS_PER_WEEK = 0.2
MAX_ALERTS_PER_WEEK = 3.0

# Folds within train, cut to hold an equal number of episodes each.
FOLDS = 3

# The peak values the multipliers were given structurally in phase 7 (deviation
# §20). The search scales the EXCESS over one: strength 0 switches a multiplier
# off entirely, 1 leaves it as set. Asking "does this multiplier earn its place"
# is worth more here than fitting four peaks separately on 111 episodes.
CALENDAR_BASE = 1.0
VIX_BASE = 1.0


@dataclass(frozen=True)
class Parameters:
    abs_leg_q95: float = windows.ABS_LEG_Q95
    abs_leg_q99: float = windows.ABS_LEG_Q99
    volume_confirm: float = windows.VOLUME_CONFIRM
    coherence_quantile: float = windows.COHERENCE_QUANTILE
    points_price_shock: float = si_index.POINTS_PRICE_SHOCK
    points_volume: float = si_index.POINTS_VOLUME
    points_single_factor: float = si_index.POINTS_SINGLE_FACTOR
    points_breadth: float = si_index.POINTS_BREADTH
    calendar_strength: float = 1.0
    vix_strength: float = 1.0
    threshold: float = si_index.THRESHOLD
    escalation_threshold: float = si_index.ESCALATION_THRESHOLD
    reversal_k_min: float = windows.REVERSAL_K_MIN


# POINTS_CLUSTER_SHIFT is not searched: with every weight free the score's scale
# is arbitrary - doubling all weights and the threshold changes nothing - so one
# weight has to be pinned for the rest to mean anything. It keeps the spec's 4.
# Every grid reaches past where the answer is expected to fall. An optimum on a
# grid edge is not an optimum, it is the grid saying it was drawn too small - the
# first run of this search put three parameters on their edges and the grids were
# widened until none of them sat there.
GRIDS = {
    "abs_leg_q99": [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0],
    "abs_leg_q95": [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0],
    "volume_confirm": [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0],
    "coherence_quantile": [0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95],
    "points_price_shock": [0, 1, 2, 3, 4, 5, 6],
    "points_volume": [0, 1, 2, 3, 4, 5, 6, 8],
    "points_single_factor": [0, 1, 2, 3, 4, 5, 6],
    "points_breadth": [0, 1, 2, 3, 4, 6, 8, 10, 14, 20],
    "calendar_strength": [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0],
    "vix_strength": [0.0, 0.5, 1.0, 1.5, 2.0, 3.0],
    "reversal_k_min": [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5],
    "threshold": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 18, 20, 24, 30, 36],
    "escalation_threshold": [8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 40, 48],
}

# Searched in this order, thresholds last in each cycle: the weights move the
# scale the thresholds live on, so they are re-fitted after every change to it.
ORDER = ["abs_leg_q99", "abs_leg_q95", "volume_confirm", "coherence_quantile",
         "points_price_shock", "points_volume", "points_single_factor",
         "points_breadth", "calendar_strength", "vix_strength",
         "reversal_k_min", "threshold", "escalation_threshold"]


class Harness:
    """Everything the search needs, loaded once and reused for every candidate."""

    def __init__(self, basket, metrics: dict[str, pd.DataFrame],
                 basket_frame: pd.DataFrame, labels: pd.DataFrame, start: int):
        from meals import cross_section

        self.basket = basket
        self.metrics = metrics
        self.hours = basket_frame.index
        self.frame = basket_frame
        self.cross_section = cross_section

        scored = labels[(labels.index >= start) & labels["significant"].notna()]
        boundary = int(truth.TRAIN_END.timestamp())
        self.train = scored[scored.index < boundary]
        self.position = {hour: i for i, hour in enumerate(self.train.index)}
        self.episodes = evaluate.episodes(self.train["significant"].to_numpy(dtype=bool))
        self.weeks = max(len(self.train) / (24 * 7), 1e-9)
        self.folds = self._folds(FOLDS)

        # Both multipliers are linear in their peak, so the excess over one is
        # all that needs storing.
        self.calendar_excess = basket_frame["m_calendar"].fillna(1.0) - 1.0
        self.vix_excess = basket_frame["m_vix"].fillna(1.0) - 1.0
        self._triggers: dict[tuple, pd.DataFrame] = {}

    def _folds(self, count: int) -> list[tuple[dict, list]]:
        """Contiguous stretches of train holding an equal number of episodes.

        Cut by episodes rather than by hours: train is front-loaded, and halving
        it by time puts 92 episodes on one side and 19 on the other, which makes
        the quieter fold's F1 mostly noise.
        """
        hours = self.train.index
        if not self.episodes:
            return []
        edges = [0]
        for part in range(1, count):
            index = (len(self.episodes) * part) // count
            edges.append(self.episodes[index][0])
        edges.append(len(hours))

        out = []
        for low, high in zip(edges, edges[1:]):
            part = self.train.iloc[low:high]
            if part.empty:
                continue
            out.append(({hour: i for i, hour in enumerate(part.index)},
                        evaluate.episodes(part["significant"].to_numpy(dtype=bool))))
        return out

    def triggers(self, params: Parameters) -> pd.DataFrame:
        """Trigger columns for the parameters that decide which triggers fire."""
        from meals import zscore

        key = (params.abs_leg_q95, params.abs_leg_q99, params.volume_confirm,
               params.coherence_quantile)
        if key in self._triggers:
            return self._triggers[key]

        rebreached = {}
        for asset_id, frame in self.metrics.items():
            indexed = frame.set_index("hour_utc")
            hit = zscore.breaches(indexed["z"].abs(), indexed["r"].abs(),
                                  indexed["sigma_lt"], indexed["q95"], indexed["q99"],
                                  params.abs_leg_q95, params.abs_leg_q99)
            rebreached[asset_id] = indexed.assign(
                breach_q95=hit["breach_q95"].to_numpy(),
                breach_q99=hit["breach_q99"].to_numpy()).reset_index()

        agreement, _ = self.cross_section.coherence_compression(
            self.frame["coherence"], self.frame["m_weighted_median"])
        if params.coherence_quantile != windows.COHERENCE_QUANTILE:
            threshold = (self.frame["coherence"].shift(1)
                         .rolling(windows.W_CS, min_periods=windows.W_CS)
                         .quantile(params.coherence_quantile))
            m_std = (self.frame["m_weighted_median"].shift(1)
                     .rolling(windows.W_CS, min_periods=windows.W_CS).std(ddof=1))
            agreement = (((self.frame["coherence"] > threshold)
                          & (self.frame["m_weighted_median"].abs() > 2 * m_std))
                         .where(threshold.notna() & m_std.notna(), pd.NA)
                         .astype("boolean"))
        single = self.cross_section.single_factor(
            agreement, self.frame["pca_sync"]).where(self.frame["quorum_ok"], pd.NA)

        out = si_index.base_points(self.basket, rebreached, single, self.hours,
                                   params.volume_confirm)
        self._triggers[key] = out
        return out

    def events(self, params: Parameters):
        triggers = self.triggers(params)
        points = (triggers["trigger_price_shock"] * params.points_price_shock
                  + triggers["trigger_volume"] * params.points_volume
                  + triggers["trigger_cluster_shift"] * si_index.POINTS_CLUSTER_SHIFT
                  + triggers["trigger_single_factor"] * params.points_single_factor
                  + triggers["trigger_cluster_shift"] * triggers["breadth_share"]
                  * params.points_breadth)

        frame = self.frame.assign(
            base_points=points,
            trigger_cluster_shift=triggers["trigger_cluster_shift"],
            breadth_q99=triggers["breadth_q99"],
            si_total=points * (1 + params.calendar_strength * self.calendar_excess)
            * (1 + params.vix_strength * self.vix_excess))
        frame = frame.drop(columns=["sigma_m", "k"], errors="ignore").join(
            cluster.reversal_scale(frame["m_weighted_median"],
                                   k_min=params.reversal_k_min))
        found, _ = cluster.run(frame, params.threshold, params.escalation_threshold)
        return found

    def score(self, params: Parameters) -> dict:
        found = self.events(params)
        detections = np.array([self.position[e.t0_utc] for e in found
                               if e.t0_utc in self.position])
        result = evaluate.score(detections, self.episodes)
        result["per_week"] = len(detections) / self.weeks

        per_fold = []
        for local, runs in self.folds:
            inside = np.array([local[e.t0_utc] for e in found if e.t0_utc in local])
            fold = evaluate.score(inside, runs)["f1"]
            per_fold.append(0.0 if fold != fold else fold)
        result["fold_f1"] = per_fold
        result["fold_mean"] = float(np.mean(per_fold)) if per_fold else float("nan")
        result["fold_spread"] = float(np.std(per_fold)) if per_fold else float("nan")

        if not (MIN_ALERTS_PER_WEEK <= result["per_week"] <= MAX_ALERTS_PER_WEEK):
            # Outside the declared band the candidate is not compared at all - it
            # is simply not a configuration this project would ship.
            result["objective"] = -1.0
        else:
            # Mean across folds, less their spread: a configuration that works in
            # one regime and not the next is worth less than a steadier one, and
            # thirteen parameters over 111 episodes will find the former if the
            # objective lets them.
            result["objective"] = result["fold_mean"] - result["fold_spread"]
        return result


def search_once(harness: Harness, start: Parameters | None = None,
                cycles: int = 3, names: list[str] | None = None
                ) -> tuple[Parameters, dict, list]:
    """Coordinate descent over the grids. Returns the best found and its trace."""
    best = start or Parameters()
    best_score = harness.score(best)
    trace = []

    for cycle in range(cycles):
        improved = False
        for name in (names or ORDER):
            candidates = []
            for value in GRIDS[name]:
                trial = replace(best, **{name: value})
                candidates.append((value, harness.score(trial)))
            value, result = max(candidates, key=lambda pair: pair[1]["objective"])
            trace.append({"cycle": cycle, "parameter": name, "chosen": value,
                          "curve": [(v, round(s["objective"], 4)) for v, s in candidates]})
            if result["objective"] > best_score["objective"] + 1e-9:
                best, best_score, improved = (replace(best, **{name: value}),
                                              result, True)
                log.info("cycle %d: %s -> %s   F1 %.4f (%d alerts, %.2f/week)",
                         cycle, name, value, best_score["objective"],
                         best_score["detections"], best_score["per_week"])
        if not improved:
            break
    return best, best_score, trace


def search(harness: Harness, cycles: int = 3, restarts: int = 8,
           seed: int = 0, names: list[str] | None = None
           ) -> tuple[Parameters, dict, list]:
    """Coordinate descent from several starting points, best train F1 wins.

    One descent is greedy and path-dependent: a large early step can walk it into
    a worse local optimum than a smaller one would have found, and widening the
    grids made exactly that happen - the wider search scored below the narrower
    one from the same start. Restarting from scattered points costs a minute and
    removes the dependence on where the walk began.

    The starts are drawn from the grids themselves with a fixed seed, so the
    calibration is reproducible: the same data and the same grids give the same
    answer, which §6.2 requires of everything else here too.
    """
    order = names or ORDER
    rng = np.random.default_rng(seed)
    starts = [Parameters()]
    for _ in range(max(restarts - 1, 0)):
        starts.append(Parameters(**{name: type(getattr(Parameters(), name))(
            rng.choice(GRIDS[name])) for name in order}))

    best, best_score, trace = None, {"objective": -2.0}, []
    for index, start in enumerate(starts):
        found, score_of, walk = search_once(harness, start, cycles, order)
        log.info("start %d/%d: F1 %.4f (%d alerts, %.2f/week)", index + 1,
                 len(starts), score_of["objective"], score_of["detections"],
                 score_of["per_week"])
        if score_of["objective"] > best_score["objective"]:
            best, best_score = found, score_of
            trace = [{"start": index, **step} for step in walk]
    return best, best_score, trace


def stability(harness: Harness, params: Parameters) -> dict:
    """F1 on each half of train, by time.

    A fit that holds on one half and collapses on the other is a fit to the
    noise between 111 episodes, and the headline number is not to be believed
    whatever it says.
    """
    found = harness.events(params)
    hours = harness.train.index
    middle = hours[len(hours) // 2]
    out = {}
    for name, mask in (("first half", hours < middle), ("second half", hours >= middle)):
        part = harness.train[mask]
        local = {hour: i for i, hour in enumerate(part.index)}
        result = evaluate.score(
            np.array([local[e.t0_utc] for e in found if e.t0_utc in local]),
            evaluate.episodes(part["significant"].to_numpy(dtype=bool)))
        out[name] = {k: (None if result[k] != result[k] else round(result[k], 4))
                     for k in ("precision", "recall", "f1", "detections", "episodes")}
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    from meals import cross_section, journal, pipeline
    from meals.basket import load_basket

    parser = argparse.ArgumentParser(description="Calibrate the starred constants on train (§7)")
    parser.add_argument("--metrics-dir", default=pipeline.DEFAULT_METRICS_DIR)
    parser.add_argument("--basket-metrics", default=cross_section.DEFAULT_BASKET_METRICS_PATH)
    parser.add_argument("--labels", default=truth.DEFAULT_LABELS_PATH)
    parser.add_argument("--warmup", default=journal.DEFAULT_WARMUP_PATH)
    parser.add_argument("--out", default=DEFAULT_RESULT_PATH)
    parser.add_argument("--cycles", type=int, default=4)
    parser.add_argument("--restarts", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--parameters", default=None,
                        help="comma-separated subset to search; the rest keep "
                             "their configured values")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    basket = load_basket()
    harness = Harness(
        basket,
        pipeline.load_all(basket, args.metrics_dir),
        pd.read_parquet(args.basket_metrics).set_index("hour_utc").sort_index(),
        pd.read_parquet(args.labels).set_index("hour_utc").sort_index(),
        evaluate.evaluation_start(pd.read_parquet(args.warmup)))

    log.info("train: %d hours, %d episodes, %.0f weeks",
             len(harness.train), len(harness.episodes), harness.weeks)
    log.info("folds: %s episodes", [len(runs) for _, runs in harness.folds])
    before = harness.score(Parameters())
    log.info("starting point: objective %.4f | train F1 %.4f, precision %.4f, "
             "recall %.4f, %d alerts", before["objective"], before["f1"],
             before["precision"], before["recall"], before["detections"])

    names = args.parameters.split(",") if args.parameters else None
    if names:
        unknown = [n for n in names if n not in GRIDS]
        if unknown:
            log.error("no such parameter: %s", unknown)
            return 2
        log.info("searching %d of %d parameters: %s", len(names), len(ORDER), names)
    best, best_score, trace = search(harness, cycles=args.cycles,
                                     restarts=args.restarts, seed=args.seed,
                                     names=names)
    halves = stability(harness, best)

    log.info("chosen: %s", asdict(best))
    log.info("objective %.4f -> %.4f | train F1 %.4f -> %.4f",
             before["objective"], best_score["objective"], before["f1"],
             best_score["f1"])
    log.info("per-fold F1: %s (mean %.4f, spread %.4f)",
             [round(v, 4) for v in best_score["fold_f1"]],
             best_score["fold_mean"], best_score["fold_spread"])

    edges = [name for name in (names or ORDER)
             if getattr(best, name) in (GRIDS[name][0], GRIDS[name][-1])]
    if edges:
        log.warning("on a grid edge, so the grid may be too narrow: %s", edges)
    for name, part in halves.items():
        log.info("  %-11s F1 %s (precision %s, recall %s, %s alerts, %s episodes)",
                 name, part["f1"], part["precision"], part["recall"],
                 part["detections"], part["episodes"])

    payload = {
        "parameters": asdict(best),
        "train": {k: (None if best_score[k] != best_score[k] else best_score[k])
                  for k in ("precision", "recall", "f1", "detections", "episodes",
                            "caught", "median_lead", "per_week", "objective",
                            "fold_f1", "fold_mean", "fold_spread")},
        "train_before": {k: (None if before[k] != before[k] else before[k])
                         for k in ("precision", "recall", "f1", "detections")},
        "stability": halves,
        "band_per_week": [MIN_ALERTS_PER_WEEK, MAX_ALERTS_PER_WEEK],
        "search": {"cycles": args.cycles, "restarts": args.restarts,
                   "seed": args.seed, "parameters": names or ORDER},
        "trace": trace,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, sort_keys=True)
        f.write("\n")
    log.info("written to %s - apply these to the constants, then freeze", args.out)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

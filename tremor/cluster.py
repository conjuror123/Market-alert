"""Cluster events: gate, cooldown, escalations (spec §4.1, §5).

The cooldown here is not a mute timer but a way of telling a NEW event from the
continuation of an old one. After a strong move the market keeps rumbling for
several days, and without a pause the system would report the same storm every
hour. But a rigid pause fails the opposite way: if something genuinely larger
happens inside it, staying silent is not an option. Hence the two early-break
branches - and the debounce that keeps them from turning into an ordinary stream.

The priority of the branches is stated outright: if both hold in the same hour,
the higher-order shock applies and the vector reversal is not considered that
hour. The difference matters - a shock extends the current event, a reversal
opens a new one with a link to its parent.

Everything is measured in reference-calendar hours, so the automaton walks their
ordered list rather than calendar time: 72 reference hours are three trading
days, not three calendar days.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tremor import si_index, versioning, windows


@dataclass
class ClusterEvent:
    event_id: str
    t0_utc: int
    si_total_t0: float
    base_points_t0: int
    parent_event_id: str | None = None
    escalation_seq: int = 0
    escalations: list[dict] = field(default_factory=list)
    status: str = "open"
    # The hour at which this event stops being active - its cooldown expiring, or
    # a vector reversal closing it early. None means it was still open when the
    # history ran out. Not part of cluster_events (§6.4 does not list it there);
    # it exists because §8.2 needs to know which hours an event was active over.
    cooldown_until_utc: int | None = None


# Branch A is forbidden in the first trading hour after T0 (§5.2).
BRANCH_A_MIN_AGE = 1
BRANCH_B_MIN_AGE = windows.REVERSAL_DELAY
MAX_EARLY_BREAKS = 2


def reversal_scale(m: pd.Series, window: int = windows.W_CS,
                   k_min: float | None = None) -> pd.DataFrame:
    """sigma_M and k_t for the vector-reversal branch (§5.2).

    k_t is taken as the maximum of the empirical percentile and 1.5: the
    percentile adapts to the period, and in a prolonged lull it would sink so low
    that any wobble would count as a reversal.
    """
    sigma = m.shift(1).rolling(window, min_periods=window).std(ddof=1)
    normalised = (m / sigma).abs()
    k = normalised.shift(1).rolling(window, min_periods=window).quantile(0.975)
    floor = windows.REVERSAL_K_MIN if k_min is None else k_min
    return pd.DataFrame({"sigma_m": sigma, "k": np.maximum(k, floor)})


def run(frame: pd.DataFrame, threshold: float | None = None,
        escalation_threshold: float | None = None) -> tuple[list[ClusterEvent], pd.DataFrame]:
    """Sequential automaton over reference-calendar hours.

    The input is a frame indexed by ascending hours with the columns:
    quorum_ok, trigger_cluster_shift, si_total, base_points, breadth_q99,
    m_weighted_median, sigma_m, k.

    Returns the events and an hour-by-hour decision journal: what exactly fired
    in each hour and why a notification went out or did not.
    """
    # Arguments rather than constants only so that §7 can sweep them; the
    # defaults are the configuration and are what every real run uses.
    gate_at = si_index.THRESHOLD if threshold is None else threshold
    escalate_at = (si_index.ESCALATION_THRESHOLD if escalation_threshold is None
                   else escalation_threshold)

    hours = frame.index.to_numpy()
    quorum = frame["quorum_ok"].fillna(False).to_numpy(dtype=bool)
    shift = frame["trigger_cluster_shift"].fillna(False).to_numpy(dtype=bool)
    si = frame["si_total"].fillna(0.0).to_numpy(dtype=float)
    points = frame["base_points"].fillna(0).to_numpy(dtype=int)
    breadth = frame["breadth_q99"].fillna(False).to_numpy(dtype=bool)
    m = frame["m_weighted_median"].to_numpy(dtype=float)
    sigma_m = frame["sigma_m"].to_numpy(dtype=float)
    k = frame["k"].to_numpy(dtype=float)

    events: list[ClusterEvent] = []
    journal = np.full(len(frame), "", dtype=object)

    current: ClusterEvent | None = None
    opened_at = -1              # index of the current event's T0 hour
    cooldown_until = -1         # index up to which the pause applies

    def close_at(event: ClusterEvent, index: int) -> None:
        """Records the hour the event stops being active (§8.2 needs the span)."""
        event.cooldown_until_utc = int(hours[index]) if index < len(hours) else None

    early_breaks: list[int] = []
    m_at_t0 = np.nan

    for i in range(len(frame)):
        if not quorum[i]:
            journal[i] = "no_quorum"
            continue

        gate = shift[i] and si[i] >= gate_at

        if current is None or i >= cooldown_until:
            if current is not None and i >= cooldown_until:
                current.status = "finished"
                current = None
            if gate:
                current = ClusterEvent(event_id=f"cluster:{int(hours[i])}",
                                       t0_utc=int(hours[i]), si_total_t0=float(si[i]),
                                       base_points_t0=int(points[i]))
                events.append(current)
                opened_at, m_at_t0 = i, m[i]
                cooldown_until = i + windows.CLUSTER_COOLDOWN
                close_at(current, cooldown_until)
                early_breaks = []
                journal[i] = "event_created"
            elif shift[i]:
                journal[i] = "gate_below_threshold"
            continue

        # --- inside the cooldown ---
        age = i - opened_at
        # Debounce: at most two breaks per rolling window of 24 reference hours.
        recent = [b for b in early_breaks if i - b < windows.ESCALATION_DEBOUNCE]
        exhausted = len(recent) >= MAX_EARLY_BREAKS

        higher_order = (age >= BRANCH_A_MIN_AGE
                        and (si[i] >= escalate_at or breadth[i]))
        reversal = False
        if age >= BRANCH_B_MIN_AGE and gate and np.isfinite(sigma_m[i]) and np.isfinite(k[i]):
            limit = k[i] * sigma_m[i]
            reversal = ((m_at_t0 <= -limit and m[i] >= limit)
                        or (m_at_t0 >= limit and m[i] <= -limit))

        if higher_order:
            if exhausted:
                journal[i] = "branch_a_debounced"
                continue
            current.escalation_seq += 1
            current.escalations.append({
                "seq": current.escalation_seq, "hour_utc": int(hours[i]),
                "kind": "higher_order_shock", "si_total": float(si[i]),
                "reason": ("si>=escalation_threshold"
                           if si[i] >= escalate_at else "breadth_q99"),
            })
            # The cooldown restarts from the moment of the escalation.
            cooldown_until = i + windows.CLUSTER_COOLDOWN
            close_at(current, cooldown_until)
            early_breaks.append(i)
            journal[i] = "escalation"
            continue

        if reversal:
            if exhausted:
                journal[i] = "branch_b_debounced"
                continue
            parent = current.event_id
            current.status = "finished"
            close_at(current, i)
            current = ClusterEvent(event_id=f"cluster:{int(hours[i])}",
                                   t0_utc=int(hours[i]), si_total_t0=float(si[i]),
                                   base_points_t0=int(points[i]), parent_event_id=parent)
            events.append(current)
            opened_at, m_at_t0 = i, m[i]
            cooldown_until = i + windows.CLUSTER_COOLDOWN
            close_at(current, cooldown_until)
            early_breaks = [i]
            journal[i] = "reversal_event"
            continue

        if gate:
            journal[i] = "suppressed_by_cooldown"

    if current is not None:
        current.status = "open"

    return events, pd.DataFrame({"decision": journal}, index=frame.index)


def events_frame(events: list[ClusterEvent]) -> pd.DataFrame:
    columns = ["event_id", "t0_utc", "status", "si_total_t0", "base_points_t0",
               "parent_event_id", "escalation_seq"]
    if not events:
        return pd.DataFrame({c: pd.Series(dtype="object") for c in columns})
    return pd.DataFrame([{c: getattr(e, c) for c in columns} for e in events])


def escalations_frame(events: list[ClusterEvent]) -> pd.DataFrame:
    rows = [{"event_id": e.event_id, **escalation}
            for e in events for escalation in e.escalations]
    if not rows:
        return pd.DataFrame({c: pd.Series(dtype="object") for c in
                             ["event_id", "seq", "hour_utc", "kind", "si_total", "reason"]})
    return pd.DataFrame(rows)


# Columns this run itself appends to metrics_basket_hour. On a repeat run they
# are already in the file, and join would fail on the overlapping names.
DERIVED_COLUMNS = ("m_calendar", "m_vix", "si_total", "sigma_m", "k", "decision",
                   "base_points", "breadth_q99", "n_active_blocks",
                   "n_active_blocks_q99", "breadth_share", "saed_count_24h",
                   "saed_breadth_threshold")


def reset_derived(basket_frame: pd.DataFrame) -> pd.DataFrame:
    """Clears this run's own output from the previous run (§6.2).

    The basket-metrics file is both input and output here. The §6.2 requirement -
    a repeat run of the same hour must neither fail nor duplicate the result - is
    met by dropping the derived columns and recomputing them, not by someone
    remembering to run cross_section before cluster.
    """
    derived = [c for c in DERIVED_COLUMNS if c in basket_frame.columns]
    derived += [c for c in basket_frame.columns if c.startswith("trigger_")]
    # cross_section stamped the file when it wrote it; this run re-stamps it.
    derived += [c for c in versioning.VERSION_COLUMNS if c in basket_frame.columns]
    return basket_frame.drop(columns=derived)


def tag_overlap(saed_events: pd.DataFrame, events: list[ClusterEvent]) -> pd.DataFrame:
    """overlap_with_cluster for every SAED event (§8.2, §8.5).

    Per §8.2 a single-asset event that coincides with an active cluster event is
    not suppressed - it is only tagged. The tag can be set no earlier than here:
    SAED runs before the cluster automaton, so at the moment its events are built
    the cluster events of this run do not exist yet. Until then the field is NULL
    rather than False, which under §1.2 is the difference between "no overlap" and
    "not evaluated".

    An event is active from T0 until its cooldown expires or a reversal closes it.
    The comparison is in WALL-CLOCK hours on purpose, although the cooldown itself
    is counted in reference-calendar ones: a SAED event on a crypto asset can fall
    on a Saturday, when the reference calendar has no hours at all, and a cluster
    event opened on Friday is still active then. Its span is converted to wall
    clock once, here, and the weekend inside it is part of the span.
    """
    if saed_events.empty:
        return saed_events.assign(overlap_with_cluster=pd.Series(dtype="boolean"))

    hours = saed_events["hour_utc"].to_numpy()
    overlap = np.zeros(len(saed_events), dtype=bool)
    for event in events:
        end = event.cooldown_until_utc
        covered = hours >= event.t0_utc
        if end is not None:
            covered &= hours < end
        overlap |= covered
    return saed_events.assign(overlap_with_cluster=pd.array(overlap, dtype="boolean"))


def saed_breadth(saed_events: pd.DataFrame, hours: pd.Index,
                 horizon: int = windows.SUSTAINED_WINDOW,
                 window: int = windows.W_CS) -> tuple[pd.Series, pd.Series]:
    """How many single-asset events fired in the trailing `horizon` hours.

    §8.6 makes the dependency between the two modules one-way: SAED takes the
    basket factor as input and feeds nothing back. That kept them independent,
    and it was throwing away the better predictor - on train the SAED count alone
    reaches 53% precision at its 98th percentile and 83% at its 99th, where the
    cluster detector reaches 37%. The count is already computed; only the wiring
    was missing.

    No cycle is created. SAED depends on the basket factor from cross_section,
    not on anything cluster produces, so the run order pipeline -> cross_section
    -> saed -> cluster stands as it was.
    """
    if saed_events.empty:
        empty = pd.Series(np.nan, index=hours)
        return empty, empty
    per_hour = saed_events.groupby("hour_utc").size().reindex(hours).fillna(0)
    accumulated = per_hour.rolling(horizon, min_periods=horizon // 2).sum()
    threshold = (accumulated.shift(1).rolling(window, min_periods=window)
                 .quantile(windows.SAED_BREADTH_QUANTILE))
    return accumulated, threshold


DEFAULT_EVENTS_PATH = "data/tremor/cluster_events.parquet"
DEFAULT_ESCALATIONS_PATH = "data/tremor/cluster_event_escalations.parquet"


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging
    import os

    from tremor import (bars, calendar_multiplier, cross_section, journal, pipeline,
                       saed, vix)
    from tremor.basket import load_basket

    parser = argparse.ArgumentParser(description="SI-Index and cluster events (§4, §5)")
    parser.add_argument("--metrics-dir", default=pipeline.DEFAULT_METRICS_DIR)
    parser.add_argument("--basket-metrics", default=cross_section.DEFAULT_BASKET_METRICS_PATH)
    parser.add_argument("--events-out", default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--escalations-out", default=DEFAULT_ESCALATIONS_PATH)
    parser.add_argument("--saed-events", default=saed.DEFAULT_EVENTS_PATH)
    parser.add_argument("--journal-out", default=journal.DEFAULT_JOURNAL_PATH)
    parser.add_argument("--warmup-out", default=journal.DEFAULT_WARMUP_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("tremor.cluster")

    basket = load_basket()
    metrics = pipeline.load_all(basket, args.metrics_dir)
    basket_frame = pd.read_parquet(args.basket_metrics).set_index("hour_utc").sort_index()
    basket_frame = reset_derived(basket_frame)
    hours = basket_frame.index

    calendar = calendar_multiplier.multiplier_series(hours)
    scored = vix.score(vix.load_series(
        os.path.join(bars.DEFAULT_VIX_DIR, f"{basket.volatility_index.file_stem}.parquet")))
    vix_windows = vix.windows_from_spikes(scored, hours.to_numpy())
    vix_multiplier = vix.multiplier_series(hours, vix_windows)

    # The single-asset channel (§8.6 departure, deviation §24). Read before the
    # points are assembled, because it is one of the triggers now.
    saed_events = (pd.read_parquet(args.saed_events)
                   if os.path.exists(args.saed_events) else pd.DataFrame())
    saed_count, saed_threshold = saed_breadth(saed_events, hours)
    saed_hit = (saed_count > saed_threshold).where(saed_threshold.notna(), False)

    points = si_index.base_points(basket, metrics, basket_frame["single_factor"],
                                  hours, windows.VOLUME_CONFIRM,
                                  sustained=basket_frame.get("trigger_sustained"),
                                  saed_breadth=saed_hit)
    frame = basket_frame.drop(columns=["trigger_sustained"], errors="ignore").join(points)
    frame["saed_count_24h"] = saed_count
    frame["saed_breadth_threshold"] = saed_threshold
    frame["trigger_sustained"] = points["trigger_sustained"]
    frame["trigger_saed_breadth"] = points["trigger_saed_breadth"]
    frame["m_calendar"] = pd.Series(calendar).reindex(hours).fillna(1.0)
    frame["m_vix"] = pd.Series(vix_multiplier).reindex(hours).fillna(1.0)
    frame["si_total"] = si_index.si_total(frame["base_points"], frame["m_calendar"],
                                          frame["m_vix"])
    frame = frame.join(reversal_scale(frame["m_weighted_median"]))

    events, decisions = run(frame)
    frame = frame.join(decisions)

    # The fingerprint is taken over the RAW inputs (versioning.RAW_INPUTS), not
    # over the basket-metrics file: that file is both input and output for this
    # run, and including it would mean a new run_version on every repeat.
    config, run_id, fingerprint = versioning.stamps()

    # Cluster events carry the versions plus their provenance (§6.3, §6.4):
    # created_at survives a rerun, and recalculated says the row was rebuilt under
    # a run_version different from the one that first produced it.
    stamped = versioning.provenance(
        versioning.stamp(events_frame(events), config, run_id),
        versioning.previous_table(args.events_out), fingerprint)

    for path, data in ((args.events_out, stamped),
                       (args.escalations_out,
                        versioning.stamp(escalations_frame(events), config, run_id))):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data.to_parquet(path, index=False, compression="zstd")
    versioning.stamp(frame.reset_index(names="hour_utc"), config, run_id).to_parquet(
        args.basket_metrics, index=False, compression="zstd")

    # The overlap tag (§8.2) can only be set once the cluster events exist, so
    # this run finishes the table SAED left with the field still NULL.
    if os.path.exists(args.saed_events):
        tagged = versioning.stamp(
            tag_overlap(pd.read_parquet(args.saed_events).drop(
                columns=["overlap_with_cluster"], errors="ignore"), events),
            config, run_id)
        tagged.to_parquet(args.saed_events, index=False, compression="zstd")
        log.info("SAED events tagged overlap_with_cluster: %d of %d",
                 int(tagged["overlap_with_cluster"].fillna(False).sum()), len(tagged))
    else:
        log.warning("no SAED events table - overlap_with_cluster left unset; "
                    "run python -m tremor.saed first")

    # Decision journal (§6.1).
    # Residuals are read from disk if a SAED run has already happened: there is no
    # need to recompute the regressions just for the journal, and without them the
    # saed rows simply do not appear.
    residual_frames = saed.load_residuals(basket)
    if not residual_frames:
        log.warning("no residual series - the journal will have no SAED rows; "
                    "run python -m tremor.saed first")
    rows = pd.concat([
        journal.stamp(journal.asset_decisions(metrics, residual_frames), config, run_id),
        journal.stamp(journal.basket_decisions(frame), config, run_id),
    ], ignore_index=True)
    os.makedirs(os.path.dirname(args.journal_out) or ".", exist_ok=True)
    rows.to_parquet(args.journal_out, index=False, compression="zstd")
    journal.first_valid_hour(metrics, frame).to_parquet(args.warmup_out, index=False,
                                                        compression="zstd")
    log.info("decision journal: %d rows, config %s, run %s", len(rows), config, run_id)

    reversals = sum(1 for e in events if e.parent_event_id)
    log.info("cluster events %d (of them reversals %d), escalations %d",
             len(events), reversals, sum(e.escalation_seq for e in events))
    log.info("base points: median %.0f, max %d | SI_total: max %.1f",
             frame["base_points"].median(), int(frame["base_points"].max()),
             frame["si_total"].max())
    log.info("decisions by hour: %s",
             frame["decision"].value_counts().head(6).to_dict())
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

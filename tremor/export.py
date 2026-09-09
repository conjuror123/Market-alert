"""Export of a cluster event to JSON under a fixed schema (spec §6.5).

An event in the database is one row: an identifier, an hour, some points. Working
out from that row six months later what actually happened is impossible: the row
holds neither the behaviour of the assets around T0, nor whether there was
synchrony, nor which single-asset spikes happened nearby. The export reconstructs
that whole picture, and so it is not a report for a human but a machine-readable
slice under a fixed schema: truth labelling in phase 7 is computed from it, and
runs of different versions are compared against it.

The window is [T0 - 12, T0 + 12] in REFERENCE-CALENDAR hours, not calendar ones.
The difference is not cosmetic: a Friday-evening event measured in calendar hours
would pull the weekend into its right arm, where there are neither ETF bars nor
currency pairs, and half the window would be empty. Reference-calendar hours move
that arm to Sunday evening, where trading exists.

In real time the right half of the window has not happened yet. That is not an
error: the export is written after the fact, for analysis and labelling, and an
event with a truncated right arm is flagged truncated_right - so that the backtest
does not mistake an incomplete window for a calm market.
"""
from __future__ import annotations

import json
import logging
import os

import numpy as np
import pandas as pd

from tremor import versioning
from tremor.basket import Basket

log = logging.getLogger("tremor.export")

DEFAULT_EXPORT_DIR = os.path.join("data", "tremor", "events")
SCHEMA_PATH = os.path.join("schema", "event_export.schema.json")
SCHEMA_VERSION = "1.0"

# Arm of the window in reference-calendar hours (§6.5).
WINDOW_HOURS = 12

# Per-asset series that go into the export. r is the actual return of the hour,
# z its Z-score, z_resid the score of the residual on the factors, v_r the volume
# confirmation. Nothing more is taken into the schema on purpose: the winsorized
# series and the EWMA states are recoverable from metrics_asset_hour by hour and
# asset, and duplicating them in every event would quadruple the export for the
# sake of quantities nobody reads by eye.
ASSET_SERIES = ("r", "z", "z_resid", "v_r")

# Aggregate basket metrics for every hour of the window (§6.5).
BASKET_SERIES = ("csv_norm", "pc1_ratio", "mean_pairwise_corr", "m_weighted_median",
                 "si_total", "base_points", "n_assets", "quorum_ok")


def _clean(value):
    """NaN, NA and numpy scalars -> something json can actually write.

    json.dumps emits NaN as the literal NaN, which is not valid JSON and breaks
    any strict parser on the other side. A gap here is null.
    """
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    return value


def window_hours(hours: np.ndarray, t0_utc: int,
                 arm: int = WINDOW_HOURS) -> tuple[np.ndarray, bool, bool]:
    """Hours of the window around T0 and truncation flags for both edges.

    hours is an ordered array of reference-calendar hours (every hour the system
    computed anything for). The window is taken by POSITION in that array, not by
    time arithmetic: that is exactly how "12 reference-calendar hours" is defined
    in §2.2.
    """
    position = int(np.searchsorted(hours, t0_utc))
    if position >= len(hours) or hours[position] != t0_utc:
        return np.empty(0, dtype=hours.dtype), True, True
    left, right = position - arm, position + arm
    truncated_left, truncated_right = left < 0, right >= len(hours)
    return hours[max(left, 0):right + 1], truncated_left, truncated_right


def _series_for(frame: pd.DataFrame, hours: np.ndarray,
                columns: tuple[str, ...]) -> dict[str, list]:
    """Series over the hours of the window. An hour without data yields null
    rather than dropping out of the series: every series of an event has the
    length of the window, and the consumer never has to align them by index."""
    aligned = frame.reindex(hours)
    return {name: [_clean(v) for v in aligned[name]]
            for name in columns if name in aligned.columns}


def _asset_frame(metrics: pd.DataFrame,
                 residuals: pd.DataFrame | None) -> pd.DataFrame:
    frame = metrics.set_index("hour_utc")
    if residuals is not None and not residuals.empty:
        extra = residuals.set_index("hour_utc")
        for name in ("z_resid", "e_resid", "beta_block"):
            if name in extra.columns:
                frame[name] = extra[name]
    return frame


def build_event(event: pd.Series, basket_frame: pd.DataFrame,
                metrics: dict[str, pd.DataFrame],
                residuals: dict[str, pd.DataFrame],
                saed_events: pd.DataFrame,
                escalations: pd.DataFrame,
                config_version: str, run_version: str,
                basket: Basket | None = None) -> dict:
    """The complete slice of one cluster event."""
    hours = basket_frame.index.to_numpy()
    window, truncated_left, truncated_right = window_hours(hours, int(event["t0_utc"]))

    assets = {}
    for asset_id in sorted(metrics):
        frame = _asset_frame(metrics[asset_id], residuals.get(asset_id))
        series = _series_for(frame, window, ASSET_SERIES)
        if not any(v is not None for values in series.values() for v in values):
            # An instrument with no value anywhere in the window does not enter
            # the export: otherwise a night-time event would drag along twelve
            # ETFs with series of nothing but null.
            continue
        entry = {"block": frame["block"].dropna().iloc[0] if "block" in frame else None,
                 "series": series}
        if basket is not None:
            asset = next((a for a in basket.instruments if a.asset_id == asset_id), None)
            if asset is not None:
                entry["tier"] = asset.tier
                entry["in_basket"] = asset.in_basket
        assets[asset_id] = entry

    in_window = saed_events[saed_events["hour_utc"].isin(window)] if not saed_events.empty \
        else saed_events
    saed = [{"event_id": row["event_id"], "asset_id": row["asset_id"],
             "block": row["block"], "hour_utc": int(row["hour_utc"]),
             "z_resid": _clean(row["z_resid"]), "e_resid": _clean(row["e_resid"]),
             "r": _clean(row["r"]), "repeat_count": _clean(row["repeat_count"]),
             "tier": row["tier"] if "tier" in in_window and pd.notna(row["tier"])
                     else None,
             "basis": row["basis"] if "basis" in in_window and pd.notna(row["basis"])
                      else None}
            for _, row in in_window.iterrows()]

    own = escalations[escalations["event_id"] == event["event_id"]] \
        if not escalations.empty else escalations
    steps = [{"seq": int(row["seq"]), "hour_utc": int(row["hour_utc"]),
              "kind": row["kind"], "si_total": _clean(row["si_total"]),
              "reason": row["reason"]} for _, row in own.iterrows()]

    return {
        "schema_version": SCHEMA_VERSION,
        "config_version": config_version,
        "run_version": run_version,
        "event_id": event["event_id"],
        "t0_utc": int(event["t0_utc"]),
        "status": event["status"],
        "parent_event_id": _clean(event["parent_event_id"]),
        "si_total_t0": _clean(event["si_total_t0"]),
        "base_points_t0": _clean(event["base_points_t0"]),
        "window": {
            "arm_reference_hours": WINDOW_HOURS,
            "hours_utc": [int(h) for h in window],
            "truncated_left": truncated_left,
            "truncated_right": truncated_right,
        },
        "escalations": steps,
        "basket": _series_for(basket_frame, window, BASKET_SERIES),
        "assets": assets,
        "saed_events": saed,
    }


def write_event(payload: dict, out_dir: str = DEFAULT_EXPORT_DIR) -> str:
    """One event, one file. The name is built from event_id with the colon
    replaced by an underscore: Windows does not accept such a name, and the export
    has to be readable there too."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{payload['event_id'].replace(':', '_')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    return path


def prune_stale(out_dir: str, written: set[str]) -> list[str]:
    """Removes exports that this run did not write.

    write_event only ever adds files, so an event that stops existing - the
    calendar archive was rebuilt, a threshold moved - leaves its file behind
    looking exactly like current output, with only run_version inside telling
    the two apart. A consumer reading the directory would take that ghost for a
    live event. The directory is the whole export, so what the run did not
    produce does not belong in it.

    Only valid for a full export: with --since the run deliberately touches part
    of the history, and everything outside that part is not stale.
    """
    removed = []
    for name in sorted(os.listdir(out_dir)):
        path = os.path.join(out_dir, name)
        if name.endswith(".json") and path not in written:
            os.remove(path)
            removed.append(path)
    return removed


def build_all(events: pd.DataFrame, basket_frame: pd.DataFrame,
              metrics: dict[str, pd.DataFrame], residuals: dict[str, pd.DataFrame],
              saed_events: pd.DataFrame, escalations: pd.DataFrame,
              config_version: str, run_version: str,
              basket: Basket | None = None) -> list[dict]:
    return [build_event(row, basket_frame, metrics, residuals, saed_events,
                        escalations, config_version, run_version, basket)
            for _, row in events.iterrows()]


def main(argv: list[str] | None = None) -> int:
    import argparse

    from tremor import cluster, cross_section, pipeline, saed
    from tremor.basket import load_basket

    parser = argparse.ArgumentParser(description="Export of cluster events (§6.5)")
    parser.add_argument("--basket-metrics", default=cross_section.DEFAULT_BASKET_METRICS_PATH)
    parser.add_argument("--events", default=cluster.DEFAULT_EVENTS_PATH)
    parser.add_argument("--escalations", default=cluster.DEFAULT_ESCALATIONS_PATH)
    parser.add_argument("--saed-events", default=saed.DEFAULT_EVENTS_PATH)
    parser.add_argument("--metrics-dir", default=pipeline.DEFAULT_METRICS_DIR)
    parser.add_argument("--residuals-dir", default=saed.DEFAULT_RESIDUALS_DIR)
    parser.add_argument("--out-dir", default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--since", type=int, default=None,
                        help="export only events with t0_utc no earlier than this")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    basket = load_basket()
    metrics = pipeline.load_all(basket, args.metrics_dir)
    residuals = saed.load_residuals(basket, args.residuals_dir)
    basket_frame = pd.read_parquet(args.basket_metrics).set_index("hour_utc").sort_index()
    events = pd.read_parquet(args.events)
    escalations = pd.read_parquet(args.escalations)
    saed_events = pd.read_parquet(args.saed_events)
    if args.since is not None:
        events = events[events["t0_utc"] >= args.since]

    config, run = versioning.versions_for()

    written = set()
    for payload in build_all(events, basket_frame, metrics, residuals, saed_events,
                             escalations, config, run, basket):
        written.add(write_event(payload, args.out_dir))
    log.info("exported %d events to %s (config %s, run %s)",
             len(written), args.out_dir, config, run)

    if args.since is None:
        removed = prune_stale(args.out_dir, written)
        if removed:
            log.info("removed %d stale exports left by an earlier run: %s",
                     len(removed), ", ".join(os.path.basename(p) for p in removed))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

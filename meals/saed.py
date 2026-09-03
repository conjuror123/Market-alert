"""Single-asset event module, SAED (spec §8).

Catches moves that the common market does not explain. It runs alongside the
cluster detector, and the dependency between them is one-way: SAED takes the
basket factor as input but has no effect on the SI-Index, the cluster gate or the
cluster cooldown.

Three things that are easy to miss and that the spec addresses separately.

The cooldown is counted in THE ASSET'S OWN BARS, not in calendar hours. Twelve
bars are one and a half trading sessions for an ETF and half a day for crypto.
Otherwise an ETF with seven bars a day would stay silent for nearly two days
where a round-the-clock instrument recovers in twelve hours.

The pause does not cancel an event, it merges it into the current one: repeat
firings inside the cooldown increment repeat_count. These are different things -
"the move stopped" and "the move continues, but we have already reported it".

The notification goes out per BLOCK alert, not per asset. If three instruments of
one block jerked in the same hour, that is one observation about the block, not
three identical messages.

Versioning (config_version, run_version) and the overlap_with_cluster flag come
with phases 6 and 5 - before those there are neither configuration versions nor
cluster events to overlap with.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from meals import windows
from meals.basket import Asset, Basket


@dataclass(frozen=True)
class SaedEvent:
    event_id: str
    asset_id: str
    block: str
    hour_utc: int          # T0_single - the hour of the first firing
    z_resid: float
    e_resid: float
    r: float
    beta: float
    repeat_count: int


def triggers(frame: pd.DataFrame) -> pd.Series:
    """The event-generation condition of §8.2: hybrid, like everything in §3.1.

    An event is created regardless of volume confirmation or any other factor -
    that is the point of the module: a single-asset move is grounds in itself,
    even when volume is ordinary.
    """
    known = (frame["z_resid"].notna() & frame["q99_resid"].notna()
             & frame["sigma_lt_resid"].notna())
    hit = ((frame["z_resid"].abs() > frame["q99_resid"])
           & (frame["e_resid"].abs()
              >= windows.ABS_LEG_Q99 * frame["sigma_lt_resid"]))
    return hit.where(known, pd.NA).astype("boolean")


def build_events(asset: Asset, frame: pd.DataFrame,
                 cooldown_bars: int = windows.SAED_COOLDOWN_BARS) -> list[SaedEvent]:
    """Runs the cooldown automaton over the asset's bars (§8.3).

    The sequential pass is layer B of §6.1: whether a firing joins the current
    event or opens a new one depends on how many bars have passed since the
    previous one began, and that is a path-dependent decision.
    """
    fired = triggers(frame).fillna(False).to_numpy(dtype=bool)
    if not fired.any():
        return []

    hours = frame["hour_utc"].to_numpy()
    z = frame["z_resid"].to_numpy()
    e = frame["e_resid"].to_numpy()
    r = frame["r"].to_numpy()
    beta = frame["beta"].to_numpy() if "beta" in frame else np.full(len(frame), np.nan)

    events: list[SaedEvent] = []
    counts: list[int] = []
    open_at: int | None = None   # index of the bar on which the current event opened

    for i in np.flatnonzero(fired):
        if open_at is not None and i - open_at < cooldown_bars:
            # Inside the pause: the same event continues, no notification.
            counts[-1] += 1
            continue
        open_at = i
        events.append(SaedEvent(
            event_id=f"{asset.file_stem}:{int(hours[i])}",
            asset_id=asset.asset_id, block=asset.block, hour_utc=int(hours[i]),
            z_resid=float(z[i]), e_resid=float(e[i]), r=float(r[i]),
            beta=float(beta[i]), repeat_count=0,
        ))
        counts.append(0)

    return [SaedEvent(**{**event.__dict__, "repeat_count": count})
            for event, count in zip(events, counts)]


def events_frame(events: list[SaedEvent]) -> pd.DataFrame:
    columns = ["event_id", "asset_id", "block", "hour_utc", "z_resid", "e_resid",
               "r", "beta", "repeat_count"]
    if not events:
        return pd.DataFrame({c: pd.Series(dtype="object" if c in
                                          ("event_id", "asset_id", "block")
                                          else "float64") for c in columns})
    return pd.DataFrame([e.__dict__ for e in events])[columns]


def aggregate_block_alerts(events: pd.DataFrame) -> pd.DataFrame:
    """Block aggregation per §8.4: simultaneous events of assets in one block
    combine into a single alert.

    Basket and non-basket instruments of the same block aggregate together - for
    the recipient this is one observation about the block, and splitting it by
    "does the instrument count towards quorum" would be splitting on an unrelated
    criterion.
    """
    if events.empty:
        return pd.DataFrame({"alert_id": [], "block": [], "hour_utc": [],
                             "assets": [], "max_abs_z_resid": [], "n_assets": []})

    grouped = events.groupby(["block", "hour_utc"], sort=True)
    alerts = grouped.agg(
        assets=("asset_id", lambda s: ",".join(sorted(s))),
        max_abs_z_resid=("z_resid", lambda s: float(s.abs().max())),
        n_assets=("asset_id", "nunique"),
    ).reset_index()
    alerts["alert_id"] = alerts["block"] + ":" + alerts["hour_utc"].astype(str)
    return alerts[["alert_id", "block", "hour_utc", "assets", "max_abs_z_resid",
                   "n_assets"]]


def link_alerts(events: pd.DataFrame, alerts: pd.DataFrame) -> pd.DataFrame:
    """Attaches the block-alert reference to each event (aggregate_alert_id, §8.5)."""
    if events.empty:
        return events.assign(aggregate_alert_id=pd.Series(dtype="object"))
    keys = alerts.set_index(["block", "hour_utc"])["alert_id"]
    index = pd.MultiIndex.from_frame(events[["block", "hour_utc"]])
    return events.assign(aggregate_alert_id=keys.reindex(index).to_numpy())


DEFAULT_EVENTS_PATH = "data/meals/saed_events.parquet"
DEFAULT_ALERTS_PATH = "data/meals/saed_block_alerts.parquet"
DEFAULT_RESIDUALS_DIR = "data/meals/residuals"

# Residual series that are written to disk. They can be recomputed from the
# metrics, but that means a full run of the regressions over the whole history -
# minutes instead of seconds - and both the decision journal (§6.1) and the event
# export (§6.5) need them.
#
# Intermediate states are not stored: the winsorized residual and sigma_eff are
# recovered unambiguously from e_resid and sigma_LT by the same §2.5 machinery,
# yet they take as much space as everything else put together - they are series
# of random numbers, and nothing compresses them.
RESIDUAL_COLUMNS = ("hour_utc", "asset_id", "beta", "beta_block", "e_resid",
                    "sigma_lt_resid", "z_resid", "q95_resid", "q99_resid")


def save_residuals(scored: dict[str, pd.DataFrame],
                   out_dir: str = DEFAULT_RESIDUALS_DIR) -> None:
    import os

    from meals.basket import load_basket

    os.makedirs(out_dir, exist_ok=True)
    stems = {a.asset_id: a.file_stem for a in load_basket().instruments}
    for asset_id, frame in scored.items():
        columns = [c for c in RESIDUAL_COLUMNS if c in frame.columns]
        frame[columns].to_parquet(os.path.join(out_dir, f"{stems[asset_id]}.parquet"),
                                  index=False, compression="zstd")


def load_residuals(basket: Basket,
                   out_dir: str = DEFAULT_RESIDUALS_DIR) -> dict[str, pd.DataFrame]:
    import os

    out = {}
    for asset in basket.instruments:
        path = os.path.join(out_dir, f"{asset.file_stem}.parquet")
        if os.path.exists(path):
            out[asset.asset_id] = pd.read_parquet(path)
    return out


def build_for_basket(basket: Basket, metrics: dict[str, pd.DataFrame],
                     factor: pd.Series,
                     block_factors: pd.DataFrame | None = None
                     ) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.DataFrame]]:
    """Computes residuals and events for every instrument, non-basket ones included.

    Non-basket instruments use the same basket factor and the same beta-estimation
    scheme (§8.1): they do not affect the factor, but they are explained by it just
    like the rest.
    """
    from meals import pipeline, residuals, windows as w

    all_events: list[SaedEvent] = []
    scored: dict[str, pd.DataFrame] = {}
    for asset in basket.instruments:
        frame = metrics.get(asset.asset_id)
        if frame is None or frame.empty:
            continue
        own_block = (block_factors[asset.asset_id]
                     if block_factors is not None and asset.asset_id in block_factors
                     else None)
        with_residuals = residuals.residuals(asset, frame, factor, own_block)
        b_asset = pipeline.bars_per_session(asset, frame, basket.anchor_exchange_tz)
        result = residuals.score_residuals(with_residuals, w.w_asset(b_asset))
        scored[asset.asset_id] = result
        all_events.extend(build_events(asset, result))

    events = events_frame(all_events)
    alerts = aggregate_block_alerts(events)
    return link_alerts(events, alerts), alerts, scored


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging
    import os

    from meals import cross_section, pipeline, sessions
    from meals.basket import load_basket

    parser = argparse.ArgumentParser(description="SAED events (§3.6, §8)")
    parser.add_argument("--metrics-dir", default=pipeline.DEFAULT_METRICS_DIR)
    parser.add_argument("--basket-metrics", default=cross_section.DEFAULT_BASKET_METRICS_PATH)
    parser.add_argument("--events-out", default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--alerts-out", default=DEFAULT_ALERTS_PATH)
    parser.add_argument("--residuals-out", default=DEFAULT_RESIDUALS_DIR)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("meals.saed")

    basket = load_basket()
    metrics = pipeline.load_all(basket, args.metrics_dir)
    if not metrics:
        log.error("No per-asset metrics - run python -m meals.pipeline first")
        return 2
    if not os.path.exists(args.basket_metrics):
        log.error("No basket metrics - run python -m meals.cross_section first")
        return 2

    basket_frame = pd.read_parquet(args.basket_metrics).set_index("hour_utc")
    factor = basket_frame["m_weighted_median"]

    panel = cross_section.build_panel(metrics, "r")
    reference = [h for h in panel.index
                 if sessions.is_reference_hour(int(h), basket.anchor_exchange_tz)]
    block_factors = cross_section.block_factors(panel.loc[reference], basket)

    events, alerts, scored = build_for_basket(basket, metrics, factor, block_factors)
    for path, frame in ((args.events_out, events), (args.alerts_out, alerts)):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        frame.to_parquet(path, index=False, compression="zstd")
    save_residuals(scored, args.residuals_out)

    log.info("events %d, block alerts %d, repeats inside pauses %d",
             len(events), len(alerts),
             int(events["repeat_count"].sum()) if not events.empty else 0)
    if not alerts.empty:
        log.info("alerts by block: %s",
                 alerts["block"].value_counts().to_dict())
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

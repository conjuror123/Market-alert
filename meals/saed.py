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

Versioning (config_version, run_version) is stamped here, on the table this
module writes. The overlap_with_cluster flag is not: it is a fact about the
cluster system, and this module runs before it, so the field leaves here as NULL
- not evaluated, per §1.2 - and cluster.tag_overlap fills it in straight after.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from meals import persistence, quality, routing, severity, windows
from meals.basket import Asset, Basket


# The two questions worth asking of one instrument's hour, and the column each
# is asked of. "Was this explained by the market" and "was this a big move" are
# different events, and a detector that asks only the first is blind to exactly
# the days when everything moves together - which is what a macro event is.
#
# Measured on this basket, that blindness was total: on the SVB collapse, the
# August 2024 yen unwind and the 2024 US election, the abnormal channel pushed
# nothing at all. The absolute channel finds 11, 17 and 9 events on those days.
# It is also the only one of the two that breathes with the market: month to
# month the abnormal channel varies 1.8x and the absolute one 33x, because the
# abnormal score has the market's volatility divided out of it twice - once by
# the instrument's own rolling sigma and once by the peer spread (BMP).
#
# The raw return is used rather than the winsorized one on purpose: winsorizing
# caps the series at 0.096 where the raw reaches 0.199, which is precisely the
# population this channel exists to find.
TIER_SOURCES = {"abnormal": "tier_abnormal", "absolute": "tier_absolute"}
ABSOLUTE_COLUMN = "r"
ABSOLUTE_LEVEL_PREFIX = "abs_level"


@dataclass(frozen=True)
class SaedEvent:
    event_id: str
    asset_id: str
    block: str
    hour_utc: int          # T0_single - the hour of the first firing
    peak_hour_utc: int     # the hour the event reached the tier it is reported at
    # Whether the non-parametric rank test agrees that the bar is extreme. A
    # separate axis from `basis`, which says which channel CLAIMED the event:
    # this says whether a test sharing none of their assumptions concurs.
    rank_confirms: "bool | None"
    # Whether the residual mean-reverts fast enough for the move to be read as
    # idiosyncratic rather than as a factor the model does not have.
    ou_reverts: "bool | None"
    z_resid: float
    e_resid: float
    r: float
    beta: float
    repeat_count: int
    tier: str
    basis: str


def _flag(value) -> "bool | None":
    """pandas boolean NA is not False - an hour whose rank window has not filled
    has not disagreed, it has not spoken."""
    return None if value is None or value is pd.NA else bool(value)


def triggers(frame: pd.DataFrame) -> pd.Series:
    """The event-generation condition: the move cleared its own routine return level.

    This replaces a single critical value shared by every instrument. The
    critical value answered "is this distinguishable from noise", which is a
    question about the null hypothesis and not about the recipient - it fired
    2.1 times a week and every event it produced looked alike. The condition
    now is that the move is at least a once-a-fortnight event FOR THIS
    INSTRUMENT, and what comes out with it is how rare it actually was, which
    is what decides whether the message interrupts anyone (see meals.severity).

    There is still no second filter on raw magnitude, volume or anything else.
    That was true of the critical-value test for the reason the event-study
    literature gives - the standardisation is the test - and it stays true
    here: a single-asset move is grounds in itself, and how much it matters is
    now carried by the tier rather than decided at the door.
    """
    if "tier" not in frame:
        raise KeyError("severity.annotate must run before triggers")

    # Assessed if EITHER ladder was fitted and had something to read. One
    # channel still warming up does not make the hour unassessed - the other
    # one answered - and only an hour where neither could speak is NULL.
    assessed = pd.Series(False, index=frame.index)
    for column, prefix in ((("z_resid_bmp" if "z_resid_bmp" in frame else "z_resid"),
                            severity.LEVEL_PREFIX),
                           (ABSOLUTE_COLUMN, ABSOLUTE_LEVEL_PREFIX)):
        first = severity.level_columns(prefix)[0]
        if column in frame and first in frame:
            assessed |= frame[column].notna() & frame[first].notna()
    return frame["tier"].notna().where(assessed, pd.NA).astype("boolean")


def build_events(asset: Asset, frame: pd.DataFrame,
                 cooldown_bars: int = windows.SAED_COOLDOWN_BARS) -> list[SaedEvent]:
    """Runs the cooldown automaton over the asset's bars (§8.3).

    The sequential pass is layer B of §6.1: whether a firing joins the current
    event or opens a new one depends on how many bars have passed since the
    previous one began, and that is a path-dependent decision.
    """
    fired = triggers(frame).fillna(False).to_numpy(dtype=bool)
    # An hour whose price moved less than the instrument can resolve is not a
    # small event, it is an unobserved one - see quality.resolvable. Applied
    # here rather than inside triggers() because it needs the instrument's tick
    # size, and triggers() deliberately reads nothing but the frame.
    if "close" in frame and getattr(asset, "tick_size", 0):
        fired &= quality.resolvable(frame["close"].to_numpy(),
                                    frame["r"].to_numpy(), asset.tick_size)
    if not fired.any():
        return []

    hours = frame["hour_utc"].to_numpy()
    z = frame["z_resid"].to_numpy()
    e = frame["e_resid"].to_numpy()
    r = frame["r"].to_numpy()
    beta = frame["beta"].to_numpy() if "beta" in frame else np.full(len(frame), np.nan)
    tier = frame["tier"].to_numpy(dtype=object)
    confirms = (frame["rank_confirms"].to_numpy(dtype=object)
                if "rank_confirms" in frame else np.full(len(frame), None))
    reverts = (frame["ou_reverts"].to_numpy(dtype=object)
               if "ou_reverts" in frame else np.full(len(frame), None))
    basis = frame["basis"].to_numpy(dtype=object) if "basis" in frame \
        else np.full(len(frame), "abnormal", dtype=object)
    rank = {name: i for i, name in enumerate(severity.TIERS)}

    events: list[SaedEvent] = []
    counts: list[int] = []
    open_at: int | None = None   # index of the bar on which the current event opened

    for i in np.flatnonzero(fired):
        if open_at is not None and i - open_at < cooldown_bars:
            # Inside the pause: the same event continues, no notification - but
            # it can still get worse. A move that opens at the routine level and
            # reaches the major one an hour later is a major event; reporting
            # the tier it happened to start at would understate it purely
            # because of when the automaton opened. So the event keeps the
            # highest tier it reached, and only the notification is suppressed.
            counts[-1] += 1
            if rank.get(tier[i], -1) > rank.get(events[-1].tier, -1):
                # The whole bar moves with the tier, not the tier alone. The
                # tier is earned by THIS hour's move, so reporting it beside the
                # opening hour's magnitude describes two different bars as one
                # event - and the delivery layer prints that magnitude next to
                # the tier's own words. It produced pushes reading "biggest move
                # in 3 years, +0.01%": a fifth of a basis point on SHY, opened
                # at the routine level at 15:00, with the extreme belonging to
                # the +0.13% at 17:00. Identity stays at the opening hour, so
                # event_id and the cooldown are untouched; the description
                # follows the bar that earned the label.
                events[-1] = SaedEvent(**{**events[-1].__dict__,
                                          "tier": tier[i], "basis": str(basis[i]),
                                          "peak_hour_utc": int(hours[i]),
                                          "rank_confirms": _flag(confirms[i]),
                                          "ou_reverts": _flag(reverts[i]),
                                          "z_resid": float(z[i]),
                                          "e_resid": float(e[i]),
                                          "r": float(r[i]),
                                          "beta": float(beta[i])})
            continue
        open_at = i
        events.append(SaedEvent(
            event_id=f"{asset.file_stem}:{int(hours[i])}",
            asset_id=asset.asset_id, block=asset.block, hour_utc=int(hours[i]),
            peak_hour_utc=int(hours[i]), rank_confirms=_flag(confirms[i]),
            ou_reverts=_flag(reverts[i]),
            z_resid=float(z[i]), e_resid=float(e[i]), r=float(r[i]),
            beta=float(beta[i]), repeat_count=0, tier=str(tier[i]),
            basis=str(basis[i]),
        ))
        counts.append(0)

    return [SaedEvent(**{**event.__dict__, "repeat_count": count})
            for event, count in zip(events, counts)]


def events_frame(events: list[SaedEvent]) -> pd.DataFrame:
    columns = ["event_id", "asset_id", "block", "hour_utc", "peak_hour_utc",
               "z_resid", "e_resid", "r", "beta", "repeat_count", "tier", "basis",
               "rank_confirms", "ou_reverts"]
    if not events:
        return pd.DataFrame({c: pd.Series(dtype="object" if c in
                                          ("event_id", "asset_id", "block",
                                           "tier", "basis")
                                          else "float64") for c in columns})
    return pd.DataFrame([e.__dict__ for e in events])[columns]


def unevaluated_overlap(events: pd.DataFrame) -> pd.DataFrame:
    """overlap_with_cluster as NULL, not False (§8.5, §1.2).

    The field is a fact about the cluster system, and this module runs before it:
    the cluster events of this run do not exist yet. NULL says exactly that -
    not evaluated - where False would claim there was no active cluster event.
    cluster.tag_overlap fills it in immediately afterwards.
    """
    return events.assign(overlap_with_cluster=pd.array([pd.NA] * len(events),
                                                       dtype="boolean"))


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
                             "assets": [], "max_abs_z_resid": [], "n_assets": [],
                             "tier": [], "channel": []})

    grouped = events.groupby(["block", "hour_utc"], sort=True)
    order = {name: i for i, name in enumerate(severity.TIERS)}
    alerts = grouped.agg(
        assets=("asset_id", lambda s: ",".join(sorted(s))),
        max_abs_z_resid=("z_resid", lambda s: float(s.abs().max())),
        n_assets=("asset_id", "nunique"),
        # The block alert is delivered at the severity of its worst member. A
        # block carrying one major move and three routine ones is a major
        # alert; averaging or taking the first would bury the reason it is
        # being sent at all.
        tier=("tier", lambda s: max(s, key=lambda t: order.get(t, -1))),
    ).reset_index()
    if "channel" in events:
        # The block is delivered on its most urgent member's channel, for the
        # same reason it carries its worst member's tier: one instrument's
        # once-in-three-years move does not become a digest line because the
        # two that moved with it were ordinary.
        urgency = {routing.PUSH: 2, routing.DIGEST: 1, routing.DROPPED: 0}
        alerts = alerts.merge(
            grouped["channel"].agg(lambda s: max(s, key=lambda c: urgency.get(c, -1)))
            .reset_index(), on=["block", "hour_utc"], how="left")
    alerts["alert_id"] = alerts["block"] + ":" + alerts["hour_utc"].astype(str)
    columns = ["alert_id", "block", "hour_utc", "assets", "max_abs_z_resid",
               "n_assets", "tier"]
    return alerts[columns + [c for c in ("channel",) if c in alerts]]


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
                    "sigma_lt_resid", "patell_scale", "t_rank", "rank_pct",
                    "rank_confirms", "ou_reversion_bars", "s_score", "ou_reverts",
                    "z_resid", "bmp_scale", "bmp_dof",
                    "t_resid", "z_resid_bmp",
                    "q95_resid", "q99_resid", "tier", "basis", "tier_abnormal",
                    "tier_absolute") + severity.LEVEL_COLUMNS \
                  + severity.level_columns(ABSOLUTE_LEVEL_PREFIX) \
                  + persistence.RETENTION_COLUMNS


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

    scored: dict[str, pd.DataFrame] = {}
    windows_by_asset: dict[str, int] = {}
    for asset in basket.instruments:
        frame = metrics.get(asset.asset_id)
        if frame is None or frame.empty:
            continue
        own_block = (block_factors[asset.asset_id]
                     if block_factors is not None and asset.asset_id in block_factors
                     else None)
        with_residuals = residuals.residuals(asset, frame, factor, own_block)
        b_asset = pipeline.bars_per_session(asset, frame, basket.anchor_exchange_tz)
        windows_by_asset[asset.asset_id] = w.w_asset(b_asset)
        scored[asset.asset_id] = residuals.score_residuals(
            with_residuals, windows_by_asset[asset.asset_id])

    # The cross-sectional pass needs every asset's Z at once, so it can only run
    # after the loop - and the thresholds have to be recomputed on the score the
    # trigger will actually read, or a standardised score would be compared
    # against an unstandardised yardstick.
    scored = residuals.standardise_cross_section(scored)
    scored = {aid: residuals.rescore_thresholds(frame, windows_by_asset[aid])
              for aid, frame in scored.items()}

    # Severity is fitted per instrument on its own standardised score, after the
    # cross-sectional pass because that is the score the trigger reads. It is
    # deliberately NOT pooled across the basket: the whole point of a return
    # period is that it is the instrument's own history that says what is rare
    # for it, and pooling would put SHY and SOL back on one yardstick.
    scored = {aid: severity.annotate(frame, tier_column=TIER_SOURCES["abnormal"])
              for aid, frame in scored.items()}
    scored = {aid: severity.annotate(frame, column=ABSOLUTE_COLUMN,
                                     prefix=ABSOLUTE_LEVEL_PREFIX,
                                     tier_column=TIER_SOURCES["absolute"],
                                     fallback=None)
              for aid, frame in scored.items()}
    scored = {aid: severity.combine(frame, TIER_SOURCES)
              for aid, frame in scored.items()}
    scored = {aid: persistence.annotate(frame) for aid, frame in scored.items()}

    all_events: list[SaedEvent] = []
    for asset in basket.instruments:
        if asset.asset_id in scored:
            all_events.extend(build_events(asset, scored[asset.asset_id]))

    events = routing.route(persistence.attach(events_frame(all_events), scored))
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

    from meals import versioning

    config, run_id = versioning.versions_for()
    events = versioning.stamp(unevaluated_overlap(events), config, run_id)
    for path, frame in ((args.events_out, events),
                        (args.alerts_out, versioning.stamp(alerts, config, run_id))):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        frame.to_parquet(path, index=False, compression="zstd")
    save_residuals(scored, args.residuals_out)

    log.info("events %d, block alerts %d, repeats inside pauses %d",
             len(events), len(alerts),
             int(events["repeat_count"].sum()) if not events.empty else 0)
    if not alerts.empty:
        log.info("alerts by block: %s",
                 alerts["block"].value_counts().to_dict())
    if not events.empty:
        span = (events["hour_utc"].max() - events["hour_utc"].min()) / (3600 * 24 * 365.25)
        counts = events["tier"].value_counts()
        log.info("events by tier: %s", {t: int(counts.get(t, 0))
                                        for t in severity.TIERS})
        if span > 0:
            log.info("per year: %s", {t: round(int(counts.get(t, 0)) / span, 2)
                                      for t in severity.TIERS})
        by_channel = events["channel"].value_counts()
        log.info("by basis: %s", events["basis"].value_counts().to_dict())
        log.info("by channel: %s", {c: int(by_channel.get(c, 0)) for c in
                                    (routing.PUSH, routing.DIGEST, routing.DROPPED)})
        if span > 0 and by_channel.get(routing.PUSH, 0):
            log.info("a push every %.0f days, %.1f items per digest",
                     365.25 / (by_channel[routing.PUSH] / span),
                     by_channel.get(routing.DIGEST, 0) / (span * 104))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

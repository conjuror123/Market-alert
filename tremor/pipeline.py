"""Assembly of per-asset metrics (spec §6.1, layer A + layer B).

Runs one instrument through the whole phase 1-2 chain: quality gate -> return
channels -> winsorization -> EWMA Z-score and adaptive thresholds. The result
goes into metrics_asset_hour (§6.4).

THE HOURLY RUN EXTENDS RATHER THAN RECOMPUTES. It used to do the latter: one new
bar arrived and all 145,000 were put through the chain again, 249 MB of parquet
rewritten to add an hour. Every window here is bounded - the longest is sigma_lt
at windows.SIGMA_LT_BARS, with the EWMA under it converging inside four w_asset -
so windows.warm_bars of lead-in is enough for the new rows to come out exactly as
a full run would have them. Measured on SPY, EUR/USD and BTC-USD the largest
relative difference in any column was 1.8e-14, which is float64 accumulation
order rather than a disagreement, and the chain runs 5 to 12 times faster.

See extend_asset_metrics for when that is NOT safe and the whole thing is rebuilt
instead - a changed configuration, a store that does not reach back far enough,
or `--full` asked for by hand. build_all, the serial path, always computes
everything: it is what the tests and in-process callers want, and it is the
reference the extension is checked against.
"""
from __future__ import annotations

import logging
import os
from datetime import date

import pandas as pd

from tremor import bars, corporate_actions, quality, returns, sessions, windows, zscore
from tremor.basket import Asset, Basket, load_basket

log = logging.getLogger("tremor.pipeline")

DEFAULT_METRICS_DIR = os.path.join("data", "tremor", "metrics")

# Exchange timezone by session template. bars_per_session counts a trading day
# in the instrument's own zone, not the basket's New York anchor.
TEMPLATE_TZ = {
    "us_equity": "America/New_York",
    "fx_continuous": "America/New_York",
    "crypto_24_7": "UTC",
}


def bars_per_session(asset: Asset, usable: pd.DataFrame,
                     anchor_tz: str = "America/New_York") -> float:
    """B_asset from §2.7: the median number of valid bars in the asset's TRADING DAY.

    It is measured, not declared: W_asset, the adaptive-threshold window, is
    computed from it, and an error here would mean a window of the wrong length
    for every threshold at once.

    Counted over the exchange's calendar days, NOT over the continuous sessions of
    returns.session_ids. These are two different notions, and confusing them is
    expensive. For the gap channel a session is a stretch of uninterrupted
    trading: for a currency pair a whole week, for round-the-clock crypto the
    entire history in one piece. Substitute that length here and crypto gets a
    B_asset near fifty thousand and a threshold window of six million bars - it
    never fills, the thresholds stay undefined, and no breaches occur at all.
    Exactly that happened: zero breaches across 49632 Bitcoin bars.

    What is needed here is a day: 120 * B_asset means "one hundred and twenty
    trading days", which is 7 bars a day for an ETF and 24 for a currency pair or
    crypto.
    """
    if usable.empty:
        return 0.0
    tz = TEMPLATE_TZ[asset.session_template]
    local_days = pd.to_datetime(usable["hour_utc"], unit="s", utc=True).dt.tz_convert(
        tz).dt.date
    return float(local_days.value_counts().median())


def build_asset_metrics(asset: Asset, basket: Basket, frame: pd.DataFrame,
                        session_table: dict[date, sessions.Session],
                        action_days: set[date] | None) -> pd.DataFrame:
    """The full metric chain for one instrument."""
    gated = quality.apply_gate(asset, frame, session_table, basket.anchor_exchange_tz)
    usable = gated[gated["is_usable"]].reset_index(drop=True)
    if usable.empty:
        return usable

    channels = returns.split_channels(asset, usable, action_days,
                                      basket.anchor_exchange_tz)
    winsorised = returns.winsorize(asset, channels)

    b_asset = bars_per_session(asset, usable, basket.anchor_exchange_tz)
    scored = zscore.compute(winsorised, windows.w_asset(b_asset))

    scored["asset_id"] = asset.asset_id
    scored["block"] = asset.block
    scored["tier"] = asset.tier
    return scored


METRIC_COLUMNS = [
    "hour_utc", "asset_id", "block", "tier", "close", "volume",
    "r", "r_gap", "r_w", "gap_masked", "is_session_open",
    "sigma_lt", "mad_eff", "z", "sigma_eff", "q95", "q99",
    "breach_q95", "breach_q99",
]


def metrics_path(base_dir: str, file_stem: str) -> str:
    return os.path.join(base_dir, f"{file_stem}.parquet")


def extend_asset_metrics(asset: Asset, basket: Basket, frame: pd.DataFrame,
                         session_table: dict[date, sessions.Session],
                         action_days: "set[date] | None",
                         stored: pd.DataFrame,
                         config_version: str) -> "pd.DataFrame | None":
    """The stored metrics with the new bars computed onto the end, or None.

    None means "this cannot be extended, compute the whole thing" - an empty
    store, a store from a different configuration, or bars that do not reach the
    window. Every caller must handle it, because a wrong extension is silent.

    WHY THIS IS EXACT rather than merely close. Every window in the chain is
    bounded: the longest is sigma_lt at windows.SIGMA_LT_BARS, and the EWMA
    underneath it converges inside four w_asset. windows.warm_bars is the sum,
    and it is the same function saed already uses to decide how far back its own
    run must reach. Recomputing the last warm_bars bars and keeping only what is
    newer than the store therefore reproduces a full run - measured on SPY,
    EUR/USD and BTC-USD, the largest relative difference in any column was
    1.8e-14, which is float64 accumulation order and not a disagreement.

    WHAT IT CANNOT SEE is a revision to a bar older than the window. The
    provider does correct history occasionally, and a correction to a bar from
    three years ago would leave the stored metrics saying what they said. That
    is what config_version guards half of - a change to the CALCULATION forces a
    cold build - and what `--full` is for on the other half. Anything longer
    than the window is a scheduled rebuild's job, not an hourly run's.
    """
    if stored is None or stored.empty or "hour_utc" not in stored:
        return None
    # A different calculation is not an extension of this one. Nothing else in
    # the store says the code changed, so this is the whole guard.
    if "config_version" in stored:
        seen = stored["config_version"].dropna().unique()
        if len(seen) != 1 or str(seen[0]) != str(config_version):
            return None

    newest = int(stored["hour_utc"].max())
    fresh = frame[frame["hour_utc"] > newest]
    if fresh.empty:
        return stored          # nothing new; the store already is the answer

    bars_per = bars_per_session(asset, frame, basket.anchor_exchange_tz)
    window = windows.warm_bars(windows.w_asset(bars_per))
    lead = frame[frame["hour_utc"] <= newest].tail(window)
    if len(lead) < window:
        return None            # not enough history behind the new bars to be exact

    recomputed = build_asset_metrics(asset, basket,
                                     pd.concat([lead, fresh], ignore_index=True),
                                     session_table, action_days)
    if recomputed.empty:
        return stored
    added = recomputed[recomputed["hour_utc"] > newest]
    if added.empty:
        return stored
    keep = [c for c in stored.columns if c in added.columns]
    return pd.concat([stored, added[keep]], ignore_index=True)


def build_all(basket: Basket, bars_dir: str = bars.DEFAULT_BARS_DIR,
              metrics_dir: str = DEFAULT_METRICS_DIR,
              versions: tuple[str, str] | None = None) -> dict[str, pd.DataFrame]:
    from tremor import versioning

    session_table = sessions.load_sessions()
    actions = corporate_actions.load_actions()
    os.makedirs(metrics_dir, exist_ok=True)

    # §6.3 requires the versions in the metrics too, not only in the events. The
    # stamp goes on what is WRITTEN, not on what is returned: downstream modules
    # get the frame in memory and take their own stamp at their own write.
    config, run = versioning.versions_for() if versions is None else versions

    result = {}
    for asset in basket.instruments:
        frame = bars.load(bars.store_path(bars_dir, asset.file_stem))
        metrics = build_asset_metrics(asset, basket, frame, session_table,
                                      actions.get(asset.ticker))
        if metrics.empty:
            log.warning("%s: no usable bars", asset.asset_id)
            continue
        stored = metrics[[c for c in METRIC_COLUMNS if c in metrics]]
        versioning.stamp(stored, config, run).to_parquet(
            metrics_path(metrics_dir, asset.file_stem), index=False, compression="zstd")
        result[asset.asset_id] = metrics
        log.info("%s: bars %d, Q95 breaches %s, Q99 %s", asset.asset_id, len(metrics),
                 int(metrics["breach_q95"].sum()), int(metrics["breach_q99"].sum()))
    return result


# --- the same thing, across the machine's cores ------------------------------
#
# The instruments are independent: nothing in the metric chain for EUR/USD looks
# at SPY. The cross-sectional work that does comes later, in cross_section, and
# reads these files off disk. So the loop above is embarrassingly parallel, and
# the only reason it was not was that it started life with twelve instruments
# and a few seconds.
#
# Written as a pool over PROCESSES rather than threads because the work is
# numpy and pandas holding the GIL for most of it. The workers return summaries
# only, never frames: a scored frame is 145,000 rows by forty columns, and
# pickling twenty-three of them back to the parent would cost more than the
# computation saved. They write their own parquet, which the loop above already
# did, and whoever needs the numbers reads them back with load_all.
_POOL_STATE: dict = {}


def _pool_init(bars_dir: str, metrics_dir: str, versions: tuple[str, str],
               full: bool = False) -> None:
    """Loaded once per worker, not once per instrument.

    The session table is six thousand rows and the corporate-action table a few
    hundred; sending either through the task queue for every instrument would
    hand back most of what the pool is for.
    """
    _POOL_STATE.update(
        bars_dir=bars_dir, metrics_dir=metrics_dir, versions=versions, full=full,
        session_table=sessions.load_sessions(),
        actions=corporate_actions.load_actions(),
    )


def _pool_one(payload: "tuple[Asset, Basket]") -> "tuple[str, int, int, int, bool] | None":
    asset, basket = payload
    from tremor import versioning

    config, run = _POOL_STATE["versions"]
    path = metrics_path(_POOL_STATE["metrics_dir"], asset.file_stem)
    frame = bars.load(bars.store_path(_POOL_STATE["bars_dir"], asset.file_stem))
    actions = _POOL_STATE["actions"].get(asset.ticker)

    # EXTEND WHERE POSSIBLE. The hourly run adds one bar to an archive of up to
    # 145,000 and used to recompute every one of them: measured, the metric
    # chain is 76% of this step's cost and the write is the rest, so the whole
    # saving is here. Falls back to the full chain whenever the store cannot be
    # trusted to be a prefix of the answer - see extend_asset_metrics.
    existing = None
    if not _POOL_STATE["full"] and os.path.exists(path):
        try:
            existing = pd.read_parquet(path)
        except Exception:                        # a truncated write from a killed run
            existing = None
    metrics = extend_asset_metrics(asset, basket, frame,
                                   _POOL_STATE["session_table"], actions,
                                   existing, config) if existing is not None else None
    extended = metrics is not None
    if not extended:
        computed = build_asset_metrics(asset, basket, frame,
                                       _POOL_STATE["session_table"], actions)
        if computed.empty:
            return None
        metrics = versioning.stamp(
            computed[[c for c in METRIC_COLUMNS if c in computed]], config, run)
    if metrics.empty:
        return None
    # AND DO NOT REWRITE AN UNCHANGED FILE. Most instruments have nothing new on
    # most runs - a US fund is closed for seventeen hours a day - and rewriting
    # its metrics anyway was a quarter of a gigabyte of parquet a run spent
    # saying the same thing. `is not existing` rather than a row count, so this
    # cannot skip a write that changed the rows without adding any.
    if extended and metrics is existing:
        return (asset.asset_id, len(metrics), int(metrics["breach_q95"].sum()),
                int(metrics["breach_q99"].sum()), extended)
    metrics.to_parquet(path, index=False, compression="zstd")
    return (asset.asset_id, len(metrics), int(metrics["breach_q95"].sum()),
            int(metrics["breach_q99"].sum()), extended)


def write_all(basket: Basket, bars_dir: str = bars.DEFAULT_BARS_DIR,
              metrics_dir: str = DEFAULT_METRICS_DIR,
              versions: tuple[str, str] | None = None,
              workers: int | None = None, full: bool = False) -> int:
    """build_all's work, in parallel, returning a count rather than the frames.

    This is what the command line calls. build_all stays as it is, serial and
    returning everything, because that is what the tests and every in-process
    caller want - and because a pool that has to be right about pickling is not
    a thing to put in the path of a caller that does not need it.
    """
    import concurrent.futures as cf
    import multiprocessing as mp
    from tremor import versioning

    os.makedirs(metrics_dir, exist_ok=True)
    versions = versions or versioning.versions_for()
    workers = workers or min(len(basket.instruments), os.cpu_count() or 1)
    if workers <= 1:
        return len(build_all(basket, bars_dir, metrics_dir, versions))

    payloads = [(asset, basket) for asset in basket.instruments]
    written = 0
    # "spawn" rather than the platform default: a forked worker inherits the
    # parent's numpy and BLAS state, and the combination of fork with a threaded
    # BLAS is the classic way to get a pool that hangs on some machines and not
    # on others.
    with cf.ProcessPoolExecutor(
            max_workers=workers, mp_context=mp.get_context("spawn"),
            initializer=_pool_init,
            initargs=(bars_dir, metrics_dir, versions, full)) as pool:
        extended = 0
        for outcome in pool.map(_pool_one, payloads):
            if outcome is None:
                continue
            asset_id, rows, q95, q99, was_extended = outcome
            written += 1
            extended += bool(was_extended)
            log.info("%s: bars %d, Q95 breaches %s, Q99 %s%s", asset_id, rows, q95, q99,
                     "" if was_extended else "  (rebuilt whole)")
    log.info("%d of %d instruments extended from the stored metrics", extended, written)
    return written


def load_all(basket: Basket, metrics_dir: str = DEFAULT_METRICS_DIR) -> dict[str, pd.DataFrame]:
    out = {}
    for asset in basket.instruments:
        path = metrics_path(metrics_dir, asset.file_stem)
        if os.path.exists(path):
            out[asset.asset_id] = pd.read_parquet(path)
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Recompute per-asset metrics (§6.1)")
    parser.add_argument("--bars-dir", default=bars.DEFAULT_BARS_DIR)
    parser.add_argument("--metrics-dir", default=DEFAULT_METRICS_DIR)
    parser.add_argument("--workers", type=int, default=None,
                        help="parallel workers; 1 forces the serial path")
    parser.add_argument("--full", action="store_true",
                        help="recompute every bar instead of extending the stored "
                             "metrics; the way to pick up a revision to old history")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    built = write_all(load_basket(), args.bars_dir, args.metrics_dir,
                      workers=args.workers, full=args.full)
    print(f"metrics computed for {built} instruments")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

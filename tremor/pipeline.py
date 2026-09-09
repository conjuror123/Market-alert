"""Assembly of per-asset metrics (spec §6.1, layer A + layer B).

Runs one instrument through the whole phase 1-2 chain: quality gate -> return
channels -> winsorization -> EWMA Z-score and adaptive thresholds -> volume
profile. The result goes into metrics_asset_hour (§6.4).

Computed in batch, over the whole history at once. The hourly run does not need
such a recomputation - appending one bar is enough for it - but that is phase 6's
concern with idempotency and versions. What matters here is different: the
cross-section needs a panel of every asset on a shared hourly grid, and there is
nothing to assemble it from until each one's metrics have been computed.
"""
from __future__ import annotations

import logging
import os
from datetime import date

import pandas as pd

from tremor import bars, corporate_actions, quality, returns, sessions, volume, windows, zscore
from tremor.basket import Asset, Basket, load_basket

log = logging.getLogger("tremor.pipeline")

DEFAULT_METRICS_DIR = os.path.join("data", "tremor", "metrics")

# Exchange timezone by session template - needed by the volume profile, whose
# norm is taken per local exchange hour.
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

    scored["v_r"] = volume.robust_volume_z(
        asset, scored, TEMPLATE_TZ[asset.session_template],
        volume.full_session_days(session_table)
        if asset.session_template == "us_equity" else None)
    scored["asset_id"] = asset.asset_id
    scored["block"] = asset.block
    scored["tier"] = asset.tier
    return scored


METRIC_COLUMNS = [
    "hour_utc", "asset_id", "block", "tier", "close", "volume",
    "r", "r_gap", "r_w", "gap_masked", "is_session_open",
    "sigma_lt", "mad_eff", "z", "sigma_eff", "q95", "q99",
    "breach_q95", "breach_q99", "v_r",
]


def metrics_path(base_dir: str, file_stem: str) -> str:
    return os.path.join(base_dir, f"{file_stem}.parquet")


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


def _pool_init(bars_dir: str, metrics_dir: str, versions: tuple[str, str]) -> None:
    """Loaded once per worker, not once per instrument.

    The session table is six thousand rows and the corporate-action table a few
    hundred; sending either through the task queue for every instrument would
    hand back most of what the pool is for.
    """
    _POOL_STATE.update(
        bars_dir=bars_dir, metrics_dir=metrics_dir, versions=versions,
        session_table=sessions.load_sessions(),
        actions=corporate_actions.load_actions(),
    )


def _pool_one(payload: "tuple[Asset, Basket]") -> "tuple[str, int, int, int] | None":
    asset, basket = payload
    from tremor import versioning

    frame = bars.load(bars.store_path(_POOL_STATE["bars_dir"], asset.file_stem))
    metrics = build_asset_metrics(asset, basket, frame, _POOL_STATE["session_table"],
                                  _POOL_STATE["actions"].get(asset.ticker))
    if metrics.empty:
        return None
    stored = metrics[[c for c in METRIC_COLUMNS if c in metrics]]
    config, run = _POOL_STATE["versions"]
    versioning.stamp(stored, config, run).to_parquet(
        metrics_path(_POOL_STATE["metrics_dir"], asset.file_stem),
        index=False, compression="zstd")
    return (asset.asset_id, len(metrics), int(metrics["breach_q95"].sum()),
            int(metrics["breach_q99"].sum()))


def write_all(basket: Basket, bars_dir: str = bars.DEFAULT_BARS_DIR,
              metrics_dir: str = DEFAULT_METRICS_DIR,
              versions: tuple[str, str] | None = None,
              workers: int | None = None) -> int:
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
            initargs=(bars_dir, metrics_dir, versions)) as pool:
        for outcome in pool.map(_pool_one, payloads):
            if outcome is None:
                continue
            asset_id, rows, q95, q99 = outcome
            written += 1
            log.info("%s: bars %d, Q95 breaches %s, Q99 %s", asset_id, rows, q95, q99)
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    built = write_all(load_basket(), args.bars_dir, args.metrics_dir,
                      workers=args.workers)
    print(f"metrics computed for {built} instruments")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

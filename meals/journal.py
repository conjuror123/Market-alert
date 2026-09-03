"""Decision journal and the trigger-readiness table (spec §6.1, §6.4, §6.6).

The journal answers the question that arises a month after a firing: why did the
system decide that way in that hour. The metrics hold VALUES, the events hold the
OUTCOME, and the most useful thing falls between them: what was compared against
what, and what came of it. Each journal row is one comparison: the value, the
threshold, the result, and the configuration and run versions under which it was
made.

The volume has to be split deliberately. Basket decisions are written for every
hour that passed quorum: about a dozen per hour, a few million rows over five
years - tolerable. Per-asset decisions are written only where a trigger FIRED:
twenty-three instruments over thirty-five thousand hours would produce millions
of rows recording "nothing happened", while the values themselves already sit in
metrics_asset_hour, from which they can be pulled by hour and asset.

The first_valid_hour table (§6.6) answers a different question: from when a
trigger can be trusted at all. Until the windows have filled, a trigger's value
is NULL rather than False, and the hour takes no part in backtest statistics.
Without such a table the burn-in blends imperceptibly into the working period,
and quality over it looks worse than it is.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from meals import si_index, windows
from meals.cross_section import QUORUM_MIN_ASSETS

DEFAULT_JOURNAL_PATH = os.path.join("data", "meals", "decision_log.parquet")
DEFAULT_WARMUP_PATH = os.path.join("data", "meals", "first_valid_hour.parquet")

COLUMNS = ["hour_utc", "scope", "trigger", "value", "threshold", "result",
           "config_version", "run_version"]


def _rows(frame: pd.DataFrame, scope: str, trigger: str,
          value: pd.Series, threshold, result: pd.Series) -> pd.DataFrame:
    """One comparison across all hours at once. Rows where no decision was taken
    (NULL) do not enter the journal: their absence is what means "not assessed"."""
    evaluated = result.notna()
    if not evaluated.any():
        return pd.DataFrame(columns=COLUMNS[:6])
    limit = (pd.Series(threshold, index=frame.index) if np.isscalar(threshold)
             else threshold)
    return pd.DataFrame({
        "hour_utc": frame.index[evaluated],
        "scope": scope,
        "trigger": trigger,
        "value": pd.to_numeric(value[evaluated], errors="coerce").to_numpy(),
        "threshold": pd.to_numeric(limit[evaluated], errors="coerce").to_numpy(),
        "result": result[evaluated].astype("boolean").to_numpy(),
    })


def basket_decisions(frame: pd.DataFrame) -> pd.DataFrame:
    """Basket decisions for every hour (§6.1)."""
    quorum = frame["quorum_ok"].astype("boolean")
    parts = [
        _rows(frame, "basket", "quorum", frame["n_assets"], QUORUM_MIN_ASSETS, quorum),
        _rows(frame, "basket", "csv_compression", frame["csv_norm"],
              frame.get("csv_norm_q10"), frame["csv_compression"]),
        _rows(frame, "basket", "pca_sync", frame["pc1_ratio"],
              frame.get("pc1_threshold"), frame["pca_sync"]),
        _rows(frame, "basket", "single_factor", frame["pc1_ratio"],
              frame.get("pc1_threshold"), frame["single_factor"]),
    ]
    for name, points in (("price_shock", si_index.POINTS_PRICE_SHOCK),
                         ("volume", si_index.POINTS_VOLUME),
                         ("cluster_shift", si_index.POINTS_CLUSTER_SHIFT)):
        column = f"trigger_{name}"
        if column in frame:
            parts.append(_rows(frame, "basket", column, frame["base_points"], points,
                               frame[column].astype("boolean").where(quorum, pd.NA)))
    if "si_total" in frame:
        parts.append(_rows(frame, "basket", "gate", frame["si_total"],
                           si_index.THRESHOLD,
                           (frame["si_total"] >= si_index.THRESHOLD).where(quorum, pd.NA)))
        parts.append(_rows(frame, "basket", "escalation_threshold", frame["si_total"],
                           si_index.ESCALATION_THRESHOLD,
                           (frame["si_total"] >= si_index.ESCALATION_THRESHOLD)
                           .where(quorum, pd.NA)))
    return pd.concat([p for p in parts if not p.empty], ignore_index=True)


def asset_decisions(metrics: dict[str, pd.DataFrame],
                    residuals: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    """Per-asset decisions - only the ones that fired (see the module docstring)."""
    parts = []
    for asset_id, frame in metrics.items():
        indexed = frame.set_index("hour_utc")
        for trigger, value, threshold in (
            ("breach_q95", indexed["z"].abs(), indexed["q95"]),
            ("breach_q99", indexed["z"].abs(), indexed["q99"]),
        ):
            fired = indexed[trigger].astype("boolean")
            hit = fired.fillna(False)
            if hit.any():
                part = _rows(indexed[hit], asset_id, trigger, value[hit],
                             threshold[hit], fired[hit])
                parts.append(part)
        if indexed["v_r"].notna().any():
            confirmed = indexed["v_r"] > windows.VOLUME_CONFIRM
            if confirmed.any():
                parts.append(_rows(indexed[confirmed], asset_id, "volume_confirm",
                                   indexed["v_r"][confirmed], windows.VOLUME_CONFIRM,
                                   confirmed[confirmed].astype("boolean")))

    for asset_id, frame in (residuals or {}).items():
        indexed = frame.set_index("hour_utc")
        if "z_resid" not in indexed:
            continue
        fired = ((indexed["z_resid"].abs() > indexed["q99_resid"])
                 & (indexed["e_resid"].abs()
                    >= windows.ABS_LEG_Q99 * indexed["sigma_lt_resid"]))
        if fired.any():
            parts.append(_rows(indexed[fired], asset_id, "saed",
                               indexed["z_resid"].abs()[fired],
                               indexed["q99_resid"][fired],
                               fired[fired].astype("boolean")))
    if not parts:
        return pd.DataFrame(columns=COLUMNS[:6])
    return pd.concat(parts, ignore_index=True)


def stamp(rows: pd.DataFrame, config_version: str, run_version: str) -> pd.DataFrame:
    """Stamps the versions. Per §6.3 they are written into EVERY row: comparing
    decisions from different versions is permitted only with the versions stated
    explicitly, and for that the version must live in the row itself, not in a
    file name."""
    return rows.assign(config_version=config_version, run_version=run_version)[COLUMNS]


def first_valid_hour(metrics: dict[str, pd.DataFrame],
                     basket_frame: pd.DataFrame | None = None) -> pd.DataFrame:
    """The first hour from which a trigger is assessed at all (§6.6)."""
    rows = []
    for asset_id, frame in metrics.items():
        indexed = frame.set_index("hour_utc")
        for trigger in ("breach_q95", "breach_q99", "v_r", "z"):
            if trigger not in indexed:
                continue
            evaluated = indexed[trigger].notna()
            rows.append({
                "scope": asset_id, "trigger": trigger,
                "first_valid_hour": int(evaluated.idxmax()) if evaluated.any() else None,
                "evaluated_hours": int(evaluated.sum()),
                "total_hours": int(len(indexed)),
            })
    if basket_frame is not None:
        for trigger in ("quorum_ok", "csv_compression", "pca_sync", "single_factor"):
            if trigger not in basket_frame:
                continue
            evaluated = basket_frame[trigger].notna()
            rows.append({
                "scope": "basket", "trigger": trigger,
                "first_valid_hour": (int(basket_frame.index[evaluated][0])
                                     if evaluated.any() else None),
                "evaluated_hours": int(evaluated.sum()),
                "total_hours": int(len(basket_frame)),
            })
    return pd.DataFrame(rows)

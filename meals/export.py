"""Экспорт кластерного события в JSON по фиксированной схеме (ТЗ п.6.5).

Событие в базе - это одна строка: идентификатор, час, баллы. Разобрать по ней
через полгода, что именно произошло, невозможно: сама строка не хранит ни
поведения активов вокруг T0, ни того, была ли синхронность, ни какие одиночные
всплески случились рядом. Экспорт восстанавливает эту картину целиком, и потому
он не отчёт для человека, а машиночитаемый срез с фиксированной схемой: по нему
считается разметка на Ф7 и сверяются прогоны разных версий.

Окно - [T0 - 12, T0 + 12] в часах ЭТАЛОННОГО КАЛЕНДАРЯ, а не в календарных.
Разница не косметическая: событие в пятницу вечером календарными часами утянуло
бы в правое плечо выходные, где нет ни баров фондов, ни валютных пар, и половина
окна оказалась бы пустой. Часы эталонного календаря переносят плечо на вечер
воскресенья, где торговля есть.

Правая половина окна в реальном времени ещё не наступила. Это не ошибка: экспорт
пишется постфактум, для разбора и разметки, и событие с усечённым правым плечом
помечено truncated_right - чтобы бэктест не принял неполное окно за спокойный
рынок.
"""
from __future__ import annotations

import json
import logging
import os

import numpy as np
import pandas as pd

from meals import versioning
from meals.basket import Basket

log = logging.getLogger("meals.export")

DEFAULT_EXPORT_DIR = os.path.join("data", "meals", "events")
SCHEMA_PATH = os.path.join("schema", "event_export.schema.json")
SCHEMA_VERSION = "1.0"

# Плечо окна в часах эталонного календаря (п.6.5).
WINDOW_HOURS = 12

# Ряды по активу, попадающие в экспорт. r - фактическая доходность часа,
# z - её Z-оценка, z_resid - оценка остатка на факторах, v_r - подтверждение
# объёмом. Больше в схему не берётся сознательно: винзоризованные ряды и
# состояния EWMA восстанавливаются из metrics_asset_hour по часу и активу, а
# дублировать их в каждом событии значило бы раздуть экспорт вчетверо ради
# величин, которые никто не читает глазами.
ASSET_SERIES = ("r", "z", "z_resid", "v_r")

# Агрегатные метрики корзины по каждому часу окна (п.6.5).
BASKET_SERIES = ("csv_norm", "pc1_ratio", "mean_pairwise_corr", "m_weighted_median",
                 "si_total", "base_points", "n_assets", "quorum_ok")


def _clean(value):
    """NaN, NA и numpy-скаляры -> то, что json умеет записать.

    json.dumps выдаёт NaN как литерал NaN, который не является валидным JSON и
    роняет любой строгий парсер на той стороне. Пропуск здесь - это null.
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
    """Часы окна вокруг T0 и признаки усечения по обоим краям.

    hours - упорядоченный массив часов эталонного календаря (все часы, по
    которым система вообще считала). Окно берётся по ПОЗИЦИИ в этом массиве,
    а не по арифметике времени: именно так "12 часов эталонного календаря"
    и определены в п.2.2.
    """
    position = int(np.searchsorted(hours, t0_utc))
    if position >= len(hours) or hours[position] != t0_utc:
        return np.empty(0, dtype=hours.dtype), True, True
    left, right = position - arm, position + arm
    truncated_left, truncated_right = left < 0, right >= len(hours)
    return hours[max(left, 0):right + 1], truncated_left, truncated_right


def _series_for(frame: pd.DataFrame, hours: np.ndarray,
                columns: tuple[str, ...]) -> dict[str, list]:
    """Ряды по часам окна. Час без данных даёт null, а не выпадает из ряда:
    длина всех рядов события совпадает с длиной окна, и потребителю не нужно
    сопоставлять их по индексу."""
    aligned = frame.reindex(hours)
    return {name: [_clean(v) for v in aligned[name]]
            for name in columns if name in aligned.columns}


def _asset_frame(metrics: pd.DataFrame,
                 residuals: pd.DataFrame | None) -> pd.DataFrame:
    frame = metrics.set_index("hour_utc")
    if residuals is not None and not residuals.empty:
        extra = residuals.set_index("hour_utc")
        for name in ("z_resid", "e_resid", "beta"):
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
    """Полный срез одного кластерного события."""
    hours = basket_frame.index.to_numpy()
    window, truncated_left, truncated_right = window_hours(hours, int(event["t0_utc"]))

    assets = {}
    for asset_id in sorted(metrics):
        frame = _asset_frame(metrics[asset_id], residuals.get(asset_id))
        series = _series_for(frame, window, ASSET_SERIES)
        if not any(v is not None for values in series.values() for v in values):
            # Инструмент, у которого во всём окне нет ни одного значения, в
            # экспорт не идёт: ночное событие иначе тащило бы за собой
            # двенадцать фондов с рядами из одних null.
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
             "r": _clean(row["r"]), "repeat_count": _clean(row["repeat_count"])}
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
    """Одно событие - один файл. Имя строится из event_id, в котором двоеточие
    заменено подчёркиванием: Windows такого имени не принимает, а экспорт
    должен читаться и там."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{payload['event_id'].replace(':', '_')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    return path


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

    from meals import cluster, cross_section, pipeline, saed
    from meals.basket import load_basket

    parser = argparse.ArgumentParser(description="Экспорт кластерных событий (п.6.5)")
    parser.add_argument("--basket-metrics", default=cross_section.DEFAULT_BASKET_METRICS_PATH)
    parser.add_argument("--events", default=cluster.DEFAULT_EVENTS_PATH)
    parser.add_argument("--escalations", default=cluster.DEFAULT_ESCALATIONS_PATH)
    parser.add_argument("--saed-events", default=saed.DEFAULT_EVENTS_PATH)
    parser.add_argument("--metrics-dir", default=pipeline.DEFAULT_METRICS_DIR)
    parser.add_argument("--residuals-dir", default=saed.DEFAULT_RESIDUALS_DIR)
    parser.add_argument("--out-dir", default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--since", type=int, default=None,
                        help="экспортировать только события с t0_utc не раньше")
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

    written = 0
    for payload in build_all(events, basket_frame, metrics, residuals, saed_events,
                             escalations, config, run, basket):
        write_event(payload, args.out_dir)
        written += 1
    log.info("экспортировано событий %d в %s (config %s, run %s)",
             written, args.out_dir, config, run)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

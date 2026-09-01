"""Кластерные события: гейт, кулдаун, эскалации (ТЗ п.4.1, п.5).

Кулдаун здесь не таймер молчания, а способ отличить НОВОЕ событие от
продолжения старого. Рынок после сильного движения ещё несколько дней шумит, и
без паузы система рассказывала бы об одном и том же шторме каждый час. Но
жёсткая пауза плоха обратным: если внутри неё случится нечто действительно
большее, промолчать нельзя. Отсюда две ветви досрочного пробоя - и антидребезг,
который не даёт им превратиться в обычный поток.

Приоритет ветвей задан прямо: если в один час выполнены обе, применяется шок
высшего порядка, а векторный разворот в этот час не рассматривается. Разница
существенная - шок наращивает текущее событие, разворот открывает новое со
ссылкой на родителя.

Всё измеряется в часах эталонного календаря, поэтому автомат идёт по их
упорядоченному списку, а не по календарному времени: 72 ч.э.к. - это трое
торговых суток, а не трое календарных.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from meals import si_index, windows


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


# Ветвь А запрещена в первый торговый час после T0 (п.5.2).
BRANCH_A_MIN_AGE = 1
BRANCH_B_MIN_AGE = windows.REVERSAL_DELAY
MAX_EARLY_BREAKS = 2


def reversal_scale(m: pd.Series, window: int = windows.W_CS) -> pd.DataFrame:
    """sigma_M и k_t для ветви векторного разворота (п.5.2).

    k_t берётся как максимум из эмпирического перцентиля и 1.5: перцентиль
    подстраивается под период, и в затяжное затишье он опустился бы так низко,
    что разворотом считалось бы любое колебание.
    """
    sigma = m.shift(1).rolling(window, min_periods=window).std(ddof=1)
    normalised = (m / sigma).abs()
    k = normalised.shift(1).rolling(window, min_periods=window).quantile(0.975)
    return pd.DataFrame({"sigma_m": sigma, "k": np.maximum(k, 1.5)})


def run(frame: pd.DataFrame) -> tuple[list[ClusterEvent], pd.DataFrame]:
    """Последовательный автомат по часам эталонного календаря.

    На вход - кадр, проиндексированный часами по возрастанию, с колонками:
    quorum_ok, trigger_cluster_shift, si_total, base_points, breadth_q99,
    m_weighted_median, sigma_m, k.

    Возвращает события и покасовой журнал решений: что именно сработало в
    каждый час и почему уведомление ушло или не ушло.
    """
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
    opened_at = -1              # индекс часа T0 текущего события
    cooldown_until = -1         # индекс, до которого действует пауза
    early_breaks: list[int] = []
    m_at_t0 = np.nan

    for i in range(len(frame)):
        if not quorum[i]:
            journal[i] = "no_quorum"
            continue

        gate = shift[i] and si[i] >= si_index.THRESHOLD

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
                early_breaks = []
                journal[i] = "event_created"
            elif shift[i]:
                journal[i] = "gate_below_threshold"
            continue

        # --- внутри кулдауна ---
        age = i - opened_at
        # Антидребезг: не более двух пробоев на скользящее окно 24 ч.э.к.
        recent = [b for b in early_breaks if i - b < windows.ESCALATION_DEBOUNCE]
        exhausted = len(recent) >= MAX_EARLY_BREAKS

        higher_order = (age >= BRANCH_A_MIN_AGE
                        and (si[i] >= si_index.ESCALATION_THRESHOLD or breadth[i]))
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
                           if si[i] >= si_index.ESCALATION_THRESHOLD else "breadth_q99"),
            })
            # Кулдаун отсчитывается заново от момента эскалации.
            cooldown_until = i + windows.CLUSTER_COOLDOWN
            early_breaks.append(i)
            journal[i] = "escalation"
            continue

        if reversal:
            if exhausted:
                journal[i] = "branch_b_debounced"
                continue
            parent = current.event_id
            current.status = "finished"
            current = ClusterEvent(event_id=f"cluster:{int(hours[i])}",
                                   t0_utc=int(hours[i]), si_total_t0=float(si[i]),
                                   base_points_t0=int(points[i]), parent_event_id=parent)
            events.append(current)
            opened_at, m_at_t0 = i, m[i]
            cooldown_until = i + windows.CLUSTER_COOLDOWN
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


DEFAULT_EVENTS_PATH = "data/meals/cluster_events.parquet"
DEFAULT_ESCALATIONS_PATH = "data/meals/cluster_event_escalations.parquet"


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging
    import os

    from meals import bars, calendar_multiplier, cross_section, pipeline, vix
    from meals.basket import load_basket

    parser = argparse.ArgumentParser(description="SI-Index и кластерные события (п.4, п.5)")
    parser.add_argument("--metrics-dir", default=pipeline.DEFAULT_METRICS_DIR)
    parser.add_argument("--basket-metrics", default=cross_section.DEFAULT_BASKET_METRICS_PATH)
    parser.add_argument("--events-out", default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--escalations-out", default=DEFAULT_ESCALATIONS_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("meals.cluster")

    basket = load_basket()
    metrics = pipeline.load_all(basket, args.metrics_dir)
    basket_frame = pd.read_parquet(args.basket_metrics).set_index("hour_utc").sort_index()
    # Прогон дописывает свои колонки в тот же файл, поэтому при повторном
    # запуске они уже там. Требование п.6.2 - повторный прогон того же часа не
    # должен ни падать, ни двоить результат, - так что производные колонки
    # сбрасываются и считаются заново.
    derived = [c for c in ("m_calendar", "m_vix", "si_total", "sigma_m", "k",
                           "decision", "base_points", "breadth_q99",
                           "n_active_blocks", "n_active_blocks_q99")
               if c in basket_frame.columns]
    derived += [c for c in basket_frame.columns if c.startswith("trigger_")]
    basket_frame = basket_frame.drop(columns=derived)
    hours = basket_frame.index

    calendar = calendar_multiplier.multiplier_series(hours)
    scored = vix.score(vix.load_series(
        os.path.join(bars.DEFAULT_VIX_DIR, f"{basket.volatility_index.file_stem}.parquet")))
    vix_windows = vix.windows_from_spikes(scored, hours.to_numpy())
    vix_multiplier = vix.multiplier_series(hours, vix_windows)

    points = si_index.base_points(basket, metrics, basket_frame["single_factor"],
                                  hours, windows.VOLUME_CONFIRM)
    frame = basket_frame.join(points)
    frame["m_calendar"] = pd.Series(calendar).reindex(hours).fillna(1.0)
    frame["m_vix"] = pd.Series(vix_multiplier).reindex(hours).fillna(1.0)
    frame["si_total"] = si_index.si_total(frame["base_points"], frame["m_calendar"],
                                          frame["m_vix"])
    frame = frame.join(reversal_scale(frame["m_weighted_median"]))

    events, journal = run(frame)
    frame = frame.join(journal)

    for path, data in ((args.events_out, events_frame(events)),
                       (args.escalations_out, escalations_frame(events))):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data.to_parquet(path, index=False, compression="zstd")
    frame.reset_index(names="hour_utc").to_parquet(args.basket_metrics, index=False,
                                                   compression="zstd")

    reversals = sum(1 for e in events if e.parent_event_id)
    log.info("кластерных событий %d (из них разворотов %d), эскалаций %d",
             len(events), reversals, sum(e.escalation_seq for e in events))
    log.info("баллы: медиана %.0f, максимум %d | SI_total: максимум %.1f",
             frame["base_points"].median(), int(frame["base_points"].max()),
             frame["si_total"].max())
    log.info("решения по часам: %s",
             frame["decision"].value_counts().head(6).to_dict())
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

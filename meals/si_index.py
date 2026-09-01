"""Сводный Индекс Сенсации (ТЗ п.4).

    SI_total = (сумма базовых баллов) * M_calendar * M_VIX

Четыре триггера отвечают на четыре разных вопроса, и складываются баллы именно
потому, что вопросы разные: было ли движение экстремальным (ценовой шок),
подтверждено ли оно потоком заявок (объём), затронуло ли оно РАЗНЫЕ части рынка
(кластерный сдвиг) и объясняется ли всё происходящее одной причиной
(однофакторность). Событие, набравшее баллы по нескольким осям сразу,
качественно отличается от того, что набрало столько же по одной.

Балл за тип триггера начисляется ОДИН раз за час, сколько бы активов условию ни
удовлетворяло. Кластерный сдвиг и однофакторность уже агрегируют информацию по
корзине, и умножать их ещё и на число активов значило бы считать одно и то же
дважды.

Широта считается по БЛОКАМ, а не по тикерам. Шесть валютных пар, дёрнувшихся на
одном движении доллара, - это один блок, а не шесть свидетельств.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from meals.basket import Basket

# Веса триггеров (п.4.2), все помечены в ТЗ звёздочкой.
POINTS_PRICE_SHOCK = 3
POINTS_VOLUME = 2
POINTS_CLUSTER_SHIFT = 4
POINTS_SINGLE_FACTOR = 3
MAX_POINTS = POINTS_PRICE_SHOCK + POINTS_VOLUME + POINTS_CLUSTER_SHIFT + POINTS_SINGLE_FACTOR

# Доля активов блока, при которой блок считается активным (п.4.2).
BLOCK_ACTIVE_SHARE = 0.33
BLOCK_ACTIVE_MIN = 2
MIN_ACTIVE_BLOCKS = 2

# Пороги решений (п.4.5), тоже стартовые значения.
THRESHOLD = 7
ESCALATION_THRESHOLD = 12

# Шкала широты по Q99 (п.4.5, п.5.2).
BREADTH_SHARE = 0.50
BREADTH_MIN_BLOCKS = 2


def _panel(metrics: dict[str, pd.DataFrame], column: str, hours: pd.Index) -> pd.DataFrame:
    series = {aid: df.set_index("hour_utc")[column] for aid, df in metrics.items()}
    return pd.DataFrame(series).reindex(hours)


def active_blocks(breaches: pd.DataFrame, present: pd.DataFrame,
                  blocks: dict[str, str]) -> pd.DataFrame:
    """Сколько активов блока пробило порог и сколько их вообще в сессии.

    Доля берётся от активов В СЕССИИ, а не от списочного состава блока: ночью
    фонды закрыты, и требовать от блока equity трети участников было бы
    требованием невыполнимым, а не строгим.
    """
    counts = {}
    for block in sorted(set(blocks.values())):
        columns = [c for c in breaches.columns if blocks.get(c) == block]
        if not columns:
            continue
        hit = breaches[columns].fillna(False).sum(axis=1)
        in_session = present[columns].sum(axis=1)
        counts[block] = (hit >= np.maximum(BLOCK_ACTIVE_SHARE * in_session,
                                           BLOCK_ACTIVE_MIN))
    return pd.DataFrame(counts, index=breaches.index)


def base_points(basket: Basket, metrics: dict[str, pd.DataFrame],
                single_factor: pd.Series, hours: pd.Index,
                volume_threshold: float) -> pd.DataFrame:
    """Базовые баллы по п.4.2, по каждому триггеру отдельно."""
    ids = {a.asset_id for a in basket.assets}
    blocks = {a.asset_id: a.block for a in basket.assets}
    tier1 = {a.asset_id for a in basket.assets if a.tier == 1}
    with_volume = {a.asset_id for a in basket.assets if a.has_volume}
    basket_metrics = {k: v for k, v in metrics.items() if k in ids}

    q99 = _panel(basket_metrics, "breach_q99", hours).astype("boolean")
    q95 = _panel(basket_metrics, "breach_q95", hours).astype("boolean")
    v_r = _panel(basket_metrics, "v_r", hours)
    present = _panel(basket_metrics, "r", hours).notna()

    price_shock = q99.fillna(False).any(axis=1)

    # Объём подтверждает не сам по себе, а у актива, который УЖЕ дал ценовой
    # шок: всплеск объёма без движения цены - это другое событие.
    volume_columns = [c for c in q99.columns if c in with_volume]
    volume_confirms = (q99[volume_columns].fillna(False)
                       & (v_r[volume_columns] > volume_threshold)).any(axis=1)

    active = active_blocks(q95, present, blocks)
    tier1_columns = [c for c in q95.columns if c in tier1]
    cluster_shift = ((active.sum(axis=1) >= MIN_ACTIVE_BLOCKS)
                     & q95[tier1_columns].fillna(False).any(axis=1))

    active_q99 = active_blocks(q99, present, blocks)
    represented = pd.DataFrame({
        block: present[[c for c in present.columns if blocks.get(c) == block]].any(axis=1)
        for block in sorted(set(blocks.values()))
    }, index=hours).sum(axis=1)
    breadth = ((active_q99.sum(axis=1) >= BREADTH_MIN_BLOCKS)
               & (active_q99.sum(axis=1) >= BREADTH_SHARE * represented))

    single = single_factor.reindex(hours).fillna(False).astype(bool)

    return pd.DataFrame({
        "trigger_price_shock": price_shock,
        "trigger_volume": volume_confirms,
        "trigger_cluster_shift": cluster_shift,
        "trigger_single_factor": single,
        "n_active_blocks": active.sum(axis=1),
        "n_active_blocks_q99": active_q99.sum(axis=1),
        "breadth_q99": breadth,
        "base_points": (price_shock * POINTS_PRICE_SHOCK
                        + volume_confirms * POINTS_VOLUME
                        + cluster_shift * POINTS_CLUSTER_SHIFT
                        + single * POINTS_SINGLE_FACTOR),
    }, index=hours)


def si_total(points: pd.Series, m_calendar: pd.Series, m_vix: pd.Series) -> pd.Series:
    """Итоговый балл после мультипликаторов - основная шкала всех пороговых
    решений (п.4.5)."""
    return points * m_calendar * m_vix

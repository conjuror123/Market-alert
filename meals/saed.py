"""Модуль одиночных событий SAED (ТЗ п.8).

Ловит движения, которые не объясняются общим рынком. Работает параллельно с
кластерным детектором и зависимость между ними односторонняя: SAED берёт фактор
корзины как вход, но на SI-Index, кластерный гейт и кулдаун не влияет никак.

Три вещи, которые легко упустить и которые ТЗ оговаривает отдельно.

Кулдаун считается в БАРАХ САМОГО АКТИВА, а не в календарных часах. Двенадцать
баров - это полторы торговых сессии для биржевого фонда и полсуток для крипты.
Иначе фонд, у которого в дне семь баров, молчал бы почти двое суток там, где
круглосуточный инструмент отходит за двенадцать часов.

Пауза не отменяет событие, а объединяет его с текущим: повторные срабатывания
внутри кулдауна увеличивают repeat_count. Это разные вещи - "движение
прекратилось" и "движение продолжается, но мы о нём уже сообщили".

Уведомление уходит по БЛОЧНОМУ алерту, а не по каждому активу. Если в один час
дёрнулись три бумаги одного блока, это одно наблюдение о блоке, а не три
одинаковых сообщения.

Версионирование (config_version, run_version) и метка overlap_with_cluster
появятся на Ф6 и Ф5 - до них нет ни версий конфигурации, ни кластерных событий,
с которыми можно было бы пересечься.
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
    hour_utc: int          # T0_single - час первого срабатывания
    z_resid: float
    e_resid: float
    r: float
    beta: float
    repeat_count: int


def triggers(frame: pd.DataFrame) -> pd.Series:
    """Условие генерации события по п.8.2: гибридное, как и всё в п.3.1.

    Событие создаётся независимо от подтверждения объёмом и любых других
    факторов - в этом и смысл модуля: одиночное движение само по себе является
    поводом, даже если объём обычный.
    """
    known = (frame["z_resid"].notna() & frame["q99_resid"].notna()
             & frame["sigma_lt_resid"].notna())
    hit = ((frame["z_resid"].abs() > frame["q99_resid"])
           & (frame["e_resid"].abs()
              >= windows.ABS_LEG_Q99 * frame["sigma_lt_resid"]))
    return hit.where(known, pd.NA).astype("boolean")


def build_events(asset: Asset, frame: pd.DataFrame,
                 cooldown_bars: int = windows.SAED_COOLDOWN_BARS) -> list[SaedEvent]:
    """Прогоняет автомат кулдауна по барам актива (п.8.3).

    Последовательный проход - слой B из п.6.1: попадёт срабатывание в текущее
    событие или откроет новое, зависит от того, сколько баров прошло с начала
    предыдущего, а это путезависимое решение.
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
    open_at: int | None = None   # индекс бара, на котором открыто текущее событие

    for i in np.flatnonzero(fired):
        if open_at is not None and i - open_at < cooldown_bars:
            # Внутри паузы: то же событие продолжается, уведомления нет.
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
    """Блочная агрегация по п.8.4: одновременные события активов одного блока
    складываются в один алерт.

    Инструменты корзины и внекорзинные одного блока агрегируются вместе - для
    получателя это одно наблюдение о блоке, и делить его по признаку "входит ли
    инструмент в расчёт кворума" было бы делением по чужому основанию.
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
    """Проставляет событиям ссылку на блочный алерт (aggregate_alert_id, п.8.5)."""
    if events.empty:
        return events.assign(aggregate_alert_id=pd.Series(dtype="object"))
    keys = alerts.set_index(["block", "hour_utc"])["alert_id"]
    index = pd.MultiIndex.from_frame(events[["block", "hour_utc"]])
    return events.assign(aggregate_alert_id=keys.reindex(index).to_numpy())


DEFAULT_EVENTS_PATH = "data/meals/saed_events.parquet"
DEFAULT_ALERTS_PATH = "data/meals/saed_block_alerts.parquet"


def build_for_basket(basket: Basket, metrics: dict[str, pd.DataFrame],
                     factor: pd.Series,
                     block_factors: pd.DataFrame | None = None
                     ) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.DataFrame]]:
    """Считает остатки и события по всем инструментам, включая внекорзинные.

    Внекорзинные используют тот же фактор корзины и ту же схему оценки беты
    (п.8.1): они не влияют на фактор, но объясняются им так же, как и остальные.
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

    parser = argparse.ArgumentParser(description="События SAED (п.3.6, п.8)")
    parser.add_argument("--metrics-dir", default=pipeline.DEFAULT_METRICS_DIR)
    parser.add_argument("--basket-metrics", default=cross_section.DEFAULT_BASKET_METRICS_PATH)
    parser.add_argument("--events-out", default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--alerts-out", default=DEFAULT_ALERTS_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("meals.saed")

    basket = load_basket()
    metrics = pipeline.load_all(basket, args.metrics_dir)
    if not metrics:
        log.error("Нет метрик по активам - сначала python -m meals.pipeline")
        return 2
    if not os.path.exists(args.basket_metrics):
        log.error("Нет метрик корзины - сначала python -m meals.cross_section")
        return 2

    basket_frame = pd.read_parquet(args.basket_metrics).set_index("hour_utc")
    factor = basket_frame["m_weighted_median"]

    panel = cross_section.build_panel(metrics, "r")
    reference = [h for h in panel.index
                 if sessions.is_reference_hour(int(h), basket.anchor_exchange_tz)]
    block_factors = cross_section.block_factors(panel.loc[reference], basket)

    events, alerts, _ = build_for_basket(basket, metrics, factor, block_factors)
    for path, frame in ((args.events_out, events), (args.alerts_out, alerts)):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        frame.to_parquet(path, index=False, compression="zstd")

    log.info("событий %d, блочных алертов %d, повторов внутри пауз %d",
             len(events), len(alerts),
             int(events["repeat_count"].sum()) if not events.empty else 0)
    if not alerts.empty:
        log.info("алертов по блокам: %s",
                 alerts["block"].value_counts().to_dict())
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

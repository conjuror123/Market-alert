"""Сборка метрик по каждому активу (ТЗ п.6.1, слой A + слой B).

Прогоняет один инструмент через всю цепочку Ф1-Ф2: гейт качества -> каналы
доходности -> винзоризация -> EWMA Z-score и адаптивные пороги -> профиль
объёма. Результат складывается в metrics_asset_hour (п.6.4).

Считается пакетно, по всей истории сразу. Часовому прогону такой пересчёт не
нужен - ему хватит дописать один бар, - но это забота Ф6 об идемпотентности и
версиях. Здесь важно другое: кросс-секции нужна панель из всех активов на общей
часовой сетке, а собрать её не из чего, пока метрики каждого не посчитаны.
"""
from __future__ import annotations

import logging
import os
from datetime import date

import pandas as pd

from meals import bars, corporate_actions, quality, returns, sessions, volume, windows, zscore
from meals.basket import Asset, Basket, load_basket

log = logging.getLogger("meals.pipeline")

DEFAULT_METRICS_DIR = os.path.join("data", "meals", "metrics")

# Таймзона биржи по шаблону сессии - нужна профилю объёма, у которого норма
# берётся по локальному биржевому часу.
TEMPLATE_TZ = {
    "us_equity": "America/New_York",
    "fx_continuous": "America/New_York",
    "crypto_24_7": "UTC",
}


def bars_per_session(asset: Asset, usable: pd.DataFrame,
                     anchor_tz: str = "America/New_York") -> float:
    """B_asset из п.2.7: медианное число валидных баров в ТОРГОВОМ ДНЕ актива.

    Измеряется, а не задаётся: из него считается W_asset, окно адаптивных
    порогов, и ошибка здесь означала бы окно неверной длины у всех порогов
    сразу.

    Считается по календарным дням биржи, а НЕ по непрерывным сессиям из
    returns.session_ids. Это два разных понятия, и путать их дорого. Для
    гэп-канала сессия - это отрезок непрерывной торговли: у валютной пары целая
    неделя, у круглосуточной крипты вся история одним куском. Подставив такую
    длину сюда, для крипты получаем B_asset под пятьдесят тысяч и окно порогов
    в шесть миллионов баров - оно не набирается никогда, пороги остаются
    неопределёнными, и пробоев не возникает вовсе. Ровно это и случилось:
    ноль пробоев на 49632 барах биткойна.

    Здесь нужен день: 120 * B_asset означает "сто двадцать торговых дней", и
    для фонда это 7 баров в дне, для валютной пары и крипты - 24.
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
    """Полная цепочка метрик для одного инструмента."""
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
              metrics_dir: str = DEFAULT_METRICS_DIR) -> dict[str, pd.DataFrame]:
    session_table = sessions.load_sessions()
    actions = corporate_actions.load_actions()
    os.makedirs(metrics_dir, exist_ok=True)

    result = {}
    for asset in basket.instruments:
        frame = bars.load(bars.store_path(bars_dir, asset.file_stem))
        metrics = build_asset_metrics(asset, basket, frame, session_table,
                                      actions.get(asset.ticker))
        if metrics.empty:
            log.warning("%s: нет пригодных баров", asset.asset_id)
            continue
        stored = metrics[[c for c in METRIC_COLUMNS if c in metrics]]
        stored.to_parquet(metrics_path(metrics_dir, asset.file_stem), index=False,
                          compression="zstd")
        result[asset.asset_id] = metrics
        log.info("%s: баров %d, пробоев Q95 %s, Q99 %s", asset.asset_id, len(metrics),
                 int(metrics["breach_q95"].sum()), int(metrics["breach_q99"].sum()))
    return result


def load_all(basket: Basket, metrics_dir: str = DEFAULT_METRICS_DIR) -> dict[str, pd.DataFrame]:
    out = {}
    for asset in basket.instruments:
        path = metrics_path(metrics_dir, asset.file_stem)
        if os.path.exists(path):
            out[asset.asset_id] = pd.read_parquet(path)
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Пересчёт метрик по активам (п.6.1)")
    parser.add_argument("--bars-dir", default=bars.DEFAULT_BARS_DIR)
    parser.add_argument("--metrics-dir", default=DEFAULT_METRICS_DIR)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    built = build_all(load_basket(), args.bars_dir, args.metrics_dir)
    print(f"метрики посчитаны по {len(built)} инструментам")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

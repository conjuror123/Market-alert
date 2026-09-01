"""Конфигурация корзины: активы, блоки, тиры, производные веса (ТЗ п.2.3).

Ключевое отличие от config.yaml существующего мониторинга: здесь у актива нет
и не может быть собственных порогов. Пороги в MEALS адаптивные - перцентили
собственного распределения |Z| (п.3.1), - поэтому конфигурация описывает
только СОСТАВ и СВОЙСТВА инструментов, а не чувствительность к ним.

Вес - тоже не поле конфигурации. По п.2.3 он производный:
    weight_i = 1 / (N_blocks * N_assets_block)
и если записанный вес разойдётся с правилом, конфигурация считается
невалидной. Единственный надёжный способ не разойтись - не хранить вес вовсе,
а считать его из состава. Именно так здесь и сделано.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime

import yaml

DEFAULT_BASKET_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "basket.yaml")

# Блоки п.2.3 плюс crypto - пятый блок добавлен осознанно, см. basket.yaml.
BLOCKS = ("equity", "rates", "FX", "commodities", "crypto")
TIERS = (1, 2)
SOURCES = ("twelvedata", "coinbase")
FETCH_INTERVALS = ("30min", "1h")


class BasketConfigError(ValueError):
    """Конфигурация корзины невалидна и не может быть использована."""


@dataclass(frozen=True)
class Asset:
    ticker: str
    source: str
    tier: int
    block: str
    has_volume: bool
    session_template: str
    fetch_interval: str
    label: str
    in_basket: bool
    # Шаг котировки источника. Нужен винзоризации п.2.5: нижняя отсечка
    # eps_MAD включает доходность в половину тика, без которой в тихие часы,
    # когда цена стоит, MAD схлопывается в ноль и рядовое движение выглядит
    # экстремальным. Измеряется по реальным данным (meals.audit), а не
    # берётся из спецификации биржи: значение имеет тот формат котировки,
    # который реально отдаёт источник.
    tick_size: float

    @property
    def asset_id(self) -> str:
        """Логический идентификатор для метрик, логов и событий."""
        return f"{self.source}:{self.ticker}"

    @property
    def file_stem(self) -> str:
        """Имя файла в хранилище. Совпадает со схемой candle_store существующего
        мониторинга, чтобы уже накопленную историю можно было импортировать
        без переименований."""
        return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{self.source}_{self.ticker}")


@dataclass(frozen=True)
class VolatilityIndex:
    """Внешний индикатор стресса (п.4.4). В корзину не входит."""
    series_id: str
    source: str
    interval: str
    label: str
    # Своя глубина истории: дневному ряду нужно 720 наблюдений на разогрев,
    # это почти три года, и общий с корзиной старт 2021 года оставил бы
    # множитель равным единице на всём train-периоде. См. basket.yaml.
    history_since: date

    @property
    def file_stem(self) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{self.source}_{self.series_id}")


@dataclass(frozen=True)
class Basket:
    assets: tuple[Asset, ...]
    outside: tuple[Asset, ...]
    volatility_index: VolatilityIndex
    anchor_exchange_tz: str
    history_since: date
    session_templates: dict

    @property
    def instruments(self) -> tuple[Asset, ...]:
        """Всё, по чему нужны бары: корзина плюс инструменты вне корзины."""
        return self.assets + self.outside

    def by_block(self) -> dict[str, list[Asset]]:
        blocks: dict[str, list[Asset]] = {}
        for a in self.assets:
            blocks.setdefault(a.block, []).append(a)
        return blocks

    def weights(self) -> dict[str, float]:
        """Веса по правилу равновесности п.2.3: блоки равновесны между собой,
        активы внутри блока равновесны между собой.

        Считается по всему составу корзины. Когда на Ф1 появятся active_from /
        active_to, сюда добавится аргумент даты - правило от этого не меняется,
        меняется только то, какие активы считаются активными.
        """
        blocks = self.by_block()
        n_blocks = len(blocks)
        return {
            a.asset_id: 1.0 / (n_blocks * len(members))
            for members in blocks.values()
            for a in members
        }


def _as_date(value) -> date:
    return value if isinstance(value, date) else datetime.strptime(str(value), "%Y-%m-%d").date()


def _asset(raw: dict, *, in_basket: bool) -> Asset:
    missing = {"ticker", "source", "tier", "block", "has_volume", "session_template",
               "fetch_interval", "tick_size"} - set(raw)
    if missing:
        raise BasketConfigError(f"{raw.get('ticker', '?')}: не заданы поля {sorted(missing)}")
    if raw["block"] not in BLOCKS:
        raise BasketConfigError(
            f"{raw['ticker']}: блок '{raw['block']}' не из перечня {list(BLOCKS)}")
    if raw["tier"] not in TIERS:
        raise BasketConfigError(f"{raw['ticker']}: tier должен быть 1 или 2, а не {raw['tier']!r}")
    if raw["source"] not in SOURCES:
        raise BasketConfigError(
            f"{raw['ticker']}: источник '{raw['source']}' не из перечня {list(SOURCES)}")
    if raw["fetch_interval"] not in FETCH_INTERVALS:
        raise BasketConfigError(
            f"{raw['ticker']}: fetch_interval '{raw['fetch_interval']}' "
            f"не из перечня {list(FETCH_INTERVALS)}")
    if not isinstance(raw["has_volume"], bool):
        raise BasketConfigError(f"{raw['ticker']}: has_volume должен быть true или false")
    if not isinstance(raw["tick_size"], (int, float)) or raw["tick_size"] <= 0:
        raise BasketConfigError(
            f"{raw['ticker']}: tick_size должен быть положительным числом, "
            f"а не {raw['tick_size']!r}")
    return Asset(
        ticker=raw["ticker"], source=raw["source"], tier=int(raw["tier"]),
        block=raw["block"], has_volume=bool(raw["has_volume"]),
        session_template=raw["session_template"], fetch_interval=raw["fetch_interval"],
        tick_size=float(raw["tick_size"]),
        label=raw.get("label", raw["ticker"]), in_basket=in_basket,
    )


def load_basket(path: str = DEFAULT_BASKET_PATH) -> Basket:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    assets = tuple(_asset(a, in_basket=True) for a in raw.get("assets") or [])
    outside = tuple(_asset(a, in_basket=False) for a in raw.get("outside_basket") or [])
    if not assets:
        raise BasketConfigError("В корзине нет ни одного актива")

    seen: set[str] = set()
    for a in assets + outside:
        if a.asset_id in seen:
            raise BasketConfigError(f"{a.asset_id}: инструмент указан дважды")
        seen.add(a.asset_id)

    templates = raw.get("session_templates") or {}
    for a in assets + outside:
        if a.session_template not in templates:
            raise BasketConfigError(
                f"{a.ticker}: шаблон сессии '{a.session_template}' не описан "
                f"в session_templates")

    # Кворум часа (п.2.3) требует минимум два блока по два актива. Корзина, где
    # это недостижимо ни при каком часе, бессмысленна: кластерные триггеры в ней
    # не сработают никогда, а не "редко".
    populated = [b for b, members in Basket(
        assets, outside, VolatilityIndex("", "", "", "", date.today()), "",
        date.today(), templates
    ).by_block().items() if len(members) >= 2]
    if len(populated) < 2:
        raise BasketConfigError(
            "Кворум недостижим: нужно минимум два блока, в каждом не меньше двух "
            f"активов, а таких блоков {len(populated)}")

    vix_raw = raw.get("volatility_index") or {}
    if not vix_raw.get("series_id"):
        raise BasketConfigError("Не задан volatility_index.series_id (п.4.4)")

    since = raw.get("history_since")
    history_since = _as_date(since)

    return Basket(
        assets=assets,
        outside=outside,
        volatility_index=VolatilityIndex(
            series_id=vix_raw["series_id"], source=vix_raw.get("source", "fred"),
            interval=vix_raw.get("interval", "1d"),
            history_since=_as_date(vix_raw.get("history_since", since)),
            label=vix_raw.get("label", vix_raw["series_id"]),
        ),
        anchor_exchange_tz=raw.get("anchor_exchange_tz", "America/New_York"),
        history_since=history_since,
        session_templates=templates,
    )

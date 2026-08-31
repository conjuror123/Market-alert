"""Таблица корпоративных действий (ТЗ п.2.4, п.6.4).

Дивидендного эндпоинта на тарифе нет - и /dividends, и /splits отвечают 403.
Но даты отсечек можно вывести из самих котировок: вендор умеет отдавать один и
тот же ряд скорректированным и нескорректированным, а отношение между ними -
ступенчатая функция, которая меняется ровно в даты корпоративных действий.
Размер ступени и есть размер выплаты в долях цены.

Проверено: на конце ряда фактор равен ровно 1.0 (будущих выплат нет), а шум
между ступенями держится на уровне 1e-8 - это округление в строках самого
вендора. Порог отсечки взят на два порядка выше шума и на порядок ниже самой
мелкой реальной выплаты.

Зачем это нужно, если ряды у нас нескорректированные. Скорректированные брать
нельзя: они пересчитываются задним числом при каждой новой выплате, поэтому в
дописываемом по часу хранилище старые бары несли бы один коэффициент, а свежие
другой - и на стыке возникал бы искусственный скачок в размере дивиденда, уже
не на границе сессии, а в произвольном часе. Это хуже той проблемы, которую
корректировка решает. Вместо этого ряд остаётся нескорректированным, а падение
цены в день отсечки помечается здесь и исключается из гэп-канала.
"""
from __future__ import annotations

import csv
import os
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date

import requests

DEFAULT_ACTIONS_PATH = os.path.join("data", "meals", "corporate_actions.csv")
TIME_SERIES_URL = "https://api.twelvedata.com/time_series"

# Порог, ниже которого ступень считается шумом округления. Наблюдаемый шум -
# порядка 1e-8, самая мелкая реальная выплата у коротких трежерис в 2021 году -
# около 1.5e-4 от цены. Порог посередине, ближе к шуму.
STEP_THRESHOLD = 1e-5

# Ступень крупнее этого - уже не выплата, а дробление акций. Порог высокий
# намеренно: настоящее дробление сдвигает коэффициент в разы (2:1 это 50%), а
# годовая выплата сырьевого фонда доходит до 5% от цены - у DBC в декабре 2024
# было 5.07%, и при пороге в 5% она попала бы в дробления.
SPLIT_THRESHOLD = 0.20

REQUEST_DELAY_SECONDS = 8.0


@dataclass(frozen=True)
class CorporateAction:
    ticker: str
    day: date          # дата отсечки: первый день, когда цена идёт уже без выплаты
    kind: str          # "dividend" или "split"
    factor_step: float # доля цены, на которую сдвинулся коэффициент


class CorporateActionsError(RuntimeError):
    pass


def _daily_closes(symbol: str, api_key: str, since: date, session: requests.Session,
                  adjusted: bool) -> dict[date, float]:
    params = {"symbol": symbol, "interval": "1day", "start_date": since.isoformat(),
              "apikey": api_key, "timezone": "UTC", "outputsize": 5000}
    if adjusted:
        params["adjust"] = "all"
    resp = session.get(f"{TIME_SERIES_URL}?{urllib.parse.urlencode(params)}", timeout=40,
                       headers={"User-Agent": "market-alert-bot"})
    if resp.status_code != 200:
        raise CorporateActionsError(f"{symbol}: статус {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("status") == "error":
        raise CorporateActionsError(f"{symbol}: {data.get('message')}")
    return {date.fromisoformat(v["datetime"][:10]): float(v["close"])
            for v in data.get("values", [])}


def derive_actions(symbol: str, api_key: str, since: date,
                   session: requests.Session | None = None) -> list[CorporateAction]:
    """Находит даты корпоративных действий по ступеням коэффициента коррекции."""
    sess = session or requests.Session()
    raw = _daily_closes(symbol, api_key, since, sess, adjusted=False)
    time.sleep(REQUEST_DELAY_SECONDS)
    adjusted = _daily_closes(symbol, api_key, since, sess, adjusted=True)

    days = sorted(set(raw) & set(adjusted))
    if not days:
        raise CorporateActionsError(f"{symbol}: ряды не пересекаются")

    actions = []
    previous = None
    for day in days:
        if raw[day] <= 0:
            continue
        factor = adjusted[day] / raw[day]
        if previous is not None and previous > 0:
            step = factor / previous - 1
            if abs(step) >= STEP_THRESHOLD:
                actions.append(CorporateAction(
                    ticker=symbol, day=day,
                    kind="split" if abs(step) >= SPLIT_THRESHOLD else "dividend",
                    factor_step=step,
                ))
        previous = factor
    return actions


def write_actions(path: str, actions: list[CorporateAction]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["ticker", "date", "kind", "factor_step"])
        for a in sorted(actions, key=lambda a: (a.ticker, a.day)):
            writer.writerow([a.ticker, a.day.isoformat(), a.kind, f"{a.factor_step:.8f}"])


def load_actions(path: str = DEFAULT_ACTIONS_PATH) -> dict[str, set[date]]:
    """Даты корпоративных действий по тикеру. Пустой словарь, если таблицы нет:
    её отсутствие не должно ронять часовой прогон - оно означает лишь, что
    гэпы отсечек пока не помечаются."""
    if not os.path.exists(path):
        return {}
    by_ticker: dict[str, set[date]] = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            by_ticker.setdefault(row["ticker"], set()).add(date.fromisoformat(row["date"]))
    return by_ticker


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging

    from meals.basket import load_basket

    parser = argparse.ArgumentParser(description="Таблица корпоративных действий (п.2.4)")
    parser.add_argument("--out", default=DEFAULT_ACTIONS_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("meals.corporate_actions")

    api_key = os.environ.get("TWELVEDATA_API_KEY", "")
    if not api_key:
        log.error("Не задан TWELVEDATA_API_KEY")
        return 2

    basket = load_basket()
    # Выплаты и дробления бывают у биржевых фондов. У валютных пар и крипты
    # корпоративных действий не существует по природе инструмента.
    funds = [a for a in basket.instruments if a.source == "twelvedata" and a.block != "FX"]

    session = requests.Session()
    all_actions: list[CorporateAction] = []
    for i, asset in enumerate(funds):
        try:
            found = derive_actions(asset.ticker, api_key, basket.history_since, session)
            all_actions.extend(found)
            splits = sum(1 for a in found if a.kind == "split")
            log.info("%s: выплат %d, дроблений %d", asset.ticker,
                     len(found) - splits, splits)
        except Exception as exc:
            log.error("%s: не удалось - %s", asset.ticker, exc)
        if i < len(funds) - 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    write_actions(args.out, all_actions)
    log.info("%s: записей %d", args.out, len(all_actions))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

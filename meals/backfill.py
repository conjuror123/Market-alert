"""Ф0: заполнение хранилища часовыми барами с 2021 года (ТЗ п.2.1).

Разовый инструмент, не часть часового прогона.

Две трети корзины качать заново не нужно: валютные пары и крипта уже лежат в
data/candle_history/ - существующий мониторинг накопил их с 2021-01-01. Они
импортируются из NDJSON и докачиваются только на недостающий хвост. Из сети
целиком тянутся лишь биржевые фонды, которых в прежней корзине не было.

Биржевые фонды запрашиваются ПОЛУЧАСОВЫМИ барами и складываются в часовые по
границе круглого часа UTC (см. bars.to_hourly): их собственная часовая сетка
идёт по :30 и не совпала бы с валютными парами и криптой. Кредитов это не
стоит - тариф Twelve Data считает запросы, а не строки, - но требует более
крупного окна на запрос: у фонда около 13 получасовых баров в торговый день,
так что в один ответ на 5000 строк влезает больше года.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timezone

import requests

from meals import bars, fred
from meals.basket import Asset, Basket, load_basket
from price_monitor import candle_store, coinbase, twelvedata
from price_monitor.models import ExchangeError

log = logging.getLogger("meals.backfill")

LEGACY_HISTORY_DIR = os.path.join("data", "candle_history")

COINBASE_BASE_URL = "https://api.exchange.coinbase.com"
TWELVEDATA_BASE_URL = "https://api.twelvedata.com"

# Пауза между запросами к Twelve Data. Бесплатный тариф - 8 запросов в минуту,
# 8 секунд держат ровно эту границу с небольшим запасом. Пауза выдерживается и
# между инструментами, не только внутри одного: лимит общий на ключ, а его же
# в это время расходует работающий часовой мониторинг.
TWELVEDATA_DELAY_SECONDS = 8.0

# Сколько календарных дней просить в одном запросе. У получасовых баров фонда
# около 13 строк в торговый день, так что 300 дней - это ~3900 строк при
# потолке в 5000. У часовых валютных пар 24 строки в сутки, и там работает
# более осторожное значение по умолчанию самого клиента.
CHUNK_DAYS = {"30min": 300, "1h": 150}


def _days_since(start: date) -> float:
    return max(1.0, (datetime.now(timezone.utc) - datetime.combine(
        start, datetime.min.time(), tzinfo=timezone.utc)).total_seconds() / 86400)


def import_legacy(asset: Asset, path: str, legacy_dir: str = LEGACY_HISTORY_DIR) -> int:
    """Переносит уже накопленную NDJSON-историю в Parquet. Схема имён файлов у
    candle_store и у MEALS одна и та же, так что сопоставление прямое."""
    legacy_path = candle_store.store_path(legacy_dir, asset.source, asset.ticker)
    if not os.path.exists(legacy_path):
        return 0
    candles = candle_store.load_candles(legacy_path)
    if not candles:
        return 0
    return bars.merge(path, bars.to_hourly(bars.candles_to_frame(candles)))


def fetch_missing(asset: Asset, path: str, since: date, api_key: str,
                  session: requests.Session) -> int:
    """Докачивает то, чего в хранилище ещё нет: от последнего сохранённого бара
    до сейчас, а при пустом хранилище - от `since`.

    Просит на сутки больше, чем формально нужно: последний сохранённый бар мог
    быть неполным на момент сохранения, и перекрытие даёт источнику шанс отдать
    его исправленную версию (merge оставит новую).
    """
    stored = bars.load(path)
    if stored.empty:
        days = _days_since(since)
    else:
        last = datetime.fromtimestamp(int(stored["hour_utc"].max()), tz=timezone.utc)
        days = max(1.0, (datetime.now(timezone.utc) - last).total_seconds() / 86400 + 1)

    if asset.source == "twelvedata":
        candles = twelvedata.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            base_url=TWELVEDATA_BASE_URL, api_key=api_key, session=session,
            request_delay_seconds=TWELVEDATA_DELAY_SECONDS,
            chunk_days=CHUNK_DAYS[asset.fetch_interval],
        )
    elif asset.source == "coinbase":
        candles = coinbase.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            base_url=COINBASE_BASE_URL, session=session,
        )
    else:
        raise ExchangeError(f"{asset.asset_id}: неизвестный источник '{asset.source}'")

    return bars.merge(path, bars.to_hourly(bars.candles_to_frame(candles)))


def backfill_instrument(asset: Asset, basket: Basket, bars_dir: str, api_key: str,
                        session: requests.Session, legacy_dir: str = LEGACY_HISTORY_DIR) -> dict:
    path = bars.store_path(bars_dir, asset.file_stem)
    from_legacy = import_legacy(asset, path, legacy_dir)
    from_api = fetch_missing(asset, path, basket.history_since, api_key, session)
    stored = bars.load(path)
    return {
        "asset_id": asset.asset_id,
        "from_legacy": from_legacy,
        "from_api": from_api,
        "rows": len(stored),
        "first": int(stored["hour_utc"].min()) if not stored.empty else None,
        "last": int(stored["hour_utc"].max()) if not stored.empty else None,
    }


def backfill_vix(basket: Basket, vix_dir: str, api_key: str,
                 session: requests.Session) -> dict:
    vix = basket.volatility_index
    frame = fred.fetch_series(vix.series_id, api_key, vix.history_since, session=session)
    path = os.path.join(vix_dir, f"{vix.file_stem}.parquet")
    os.makedirs(vix_dir, exist_ok=True)
    frame.sort_values("day").reset_index(drop=True).to_parquet(
        path, index=False, compression="zstd")
    return {"asset_id": vix.series_id, "rows": len(frame),
            "first": int(frame["day"].min()), "last": int(frame["day"].max())}


def _fmt(epoch: int | None) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d") if epoch else "-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ф0: бэкфилл часовой истории MEALS")
    parser.add_argument("--instruments", default="",
                        help="Тикеры через запятую; по умолчанию - вся корзина")
    parser.add_argument("--bars-dir", default=bars.DEFAULT_BARS_DIR)
    parser.add_argument("--vix-dir", default=bars.DEFAULT_VIX_DIR)
    parser.add_argument("--legacy-dir", default=LEGACY_HISTORY_DIR)
    parser.add_argument("--skip-vix", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    basket = load_basket()
    wanted = {t.strip() for t in args.instruments.split(",") if t.strip()}
    instruments = [a for a in basket.instruments if not wanted or a.ticker in wanted]
    if wanted and not instruments:
        log.error("Ни один инструмент не совпал с --instruments %s", args.instruments)
        return 2

    api_key = os.environ.get("TWELVEDATA_API_KEY", "")
    if not api_key and any(a.source == "twelvedata" for a in instruments):
        log.error("Не задан TWELVEDATA_API_KEY, а в списке есть инструменты Twelve Data")
        return 2

    session = requests.Session()
    failures = 0
    for i, asset in enumerate(instruments):
        try:
            r = backfill_instrument(asset, basket, args.bars_dir, api_key, session, args.legacy_dir)
            log.info("%s: %d баров (%s .. %s), из локальной истории %d, из сети %d",
                     r["asset_id"], r["rows"], _fmt(r["first"]), _fmt(r["last"]),
                     r["from_legacy"], r["from_api"])
        except Exception as exc:
            failures += 1
            log.error("%s: не удалось - %s", asset.asset_id, exc)
        # Лимит в 8 запросов в минуту общий на ключ, и его же расходует
        # работающий часовой мониторинг - пауза нужна и между инструментами.
        if asset.source == "twelvedata" and i < len(instruments) - 1:
            time.sleep(TWELVEDATA_DELAY_SECONDS)

    if not args.skip_vix:
        fred_key = os.environ.get("FRED_API_KEY", "")
        if not fred_key:
            log.error("Не задан FRED_API_KEY - ряд VIX пропущен")
            failures += 1
        else:
            try:
                r = backfill_vix(basket, args.vix_dir, fred_key, session)
                log.info("%s: %d дневных значений (%s .. %s)",
                         r["asset_id"], r["rows"], _fmt(r["first"]), _fmt(r["last"]))
            except Exception as exc:
                failures += 1
                log.error("VIX: не удалось - %s", exc)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Ф0: таблица покрытия данных (ТЗ п.2.1).

По п.2.1 состав корзины не утверждается без этой таблицы, и это не
формальность: почти все параметры окон в п.2.7 заданы в ВАЛИДНЫХ ТОРГОВЫХ
БАРАХ актива, а не в календарном времени. Сколько баров в дне у конкретного
инструмента - то самое B_asset, из которого считается W_asset = max(120 *
B_asset, 720), окно адаптивных порогов Q95/Q99. Не измерив его на реальных
данных, размер окна пришлось бы угадывать.

Отчёт отвечает на три вопроса из п.2.1 - глубина истории, наличие и
сопоставимость часового объёма, целостность рядов - и печатается в Markdown,
чтобы его можно было приложить к решению о составе корзины.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import pandas as pd

from meals import bars
from meals.basket import Asset, load_basket

HOUR = 3600


def _day(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")


def measure_precision(closes: pd.Series, sample: int = 5000) -> float | None:
    """Измеряет ТОЧНОСТЬ КОТИРОВКИ источника - наименьший разряд, в котором он
    вообще выдаёт значения.

    Это не биржевой шаг цены. У валютных пар они совпадают: Twelve Data отдаёт
    пять знаков, и шаг там действительно 1e-5. У биржевых фондов - нет:
    источник возвращает 769.53992 там, где на бирже стоит 769.54, так что
    измеренная точность 1e-7 говорит о формате хранения у вендора, а не о
    шаге торгов, который равен одному центу.

    Поэтому tick_size в конфигурации задан явно, а эта величина нужна для
    проверки: если заявленный шаг ОКАЖЕТСЯ МЕЛЬЧЕ измеренной точности, значит
    конфигурация обещает разрешение, которого в данных нет.
    """
    if closes.empty:
        return None
    decimals = 0
    for value in closes.tail(sample):
        text = f"{float(value):.10g}"
        if "." in text and "e" not in text:
            decimals = max(decimals, len(text.split(".")[1]))
    return 10.0 ** -decimals


def audit_instrument(asset: Asset, frame: pd.DataFrame) -> dict:
    row = {
        "asset_id": asset.asset_id, "ticker": asset.ticker, "block": asset.block,
        "tier": asset.tier, "source": asset.source, "interval": asset.fetch_interval,
        "in_basket": asset.in_basket, "has_volume_declared": asset.has_volume,
        "tick_size": asset.tick_size, "rows": len(frame),
    }
    if frame.empty:
        return row | {"first": None, "last": None, "days": 0, "bars_per_day": 0.0,
                      "precision": None,
                      "zero_volume_pct": None, "has_volume_actual": False,
                      "max_gap_hours": None, "partial_hours": 0, "ohlc_violations": 0,
                      "nonpositive_prices": 0, "negative_volume": 0, "duplicate_hours": 0}

    hours = frame["hour_utc"].astype("int64")
    days = pd.to_datetime(hours, unit="s", utc=True).dt.date
    bars_per_day = float(days.value_counts().median())

    # Самый большой разрыв между соседними барами. Выходные и праздники дают
    # законные разрывы, поэтому число само по себе не является дефектом - оно
    # нужно, чтобы отличить обычный уик-энд от настоящей дыры в истории.
    diffs = hours.diff().dropna()
    max_gap = int(diffs.max() // HOUR) if len(diffs) else 0

    volume = frame["volume"].astype("float64")
    zero_pct = float((volume == 0).mean() * 100)

    # Согласованность OHLC (п.2.6) - с допуском в полтика. Источник округляет
    # поля бара независимо и по-разному: у TLT встречается close 92.42 при high
    # 92.415, у EUR/USD - open 1.0886 при low 1.08862. Это разница меньше
    # одного тика, то есть артефакт округления, а не сломанный бар. Буквальная
    # проверка без допуска пометила бы такие бары is_invalid и выбросила бы из
    # расчётов совершенно нормальные часы.
    tol = asset.tick_size / 2
    ohlc_bad = int((
        (frame["low"] > frame[["open", "close"]].min(axis=1) + tol)
        | (frame[["open", "close"]].max(axis=1) > frame["high"] + tol)
    ).sum())

    return row | {
        "first": _day(hours.min()),
        "last": _day(hours.max()),
        "days": int(days.nunique()),
        "bars_per_day": bars_per_day,
        "precision": measure_precision(frame["close"].astype("float64")),
        "zero_volume_pct": zero_pct,
        "has_volume_actual": bool(zero_pct < 99.0),
        "max_gap_hours": max_gap,
        # Часы, собранные не из полного набора баров источника: у биржевых
        # фондов это первые полчаса сессии, то есть "первый бар сессии" п.2.4.
        "partial_hours": int((frame["n_src"] < frame["n_src"].median()).sum()),
        "ohlc_violations": ohlc_bad,
        "nonpositive_prices": int((frame[["open", "high", "low", "close"]] <= 0).any(axis=1).sum()),
        "negative_volume": int((volume < 0).sum()),
        "duplicate_hours": int(len(frame) - hours.nunique()),
    }


def audit_vix(basket, vix_dir: str) -> dict | None:
    path = os.path.join(vix_dir, f"{basket.volatility_index.file_stem}.parquet")
    if not os.path.exists(path):
        return None
    frame = pd.read_parquet(path)
    return {
        "series_id": basket.volatility_index.series_id, "rows": len(frame),
        "first": _day(frame["day"].min()), "last": _day(frame["day"].max()),
        "median_lag_hours": float(
            ((frame["available_at"] - frame["day"]) / HOUR).median()),
    }


def _flag(row: dict) -> str:
    """Что в этой строке требует внимания. Пусто - значит инструмент готов."""
    notes = []
    if row["rows"] == 0:
        return "нет данных"
    if row["has_volume_declared"] and not row["has_volume_actual"]:
        notes.append("объём заявлен, но пуст")
    if not row["has_volume_declared"] and row["has_volume_actual"]:
        notes.append("объём есть, хотя не заявлен")
    for field, label in (("ohlc_violations", "OHLC"), ("nonpositive_prices", "цены<=0"),
                         ("negative_volume", "объём<0"), ("duplicate_hours", "дубли")):
        if row[field]:
            notes.append(f"{label}: {row[field]}")
    # Шаг цены мельче того, что источник вообще способен выдать - значит
    # half_tick_return в п.2.5 посчитается по разрешению, которого нет.
    if row["precision"] and row["tick_size"] < row["precision"]:
        notes.append(f"шаг {row['tick_size']:g} мельче точности источника {row['precision']:g}")
    return ", ".join(notes)


def render(rows: list[dict], vix: dict | None) -> str:
    out = ["# Таблица покрытия данных MEALS", "",
           f"Составлена {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. "
           "Требование п.2.1 ТЗ: без неё состав корзины не утверждается.", ""]

    for in_basket, title in ((True, "Корзина"), (False, "Вне корзины (только SAED)")):
        subset = [r for r in rows if r["in_basket"] == in_basket]
        if not subset:
            continue
        out += [f"## {title}", "",
                "| Инструмент | Блок | Тир | Интервал | Баров | Период | Дней | "
                "Баров в день | W_asset | Шаг цены | Точность источника | "
                "Объём=0 | Макс. разрыв, ч | Замечания |",
                "|---|---|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|"]
        for r in sorted(subset, key=lambda x: (x["block"], -x["tier"], x["ticker"])):
            head = (f"| `{r['ticker']}` | {r['block']} | {r['tier']} | {r['interval']} | "
                    f"{r['rows']:,} |")
            if not r["rows"]:
                # Инструмент без единого бара - сам по себе результат аудита, а
                # не повод уронить отчёт на форматировании пустых чисел.
                out.append(head + " — | — | — | — | "
                           f"{r['tick_size']:g} | — | — | — | {_flag(r)} |")
                continue
            # W_asset из п.2.7 - окно адаптивных порогов, прямое следствие
            # измеренного здесь числа баров в торговом дне.
            w_asset = max(int(120 * r["bars_per_day"]), 720)
            out.append(
                head +
                f" {r['first']} .. {r['last']} | {r['days']:,} | "
                f"{r['bars_per_day']:.0f} | {w_asset:,} | "
                f"{r['tick_size']:g} | {r['precision']:g} | "
                f"{r['zero_volume_pct']:.0f}% | {r['max_gap_hours']} | {_flag(r) or '—'} |"
            )
        out.append("")

    if vix:
        out += ["## Внешний индикатор стресса", "",
                f"`{vix['series_id']}`: {vix['rows']:,} дневных значений, "
                f"{vix['first']} .. {vix['last']}. Медианная задержка публикации — "
                f"{vix['median_lag_hours']:.0f} ч от полуночи дня наблюдения "
                "(п.4.4, отступление зафиксировано в basket.yaml).", ""]

    total = sum(r["rows"] for r in rows)
    out += ["## Итого", "",
            f"Инструментов: {len(rows)}. Баров: {total:,}. "
            f"Проблемных строк: {sum(1 for r in rows if _flag(r))}.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ф0: таблица покрытия данных MEALS")
    parser.add_argument("--bars-dir", default=bars.DEFAULT_BARS_DIR)
    parser.add_argument("--vix-dir", default=bars.DEFAULT_VIX_DIR)
    parser.add_argument("--out", default=os.path.join("data", "meals", "coverage.md"))
    args = parser.parse_args(argv)

    basket = load_basket()
    rows = [
        audit_instrument(a, bars.load(bars.store_path(args.bars_dir, a.file_stem)))
        for a in basket.instruments
    ]
    report = render(rows, audit_vix(basket, args.vix_dir))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Хранилище часовых баров MEALS: Parquet, по файлу на инструмент.

Почему Parquet, а не NDJSON как в существующем мониторинге: там файл только
дописывается по строке в час и растёт медленно, а здесь на корзину из 23
инструментов с 2021 года приходится порядка полумиллиона баров, и читать их
целиком нужно на каждом прогоне PCA и регрессий. Колоночный формат с типами
читается на порядок быстрее и занимает в несколько раз меньше места.

Конвенция времени - из п.1.2 ТЗ: hour_utc хранит момент ОТКРЫТИЯ бара, а
момент закрытия t = hour_utc + 1 час. Это та же конвенция, что уже
используется в candle_store существующего мониторинга (open_time), так что
накопленная история импортируется без сдвига.
"""
from __future__ import annotations

import os

import pandas as pd

from price_monitor.models import Candle

HOUR = 3600

# n_src - сколько исходных баров источника сложилось в этот часовой бар.
# Нужен на Ф1: у биржевых фондов первые полчаса сессии дают часовой бар из
# одного получасового, и это ровно тот "первый бар сессии", который по п.2.4
# раскладывается на гэп-канал и внутричасовую доходность. Без этого поля
# отличить его от полноценного часа было бы нельзя.
SCHEMA = {
    "hour_utc": "int64",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
    "n_src": "int64",
}


def store_path(base_dir: str, file_stem: str) -> str:
    return os.path.join(base_dir, f"{file_stem}.parquet")


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series(dtype=dt) for name, dt in SCHEMA.items()})


def load(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return empty_frame()
    return pd.read_parquet(path).astype(SCHEMA).sort_values("hour_utc").reset_index(drop=True)


def write(path: str, frame: pd.DataFrame) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    frame.astype(SCHEMA).sort_values("hour_utc").reset_index(drop=True).to_parquet(
        path, index=False, compression="zstd")


def merge(path: str, frame: pd.DataFrame) -> int:
    """Идемпотентно доводит хранилище до объединения того, что уже есть, и
    `frame`. При совпадении hour_utc побеждает новая строка: источник мог
    пересмотреть бар, и свежая версия достовернее. Возвращает число
    добавленных строк (пересмотры существующих в счёт не идут).
    """
    if frame.empty:
        return 0
    existing = load(path)
    before = len(existing)
    combined = pd.concat([existing, frame.astype(SCHEMA)], ignore_index=True)
    combined = combined.drop_duplicates(subset="hour_utc", keep="last")
    write(path, combined)
    return len(combined) - before


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    if not candles:
        return empty_frame()
    return pd.DataFrame({
        "hour_utc": [c.open_time for c in candles],
        "open": [c.open for c in candles],
        "high": [c.high for c in candles],
        "low": [c.low for c in candles],
        "close": [c.close for c in candles],
        "volume": [c.volume for c in candles],
        "n_src": [1] * len(candles),
    }).astype(SCHEMA)


def to_hourly(frame: pd.DataFrame) -> pd.DataFrame:
    """Складывает бары произвольной внутричасовой сетки в часовые по границе
    круглого часа UTC.

    Нужно из-за того, что сетки источников не совпадают: у биржевых фондов бары
    идут по :30 (09:30, 10:30, ...), у валютных пар и крипты - по круглому часу.
    Кросс-секция - взвешенная медиана, CSV, PCA, корреляционная матрица -
    требует, чтобы "один и тот же час" означал одно и то же для всех активов,
    иначе синхронность измеряется на рядах, смещённых друг относительно друга.
    Поэтому фонды запрашиваются получасовыми барами и складываются здесь.

    Для рядов, уже стоящих на круглом часе, операция тождественна.
    """
    if frame.empty:
        return empty_frame()
    # Два бара с ОДИНАКОВЫМ hour_utc - это один и тот же бар, попавший на вход
    # дважды, а не два разных. Отбросить их надо ДО агрегации: объём
    # складывается суммой, и на дубле он бы удвоился. Ровно это и случилось бы
    # на накопленной истории, где неудачное слияние веток продублировало блок
    # из 299 часов. Побеждает последняя копия: она либо равнозначна, либо
    # полнее - более поздняя загрузка застаёт час уже закрытым.
    df = (frame.astype(SCHEMA)
          .drop_duplicates(subset="hour_utc", keep="last")
          .sort_values("hour_utc"))
    grouped = df.groupby(df["hour_utc"] // HOUR * HOUR, sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        n_src=("close", "size"),
    )
    return grouped.reset_index(names="hour_utc").astype(SCHEMA)


# Каталоги хранилища. Держатся здесь, а не в каждом вызывающем модуле, чтобы
# бэкфилл и аудит не могли разойтись в том, где лежат данные.
DEFAULT_BARS_DIR = os.path.join("data", "meals", "bars")
DEFAULT_VIX_DIR = os.path.join("data", "meals", "vix")

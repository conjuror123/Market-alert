"""Версии конфигурации и прогона (ТЗ п.6.2, п.6.3).

П.6.3 запрещает ретро-изменение параметров без пересчёта истории с новой
версией. Запрет соблюдается не дисциплиной, а устройством: config_version - это
хеш содержимого всего, что влияет на результат. Поменяли порог, окно, состав
корзины или формулу - версия изменилась сама, и старые события уже помечены
другой. Ручной счётчик здесь бесполезен: его забывают увеличить ровно тогда,
когда это важнее всего.

run_version устроен иначе. По п.6.2 повторный прогон того же часа с той же
версией не должен создавать дублей, а поздние или пересмотренные данные -
обязаны попасть в пересчёт с НОВОЙ версией. Обоим требованиям отвечает одна и
та же конструкция: run_version - хеш от config_version и отпечатка входных
данных. Прогон по неизменившимся данным даёт ту же версию и потому идемпотентен;
стоит источнику дослать или исправить бар - отпечаток меняется, и пересчёт
получает новую версию автоматически.
"""
from __future__ import annotations

import hashlib
import os

# Всё, что влияет на результат расчёта. Список намеренно явный: молчаливое
# "хешируем весь пакет" ломало бы версию от правки комментария, а хешировать
# только конфигурацию значило бы не заметить изменения формулы.
CONFIG_INPUTS = (
    os.path.join("config", "basket.yaml"),
    os.path.join("meals", "windows.py"),
    os.path.join("meals", "zscore.py"),
    os.path.join("meals", "returns.py"),
    os.path.join("meals", "volume.py"),
    os.path.join("meals", "quality.py"),
    os.path.join("meals", "residuals.py"),
    os.path.join("meals", "cross_section.py"),
    os.path.join("meals", "si_index.py"),
    os.path.join("meals", "cluster.py"),
    os.path.join("meals", "saed.py"),
    os.path.join("meals", "calendar_multiplier.py"),
    os.path.join("meals", "vix.py"),
)

# Сырые входы расчёта: всё, что приходит извне и не является результатом самой
# системы. Производные файлы - метрики активов, метрики корзины, остатки -
# сюда НЕ входят, и это принципиально. Метрики корзины прогону cluster
# одновременно вход и выход: включи их в отпечаток, и повторный запуск по тем
# же данным получил бы новую run_version просто потому, что предыдущий запуск
# переписал файл. Идемпотентность п.6.2 держится ровно на том, что версия
# зависит только от сырых данных и конфигурации, а всё остальное - их функция.
RAW_INPUTS = (
    os.path.join("data", "meals", "bars"),
    os.path.join("data", "meals", "vix"),
    os.path.join("data", "meals", "sessions"),
    os.path.join("data", "meals", "corporate_actions.csv"),
    os.path.join("data", "economic_calendar", "calendar.ndjson"),
)

VERSION_LENGTH = 12

# Размер куска при чтении данных. Файлы штучные и небольшие, но читать
# семнадцатимегабайтный календарь одним bytes-объектом незачем.
CHUNK = 1 << 20


def _digest(chunks) -> str:
    accumulator = hashlib.sha256()
    for chunk in chunks:
        accumulator.update(chunk)
        accumulator.update(b"\x00")
    return accumulator.hexdigest()[:VERSION_LENGTH]


def config_version(root: str = ".", inputs=CONFIG_INPUTS) -> str:
    """Версия конфигурации: хеш содержимого файлов, влияющих на расчёт.

    Отсутствующий файл - не повод для исключения, а часть состояния: его
    отсутствие тоже меняет версию, и это правильнее, чем упасть.
    """
    chunks = []
    for relative in sorted(inputs):
        chunks.append(relative.encode("utf-8"))
        path = os.path.join(root, relative)
        if os.path.exists(path):
            with open(path, "rb") as f:
                chunks.append(f.read())
        else:
            chunks.append(b"<absent>")
    return _digest(chunks)


def _expand(paths):
    """Каталоги раскрываются в список файлов, файлы остаются собой.

    Каталог как таковой отпечатком быть не может: его собственное время правки
    меняется только при добавлении или удалении записи, а дописанный бар внутри
    уже существующего файла его не трогает - и пересчёт по обновлённым данным
    получил бы ту же run_version, что и прогон до обновления.
    """
    for path in paths:
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for name in sorted(files):
                    yield os.path.join(root, name)
        else:
            yield str(path)


def data_fingerprint(paths=RAW_INPUTS) -> str:
    """Отпечаток входных данных: хеш их СОДЕРЖИМОГО.

    Размер и время правки были бы дешевле, но неверны в обе стороны. Прогон
    бэкфилла переписывает файл теми же барами - размер тот же, время новое, и
    пересчёт по неизменившимся данным получил бы новую версию, то есть
    идемпотентности п.6.2 не было бы вовсе. Наоборот, исправленный вендором бар
    той же длины оставил бы размер прежним, и правка могла бы проскочить
    незамеченной, если файл переписан в ту же секунду.

    Платим за это сотней миллисекунд: сырых данных здесь около тридцати
    мегабайт, а производные файлы в отпечаток не входят (см. RAW_INPUTS).
    """
    chunks = []
    for path in sorted(set(_expand(paths))):
        chunks.append(str(path).encode("utf-8"))
        if os.path.exists(path):
            with open(path, "rb") as f:
                while True:
                    block = f.read(CHUNK)
                    if not block:
                        break
                    chunks.append(block)
        else:
            chunks.append(b"<absent>")
    return _digest(chunks)


def run_version(config: str, fingerprint: str) -> str:
    """Версия прогона. Одинаковые вход и конфигурация дают одинаковую версию -
    отсюда идемпотентность по п.6.2."""
    return _digest([config.encode("utf-8"), fingerprint.encode("utf-8")])


def versions_for(data_paths=RAW_INPUTS, root: str = ".") -> tuple[str, str]:
    config = config_version(root)
    return config, run_version(config, data_fingerprint(data_paths))

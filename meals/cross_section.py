"""Кросс-секция корзины: кворум, M_t, CSV, PCA (ТЗ п.2.3, 3.2-3.4).

Здесь система впервые перестаёт смотреть на активы по отдельности и начинает
измерять то, ради чего она вообще строится: НАСКОЛЬКО СОГЛАСОВАННО движется
рынок целиком. Одиночный всплеск отдельного актива - это Ф2 и модуль SAED;
кластерное событие - это когда несколько блоков дёргаются разом, и увидеть это
можно только глядя на все ряды одновременно.

Два независимых способа поймать одну и ту же согласованность:

- СЖАТИЕ разброса (п.3.2). Обычно активы расходятся: у каждого своя новость,
  свой поток заявок. Когда приходит общий макрофактор, разброс между ними
  схлопывается - все идут в одну сторону примерно одинаково, - и при этом сама
  корзина заметно смещается. Узкий разброс при крупном общем сдвиге и есть
  подпись общего фактора.
- СИНХРОННОСТЬ по PCA (п.3.3). То же самое с другой стороны: если первая
  главная компонента объясняет непривычно большую долю дисперсии, значит
  движением всех активов заправляет одна общая причина.

По п.3.4 они объединяются через ИЛИ и логируются раздельно - а на бэктесте
корреляция их срабатываний обязана быть проверена: если она выше 0.7, одно из
подусловий исключается из продуктивной конфигурации как дублирующее.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from meals import windows
from meals.basket import Basket

# Кворум часа (п.2.3).
QUORUM_MIN_ASSETS = 8
QUORUM_MIN_TIER1 = 2
QUORUM_MIN_BLOCKS = 2
QUORUM_MIN_PER_BLOCK = 2

# Обусловленность PCA (п.3.3).
PCA_MIN_ASSETS = 3
PCA_MIN_ROWS = 60
PCA_ROWS_PER_ASSET = 3
CONSTANT_COLUMN_STD = 1e-12

# Надбавка к медиане в пороге синхронности (п.3.3).
PCA_SYNC_MARGIN = 0.05

# Сколько определённых значений PC1_ratio должно попасть в окно W_cs, чтобы
# перцентильный порог имел смысл.
#
# Окно по ТЗ - 1200 ч.э.к., и менять его нельзя: это календарный охват
# статистики, десять торговых недель. Но PC1_ratio существует только в часы
# полного режима, а их около 28% - в окне из 1200 часов оказывается порядка 335
# значений. Требовать 1200 НАБЛЮДЕНИЙ внутри 1200 ЧАСОВ значит требовать
# невозможного: при таком условии порог не считается ни разу за всю историю.
# Поэтому охват остаётся прежним, а требование предъявляется к числу значений,
# и оно взято таким, чтобы 95-й перцентиль опирался хотя бы на пять точек в
# хвосте.
PCA_STAT_MIN_OBS = 100


def weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    """Взвешенная медиана: значение, слева от которого лежит половина веса.

    Медиана, а не среднее, потому что M_t обязана описывать корзину ЦЕЛИКОМ, а
    не поддаваться одному активу, который сегодня улетел. Взвешенная - потому
    что вес актива задан правилом равновесности блоков (п.2.3), и без весов
    блок из шести валютных пар перевешивал бы блок из трёх криптоактивов
    просто числом участников.
    """
    order = np.argsort(values)
    v, w = values[order], weights[order]
    cumulative = np.cumsum(w)
    half = cumulative[-1] / 2
    index = int(np.searchsorted(cumulative, half))
    if index > 0 and np.isclose(cumulative[index - 1], half):
        # Вес делится ровно пополам - берём середину между соседями, чтобы
        # результат не зависел от порядка сортировки равных весов.
        return float((v[index - 1] + v[index]) / 2)
    return float(v[min(index, len(v) - 1)])


def build_panel(metrics: dict[str, pd.DataFrame], column: str = "r") -> pd.DataFrame:
    """Широкая панель: строки - часы, столбцы - активы, значения - `column`.

    Пропуски остаются пропусками. Заполнять их нулями по п.3.3 ЗАПРЕЩЕНО: ноль
    это утверждение "актив не двигался", а его отсутствие означает "мы не
    знаем", и подмена одного другим завышает согласованность корзины ровно там,
    где данных нет.
    """
    series = {aid: df.set_index("hour_utc")[column] for aid, df in metrics.items()}
    return pd.DataFrame(series).sort_index()


def quorum(panel: pd.DataFrame, basket: Basket) -> pd.DataFrame:
    """Кворум часа по п.2.3: достаточно ли активов, тиров и блоков, чтобы час
    вообще имел смысл оценивать."""
    present = panel.notna()
    blocks = {a.asset_id: a.block for a in basket.assets}
    tier1 = [a.asset_id for a in basket.assets if a.tier == 1]
    columns = [c for c in panel.columns if c in blocks]
    present = present[columns]

    n_assets = present.sum(axis=1)
    n_tier1 = present[[c for c in columns if c in tier1]].sum(axis=1)

    per_block = {}
    for block in {blocks[c] for c in columns}:
        members = [c for c in columns if blocks[c] == block]
        per_block[block] = present[members].sum(axis=1)
    block_counts = pd.DataFrame(per_block)
    populated = (block_counts >= QUORUM_MIN_PER_BLOCK).sum(axis=1)

    ok = ((n_assets >= QUORUM_MIN_ASSETS)
          & (n_tier1 >= QUORUM_MIN_TIER1)
          & (populated >= QUORUM_MIN_BLOCKS))
    return pd.DataFrame({
        "quorum_ok": ok, "n_assets": n_assets, "n_tier1": n_tier1,
        "n_blocks": (block_counts > 0).sum(axis=1), "n_blocks_populated": populated,
    })


def basket_median(panel: pd.DataFrame, basket: Basket) -> pd.Series:
    """M_t - взвешенная медианная доходность корзины (п.2.3)."""
    weights = basket.weights()
    columns = [c for c in panel.columns if c in weights]
    w = np.array([weights[c] for c in columns])
    values = panel[columns].to_numpy(dtype="float64")

    out = np.full(len(panel), np.nan)
    for i in range(len(panel)):
        mask = np.isfinite(values[i])
        if mask.any():
            out[i] = weighted_median(values[i][mask], w[mask])
    return pd.Series(out, index=panel.index)


def block_factors(panel: pd.DataFrame, basket: Basket) -> pd.DataFrame:
    """Фактор собственного блока для каждого инструмента, БЕЗ него самого.

    Отступление от п.3.6, где регрессор один - фактор корзины. Причина
    измерена, а не предположена: в часы, когда срабатывают четыре и более
    валютные пары, в 97% случаев все они согласны по направлению доллара, а у
    крипты согласие по знаку остатка стопроцентное по медиане. Это не
    независимые идиосинкратические движения, а одно движение блока, которое
    фактор корзины не поглотил и которое целиком утекло в остатки всех его
    участников. Взвешенная медиана по пяти блокам почти не сдвигается, когда
    ходит один блок весом в одну пятую - и модуль, задуманный ловить
    ОДИНОЧНЫЕ движения, систематически срабатывал блоками.

    Величина M_block,t в ТЗ определена (п.2.3), но зарезервирована под разметку
    истины в п.7. Здесь она становится вторым регрессором.

    Исключение самого актива обязательно. Иначе в блоке из трёх криптоактивов
    инструмент на треть вычитал бы сам себя, и собственное движение частично
    исчезало бы из остатка - ровно та ошибка, от которой в п.3.6 защищает
    оценка беты на данных до текущего бара.

    Медиана здесь обычная, а не взвешенная, и это не упрощение: по правилу
    равновесности п.2.3 веса всех активов внутри блока равны между собой, так
    что взвешенная медиана блока совпадает с обычной.
    """
    members: dict[str, list[str]] = {}
    for asset in basket.assets:
        if asset.asset_id in panel.columns:
            members.setdefault(asset.block, []).append(asset.asset_id)

    out = pd.DataFrame(index=panel.index, dtype="float64")
    for block, columns in members.items():
        values = panel[columns].to_numpy(dtype="float64")
        for position, asset_id in enumerate(columns):
            others = np.delete(values, position, axis=1)
            with np.errstate(invalid="ignore"):
                out[asset_id] = np.nanmedian(others, axis=1) if others.size else np.nan

        # Инструменты вне корзины в фактор не входят (п.8.1), поэтому для них
        # исключать нечего - берётся медиана блока целиком.
        for asset in basket.outside:
            if asset.block == block:
                out[asset.asset_id] = np.nanmedian(values, axis=1)
    return out


def cross_sectional_volatility(panel: pd.DataFrame, sigma_panel: pd.DataFrame,
                               basket: Basket) -> pd.DataFrame:
    """CSV и CSV_norm по п.3.2.

    CSV - разброс доходностей активов ВНУТРИ часа. Нормируется на среднюю
    собственную волатильность тех же активов, иначе величина мерила бы не
    согласованность, а просто бурность рынка: в шторм разброс велик у всех.
    """
    columns = [c for c in panel.columns if c in {a.asset_id for a in basket.assets}]
    values = panel[columns]
    csv = values.std(axis=1, ddof=1)  # ddof=1 по п.1.2
    ewma_volatility = sigma_panel[columns].where(values.notna()).mean(axis=1)
    return pd.DataFrame({
        "csv": csv,
        "ewma_volatility": ewma_volatility,
        "csv_norm": csv / ewma_volatility,
    })


def csv_compression(csv_norm: pd.Series, m: pd.Series,
                    window: int = windows.W_CS) -> pd.Series:
    """Подусловие сжатия (п.3.2): разброс необычно узок И корзина заметно
    сдвинулась. Одного узкого разброса мало - тихий час тоже узок, но в нём
    ничего не происходит."""
    rolling = csv_norm.shift(1).rolling(window, min_periods=window)
    q10 = rolling.quantile(0.10)
    m_std = m.shift(1).rolling(window, min_periods=window).std(ddof=1)
    result = (csv_norm < q10) & (m.abs() > 2 * m_std)
    return (result.where(q10.notna() & m_std.notna(), pd.NA).astype("boolean"), q10)


def full_basket_regime(quorum_frame: pd.DataFrame, basket: Basket) -> pd.Series:
    """Часы, в которых торгует ВСЯ корзина - все блоки представлены.

    У корзины два режима, и это следствие состава: биржевые фонды торгуют 6.5
    часа, валютные пары 24/5, крипта круглосуточно. В американскую сессию
    работают все пять блоков, остальные семнадцать часов суток - только
    валютные пары и крипта.
    """
    return quorum_frame["n_blocks_populated"] >= len(basket.by_block())


def pc1_ratio(panel: pd.DataFrame, quorum_ok: pd.Series, basket: Basket,
              window: int = windows.W_PCA,
              regime: pd.Series | None = None) -> pd.Series:
    """Доля дисперсии, объяснённая первой главной компонентой (п.3.3).

    Считается по КОРРЕЛЯЦИОННОЙ матрице, а не по ковариационной: иначе самый
    волатильный актив в одиночку определял бы первую компоненту, и величина
    измеряла бы его размах, а не общность движения.

    В окно берутся только активы с валидным баром во ВСЕХ включённых часах:
    неполный столбец сделал бы корреляции между парами несопоставимыми,
    посчитанными на разных подмножествах времени.

    Отсюда следует ограничение, которого п.3.3 не предвидит, потому что
    предполагает у всех активов одну сессию. У нас их две. Любое окно из 120
    часов задевает ночь, когда фонды закрыты, поэтому требование полноты
    выбрасывает из матрицы ВСЕ фонды - проверено на реальных данных: в окне
    остаются ровно шесть валютных пар и три криптоактива, и так в каждом окне.
    Синхронность блоков equity, rates и commodities не измерялась бы никогда,
    хотя ради неё кластерный детектор и строится.

    Поэтому окно набирается из часов ОДНОГО режима - тех, где торгует вся
    корзина. Тогда все активы полны, корреляции сопоставимы, а PC1_ratio
    измеряет то, что должен: общность движения по всем блокам. Ночью величина
    остаётся NULL, и по п.3.4 триггер однофакторности опирается на одно лишь
    сжатие - этот случай спецификация оговаривает прямо.

    Смешивать режимы в одном ряду было бы хуже, чем не считать вовсе: порог
    синхронности - скользящий перцентиль самого PC1_ratio, и на ряду, где
    чередуются два разных типичных уровня, он описывал бы пропорцию режимов, а
    не аномалию.
    """
    columns = [c for c in panel.columns if c in {a.asset_id for a in basket.assets}]
    values = panel[columns]
    mask = quorum_ok.reindex(values.index, fill_value=False)
    if regime is not None:
        mask = mask & regime.reindex(values.index, fill_value=False)
    eligible = values[mask]

    out = pd.Series(np.nan, index=panel.index)
    # Средняя парная корреляция (п.6.5) считается здесь же: корреляционная
    # матрица для неё уже построена, и отдельный проход по тем же окнам стоил
    # бы столько же, сколько весь PCA.
    mean_corr = pd.Series(np.nan, index=panel.index)
    positions = {h: i for i, h in enumerate(eligible.index)}
    matrix = eligible.to_numpy(dtype="float64")

    for hour in panel.index:
        end = positions.get(hour)
        if end is None or end + 1 < window:
            continue
        block = matrix[end + 1 - window: end + 1]
        complete = ~np.isnan(block).any(axis=0)
        block = block[:, complete]
        if block.shape[1] < PCA_MIN_ASSETS:
            continue

        std = block.std(axis=0, ddof=1)
        block = block[:, std > CONSTANT_COLUMN_STD]
        n_assets = block.shape[1]
        if n_assets < PCA_MIN_ASSETS:
            continue
        if block.shape[0] < max(PCA_ROWS_PER_ASSET * n_assets, PCA_MIN_ROWS):
            continue

        correlation = np.corrcoef(block, rowvar=False)
        eigenvalues = np.linalg.eigvalsh(correlation)
        total = eigenvalues.sum()
        if total > 0:
            out[hour] = float(eigenvalues[-1] / total)
        # Среднее арифметическое элементов ВНЕ главной диагонали. Диагональ -
        # это корреляция актива с самим собой, единица по построению, и её
        # включение просто подтягивало бы среднее вверх тем сильнее, чем меньше
        # активов в окне.
        off_diagonal = correlation[~np.eye(n_assets, dtype=bool)]
        if off_diagonal.size:
            mean_corr[hour] = float(np.nanmean(off_diagonal))
    return out, mean_corr


def pca_sync(ratio: pd.Series, window: int = windows.W_CS) -> pd.Series:
    """Подусловие синхронности (п.3.3). Порог - максимум из перцентиля и
    медианы с надбавкой: одного перцентиля мало, потому что в затяжной период
    высокой связности он подтягивается вверх и перестаёт что-либо отсекать."""
    rolling = ratio.shift(1).rolling(window, min_periods=PCA_STAT_MIN_OBS)
    threshold = np.maximum(rolling.quantile(0.95), rolling.median() + PCA_SYNC_MARGIN)
    result = ratio > threshold
    return (result.where(threshold.notna() & ratio.notna(), pd.NA).astype("boolean"),
            threshold)


def single_factor(compression: pd.Series, sync: pd.Series) -> pd.Series:
    """Единый триггер однофакторности (п.3.4): сжатие ИЛИ синхронность.

    Если PC1_ratio не оценён, синхронность логируется как NULL, а значение
    триггера равно одному лишь сжатию - по п.3.4 час, прошедший кворум, обязан
    получить определённое значение, иначе он не даст слагаемого в SI-Index.
    """
    filled_sync = sync.fillna(False).astype(bool)
    result = compression.fillna(False).astype(bool) | filled_sync
    # NULL остаётся только там, где НИ ОДНО подусловие не оценено.
    unknown = compression.isna() & sync.isna()
    return result.where(~unknown, pd.NA).astype("boolean")


def build_basket_metrics(metrics: dict[str, pd.DataFrame], basket: Basket,
                         reference_hours: pd.Index | None = None) -> pd.DataFrame:
    """Собирает metrics_basket_hour (п.6.4): кворум, M_t, CSV, PCA и триггер
    однофакторности на общей часовой сетке."""
    panel = build_panel(metrics, "r")
    sigma_panel = build_panel(metrics, "sigma_eff")
    if reference_hours is not None:
        panel = panel.loc[panel.index.intersection(reference_hours)]
        sigma_panel = sigma_panel.reindex(panel.index)

    quorum_frame = quorum(panel, basket)
    ok = quorum_frame["quorum_ok"]
    m = basket_median(panel, basket)
    csv_frame = cross_sectional_volatility(panel, sigma_panel, basket)

    regime = full_basket_regime(quorum_frame, basket)
    ratio, mean_corr = pc1_ratio(panel, ok, basket, regime=regime)

    # Час без кворума не оценивается вовсе: по п.2.3 все кластерные триггеры
    # получают NULL, а не False.
    compression, compression_threshold = csv_compression(csv_frame["csv_norm"], m)
    compression = compression.where(ok, pd.NA)
    sync, sync_threshold = pca_sync(ratio)
    sync = sync.where(ok, pd.NA)

    out = pd.concat([quorum_frame, csv_frame], axis=1)
    out["m_weighted_median"] = m
    out["pc1_ratio"] = ratio
    out["pc1_threshold"] = sync_threshold
    out["mean_pairwise_corr"] = mean_corr
    out["in_full_regime"] = regime
    out["csv_norm_q10"] = compression_threshold
    out["csv_compression"] = compression.astype("boolean")
    out["pca_sync"] = sync.astype("boolean")
    out["single_factor"] = single_factor(compression, sync).where(ok, pd.NA).astype("boolean")
    return out


def subcondition_correlation(frame: pd.DataFrame) -> float:
    """Корреляция срабатываний сжатия и синхронности (п.3.4).

    Проверка обязательна на бэктесте: если подусловия срабатывают почти всегда
    вместе, второе не добавляет информации, а лишь удваивает вес одного и того
    же наблюдения в SI-Index. По ТЗ при корреляции выше 0.7 одно из них
    исключается из продуктивной конфигурации.

    Считается только по часам, где оценены ОБА - там, где PC1_ratio не
    определён, сравнивать не с чем.
    """
    both = frame[["csv_compression", "pca_sync"]].dropna()
    if both.empty or both.nunique().min() < 2:
        return float("nan")
    return float(both["csv_compression"].astype(float).corr(
        both["pca_sync"].astype(float)))


DEFAULT_BASKET_METRICS_PATH = os.path.join("data", "meals", "metrics_basket_hour.parquet")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging

    from meals import pipeline, sessions
    from meals.basket import load_basket

    parser = argparse.ArgumentParser(description="Метрики корзины по часам (п.3.2-3.4)")
    parser.add_argument("--metrics-dir", default=pipeline.DEFAULT_METRICS_DIR)
    parser.add_argument("--out", default=DEFAULT_BASKET_METRICS_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("meals.cross_section")

    basket = load_basket()
    metrics = pipeline.load_all(basket, args.metrics_dir)
    if not metrics:
        log.error("Нет метрик по активам - сначала запустите python -m meals.pipeline")
        return 2

    panel_hours = build_panel(metrics, "r").index
    reference = pd.Index([h for h in panel_hours
                          if sessions.is_reference_hour(int(h), basket.anchor_exchange_tz)])
    frame = build_basket_metrics(metrics, basket, reference)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    frame.reset_index(names="hour_utc").to_parquet(args.out, index=False,
                                                   compression="zstd")

    correlation = subcondition_correlation(frame)
    log.info("часов %d, кворум %d, сжатие %d, синхронность %d, однофакторность %d",
             len(frame), int(frame["quorum_ok"].sum()),
             int(frame["csv_compression"].sum()), int(frame["pca_sync"].sum()),
             int(frame["single_factor"].sum()))
    log.info("корреляция подусловий (п.3.4): %s",
             f"{correlation:.4f}" if correlation == correlation
             else "не определена - одно из подусловий не сработало ни разу")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

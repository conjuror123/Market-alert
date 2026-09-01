"""Множитель стресса по VIX (ТЗ п.4.4).

Условие одностороннее, и это принципиально: страх и облегчение - не
симметричные состояния рынка. Резкий рост VIX означает, что участники платят за
защиту, то есть считают ближайшее будущее опасным; такой же по величине спад
означает лишь возвращение к норме. Поэтому падение VIX стрессом не считается и
множителя не даёт.

Окно фиксированное и НЕ продлевается повторными скачками. Иначе затяжной период
высокой волатильности - когда VIX дёргается вверх каждый день - держал бы
множитель включённым неделями, и он перестал бы отличать острый момент от
общего фона. Повторы внутри окна считаются и логируются, но окно не двигают.

Отступление от буквы п.4.4, зафиксированное в docs/meals-otstupleniya.md: ряд
дневной, потому что внутридневного VIX нет ни у одного доступного источника, а
момент начала окна - это момент, когда значение стало ИЗВЕСТНО системе, а не
дата наблюдения. FRED публикует значение на следующий рабочий день, и отсчёт от
даты наблюдения означал бы, что бэктест пользуется тем, чего в тот час ещё не
существовало.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from meals import windows, zscore

# Величина множителя и длина окна. Обе помечены в ТЗ звёздочкой.
M_VIX = 1.3
WINDOW_HOURS = windows.VIX_WINDOW

# Порог абсолютной ноги для ряда VIX (п.4.4): 1.5 * sigma_LT.
ABS_LEG = windows.ABS_LEG_Q95


@dataclass(frozen=True)
class VixWindow:
    opened_at: int      # момент, с которого множитель действует (epoch UTC)
    closes_at: int      # момент, после которого он снова равен 1.0
    spike_count: int    # сколько скачков пришлось на это окно, включая первый


def load_series(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Нет ряда VIX {path}. Загрузить: python -m meals.backfill")
    return pd.read_parquet(path).sort_values("day").reset_index(drop=True)


def score(series: pd.DataFrame, window: int = windows.SIGMA_LT_MIN_BARS) -> pd.DataFrame:
    """Прогоняет ряд VIX через аппарат п.3.1 с собственными состояниями.

    Окно порогов для дневного ряда - 720 баров: по п.2.7 это
    max(120 * B_asset, 720), а у дневного ряда в сутках один бар, так что
    работает нижняя граница.
    """
    out = series.copy()
    out["r"] = np.log(out["close"] / out["close"].shift(1))
    out["sigma_lt"] = (out["r"].shift(1)
                       .rolling(windows.SIGMA_LT_BARS,
                                min_periods=windows.SIGMA_LT_MIN_BARS).std(ddof=1))

    from meals.returns import _rolling_mad

    mad_24 = _rolling_mad(out["r"], windows.MAD_WINDOW)
    mad_eff = np.maximum(mad_24, 0.2 * out["sigma_lt"])
    limit = 5 * mad_eff
    out["r_w"] = np.where(out["r"].abs() > limit, np.sign(out["r"]) * limit, out["r"])
    out.loc[mad_eff.isna(), "r_w"] = out["r"][mad_eff.isna()]

    z, sigma_eff = zscore.ewma_state(out["r"].to_numpy(dtype="float64"),
                                     out["r_w"].to_numpy(dtype="float64"),
                                     out["sigma_lt"].to_numpy(dtype="float64"))
    out["z"] = z
    out.loc[out["sigma_lt"].isna(), "z"] = np.nan
    q95, _ = zscore.adaptive_thresholds(out["z"].abs(), window)
    out["q95"] = q95

    # Все три условия одновременно, и Z берётся СО ЗНАКОМ.
    out["is_spike"] = ((out["z"] > out["q95"])
                       & (out["r"] > 0)
                       & (out["r"].abs() >= ABS_LEG * out["sigma_lt"]))
    return out


def windows_from_spikes(scored: pd.DataFrame, reference_hours: np.ndarray,
                        window_hours: int = WINDOW_HOURS) -> list[VixWindow]:
    """Строит окна действия множителя.

    Новое окно открывается только скачком ПОСЛЕ закрытия предыдущего; скачки
    внутри действующего окна лишь увеличивают счётчик.
    """
    reference = np.asarray(sorted(reference_hours))
    result: list[VixWindow] = []
    for _, row in scored[scored["is_spike"].fillna(False)].iterrows():
        opened = int(row["available_at"])
        if result and opened < result[-1].closes_at:
            last = result[-1]
            result[-1] = VixWindow(last.opened_at, last.closes_at, last.spike_count + 1)
            continue
        # Конец окна - через 24 часа ЭТАЛОННОГО КАЛЕНДАРЯ, а не 24 календарных:
        # выходные в счёт не идут.
        start = int(np.searchsorted(reference, opened, side="left"))
        end_index = start + window_hours
        closes = (int(reference[end_index]) if end_index < len(reference)
                  else int(reference[-1]) + 3600)
        result.append(VixWindow(opened, closes, 1))
    return result


def multiplier_series(hours_utc, vix_windows: list[VixWindow]) -> dict[int, float]:
    """Множитель по часам: 1.3 внутри окна, 1.0 вне его."""
    result = {int(h): 1.0 for h in hours_utc}
    if not vix_windows:
        return result
    hours = np.array(sorted(result))
    for window in vix_windows:
        inside = hours[(hours >= window.opened_at) & (hours < window.closes_at)]
        for hour in inside:
            result[int(hour)] = M_VIX
    return result

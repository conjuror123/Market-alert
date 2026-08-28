"""Anomaly detection: is the latest price move / volume abnormal for THIS asset,
based on its own recent volatility and volume behaviour rather than a fixed % threshold.

Two independent signals have to agree before a price move is flagged:

1. EWMA volatility z-score - an adaptive estimate of "how big moves normally are for
   this asset right now" (RiskMetrics-style exponentially weighted variance). Reacts
   quickly to volatility regime changes (e.g. an asset that has been calm vs. one that
   has been choppy all week).
2. Robust baseline z-score - median/MAD (median absolute deviation) over a longer
   rolling window. MAD is not distorted by a handful of previous spikes the way a
   plain standard deviation would be, so it acts as a sanity check on signal (1).

Volume is scored the same way (robust median/MAD z-score) and, combined with even a
moderate price move, is used to flag volume surges that a pure price-based check
would miss (e.g. accumulation/distribution before a breakout).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from price_monitor.models import Candle

MAD_CONSISTENCY_CONST = 1.4826  # scales MAD to be comparable to a normal std-dev


@dataclass
class Signal:
    symbol: str
    last_close: float
    last_return_pct: float
    ewma_z: float
    robust_z: float
    volume_z: float
    price_alert: bool
    volume_alert: bool
    reasons: list[str]

    @property
    def is_alert(self) -> bool:
        return self.price_alert or self.volume_alert


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def ewma_volatility(returns: list[float], lam: float) -> float:
    """Recursive RiskMetrics EWMA variance estimate over `returns`, returned as std-dev."""
    if not returns:
        return 0.0
    var = returns[0] ** 2
    for r in returns[1:]:
        var = lam * var + (1 - lam) * r ** 2
    return math.sqrt(var)


def robust_z_score(value: float, history: list[float]) -> float:
    """Median/MAD-based z-score of `value` against `history` (value excluded).

    Falls back to a population std-dev z-score if the history has zero MAD (a
    degenerate, perfectly flat window), and to a large sign-aware sentinel if even
    that is zero (a genuinely constant history with a clearly different new value).
    """
    if len(history) < 2:
        return 0.0
    sorted_hist = sorted(history)
    n = len(sorted_hist)
    median = sorted_hist[n // 2] if n % 2 else (sorted_hist[n // 2 - 1] + sorted_hist[n // 2]) / 2
    deviations = sorted([abs(x - median) for x in history])
    mad = (
        deviations[n // 2]
        if n % 2
        else (deviations[n // 2 - 1] + deviations[n // 2]) / 2
    )
    mad_scaled = MAD_CONSISTENCY_CONST * mad
    if mad_scaled > 0:
        return (value - median) / mad_scaled

    mean = sum(history) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in history) / n)
    if std > 0:
        return (value - mean) / std
    if value == median:
        return 0.0
    return math.copysign(1e6, value - median)


def analyze(
    symbol: str,
    candles: list[Candle],
    ewma_lambda: float,
    mad_window: int,
    price_zscore_threshold: float,
    volume_zscore_threshold: float,
    volume_min_price_move_z: float,
    min_history: int,
) -> Signal | None:
    """Analyze the most recent closed candle against the asset's own recent history.

    Returns None if there isn't enough history yet to make a confident judgement.
    """
    if len(candles) < min_history + 1:
        return None

    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]

    returns = log_returns(closes)
    last_return = returns[-1]
    history_returns = returns[:-1][-mad_window:]

    ewma_sigma = ewma_volatility(history_returns, ewma_lambda)
    ewma_z = last_return / ewma_sigma if ewma_sigma > 0 else 0.0
    robust_z = robust_z_score(last_return, history_returns)

    last_volume = volumes[-1]
    history_volumes = volumes[:-1][-mad_window:]
    volume_z = robust_z_score(last_volume, history_volumes)

    price_alert = abs(ewma_z) >= price_zscore_threshold and abs(robust_z) >= price_zscore_threshold
    volume_alert = (
        volume_z >= volume_zscore_threshold
        and max(abs(ewma_z), abs(robust_z)) >= volume_min_price_move_z
    )

    reasons = []
    if price_alert:
        direction = "рост" if last_return > 0 else "падение"
        reasons.append(
            f"аномальное {direction} цены: EWMA z={ewma_z:.2f}, робастный z={robust_z:.2f} "
            f"(порог {price_zscore_threshold})"
        )
    if volume_alert:
        reasons.append(
            f"всплеск объёма: z={volume_z:.2f} (порог {volume_zscore_threshold}) "
            f"на фоне движения цены z={max(abs(ewma_z), abs(robust_z)):.2f}"
        )

    return Signal(
        symbol=symbol,
        last_close=closes[-1],
        last_return_pct=(math.exp(last_return) - 1) * 100,
        ewma_z=ewma_z,
        robust_z=robust_z,
        volume_z=volume_z,
        price_alert=price_alert,
        volume_alert=volume_alert,
        reasons=reasons,
    )

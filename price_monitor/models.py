"""Shared data types used across market data sources."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


class ExchangeError(RuntimeError):
    pass

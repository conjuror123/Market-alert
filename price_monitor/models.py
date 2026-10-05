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


class Unreachable(ExchangeError):
    """No answer on any attempt: each timed out, failed to connect or got a
    server error (5xx). A refusal (404, 400) is an answer, not this.

    Each costs about a minute and a half of timeouts and retries, and the hourly
    job has twenty, so a provider that does this to UNANSWERED_IN_A_ROW requests
    in a row is not asked again in the run (jump.backfill, jump.verify)."""


UNANSWERED_IN_A_ROW = 2

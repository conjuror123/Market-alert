"""Dispatches candle fetching to the right market data source per asset."""
from __future__ import annotations

import requests

from price_monitor import coinbase, yahoo
from price_monitor.config import AssetConfig, Config
from price_monitor.models import Candle, ExchangeError

SOURCES = {
    "coinbase": (coinbase.fetch_klines, lambda cfg: cfg.coinbase_base_url),
    "yahoo": (yahoo.fetch_klines, lambda cfg: cfg.yahoo_base_url),
}


def fetch_candles(
    asset: AssetConfig, cfg: Config, session: requests.Session | None = None
) -> list[Candle]:
    try:
        fetch_fn, base_url_fn = SOURCES[asset.source]
    except KeyError as exc:
        raise ExchangeError(
            f"Unknown source '{asset.source}' for {asset.symbol}. Supported: {sorted(SOURCES)}"
        ) from exc

    params = cfg.params_for(asset)
    return fetch_fn(
        symbol=asset.symbol,
        interval=params.interval,
        limit=params.lookback,
        base_url=base_url_fn(cfg),
        session=session,
    )

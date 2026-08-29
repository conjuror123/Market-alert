"""Dispatches candle fetching to the right market data source per asset."""
from __future__ import annotations

import requests

from price_monitor import coinbase, twelvedata, yahoo
from price_monitor.config import AssetConfig, Config
from price_monitor.models import Candle, ExchangeError

# Each source maps to its fetch function plus the extra (non symbol/interval/
# limit/session) kwargs it needs from Config - a plain base_url for most
# sources, but twelvedata also needs its API key.
SOURCES = {
    "coinbase": (coinbase.fetch_klines, lambda cfg: {"base_url": cfg.coinbase_base_url}),
    "yahoo": (yahoo.fetch_klines, lambda cfg: {"base_url": cfg.yahoo_base_url}),
    "twelvedata": (twelvedata.fetch_klines, lambda cfg: {
        "base_url": cfg.twelvedata_base_url, "api_key": cfg.twelvedata_api_key,
    }),
}


def fetch_candles(
    asset: AssetConfig, cfg: Config, session: requests.Session | None = None
) -> list[Candle]:
    try:
        fetch_fn, extra_kwargs_fn = SOURCES[asset.source]
    except KeyError as exc:
        raise ExchangeError(
            f"Unknown source '{asset.source}' for {asset.symbol}. Supported: {sorted(SOURCES)}"
        ) from exc

    params = cfg.params_for(asset)
    return fetch_fn(
        symbol=asset.symbol,
        interval=params.interval,
        limit=params.lookback,
        session=session,
        **extra_kwargs_fn(cfg),
    )

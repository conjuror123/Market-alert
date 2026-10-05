"""Basket configuration: the instruments, their blocks and providers, and the
detector's settings (config/basket.yaml).

An asset has no thresholds of its own, and cannot have any: every instrument is
judged against its own half-year by the same rule (tremor.jumps, whose settings
sit under `detector:`), so the configuration describes only the COMPOSITION and
the PROPERTIES of the instruments, not the sensitivity to them.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime

import yaml

DEFAULT_BASKET_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "basket.yaml")

# The blocks group instruments by what moves them: gold and crude do not answer
# to the same news, so commodities are four blocks, and credit is apart from
# rates. Reports count by block, and the funds' two consolidated feeds alternate
# within each block so an outage of either leaves every block reporting.
BLOCKS = ("equity", "rates", "credit", "energy", "precious_metals",
          "industrial_metals", "agriculture", "FX", "crypto")

# A block of one is a typo in basket.yaml, not a group.
BLOCK_MIN_MEMBERS = 2

# WHAT `source` IS, AND WHAT IT IS NOT. It names the store, not the server.
# `asset_id` and `file_stem` are both built from it, so every bar on disk, every
# event ever exported is keyed by it - which makes
# it an identity, and identities cannot be edited to follow an implementation
# detail. Moving XLK from Twelve Data to Yahoo by rewriting `source` would point
# the fetch at an empty store and orphan six years of history and every verdict
# recorded against it.
#
# `provider` is who actually answers the request, and that CAN change: see
# docs/manual.md, "Data in", for the current split. It defaults
# to `source`, which is why the crypto rows need no provider line.
SOURCES = ("twelvedata", "binance", "yahoo", "sina")

# Who can be asked for bars. Wider than SOURCES because a provider may serve an
# instrument whose history came from somewhere else.
PROVIDERS = ("twelvedata", "binance", "tiingo", "alpaca", "yahoo", "sifting", "google",
             "sina")
FETCH_INTERVALS = ("30min", "1h")


class BasketConfigError(ValueError):
    """The basket configuration is invalid and cannot be used."""


@dataclass(frozen=True)
class Asset:
    ticker: str
    # The store's identity. See SOURCES - this is not necessarily who serves it.
    source: str
    block: str
    has_volume: bool
    session_template: str
    fetch_interval: str
    label: str
    in_basket: bool
    # The source's quote step. The bar gate allows half of it as rounding when
    # it checks a bar's OHLC (tremor.quality.ohlc_inconsistent). It is checked
    # against real data (tremor.audit) rather than taken from the exchange
    # specification: the value must match the quote format the source actually
    # serves.
    tick_size: float
    # Who is asked for the bars. Defaults to `source` when the configuration
    # does not say otherwise.
    provider: str = ""

    @property
    def fetched_from(self) -> str:
        """The provider to request bars from, which may not be `source`."""
        return self.provider or self.source

    @property
    def asset_id(self) -> str:
        """Logical identifier used in metrics, logs and events."""
        return f"{self.source}:{self.ticker}"

    @property
    def file_stem(self) -> str:
        """File name in the store, built from `source`: an identity, never a
        provider."""
        return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{self.source}_{self.ticker}")


@dataclass(frozen=True)
class VolatilityIndex:
    """External stress indicator. Not part of the basket."""
    series_id: str
    source: str
    interval: str
    label: str
    # Its own history depth: a daily series needs 720 observations to warm up,
    # almost three years, before the fear-gauge line on the weekly note can
    # speak (tremor.vix). See basket.yaml.
    history_since: date

    @property
    def file_stem(self) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{self.source}_{self.series_id}")


@dataclass(frozen=True)
class Basket:
    assets: tuple[Asset, ...]
    outside: tuple[Asset, ...]
    volatility_index: VolatilityIndex
    anchor_exchange_tz: str
    history_since: date
    session_templates: dict
    # How far back to ACQUIRE: "take whatever the sources will still give".
    # Free archives freeze - Dukascopy publishes whole months only, and a file
    # not taken now may not be takeable later - so bars not stored now may not
    # be recoverable later. history_since is only its fallback.
    #
    # Last and optional so that a hand-built Basket falls back to
    # history_since.
    fetch_since: date | None = None

    @property
    def acquire_since(self) -> date:
        """The acquisition floor: fetch_since, or history_since where none is set."""
        return self.fetch_since or self.history_since

    @property
    def instruments(self) -> tuple[Asset, ...]:
        """Everything that needs bars: the basket plus the non-basket instruments."""
        return self.assets + self.outside

    def by_block(self) -> dict[str, list[Asset]]:
        blocks: dict[str, list[Asset]] = {}
        for a in self.assets:
            blocks.setdefault(a.block, []).append(a)
        return blocks


def _as_date(value) -> date:
    return value if isinstance(value, date) else datetime.strptime(str(value), "%Y-%m-%d").date()


def _asset(raw: dict, *, in_basket: bool) -> Asset:
    missing = {"ticker", "source", "block", "has_volume", "session_template",
               "fetch_interval", "tick_size"} - set(raw)
    if missing:
        raise BasketConfigError(f"{raw.get('ticker', '?')}: missing fields {sorted(missing)}")
    if raw["block"] not in BLOCKS:
        raise BasketConfigError(
            f"{raw['ticker']}: block '{raw['block']}' is not one of {list(BLOCKS)}")
    if raw["source"] not in SOURCES:
        raise BasketConfigError(
            f"{raw['ticker']}: source '{raw['source']}' is not one of {list(SOURCES)}")
    if raw.get("provider") and raw["provider"] not in PROVIDERS:
        raise BasketConfigError(
            f"{raw['ticker']}: provider '{raw['provider']}' is not one of "
            f"{list(PROVIDERS)}")
    if raw["fetch_interval"] not in FETCH_INTERVALS:
        raise BasketConfigError(
            f"{raw['ticker']}: fetch_interval '{raw['fetch_interval']}' "
            f"is not one of {list(FETCH_INTERVALS)}")
    if not isinstance(raw["has_volume"], bool):
        raise BasketConfigError(f"{raw['ticker']}: has_volume must be true or false")
    if not isinstance(raw["tick_size"], (int, float)) or raw["tick_size"] <= 0:
        raise BasketConfigError(
            f"{raw['ticker']}: tick_size must be a positive number, "
            f"not {raw['tick_size']!r}")
    return Asset(
        ticker=raw["ticker"], source=raw["source"],
        block=raw["block"], has_volume=bool(raw["has_volume"]),
        session_template=raw["session_template"], fetch_interval=raw["fetch_interval"],
        tick_size=float(raw["tick_size"]),
        label=raw.get("label", raw["ticker"]), in_basket=in_basket,
        provider=raw.get("provider", ""),
    )


def load_basket(path: str = DEFAULT_BASKET_PATH) -> Basket:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    assets = tuple(_asset(a, in_basket=True) for a in raw.get("assets") or [])
    outside = tuple(_asset(a, in_basket=False) for a in raw.get("outside_basket") or [])
    if not assets:
        raise BasketConfigError("The basket contains no assets")

    seen: set[str] = set()
    for a in assets + outside:
        if a.asset_id in seen:
            raise BasketConfigError(f"{a.asset_id}: instrument listed twice")
        seen.add(a.asset_id)

    templates = raw.get("session_templates") or {}
    for a in assets + outside:
        if a.session_template not in templates:
            raise BasketConfigError(
                f"{a.ticker}: session template '{a.session_template}' is not "
                f"described in session_templates")

    by_block = Basket(
        assets, outside, VolatilityIndex("", "", "", "", date.today()), "",
        date.today(), templates
    ).by_block()

    # A one-member block is a config error (BLOCK_MIN_MEMBERS).
    short = {b: len(m) for b, m in by_block.items() if len(m) < BLOCK_MIN_MEMBERS}
    if short:
        listed = ", ".join(f"{b} ({n})" for b, n in sorted(short.items()))
        raise BasketConfigError(
            f"Block below the minimum of {BLOCK_MIN_MEMBERS} members: {listed}. "
            "A block groups instruments; one member is a typo.")

    vix_raw = raw.get("volatility_index") or {}
    if not vix_raw.get("series_id"):
        raise BasketConfigError("volatility_index.series_id is not set")

    since = raw.get("history_since")
    history_since = _as_date(since)
    # Defaults to history_since, so a config that never heard of it behaves
    # exactly as before.
    fetch_since = _as_date(raw.get("fetch_since", since))

    return Basket(
        assets=assets,
        outside=outside,
        volatility_index=VolatilityIndex(
            series_id=vix_raw["series_id"], source=vix_raw.get("source", "fred"),
            interval=vix_raw.get("interval", "1d"),
            history_since=_as_date(vix_raw.get("history_since", since)),
            label=vix_raw.get("label", vix_raw["series_id"]),
        ),
        anchor_exchange_tz=raw.get("anchor_exchange_tz", "America/New_York"),
        history_since=history_since,
        fetch_since=fetch_since,
        session_templates=templates,
    )

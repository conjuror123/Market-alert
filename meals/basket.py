"""Basket configuration: assets, blocks, tiers, derived weights (spec §2.3).

The key difference from the existing monitor's config.yaml: here an asset has no
thresholds of its own, and cannot have any. Thresholds in MEALS are adaptive -
percentiles of an asset's own |Z| distribution (§3.1) - so the configuration
describes only the COMPOSITION and the PROPERTIES of the instruments, not the
sensitivity to them.

The weight is not a configuration field either. Per §2.3 it is derived:
    weight_i = 1 / (N_blocks * N_assets_block)
and if a stored weight diverges from the rule, the configuration counts as
invalid. The only reliable way never to diverge is not to store the weight at all
but to compute it from the composition. That is exactly what is done here.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime

import yaml

DEFAULT_BASKET_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "basket.yaml")

# The blocks of §2.3 plus crypto - the fifth block was added deliberately, see basket.yaml.
BLOCKS = ("equity", "rates", "FX", "commodities", "crypto")
TIERS = (1, 2)
SOURCES = ("twelvedata", "coinbase")
FETCH_INTERVALS = ("30min", "1h")


class BasketConfigError(ValueError):
    """The basket configuration is invalid and cannot be used."""


@dataclass(frozen=True)
class Asset:
    ticker: str
    source: str
    tier: int
    block: str
    has_volume: bool
    session_template: str
    fetch_interval: str
    label: str
    in_basket: bool
    # The source's quote step. Needed by the §2.5 winsorization: the eps_MAD
    # floor includes the return on half a tick, without which, in quiet hours when
    # the price stands still, MAD collapses to zero and an ordinary move looks
    # extreme. It is measured against real data (meals.audit) rather than taken
    # from the exchange specification: the value must match the quote format the
    # source actually serves.
    tick_size: float

    @property
    def asset_id(self) -> str:
        """Logical identifier used in metrics, logs and events."""
        return f"{self.source}:{self.ticker}"

    @property
    def file_stem(self) -> str:
        """File name in the store. Matches the candle_store scheme of the existing
        monitor so that the history already accumulated can be imported without
        renaming anything."""
        return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{self.source}_{self.ticker}")


@dataclass(frozen=True)
class VolatilityIndex:
    """External stress indicator (§4.4). Not part of the basket."""
    series_id: str
    source: str
    interval: str
    label: str
    # Its own history depth: a daily series needs 720 observations to warm up,
    # which is almost three years, and a start shared with the basket in 2021
    # would leave the multiplier equal to one across the whole train period. See
    # basket.yaml.
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

    @property
    def instruments(self) -> tuple[Asset, ...]:
        """Everything that needs bars: the basket plus the non-basket instruments."""
        return self.assets + self.outside

    def by_block(self) -> dict[str, list[Asset]]:
        blocks: dict[str, list[Asset]] = {}
        for a in self.assets:
            blocks.setdefault(a.block, []).append(a)
        return blocks

    def weights(self) -> dict[str, float]:
        """Weights under the equality rule of §2.3: blocks are equal to one
        another, and assets within a block are equal to one another.

        Computed over the whole basket composition. When active_from / active_to
        appear in phase 1, a date argument will be added here - the rule itself
        does not change, only which assets count as active.
        """
        blocks = self.by_block()
        n_blocks = len(blocks)
        return {
            a.asset_id: 1.0 / (n_blocks * len(members))
            for members in blocks.values()
            for a in members
        }


def _as_date(value) -> date:
    return value if isinstance(value, date) else datetime.strptime(str(value), "%Y-%m-%d").date()


def _asset(raw: dict, *, in_basket: bool) -> Asset:
    missing = {"ticker", "source", "tier", "block", "has_volume", "session_template",
               "fetch_interval", "tick_size"} - set(raw)
    if missing:
        raise BasketConfigError(f"{raw.get('ticker', '?')}: missing fields {sorted(missing)}")
    if raw["block"] not in BLOCKS:
        raise BasketConfigError(
            f"{raw['ticker']}: block '{raw['block']}' is not one of {list(BLOCKS)}")
    if raw["tier"] not in TIERS:
        raise BasketConfigError(f"{raw['ticker']}: tier must be 1 or 2, not {raw['tier']!r}")
    if raw["source"] not in SOURCES:
        raise BasketConfigError(
            f"{raw['ticker']}: source '{raw['source']}' is not one of {list(SOURCES)}")
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
        ticker=raw["ticker"], source=raw["source"], tier=int(raw["tier"]),
        block=raw["block"], has_volume=bool(raw["has_volume"]),
        session_template=raw["session_template"], fetch_interval=raw["fetch_interval"],
        tick_size=float(raw["tick_size"]),
        label=raw.get("label", raw["ticker"]), in_basket=in_basket,
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

    # The hourly quorum (§2.3) requires at least two blocks of two assets each. A
    # basket where that is unreachable in any hour is pointless: its cluster
    # triggers will never fire, not merely "rarely".
    populated = [b for b, members in Basket(
        assets, outside, VolatilityIndex("", "", "", "", date.today()), "",
        date.today(), templates
    ).by_block().items() if len(members) >= 2]
    if len(populated) < 2:
        raise BasketConfigError(
            "Quorum unreachable: at least two blocks of no fewer than two assets "
            f"each are needed, and there are {len(populated)} such blocks")

    vix_raw = raw.get("volatility_index") or {}
    if not vix_raw.get("series_id"):
        raise BasketConfigError("volatility_index.series_id is not set (§4.4)")

    since = raw.get("history_since")
    history_since = _as_date(since)

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
        session_templates=templates,
    )

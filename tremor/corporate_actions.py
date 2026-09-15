"""Corporate-actions table (spec §2.4, §6.4).

The store is unadjusted for dividends: adjusted series are recomputed
retroactively on every payout, so in a store appended hour by hour the old bars
would carry one coefficient and the new ones another - and at the seam an
artificial jump the size of the dividend would appear, not at a session boundary
but in an arbitrary hour. That is worse than the problem the adjustment solves.
Instead the series stays unadjusted, the ex-date is recorded here, and the price
drop is excluded from the gap channel.

Declared values, not inferred ones. Twelve Data has no dividend endpoint that
this plan can call, and deriving a step from the ratio of its adjusted and
unadjusted daily closes cannot see a share split: both series are already
split-adjusted, so the split cancels in the ratio, and every pre-split dividend
on XLK/XLY/XLE/XLU/XLB reads twice its true size. Tiingo publishes `divCash`
and `splitFactor` on the free daily endpoint; those are what this table is
built from. The Twelve Data ratio path remains as a labelled fallback for the
funds that never split - it needs no extra key - and it cannot see splits.
"""
from __future__ import annotations

import csv
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date

import requests

from price_monitor import tiingo
from price_monitor.tiingo import DailyRow

DEFAULT_ACTIONS_PATH = os.path.join("data", "tremor", "corporate_actions.csv")
TIME_SERIES_URL = "https://api.twelvedata.com/time_series"

# Threshold below which a step counts as rounding noise. The observed noise is
# around 1e-8; the smallest real payout, on short Treasuries in 2021, was about
# 1.5e-4 of price. The threshold sits between them, closer to the noise.
STEP_THRESHOLD = 1e-5

# Kept only so the Twelve Data fallback still classifies a ratio step. The
# Tiingo path does not use it: splits are declared (`splitFactor`) rather than
# inferred from magnitude. A 0.20 floor is high on purpose - DBC paid 5.07% in
# December 2024 - but it has never fired on the ratio path, because a split
# cancels between two split-adjusted series.
SPLIT_THRESHOLD = 0.20

REQUEST_DELAY_SECONDS = 8.0
# 44 funds at 50 requests/hour. Sleeping ~70s keeps the deriver inside the
# rolling hourly bucket even if the live monitor has just spent 37 of them.
# RateLimited does not retry, so a collision aborts rather than writing a
# truncated table.
TIINGO_REQUEST_DELAY_SECONDS = 70.0

UNADJUST_KINDS = ("dividend",)

log = logging.getLogger("tremor.corporate_actions")


@dataclass(frozen=True)
class CorporateAction:
    ticker: str
    day: date          # ex-date: the first day the price trades without the payout
    kind: str          # "dividend" or "split"
    factor_step: float  # fraction of price by which the coefficient moved


class CorporateActionsError(RuntimeError):
    pass


class IncompleteActionsError(CorporateActionsError):
    """Some selected tickers failed; the table must not be rewritten."""

    def __init__(self, failed: list[str]):
        self.failed = failed
        super().__init__(f"{len(failed)} ticker(s) failed: {', '.join(failed)}")


def _daily_closes(symbol: str, api_key: str, since: date, session: requests.Session,
                  adjusted: bool) -> dict[date, float]:
    params = {"symbol": symbol, "interval": "1day", "start_date": since.isoformat(),
              "apikey": api_key, "timezone": "UTC", "outputsize": 5000}
    if adjusted:
        params["adjust"] = "all"
    resp = session.get(f"{TIME_SERIES_URL}?{urllib.parse.urlencode(params)}", timeout=40,
                       headers={"User-Agent": "market-alert-bot"})
    if resp.status_code != 200:
        raise CorporateActionsError(f"{symbol}: status {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("status") == "error":
        raise CorporateActionsError(f"{symbol}: {data.get('message')}")
    return {date.fromisoformat(v["datetime"][:10]): float(v["close"])
            for v in data.get("values", [])}


def derive_actions(symbol: str, api_key: str, since: date,
                   session: requests.Session | None = None) -> list[CorporateAction]:
    """Twelve Data fallback: corporate-action dates from the adjustment ratio.

    Cannot see splits. Both series are split-adjusted, so a 2:1 cancels in the
    ratio and every pre-split dividend on a split fund reads twice its size.
    """
    sess = session or requests.Session()
    raw = _daily_closes(symbol, api_key, since, sess, adjusted=False)
    time.sleep(REQUEST_DELAY_SECONDS)
    adjusted = _daily_closes(symbol, api_key, since, sess, adjusted=True)

    days = sorted(set(raw) & set(adjusted))
    if not days:
        raise CorporateActionsError(f"{symbol}: the series do not overlap")

    actions = []
    previous = None
    for day in days:
        if raw[day] <= 0:
            continue
        factor = adjusted[day] / raw[day]
        if previous is not None and previous > 0:
            step = factor / previous - 1
            if abs(step) >= STEP_THRESHOLD:
                actions.append(CorporateAction(
                    ticker=symbol, day=day,
                    kind="split" if abs(step) >= SPLIT_THRESHOLD else "dividend",
                    factor_step=step,
                ))
        previous = factor
    return actions


def derive_actions_tiingo(ticker: str, rows: list[DailyRow]) -> list[CorporateAction]:
    """Declared dividends and splits from Tiingo daily rows.

    The vendor multiplies pre-ex prices by (1 - d) where
    d = divCash / close_{t-1} of the raw close, so
    factor_t / factor_{t-1} = 1/(1-d) and the step that `unadjust_factor`
    compounds is d/(1-d), not d. Using adjClose as the denominator would
    reintroduce the 2x pre-split error plus a second time-varying one.

    Splits are recorded with factor_step 1.0 for provenance and for
    split_channels' ex-date masking. They are excluded at load_steps: the
    store is already split-adjusted, and feeding a 2.0 into the cumprod would
    manufacture a 100% error on every older bar.
    """
    ordered = sorted(rows, key=lambda r: r.day)
    actions: list[CorporateAction] = []
    prev_close: float | None = None
    for row in ordered:
        if prev_close is not None and prev_close > 0 and row.div_cash:
            d = row.div_cash / prev_close
            if 0.0 < d < 1.0:
                step = d / (1.0 - d)
                if abs(step) >= STEP_THRESHOLD:
                    actions.append(CorporateAction(
                        ticker=ticker, day=row.day,
                        kind="dividend", factor_step=step,
                    ))
        if abs(row.split_factor - 1.0) >= STEP_THRESHOLD:
            actions.append(CorporateAction(
                ticker=ticker, day=row.day,
                kind="split", factor_step=1.0,
            ))
        if row.close > 0:
            prev_close = row.close
    return actions


def write_actions(path: str, actions: list[CorporateAction]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["ticker", "date", "kind", "factor_step"])
        for a in sorted(actions, key=lambda a: (a.ticker, a.day, a.kind)):
            writer.writerow([a.ticker, a.day.isoformat(), a.kind, f"{a.factor_step:.8f}"])


def load_actions(path: str = DEFAULT_ACTIONS_PATH) -> dict[str, set[date]]:
    """Corporate-action dates by ticker. An empty dict if there is no table: its
    absence must not break the hourly run - it merely means ex-date gaps are not
    being flagged yet."""
    if not os.path.exists(path):
        return {}
    by_ticker: dict[str, set[date]] = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            by_ticker.setdefault(row["ticker"], set()).add(date.fromisoformat(row["date"]))
    return by_ticker


def load_steps(path: str = DEFAULT_ACTIONS_PATH,
               kinds: tuple[str, ...] = UNADJUST_KINDS) -> dict[str, list[tuple[date, float]]]:
    """Ex-dates WITH their sizes, oldest first, for undoing a vendor's adjustment.

    Default `kinds` is dividends only. The store is unadjusted for dividends and
    already split-adjusted; a split row in the cumprod would double (or halve)
    every older bar. `load_actions` still returns every date, including splits,
    because the gap channel wants the ex-date regardless of kind.

    The `kinds` argument is defaulted so existing zero-arg callers - including
    test monkeypatches of `lambda: {}` - keep working.
    """
    if not os.path.exists(path):
        return {}
    by_ticker: dict[str, list[tuple[date, float]]] = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            kind = row.get("kind") or "dividend"
            if kind not in kinds:
                continue
            try:
                step = float(row["factor_step"])
            except (TypeError, ValueError):
                continue
            by_ticker.setdefault(row["ticker"], []).append(
                (date.fromisoformat(row["date"]), step))
    for steps in by_ticker.values():
        steps.sort()
    return by_ticker


def unadjust_factor(steps: list[tuple[date, float]], moments,
                    reference: date, reference_factor: float = 1.0):
    """The divisor turning a vendor's adjusted price into the real one.

    An adjusted series is the true price scaled by the payouts that came AFTER
    it, so its coefficient rises through time and the factor between any two
    moments is the product of (1 + step) over the ex-dates in between. The sign
    is not assumed: measured against SPY's actual closes, (1 + step) explains
    the drift to 0.02% over three years and 0.15% over seven, where (1 - step)
    is out by 11% and 24%.

    `reference_factor` is measured rather than derived, which is the point. The
    vendor's own anchor - end of their data, end of an era within it, something
    else - never has to be guessed: pinning the factor at one moment where the
    true price is independently known leaves the ex-dates responsible only for
    the CHANGE from there. That is a far weaker claim than reproducing their
    convention, and unlike it, it can be checked.
    """
    import numpy as np
    import pandas as pd

    days = pd.to_datetime(pd.Series(moments), unit="s", utc=True).dt.date
    ordered = sorted(steps)
    if not ordered:
        return np.full(len(days), float(reference_factor))

    # Cumulative product once, then two lookups per moment. The direct form is
    # a product per row over every ex-date, which on a 2.3M-row minute series
    # is a hundred and eighty million multiplications for one instrument.
    dates = np.array([d.toordinal() for d, _ in ordered])
    growth = np.concatenate([[1.0], np.cumprod([1.0 + s for _, s in ordered])])

    taken = np.searchsorted(dates, np.array([d.toordinal() for d in days]),
                            side="right")
    at_reference = int(np.searchsorted(dates, reference.toordinal(), side="right"))
    return reference_factor * growth[taken] / growth[at_reference]


def _funds(basket) -> list:
    # Payouts and splits happen to ETFs. Currency pairs and crypto have no
    # corporate actions by the nature of the instrument.
    return [a for a in basket.instruments if a.source == "twelvedata" and a.block != "FX"]


def collect_from_tiingo(funds, since: date, api_key: str, session,
                        delay: float = TIINGO_REQUEST_DELAY_SECONDS) -> list[CorporateAction]:
    """Fetch every fund or refuse. A partial table is how history disappears."""
    all_actions: list[CorporateAction] = []
    failed: list[str] = []
    for i, asset in enumerate(funds):
        try:
            rows = tiingo.fetch_daily_history(asset.ticker, since, api_key=api_key,
                                              session=session)
            found = derive_actions_tiingo(asset.ticker, rows)
            all_actions.extend(found)
            splits = sum(1 for a in found if a.kind == "split")
            log.info("%s: payouts %d, splits %d", asset.ticker,
                     len(found) - splits, splits)
        except tiingo.RateLimited:
            raise
        except Exception as exc:
            log.error("%s: failed - %s", asset.ticker, exc)
            failed.append(asset.ticker)
        if i < len(funds) - 1:
            time.sleep(delay)
    if failed:
        raise IncompleteActionsError(failed)
    return all_actions


def collect_from_twelvedata(funds, since: date, api_key: str, session,
                            delay: float = REQUEST_DELAY_SECONDS) -> list[CorporateAction]:
    all_actions: list[CorporateAction] = []
    failed: list[str] = []
    for i, asset in enumerate(funds):
        try:
            found = derive_actions(asset.ticker, api_key, since, session)
            all_actions.extend(found)
            splits = sum(1 for a in found if a.kind == "split")
            log.info("%s: payouts %d, splits %d", asset.ticker,
                     len(found) - splits, splits)
        except Exception as exc:
            log.error("%s: failed - %s", asset.ticker, exc)
            failed.append(asset.ticker)
        if i < len(funds) - 1:
            time.sleep(delay)
    if failed:
        raise IncompleteActionsError(failed)
    return all_actions


def main(argv: list[str] | None = None) -> int:
    import argparse

    from tremor.basket import load_basket

    parser = argparse.ArgumentParser(
        description="Corporate-actions table (§2.4). "
                    "tiingo (default) uses declared divCash/splitFactor. "
                    "twelvedata infers from the adjusted/raw ratio and cannot see splits.")
    parser.add_argument("--out", default=DEFAULT_ACTIONS_PATH)
    parser.add_argument(
        "--source", choices=("tiingo", "twelvedata"), default="tiingo",
        help="tiingo (default) uses declared dividends and splits. "
             "twelvedata cannot see splits: both of its series are split-adjusted "
             "so the split cancels in the ratio.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    basket = load_basket()
    funds = _funds(basket)
    session = requests.Session()

    try:
        if args.source == "twelvedata":
            log.warning(
                "Twelve Data cannot see splits; pre-split dividend steps on "
                "split funds will be twice their true size.")
            api_key = os.environ.get("TWELVEDATA_API_KEY", "")
            if not api_key:
                log.error("TWELVEDATA_API_KEY is not set")
                return 2
            actions = collect_from_twelvedata(
                funds, basket.acquire_since, api_key, session)
        else:
            api_key = os.environ.get("TIINGO_API_KEY", "")
            if not api_key:
                log.error("TIINGO_API_KEY is not set")
                return 2
            # The ACQUISITION floor, not the analysis one. This table has to
            # reach at least as far back as the bars do or an ex-date older
            # than it arrives as an unexplained price drop - and the detector,
            # which now rates moves by how rare they are for the instrument,
            # would promote a monthly bond-fund distribution to an alert.
            # HYG, IEF and SHY pay monthly; the store already holds a year of
            # ETF bars older than this table's first row.
            actions = collect_from_tiingo(
                funds, basket.acquire_since, api_key, session)
    except tiingo.RateLimited as exc:
        log.error("Tiingo request budget spent (%s). Refusing to write a truncated table.",
                  exc)
        return 1
    except IncompleteActionsError as exc:
        log.error("Refusing to write a truncated table: %s", exc)
        return 1

    write_actions(args.out, actions)
    log.info("%s: records %d", args.out, len(actions))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

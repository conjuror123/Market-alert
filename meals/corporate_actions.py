"""Corporate-actions table (spec §2.4, §6.4).

The plan has no dividend endpoint - both /dividends and /splits answer 403. But
ex-dates can be derived from the quotes themselves: the vendor can serve the same
series adjusted and unadjusted, and their ratio is a step function that changes
on exactly the corporate-action dates. The size of the step is the size of the
payout as a fraction of price.

Verified: at the end of the series the factor is exactly 1.0 (there are no future
payouts), and between steps it holds at the 1e-8 level - that is rounding in the
vendor's string. The detection threshold is set two orders of magnitude above
that noise and an order of magnitude below the smallest real payout.

Why this is needed when our series are unadjusted. Adjusted series cannot be
used: they are recomputed retroactively on every new payout, so in a store
appended hour by hour the old bars would carry one coefficient and the new ones
another - and at the seam an artificial jump the size of the dividend would
appear, not at a session boundary but in an arbitrary hour. That is worse than
the problem the adjustment solves. Instead the series stays unadjusted, and the
price drop on the ex-date is flagged here and excluded from the gap channel.
"""
from __future__ import annotations

import csv
import os
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date

import requests

DEFAULT_ACTIONS_PATH = os.path.join("data", "meals", "corporate_actions.csv")
TIME_SERIES_URL = "https://api.twelvedata.com/time_series"

# Threshold below which a step counts as rounding noise. The observed noise is
# around 1e-8; the smallest real payout, on short Treasuries in 2021, was about
# 1.5e-4 of price. The threshold sits between them, closer to the noise.
STEP_THRESHOLD = 1e-5

# A step larger than this is no longer a payout but a share split. The threshold
# is deliberately high: a real split moves the coefficient several-fold (2:1 is
# 50%), whereas a commodity fund's annual payout reaches 5% of price - DBC paid
# 5.07% in December 2024, and at a 5% threshold it would have counted as a split.
SPLIT_THRESHOLD = 0.20

REQUEST_DELAY_SECONDS = 8.0


@dataclass(frozen=True)
class CorporateAction:
    ticker: str
    day: date          # ex-date: the first day the price trades without the payout
    kind: str          # "dividend" or "split"
    factor_step: float  # fraction of price by which the coefficient moved


class CorporateActionsError(RuntimeError):
    pass


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
    """Finds corporate-action dates from the steps in the adjustment coefficient."""
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


def write_actions(path: str, actions: list[CorporateAction]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["ticker", "date", "kind", "factor_step"])
        for a in sorted(actions, key=lambda a: (a.ticker, a.day)):
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


def load_steps(path: str = DEFAULT_ACTIONS_PATH) -> dict[str, list[tuple[date, float]]]:
    """Ex-dates WITH their sizes, oldest first, for undoing a vendor's adjustment.

    load_actions above returns only the dates, which is all the gap channel
    needs. Reversing an adjusted price series needs the sizes too: the factor
    between two moments is the product of (1 - step) over the ex-dates between
    them.
    """
    if not os.path.exists(path):
        return {}
    by_ticker: dict[str, list[tuple[date, float]]] = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
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


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging

    from meals.basket import load_basket

    parser = argparse.ArgumentParser(description="Corporate-actions table (§2.4)")
    parser.add_argument("--out", default=DEFAULT_ACTIONS_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("meals.corporate_actions")

    api_key = os.environ.get("TWELVEDATA_API_KEY", "")
    if not api_key:
        log.error("TWELVEDATA_API_KEY is not set")
        return 2

    basket = load_basket()
    # Payouts and splits happen to ETFs. Currency pairs and crypto have no
    # corporate actions by the nature of the instrument.
    funds = [a for a in basket.instruments if a.source == "twelvedata" and a.block != "FX"]

    session = requests.Session()
    all_actions: list[CorporateAction] = []
    for i, asset in enumerate(funds):
        try:
            # The ACQUISITION floor, not the analysis one. This table has to
            # reach at least as far back as the bars do or an ex-date older
            # than it arrives as an unexplained price drop - and the detector,
            # which now rates moves by how rare they are for the instrument,
            # would promote a monthly bond-fund distribution to an alert.
            # HYG, IEF and SHY pay monthly; the store already holds a year of
            # ETF bars older than this table's first row.
            found = derive_actions(asset.ticker, api_key, basket.acquire_since, session)
            all_actions.extend(found)
            splits = sum(1 for a in found if a.kind == "split")
            log.info("%s: payouts %d, splits %d", asset.ticker,
                     len(found) - splits, splits)
        except Exception as exc:
            log.error("%s: failed - %s", asset.ticker, exc)
        if i < len(funds) - 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    write_actions(args.out, all_actions)
    log.info("%s: records %d", args.out, len(all_actions))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

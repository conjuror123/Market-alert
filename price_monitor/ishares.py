"""iShares' own distribution table: the payouts of the UK's gilts fund (IGLT).

THE PAGE is the fund's product page on ishares.com (product 251806, the UK
individual investor's site; robots.txt allows /uk/individual/en/products/). It
embeds its distribution table as data: each payout's ex-date (YYYYMMDD), record
date, payable date and amount in pounds a share, newest first - 40 back to 2007
on 2026-10-19. A payout is on it once declared, before the ex-date's open.

WHY NOT YAHOO'S. Yahoo files some of IGLT's payouts under the record date, the
day after the ex-date: 2026-05-22 and 2025-05-16 for the ex-dates 2026-05-21 and
2025-05-15, the nights the price fell, and 2024-11 under both days. These
ex-dates are the drops' (docs/decisions.md, "The funds abroad's payouts").

AND THE STANDING RISK. A web page, about 2.9 MB, read once a session for its
embedded table. Should its shape change, the read raises: the fund's payouts go
unconfirmed, its nights unscored, and the health chat says so after five days
(jump.backfill, the dividend check).
"""
from __future__ import annotations

import html
import re
import time
from datetime import date

import requests

from price_monitor.models import ExchangeError, Unreachable

PAGES = {"IGLT.L": "https://www.ishares.com/uk/individual/en/products/251806/"
                   "ishares-uk-gilts-ucits-etf?switchLocale=y&siteEntryPassthrough=true"}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124 Safari/537.36",
           "Accept": "text/html", "Accept-Language": "en-GB"}
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0

_EX_DATES = re.compile(r'"exDate":\{"name":"exDate"[^{}]*?"value":\[([0-9,\s]*)\]')
_AMOUNTS = re.compile(r'"totalDistribution":\{"name":"totalDistribution"[^{}]*?"value":\[([^\]]*)\]')


def parse(text: str, ticker: str = "") -> "list[tuple[date, float]]":
    """Each payout's ex-date and amount from the page's embedded table."""
    page = html.unescape(text)
    days, amounts = _EX_DATES.search(page), _AMOUNTS.search(page)
    if days is None or amounts is None:
        raise ExchangeError(f"{ticker}: iShares' page has no distribution table")
    days = [d.strip() for d in days.group(1).split(",") if d.strip()]
    amounts = [a.strip().strip('"') for a in amounts.group(1).split(",") if a.strip()]
    if len(days) != len(amounts):
        raise ExchangeError(f"{ticker}: iShares' table has {len(days)} ex-dates and "
                            f"{len(amounts)} amounts")
    out = []
    for day, amount in zip(days, amounts):
        try:
            out.append((date(int(day[:4]), int(day[4:6]), int(day[6:8])), float(amount)))
        except ValueError as exc:
            raise ExchangeError(f"{ticker}: iShares' table row {day} {amount!r}") from exc
    return sorted(out)


def fetch_payouts(ticker: str, session: requests.Session | None = None) -> "list[tuple[date, float]]":
    """The fund's payouts as iShares declares them: (ex-date, pounds a share)."""
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = (session or requests).get(PAGES[ticker], headers=HEADERS, timeout=60)
        except requests.RequestException as exc:
            last = Unreachable(f"{ticker}: {exc}")
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = (Unreachable if resp.status_code >= 500 else ExchangeError)(
                f"{ticker}: iShares answered {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(f"{ticker}: iShares answered {resp.status_code}")
        return parse(resp.text, ticker)
    raise last or ExchangeError(f"{ticker}: no response")

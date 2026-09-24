"""Yahoo Finance's chart endpoint, used for the US equity ETFs.

WHY THIS CARRIES THE ETFs. Twelve Data's free tier allows 8 requests a minute,
which the backfill loop honours with an 8-second pause between instruments. For
44 ETFs that pause IS the run: 352 seconds of deliberate waiting, against about
30 seconds of actual work. Yahoo has no key, no credit budget and no enforced
pace, and it answers in 0.77 seconds.

AND WHY IT IS TRUSTED TO. Cheap and fast would not be enough on its own - a
second feed is only usable if it prices the basket the same way. Measured
against the bars already stored from Twelve Data, folded onto the same hourly
grid, over 62 overlapping hours:

    23 ETFs tested - 8 liquid, 15 thin -   median 0.00 bps, p90 0.00 bps

Not "close": identical, including on the thin commodity funds where Tiingo's
IEX-only feed drifts by 2 to 8 bps and, on UGA, by 77 bps in a single hour.
Yahoo serves the consolidated tape, and its reported volume is within a percent
of the stored figure rather than the 2-8% an IEX-only feed returns.

SPLITS ARE BACK-ADJUSTED, which matters because the store is too. XLK's hourly
closes across the 2:1 split of 2025-12-05 run 145.53 then 146.58 here, with no
step - the same two numbers the store already holds. A raw series would have
put a 2x discontinuity into the middle of a return.

WHAT IT IS NOT FOR. Deepening history. Yahoo caps intraday lookback by interval
(60 days at 30 minutes, 730 at an hour), so it can only ever serve the recent
end - see MAX_LOOKBACK_DAYS. Twelve Data and HF Data keep the archive.

AND THE STANDING RISK. This endpoint is undocumented and unversioned. It can
change shape, start demanding a cookie, or rate-limit without notice, and there
is no support channel and no SLA. That is why the ETFs are split across two
providers rather than all sent here: a Yahoo outage costs the thin ETFs, not
the basket, and Twelve Data remains configured underneath. Treat a schema
change as expected maintenance rather than a surprise.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from price_monitor.models import Candle, ExchangeError

log = logging.getLogger("price_monitor.yahoo")

BASE_URL = "https://query1.finance.yahoo.com"
CHART_ENDPOINT = "/v8/finance/chart/{symbol}"

# This app's interval names -> Yahoo's.
INTERVAL_CODES = {"30min": "30m", "1h": "1h", "1d": "1d"}

INTERVAL_SECONDS = {"30min": 1800, "1h": 3600, "1d": 86400}

# How far back Yahoo will serve each interval AT ALL. These are properties of
# the endpoint, not of a plan, and asking past them returns an empty result
# rather than an error - which would look exactly like a quiet market. The
# limits are enforced here so that a request which cannot be answered is
# refused loudly instead.
MAX_LOOKBACK_DAYS = {"30min": 55, "1h": 700, "1d": 36500}

# Yahoo answers an unadorned request with 429. Any ordinary browser string is
# accepted; this one names the project so the traffic is attributable.
USER_AGENT = "Mozilla/5.0 (compatible; market-alert-bot/1.0)"

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


class RateLimited(ExchangeError):
    """Yahoo answered 429 after retries. The rest of this provider is skipped."""


def _interval_code(interval: str) -> str:
    try:
        return INTERVAL_CODES[interval]
    except KeyError:
        raise ExchangeError(f"Unsupported Yahoo interval '{interval}'") from None


def _granularity_seconds(interval: str) -> int:
    try:
        return INTERVAL_SECONDS[interval]
    except KeyError:
        raise ExchangeError(f"Unsupported Yahoo interval '{interval}'") from None


def _parse(payload: dict, granularity: int, symbol: str) -> list[Candle]:
    """Turn one chart response into candles, dropping the gaps.

    Yahoo pads its arrays so that every index lines up with a timestamp, and
    writes null into a slot no trade fell in. A null is a hole, not a zero: the
    surrounding bars are real and must be kept, so the row is skipped rather
    than the response rejected.
    """
    chart = payload.get("chart") or {}
    error = chart.get("error")
    if error:
        raise ExchangeError(
            f"{symbol}: {error.get('code', 'error')} - {error.get('description', '')}")
    results = chart.get("result") or []
    if not results:
        return []
    result = results[0]
    stamps = result.get("timestamp") or []
    quotes = (result.get("indicators") or {}).get("quote") or [{}]
    q = quotes[0] if quotes else {}
    opens, highs = q.get("open") or [], q.get("high") or []
    lows, closes = q.get("low") or [], q.get("close") or []
    volumes = q.get("volume") or []

    candles: list[Candle] = []
    for i, stamp in enumerate(stamps):
        if i >= len(opens) or i >= len(highs) or i >= len(lows) or i >= len(closes):
            break
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if o is None or h is None or l is None or c is None:
            continue
        v = float(volumes[i] or 0.0) if i < len(volumes) else 0.0
        # THE CLOSING STUB. Yahoo emits one extra bar at the regular-market
        # close - 20:00 UTC, 16:00 in New York - carrying no volume and the
        # same number four times over. It is a marker for the closing instant,
        # not a period anything traded in: the 19:30 bar already contains the
        # closing auction, and Twelve Data does not produce it at all. Kept, it
        # would open an hourly bucket the session calendar does not expect, on
        # every ETF, on every run - a zero-return hour appended after the close.
        # Measured on 14 of 15 tickers, once per session, always at 20:00.
        #
        # Volume alone is not the test: an illiquid fund can print every trade
        # of an hour at one price. Zero volume AND zero width together are the
        # signature, and a bar that really traded fails both.
        if v == 0.0 and o == h == l == c:
            continue
        t = int(stamp)
        candles.append(Candle(open_time=t, open=float(o), high=float(h),
                              low=float(l), close=float(c),
                              volume=v, close_time=t + granularity))
    return candles


def _request(session, url: str, params: dict, granularity: int,
             symbol: str, parse=None):
    parse = parse or (lambda payload: _parse(payload, granularity, symbol))
    sess = session or requests
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        try:
            resp = sess.get(url, params=params, timeout=30,
                            headers={"User-Agent": USER_AGENT})
        except requests.RequestException as exc:
            last = ExchangeError(f"{symbol}: {exc}")
            continue
        if resp.status_code == 404:
            # Yahoo does not know this ticker. Retrying cannot change that.
            raise ExchangeError(f"{symbol}: unknown to Yahoo (404)")
        if resp.status_code == 429:
            last = RateLimited(f"{symbol}: status 429")
            continue
        if resp.status_code in (500, 502, 503, 504):
            last = ExchangeError(f"{symbol}: status {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise ExchangeError(
                f"{symbol}: unexpected status {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            last = ExchangeError(f"{symbol}: response was not JSON ({exc})")
            continue
        return parse(payload)
    raise last or ExchangeError(f"{symbol}: no response")


def fetch_full_history(
    symbol: str,
    interval: str,
    days: float,
    base_url: str = BASE_URL,
    session: requests.Session | None = None,
    end: datetime | None = None,
) -> list[Candle]:
    """Up to `days` of candles ending at `end` (default now).

    One request covers the whole window - Yahoo has no per-request candle cap
    worth paging around at these intervals, so unlike the Twelve Data and
    Coinbase clients there is no chunk loop here and nothing to pace.
    """
    granularity = _granularity_seconds(interval)
    limit = MAX_LOOKBACK_DAYS[interval]
    if days > limit:
        raise ExchangeError(
            f"{symbol}: Yahoo serves at most {limit} days at {interval}; "
            f"{days:.0f} were asked for. Use Twelve Data or HF Data to deepen.")
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=max(days, 1.0))
    url = f"{base_url}{CHART_ENDPOINT.format(symbol=symbol)}"
    params = {
        "interval": _interval_code(interval),
        "period1": int(start.timestamp()),
        "period2": int(end.timestamp()),
    }
    # Not trimmed to the requested window. Both providers round outwards to a
    # session boundary and can answer with a bar or two from before `start`;
    # those are real bars, and bars.merge takes the union, so discarding them
    # would throw away data that was already paid for.
    return _request(session, url, params, granularity, symbol)


def _parse_dividends(payload: dict, symbol: str) -> "list[tuple[date, float]]":
    """(ex-date, step) pairs, the step in the corporate-actions table's form.

    The table stores d / (1 - d) with d the payout over the PREVIOUS close (see
    tremor.corporate_actions.derive_actions_tiingo), so that is what this
    returns. Yahoo's daily closes are split-adjusted and so are its payouts, so
    the ratio is the same one the raw Tiingo figures give.
    """
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError) as exc:
        error = (payload.get("chart") or {}).get("error") if isinstance(payload, dict) else None
        raise ExchangeError(f"{symbol}: malformed chart response ({error or exc})") from exc
    zone = ZoneInfo(result.get("meta", {}).get("exchangeTimezoneName")
                    or "America/New_York")
    stamps = result.get("timestamp") or []
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0]
              .get("close") or [])
    days = [datetime.fromtimestamp(int(t), zone).date() for t in stamps]
    out: list[tuple[date, float]] = []
    for item in ((result.get("events") or {}).get("dividends") or {}).values():
        try:
            amount = float(item["amount"])
            ex_day = datetime.fromtimestamp(int(item["date"]), zone).date()
        except (KeyError, TypeError, ValueError):
            continue
        before = [c for d, c in zip(days, closes) if d < ex_day and c]
        if not before or amount <= 0:
            continue
        d = amount / float(before[-1])
        if 0.0 < d < 1.0:
            out.append((ex_day, d / (1.0 - d)))
    return sorted(out)


def fetch_dividends(symbol: str, since: date, base_url: str = BASE_URL,
                    session: requests.Session | None = None,
                    now: datetime | None = None) -> "list[tuple[date, float]]":
    """Every payout from `since` to today, as (ex-date, step).

    For the overnight gap, which may only be scored on a day whose payout is
    known (tremor.backfill.check_dividends). Daily bars come back with the
    events so the previous close is in the same answer; the window starts a
    week before `since` so an ex-date on `since` still has a close before it.
    Raises on a failed request - an empty list means "asked, and none", which
    is the only answer that may advance a fund's checked-through date.
    """
    now = now or datetime.now(timezone.utc)
    start = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc) \
        - timedelta(days=7)
    url = f"{base_url}{CHART_ENDPOINT.format(symbol=symbol)}"
    params = {"interval": "1d", "events": "div",
              "period1": int(start.timestamp()),
              "period2": int(now.timestamp()) + 86400}
    found = _request(session, url, params, 86400, symbol,
                     parse=lambda payload: _parse_dividends(payload, symbol))
    return [(day, step) for day, step in found if day >= since]

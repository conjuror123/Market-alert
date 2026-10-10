"""Fetching hourly bars into the store.

Runs every hour as the first step of the pass, and carries the deepening modes
that are run by hand.

THE HOURLY PATH asks each instrument only for what it can be missing: the walk
starts at its newest stored bar minus three hours, and an instrument whose market
has been shut since that bar is not asked at all (see nothing_can_have_appeared).
Providers are chosen per instrument by `provider` in config/basket.yaml (Tiingo,
Alpaca, Twelve Data, Yahoo, Sina, SiftingIO, Binance, Google Finance for TUR;
docs/manual.md, "Data in"), and only Twelve Data is paced, because only its free
tier enforces a per-minute limit. After the fetch, the other sources vote on
the far moves (jump.verify).

US EQUITY ETFs ARE REQUESTED AS HALF-HOURLY BARS and folded onto the round UTC
hour (see bars.to_hourly): their own grid runs on the :30 and would not line up
with the currency pairs and crypto, which would make "the same hour" mean two
different things across instruments. It costs nothing extra - the providers
count requests, not rows.

THE DEEPENING MODES (--extend-history, --deepen-alpaca, --deepen-dukascopy,
--deepen-wallstreetcn, --fill-gaps) reach back past what the live providers
serve, and are routed to `source` rather than `provider`: Yahoo serves 59 days
of half-hourly bars and Tiingo caps a response at 10000 rows, so only the
archive provider can answer a walk backwards. An import from another source is
gated on agreeing with the bars already stored (see verify_alignment), so a
series on a different adjustment basis is refused rather than spliced.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from jump import atomic, bars, cboe, corporate_actions, fred, futures, quality, verify
from jump import sessions as _sessions
from jump.basket import Asset, Basket, load_basket
from jump.usage import Usage
from price_monitor import (alpaca, binance, dukascopy, google, sifting, sina,
                           tiingo, twelvedata, wallstreetcn, yahoo)
from price_monitor.models import UNANSWERED_IN_A_ROW, ExchangeError, KeyRefused, Unreachable
from price_monitor.notifier import quote, send_health

log = logging.getLogger("jump.backfill")

TWELVEDATA_BASE_URL = "https://api.twelvedata.com"
TIINGO_BASE_URL = tiingo.BASE_URL
YAHOO_BASE_URL = yahoo.BASE_URL

# Pause between Twelve Data requests. The free plan allows 8 requests a minute,
# and 8 seconds hold exactly that boundary with a small margin. The pause is held
# between instruments too, not only within one: the limit is per key, and the
# running hourly monitor is spending it at the same time.
TWELVEDATA_DELAY_SECONDS = 8.0

# How many calendar days to request at once. Half-hourly bars give about 13 rows
# per trading day, so 300 days is ~3900 rows against a ceiling of 5000. Hourly
# currency pairs give 24 rows a day, and there the client's own, more cautious
# default applies.
CHUNK_DAYS = {"30min": 300, "1h": 150}

# How far back each provider will answer, by interval. The archive providers
# page backwards without a wall and are absent on purpose; the two recent-end
# providers are not, and a request past their reach returns an empty result,
# which is indistinguishable from a quiet market.
_PROVIDER_REACH = {
    "yahoo": yahoo.MAX_LOOKBACK_DAYS,
    "tiingo": tiingo.MAX_LOOKBACK_DAYS,
}

# How many instruments with an EMPTY store one ordinary run will fetch. See the
# loop in main() - this is the guard that keeps a batch of newly configured
# tickers from turning the hourly job into a backfill.
SEED_PER_RUN = 4


# Each keyed provider by name, with the secret its key is read from.
PROVIDER_KEYS = {
    "twelvedata": ("Twelve Data", "TWELVEDATA_API_KEY"),
    "tiingo": ("Tiingo", "TIINGO_API_KEY"),
    "sifting": ("SiftingIO", "SIFTING_API_KEY"),
    "alpaca": ("Alpaca", "ALPACA_KEY_ID / ALPACA_SECRET_KEY"),
}


def format_provider_failure(dark: list[tuple[str, str, str]],
                            tiingo_gone: bool = False,
                            tiingo_skipped: int = 0,
                            tiingo_remaining: str | None = None,
                            tiingo_trip: str | None = None,
                            yahoo_gone: bool = False,
                            sifting_gone: bool = False,
                            sifting_skipped: int = 0,
                            sifting_remaining: str | None = None,
                            sifting_trip: str | None = None,
                            silent: "dict[str, list] | None" = None,
                            stale: "list[tuple[str, str, int]] | None" = None,
                            vote: "list[str] | None" = None,
                            keys: "dict[str, list] | None" = None) -> str:
    """One operational message naming who went dark. Does not switch provider.

    A spent budget (Tiingo, SiftingIO) is said every run it happens: a quota
    is gone. A Yahoo rate limit is not said: one refusal costs its instruments
    an hour, fetched again next run. It is said by what it costs, when one of
    them goes without a bar past its limit (`stale`, "refused this run").

    `silent` is each provider stopped for not answering: [the instrument it
    was stopped at, how many after it were skipped]. `stale` is each
    instrument with nothing new for longer than its limit: (asset_id,
    provider, session hours since its newest bar).

    `keys` is each provider whose key is missing or was refused: [why, the
    instrument it was refused at (None: not set), how many were not asked].
    Said every run it happens: none of its instruments is fetched.

    The providers first and the instruments last, the long list of errors
    last of all: a message cut to Telegram's limit loses only its tail."""
    lines = []
    for provider, (why, trip, skipped) in (keys or {}).items():
        name, env = PROVIDER_KEYS.get(provider, (provider, "its key"))
        if lines:
            lines.append("")
        if trip is None:
            lines.append(f"⚠️ <b>{name}: {env} is not set</b>")
            lines.append(f"Its {skipped} instrument(s) are not fetched; the other providers "
                         "are. Said every run until it is set.")
        else:
            lines.append(f"⚠️ <b>{name} refused the key at {trip}</b>")
            more = f"; {skipped} more {name} instrument(s) skipped" if skipped else ""
            lines.append(f"{quote(why)}{more}. Asked again next run.")
    for name, gone, trip, left, skipped in (
            ("Tiingo", tiingo_gone, tiingo_trip,
             f"remaining headroom {tiingo_remaining}" if tiingo_remaining is not None
             else "remaining headroom not in the 429", tiingo_skipped),
            ("SiftingIO", sifting_gone, sifting_trip,
             f"{sifting_remaining} left in its short window" if sifting_remaining is not None
             else "what is left not in the 429", sifting_skipped)):
        if not gone:
            continue
        if lines:
            lines.append("")
        at = f" at {trip}" if trip else ""
        lines.append(f"⚠️ <b>{name} request budget spent{at}</b>")
        more = f"; {skipped} more {name} instrument(s) skipped" if skipped else ""
        lines.append(f"{left.capitalize()}{more}. Asked again next run.")
    for provider, (trip, skipped) in (silent or {}).items():
        if lines:
            lines.append("")
        lines.append(f"⚠️ <b>{provider} did not answer</b>")
        lines.append(f"{UNANSWERED_IN_A_ROW} instruments in a row timed out or got a server "
                     f"error, the last {trip}; {skipped} remaining {provider} "
                     "instrument(s) skipped. Asked again next run.")
    if stale:
        if lines:
            lines.append("")
        lines.append(f"⚠️ <b>No new bar: {len(stale)} instrument(s)</b>")
        for asset_id, provider, hours in stale[:20]:
            refused = ", refused this run" if provider == "yahoo" and yahoo_gone else ""
            lines.append(f"• {asset_id} ({provider}{refused}): {hours} session hours "
                         "since its newest bar")
        if len(stale) > 20:
            lines.append(f"• …and {len(stale) - 20} more")
        lines.append("Named when it passes its limit, then once a day.")
    if vote:
        if lines:
            lines.append("")
        lines.append("⚠️ <b>The vote</b>")
        lines.extend(f"• {quote(note)}" for note in vote)
    if dark:
        if lines:
            lines.append("")
        lines.append(f"⚠️ <b>Backfill: {len(dark)} instrument(s) went dark</b>")
        for asset_id, provider, err in dark[:20]:
            lines.append(f"• {asset_id} ({provider}): {quote(err)}")
        if len(dark) > 20:
            lines.append(f"• …and {len(dark) - 20} more")
    return "\n".join(lines)


def send_ops_alert(text: str) -> None:
    """The health chat (price_monitor.notifier.send_health), never the channel."""
    send_health(text)


def _days_since(start: date) -> float:
    return max(1.0, (datetime.now(timezone.utc) - datetime.combine(
        start, datetime.min.time(), tzinfo=timezone.utc)).total_seconds() / 86400)


# Overlap the newest stored bar by this many hours on a forward fetch, and
# refuse to skip a fetch that is this close to that bar. A bar served while
# its hour was still open is revised later; merge keeps the new copy.
SETTLE_HOURS = 3


def fetch_missing(asset: Asset, path: str, since: date, api_key: str,
                  session: requests.Session, extend_history: bool = False,
                  tiingo_key: str = "",
                  now: datetime | None = None, sifting_key: str = "",
                  recount: "list[int] | None" = None) -> int:
    """Fetches whatever the store does not have yet: from the last saved bar up
    to now, or from `since` when the store is empty.

    The ordinary forward fetch starts at the newest stored bar minus
    SETTLE_HOURS, so a bar served while its hour was still open is re-asked.
    bars.merge keeps the later copy.

    `extend_history` asks from `since` even when the store already has data. The
    ordinary path only ever reaches FORWARD from the last saved bar, which is
    right for a daily top-up and useless for deepening the archive: moving
    fetch_since earlier changes nothing without it, because the store is not
    empty and the window is measured from its newest bar rather than its oldest.
    bars.merge takes the union, so the old rows survive and only genuinely new
    ones are added.
    """
    stored = bars.load(path)
    now = now or datetime.now(timezone.utc)
    end: datetime | None = None
    if stored.empty:
        # A never-seen instrument gets ONE chunk here and leaves the archive to
        # the deepening workflow. Walking to the 2002 floor is about thirty
        # chunks paced eight seconds apart - four minutes and thirty credits per
        # instrument, which for a batch of new tickers is hours of wall clock
        # inside a job meant to take ninety seconds. One chunk gets the
        # instrument producing bars now; depth is a separate, deliberate act.
        days = float(CHUNK_DAYS[asset.fetch_interval]) if not extend_history \
            else _days_since(since)
    elif extend_history:
        # Deepening: the walk goes backwards, so it starts at the oldest bar
        # already held rather than at today. Starting at today would spend a
        # credit per chunk re-fetching years that are already on disk before
        # reaching any new ground, and the free tier's 800 a day is the real
        # ceiling on how far back a run gets.
        end = datetime.fromtimestamp(int(stored["hour_utc"].min()), tz=timezone.utc)
        days = max(1.0, (end - datetime.combine(
            since, datetime.min.time(), tzinfo=timezone.utc)).total_seconds() / 86400)
    else:
        last = datetime.fromtimestamp(int(stored["hour_utc"].max()), tz=timezone.utc)
        start = last - timedelta(hours=SETTLE_HOURS)
        if recount:
            # A vote due a count again (jump.verify.recount_hours): the store's
            # own provider is asked back to its hour in the same request, and a
            # bar it has corrected since replaces the stored one, as the last
            # three hours' do.
            start = min(start, datetime.fromtimestamp(min(recount), tz=timezone.utc))
        days = max(SETTLE_HOURS / 24.0, (now - start).total_seconds() / 86400)

    # WHICH PROVIDER ANSWERS, and it is not always the fast one. Deepening walks
    # BACKWARDS through history, and only the archive provider holds it: Yahoo
    # serves at most 59 days of 30-minute bars and Tiingo caps a response at
    # 10000 rows. `source` is the provider the archive came from, so that is who
    # a deepening run asks, however the hourly top-up is routed.
    provider = asset.source if extend_history else asset.fetched_from
    # A first fetch of an empty store asks for a whole chunk - 300 days at 30
    # minutes - which is more than the recent-end providers will answer. Clamp
    # it rather than fail: the point of the seeding path is to get an instrument
    # producing bars now, and depth is a separate, deliberate act either way.
    reach = _PROVIDER_REACH.get(provider, {}).get(asset.fetch_interval)
    if reach is not None and days > reach:
        log.info("%s: %s serves %d days at %s, not %.0f - asking for what it has",
                 asset.asset_id, provider, reach, asset.fetch_interval, days)
        days = float(reach)

    if provider == "twelvedata":
        candles = twelvedata.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            base_url=TWELVEDATA_BASE_URL, api_key=api_key, session=session,
            request_delay_seconds=TWELVEDATA_DELAY_SECONDS,
            chunk_days=CHUNK_DAYS[asset.fetch_interval], end=end,
        )
    elif provider == "binance":
        candles = binance.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            session=session, end=end,
        )
    elif provider == "tiingo":
        candles = tiingo.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            base_url=TIINGO_BASE_URL, api_key=tiingo_key, session=session, end=end,
        )
    elif provider == "sifting":
        candles = sifting.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            api_key=sifting_key, session=session, end=end,
        )
    elif provider == "yahoo" and futures.front_contract(asset.ticker, now.date()):
        # A rolled futures series: the front contract's own bars, from the
        # session it became front - never Yahoo's continuous series, which
        # mixes in other contracts' prints (jump.futures). Asked from the
        # session it became front, every run: an open month's hours since
        # then are this contract's even when the roll calendar moved under
        # them (live cattle's, 2026-10-07), and one request covers it.
        symbol, since_day = futures.front_contract(asset.ticker, now.date())
        floor = _sessions.daily_session_open(since_day, asset.session_template)
        days = max(days, (now.timestamp() - floor) / 86400)
        candles = yahoo.fetch_full_history(
            symbol=symbol, interval=asset.fetch_interval, days=days,
            base_url=YAHOO_BASE_URL, session=session, end=end)
        candles = [c for c in candles if c.open_time >= floor]
    elif provider == "yahoo":
        candles = yahoo.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            base_url=YAHOO_BASE_URL, session=session, end=end,
        )
    elif provider == "alpaca":
        # IEX, live: the half-hour bars of the hours since the newest stored one.
        stop = end or now
        candles = alpaca.fetch_history(
            asset.ticker, stop - timedelta(days=days), stop,
            alpaca.headers(os.environ.get("ALPACA_KEY_ID", "").strip(),
                           os.environ.get("ALPACA_SECRET_KEY", "").strip()),
            session, feed="iex")
    elif provider == "sina" and asset.session_template == CALENDAR_TEMPLATE:
        # A US fund: its last ~78 days of half-hour bars, whatever `days` asks.
        candles = sina.fetch_us_bars(asset.ticker, session)
    elif provider == "sina" and asset.session_template == "lme":
        # An LME metal: its last 1,023 hourly bars, about three months.
        candles = sina.fetch_bars(asset.ticker, session, url=sina.GLOBAL_URL)
    elif provider == "google":
        # The latest session, whatever `days` asks: the page holds no more.
        candles = google.fetch_full_history(
            symbol=asset.ticker, interval=asset.fetch_interval, days=days,
            session=session, end=end,
        )
    else:
        raise ExchangeError(f"{asset.asset_id}: unknown provider '{provider}'")

    return bars.merge(path, bars.to_hourly(bars.candles_to_frame(candles)))


# A fund whose dividend record has not been confirmed for this long is worth a
# line on the health chat: its overnight gap has been unscored all that time,
# and nothing else would say so. Five calendar days is a long weekend plus a
# failed morning, not one bad hour.
DIVIDEND_CHECK_STALE_DAYS = 5


def check_dividends(funds: "list[Asset]", table: "dict | None",
                    session: requests.Session, now: datetime | None = None,
                    actions_path: str = corporate_actions.DEFAULT_ACTIONS_PATH,
                    checks_path: str = corporate_actions.DEFAULT_CHECKS_PATH) -> dict:
    """Confirms each fund's payouts through today, once a session, after the open.

    THE OVERNIGHT GAP DEPENDS ON THIS. An ex-date drop the table has not heard
    of reads as a gap the size of the dividend - about eighteen false events a
    year across the basket, seven of them BKLN's - so a gap is scored only on a
    date its fund has been checked through (returns.overnight_gaps). The manual
    Tiingo refresh cannot do that job: it runs when someone runs it, and
    Tiingo's row for an ex-date lands only after that session closes.

    Yahoo, because it is already a provider here, needs no key, and answers
    payouts and daily closes in one request. After the open because the answer
    has to cover TODAY's ex-date, and on trading days only because no gap exists
    on any other. Payouts are appended before the checked-through dates move, so
    a run killed between the two writes leaves a fund unchecked rather than
    vouched for without its dividend.

    A failed fund keeps its old date and is asked again next hour; its gap stays
    unscored meanwhile, which is the safe direction.

    The same answer carries the fund's splits, recorded as declared: a split a
    provider has not adjusted would read as an overnight crash of its size.
    """
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo("America/New_York")
    today = now.astimezone(zone).date()
    if not table or today not in table:
        return {"skipped": "not a trading day", "checked": 0, "added": 0, "failed": []}
    opened, _ = quality._session_bounds_utc(today, table[today])
    if now.timestamp() < opened:
        return {"skipped": "before the open", "checked": 0, "added": 0, "failed": []}

    checks = corporate_actions.load_checks(checks_path)
    due = [a for a in funds if checks.get(a.ticker, "") < today.isoformat()]
    found: list[corporate_actions.CorporateAction] = []
    failed: list[str] = []
    checked = 0
    unanswered = 0
    for position, asset in enumerate(due):
        since = (date.fromisoformat(checks[asset.ticker]) + timedelta(days=1)
                 if asset.ticker in checks else today - timedelta(days=30))
        try:
            pairs, splits = yahoo.fetch_dividends_and_splits(asset.ticker, since,
                                                             session=session, now=now)
        except yahoo.RateLimited as exc:
            log.warning("dividend check: Yahoo rate-limited at %s - %s",
                        asset.ticker, exc)
            failed.extend(a.ticker for a in due[position:])
            break
        except Unreachable as exc:
            log.warning("dividend check: %s failed - %s", asset.ticker, exc)
            failed.append(asset.ticker)
            unanswered += 1
            if unanswered >= UNANSWERED_IN_A_ROW:
                log.warning("dividend check: Yahoo did not answer %d funds in a row; "
                            "the rest wait for the next run", unanswered)
                failed.extend(a.ticker for a in due[position + 1:])
                break
            continue
        except Exception as exc:
            log.warning("dividend check: %s failed - %s", asset.ticker, exc)
            failed.append(asset.ticker)
            unanswered = 0
            continue
        unanswered = 0
        found.extend(corporate_actions.CorporateAction(
            ticker=asset.ticker, day=day, kind="dividend", factor_step=step)
            for day, step in pairs)
        # Declared, so that day's gap is not scored whatever the ratio (the
        # split rows' factor_step is 1.0, as the Tiingo path writes them).
        found.extend(corporate_actions.CorporateAction(
            ticker=asset.ticker, day=day, kind="split", factor_step=1.0)
            for day in splits)
        checks[asset.ticker] = today.isoformat()
        checked += 1

    added = corporate_actions.merge_actions(found, actions_path) if found else 0
    if checked:
        corporate_actions.write_checks(checks, checks_path)
    # Named once a day - on the first run after the open - rather than every
    # hour a fund stays behind, so a broken check is one line and not a stream.
    first_run = now.timestamp() < opened + 3600
    horizon = (today - timedelta(days=DIVIDEND_CHECK_STALE_DAYS)).isoformat()
    return {"skipped": None, "checked": checked, "added": added, "failed": failed,
            "stale": sorted(a.ticker for a in funds
                            if checks.get(a.ticker, "") < horizon) if first_run else []}


# How far back the skip looks for the newest in-session bar: more than a
# weekend's worth of hourly bars served while the market was shut.
SKIP_LOOKBACK_BARS = 240


def nothing_can_have_appeared(asset: Asset, path: str,
                              table: "dict | None",
                              now: datetime | None = None) -> bool:
    """True when the calendar says no new bar can exist for this instrument yet.

    EVERY GUARD HERE IS AGAINST THE SAME MISTAKE - skipping a fetch that would
    have returned something. A missed bar is a hole in the history and a move
    the detector never sees, which is far worse than a wasted request, so each
    condition below refuses to skip unless it is certain:

      - crypto_24_7 is never skipped: it genuinely trades every hour.
      - fx_continuous uses the same Sun 17:00 → Fri 17:00 New York week as the
        bar walk (`instrument_day_hours`), so the skip cannot disagree with
        what the walk considers a bar.
      - us_equity still needs the session table; a missing or short table never
        causes a skip.
      - a daily-session market (nickel, futures, the real) uses its own
        open and close, as the gate does (sessions.DAILY_SESSIONS).
      - never on an empty store, where there is no newest bar to reason from.
      - never within SETTLE_HOURS of the newest stored bar, so a bar served
        while its hour was still open is re-asked for.
      - and finally, only when no expected hour at all sits between the newest
        stored bar and now.
    """
    template = asset.session_template
    if template == "crypto_24_7":
        return False
    if template == "us_equity" and not table:
        return False
    if template not in ("us_equity", "fx_continuous") and \
            not _sessions.is_calendar_template(template):
        return False

    stored = bars.load(path)
    if stored.empty:
        return False

    now = now or datetime.now(timezone.utc)
    # The newest bar INSIDE the session. A provider may serve bars while the
    # market is shut - SiftingIO through the FX weekend, the real round the
    # clock - and they are stored for the quality gate to drop. Measured from
    # them, the newest bar is always an hour old and the instrument is asked
    # every closed hour: for the currency pairs, past SiftingIO's monthly quota.
    recent = stored["hour_utc"].astype("int64").tail(SKIP_LOOKBACK_BARS)
    inside = recent[quality.in_session(asset, recent, table).to_numpy(dtype=bool)]
    newest = int((inside if not inside.empty else recent).max())
    if now.timestamp() - newest < SETTLE_HOURS * 3600:
        return False
    if template == "us_equity" and max(table) < now.date():
        return False

    since = datetime.fromtimestamp(newest, tz=timezone.utc).date()
    expected: list[int] = []
    day = since
    while day <= now.date():
        expected.extend(_sessions.instrument_day_hours(day, template, table))
        day += timedelta(days=1)
    # Only hours already begun: the day's later session hours are on the
    # calendar from 00:00 UTC, but nothing of them can exist before they start.
    return not any(newest < hour <= now.timestamp() for hour in expected)


# How many of an instrument's session hours may end with no bar stored before
# it is named stale - asked, answered, and nothing new. Over the 90 days to
# 2026-09-29 the longest such runs were: funds 5 (SLX, a thin day), pairs 3
# (the week's open), coins 0, and a daily-session market one whole session on
# a holiday its calendar does not know and then some thin hours - the LME 18
# on a UK bank holiday, cotton 27 by Monday 10:00 after Independence Day -
# hence two sessions' bars there. Replayed at every run of those 90 days, the
# limits name nothing but cocoa's three weeks without a bar (2026-07-21..08-10),
# which went unnoticed.
STALE_AFTER_HOURS = {"crypto_24_7": 3, "fx_continuous": 6, "us_equity": 7}


def stale_limit(template: str) -> int:
    if template in STALE_AFTER_HOURS:
        return STALE_AFTER_HOURS[template]
    return 2 * _sessions.daily_bars_per_day(template)


def stale_hours(asset: Asset, path: str, table: "dict | None",
                now: datetime | None = None) -> "tuple[int, bool]":
    """How many of the instrument's session hours have ended since its newest
    stored in-session bar, by the same calendar as the skip rule, and whether
    the count just moved - one of them ended within the hour. Overnight and at
    weekends it stands still. (0, False) without a store or (for funds) without
    the session table."""
    template = asset.session_template
    if template == "us_equity" and not table:
        return 0, False
    now = now or datetime.now(timezone.utc)
    newest = None
    # The last 40 days answer almost every time; the whole store otherwise, so
    # an instrument dark for months is not read as fresh.
    for since in (int(now.timestamp()) - 40 * 86400, None):
        stored = bars.load(path, since=since)
        recent = stored["hour_utc"].astype("int64")
        inside = recent[quality.in_session(asset, recent, table).to_numpy(dtype=bool)]
        if not inside.empty:
            newest = int(inside.max())
            break
    if newest is None:
        return 0, False
    ended, day = [], datetime.fromtimestamp(newest, tz=timezone.utc).date()
    while day <= now.date():
        ended += [h for h in _sessions.instrument_day_hours(day, template, table)
                  if newest < h and h + 3600 <= now.timestamp()]
        day += timedelta(days=1)
    return len(ended), bool(ended) and max(ended) + 3600 > now.timestamp() - 3600


def stale_to_name(asset: Asset, hours: int, ticking: bool) -> bool:
    """Named on the run its count passes the limit, then about once a day - a
    day being the template's bars in one (24 for coins and pairs) - so a long
    outage is a line a day, not a line an hour. Only on a run the count moved
    (`ticking`): standing still overnight, it would repeat every hour. A run
    missed on the day it passes is caught by the next day's."""
    limit = stale_limit(asset.session_template)
    if hours <= limit or not ticking:
        return False
    if asset.session_template in ("crypto_24_7", "fx_continuous"):
        per_day = 24
    elif asset.session_template == "us_equity":
        per_day = 7
    else:
        per_day = _sessions.daily_bars_per_day(asset.session_template)
    return (hours - limit - 1) % per_day == 0


# A minute and a second between batched Twelve Data requests: each spends the
# whole minute's credits.
TWELVEDATA_BATCH_GAP_SECONDS = 61.0


def fetch_twelvedata_live(assets: "list[Asset]", bars_dir: str, api_key: str,
                          now: datetime | None = None, sleep=time.sleep,
                          session: "requests.Session | None" = None,
                          recount: "dict[str, list[int]] | None" = None
                          ) -> "list[tuple[Asset, dict | Exception]]":
    """The hourly fetch of the Twelve Data funds, BATCH_SIZE symbols a request.

    It runs in a thread beside the rest of the pass (main), so the minute
    between two batches is spent while Tiingo, Alpaca, Yahoo and the others are
    being asked, not on top of them. Only stores that already hold bars come
    here; a new one is seeded on the ordinary path. Each batch asks from the
    earliest of its funds' newest-bar-minus-SETTLE_HOURS, or of their votes due a
    count again (`recount`, as fetch_missing): one more hour or two for some of
    them costs nothing, a symbol being one credit whatever its rows.
    Returns, per fund, the same summary as backfill_instrument or the error.
    """
    now = now or datetime.now(timezone.utc)
    # Its own session, the thread's: a Session is not shared across threads.
    session = session or requests.Session()
    results: "list[tuple[Asset, dict | Exception]]" = []
    groups: dict = {}
    for asset in assets:
        groups.setdefault(asset.fetch_interval, []).append(asset)
    batches = [members[k:k + twelvedata.BATCH_SIZE]
               for members in groups.values()
               for k in range(0, len(members), twelvedata.BATCH_SIZE)]
    for n, batch in enumerate(batches):
        if n:
            sleep(TWELVEDATA_BATCH_GAP_SECONDS)
        paths = {a.ticker: bars.store_path(bars_dir, a.file_stem) for a in batch}
        newest = min(int(bars.load(p)["hour_utc"].max()) for p in paths.values())
        start = (datetime.fromtimestamp(newest, tz=timezone.utc)
                 - timedelta(hours=SETTLE_HOURS))
        voted = [h for a in batch for h in (recount or {}).get(a.asset_id, [])]
        if voted:
            start = min(start, datetime.fromtimestamp(min(voted), tz=timezone.utc))
        try:
            got = twelvedata.fetch_batch([a.ticker for a in batch], batch[0].fetch_interval,
                                         start, now, TWELVEDATA_BASE_URL, api_key, session)
        except (twelvedata.DailyQuotaExhausted, KeyRefused) as exc:
            # Every later batch would be told the same.
            results += [(a, exc) for b in batches[n:] for a in b]
            break
        except Exception as exc:
            results += [(a, exc) for a in batch]
            continue
        for asset in batch:
            answer = got.get(asset.ticker)
            if isinstance(answer, Exception):
                results.append((asset, answer))
                continue
            path = paths[asset.ticker]
            added = bars.merge(path, bars.to_hourly(bars.candles_to_frame(answer)))
            stored = bars.load(path)
            results.append((asset, {
                "asset_id": asset.asset_id, "from_api": added,
                "rows": len(stored), "first": int(stored["hour_utc"].min()),
                "last": int(stored["hour_utc"].max())}))
    return results


def backfill_instrument(asset: Asset, basket: Basket, bars_dir: str, api_key: str,
                        session: requests.Session, extend_history: bool = False, tiingo_key: str = "",
                        sifting_key: str = "", recount: "list[int] | None" = None) -> dict:
    path = bars.store_path(bars_dir, asset.file_stem)
    from_api = fetch_missing(asset, path, basket.acquire_since, api_key, session,
                             extend_history, tiingo_key, sifting_key=sifting_key,
                             recount=recount)
    stored = bars.load(path)
    return {
        "asset_id": asset.asset_id,
        "from_api": from_api,
        "rows": len(stored),
        "first": int(stored["hour_utc"].min()) if not stored.empty else None,
        "last": int(stored["hour_utc"].max()) if not stored.empty else None,
    }


def _vix_day(epoch: int) -> date:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).date()


def _latest_vix_day_available(now: datetime) -> date | None:
    """Newest observation day whose CBOE or FRED gate has already opened."""
    d = now.date()
    now_ts = now.timestamp()
    for _ in range(21):
        if now_ts >= cboe.available_at(d) or now_ts >= fred.available_at(d):
            return d
        d -= timedelta(days=1)
    return None


def _vix_frames_equal(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    cols = ["day", "close", "available_at"]
    if left.empty and right.empty:
        return True
    if len(left) != len(right) or left.empty or right.empty:
        return False
    a = left[cols].sort_values("day").reset_index(drop=True)
    b = right[cols].sort_values("day").reset_index(drop=True)
    return a.equals(b)


def backfill_vix(basket: Basket, vix_dir: str, api_key: str,
                 session: requests.Session,
                 now: datetime | None = None) -> dict:
    """The daily VIX series, from both sources that serve it.

    CBOE computes the index and posts the close the same evening; FRED
    republishes it on the next business day. Taking both costs one extra HTTP
    call and buys two things: the gauge stops running up to three calendar days
    behind over a weekend, and neither source is a single point of failure -
    a run with no FRED key still has a series, which it did not before.

    Neither is trusted over the other, because measured over the whole record
    they agree to the cent on all 9,270 days they share. They are unioned: CBOE
    carries the newest day, FRED carries 1999-12-31, which CBOE's file omits.

    The series gains at most one value a day. CBOE's endpoint takes no range
    parameters (~400 KB every time), so the only saving is not calling it.
    When the store already covers what the availability gates say can exist,
    both fetches and the parquet write are skipped.
    """
    vix = basket.volatility_index
    path = os.path.join(vix_dir, f"{vix.file_stem}.parquet")
    stored = pd.DataFrame()
    if os.path.exists(path):
        stored = pd.read_parquet(path).sort_values("day").reset_index(drop=True)

    now = now or datetime.now(timezone.utc)
    latest = _latest_vix_day_available(now)
    if not stored.empty and latest is not None:
        newest = _vix_day(int(stored["day"].max()))
        if newest >= latest:
            log.info("VIX: stored through %s already covers what can exist; not fetched",
                     newest)
            return {"asset_id": vix.series_id, "rows": len(stored),
                    "first": int(stored["day"].min()), "last": int(stored["day"].max()),
                    "sources": ["stored"], "trouble": []}

    pieces, sources, trouble = [], [], []
    try:
        pieces.append(cboe.fetch_vix_history(vix.history_since, session=session))
        sources.append("cboe")
    except Exception as exc:
        trouble.append(f"cboe: {exc}")

    fred_start = vix.history_since
    if not stored.empty:
        newest = _vix_day(int(stored["day"].max()))
        # Wide enough to span a holiday stretch; empty FRED raises.
        fred_start = max(vix.history_since, newest - timedelta(days=21))

    if api_key:
        try:
            pieces.append(fred.fetch_series(vix.series_id, api_key,
                                            fred_start, session=session))
            sources.append("fred")
        except Exception as exc:
            trouble.append(f"fred: {exc}")
    else:
        trouble.append("fred: FRED_API_KEY is not set")

    if not stored.empty:
        pieces.append(stored)

    frame = cboe.merge(*pieces)
    if frame.empty:
        raise RuntimeError("no VIX source answered - " + "; ".join(trouble))

    if _vix_frames_equal(frame, stored):
        log.info("VIX: merged frame equals the store; parquet not rewritten")
        return {"asset_id": vix.series_id, "rows": len(frame),
                "first": int(frame["day"].min()), "last": int(frame["day"].max()),
                "sources": sources, "trouble": trouble}

    os.makedirs(vix_dir, exist_ok=True)
    atomic.write_parquet(path, frame)
    return {"asset_id": vix.series_id, "rows": len(frame),
            "first": int(frame["day"].min()), "last": int(frame["day"].max()),
            "sources": sources, "trouble": trouble}


def _fmt(epoch: int | None) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d") if epoch else "-"


# The NYSE table describes the US equity session and nothing else. A currency
# pair trading around the clock has no "missing Tuesday" to find against it, so
# gap filling is offered only where the calendar is authoritative.
CALENDAR_TEMPLATE = "us_equity"

# How many days either side of a run of missing sessions to ask for. A window
# rather than the exact day because a request for a single date can land on the
# wrong side of the provider's own boundary handling, and the extra rows merge
# away for free.
GAP_PADDING_DAYS = 1


def missing_sessions(path: str, table: dict) -> list[date]:
    """Trading days the calendar has and the store does not.

    Bounded by what is stored: a day before the first bar or after the last is
    not a gap, it is simply outside the archive, and reporting those would bury
    the real holes under thousands of them.
    """
    stored = bars.load(path)
    if stored.empty:
        return []
    days = set(pd.to_datetime(stored["hour_utc"], unit="s", utc=True).dt.date)
    lo, hi = min(days), max(days)
    return sorted({d for d in table if lo <= d <= hi} - days)


def missing_hours(path: str, table: dict) -> list[int]:
    """Hours the calendar has and the store does not, inside the stored range.

    The day-level view above cannot see these. A day that holds three of its
    seven bars is present, so it is not a gap by that measure, and every hole
    inside a day stayed invisible for as long as whole days were the unit of
    counting - which is how a five-hour hole on 2020-02-19 survived a gap fill
    that reported itself complete.

    That matters more than the count suggests. An hourly return is taken
    between consecutive STORED bars, so a missing hour does not shrink the
    series, it silently turns a one-hour return into a two-hour one - a larger
    move measured against a one-hour scale. The holes are rare, but they
    inflate exactly the quantity the detector scores.
    """
    stored = bars.load(path)
    if stored.empty:
        return []
    have = set(stored["hour_utc"].astype(int))
    days = pd.to_datetime(stored["hour_utc"], unit="s", utc=True).dt.date
    want = _sessions.expected_hours(table, min(days), max(days))
    return sorted(want - have)


def _runs(days: list[date]) -> list[tuple[date, date]]:
    """Consecutive missing days folded into single spans, to save requests."""
    spans: list[tuple[date, date]] = []
    for day in days:
        if spans and (day - spans[-1][1]).days <= 3:
            spans[-1] = (spans[-1][0], day)
        else:
            spans.append((day, day))
    return spans


def fill_gaps(asset: Asset, path: str, table: dict, api_key: str,
              session: requests.Session, ask: bool = True) -> dict:
    """Re-asks the provider for the sessions the store is missing.

    Whether the day is recoverable at all is the point of running this: the
    provider may simply not hold it, in which case the request comes back empty
    and the gap is confirmed as theirs rather than ours. Either answer is worth
    having, and only one of them costs a credit. With `ask` false nothing is
    requested and the gaps are only listed (Twelve Data's credits are spent).
    A spent budget raises DailyQuotaExhausted: every later request would be
    refused the same way.
    """
    if asset.session_template != CALENDAR_TEMPLATE:
        return {"skipped": "no authoritative calendar", "added": 0, "gaps": 0}
    if asset.source != "twelvedata":
        # Deliberately `source`, not `fetched_from`. This walks backwards
        # through history, which Twelve Data holds and the fast providers do
        # not: Yahoo serves at most 59 days of 30-minute bars and Tiingo caps a
        # response at 10000 rows. An instrument moved to another provider for
        # its hourly top-up is still filled from Twelve Data here.
        return {"skipped": f"source {asset.source} not supported here",
                "added": 0, "gaps": 0}

    gaps = missing_sessions(path, table)
    if not gaps:
        return {"skipped": None, "added": 0, "gaps": 0, "still_missing": []}
    if not ask:
        return {"skipped": None, "added": 0, "gaps": len(gaps), "still_missing": gaps}

    added = 0
    for start, end in _runs(gaps):
        window_end = datetime.combine(end, datetime.min.time(),
                                      tzinfo=timezone.utc) + timedelta(
            days=GAP_PADDING_DAYS + 1)
        span = (end - start).days + 2 * GAP_PADDING_DAYS + 1
        try:
            candles = twelvedata.fetch_full_history(
                symbol=asset.ticker, interval=asset.fetch_interval, days=span,
                base_url=TWELVEDATA_BASE_URL, api_key=api_key, session=session,
                request_delay_seconds=TWELVEDATA_DELAY_SECONDS,
                chunk_days=CHUNK_DAYS[asset.fetch_interval], end=window_end)
        except twelvedata.DailyQuotaExhausted as exc:
            exc.added = added           # what the earlier gaps already stored
            raise
        except ExchangeError as exc:
            log.warning("%s: %s..%s could not be re-fetched: %s",
                        asset.asset_id, start, end, exc)
            continue
        if candles:
            added += bars.merge(path, bars.to_hourly(bars.candles_to_frame(candles)))
        time.sleep(TWELVEDATA_DELAY_SECONDS)

    left = missing_sessions(path, table)
    return {"skipped": None, "added": added, "gaps": len(gaps),
            "still_missing": left}


# How closely the re-derived hourly bars must track what is already stored
# before any of them are kept. A timezone read wrong shifts four months of
# every year by an hour, which does not look like an error - it looks like
# noise, and scores about 0.5 where the truth scores 0.99. HistData, tested
# the same way, gave 0.94 correctly localised and 0.53 read as UTC, so the
# gap between right and wrong is wide and the bar can sit well inside it.
ALIGNMENT_MIN_CORRELATION = 0.90
ALIGNMENT_MIN_HOURS = 200

# And how far apart the LEVELS may be. This is a separate question from the one
# above and the first version of this check did not ask it, which let 30024
# dividend-adjusted SPY bars into the store.
#
# Adjustment is multiplicative, so it barely touches returns: that import
# scored 0.9959 on correlation while sitting 99 basis points below the stored
# prices, and the gap grew the further back it went - 0.972x of the true close
# at the end of 2019, 0.885x in 2015, 0.733x in 2005, which is SPY's dividends
# compounded. Spliced under an unadjusted store it put a 3.07% step across one
# weekend at the seam.
#
# Two sources reporting the same consolidated tape should agree on the price of
# SPY to a basis point or two, so 25 is loose enough for timing differences
# inside a minute and nowhere near loose enough to admit a payout.
ALIGNMENT_MAX_MEDIAN_BP = 25.0


def verify_alignment(minutes: "pd.DataFrame", stored: "pd.DataFrame") -> dict:
    """Checks re-derived bars against the ones already held, over their overlap.

    The overlap is the hours both hold, which the store can already price
    independently. The years being imported have no second opinion at all,
    which is exactly why the years that do have one are made to earn the rest.
    """
    import numpy as np

    hourly = bars.to_hourly(minutes)
    # A bar removed on purpose has no price to compare (bars.removed).
    stored = stored[~bars.removed(stored).to_numpy()]
    joined = hourly.merge(stored[["hour_utc", "close"]], on="hour_utc",
                          how="inner", suffixes=("_new", "_stored"))
    if len(joined) < ALIGNMENT_MIN_HOURS:
        return {"hours": len(joined), "correlation": float("nan"),
                "median_bp": float("nan"), "ok": False,
                "why": f"only {len(joined)} overlapping hours"}

    new = np.log(joined["close_new"].to_numpy())
    old = np.log(joined["close_stored"].to_numpy())
    returns_new, returns_old = np.diff(new), np.diff(old)
    good = np.isfinite(returns_new) & np.isfinite(returns_old)
    correlation = float(np.corrcoef(returns_new[good], returns_old[good])[0, 1])
    median_bp = float(np.median(
        np.abs(joined["close_new"] - joined["close_stored"])
        / joined["close_stored"]) * 1e4)
    # Where the two disagree most, for a refusal to be read rather than guessed.
    gap = np.abs(returns_new - returns_old)
    worst = [(datetime.fromtimestamp(int(joined["hour_utc"].iloc[i + 1]), tz=timezone.utc)
              .strftime("%Y-%m-%d %H:%M"), round(float(returns_new[i]) * 1e4),
              round(float(returns_old[i]) * 1e4))
             for i in np.argsort(np.where(good, gap, -1))[::-1][:3]]
    reasons = []
    if not correlation >= ALIGNMENT_MIN_CORRELATION:
        reasons.append(f"correlation {correlation:.4f} below "
                       f"{ALIGNMENT_MIN_CORRELATION}")
    if not median_bp <= ALIGNMENT_MAX_MEDIAN_BP:
        reasons.append(f"median level gap {median_bp:.1f}bp above "
                       f"{ALIGNMENT_MAX_MEDIAN_BP}bp - the series disagree on "
                       f"PRICE while agreeing on returns, which is what a "
                       f"dividend adjustment looks like")
    if reasons:
        reasons.append("worst hours (new bp, stored bp): " + ", ".join(
            f"{t} {a:+d}/{b:+d}" for t, a, b in worst))
    return {"hours": len(joined), "correlation": correlation,
            "median_bp": median_bp, "ok": not reasons,
            "why": "; ".join(reasons), "worst": worst}


# A fund's ticker before a rename, for the history sources that file the years
# before it under the old name (probed 2026-10-02): Alpaca serves CIU 2016 to
# 2018-07 and CRED 2016 on. Only ever a candidate: the history it brings is written only if it
# passes the same overlap gate against the stored bars as any other source.
FORMER_TICKERS = {"IGIB": "CIU", "USIG": "CRED"}


# How far back a repair reaches: Yahoo's 30-minute bars, a day short of their
# limit (yahoo.MAX_LOOKBACK_DAYS), so the request is never refused at the edge.
REPAIR_DAYS = yahoo.MAX_LOOKBACK_DAYS["30min"] - 1


def repair_from_yahoo(asset: Asset, path: str, table: dict, session: requests.Session,
                      now: datetime | None = None) -> dict:
    """Fills a fund's missing hours of the last REPAIR_DAYS from Yahoo's
    30-minute bars, folded to the hour - a source the funds are voted on
    already, and the only free one that reaches back weeks.

    For holes in the open month (python -m jump.backfill --repair, run by hand
    from the hourly workflow, which alone may save the open months). One went
    in when Google served TUR only its latest session: 2026-10-01 and -02
    were never fetched.

    Yahoo's intraday bars are not dividend-adjusted, like the store, so nothing
    is unadjusted. Written only if the overlap passes the same gate as any
    import (verify_alignment), and only the hours the store lacks: the live
    source keeps its own bars."""
    if asset.session_template != CALENDAR_TEMPLATE:
        return {"skipped": "no authoritative calendar", "added": 0, "hours": 0}
    now = now or datetime.now(timezone.utc)
    since = int(now.timestamp()) - REPAIR_DAYS * 86400
    wanted = {h for h in missing_hours(path, table) if h >= since}
    if not wanted:
        return {"skipped": None, "added": 0, "hours": 0, "still_missing_hours": 0}

    candles = yahoo.fetch_full_history(asset.ticker, "30min", days=REPAIR_DAYS,
                                       session=session, end=now)
    minutes = bars.candles_to_frame(candles)
    if minutes.empty:
        return {"skipped": "Yahoo returned no bars", "added": 0, "hours": len(wanted)}
    stored = bars.load(path, since=since)
    check = verify_alignment(minutes, stored)
    if not check["ok"]:
        return {"skipped": f"alignment check failed: {check['why']}", "added": 0,
                "hours": len(wanted), "check": check}
    hourly = bars.to_hourly(minutes)
    patch = hourly[hourly["hour_utc"].isin(wanted).to_numpy()]
    added = bars.merge(path, patch) if not patch.empty else 0
    left = [h for h in missing_hours(path, table) if h >= since]
    return {"skipped": None, "added": added, "hours": len(wanted),
            "still_missing_hours": len(left), "check": check}


# How far ABOVE the oldest stored bar to fetch before writing anything below it.
# Dukascopy covers the whole stored range, so an overlap can be bought for six
# extra requests a pair and the splice can be gated on it instead of trusted.
# Three months of a 24/5 pair is about 1500 hours against the 200 the check needs.
DUKASCOPY_OVERLAP_DAYS = 93


def deepen_from_dukascopy(asset: Asset, path: str, since: date,
                          session: requests.Session) -> dict:
    """Fills an FX pair's history BELOW what is already stored, from Dukascopy.

    The overlap is fetched but NOT written. Its whole purpose is to earn the
    right to write the years underneath it: those years have no second opinion
    anywhere, and the only evidence available that this archive can be trusted
    to sit beside the stored bars is that where the two do meet, they agree.
    The same two gates as everywhere else - returns must correlate and the
    LEVELS must match, because a series that agrees on returns while disagreeing
    on price is what a bad scale or a stale rate looks like, and on this source
    a wrong point size is a factor of a thousand.

    Nothing is written unless both pass, and nothing is written at or above the
    oldest stored bar even then: the live provider keeps the recent end, and a
    merge lets the incoming row win.
    """
    symbol = dukascopy.symbol_for(asset.ticker)
    if symbol is None:
        return {"skipped": "no Dukascopy symbol", "added": 0}

    stored = bars.load(path)
    if stored.empty:
        # Deepening is defined against something, and with nothing stored there
        # is also nothing to check the archive against - which for this source
        # matters more than usual.
        return {"skipped": "nothing stored yet", "added": 0}

    floor = stored["hour_utc"].min()
    oldest = datetime.fromtimestamp(int(floor), tz=timezone.utc)
    if oldest.date() <= since:
        return {"skipped": "already reaches back far enough", "added": 0}

    end = oldest.date() + timedelta(days=DUKASCOPY_OVERLAP_DAYS)
    candles = dukascopy.fetch_history(symbol, since, end, session)
    if not candles:
        return {"skipped": "archive returned nothing", "added": 0}

    frame = bars.to_hourly(bars.candles_to_frame(candles))
    check = verify_alignment(frame, stored)
    if not check["ok"]:
        return {"skipped": f"alignment check failed: {check['why']}",
                "added": 0, "check": check}

    below = frame[frame["hour_utc"] < int(floor)]
    if below.empty:
        return {"skipped": "nothing below the oldest stored bar", "added": 0,
                "check": check}
    added = bars.merge(path, below)
    return {"skipped": None, "added": added, "fetched": len(candles),
            "from": since, "to": oldest.date(), "check": check}


# How far ABOVE the oldest stored bar Alpaca is asked, to buy the overlap the
# splice is gated on. A fund's store starts 2020-02-10; three months of seven
# hours a day is about 440 hours against the 200 the check needs, and it covers
# the March 2020 crash, where a timing or adjustment error would show loudest.
ALPACA_OVERLAP_DAYS = 93


def deepen_from_alpaca(asset: Asset, path: str, since: date, auth: dict,
                       session: requests.Session) -> dict:
    """Fills a US fund's history below what is stored, from Alpaca's
    consolidated tape (2016 on). Same gates as the Dukascopy deepening: the
    overlap is fetched and not written, and the years below are written only if
    returns correlate and LEVELS match there - a dividend-adjusted series agrees
    on returns and sits below the store on price, and the level gate refuses it.
    Nothing at or above the oldest stored bar is written."""
    if asset.session_template != CALENDAR_TEMPLATE:
        return {"skipped": "not a US-equity instrument", "added": 0}
    stored = bars.load(path)
    if stored.empty:
        return {"skipped": "nothing stored yet", "added": 0}
    floor = int(stored["hour_utc"].min())
    oldest = datetime.fromtimestamp(floor, tz=timezone.utc)
    start = max(datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc),
                alpaca.FIRST)
    if oldest <= start:
        return {"skipped": "already reaches back far enough", "added": 0}

    end = min(oldest + timedelta(days=ALPACA_OVERLAP_DAYS),
              datetime.now(timezone.utc) - timedelta(minutes=20))
    candles = alpaca.fetch_history(FORMER_TICKERS.get(asset.ticker, asset.ticker),
                                   start, end, auth, session)
    if not candles:
        return {"skipped": "Alpaca returned nothing", "added": 0}

    frame = bars.to_hourly(bars.candles_to_frame(candles))
    check = verify_alignment(frame, stored)
    if not check["ok"]:
        return {"skipped": f"alignment check failed: {check['why']}",
                "added": 0, "check": check}
    below = frame[frame["hour_utc"] < floor]
    if below.empty:
        return {"skipped": "nothing below the oldest stored bar (listed later)",
                "added": 0, "check": check}
    added = bars.merge(path, below)
    first = datetime.fromtimestamp(int(below["hour_utc"].min()), tz=timezone.utc).date()
    return {"skipped": None, "added": added, "fetched": len(candles),
            "from": first, "to": oldest.date(), "check": check}


# A session's first bar in Wallstreetcn's LME history can open on a print far
# off the market that is gone within the hour: aluminium opened 2026-05-15 and
# 06-01 so, and imported, each read as a major move. A first bar
# - after more than FIRST_BAR_GAP_HOURS without one - whose open is at least
# OPENING_OFF from the last close, and whose close is back at least as far the
# other way, is left out of the import: a hole, not a bar. In its bars of
# 2026-04 to 07, below Sina's, 8 of aluminium's 59 first bars, 3 of tin's 54 and
# none of nickel's 59; none since, in its bars or Sina's. For the LME's metals
# alone, whose hours move tens of bp.
OPENING_OFF = 0.01
FIRST_BAR_GAP_HOURS = 3


def opening_misprints(frame: "pd.DataFrame") -> "pd.Series":
    """Which bars open a session on a print gone within the hour (OPENING_OFF)."""
    import numpy as np

    first = frame["hour_utc"].diff().fillna(np.inf) > FIRST_BAR_GAP_HOURS * 3600
    night = np.log(frame["open"] / frame["close"].shift(1))
    back = np.log(frame["close"] / frame["open"])
    return (first & (night.abs() >= OPENING_OFF) & (back.abs() >= OPENING_OFF)
            & (np.sign(night) != np.sign(back)))


def deepen_from_wallstreetcn(asset: Asset, path: str, session: requests.Session,
                             now: datetime | None = None) -> dict:
    """Fills an LME metal's history below what is stored, from Wallstreetcn's
    hourly bars (price_monitor.wallstreetcn, about 170 days back). Gated like
    every import: its bars over the stored months must move with them and sit
    at their level (verify_alignment). Only its hours in the LME's session are
    taken, less the openings gone within the hour (opening_misprints), and
    nothing at or above the oldest stored bar: Sina keeps its own."""
    code = wallstreetcn.LME.get(asset.ticker)
    if code is None:
        return {"skipped": "not an LME metal", "added": 0}
    stored = bars.load(path)
    if stored.empty:
        return {"skipped": "nothing stored yet", "added": 0}
    floor = int(stored["hour_utc"].min())
    candles = wallstreetcn.fetch_hourly(code, session, now,
                                        days=wallstreetcn.MAX_TICKS / 24)
    if not candles:
        return {"skipped": "Wallstreetcn returned nothing", "added": 0}

    frame = bars.to_hourly(bars.candles_to_frame(candles))
    check = verify_alignment(frame, stored)
    if not check["ok"]:
        return {"skipped": f"alignment check failed: {check['why']}",
                "added": 0, "check": check}
    frame = frame[quality.in_session(asset, frame["hour_utc"]).to_numpy(bool)]
    frame = frame.sort_values("hour_utc").reset_index(drop=True)
    below = frame["hour_utc"] < floor
    off = opening_misprints(frame) & below
    patch = frame[(below & ~off).to_numpy()]
    if patch.empty:
        return {"skipped": "nothing below the oldest stored bar", "added": 0,
                "check": check}
    added = bars.merge(path, patch)
    day = lambda h: datetime.fromtimestamp(int(h), tz=timezone.utc)  # noqa: E731
    return {"skipped": None, "added": added, "from": day(patch["hour_utc"].min()).date(),
            "to": day(floor).date(), "check": check,
            "left_out": [f"{day(h):%Y-%m-%d %H:%M}" for h in frame.loc[off, "hour_utc"]]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Jump's hourly bars into the store")
    parser.add_argument("--instruments", default="",
                        help="Comma-separated tickers; the whole basket by default")
    parser.add_argument("--bars-dir", default=bars.DEFAULT_BARS_DIR)
    parser.add_argument("--vix-dir", default=bars.DEFAULT_VIX_DIR)
    parser.add_argument("--skip-vix", action="store_true")
    parser.add_argument("--extend-history", action="store_true",
                        help="ask from basket.fetch_since even where the store "
                             "already has bars, to deepen the archive backwards")
    parser.add_argument("--fill-gaps", action="store_true",
                        help="re-ask Twelve Data for trading days and hours "
                             "the NYSE calendar has and the store does not. "
                             "Whether a day is recoverable is the point: an "
                             "empty answer confirms the hole is the provider's.")
    parser.add_argument("--deepen-dukascopy", action="store_true",
                        help="fill the FX pairs' history below what is stored "
                             "from Dukascopy's public archive, which reaches "
                             "2003 and also carries USD/CNH. Needs no key. "
                             "Overlaps the store and is gated on agreeing with "
                             "it.")
    parser.add_argument("--deepen-alpaca", action="store_true",
                        help="fill the US funds' history below what is stored "
                             "from Alpaca's consolidated tape, 2016 on "
                             "(ALPACA_KEY_ID, ALPACA_SECRET_KEY). Overlaps the "
                             "store and is gated on agreeing with it.")
    parser.add_argument("--deepen-wallstreetcn", action="store_true",
                        help="fill the LME's metals' history below what is "
                             "stored from Wallstreetcn, about 170 days back, "
                             "less the openings gone within the hour. Needs no "
                             "key. Overlaps the store and is gated on agreeing "
                             "with it.")
    parser.add_argument("--live-pass", action="store_true",
                        help="the ordinary hourly fetch without the VIX, for "
                             "trying a provider change from the backfill "
                             "workflow on a few named instruments")
    parser.add_argument("--repair", action="store_true",
                        help="fill the named funds' missing hours of the last "
                             "REPAIR_DAYS from Yahoo, gated on agreeing with the "
                             "store: holes in the open month. Run from the hourly "
                             "workflow's `repair` input, which saves the open "
                             "months. Needs --instruments.")
    args = parser.parse_args(argv)
    # A history walk shares the Twelve Data key with the hourly run: it leaves
    # the run its minutes and its share of the day (twelvedata.ARCHIVE_CREDIT_CAP).
    twelvedata.archive_mode = bool(args.extend_history or args.fill_gaps)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.live_pass:
        args.skip_vix = True
    basket = load_basket()
    wanted = {t.strip() for t in args.instruments.split(",") if t.strip()}
    instruments = [a for a in basket.instruments if not wanted or a.ticker in wanted]
    if wanted and not instruments:
        log.error("No instrument matched --instruments %s", args.instruments)
        return 2

    if args.fill_gaps:

        api_key = os.environ.get("TWELVEDATA_API_KEY", "")
        if not api_key:
            log.error("TWELVEDATA_API_KEY is not set")
            return 2
        table = _sessions.load_sessions()
        session = requests.Session()
        filled = unfilled = 0
        td_spent = False
        for asset in instruments:
            path = bars.store_path(args.bars_dir, asset.file_stem)
            try:
                try:
                    out = fill_gaps(asset, path, table, api_key, session, ask=not td_spent)
                except twelvedata.DailyQuotaExhausted as exc:
                    # Said once: every later request would be refused the same
                    # way. The remaining gaps are only listed.
                    td_spent = True
                    log.warning("Twelve Data stops here - %s. The remaining gaps are "
                                "only listed.", exc)
                    out = fill_gaps(asset, path, table, api_key, session, ask=False)
                    out["added"] = getattr(exc, "added", 0)
            except Exception as exc:
                log.error("%s: gap fill failed - %s", asset.asset_id, exc)
                continue
            if out["skipped"]:
                continue
            filled += out["added"]
            left = out["still_missing"] if out["gaps"] else []
            if out["gaps"]:
                log.info("%s: %d gap(s), +%d bars from Twelve Data, %d still "
                         "missing%s", asset.asset_id, out["gaps"], out["added"],
                         len(left), f" {[str(d) for d in left]}" if left else "")

            # A day is not a unit of completeness. The store can hold a session
            # and still be missing five of its seven bars, and asking only "is
            # the day there" walks past that: the hours are counted too. Twelve
            # Data does not hold them - 0 of 21 recovered in run 33994110137 -
            # and they stay listed: no mode fills inside the store from another
            # source.
            hours_left = missing_hours(path, table)
            if not left and not hours_left:
                log.info("%s: no gaps", asset.asset_id)
                continue
            if hours_left and not left:
                log.info("%s: whole sessions complete, %d hour(s) missing "
                         "inside them", asset.asset_id, len(hours_left))
            unfilled += len(hours_left)
        log.info("gap fill: %d hour(s) recovered, %d confirmed missing at "
                 "the source", filled, unfilled)
        return 0

    if args.deepen_alpaca:
        key = os.environ.get("ALPACA_KEY_ID", "").strip()
        secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        if not key or not secret:
            log.error("ALPACA_KEY_ID / ALPACA_SECRET_KEY are not set")
            return 2
        auth = alpaca.headers(key, secret)
        session = requests.Session()
        total = 0
        for asset in instruments:
            path = bars.store_path(args.bars_dir, asset.file_stem)
            try:
                out = deepen_from_alpaca(asset, path, basket.acquire_since,
                                         auth, session)
            except Exception as exc:
                log.error("%s: Alpaca deepening failed - %s", asset.asset_id, exc)
                continue
            check = out.get("check")
            if out["skipped"]:
                if check:
                    log.info("%s: skipped (%s); overlap %d hours, corr %.4f, "
                             "median %.2fbp", asset.asset_id, out["skipped"],
                             check["hours"], check["correlation"],
                             check["median_bp"])
                else:
                    log.info("%s: skipped (%s)", asset.asset_id, out["skipped"])
                continue
            total += out["added"]
            log.info("%s: +%d bars from Alpaca (%s .. %s, %d half-hours fetched); "
                     "overlap check %d hours, corr %.4f, median %.2fbp",
                     asset.asset_id, out["added"], out["from"], out["to"],
                     out["fetched"], check["hours"], check["correlation"],
                     check["median_bp"])
        log.info("Alpaca deepening added %d bars", total)
        return 0

    if args.deepen_dukascopy:
        # Its own mode rather than a step inside the usual pass: it needs no
        # API key, spends no credits, and touches only the pairs the archive
        # carries. Run per pair from the workflow - each is a few hundred
        # requests, and a failure part way through then costs one pair rather
        # than all of them.
        session = requests.Session()
        total = 0
        for asset in instruments:
            path = bars.store_path(args.bars_dir, asset.file_stem)
            try:
                result = deepen_from_dukascopy(asset, path, basket.acquire_since,
                                               session)
            except Exception as exc:
                log.error("%s: Dukascopy deepening failed - %s",
                          asset.asset_id, exc)
                continue
            if result["skipped"]:
                log.info("%s: skipped (%s)", asset.asset_id, result["skipped"])
                continue
            total += result["added"]
            check = result["check"]
            log.info("%s: +%d bars from Dukascopy (%s .. %s, %d fetched); "
                     "overlap check %d hours, corr %.4f, median %.2fbp",
                     asset.asset_id, result["added"], result["from"],
                     result["to"], result["fetched"], check["hours"],
                     check["correlation"], check["median_bp"])
        log.info("Dukascopy deepening added %d bars", total)
        return 0

    if args.deepen_wallstreetcn:
        session = requests.Session()
        total = 0
        for asset in (a for a in instruments if a.ticker in wallstreetcn.LME):
            path = bars.store_path(args.bars_dir, asset.file_stem)
            try:
                out = deepen_from_wallstreetcn(asset, path, session)
            except Exception as exc:
                log.error("%s: Wallstreetcn deepening failed - %s", asset.asset_id, exc)
                continue
            check = out.get("check")
            if out["skipped"]:
                log.info("%s: skipped (%s)%s", asset.asset_id, out["skipped"],
                         f"; overlap {check['hours']} hours, corr {check['correlation']:.4f},"
                         f" median {check['median_bp']:.2f}bp" if check else "")
                continue
            total += out["added"]
            log.info("%s: +%d bars from Wallstreetcn (%s .. %s), %d opening(s) left "
                     "out %s; overlap check %d hours, corr %.4f, median %.2fbp",
                     asset.asset_id, out["added"], out["from"], out["to"],
                     len(out["left_out"]), out["left_out"], check["hours"],
                     check["correlation"], check["median_bp"])
        log.info("Wallstreetcn deepening added %d bars", total)
        return 0

    if args.repair:
        if not wanted:
            log.error("--repair needs --instruments: it writes into the store")
            return 2
        table = _sessions.load_sessions()
        session = requests.Session()
        failed = 0
        for asset in instruments:
            path = bars.store_path(args.bars_dir, asset.file_stem)
            try:
                out = repair_from_yahoo(asset, path, table, session)
            except Exception as exc:
                failed += 1
                log.error("%s: repair failed - %s", asset.asset_id, exc)
                continue
            check = out.get("check") or {}
            if out["skipped"]:
                failed += 1
                log.error("%s: not repaired - %s", asset.asset_id, out["skipped"])
            else:
                log.info("%s: %d hour(s) missing in the last %d days, +%d from Yahoo, "
                         "%d still missing%s", asset.asset_id, out["hours"], REPAIR_DAYS,
                         out["added"], out["still_missing_hours"],
                         f" (overlap {check['hours']} hours, corr "
                         f"{check['correlation']:.4f}, median {check['median_bp']:.2f}bp)"
                         if check else "")
        return 1 if failed else 0

    # A MISSING OR REFUSED KEY costs that provider's instruments, not the run:
    # they are not asked, the others are, and the health chat names the key.
    # provider -> [why, refused at (None: not set), how many not asked]
    keyless: dict[str, list] = {}
    api_key = os.environ.get("TWELVEDATA_API_KEY", "")
    tiingo_key = os.environ.get("TIINGO_API_KEY", "")
    sifting_key = os.environ.get("SIFTING_API_KEY", "")
    alpaca_keys = (os.environ.get("ALPACA_KEY_ID", "").strip()
                   and os.environ.get("ALPACA_SECRET_KEY", "").strip())
    for provider, key in (("twelvedata", api_key), ("tiingo", tiingo_key),
                          ("sifting", sifting_key), ("alpaca", alpaca_keys)):
        served = sum(a.fetched_from == provider for a in instruments)
        if served and not key:
            keyless[provider] = [f"{PROVIDER_KEYS[provider][1]} is not set", None, served]
            log.error("%s is not set: %d %s instrument(s) not asked for; the other "
                      "providers continue.", PROVIDER_KEYS[provider][1], served,
                      PROVIDER_KEYS[provider][0])
    alpaca_gone = False

    usage = Usage()
    session = usage.session()
    # Loaded once. A missing table is not an error here - it only means no
    # instrument can be skipped, which is the safe direction.
    try:
        session_table = _sessions.load_sessions()
    except FileNotFoundError:
        log.warning("No session table; every instrument will be asked for")
        session_table = None

    failures = 0
    fetched = 0                 # asked and answered, new bars or not
    closed = 0                  # not asked: the calendar says nothing new
    waiting = 0                 # new, waiting their turn to be seeded
    limited = 0                 # not asked: their provider's limit is spent
    seeded = 0
    quota_gone = False
    share_spent = False         # a history walk's share of the day, not the day
    tiingo_gone = False
    tiingo_skipped = 0
    tiingo_trip = None
    tiingo_remaining = None
    yahoo_gone = False
    sifting_gone = False
    sifting_skipped = 0
    sifting_trip = None
    sifting_remaining = None
    # A provider that times out or errors on every attempt costs ~96 s an
    # instrument (186 s for Alpaca), and the job has 20 minutes: asked one by
    # one, a silent provider's funds took it past them, and a cancelled job
    # delivers nothing and tells no one. Stopped after UNANSWERED_IN_A_ROW.
    unanswered: dict[str, int] = {}       # provider -> instruments in a row
    silent: dict[str, list] = {}          # provider -> [stopped at, skipped since]
    dark: list[tuple[str, str, str]] = []

    # TWELVE DATA FIRST, AND BESIDE THE REST. Its funds are fetched in batches
    # in a thread started before anything else is asked, so its eight credits a
    # minute are waited out while the other providers answer. Only the
    # ordinary pass over stores that hold bars; the skip rule is the same.
    background: "set[str]" = set()
    td_thread = None
    # The votes due a count again this run (jump.verify.recount_hours): each
    # instrument's own provider is asked back to their hours in its ordinary
    # request, so a bar it has corrected since comes in.
    votes = {} if args.extend_history else verify.votes()
    voted_at = int(time.time())

    def voted(asset: Asset) -> "list[int]":
        return verify.recount_hours(asset, votes, voted_at)

    if not args.extend_history and api_key:
        live = [a for a in instruments if a.fetched_from == "twelvedata"
                and not bars.load(bars.store_path(args.bars_dir, a.file_stem)).empty]
        due = [a for a in live if not nothing_can_have_appeared(
            a, bars.store_path(args.bars_dir, a.file_stem), session_table)]
        background = {a.asset_id for a in live}
        closed += len(live) - len(due)
        if due:
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(max_workers=1)
            td_thread = pool.submit(fetch_twelvedata_live, due, args.bars_dir, api_key,
                                    session=usage.session(),
                                    recount={a.asset_id: voted(a) for a in due})
            pool.shutdown(wait=False)

    for i, asset in enumerate(instruments):
        if asset.asset_id in background:
            continue
        path = bars.store_path(args.bars_dir, asset.file_stem)
        if not args.extend_history and bars.load(path).empty:
            # Only a few never-seen instruments per run. Adding a block of new
            # tickers to the configuration otherwise turns the next hourly run
            # into a batch job: the free tier is 800 credits a day and eight a
            # minute, so thirty-eight empty stores would spend the budget and
            # overrun the job. They are seeded a handful at a time and are all
            # producing bars within a few hours.
            if seeded >= SEED_PER_RUN:
                waiting += 1
                log.info("%s: new instrument, waiting its turn to be seeded",
                         asset.asset_id)
                continue
            seeded += 1
        elif not args.extend_history and nothing_can_have_appeared(
                asset, path, session_table):
            closed += 1
            log.info("%s: market closed since the newest stored bar, not asked for",
                     asset.asset_id)
            continue
        if tiingo_gone and asset.fetched_from == "tiingo":
            limited += 1
            tiingo_skipped += 1
            continue
        if yahoo_gone and asset.fetched_from == "yahoo":
            limited += 1
            continue
        if sifting_gone and asset.fetched_from == "sifting":
            limited += 1
            sifting_skipped += 1
            continue
        if alpaca_gone and asset.fetched_from == "alpaca":
            limited += 1
            dark.append((asset.asset_id, "alpaca", "skipped: Alpaca's rate limit is spent"))
            continue
        if asset.fetched_from in silent:
            silent[asset.fetched_from][1] += 1
            continue
        if asset.fetched_from in keyless:
            if keyless[asset.fetched_from][1] is not None:
                keyless[asset.fetched_from][2] += 1
            continue
        try:
            r = backfill_instrument(asset, basket, args.bars_dir, api_key, session,
                                    args.extend_history, tiingo_key, sifting_key,
                                    recount=voted(asset))
            log.info("%s: %d bars (%s .. %s), %d new", r["asset_id"], r["rows"],
                     _fmt(r["first"]), _fmt(r["last"]), r["from_api"])
            unanswered.pop(asset.fetched_from, None)
            fetched += 1
        except twelvedata.DailyQuotaExhausted as exc:
            # STOP THE WHOLE LOOP, and this is the difference between a run that
            # delivers on slightly stale bars and a run that delivers nothing.
            # The budget is per key and per day, so every instrument after this
            # one fails too; asking them anyway cost 32 seconds each and pushed
            # the job past its timeout, which skipped delivery entirely.
            quota_gone = True
            share_spent = isinstance(exc, twelvedata.ArchiveShareSpent)
            log.error("Twelve Data credits are spent - %s", exc)
            log.error("Stopping the fetch here. %d instrument(s) not asked for; "
                      "the pipeline continues on the bars already stored.",
                      len(instruments) - i - 1)
            break
        except tiingo.RateLimited as exc:
            # Tiingo's 50-an-hour bucket, or its 1000-a-day one. Neither clears
            # inside a run, so every later Tiingo instrument would spend a
            # round trip to be told the same thing. The others carry on: this
            # is one provider being out, not the run failing. Provider is not
            # switched automatically - a silent fall back to slower or
            # different-priced data cannot happen unnoticed.
            tiingo_gone = True
            tiingo_trip = asset.asset_id
            tiingo_remaining = getattr(exc, "remaining", None)
            log.error("Tiingo's request budget is spent - %s", exc)
            log.error("Skipping the remaining Tiingo instruments; the other "
                      "providers continue.")
        except sifting.RateLimited as exc:
            # The monthly quota or the burst limit. Same reasoning as Tiingo:
            # it will not clear inside the run, so stop asking and keep the rest.
            sifting_gone = True
            sifting_trip = asset.asset_id
            sifting_remaining = getattr(exc, "remaining", None)
            log.error("SiftingIO's request budget is spent (X-Quota-Remaining: %s) - %s",
                      getattr(exc, "remaining", None), exc)
            log.error("Skipping the remaining SiftingIO instruments; the other "
                      "providers continue.")
        except alpaca.RateLimited as exc:
            # Same reasoning: Alpaca's limit does not clear inside the run.
            alpaca_gone = True
            dark.append((asset.asset_id, "alpaca", str(exc)))
            log.error("Alpaca's rate limit is spent - %s", exc)
            log.error("Skipping the remaining Alpaca instruments; the other "
                      "providers continue.")
        except yahoo.RateLimited as exc:
            yahoo_gone = True
            log.error("%s: Yahoo's rate limit is spent - %s", asset.asset_id, exc)
            log.error("Skipping the remaining Yahoo instruments; the other "
                      "providers continue.")
        except KeyRefused as exc:
            keyless[asset.fetched_from] = [str(exc), asset.asset_id, 0]
            log.error("%s: %s; skipping the rest of %s this run, the other providers "
                      "continue.", asset.asset_id, exc, asset.fetched_from)
        except Unreachable as exc:
            failures += 1
            dark.append((asset.asset_id, asset.fetched_from, str(exc)))
            log.error("%s: failed - %s", asset.asset_id, exc)
            provider = asset.fetched_from
            unanswered[provider] = unanswered.get(provider, 0) + 1
            if unanswered[provider] >= UNANSWERED_IN_A_ROW:
                silent[provider] = [asset.asset_id, 0]
                log.error("%s did not answer %d instruments in a row; skipping the "
                          "rest of it this run, the other providers continue.",
                          provider, unanswered[provider])
        except Exception as exc:
            failures += 1
            dark.append((asset.asset_id, asset.fetched_from, str(exc)))
            log.error("%s: failed - %s", asset.asset_id, exc)
            unanswered.pop(asset.fetched_from, None)        # it answered
        # The 8-requests-per-minute limit is per key, and the running hourly
        # monitor spends it too - the pause is needed between instruments as well.
        # Only Twelve Data is paced: no other provider here has a per-minute
        # limit this pass can reach, and pausing after them would spend the wait
        # twice over.
        if asset.fetched_from == "twelvedata" and i < len(instruments) - 1:
            time.sleep(TWELVEDATA_DELAY_SECONDS)

    if td_thread is not None:
        try:
            td_results = td_thread.result()
        except Exception as exc:                     # the thread itself broke
            td_results = [(a, exc) for a in instruments if a.asset_id in background]
        for asset, r in td_results:
            if isinstance(r, twelvedata.DailyQuotaExhausted):
                quota_gone = True
                log.error("%s: Twelve Data daily credits are gone - %s", asset.asset_id, r)
            elif isinstance(r, KeyRefused):
                if "twelvedata" in keyless:
                    keyless["twelvedata"][2] += 1
                else:
                    keyless["twelvedata"] = [str(r), asset.asset_id, 0]
                    log.error("%s: %s", asset.asset_id, r)
            elif isinstance(r, Exception):
                failures += 1
                dark.append((asset.asset_id, "twelvedata", str(r)))
                log.error("%s: failed - %s", asset.asset_id, r)
            else:
                fetched += 1
                log.info("%s: %d bars (%s .. %s), from network %d (batched)",
                         r["asset_id"], r["rows"], _fmt(r["first"]), _fmt(r["last"]),
                         r["from_api"])

    # STALE: nothing new for longer than its calendar allows - a provider
    # serving an old series, a contract it no longer carries, or Yahoo refusing
    # run after run (a Yahoo rate limit is said only here). Not the instruments
    # already named above, as failed or skipped.
    stale: list[tuple[str, str, int]] = []
    if session_table and not args.instruments and not args.extend_history:
        named = {asset_id for asset_id, _, _ in dark}
        stopped = set(silent) | set(keyless) | {p for p, gone in (
            ("tiingo", tiingo_gone), ("sifting", sifting_gone),
            ("alpaca", alpaca_gone), ("twelvedata", quota_gone)) if gone}
        for asset in instruments:
            if asset.asset_id in named or asset.fetched_from in stopped:
                continue
            try:
                hours, ticking = stale_hours(
                    asset, bars.store_path(args.bars_dir, asset.file_stem), session_table)
            except Exception as exc:
                log.warning("%s: staleness not checked - %s", asset.asset_id, exc)
                continue
            if stale_to_name(asset, hours, ticking):
                stale.append((asset.asset_id, asset.fetched_from, hours))
                log.error("%s: no new bar for %d session hours", asset.asset_id, hours)

    if closed:
        log.info("%d instrument(s) not asked: the calendar says nothing new can exist",
                 closed)
    if waiting:
        log.info("%d new instrument(s) waiting their turn to be seeded", waiting)
    if limited:
        log.info("%d instrument(s) not asked: their provider's rate limit is spent",
                 limited)
    if any(skipped for _, skipped in silent.values()):
        log.info("%d instrument(s) not asked: their provider did not answer",
                 sum(skipped for _, skipped in silent.values()))
    # Not counted as a failure: a spent budget is a known limit being reached,
    # not a breakage, and failing the run would turn a daily certainty into a
    # daily red cross. It is logged loudly instead - and the run still delivers.
    if quota_gone and share_spent:
        log.warning("This walk has spent its share of the day's Twelve Data credits "
                    "(%d); the rest is left to the hourly run. Run it again tomorrow "
                    "to go on.", twelvedata.ARCHIVE_CREDIT_CAP)
    elif quota_gone:
        log.warning("The Twelve Data budget is spent for the UTC day. "
                    "Bars will resume at midnight.")
    if tiingo_gone:
        log.warning("The Tiingo request budget is spent. The hourly bucket "
                    "refills within the hour; the daily one at midnight UTC.")
    if yahoo_gone:
        log.warning("Yahoo's rate limit is spent. Remaining Yahoo instruments "
                    "were skipped; the other providers continue.")

    # Only on the ordinary hourly pass over the whole basket, and never once
    # Yahoo has said stop or stopped answering: a rate limit is not a reason to
    # spend it again, nor a silence to wait it out again.
    if session_table and not args.instruments and not yahoo_gone \
            and "yahoo" not in silent:
        try:
            r = check_dividends(corporate_actions._funds(basket), session_table,
                                session)
            if r["skipped"] is None:
                log.info("dividend check: %d fund(s) confirmed, %d payout(s) added%s",
                         r["checked"], r["added"],
                         f", failed: {' '.join(r['failed'])}" if r["failed"] else "")
            if r.get("stale"):
                send_ops_alert(
                    "⚠️ <b>Dividend check behind</b>\n"
                    f"No confirmed payout record for {DIVIDEND_CHECK_STALE_DAYS}+ days: "
                    f"{', '.join(r['stale'])}. Their overnight gaps are unscored "
                    "until it catches up.")
        except Exception as exc:
            # Warned, not failed: the cost is gaps left unscored, which is what
            # the check exists to guarantee rather than a breakage.
            log.warning("dividend check failed - %s", exc)

    # THE SOURCES' VOTE ON THE FAR MOVES, on the ordinary pass over the whole
    # basket, before anything is scored: a move most did not see is not scored
    # and its message says so (jump.verify). A provider that has said stop or
    # stopped answering is not asked again: in the vote it is an outage.
    vote: list[str] = []
    if session_table and not args.instruments and not args.extend_history:
        try:
            r = verify.verify(basket.instruments, args.bars_dir, session_table, session,
                              blocked=({"yahoo"} if yahoo_gone else set()) | set(silent))
            for name in (r or {}).get("stopped", []):
                vote.append(f"{name} stopped for the run (a rate limit, or no answer twice "
                            "in a row): an outage in the vote, counted against the moves "
                            "it is asked about until a later count")
        except Exception as exc:
            # Not a failed run: an unvoted move is scored, as every move never
            # voted is. Named, though.
            log.warning("the vote failed - %s", exc)
            vote.append(f"the vote failed ({exc}): this hour's far moves are scored "
                        "unvoted")

    if not args.skip_vix:
        try:
            r = backfill_vix(basket, args.vix_dir,
                             os.environ.get("FRED_API_KEY", ""), session)
            log.info("%s: %d daily values (%s .. %s) from %s",
                     r["asset_id"], r["rows"], _fmt(r["first"]), _fmt(r["last"]),
                     " + ".join(r["sources"]))
            # Warned rather than failed: one source is enough to deliver, and a
            # run that goes red over a mirror being down would cry wolf. The
            # message names which one so a silent degradation is still visible.
            for note in r["trouble"]:
                log.warning("VIX source unavailable - %s", note)
        except Exception as exc:
            failures += 1
            dark.append(("VIX", "cboe/fred", str(exc)))
            log.error("VIX: failed - %s", exc)

    log.info(usage.line())
    text = format_provider_failure(
        dark, tiingo_gone=tiingo_gone, tiingo_skipped=tiingo_skipped,
        tiingo_remaining=tiingo_remaining, tiingo_trip=tiingo_trip,
        yahoo_gone=yahoo_gone, sifting_gone=sifting_gone,
        sifting_skipped=sifting_skipped, sifting_remaining=sifting_remaining,
        sifting_trip=sifting_trip, silent=silent, stale=stale,
        vote=vote, keys=keyless)
    if text:
        send_ops_alert(text)

    # Nonzero, so the hourly job goes red, only when nothing could be fetched
    # at all: one provider or instrument failing is that provider's, named on
    # the health chat above, and the run delivered for everyone else (as the
    # detector treats one instrument's error, jump.jumps.run).
    return 1 if failures and not fetched else 0


if __name__ == "__main__":
    sys.exit(main())

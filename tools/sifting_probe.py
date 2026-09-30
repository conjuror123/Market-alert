"""What can SiftingIO serve this project, on the key we have?

The widening needs a LIVE source for about 96 more US-listed funds and 9 FX
pairs (docs/concerns-for-later.md, item 6). The four questions, cheapest
disqualifier first:

  FRESHNESS  how old is the newest bar it serves? The run fires at :05 and
             wants the bar that closed at :00. Its pricing page promises
             real-time only on paid tiers, so the free tier is measured.
  COVERAGE   which candidates exist, and how deep - the floor is 2020-02-10.
  AGREEMENT  median and p90 disagreement in bps against the stored bars, on
             the thin funds that already cost us a provider switch; the line
             is median <= 2 bps and p90 <= 5 (tools/alpaca_compare.py).
  BUDGET     the free tier is 10,000 calls a month; the probe spends ~160.

Bars are asked at 30 minutes and folded onto the store's round-hour grid, the
same way the live fetch folds every provider (tremor.bars.to_hourly).

Read-only. Fetches, measures, prints; writes nothing. Runs in Actions, where
the key is a repository secret.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from price_monitor.models import Candle
from tremor import bars
from tremor.basket import load_basket

BASE = "https://api.sifting.io/v1"
TIMEOUT = 30
PAUSE = 1.05                   # 60 a minute on the free tier
BARS_DIR = "data/tremor/bars"
SAFE_MEDIAN_BPS, SAFE_P90_BPS = 2.0, 5.0
FLOOR = "2020-02-10"

FUNDS = """KRE XRT ITB IYT XME XBI XPH IHI VDC VHT VPU SMH SOXX IGV FDN CIBR SKYY
DIA RSP MDY EWJ EWG EWU EWQ EWC EWA EWL EWN EZU FXI EWZ EWW INDA EWY EWT EZA EPI
TUR VNQ IYR RWR SCHH REM VNQI RWX GOVT SCHO VGIT VGLT SPTL BWX VTIP SCHP STIP
VMBS SPMB LMBS JMBS VCIT VCSH IGIB SPIB USIG QLTA GIGB SLQD SHYG USHY ANGL SRLN
FALN EMLC VWOB PCY EBND LEMB EMHY CEMB DBO DBE UNL IAU SGOL SIVR GLTR LIT REMX
SLX CANE""".split()
NEW_PAIRS = ["USDSEK", "USDNOK", "USDMXN", "USDZAR", "USDBRL", "USDTRY", "USDINR",
             "USDKRW", "USDPLN"]

_calls = 0


def _get(path: str, **params):
    global _calls
    _calls += 1
    headers = {"X-API-Key": os.environ["SIFTING_API_KEY"], "Accept-Encoding": "gzip"}
    try:
        resp = requests.get(f"{BASE}{path}", params=params, headers=headers,
                            timeout=TIMEOUT)
    except requests.RequestException as exc:
        return None, f"request failed: {type(exc).__name__}", {}
    limits = {k: v for k, v in resp.headers.items()
              if "ratelimit" in k.lower() or "quota" in k.lower()}
    time.sleep(PAUSE)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:160]}", limits
    try:
        return resp.json(), "ok", limits
    except ValueError:
        return None, f"not JSON: {resp.text[:160]}", limits


def _rows(payload) -> list:
    return (payload or {}).get("data") or []


def _age(ms, now: datetime) -> str:
    minutes = (now.timestamp() - ms / 1000) / 60
    return f"{minutes:.0f} min" if minutes < 180 else f"{minutes / 60:.1f} h"


def _hourly(rows) -> pd.DataFrame:
    candles = [Candle(open_time=int(r["t"] // 1000), open=float(r["o"]),
                      high=float(r["h"]), low=float(r["l"]), close=float(r["c"]),
                      volume=float(r.get("v") or 0.0),
                      close_time=int(r["t"] // 1000) + 1800) for r in rows]
    return bars.to_hourly(bars.candles_to_frame(candles)) if candles else bars.empty_frame()


def _compare(fresh: pd.DataFrame, stored: pd.DataFrame):
    if fresh.empty or stored.empty:
        return None
    joined = fresh.merge(stored, on="hour_utc", suffixes=("_sf", "_st"))
    joined = joined[joined["hour_utc"] < int(stored["hour_utc"].max())]
    if joined.empty:
        return None
    bps = (joined["close_sf"] - joined["close_st"]).abs() / joined["close_st"] * 1e4
    return len(joined), bps.median(), bps.quantile(0.9), bps.max()


def freshness(now: datetime) -> None:
    print("1. FRESHNESS - the newest bar each serves, against the clock")
    for label, path in [("SPY (US stocks)", "/hist/stocks/SPY/bars"),
                        ("EURUSD (forex)", "/hist/forex/EURUSD/bars"),
                        ("USDMXN (forex)", "/hist/forex/USDMXN/bars")]:
        for interval, back in (("1m", timedelta(minutes=40)), ("1h", timedelta(hours=5))):
            start = (now - back).strftime("%Y-%m-%dT%H:%M:%SZ")
            data, status, limits = _get(path, start=start, interval=interval, limit=2000)
            rows = _rows(data)
            if rows:
                newest = max(r["t"] for r in rows)
                opened = datetime.fromtimestamp(newest / 1000, tz=timezone.utc)
                detail = (f"{len(rows)} bars, newest opened {opened:%H:%M} UTC "
                          f"({_age(newest, now)} ago)")
            else:
                detail = "no bars in the window"
            as_of = (data or {}).get("meta", {}).get("as_of", "")
            print(f"   {label:16s} {interval:3s} {status:6.40s} {detail}  as_of={as_of}")
    print(f"   limits on the last call: {limits}")


def fx_coverage(now: datetime) -> None:
    print("\n2. FX - the new pairs, and the eight held against the stored bars")
    start = (now - timedelta(days=21)).strftime("%Y-%m-%d")
    basket = load_basket()
    held = {a.ticker.replace("/", ""): a for a in basket.instruments
            if a.session_template == "fx_continuous"}
    for pair in NEW_PAIRS + sorted(held):
        data, status, _ = _get(f"/hist/forex/{pair}/bars", start=start,
                               interval="1h", limit=2000)
        rows = _rows(data)
        line = f"   {pair}: {status:6.40s} {len(rows)} hourly bars"
        if rows:
            line += f", newest {_age(rows[-1]['t'], now)} ago"
        if pair in held and rows:
            fresh = _hourly([dict(r, t=r["t"]) for r in rows])
            stored = bars.load(bars.store_path(BARS_DIR, held[pair].file_stem))
            got = _compare(fresh, stored)
            if got:
                n, med, p90, worst = got
                ok = "PASS" if med <= SAFE_MEDIAN_BPS and p90 <= SAFE_P90_BPS else "FAIL"
                line += f" | vs stored: {n}h median {med:.2f} p90 {p90:.2f} max {worst:.1f} bps {ok}"
        print(line)


def agreement(now: datetime) -> None:
    print("\n3. AGREEMENT - held funds, 30-minute bars folded to hours, against the store")
    basket = load_basket()
    funds = [a for a in basket.instruments if a.session_template == "us_equity"]
    # The thin ones are the question; three liquid ones as a control.
    chosen = [a for a in funds if a.provider == "yahoo"] + \
             [a for a in funds if a.ticker in ("SPY", "GLD", "TLT")]
    start = (now - timedelta(days=28)).strftime("%Y-%m-%d")
    passed = 0
    for a in chosen:
        data, status, _ = _get(f"/hist/stocks/{a.ticker}/bars", start=start,
                               interval="30m", limit=2000)
        rows = _rows(data)
        stored = bars.load(bars.store_path(BARS_DIR, a.file_stem))
        got = _compare(_hourly(rows), stored) if rows else None
        if not got:
            print(f"   {a.ticker:5s} ({a.provider}) {status:6.40s} no overlap ({len(rows)} bars)")
            continue
        n, med, p90, worst = got
        ok = med <= SAFE_MEDIAN_BPS and p90 <= SAFE_P90_BPS
        passed += ok
        print(f"   {a.ticker:5s} ({a.provider:6s}) {n:4d}h  median {med:5.2f}  p90 {p90:5.2f}"
              f"  max {worst:6.1f} bps  {'PASS' if ok else 'FAIL'}")
    print(f"   {passed} of {len(chosen)} within median {SAFE_MEDIAN_BPS} / p90 {SAFE_P90_BPS} bps")


def coverage() -> None:
    print(f"\n4. COVERAGE AND DEPTH - the candidate funds, oldest daily bar (floor {FLOOR})")
    missing, shallow, deep = [], [], 0
    for ticker in FUNDS:
        data, status, _ = _get(f"/hist/stocks/{ticker}/bars", start="2000-01-01",
                               interval="1d", order="asc", limit=1)
        rows = _rows(data)
        if not rows:
            missing.append(f"{ticker}({status[:12]})")
            continue
        first = datetime.fromtimestamp(rows[0]["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if first > FLOOR:
            shallow.append(f"{ticker}({first})")
        else:
            deep += 1
    print(f"   reach {FLOOR}: {deep} of {len(FUNDS)}")
    print(f"   shallower: {' '.join(shallow) or 'none'}")
    print(f"   not served: {' '.join(missing) or 'none'}")


def client_check(now: datetime) -> None:
    """The real client (price_monitor.sifting), as the hourly fetch calls it."""
    from price_monitor import sifting

    key = os.environ["SIFTING_API_KEY"]
    basket = load_basket()
    print("CLIENT - held pairs through price_monitor.sifting, against the store")
    for a in basket.instruments:
        if a.session_template != "fx_continuous":
            continue
        candles = sifting.fetch_full_history(a.ticker, a.fetch_interval, days=21, api_key=key)
        fresh = bars.to_hourly(bars.candles_to_frame(candles))
        got = _compare(fresh, bars.load(bars.store_path(BARS_DIR, a.file_stem)))
        newest = datetime.fromtimestamp(int(fresh["hour_utc"].max()), tz=timezone.utc)
        verdict = "no overlap"
        if got:
            n, med, p90, worst = got
            ok = med <= SAFE_MEDIAN_BPS and p90 <= SAFE_P90_BPS
            verdict = (f"{n}h median {med:.2f} p90 {p90:.2f} max {worst:.1f} bps "
                       f"{'PASS' if ok else 'FAIL'}")
        print(f"   {a.ticker:8s} provider={a.fetched_from:8s} {len(fresh)} hours, newest "
              f"{newest:%a %H:%M} UTC | {verdict}")

    print("\nHOURS - when the new pairs actually trade (last 4 weeks, weekdays)")
    for pair in NEW_PAIRS:
        candles = sifting.fetch_full_history(pair[:3] + "/" + pair[3:], "1h", days=28,
                                             api_key=key)
        frame = bars.candles_to_frame(candles)
        if frame.empty:
            print(f"   {pair}: no bars")
            continue
        moments = pd.to_datetime(frame["hour_utc"], unit="s", utc=True)
        weekdays = moments[moments.dt.weekday < 5]
        days = weekdays.dt.date.nunique()
        per_hour = weekdays.dt.hour.value_counts().reindex(range(24), fill_value=0)
        thin = [h for h, n in per_hour.items() if n < 0.5 * days]
        steps = frame["hour_utc"].sort_values().diff().dropna() / 3600
        midweek = steps[(steps > 1) & (steps < 40)]
        print(f"   {pair}: {len(frame)} bars, {len(weekdays) / max(days, 1):.1f} a weekday; "
              f"hours (UTC) mostly missing: {thin or 'none'}; "
              f"midweek gaps over an hour: {len(midweek)} (longest {midweek.max() if len(midweek) else 0:.0f}h)")


# Each pair's home market, whose holidays may empty its hours: the exchange
# calendar (exchange_calendars) and the timezone its days are counted in.
HOME = {"USDKRW": ("XKRX", "Asia/Seoul"), "USDINR": ("XBOM", "Asia/Kolkata"),
        "USDBRL": ("BVMF", "America/Sao_Paulo"), "USDMXN": ("XMEX", "America/Mexico_City"),
        "USDZAR": ("XJSE", "Africa/Johannesburg"), "USDTRY": ("XIST", "Europe/Istanbul"),
        "USDPLN": ("XWAR", "Europe/Warsaw"), "USDSEK": ("XSTO", "Europe/Stockholm"),
        "USDNOK": ("XOSL", "Europe/Oslo"), "USDCNH": ("XSHG", "Asia/Shanghai"),
        "EURUSD": (None, "UTC")}


def holes(now: datetime) -> None:
    """Two years of hourly bars per pair: when it trades, how deep it goes, and
    whether the hours it lacks midweek are its home market's holidays."""
    import exchange_calendars as xcals
    from zoneinfo import ZoneInfo

    from price_monitor import sifting

    key = os.environ["SIFTING_API_KEY"]
    begin = now - timedelta(days=730)
    print("HOLES - two years of hourly bars, holes inside the trading week\n")
    for pair, (code, tz) in HOME.items():
        data, _, _ = _get(f"/hist/forex/{pair}/bars", start="2000-01-01", interval="1d",
                          limit=1)
        oldest = _rows(data)
        oldest = (datetime.fromtimestamp(oldest[0]["t"] / 1000, tz=timezone.utc)
                  .strftime("%Y-%m-%d") if oldest else "?")
        candles = sifting.fetch_full_history(pair[:3] + "/" + pair[3:], "1h", days=730,
                                             api_key=key, end=now)
        h = np.array(sorted({c.open_time for c in candles}), dtype="int64")
        if len(h) < 2:
            print(f"{pair}: {len(h)} bars")
            continue
        moments = pd.to_datetime(h, unit="s", utc=True)
        # When it trades: share of mid-week days (Tue-Thu, clear of the weekend
        # edges) with a bar at each UTC hour.
        mid = moments[moments.weekday.isin([1, 2, 3])]
        days = mid.normalize().nunique()
        share = (pd.Series(mid.hour).value_counts().reindex(range(24), fill_value=0)
                 / max(days, 1))
        profile = " ".join(f"{int(round(100 * s)):3d}" for s in share)
        print(f"{pair}: {len(h)} bars since {moments[0]:%Y-%m-%d}, history from {oldest}")
        print(f"   UTC hour  {' '.join(f'{i:3d}' for i in range(24))}")
        print(f"   % of days {profile}")

        closed: set = set()
        if code:
            cal = xcals.get_calendar(code, start=f"{begin:%Y-%m-%d}", end=f"{now:%Y-%m-%d}")
            weekdays = pd.bdate_range(f"{begin:%Y-%m-%d}", f"{now:%Y-%m-%d}")
            open_days = set(cal.sessions_in_range(weekdays[0], weekdays[-1]).date)
            closed = {d.date() for d in weekdays if d.date() not in open_days}
        step = np.diff(h) // 3600
        buckets = {"1": 0, "2-4": 0, "5-10": 0, "11-22": 0, "23+": 0}
        on_holiday, long_ones = 0, []
        home = ZoneInfo(tz)
        for i in np.flatnonzero(step > 1):
            gap_hours = range(int(h[i]) + 3600, int(h[i + 1]), 3600)
            stamps = pd.to_datetime(list(gap_hours), unit="s", utc=True)
            if (stamps.weekday == 5).any():      # spans a Saturday: the weekend
                continue
            k = int(step[i]) - 1                  # hours missing
            buckets["1" if k == 1 else "2-4" if k < 5 else "5-10" if k < 11
                    else "11-22" if k < 23 else "23+"] += 1
            local_days = {s.tz_convert(home).date() for s in stamps}
            holiday = bool(local_days & closed)
            on_holiday += holiday
            if k >= 5:
                long_ones.append(f"{stamps[0]:%y%m%d %H}h+{k}{'*' if holiday else ''}")
        total = sum(buckets.values())
        print(f"   midweek holes: {total}, by hours missing {buckets}; touching a home "
              f"holiday: {on_holiday}; home holidays in the span: {len(closed)}")
        print(f"   5 hours or more (first missing hour UTC, +hours, * = home holiday): "
              f"{' '.join(long_ones[-60:]) or 'none'}\n")


def archive(now: datetime) -> None:
    """SiftingIO's history of USD/KRW, USD/INR and USD/BRL starts in July 2026.
    Can Twelve Data supply the years before, at prices that agree with it?"""
    from price_monitor import sifting, twelvedata

    key = os.environ["SIFTING_API_KEY"]
    td_key = os.environ.get("TWELVEDATA_API_KEY", "")
    print("ARCHIVE - Twelve Data for the pairs SiftingIO holds only two months of\n")
    for pair in [p for p in ("USD/KRW", "USD/INR", "USD/BRL")
                 if p.replace("/", "") in (os.environ.get("SIFTING_PAIRS") or "USDKRW,USDINR,USDBRL")]:
        r = requests.get("https://api.twelvedata.com/earliest_timestamp",
                         params={"symbol": pair, "interval": "1h", "apikey": td_key},
                         timeout=TIMEOUT)
        earliest = r.json() if r.status_code == 200 else r.text[:120]
        time.sleep(8)
        try:
            td = bars.candles_to_frame(twelvedata.fetch_full_history(
                pair, "1h", days=60, base_url="https://api.twelvedata.com", api_key=td_key,
                request_delay_seconds=0, chunk_days=90, end=now))
        except Exception as exc:
            print(f"{pair}: Twelve Data refused - {str(exc)[:120]}  earliest={earliest}\n")
            time.sleep(8)
            continue
        time.sleep(8)
        sf = bars.candles_to_frame(sifting.fetch_full_history(pair, "1h", days=60,
                                                               api_key=key, end=now))
        line = (f"{pair}: earliest hourly bar {earliest}; {len(td)} Twelve Data hours in "
                f"60 days, {len(sf)} SiftingIO")
        # All hours, then only the ones a USD/BRL session would keep: 11:00 to
        # 22:00 UTC, when SiftingIO has a bar on 97% of days.
        in_session = lambda f: f[(f["hour_utc"] // 3600 % 24).between(11, 21)]  # noqa: E731
        for label, a, b in (("all hours", td, sf), ("11-22 UTC", in_session(td), in_session(sf))):
            got = _compare(a, b)
            if got:
                n, med, p90, worst = got
                ok = med <= SAFE_MEDIAN_BPS and p90 <= SAFE_P90_BPS
                line += (f"\n   against SiftingIO, {label}: {n}h median {med:.2f} p90 {p90:.2f} "
                         f"max {worst:.1f} bps {'PASS' if ok else 'FAIL'}")
        j = td.merge(sf, on="hour_utc", suffixes=("_a", "_b"))
        j["bps"] = (j["close_a"] - j["close_b"]).abs() / j["close_b"] * 1e4
        cells = " ".join(f"{h:02d}:{g['bps'].median():.1f}/{g['bps'].quantile(0.9):.1f}"
                         for h, g in j.groupby(j["hour_utc"] // 3600 % 24))
        print(line + f"\n   by UTC hour, median/p90 bps: {cells}\n")


def main() -> int:
    if not os.environ.get("SIFTING_API_KEY"):
        print("SIFTING_API_KEY is not set")
        return 1
    now = datetime.now(timezone.utc)
    print(f"Probing SiftingIO at {now:%Y-%m-%d %H:%M} UTC\n")
    if os.environ.get("SIFTING_ONLY") == "client":
        client_check(now)
        return 0
    if os.environ.get("SIFTING_ONLY") == "archive":
        archive(now)
        return 0
    if os.environ.get("SIFTING_ONLY") == "holes":
        holes(now)
        print(f"\n{_calls} direct calls, plus the client's pages")
        return 0
    freshness(now)
    if os.environ.get("SIFTING_ONLY") != "freshness":
        fx_coverage(now)
        agreement(now)
        coverage()
    print(f"\n{_calls} calls spent of the free tier's 10,000 a month")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The fund verdict table: which live source can carry each fund, measured.

The widening (docs/decisions.md, "The data") needs a live hourly source for
96 more US-listed funds. A source qualifies for a fund when its hourly closes
agree with the consolidated tape - median <= 2 bps and p90 <= 5, the line
tools/alpaca_compare.py holds every feed to - and when it does not skip hours
the tape has, because a skipped hour folds two hours of move into one reading.

WHY THE PULLS ARE SHAPED THIS WAY. Eight sources were on the table; each is
asked only what it alone can answer, at the lowest cost it can answer it:

  Alpaca SIP   the reference - the consolidated tape, free for history ending
               15 minutes back - and the depth check. It answers many symbols
               per request, so every fund, held and candidate, costs a handful
               of calls in total.
  Alpaca IEX   the same exchange Tiingo's intraday feed is, so it stands in for
               Tiingo too: on the 44 held funds both failed exactly the fourteen
               thin commodity funds. Batched like SIP. Tiingo itself is not
               called - its 50 an hour are shared with production.
  SiftingIO    measured directly, one call a fund, ~100 of the monthly 10,000.
  Yahoo        measured directly, one call a fund, no key and no quota.
  Twelve Data  consolidated, like Yahoo; eight credits check that on the names
               Yahoo does worst on.
  HF Data, FRED, Finnhub   not asked: history before 2020, daily macro series,
               and on the free tier quotes without bars. None of them can be
               the live hourly source for a fund.

Held funds ride along as the control: their split between Tiingo and Yahoo was
decided on other measurements, and the table should reproduce it.

Read-only. Fetches, measures, prints; writes nothing. Runs in Actions, where
the keys are repository secrets. With FUND_VERDICT_ONLY=freshness it instead
asks how fresh Alpaca's free IEX bars are - the one question that needs the
market open.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from price_monitor import twelvedata, yahoo
from price_monitor.models import Candle
from tremor import bars
from tremor.basket import load_basket

ALPACA = "https://data.alpaca.markets/v2"
SIFTING = "https://api.sifting.io/v1"
TWELVEDATA = "https://api.twelvedata.com"
TIMEOUT = 30
NY = ZoneInfo("America/New_York")
DAYS = 28
FLOOR = pd.Timestamp("2020-02-10", tz="UTC")
SAFE_MEDIAN_BPS, SAFE_P90_BPS = 2.0, 5.0
# Hours the tape has and the feed does not. Each one merges two hours of move
# into one reading, so a feed missing more than one hour in fifty is refused
# however well it agrees on the hours it has.
SAFE_MISSING = 0.02

# The new members of the target block map (docs/concerns-for-later.md, "The
# blocks are still the nine broad ones"), and the three broad benchmarks
# watched beside it.
CANDIDATES = {
    "US cyclicals": "KRE XRT ITB IYT XME",
    "US defensives": "XBI XPH IHI VDC VHT VPU",
    "US tech": "SMH SOXX IGV FDN CIBR SKYY",
    "benchmarks": "DIA RSP MDY",
    "developed ex-US": "EWJ EWG EWU EWQ EWC EWA EWL EWN EZU",
    "emerging": "FXI EWZ EWW INDA EWY EWT EZA EPI TUR",
    "real estate": "VNQ IYR RWR SCHH REM VNQI RWX",
    "government bonds": "GOVT SCHO VGIT VGLT SPTL BWX",
    "inflation, securitized": "VTIP SCHP STIP VMBS SPMB LMBS JMBS",
    "IG credit": "VCIT VCSH IGIB SPIB USIG QLTA GIGB SLQD",
    "HY credit": "SHYG USHY ANGL SRLN FALN",
    "EM credit": "EMLC VWOB PCY EBND LEMB EMHY CEMB",
    "energy": "DBO DBE UNL",
    "precious metals": "IAU SGOL SIVR GLTR",
    "industrial metals": "JJC JJN JJU LIT REMX SLX",
    "agriculture": "CANE JO NIB BAL COW",
}
FEEDS = ("iex", "sifting", "yahoo")

_alpaca_headers: dict[str, str] = {}
_sifting_quota = "?"


def _regular(frame: pd.DataFrame) -> pd.DataFrame:
    """Half-hour bars inside the regular session, folded to the store's hours.
    Pre- and post-market bars would otherwise fold into the 09:00 and 16:00
    hours and compare a different price."""
    if frame.empty:
        return bars.empty_frame()
    local = pd.to_datetime(frame["hour_utc"], unit="s", utc=True).dt.tz_convert(NY)
    minutes = local.dt.hour * 60 + local.dt.minute
    keep = (local.dt.weekday < 5) & (minutes >= 570) & (minutes < 960)
    return bars.to_hourly(frame[keep.to_numpy()])


def _candles(rows, key_t, scale, span) -> list[Candle]:
    out = []
    for r in rows or []:
        t = r[key_t]
        if isinstance(t, str):
            stamp = pd.Timestamp(t)
            t = int((stamp if stamp.tzinfo else stamp.tz_localize("UTC")).timestamp())
        else:
            t = int(t) // scale
        out.append(Candle(open_time=t, open=float(r["o"]), high=float(r["h"]),
                          low=float(r["l"]), close=float(r["c"]),
                          volume=float(r.get("v") or 0.0), close_time=t + span))
    return out


def alpaca_bars(symbols: list[str], timeframe: str, start: str, end: str,
                feed: str) -> dict[str, list[Candle]]:
    """Many symbols per request, paged. Raw prices, as the store keeps them."""
    out: dict[str, list[Candle]] = {s: [] for s in symbols}
    span = {"30Min": 1800, "1Week": 7 * 86400, "1Min": 60}[timeframe]
    for i in range(0, len(symbols), 50):
        token = None
        while True:
            params = {"symbols": ",".join(symbols[i:i + 50]), "timeframe": timeframe,
                      "start": start, "end": end, "adjustment": "raw", "feed": feed,
                      "limit": 10000}
            if token:
                params["page_token"] = token
            r = requests.get(f"{ALPACA}/stocks/bars", params=params,
                             headers=_alpaca_headers, timeout=TIMEOUT)
            r.raise_for_status()
            payload = r.json()
            for sym, rows in (payload.get("bars") or {}).items():
                out.setdefault(sym, []).extend(_candles(rows, "t", 1, span))
            token = payload.get("next_page_token")
            if not token:
                break
    return out


def sifting_bars(symbol: str, start: datetime, end: datetime) -> "list[Candle] | str":
    global _sifting_quota
    headers = {"X-API-Key": os.environ["SIFTING_API_KEY"], "Accept-Encoding": "gzip"}
    params = {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "interval": "30m", "limit": 2000}
    try:
        r = requests.get(f"{SIFTING}/hist/stocks/{symbol}/bars", params=params,
                         headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return f"request failed: {type(exc).__name__}"
    finally:
        time.sleep(1.05)
    _sifting_quota = r.headers.get("X-Quota-Remaining", _sifting_quota)
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    return _candles((r.json() or {}).get("data"), "t", 1000, 1800)


def measure(feed: pd.DataFrame, tape: pd.DataFrame) -> dict:
    """Agreement on the hours both have, and the tape's hours the feed lacks.
    The newest tape hour is left out: it may still have been forming."""
    if tape.empty:
        return {}
    tape = tape[tape["hour_utc"] < int(tape["hour_utc"].max())]
    if feed.empty:
        return {"n": 0, "missing": 1.0}
    joined = feed.merge(tape, on="hour_utc", suffixes=("_f", "_t"))
    if joined.empty:
        return {"n": 0, "missing": 1.0}
    bps = ((joined["close_f"] - joined["close_t"]).abs() / joined["close_t"] * 1e4)
    return {"n": len(joined), "med": float(bps.median()),
            "p90": float(bps.quantile(0.9)), "max": float(bps.max()),
            "missing": 1 - len(joined) / len(tape)}


def passes(m: dict) -> bool:
    return bool(m) and m.get("n", 0) > 0 and m["med"] <= SAFE_MEDIAN_BPS \
        and m["p90"] <= SAFE_P90_BPS and m["missing"] <= SAFE_MISSING


def _cell(m: dict) -> str:
    if not m:
        return f"{'-':>18}"
    if not m.get("n"):
        return f"{'none':>18}"
    miss = f" {m['missing']:.0%}" if m["missing"] > 0.005 else ""
    mark = "" if passes(m) else "x"
    return f"{m['med']:5.1f}/{m['p90']:5.1f}{miss:>4}{mark:>2}".rjust(18)


def verdict(now: datetime) -> int:
    basket = load_basket()
    held = {a.ticker: a for a in basket.instruments if a.session_template == "us_equity"}
    candidates = {t: block for block, names in CANDIDATES.items() for t in names.split()}
    symbols = list(held) + list(candidates)
    end = now - timedelta(minutes=20)
    start = end - timedelta(days=DAYS)
    stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    print(f"Fund verdict at {now:%Y-%m-%d %H:%M} UTC: {len(held)} held (control) + "
          f"{len(candidates)} candidates, {DAYS} days of regular-session hours.\n")

    tape = {s: _regular(bars.candles_to_frame(c)) for s, c in
            alpaca_bars(symbols, "30Min", stamp(start), stamp(end), "sip").items()}
    iex = {s: _regular(bars.candles_to_frame(c)) for s, c in
           alpaca_bars(symbols, "30Min", stamp(start), stamp(end), "iex").items()}
    weekly = alpaca_bars(symbols, "1Week", "2016-01-01T00:00:00Z",
                         stamp(now - timedelta(days=1)), "sip")
    first = {s: (pd.Timestamp(min(c.open_time for c in weekly[s]), unit="s", tz="UTC")
                 if weekly.get(s) else None) for s in symbols}
    print("Alpaca: SIP and IEX answered.")

    yh, sf = {}, {}
    for s in symbols:
        try:
            yh[s] = _regular(bars.candles_to_frame(
                yahoo.fetch_full_history(s, "30min", days=DAYS, end=end)))
        except Exception as exc:  # a verdict of its own: this feed has nothing
            yh[s] = bars.empty_frame()
            print(f"   yahoo {s}: {str(exc)[:80]}")
        time.sleep(0.3)
    print("Yahoo answered.")
    sifting_for = list(candidates) + ["SPY", "GLD", "UGA", "SOYB"]
    for s in sifting_for:
        got = sifting_bars(s, start, end)
        sf[s] = (_regular(bars.candles_to_frame(got)) if isinstance(got, list)
                 else bars.empty_frame())
    print(f"SiftingIO answered; monthly quota left: {_sifting_quota}\n")

    rows = []
    for s in symbols:
        t = tape.get(s, bars.empty_frame())
        m = {"iex": measure(iex.get(s, bars.empty_frame()), t),
             "yahoo": measure(yh[s], t),
             "sifting": measure(sf[s], t) if s in sf else {}}
        grid = len(t)
        rows.append((s, held[s].block if s in held else candidates[s],
                     held[s].fetched_from if s in held else "", first[s], grid, m))

    head = (f"{'fund':<6}{'block':<24}{'held on':<9}{'from':>11}{'hours':>6}"
            + "".join(f"{f + ' med/p90 miss':>18}" for f in FEEDS) + "  live source")
    for title, keep in (("HELD - the control", lambda r: r[2]),
                        ("CANDIDATES", lambda r: not r[2])):
        print(title)
        print(head)
        print("-" * len(head))
        for s, block, on, since, grid, m in rows:
            if not keep((s, block, on)):
                continue
            ok = [f for f in FEEDS if passes(m[f])]
            deep = since is not None and since <= FLOOR
            where = ("not on the tape" if not grid else
                     ", ".join(ok) if ok else "none agrees")
            if grid and not deep:
                where += f"  (starts {since:%Y-%m-%d})" if since is not None else ""
            print(f"{s:<6}{block[:23]:<24}{on:<9}"
                  f"{(f'{since:%Y-%m}' if since is not None else '-'):>11}{grid:>6}"
                  + "".join(_cell(m[f]) for f in FEEDS) + f"  {where}")
        print()

    print("SUMMARY, candidates")
    cand = [r for r in rows if not r[2]]
    for f in FEEDS:
        good = [r[0] for r in cand if passes(r[5][f])]
        print(f"   {f:8s} agrees on {len(good):3d}: {' '.join(good)}")
    none = [r[0] for r in cand if r[4] and not any(passes(r[5][f]) for f in FEEDS)]
    gone = [r[0] for r in cand if not r[4]]
    shallow = [f"{r[0]}({r[3]:%Y-%m})" for r in cand
               if r[4] and r[3] is not None and r[3] > FLOOR]
    print(f"   no feed agrees:  {' '.join(none) or 'none'}")
    print(f"   not on the tape: {' '.join(gone) or 'none'}")
    print(f"   start after {FLOOR:%Y-%m-%d}: {' '.join(shallow) or 'none'}")
    print(f"\nLine: median <= {SAFE_MEDIAN_BPS} bps, p90 <= {SAFE_P90_BPS} bps, "
          f"missing <= {SAFE_MISSING:.0%} of the tape's hours. 'x' marks a fail.")

    # Twelve Data, on the candidates Yahoo agrees with worst plus two controls:
    # is it the consolidated tape too? Eight credits of its daily 800.
    worst = sorted((r for r in cand if r[5]["yahoo"].get("n")),
                   key=lambda r: -r[5]["yahoo"]["p90"])[:6]
    print("\nTWELVE DATA on the names Yahoo does worst on, plus SPY and UGA")
    key = os.environ.get("TWELVEDATA_API_KEY", "")
    for s in [r[0] for r in worst] + ["SPY", "UGA"]:
        try:
            got = twelvedata.fetch_full_history(s, "30min", days=DAYS, base_url=TWELVEDATA,
                                                api_key=key, request_delay_seconds=0,
                                                chunk_days=60, end=end)
            m = measure(_regular(bars.candles_to_frame(got)), tape[s])
            print(f"   {s:6s}{_cell(m)}   yahoo{_cell(measure(yh[s], tape[s]))}")
        except Exception as exc:
            print(f"   {s:6s} {str(exc)[:100]}")
        time.sleep(8.5)
    return 0


def freshness(now: datetime) -> int:
    """Alpaca's free IEX feed, asked for the newest bars while the market is
    open. The run fires at :05 and wants the half-hour bar that closed at :00."""
    print(f"Alpaca IEX freshness at {now:%H:%M:%S} UTC")
    start = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    symbols = ["SPY", "XLK", "TLT", "EWZ", "VCSH", "SLQD"]
    for tf in ("30Min", "1Min"):
        got = alpaca_bars(symbols, tf, start, now.strftime("%Y-%m-%dT%H:%M:%SZ"), "iex")
        for s in symbols:
            c = got.get(s) or []
            if not c:
                print(f"   {tf:5s} {s:5s} no bars")
                continue
            newest = max(c, key=lambda x: x.open_time)
            opened = datetime.fromtimestamp(newest.open_time, tz=timezone.utc)
            age = (now - opened).total_seconds() / 60
            print(f"   {tf:5s} {s:5s} {len(c):3d} bars, newest opened {opened:%H:%M} UTC "
                  f"({age:.0f} min ago), close {newest.close}")
    r = requests.get(f"{ALPACA}/stocks/bars/latest", params={"symbols": "SPY,EWZ",
                     "feed": "iex"}, headers=_alpaca_headers, timeout=TIMEOUT)
    print(f"   latest-bar endpoint: HTTP {r.status_code} {r.text[:200]}")
    return 0


def by_hour(now: datetime, symbols: list[str]) -> int:
    """Yahoo against the tape, hour by hour of the New York day: is a fund's
    disagreement spread across the session or sitting in one hour?"""
    end = now - timedelta(minutes=20)
    start = end - timedelta(days=DAYS)
    stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    tape = alpaca_bars(symbols, "30Min", stamp(start), stamp(end), "sip")
    for s in symbols:
        t = _regular(bars.candles_to_frame(tape[s]))
        y = _regular(bars.candles_to_frame(yahoo.fetch_full_history(s, "30min", days=DAYS,
                                                                    end=end)))
        j = y.merge(t, on="hour_utc", suffixes=("_f", "_t"))
        j["bps"] = (j["close_f"] - j["close_t"]).abs() / j["close_t"] * 1e4
        j["ny"] = pd.to_datetime(j["hour_utc"], unit="s", utc=True).dt.tz_convert(NY).dt.hour
        cells = " ".join(f"{h:02d}h {g['bps'].median():.1f}/{g['bps'].quantile(0.9):.1f}"
                         for h, g in j.groupby("ny"))
        missing = pd.to_datetime(sorted(set(t["hour_utc"]) - set(y["hour_utc"])),
                                 unit="s", utc=True).tz_convert(NY)
        print(f"{s:5s} median/p90 bps by New York hour: {cells}")
        print("      tape hours Yahoo lacks: "
              + (" ".join(f"{m:%m-%d %H}h" for m in missing) or "none"))
    return 0


def main() -> int:
    global _alpaca_headers
    key = (os.environ.get("ALPACA_KEY_ID") or "").strip()
    secret = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    if not (key and secret):
        print("ALPACA_KEY_ID and ALPACA_SECRET_KEY are both needed")
        return 1
    _alpaca_headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    now = datetime.now(timezone.utc)
    only = os.environ.get("FUND_VERDICT_ONLY") or ""
    if only == "freshness":
        return freshness(now)
    if only.startswith("hours:"):
        return by_hour(now, [t.strip().upper() for t in only[6:].split(",") if t.strip()])
    if not os.environ.get("SIFTING_API_KEY"):
        print("SIFTING_API_KEY is not set")
        return 1
    return verdict(now)


if __name__ == "__main__":
    sys.exit(main())

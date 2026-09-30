"""What London Strategic Edge's free key serves, measured.

LSE (londonstrategicedge.com) claims hourly candles for US stocks and ETFs
back to 2003, FX back to 2009 and crypto back to 2017, with live streaming on
the same free key. This asks it what tools/fund_verdict.py asked every other
feed, against the same references:

  catalog    which of the held and wanted instruments it holds at all, and
             the span of each (the vault's own first and last tick);
  shape      one hourly pull, raw, for a fund and a pair;
  newest     the newest bar of a few funds and pairs and how old it is;
  depth      a week of hourly bars at points back to 2009, on USD/BRL and on
             SPY, CPER and TUR;
  funds      28 days of half-hour bars for every held fund and candidate,
             folded to the store's hours, against Alpaca's consolidated tape -
             median <= 2 bps, p90 <= 5, at most 2% of the tape's hours missing;
  FX         60 days of hourly bars for the eight held pairs against the
             stored bars, and for the nine new pairs against SiftingIO, the
             live source - the same line.

Read-only; prints, writes nothing. Runs in Actions, where the keys are
repository secrets. About 200 requests.
"""
from __future__ import annotations

import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from price_monitor import sifting
from tools import fund_verdict as fv
from tremor import bars

VAULT = "https://api.londonstrategicedge.com/vault"
# The download host's CDN turns away the default Python User-Agent.
AGENT = "lse-data-sdk (+https://londonstrategicedge.com)"
NEW_PAIRS = ("USD/SEK USD/NOK USD/MXN USD/ZAR USD/BRL USD/TRY USD/INR USD/KRW "
             "USD/PLN").split()
FX_DAYS = 60


def get(path: str, **params) -> "tuple[int, object]":
    try:
        r = requests.get(f"{VAULT}{path}", params={k: v for k, v in params.items()
                                                   if v is not None},
                         headers={"x-api-key": os.environ["LSE_API_KEY"],
                                  "User-Agent": AGENT}, timeout=90)
    except requests.RequestException as exc:
        return 0, f"request failed: {type(exc).__name__}"
    finally:
        time.sleep(0.3)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


def candles(symbol: str, timeframe: str, start: datetime, end: datetime,
            order: str = "asc", limit: int = 5000) -> "tuple[int, object]":
    return get("/candles", symbol=symbol, timeframe=timeframe, order=order, limit=limit,
               start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
               end=end.strftime("%Y-%m-%dT%H:%M:%SZ"))


def frame(rows) -> pd.DataFrame:
    """Candle rows as the store's frame, stamped at the bar's open."""
    if not isinstance(rows, list) or not rows:
        return bars.empty_frame()
    d = pd.DataFrame(rows)
    t = d.get("ts", d.get("timestamp"))
    hour = pd.to_datetime(t, utc=True).astype("int64") // 10**9
    out = pd.DataFrame({"hour_utc": hour.astype("int64"),
                        "open": d["open"].astype(float), "high": d["high"].astype(float),
                        "low": d["low"].astype(float), "close": d["close"].astype(float),
                        "volume": d.get("volume", pd.Series(0.0, index=d.index)).fillna(0.0)
                        .astype(float), "n_src": 1})
    return out.astype(bars.SCHEMA).sort_values("hour_utc").reset_index(drop=True)


def usage() -> None:
    code, body = get("/usage")
    print(f"USAGE: HTTP {code} {str(body)[:400]}\n")


def catalog(held_funds, candidates, held_fx) -> dict:
    code, body = get("/catalog")
    if not isinstance(body, list):
        print(f"CATALOG: HTTP {code} {str(body)[:300]}\n")
        return {}
    print(f"CATALOG: {len(body)} instruments; by dataset: "
          f"{dict(Counter(r.get('dataset') for r in body).most_common(12))}")
    by = {}
    for r in body:
        by.setdefault(r.get("symbol"), []).append(r)
    for title, names in (("held funds", held_funds), ("candidates", candidates),
                         ("held pairs", held_fx), ("new pairs", NEW_PAIRS)):
        found = [s for s in names if s in by]
        print(f"   {title}: {len(found)} of {len(names)} held"
              + (f"; missing: {' '.join(s for s in names if s not in by)}"
                 if len(found) < len(names) else ""))
    for s in ("SPY", "CPER", "TUR", "JJC", "USD/BRL", "USD/KRW", "USD/INR", "EUR/USD"):
        for r in by.get(s, []):
            print(f"      {s:8s} {r.get('dataset'):10s} first {str(r.get('first_tick'))[:19]}  "
                  f"last {str(r.get('last_tick'))[:19]}  ticks {r.get('ticks')}")
    print()
    return by


def shape(now: datetime) -> None:
    print("SHAPE - the newest hourly candles, raw")
    for s in ("SPY", "USD/BRL"):
        code, body = candles(s, "1h", now - timedelta(days=5), now, order="desc", limit=4)
        print(f"   {s}: HTTP {code} {str(body)[:600]}")
    print()


def newest(now: datetime) -> None:
    print(f"NEWEST at {now:%H:%M} UTC")
    for s in ("SPY", "XLK", "EWZ", "EUR/USD", "USD/BRL", "USD/KRW", "BTC/USD"):
        for tf in ("1h", "1m"):
            code, body = candles(s, tf, now - timedelta(days=4), now, order="desc", limit=1)
            f = frame(body)
            if f.empty:
                print(f"   {s:8s} {tf}: HTTP {code} {str(body)[:100]}")
                continue
            t = int(f["hour_utc"].iloc[-1])
            print(f"   {s:8s} {tf}: newest opened "
                  f"{datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d %H:%M} UTC "
                  f"({(now.timestamp() - t) / 60:.0f} min ago)")
    print()


def depth() -> None:
    print("DEPTH - hourly bars in the first week of March of each year")
    for s, years in (("USD/BRL", (2009, 2012, 2015, 2019, 2022, 2025)),
                     ("SPY", (2004, 2008, 2016, 2020)), ("CPER", (2012, 2016, 2020)),
                     ("TUR", (2009, 2016, 2020))):
        cells = []
        for y in years:
            start = datetime(y, 3, 2, tzinfo=timezone.utc)
            code, body = candles(s, "1h", start, start + timedelta(days=7))
            n = len(body) if isinstance(body, list) else 0
            cells.append(f"{y}: {n if code == 200 else f'HTTP {code}'}")
        print(f"   {s:8s} " + "  ".join(cells))
    print()


def funds(now: datetime, held, candidates, by) -> None:
    symbols = [s for s in list(held) + list(candidates) if s in by or not by]
    end = now - timedelta(minutes=20)
    start = end - timedelta(days=fv.DAYS)
    stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    tape = {s: fv._regular(bars.candles_to_frame(c)) for s, c in
            fv.alpaca_bars(symbols, "30Min", stamp(start), stamp(end), "sip").items()}
    print(f"FUNDS - {len(symbols)} funds, {fv.DAYS} days of half-hour bars folded to "
          f"hours, against the consolidated tape")
    print(f"{'fund':<6}{'held on':<9}{'hours':>6}{'LSE med/p90 miss':>20}")
    good_held, good_cand, failed = [], [], []
    for s in symbols:
        code, body = candles(s, "30m", start, end)
        t = tape.get(s, bars.empty_frame())
        m = fv.measure(fv._regular(frame(body)), t) if code == 200 else {}
        cell = fv._cell(m) if m else f"HTTP {code} {str(body)[:40]}"
        print(f"{s:<6}{(held[s].fetched_from if s in held else ''):<9}{len(t):>6}{cell:>20}")
        if fv.passes(m):
            (good_held if s in held else good_cand).append(s)
        elif t is not None and len(t):
            failed.append(s)
    print(f"\n   agrees: {len(good_held)} of {len(held)} held, {len(good_cand)} of "
          f"{len(candidates)} candidates")
    print(f"   candidates that agree: {' '.join(good_cand)}")
    print(f"   on the tape but failing: {' '.join(failed) or 'none'}\n")


def _fx_measure(ours: pd.DataFrame, reference: pd.DataFrame) -> str:
    if reference.empty:
        return "no reference bars"
    if ours.empty:
        return "LSE has no bars"
    j = ours.merge(reference, on="hour_utc", suffixes=("_l", "_r"))
    if j.empty:
        return "no hours in common"
    bps = (j["close_l"] - j["close_r"]).abs() / j["close_r"] * 1e4
    missing = 1 - len(j) / len(reference)
    return (f"{len(j):5d} h  median {bps.median():5.2f}  p90 {bps.quantile(0.9):5.2f} bps  "
            f"reference hours LSE lacks {missing:.0%}")


def fx(now: datetime, basket) -> None:
    start = now - timedelta(days=FX_DAYS)
    end = now - timedelta(hours=2)
    lo, hi = int(start.timestamp()), int(end.timestamp())
    print(f"FX - {FX_DAYS} days of hourly closes")
    for a in basket.instruments:
        if a.session_template != "fx_continuous":
            continue
        stored = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, a.file_stem))
        stored = stored[(stored["hour_utc"] >= lo) & (stored["hour_utc"] < hi)]
        code, body = candles(a.ticker, "1h", start, end)
        print(f"   {a.ticker:8s} vs stored   {_fx_measure(frame(body), stored)}")
    key = os.environ.get("SIFTING_API_KEY", "")
    for pair in NEW_PAIRS:
        try:
            ref = bars.candles_to_frame(sifting.fetch_full_history(pair, "1h", days=FX_DAYS,
                                                                   api_key=key, end=end))
        except Exception as exc:
            print(f"   {pair:8s} SiftingIO failed: {str(exc)[:80]}")
            continue
        code, body = candles(pair, "1h", start, end)
        ours = frame(body)
        print(f"   {pair:8s} vs SiftingIO {_fx_measure(ours, ref)}")
        if pair == "USD/BRL" and not ours.empty:
            local = ref[pd.to_datetime(ref["hour_utc"], unit="s").dt.hour.between(11, 21)]
            print(f"   {'':8s} 11-22 UTC   {_fx_measure(ours, local)}")
    print()


def main() -> int:
    if not (os.environ.get("LSE_API_KEY") or "").strip():
        print("LSE_API_KEY is not set")
        return 1
    key = (os.environ.get("ALPACA_KEY_ID") or "").strip()
    secret = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    fv._alpaca_headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    now = datetime.now(timezone.utc)
    basket = fv.load_basket()
    held = {a.ticker: a for a in basket.instruments if a.session_template == "us_equity"}
    candidates = {t: b for b, names in fv.CANDIDATES.items() for t in names.split()}
    held_fx = [a.ticker for a in basket.instruments if a.session_template == "fx_continuous"]

    usage()
    by = catalog(list(held), list(candidates), held_fx)
    shape(now)
    newest(now)
    depth()
    fx(now, basket)
    if key and secret:
        funds(now, held, candidates, by)
    else:
        print("No Alpaca keys: the fund comparison is skipped")
    usage()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Whether Google Finance's prices - the ones GOOGLEFINANCE() puts in a Google
Sheet - agree with the consolidated tape for TUR and RWX, the two funds no live
feed carries.

GOOGLEFINANCE() gives only the latest price in a sheet: no intraday history,
and Google blocks reading its history out of a sheet by API. So a sheet could
feed the bot only as a snapshot logger, and the question is whether Google's
price is the tape's price. Google's quote page carries the same feed, with the
last session's five-minute bars embedded in it; this reads those and holds them
against Alpaca's consolidated (SIP) and IEX minute bars of the same session:

  volume     Google's day volume and its bars' sum against SIP and IEX - which
             venues Google sees;
  prints     each Google bar's close against the tape's last trade in its five
             minutes, labelled at the bar's start and at its end;
  hours      the session's hourly closes, the reading the detector is fed,
             against the tape's, as tools/fund_verdict.py measures every feed.

SPY and two thin funds the store already carries ride along as controls.
Read-only; prints, writes nothing. One page per fund, a few Alpaca calls.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from tools import fund_verdict as fv
from tremor import bars

FUNDS = {"TUR": "NASDAQ", "RWX": "NYSEARCA", "SPY": "NYSEARCA", "CANE": "NYSEARCA",
         "GLTR": "NYSEARCA"}
ROW = re.compile(r'\[([\d.]+),([\d.]+),([\d.]+),([\d.]+),"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d'
                 r'[-+]\d\d:\d\d)",(\d+)\]')
BLOCK = re.compile(r"AF_initDataCallback\(\{key: 'ds:(\d+)'.*?data:(.*?), sideChannel", re.S)


def google_page(ticker: str, exchange: str) -> "tuple[pd.DataFrame, float | None, str]":
    """The last session's five-minute bars and the day's volume, from the quote
    page's embedded data. A bar's row reads open, close, high, low, stamp,
    volume; only five minutes with a trade have one."""
    try:
        r = requests.get(f"https://www.google.com/finance/quote/{ticker}:{exchange}",
                         headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"},
                         timeout=fv.TIMEOUT)
    except requests.RequestException as exc:
        return pd.DataFrame(), None, f"request failed: {type(exc).__name__}"
    finally:
        time.sleep(2)
    if r.status_code != 200:
        return pd.DataFrame(), None, f"HTTP {r.status_code}"
    blocks = dict(BLOCK.findall(r.text))
    intraday = pd.DataFrame()
    for data in blocks.values():
        rows = ROW.findall(data)
        days = {row[4][:10] for row in rows}
        if len(rows) > 1 and len(days) == 1:
            intraday = pd.DataFrame(
                [(pd.Timestamp(s).tz_convert("UTC"), float(o), float(c), float(h),
                  float(lo), float(v)) for o, c, h, lo, s, v in rows],
                columns=["t", "open", "close", "high", "low", "volume"])
            break
    day_volume = None
    try:
        day_volume = float(json.loads(blocks["3"])[0][0][14])
    except (KeyError, ValueError, IndexError, TypeError):
        pass
    return intraday, day_volume, "ok"


def minutes(ticker: str, day: pd.Timestamp, feed: str) -> pd.DataFrame:
    start = day.tz_convert(fv.NY).replace(hour=9, minute=30)
    end = start + timedelta(hours=6, minutes=31)
    stamp = lambda d: d.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    candles = fv.alpaca_bars([ticker], "1Min", stamp(start), stamp(end), feed).get(ticker, [])
    return pd.DataFrame([(pd.Timestamp(c.open_time, unit="s", tz="UTC"), c.close, c.volume)
                         for c in candles], columns=["t", "close", "volume"])


def last_trade(tape: pd.DataFrame, lo: pd.Timestamp, hi: pd.Timestamp) -> "float | None":
    inside = tape[(tape["t"] >= lo) & (tape["t"] < hi)]
    return float(inside["close"].iloc[-1]) if len(inside) else None


def prints(g: pd.DataFrame, tape: pd.DataFrame) -> str:
    """Each Google bar against the tape's last trade in the five minutes it
    starts and in the five it ends, in basis points."""
    out = []
    for label, shift in (("start", timedelta(0)), ("end", timedelta(minutes=-5))):
        diffs = []
        for _, row in g.iterrows():
            lo = row["t"] + shift
            # The 16:00 bar is the close: the tape's 15:59 minute holds it.
            if row["t"].tz_convert(fv.NY).strftime("%H:%M") == "16:00":
                lo = row["t"] - timedelta(minutes=5)
            px = last_trade(tape, lo, lo + timedelta(minutes=5))
            if px:
                diffs.append(abs(row["close"] - px) / px * 1e4)
        exact = sum(d < 0.5 for d in diffs)
        med = f"{pd.Series(diffs).median():.1f}" if diffs else "-"
        out.append(f"{label}: {exact}/{len(g)} exact, median {med}bp")
    busy = tape.assign(w=tape["t"].dt.floor("5min"))["w"].nunique()
    return "  ".join(out) + f"   tape has trades in {busy} five-minute windows"


def hours(g: pd.DataFrame, tape_30: pd.DataFrame) -> str:
    """Hourly closes as the store would hold them, Google's bars stamped at
    their start, against the SIP half-hour bars folded the same way."""
    frame = pd.DataFrame({"hour_utc": g["t"].astype("int64") // 10**9, "open": g["open"],
                          "high": g["high"], "low": g["low"], "close": g["close"],
                          "volume": g["volume"], "n_src": 1}).astype(bars.SCHEMA)
    return fv._cell(fv.measure(fv._regular(frame), fv._regular(tape_30)))


def main() -> int:
    key = (os.environ.get("ALPACA_KEY_ID") or "").strip()
    secret = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    if not key or not secret:
        print("ALPACA_KEY_ID / ALPACA_SECRET_KEY are not set")
        return 1
    fv._alpaca_headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    print(f"Google Finance against the tape, {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n")
    for ticker, exchange in FUNDS.items():
        g, day_volume, status = google_page(ticker, exchange)
        if g.empty:
            print(f"{ticker:5s} no intraday bars on the page ({status})\n")
            continue
        day = g["t"].iloc[0]
        sip, iex = minutes(ticker, day, "sip"), minutes(ticker, day, "iex")
        start = day.tz_convert(fv.NY).replace(hour=9, minute=30)
        stamp = lambda d: d.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        sip_30 = bars.candles_to_frame(fv.alpaca_bars(
            [ticker], "30Min", stamp(start), stamp(start + timedelta(hours=7)), "sip")
            .get(ticker, []))
        print(f"{ticker:5s} session {day.tz_convert(fv.NY):%Y-%m-%d}, {len(g)} Google bars")
        print(f"      volume: Google day {day_volume or '-'}, Google bars {g['volume'].sum():.0f}, "
              f"SIP {sip['volume'].sum():.0f}, IEX {iex['volume'].sum():.0f}")
        print(f"      prints vs SIP  {prints(g, sip)}")
        print(f"      prints vs IEX  {prints(g, iex)}")
        print(f"      hourly closes vs SIP (median/p90 bp, missing): {hours(g, sip_30)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

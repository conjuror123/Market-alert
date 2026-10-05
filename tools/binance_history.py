"""One-off: every coin's hourly history - its Binance USDT pair, and below
Binance's first, thin months a dollar exchange's record, where one reaches back.

BINANCE, from each pair's listing. Two pairs are joined to the pair they were
before (price_monitor/binance.py): POL/USDT continues MATICUSDT, which stopped
2024-09-10 02:00 three days before POLUSDT opened (MATIC became POL one for
one), and BCH/USDT continues BCHABCUSDT, Binance's name for Bitcoin Cash from
the 2018-11 fork to 2019-11-28. A join is refused if the price across it moved
more than JOIN_MAX - a renaming moves nothing by itself.

BELOW A SEAM, A DOLLAR EXCHANGE. Binance opened in 2017-08 and its first
months were thin: its hourly returns correlated 0.84 with Coinbase's over
2017-08 to 10, 0.90 the quarter after, 0.98 by mid-2018 and 0.99 from 2019
(BTC; ETH and LTC alike). So for the five coins a dollar exchange reaches
further back on, the record is that exchange's below a seam and Binance's from
it - 2018-06-01, after Binance matured and before USDT slipped off the dollar
in late 2018 (seams from 2018-12 fail the level gate). BCH's is 2019-01-15:
Binance lists it only from 2018-11. The dollar prices are scaled by the median
ratio of Binance's to theirs over the SEAM_CALIBRATION days after the seam, so
there is no step at it, and the splice is refused unless the next two months
pass the gates every splice passes (jump.backfill.verify_alignment).
Measured 2026-10-02, return correlation and median level gap after the ratio:

    BTC  Bitstamp   from 2011    0.989   8.7 bp
    ETH  Bitfinex   from 2016-03 0.992   6.0 bp
    LTC  Bitfinex   from 2013-05 0.989   7.1 bp
    XRP  Bitstamp   from 2017-03 0.974  13.4 bp
    BCH  Coinbase   from 2017-12 0.987  24.3 bp

BELOW A LATE USDT LISTING, THE BTC PAIR. LINK and ADA traded on Binance
against BTC months before their USDT pairs opened (LINKBTC 2017-09, LINKUSDT
2019-01; ADABTC 2017-11, ADAUSDT 2018-04). Below the seam their record is the
BTC pair times BTCUSDT, hour by hour: open by open, close by close; high and
low as the product of the two highs and of the two lows, an outer bound the
detector does not read; volume the BTC pair's, in the coin. Seams where the
USDT pair's volume caught up with the BTC pair's: LINK 2019-05-01, ADA
2018-06-01. Measured 2026-10-02, against the USDT pair after its listing:

    LINK  correlation 0.95-0.99 a month, median gap 12 bp (2019 Q1-Q2)
    ADA   correlation 0.99 a month,      median gap 6 bp

Each dollar record starts at its first month traded in at least
MIN_MONTH_COVERAGE of its hours: a market trading in fits is stale prices and
catch-ups, which read as moves.

Rewrites data/jump/bars/binance_* for the coins named, or every coin with
--all; run bare it refuses. From the repository root:
    python -m tools.binance_history LINK/USDT ADA/USDT
    python -m tools.binance_history --all
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from price_monitor import binance, bitfinex, bitstamp
from jump import bars
from jump.backfill import verify_alignment
from jump.basket import load_basket

START = datetime(2017, 1, 1, tzinfo=timezone.utc)
BEFORE = {"POL/USDT": "MATICUSDT", "BCH/USDT": "BCHABCUSDT"}
JOIN_MAX = 0.15

SEAM = datetime(2018, 6, 1, tzinfo=timezone.utc)
DOLLAR = {
    "BTC/USDT": ("bitstamp", "btcusd", datetime(2011, 8, 1, tzinfo=timezone.utc), SEAM),
    "ETH/USDT": ("bitfinex", "tETHUSD", datetime(2016, 1, 1, tzinfo=timezone.utc), SEAM),
    "LTC/USDT": ("bitfinex", "tLTCUSD", datetime(2013, 1, 1, tzinfo=timezone.utc), SEAM),
    "XRP/USDT": ("bitstamp", "xrpusd", datetime(2017, 1, 1, tzinfo=timezone.utc), SEAM),
    "BCH/USDT": ("coinbase", "coinbase_BCH-USD", None, datetime(2019, 1, 15, tzinfo=timezone.utc)),
    "LINK/USDT": ("binance-btc", "LINKBTC", datetime(2017, 9, 1, tzinfo=timezone.utc),
                  datetime(2019, 5, 1, tzinfo=timezone.utc)),
    "ADA/USDT": ("binance-btc", "ADABTC", datetime(2017, 11, 1, tzinfo=timezone.utc), SEAM),
}
SEAM_CALIBRATION = 30         # days after the seam the ratio is measured on
SEAM_CHECK = 93               # ... and through which the gates are applied
MIN_MONTH_COVERAGE = 0.9


def dollar_record(kind: str, symbol: str, start, end, session) -> pd.DataFrame:
    if kind == "binance-btc":
        return btc_cross(symbol, start, end, session)
    if kind == "bitstamp":
        candles = bitstamp.fetch_history(symbol, start, end, session)
    elif kind == "bitfinex":
        candles = bitfinex.fetch_history(symbol, start, end, session)
    else:
        frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, symbol))
        return frame[frame["hour_utc"] < int(end.timestamp())].reset_index(drop=True)
    return bars.to_hourly(bars.candles_to_frame(candles))


def btc_cross(symbol: str, start, end, session) -> pd.DataFrame:
    """A coin's BTC pair times BTCUSDT, for the hours the pair traded."""
    pair = bars.candles_to_frame(binance.fetch_history(symbol, start, end, session))
    btc = bars.candles_to_frame(binance.fetch_history("BTCUSDT", start, end, session))
    pair = pair[pair["volume"] > 0]
    j = pair.merge(btc, on="hour_utc", suffixes=("", "_btc"))
    out = pd.DataFrame({"hour_utc": j["hour_utc"], "volume": j["volume"], "n_src": 1})
    for column in ("open", "high", "low", "close"):
        out[column] = j[column] * j[f"{column}_btc"]
    return out[list(bars.SCHEMA)].astype(bars.SCHEMA).reset_index(drop=True)


def from_first_full_month(frame: pd.DataFrame) -> pd.DataFrame:
    when = pd.to_datetime(frame["hour_utc"], unit="s", utc=True)
    month = when.dt.tz_localize(None).dt.to_period("M")
    share = month.value_counts() / (month.value_counts().index.days_in_month * 24)
    full = sorted(m for m, v in share.items() if v >= MIN_MONTH_COVERAGE)
    if not full:
        return frame.iloc[0:0]
    return frame[month >= full[0]].reset_index(drop=True)


def splice(ticker: str, top: pd.DataFrame, session) -> pd.DataFrame:
    kind, symbol, start, seam = DOLLAR[ticker]
    s = int(seam.timestamp())
    end = seam + timedelta(days=SEAM_CHECK)
    usd = from_first_full_month(dollar_record(kind, symbol, start, end, session))
    joined = usd.merge(top[["hour_utc", "close"]], on="hour_utc", suffixes=("", "_top"))
    calib = joined[(joined["hour_utc"] >= s)
                   & (joined["hour_utc"] < s + SEAM_CALIBRATION * 86400)]
    ratio = float(np.median(calib["close_top"] / calib["close"]))
    scaled = usd.copy()
    scaled[["open", "high", "low", "close"]] *= ratio
    check = verify_alignment(
        scaled[scaled["hour_utc"] >= s + SEAM_CALIBRATION * 86400], top)
    first = pd.to_datetime(scaled["hour_utc"].min(), unit="s", utc=True)
    print(f"{ticker}: {kind} {symbol} from {first:%Y-%m-%d} below {seam:%Y-%m-%d}, x{ratio:.5f}; "
          f"after it {check['hours']} h, correlation {check['correlation']:.3f}, "
          f"median gap {check['median_bp']:.1f} bp")
    if not check["ok"]:
        raise SystemExit(f"   refused: {check['why']}")
    return pd.concat([scaled[scaled["hour_utc"] < s], top[top["hour_utc"] >= s]],
                     ignore_index=True)


def main(argv: "list[str] | None" = None) -> int:
    every = [a for a in load_basket().instruments if a.source == "binance"]
    parser = argparse.ArgumentParser(
        description="Rebuild coins' hourly history (rewrites their stores).")
    parser.add_argument("tickers", nargs="*", metavar="TICKER",
                        help="coins to rebuild, e.g. LINK/USDT")
    parser.add_argument("--all", action="store_true", help="rebuild every coin")
    args = parser.parse_args(argv)
    if args.all == bool(args.tickers):
        parser.error("name the coins to rebuild, or pass --all (not both)")
    unknown = sorted(set(args.tickers) - {a.ticker for a in every})
    if unknown:
        parser.error(f"not a coin in the basket: {', '.join(unknown)}")
    coins = [a for a in every if args.all or a.ticker in args.tickers]
    session = requests.Session()
    for asset in coins:
        candles = binance.fetch_history(binance.symbol_for(asset.ticker), START,
                                        session=session)
        if asset.ticker in BEFORE:
            older = binance.fetch_history(BEFORE[asset.ticker], START, session=session)
            older = [c for c in older if c.open_time < candles[0].open_time]
            step = math.log(candles[0].open / older[-1].close)
            print(f"{asset.ticker}: {BEFORE[asset.ticker]} to "
                  f"{datetime.fromtimestamp(older[-1].open_time, timezone.utc):%Y-%m-%d %H:%M}, "
                  f"then {binance.symbol_for(asset.ticker)}; {100 * step:+.1f}% across the join")
            if abs(step) > JOIN_MAX:
                print(f"   refused: more than {JOIN_MAX:.0%}")
                return 1
            candles = older + candles
        frame = bars.to_hourly(bars.candles_to_frame(candles))
        if asset.ticker in DOLLAR:
            frame = splice(asset.ticker, frame, session)
        path = bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem)
        # The whole record, written over the store: shards that did not change
        # are not touched, and months already in git keep their files.
        bars.write(path, frame)
        stored = bars.load(path)
        first = datetime.fromtimestamp(int(stored["hour_utc"].min()), timezone.utc)
        print(f"{asset.ticker}: {len(stored)} hours from {first:%Y-%m-%d}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

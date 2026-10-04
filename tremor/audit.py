"""The data-coverage report, data/tremor/coverage.md.

For every instrument: how deep its history is, whether it carries hourly
volume, and whether the series is sound (OHLC consistency, prices, duplicates,
the longest gap). Run `python -m tremor.audit` after changing the basket or
deepening history.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import pandas as pd

from tremor import bars
from tremor.basket import Asset, load_basket

HOUR = 3600


def _day(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")


def measure_precision(closes: pd.Series, sample: int = 5000) -> float | None:
    """Measures the SOURCE'S QUOTE PRECISION - the smallest decimal place at which
    it emits values at all.

    This is not the exchange price step. For currency pairs the two coincide:
    Twelve Data gives five decimals and the step really is 1e-5. For ETFs they do
    not - the source returns 769.53992 where the exchange shows 769.54, so a
    measured precision of 1e-7 describes the vendor's storage format, not the
    trading step, which is one cent.

    So tick_size is declared in the configuration, and this measures only a
    check on it: a declared step FINER than the measured precision promises a
    resolution the data does not have.
    """
    if closes.empty:
        return None
    decimals = 0
    for value in closes.tail(sample):
        text = f"{float(value):.10g}"
        if "." in text and "e" not in text:
            decimals = max(decimals, len(text.split(".")[1]))
    return 10.0 ** -decimals


def audit_instrument(asset: Asset, frame: pd.DataFrame) -> dict:
    row = {
        "asset_id": asset.asset_id, "ticker": asset.ticker, "block": asset.block,
        "tier": asset.tier, "source": asset.source, "provider": asset.fetched_from,
        "interval": asset.fetch_interval,
        "in_basket": asset.in_basket, "has_volume_declared": asset.has_volume,
        "tick_size": asset.tick_size, "rows": len(frame),
    }
    if frame.empty:
        return row | {"first": None, "last": None, "days": 0, "bars_per_day": 0.0,
                      "precision": None,
                      "zero_volume_pct": None, "has_volume_actual": False,
                      "max_gap_hours": None, "partial_hours": 0, "ohlc_violations": 0,
                      "nonpositive_prices": 0, "negative_volume": 0, "duplicate_hours": 0}

    hours = frame["hour_utc"].astype("int64")
    days = pd.to_datetime(hours, unit="s", utc=True).dt.date
    bars_per_day = float(days.value_counts().median())

    # The largest gap between adjacent bars. Weekends and holidays produce
    # legitimate gaps, so the number is not a defect in itself - it is there to
    # tell an ordinary weekend from a real hole in the history.
    diffs = hours.diff().dropna()
    max_gap = int(diffs.max() // HOUR) if len(diffs) else 0

    volume = frame["volume"].astype("float64")
    zero_pct = float((volume == 0).mean() * 100)

    # OHLC consistency, with a half-tick tolerance: sources round a bar's
    # fields independently (TLT close 92.42 against high 92.415), and a
    # sub-tick difference is rounding, not a broken bar - the same tolerance
    # as tremor.quality.
    tol = asset.tick_size / 2
    ohlc_bad = int((
        (frame["low"] > frame[["open", "close"]].min(axis=1) + tol)
        | (frame[["open", "close"]].max(axis=1) > frame["high"] + tol)
    ).sum())

    return row | {
        "first": _day(hours.min()),
        "last": _day(hours.max()),
        "days": int(days.nunique()),
        "bars_per_day": bars_per_day,
        "precision": measure_precision(frame["close"].astype("float64")),
        "zero_volume_pct": zero_pct,
        "has_volume_actual": bool(zero_pct < 99.0),
        "max_gap_hours": max_gap,
        # Hours assembled from an incomplete set of source bars: for ETFs that
        # is the first half hour of a session, the "first bar of the session".
        "partial_hours": int((frame["n_src"] < frame["n_src"].median()).sum()),
        "ohlc_violations": ohlc_bad,
        "nonpositive_prices": int((frame[["open", "high", "low", "close"]] <= 0).any(axis=1).sum()),
        "negative_volume": int((volume < 0).sum()),
        "duplicate_hours": int(len(frame) - hours.nunique()),
    }


def audit_vix(basket, vix_dir: str) -> dict | None:
    path = os.path.join(vix_dir, f"{basket.volatility_index.file_stem}.parquet")
    if not os.path.exists(path):
        return None
    frame = pd.read_parquet(path)
    return {
        "series_id": basket.volatility_index.series_id, "rows": len(frame),
        "first": _day(frame["day"].min()), "last": _day(frame["day"].max()),
        "median_lag_hours": float(
            ((frame["available_at"] - frame["day"]) / HOUR).median()),
    }


def _flag(row: dict) -> str:
    """What in this row needs attention. Empty means the instrument is sound."""
    notes = []
    if row["rows"] == 0:
        return "no data"
    if row["has_volume_declared"] and not row["has_volume_actual"]:
        notes.append("volume declared but empty")
    if not row["has_volume_declared"] and row["has_volume_actual"]:
        notes.append("volume present though not declared")
    for field, label in (("ohlc_violations", "OHLC"), ("nonpositive_prices", "prices<=0"),
                         ("negative_volume", "volume<0"), ("duplicate_hours", "duplicates")):
        if row[field]:
            notes.append(f"{label}: {row[field]}")
    # A declared price step finer than anything the source emits.
    if row["precision"] and row["tick_size"] < row["precision"]:
        notes.append(f"step {row['tick_size']:g} finer than source precision {row['precision']:g}")
    return ", ".join(notes)


def render(rows: list[dict], vix: dict | None) -> str:
    out = ["# Tremor data coverage", "",
           f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by "
           "`python -m tremor.audit`. One row per instrument: its provider, "
           "how far back its bars reach, its hourly volume, and anything wrong "
           "with the series (Notes; `—` is sound). Periods end where the "
           "bars on disk end: without the open months (`tools/hot_bars.sh "
           "restore`), at the last settled month.", ""]

    for in_basket, title in ((True, "Basket"), (False, "Tracked outside the basket")):
        subset = [r for r in rows if r["in_basket"] == in_basket]
        if not subset:
            continue
        out += [f"## {title}", "",
                "| Instrument | Block | Provider | Interval | Bars | Period | Days | "
                "Bars per day | Price step | Source precision | "
                "Volume=0 | Max gap, h | Notes |",
                "|---|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---|"]
        for r in sorted(subset, key=lambda x: (x["block"], x["ticker"])):
            head = (f"| `{r['ticker']}` | {r['block']} | {r['provider']} | {r['interval']} | "
                    f"{r['rows']:,} |")
            if not r["rows"]:
                # An instrument without a single bar is a result in itself, not
                # a reason to crash the report on formatting empty numbers.
                out.append(head + " — | — | — | "
                           f"{r['tick_size']:g} | — | — | — | {_flag(r)} |")
                continue
            out.append(
                head +
                f" {r['first']} .. {r['last']} | {r['days']:,} | "
                f"{r['bars_per_day']:.0f} | "
                f"{r['tick_size']:g} | {r['precision']:g} | "
                f"{r['zero_volume_pct']:.0f}% | {r['max_gap_hours']} | {_flag(r) or '—'} |"
            )
        out.append("")

    if vix:
        out += ["## External stress indicator", "",
                f"`{vix['series_id']}`: {vix['rows']:,} daily values, "
                f"{vix['first']} .. {vix['last']}. Median publication lag — "
                f"{vix['median_lag_hours']:.0f} h from midnight of the observation day "
                "(set in config/basket.yaml).", ""]

    total = sum(r["rows"] for r in rows)
    out += ["## Totals", "",
            f"Instruments: {len(rows)}. Bars: {total:,}. "
            f"Rows with issues: {sum(1 for r in rows if _flag(r))}.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tremor data coverage report")
    parser.add_argument("--bars-dir", default=bars.DEFAULT_BARS_DIR)
    parser.add_argument("--vix-dir", default=bars.DEFAULT_VIX_DIR)
    parser.add_argument("--out", default=os.path.join("data", "tremor", "coverage.md"))
    args = parser.parse_args(argv)

    basket = load_basket()
    rows = [
        audit_instrument(a, bars.load(bars.store_path(args.bars_dir, a.file_stem)))
        for a in basket.instruments
    ]
    report = render(rows, audit_vix(basket, args.vix_dir))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

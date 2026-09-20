"""How long should the long-run sigma window be, per instrument?

sigma_LT is the denominator of every claim this system makes: "8.3x its usual
hour" is one number divided by it. The window was inherited as a flat 5,000
bars from a design document that no longer exists and that gave no reason. This
measures one - or rather, it measures that the question has THREE answers and
says what each one costs.

WHAT THIS IS NOT. The obvious experiment is to score each window by how well it
forecasts the next bar's variance, and it was run first. Every instrument
answered "as short as you will let me", pinning at the bottom of the grid. That
is a correct answer to a question nobody is asking: this system already has a
short-horizon estimator - sigma_eff, an EWMA of period 24, floored at
0.2 x sigma_LT (tremor/zscore.py). sigma_LT is deliberately the slow one. What
it is FOR is a stable scale: the unit the ladder is written in, the thing a
reader is told a move was 8.3 times bigger than.

So it is scored on the two jobs it actually has, which pull in opposite
directions.

  A. TRACK THE REGIME. sigma should say what an ordinary hour looks like NOW.
     Scored against a CENTRED estimate of local volatility - a window reaching
     equally into the future, which no live run could use, and which is
     therefore an unbiased statement of what the truth was. Too short and the
     trailing estimate is noise; too long and it describes a market that has
     moved on. This is the bias-variance minimum, and it is the only one of the
     three that is a pure estimation problem.

  B. MEAN THE SAME THING IN EVERY ERA. "8.3x" should carry the same weight in
     2021 as in 2026, or the word attached to it - noticeable, major - is not a
     rarity at all. Scored by how much the 99th percentile of |r| / sigma moves
     from calendar year to calendar year. A stale sigma fails this: Bitcoin's
     hourly volatility has more than halved since 2017, so a window that
     remembers 2017 prints systematically small multiples now.

  C. BE LOUD WHEN IT MATTERS. A fast sigma absorbs a crisis into its own
     denominator within days, and the alerts stop exactly when the market is
     worst. Reported as the share of an instrument's alerts that land in the
     stormiest 5% of its weeks. This is not a loss to minimise - it is the cost
     side of A, and docs/decisions.md is explicit that silence during a crisis
     is the one failure this system will not accept.

A and B want a longer window; A alone has a minimum; C punishes short ones. No
single number is "optimal" because there is no single loss, and pretending
otherwise is what produced the two ladder failures docs/decisions.md records.
What this prints is where 5,000 sits on each curve.

CAUSALITY. Every sigma under test is a rolling window over returns strictly
before the bar, exactly as tremor.returns computes it. The centred estimate in
A is hindsight BY CONSTRUCTION and is the yardstick, never a forecast.

Run:  PYTHONPATH=. python tools/sigma_window.py [--quick] [--out FILE]
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

GRID = (250, 400, 600, 720, 1000, 1500, 2000, 2500, 3000, 4000, 5000, 6500,
        8000, 10000, 13000, 16000, 20000, 25000)

# 720 is the system's own floor (windows.SIGMA_LT_MIN_BARS), below which it
# refuses to compute sigma at all. The grid reaches under it so that a
# criterion wanting something shorter says so rather than piling up on the
# edge and looking like an answer.

# A window is only judged if this much of the instrument's history is left to
# judge it on, or the longest windows would be scored on a short recent stretch.
MIN_EVAL_SHARE = 0.45

# The bandwidth of the centred "truth" in criterion A, in CALENDAR days either
# side. Thirty days is short against the ~100-day half-life of volatility
# measured across this basket, so it is local, and long enough that the estimate
# is not itself mostly noise. Calendar rather than bars on purpose: the truth
# must mean the same thing for an instrument trading 24 hours and one trading 7.
TRUTH_DAYS = (30, 180)

# Two windows whose loss differs by less than this are not distinguishable. The
# loss is an average over tens of thousands of noisy terms, and reading a
# minimum off a curve flatter than its own error bar is how the fitted ladder
# failed twice (docs/decisions.md).
FLAT = 0.02


def bars_per_day(template: str) -> int:
    return {"crypto_24_7": 24, "fx_continuous": 17}.get(template, 7)


def load(asset) -> "pd.DataFrame | None":
    from tremor import returns as R

    folder = "data/tremor/bars/" + asset.asset_id.replace(":", "_").replace("/", "_")
    files = sorted(glob.glob(os.path.join(folder, "*.parquet")))
    if not files:
        return None
    frame = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    frame = (frame.sort_values("hour_utc").drop_duplicates("hour_utc")
                  .reset_index(drop=True))
    return R.split_channels(asset, frame)


def local_truth(returns: pd.Series, half_width: int) -> pd.Series:
    """Volatility around each bar, measured with hindsight.

    Centred, so it lags nothing and leads nothing - which is exactly why no live
    run may use it and exactly why it can serve as the truth a causal estimate
    is scored against.
    """
    width = 2 * half_width + 1
    return (returns.rolling(width, center=True, min_periods=width // 2)
                   .std(ddof=1))


def curves(frame: pd.DataFrame, template: str, rung: float, grid) -> dict:
    r = frame["r"]
    when = pd.to_datetime(frame["hour_utc"], unit="s", utc=True)
    year = when.dt.year.to_numpy()
    # Whole weeks off the epoch, as integers: a week is only a bucket here, and
    # bucketing by a timezone-aware timestamp costs a conversion per bar.
    week = (frame["hour_utc"].to_numpy() // (7 * 86400)).astype("int64")
    truths = {d: local_truth(r, d * bars_per_day(template) // 2).to_numpy()
              for d in TRUTH_DAYS}

    start = max(grid)
    base = np.zeros(len(r), dtype=bool)
    base[start:] = True
    base &= np.isfinite(r.to_numpy()) & (r.to_numpy() != 0.0)
    for t in truths.values():
        base &= np.isfinite(t) & (t > 0)
    if base.sum() < 2000:
        return {}

    out = {}
    for n in grid:
        sigma = (r.shift(1).rolling(n, min_periods=n).std(ddof=1)
                  .to_numpy(dtype="float64"))
        ok = base & np.isfinite(sigma) & (sigma > 0)
        if ok.sum() < 2000:
            continue

        # A: how far the causal estimate sits from the local truth, in logs, so
        # that being half as large and twice as large cost the same. Scored
        # against two different notions of "local", because if the answer moves
        # with that choice then this criterion is measuring the choice.
        track = {d: float(np.mean((np.log(sigma[ok]) - np.log(t[ok])) ** 2))
                 for d, t in truths.items()}

        # B: how much the scale drifts between eras. The 99th percentile of the
        # multiple, year by year - a quantile rather than a tail count, so one
        # violent week cannot decide a year.
        ratio = np.abs(r.to_numpy()[ok]) / sigma[ok]
        per_year = pd.Series(ratio).groupby(year[ok]).quantile(0.99)
        per_year = per_year[pd.Series(ratio).groupby(year[ok]).size() >= 500]
        era = float(np.std(np.log(per_year))) if len(per_year) >= 3 else np.nan

        # C: where the alerts land. Weeks ranked by the instrument's own
        # realised volatility; the top 5% are its storms.
        here = week[ok]
        stress = pd.Series(np.abs(r.to_numpy()[ok])).groupby(here).mean()
        worst = stress.nlargest(max(1, int(0.05 * len(stress)))).index.to_numpy()
        fired = ratio >= rung
        loud = (float(np.isin(here[fired], worst).mean()) if fired.any()
                else float("nan"))

        out[n] = {"era": era, "loud": 100 * loud, "rate": float(fired.sum()),
                  **{f"track{d}": v for d, v in track.items()}}
    return out


def pick(curve: dict, key: str) -> tuple:
    vals = {n: c[key] for n, c in curve.items() if np.isfinite(c[key])}
    if not vals:
        return (np.nan, np.nan, np.nan)
    best = min(vals, key=vals.get)
    floor = vals[best]
    near = [n for n, v in vals.items() if v <= floor * (1 + FLAT)]
    return best, min(near), max(near)


def main(argv=None) -> int:
    from tremor import severity
    from tremor.basket import load_basket

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--quick", action="store_true")
    p.add_argument("--out", default="")
    args = p.parse_args(argv)

    basket = load_basket()
    chosen = basket.instruments
    if args.quick:
        want = {"coinbase:BTC-USD", "coinbase:AVAX-USD", "twelvedata:SPY",
                "twelvedata:HYG", "twelvedata:EUR/USD"}
        chosen = [a for a in chosen if a.asset_id in want]

    rows = []
    for asset in chosen:
        frame = load(asset)
        if frame is None or len(frame) < 6000:
            continue
        grid = [n for n in GRID if n <= len(frame) * MIN_EVAL_SHARE]
        rung = severity.BLOCK_SIGMA.get(asset.block, severity.DEFAULT_SIGMA)[0]
        curve = curves(frame, asset.session_template, rung, grid) if grid else {}
        if not curve:
            continue
        tb, tlo, thi = pick(curve, f"track{TRUTH_DAYS[0]}")
        wb, wlo, whi = pick(curve, f"track{TRUTH_DAYS[1]}")
        eb, elo, ehi = pick(curve, "era")
        here = curve.get(5000, {})
        rows.append({
            "asset": asset.asset_id.split(":")[-1], "block": asset.block,
            "template": asset.session_template, "history": len(frame),
            "track_best": tb, "track_from": tlo, "track_to": thi,
            "wide_best": wb, "wide_from": wlo, "wide_to": whi,
            "era_best": eb, "era_from": elo, "era_to": ehi,
            "track_5000": here.get(f"track{TRUTH_DAYS[0]}", np.nan),
            "track_floor": min(c[f"track{TRUTH_DAYS[0]}"] for c in curve.values()),
            "loud_5000": here.get("loud", np.nan),
            "loud_720": curve.get(720, {}).get("loud", np.nan),
            "loud_best_track": curve.get(tb, {}).get("loud", np.nan),
            "curve": {int(n): {k: round(float(v), 5) for k, v in c.items()}
                      for n, c in curve.items()},
        })
        print(f"  {rows[-1]['asset']:<10} track30 {tb:>6,}  track180 {wb:>6,}"
              f"  era {eb:>6,}  loud@5000 {here.get('loud', float('nan')):.0f}%",
              flush=True)

    t = pd.DataFrame(rows)
    if args.out:
        json.dump(rows, open(args.out, "w", encoding="utf-8"), indent=1)
    if t.empty:
        print("nothing measurable")
        return 1

    print("\n" + "=" * 78)
    print("A. TRACK THE REGIME - and why it cannot settle anything. The best window")
    print("   under a 30-day notion of 'the regime', and under a 180-day one:")
    for tpl, g in t.groupby("template"):
        print(f"  {tpl:<16} n={len(g):<3} truth +-30d -> {g.track_best.median():>7,.0f}"
              f"     truth +-180d -> {g.wide_best.median():>7,.0f}")
    print("\nB. MEAN THE SAME IN EVERY ERA - best window")
    for tpl, g in t.groupby("template"):
        print(f"  {tpl:<16} best {g.era_best.median():>7,.0f}"
              f"   flat {g.era_from.median():>6,.0f}-{g.era_to.median():<7,.0f}")
    print("\nC. LOUD IN A STORM - % of alerts landing in the worst 5% of weeks,")
    print("   across the whole grid. This is the one criterion that wants LENGTH.")
    show = [250, 720, 2000, 5000, 10000, 20000]
    print(f"  {'':<16}" + "".join(f"{n:>9,}" for n in show))
    for tpl, g in t.groupby("template"):
        line = f"  {tpl:<16}"
        for n in show:
            vals = [r["curve"].get(str(n), r["curve"].get(n, {})).get("loud", np.nan)
                    for _, r in g.iterrows()]
            vals = [v for v in vals if v == v]
            line += f"{np.median(vals):>8.0f}%" if vals else f"{'-':>9}"
        print(line)
    print("\nWhere 5,000 sits: above every window any estimation criterion picks,")
    print("and chosen - by this measurement - for storm coverage rather than accuracy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

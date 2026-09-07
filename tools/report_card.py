"""The report card: what the alerts look like, measured after the fact.

Nothing here feeds back into the system. No threshold is set from these numbers,
no module imports this file, and the detector cannot see its own alert count.
That is the point - the alert rate is a thing to look at afterwards, not a thing
to steer by.

Three questions, in the order they matter:

  1. RECALL ON THE OBVIOUS. Of the hours that were unmistakably big FOR THAT
     INSTRUMENT, how many reached me? This should be 100%. "For that instrument"
     is not a nicety - 5% is a quiet hour in SOL and an apocalypse in SHY, and a
     flat threshold scores the ladder against a target it is designed to miss.
     So the bucket is the instrument's own |r| percentile: a ranking, not the
     system's own fitted tail, so it is not grading its own homework.

  2. FALSE ALARMS ON THE OBVIOUSLY NORMAL. Of the hours below an instrument's
     median move, how many opened an event? This should be 0%.

  3. FREQUENCY. How often the phone actually buzzes, and how long the quiet
     stretches are. Alerts per year is the number everyone quotes and the least
     useful of the three: 27 a year reads as "one a fortnight" and is in fact a
     median gap of eight days with a tenth of them a day apart.

Two matching rules, because the two questions are different. Recall matches an
hour against an open event's COOLDOWN window - the automaton folds repeats for
twelve bars, so the one alert the reader got speaks for all of them, and scoring
negative oil at 18:00 as a miss because the push went out at 16:00 measures
`routing.collapse` rather than the detector. False alarms match on the hour an
event OPENED, which is the system claiming that hour was remarkable.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from pathlib import Path

from meals import routing, windows

BARS_DIR = Path("data/meals/bars")
RESIDUALS_DIR = Path("data/meals/residuals")
EVENTS_PATH = Path("data/meals/saed_events.parquet")

# Rank of each channel, so an hour covered by several events is credited with
# the loudest one that spoke for it.
RANK = {"push": 3, "digest": 2, "dropped": 1}

# "Obvious" is the top of an instrument's own distribution. 0.01% of twenty-two
# years of hours is a handful of bars per instrument - the ones nobody would
# accept missing.
OBVIOUS_PERCENTILE = 99.99
MIN_HOURS = 1000       # too little history to rank against


def _asset_for(stem: str, ids) -> str | None:
    for asset_id in ids:
        if asset_id.replace(":", "_").replace("/", "_") == stem:
            return asset_id
    return None


def load_events() -> pd.DataFrame:
    events = pd.read_parquet(EVENTS_PATH)
    channels, _ = routing.collapse(events, routing.channel(events))
    return events.assign(channel=channels)


def per_asset(events: pd.DataFrame):
    """For each instrument: its hours, |r|, coverage, firings and ladder state."""
    for path in sorted(BARS_DIR.glob("*.parquet")):
        asset_id = _asset_for(path.stem, events["asset_id"].unique())
        if asset_id is None:
            continue
        residuals = RESIDUALS_DIR / path.name
        if not residuals.exists():
            continue
        scored = pd.read_parquet(residuals, columns=["hour_utc", "level_routine"])
        bars = pd.read_parquet(path)
        hours = bars["hour_utc"].to_numpy("int64")
        # The system's own return channel. NOT close-to-close, which spans the
        # overnight gap the detector deliberately does not watch.
        move = np.abs(np.log(bars["close"].to_numpy(float) / bars["open"].to_numpy(float)))
        keep = np.isin(hours, scored["hour_utc"].to_numpy("int64")) & np.isfinite(move)
        hours, move = hours[keep], move[keep]
        if len(hours) < MIN_HOURS:
            continue

        fitted = scored.set_index("hour_utc")["level_routine"].reindex(hours).notna().to_numpy()
        mine = events[events["asset_id"] == asset_id]
        covered = np.zeros(len(hours), dtype=int)
        for opened, channel in zip(mine["hour_utc"].astype("int64"), mine["channel"]):
            i = int(np.searchsorted(hours, opened, "left"))
            j = min(i + windows.SAED_COOLDOWN_BARS + 1, len(hours))
            covered[i:j] = np.maximum(covered[i:j], RANK.get(channel, 0))
        fired = np.isin(hours, mine["hour_utc"].to_numpy("int64"))
        percentile = np.searchsorted(np.sort(move), move, "left") / len(move) * 100
        yield asset_id, hours, move, percentile, covered, fired, fitted


def report(events: pd.DataFrame) -> str:
    obvious = {"n": 0, "unfitted": 0, "push": 0, "digest": 0, "dropped": 0, "silent": 0}
    quiet = {"n": 0, "fired": 0}
    lines: list[str] = []

    for asset_id, _, move, percentile, covered, fired, fitted in per_asset(events):
        if "coinbase" in asset_id:
            # Crypto is excluded from the headline, not out of squeamishness: a
            # single instrument contributes thousands of 2% hours and would set
            # the number for everybody. Its own row is printed below.
            pass
        top = percentile >= OBVIOUS_PERCENTILE
        low = percentile < 50.0
        if "coinbase" not in asset_id:
            obvious["n"] += int(top.sum())
            obvious["unfitted"] += int((top & ~fitted).sum())
            seen = top & fitted
            for key, value in (("push", 3), ("digest", 2), ("dropped", 1), ("silent", 0)):
                obvious[key] += int((seen & (covered == value)).sum())
            quiet["n"] += int(low.sum())
            quiet["fired"] += int(fired[low].sum())
        lines.append(f"  {asset_id:22s} top {int(top.sum()):3d}h  "
                     f"silent {int((top & fitted & (covered == 0)).sum()):2d}  "
                     f"median hour {np.median(move):.4%}")

    fitted_n = obvious["n"] - obvious["unfitted"]
    pushes = events[events["channel"].eq("push")]
    hours = np.sort(pushes["hour_utc"].to_numpy("int64"))
    gaps = np.diff(hours) / 86400.0
    years = (hours[-1] - hours[0]) / 86400.0 / 365.25
    per_day = pd.to_datetime(hours, unit="s").floor("D").value_counts()

    out = [
        "RECALL ON THE OBVIOUS  (top 0.01% of each instrument's own hours, ex-crypto)",
        f"  hours                  {obvious['n']}",
        f"  no ladder fitted yet   {obvious['unfitted']}   (instrument too young to have levels)",
        f"  of the {fitted_n} with a ladder:",
        f"    push               {obvious['push']:5d}  {100*obvious['push']/fitted_n:5.1f}%",
        f"    digest             {obvious['digest']:5d}  {100*obvious['digest']/fitted_n:5.1f}%",
        f"    dropped, reverted  {obvious['dropped']:5d}  {100*obvious['dropped']/fitted_n:5.1f}%",
        f"    SILENT             {obvious['silent']:5d}  {100*obvious['silent']/fitted_n:5.1f}%   <- wants to be 0",
        "",
        "FALSE ALARMS ON THE OBVIOUSLY NORMAL  (hours below the instrument's median move)",
        f"  hours                  {quiet['n']}",
        f"  opened an event        {quiet['fired']}  {100*quiet['fired']/quiet['n']:.3f}%   <- wants to be 0",
        "",
        "FREQUENCY",
        f"  pushes                 {len(hours)} over {years:.1f} years = {len(hours)/years:.1f}/yr",
        f"  gap between pushes     median {np.median(gaps):.1f}d, "
        f"quarter of them under {np.percentile(gaps, 25):.1f}d, "
        f"tenth under {np.percentile(gaps, 10):.1f}d",
        f"  longest quiet stretch  {gaps.max():.0f} days",
        f"  worst single day       {per_day.max()} pushes",
        f"  digest                 {int(events['channel'].eq('digest').sum())} lines "
        f"= {events['channel'].eq('digest').sum()/years/104:.1f} per digest (two a week)",
        f"  never sent, reverted   {int(events['channel'].eq('dropped').sum())}",
        "",
        "PER INSTRUMENT",
        *lines,
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    print(report(load_events()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

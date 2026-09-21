"""Derives the three rung tables in tremor/severity.py, to be pasted by hand.

    PYTHONPATH=. python tools/ladder.py

WHY A TOOL THAT PRINTS RATHER THAN A FUNCTION THAT FITS. severity.py says it
plainly: a number that refits itself is a number nobody can reason about, and
refitting is what produced the two failures that file records. So the rungs stay
written-down constants. This derives them once, prints them as Python, and a
person pastes them in. Re-running it a year later and diffing is the whole
maintenance story.

WHAT WAS WRONG WITH THE OLD SPACING. The rungs were a uniform 1.47x apart in
size, in every block. How much RARER that makes a rung depends on the block's
tail, and the tails differ: measured over the archive the exponent runs from 2.78
in credit to 3.96 in equity, so one step was 2.92x rarer in credit and 4.60x in
equity. The effect compounds up the ladder - among the ten instruments with
at least ten top-rung events, enough for the ratio to mean anything, `extreme`
cost 2.5 `noticeable` events on EMB and 18.3 on USD/CAD, and the tail exponent
predicted which (correlation +0.83). Keep that restriction attached to the
number: the median instrument reaches the top rung six times in twenty-three
years, so unrestricted the ratio is mostly counting error.

THE CONSTRUCTION IS GUTENBERG-RICHTER'S. A magnitude scale is spaced so that each
class is a fixed factor rarer than the one below, and the size step that achieves
that is measured rather than chosen. Half a magnitude unit - 3.162x - keeps four
rungs inside the span the reader already has; a full unit would leave room for
two.

EACH RUNG IS AN EMPIRICAL QUANTILE, NOT AN EXTRAPOLATION, and the difference is
worth stating because the obvious method is the worse one. Given a tail exponent
one can place rung i at anchor * STEP**(i/alpha) and be done. Tried, and it
lands between 2.93x and 5.29x against a 3.162x target, because a real return tail
is not a clean power law - fit alpha on the top 1% and the implied exponent still
drifts as you walk further out, so the extrapolation is wrong by more the further
it reaches, which is exactly at the top rung.

So the rate is taken as the target and the level read straight off the data: the
held rung's rate is measured, each other rung wants that rate divided (or
multiplied) by STEP, and the rung IS the quantile that delivers it. No
distributional assumption, and the realised ratio is 3.162x by construction. The
tail exponent is still estimated and printed, because it says how far apart the
blocks are and is the reason a single shared size step could never have worked -
but it is now a diagnostic rather than the mechanism.

WHAT ANCHORS EACH TABLE, which is the part that is a judgement and not a
measurement:

  BLOCK_SIGMA       the bottom rung, held exactly as it was. It sets the length
                    of the weekly note - three quarters of the note's rows are
                    `noticeable` - and that is the one number a reader can
                    actually check by reading a note.

  BLOCK_MOVE_SIGMA  `major`, not the bottom. blocks.events_frame filters block
                    events to the push tiers, so a block at `noticeable` or
                    `high` computes a tier and never produces anything. Anchoring
                    on a rung that cannot fire would anchor on nothing.

  BLOCK_RESID_SIGMA neither: it is rate-matched to BLOCK_SIGMA, rung by rung,
                    which is the rule its own comment already states. It scores a
                    BMP t-statistic and the absolute ladder scores a raw return
                    over its own sigma; the two do not live on the same scale, so
                    the only sane way to make `major` mean one thing on both is to
                    match how often each fires.

THE TAIL ESTIMATE IS A HILL ESTIMATOR on the top 1% of each block's pooled
|series|, which is the standard tool and is what the literature's "inverse cubic
law" for returns is measured with. The basket mean lands at 3.5, which is that
law, so the estimate is not saying anything exotic.
"""
from __future__ import annotations

import argparse
import glob
import os

import numpy as np
import pandas as pd

from tremor import basket as bk, cross_section, severity

# Half a magnitude unit. One rung is this many times rarer than the one below,
# in every block - which is the whole point of the exercise.
STEP = 3.162

# Share of each series used for the tail fit. 1% of a 40,000-bar history is 400
# exceedances, well above the 50-100 the POT literature asks for.
TAIL_FRACTION = 0.01
MIN_EXCEEDANCES = 60

RESIDUALS_DIR = os.path.join("data", "tremor", "residuals")
METRICS_DIR = os.path.join("data", "tremor", "metrics")


def hill(values: np.ndarray, fraction: float = TAIL_FRACTION) -> float:
    """The Hill estimate of the tail exponent, from the largest `fraction`.

    Returns alpha in P(X > x) ~ x**-alpha. Larger alpha is a thinner tail.
    """
    v = np.asarray(values, dtype="float64")
    v = v[np.isfinite(v) & (v > 0)]
    k = max(int(len(v) * fraction), MIN_EXCEEDANCES)
    if len(v) <= k:
        return float("nan")
    top = np.sort(v)[-k:]
    return float(1.0 / np.mean(np.log(top / top[0])))


def rungs_from(values: np.ndarray, anchor: float, anchor_index: int,
               step: float = STEP,
               n: int = len(severity.TIERS)) -> tuple[float, ...]:
    """The ladder whose every rung is `step` times rarer than the one below.

    `anchor_index` is the rung being held - 0 for the bottom, 2 for `major`. Its
    rate is measured on `values`, every other rung's target rate follows from it,
    and each level is the quantile of `values` that delivers that rate. The held
    rung is returned exactly as given, not re-read, so holding it means holding
    it.
    """
    v = np.asarray(values, dtype="float64")
    v = v[np.isfinite(v)]
    base = float((v >= anchor).mean())
    out = []
    for i in range(n):
        if i == anchor_index:
            out.append(round(float(anchor), 1))
            continue
        rate = base / (step ** (i - anchor_index))
        # Below the anchor the target rate can exceed 1 on a short series; the
        # quantile is then the series minimum, which is the honest answer - that
        # rung cannot be made commoner than "every bar".
        out.append(round(float(np.quantile(v, max(0.0, 1.0 - rate))), 1))
    return tuple(out)


def member_series(basket, scale: str) -> dict[str, tuple[str, pd.Series]]:
    """Each instrument's signed move over `scale`, keyed by asset_id.

    THE TWO LADDERS DIVIDE BY DIFFERENT THINGS, which is easy to miss and gives a
    silently wrong answer if it is. The absolute ladder scores `|r| / sigma_lt`,
    the long-run yardstick. The block's own move is a median of `r / sigma_eff`
    (cross_section.block_moves via saed's `panels`, which builds its sigma panel
    from the `sigma_eff` column), the fast adaptive estimate - so the block series
    runs hotter in a calm stretch than the same construction on sigma_lt would.
    Reconstructing it with sigma_lt put three blocks' top rung above anything
    their series had ever reached, while the live pipeline has those blocks
    firing at `extreme` four times each. Hence the parameter.

    Signed and block-oriented, because the block factor is a median and a median
    only means something across series that move the same way (see
    Asset.block_sign).
    """
    out: dict[str, tuple[str, pd.Series]] = {}
    for asset in basket.instruments:
        path = os.path.join(METRICS_DIR, f"{asset.file_stem}.parquet")
        if not os.path.exists(path):
            continue
        frame = pd.read_parquet(path, columns=["hour_utc", "r", scale])
        denom = frame[scale].where(np.isfinite(frame[scale]) & (frame[scale] > 0))
        scaled = (frame["r"] / denom) * asset.block_sign
        scaled.index = frame["hour_utc"].to_numpy()
        out[asset.asset_id] = (asset.block, scaled[np.isfinite(scaled)])
    return out


def block_medians(members: dict[str, tuple[str, pd.Series]]) -> dict[str, pd.Series]:
    """A block's own move: the median across its members, hour by hour.

    A bar is kept only where cross_section.BLOCK_MOVE_MIN_MEMBERS reported, the
    same floor the live construction uses - two, below which a median is a copy
    of whichever member happened to be trading.
    """
    out: dict[str, pd.Series] = {}
    for block in sorted({b for b, _ in members.values()}):
        series = [s for b, s in members.values() if b == block]
        panel = pd.concat(series, axis=1)
        median = panel.median(axis=1, skipna=True)
        quorum = panel.notna().sum(axis=1) >= cross_section.BLOCK_MOVE_MIN_MEMBERS
        out[block] = median[quorum]
    return out


def abnormal_series() -> dict[str, pd.Series]:
    """|z_resid_bmp| per block, pooled across the block's members."""
    pooled: dict[str, list[np.ndarray]] = {}
    basket = bk.load_basket()
    block_of = {a.asset_id: a.block for a in basket.instruments}
    for path in sorted(glob.glob(os.path.join(RESIDUALS_DIR, "*.parquet"))):
        frame = pd.read_parquet(path, columns=["asset_id", "z_resid_bmp"])
        if frame.empty:
            continue
        block = block_of.get(str(frame["asset_id"].iloc[0]))
        if block is None:
            continue
        values = frame["z_resid_bmp"].abs().to_numpy(dtype="float64")
        pooled.setdefault(block, []).append(values[np.isfinite(values)])
    return {b: pd.Series(np.concatenate(v)) for b, v in pooled.items()}


def rate_matched(target_rungs: tuple[float, ...], absolute: np.ndarray,
                 abnormal: np.ndarray) -> tuple[float, ...]:
    """The |z| levels that fire as often as `target_rungs` fire on `absolute`.

    Both series are pooled over the same block but are not the same length - the
    residual series starts where the regression has enough history - so the match
    is on RATE, a share of each series' own bars, not on a count.
    """
    out = []
    for rung in target_rungs:
        share = float((absolute >= rung).mean())
        out.append(round(float(np.quantile(abnormal, 1.0 - share)), 1)
                   if share > 0 else float(np.max(abnormal)))
    return tuple(out)


def check(name: str, rows: dict[str, tuple[float, ...]]) -> None:
    """Refuse to print a table basket.py would reject, and say which block.

    Rungs must increase and be positive (tremor/basket.py enforces both). A
    non-monotonic row means the anchor sat so far out on its series that the
    quantile above it saturated - which is a wrong SERIES, not a wrong step, and
    is exactly how reconstructing the block move with the wrong sigma showed up.
    """
    for block, values in rows.items():
        if values[0] <= 0:
            raise SystemExit(f"{name}[{block}]: rungs must be positive, got {values}")
        if not all(a < b for a, b in zip(values, values[1:])):
            raise SystemExit(
                f"{name}[{block}]: rungs must increase, got {values}. The anchor is "
                f"probably beyond what this series reaches, so the quantiles above "
                f"it saturate - check the series, not the step.")


def _table(name: str, annotation: str, rows: dict[str, tuple[float, ...]]) -> str:
    width = max(len(b) for b in rows) + 3
    lines = [f"{name}: {annotation} = {{"]
    for block, values in sorted(rows.items(), key=lambda kv: -kv[1][0]):
        cells = ", ".join(f"{v:5.1f}" for v in values)
        lines.append(f'    "{block}":'.ljust(width + 5) + f" ({cells}),")
    lines.append("}")
    return "\n".join(lines)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--step", type=float, default=STEP,
                        help="how many times rarer each rung is than the one below")
    args = parser.parse_args(argv)
    step = args.step

    basket = bk.load_basket()
    members = member_series(basket, "sigma_lt")
    medians = block_medians(member_series(basket, "sigma_eff"))
    abnormal = abnormal_series()

    pooled_abs = {}
    for block in sorted({b for b, _ in members.values()}):
        vals = np.concatenate([s.abs().to_numpy() for b, s in members.values()
                               if b == block])
        pooled_abs[block] = vals[np.isfinite(vals) & (vals > 0)]

    print(f"Ladder derivation: each rung {step}x rarer than the one below.\n")
    print(f"  {'block':19s} {'alpha':>6s} {'size step':>10s}   "
          f"{'alpha(block)':>12s} {'size step':>10s}")
    alpha_member, alpha_block = {}, {}
    for block in sorted(pooled_abs):
        am = hill(pooled_abs[block])
        ab = hill(medians[block].abs().to_numpy())
        alpha_member[block], alpha_block[block] = am, ab
        print(f"  {block:19s} {am:6.2f} {step**(1/am):10.2f}   "
              f"{ab:12.2f} {step**(1/ab):10.2f}")

    absolute = {b: rungs_from(pooled_abs[b], severity.BLOCK_SIGMA[b][0], 0, step)
                for b in pooled_abs if b in severity.BLOCK_SIGMA}
    move = {b: rungs_from(medians[b].abs().to_numpy(),
                          severity.BLOCK_MOVE_SIGMA[b][2], 2, step)
            for b in medians if b in severity.BLOCK_MOVE_SIGMA}
    resid = {}
    for block, rung in absolute.items():
        if block in abnormal:
            resid[block] = rate_matched(rung, pooled_abs[block],
                                        abnormal[block].to_numpy())

    # A fallback block has no series of its own to read quantiles off, so its
    # default is the element-wise median of the blocks that do - a typical
    # ladder rather than an extrapolation from a tail nobody measured.
    def typical(rows):
        return tuple(round(float(np.median([v[i] for v in rows.values()])), 1)
                     for i in range(len(severity.TIERS)))

    print("\n" + "=" * 70)
    print("PASTE INTO tremor/severity.py")
    print("=" * 70 + "\n")
    check("BLOCK_SIGMA", absolute)
    check("BLOCK_RESID_SIGMA", resid)
    check("BLOCK_MOVE_SIGMA", move)
    print(_table("BLOCK_SIGMA", "dict[str, tuple[float, float, float, float]]",
                 absolute))
    print(f"DEFAULT_SIGMA: tuple[float, float, float, float] = "
          f"{typical(absolute)}\n")
    print(_table("BLOCK_RESID_SIGMA", "dict[str, tuple[float, float, float, float]]",
                 resid))
    print(f"DEFAULT_RESID_SIGMA = {typical(resid)}\n")
    print(_table("BLOCK_MOVE_SIGMA", "dict[str, tuple[float, float, float, float]]",
                 move))
    print(f"DEFAULT_BLOCK_MOVE_SIGMA: tuple[float, float, float, float] = "
          f"{typical(move)}")

    print("\n" + "=" * 70)
    print("REALISED RATIOS - what the derived rungs actually do on the archive")
    print("=" * 70)
    print(f"\n  {'block':19s} {'rung1/rung2':>12s} {'rung2/rung3':>12s} "
          f"{'rung3/rung4':>12s}    (target {step})")
    for block, rung in sorted(absolute.items()):
        counts = [(pooled_abs[block] >= r).sum() for r in rung]
        ratios = [counts[i] / counts[i + 1] if counts[i + 1] else float("nan")
                  for i in range(3)]
        print(f"  {block:19s} " + " ".join(f"{r:12.2f}" for r in ratios))
    print(f"\n  the block's own ladder:")
    for block, rung in sorted(move.items()):
        v = medians[block].abs().to_numpy()
        counts = [(v >= r).sum() for r in rung]
        ratios = [counts[i] / counts[i + 1] if counts[i + 1] else float("nan")
                  for i in range(3)]
        print(f"  {block:19s} " + " ".join(f"{r:12.2f}" for r in ratios))
    print("\n  today's member ladder, for comparison:")
    for block in sorted(absolute):
        cur = severity.BLOCK_SIGMA[block]
        counts = [(pooled_abs[block] >= r).sum() for r in cur]
        ratios = [counts[i] / counts[i + 1] if counts[i + 1] else float("nan")
                  for i in range(3)]
        print(f"  {block:19s} " + " ".join(f"{r:12.2f}" for r in ratios))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The cross-section the detector reads: panels, the block factor, the block move.

Everything here is one question asked across instruments at once - what did the
rest of this instrument's block do this hour - and it is built in memory by
tremor.saed from the per-instrument metrics.

The basket-wide statistics that used to live here (quorum, the basket median,
dispersion compression, PCA synchrony, the single-factor trigger) fed the cluster
detector and nothing else. That detector was deleted, and they were removed with
the price z-score they were built on.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from tremor.basket import Basket


def build_panel(metrics: dict[str, pd.DataFrame], column: str = "r") -> pd.DataFrame:
    """Wide panel: rows are hours, columns are assets, values are `column`.

    Gaps stay gaps. FILLING THEM WITH ZEROS IS FORBIDDEN: a zero asserts "the
    asset did not move", while its absence means "we do not know", and swapping
    one for the other inflates the basket's coherence exactly where there is no
    data.
    """
    series = {aid: df.set_index("hour_utc")[column] for aid, df in metrics.items()}
    return pd.DataFrame(series).sort_index()


def block_factors(panel: pd.DataFrame, basket: Basket,
                  sigma_panel: pd.DataFrame | None = None) -> pd.DataFrame:
    """The own-block factor for each instrument, EXCLUDING the instrument itself.

    THE ONLY REGRESSOR. There is no basket-wide factor beside it: a median
    cannot carry a factor whose members respond with opposite signs, and the
    equity-rates sign is not stable across the record. See basket.yaml.

    A block factor works because its members do agree. In hours when four or more currency pairs fire, 97% of the
    time they all agree on the direction of the dollar, and for crypto the
    agreement on residual sign is 100% at the median. Those are not independent
    idiosyncratic moves but one block move, and a module meant to catch
    SINGLE-ASSET moves was firing in blocks, systematically.

    Excluding the asset itself is mandatory. Otherwise, in a block of three
    instruments, one would subtract a third of itself and its own move would
    partly vanish from the residual - exactly the error the design guards against
    by estimating beta on data before the current bar.

    STANDARDISED BEFORE THE MEDIAN IS TAKEN, and this is what changed when the
    blocks were widened. A plain median across raw returns is set by whichever
    member happens to lie in the middle, and in a block that spans SHY and TLT
    the middle member moves twenty-five times less than the extreme one - so the
    factor was reporting the calm end of the block and calling it the block. The
    fix is to divide each member by its own effective sigma, take the median of
    those unitless numbers, and multiply back by the sigma of the instrument
    being modelled:

        B_i = sigma_i * median_{j != i} ( sign_j * r_j / sigma_j ) * sign_i

    which is scale-free in the members and lands in the units of instrument i.
    With no sigma panel the plain median is used, which is what a block of
    like-for-like instruments gave before and what the tests are written against.

    The median is unweighted, and that is not a simplification: under the
    equality rule all weights within a block are equal, so a block's weighted
    median coincides with the plain one.
    """
    members, signs = _block_members(panel, basket)
    out = pd.DataFrame(index=panel.index, dtype="float64")
    for block, columns in members.items():
        scale = _scale_row(sigma_panel, columns, panel.index)
        unit = _oriented_units(panel, columns, signs, scale)
        for position, asset_id in enumerate(columns):
            others = np.delete(unit, position, axis=1)
            factor = (_nanmedian(others) if others.size
                      else np.full(len(panel), np.nan))
            # Returned in the INSTRUMENT'S own units and orientation: the
            # regression that consumes this expects a series the instrument moves
            # with, and converting back here keeps beta_block near one for a
            # member that simply follows its block.
            out[asset_id] = factor * scale[:, position] * signs[asset_id]

        # Instruments outside the basket do not enter the factor, so there is
        # nothing to exclude for them - the whole block median is used, and their
        # own sigma scales it back.
        for asset in basket.outside:
            if asset.block != block:
                continue
            own = _scale_row(sigma_panel, [asset.asset_id], panel.index)[:, 0]
            out[asset.asset_id] = _nanmedian(unit) * own * signs[asset.asset_id]
    return out


def block_moves(panel: pd.DataFrame, basket: Basket,
                sigma_panel: pd.DataFrame | None = None) -> pd.DataFrame:
    """One series per block: how far the block itself moved, in sigmas.

    The block factor above answers "what did this instrument's peers do", once
    per instrument and in that instrument's units. This answers the question the
    peers themselves raise - "did the whole block move" - once per block, and it
    is a different question with a different consumer: the ladder is fitted to
    THIS series to give a block its own return period.

    Why it has to exist at all. Widening the blocks made every member's residual
    smaller on the days the block moves together, which is the point - but it
    also means that on those days no member is abnormal and nothing pushes. The
    biggest days in the record are exactly the days a whole complex moves as one,
    and without this series they would be the quietest.

    Unitless on purpose, where block_factors converts back. A block has no units
    of its own - "the equity block moved 2%" is a claim about a median of
    percentages across instruments with different volatilities - so the honest
    statement is in sigmas: the median member moved this many times its own usual
    hour. The message converts that back into named members and their own
    percentages, which is what a reader can check.

    Every member counts here, including the ones the leave-one-out factor omits
    for a given instrument, because the subject is the block rather than any one
    of its members. Instruments outside the basket stay out, for the same reason
    they stay out of the factor: they are watched, not counted.
    """
    members, signs = _block_members(panel, basket)
    out = pd.DataFrame(index=panel.index, dtype="float64")
    counts = pd.DataFrame(index=panel.index, dtype="float64")
    for block, columns in members.items():
        scale = _scale_row(sigma_panel, columns, panel.index)
        unit = _oriented_units(panel, columns, signs, scale)
        out[block] = _nanmedian(unit)
        counts[block] = np.isfinite(unit).sum(axis=1)
    return out.where(counts >= BLOCK_MOVE_MIN_MEMBERS)


# How many members must be present in an hour before the block is credited with
# a move of its own. Two is the floor at which a median is a median rather than
# a copy of the one instrument that happened to be trading, and it matches the
# floor the configuration already enforces on a block's size.
BLOCK_MOVE_MIN_MEMBERS = 2


def _block_members(panel: pd.DataFrame,
                   basket: Basket) -> "tuple[dict[str, list[str]], dict[str, float]]":
    """Which basket instruments make up each block, and how each is oriented."""
    members: dict[str, list[str]] = {}
    signs: dict[str, float] = {}
    for asset in basket.assets:
        if asset.asset_id in panel.columns:
            members.setdefault(asset.block, []).append(asset.asset_id)
            signs[asset.asset_id] = asset.block_sign
    for asset in basket.outside:
        signs.setdefault(asset.asset_id, asset.block_sign)
    return members, signs


def _oriented_units(panel: pd.DataFrame, columns: list[str],
                    signs: dict[str, float], scale: np.ndarray) -> np.ndarray:
    """Members' returns, sign-oriented and divided by their own scale.

    ORIENTED before any median is taken. A median represents a common move only
    if the members respond to it with the same sign, and the FX block does not:
    it holds pairs with the dollar as base and pairs with it as quote, so a
    dollar move pushes half up and half down and the median of the six is close
    to nothing. See Asset.block_sign for the measurement - the block factor was
    seeing about a quarter of the dollar move and the rest was leaking into every
    member's residual.
    """
    sign_row = np.array([signs[c] for c in columns], dtype="float64")
    oriented = panel[columns].to_numpy(dtype="float64") * sign_row
    with np.errstate(invalid="ignore", divide="ignore"):
        return oriented / scale


def _nanmedian(values: np.ndarray) -> np.ndarray:
    """Row-wise median ignoring gaps, quietly where a whole row is missing.

    An hour in which no member of a block traded is an ordinary fact - most
    blocks are shut for most of the day - and numpy's warning for it would be
    emitted once per row per block, which at a hundred thousand hours drowns the
    log the run is supposed to be read from.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmedian(values, axis=1)


def _scale_row(sigma_panel: "pd.DataFrame | None", columns: list[str],
               index: pd.Index) -> np.ndarray:
    """The per-member scale the block median is taken in, as a 2-D array.

    Ones when there is no sigma panel, which reduces the whole construction to
    a plain median of raw returns. A sigma that is missing, zero
    or negative becomes NaN rather than one: dividing by a scale we do not have
    would put that member into the median at its raw size, which is the very
    thing standardising is meant to stop.
    """
    if sigma_panel is None:
        return np.ones((len(index), len(columns)), dtype="float64")
    frame = sigma_panel.reindex(index=index, columns=columns)
    scale = frame.to_numpy(dtype="float64")
    return np.where(np.isfinite(scale) & (scale > 0), scale, np.nan)

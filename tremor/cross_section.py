"""Cross-section of the basket: quorum, M_t, CSV, PCA (spec §2.3, §3.2-3.4).

This is where the system stops looking at assets one by one and starts measuring
the thing it exists for: HOW COHERENTLY the market as a whole is moving. A single
spike in one asset belongs to phase 2 and the SAED module; a cluster event is
several blocks jerking at once, and that can only be seen by looking at every
series simultaneously.

Two independent ways of catching the same coherence:

- COMPRESSION of dispersion (§3.2). Normally assets disagree: each has its own
  news, its own order flow. When a common macro factor arrives, the dispersion
  between them collapses - everyone moves the same way by about the same amount -
  and the basket itself shifts noticeably. A narrow spread together with a large
  common move is the signature of a common factor.
- SYNCHRONY via PCA (§3.3). The same thing from the other side: if the first
  principal component explains an unusually large share of the variance, then one
  common cause is driving every asset.

Per §3.4 they are combined with OR and logged separately - and the backtest must
check the correlation of their firings: above 0.7, one of the sub-conditions is
dropped from the production configuration as redundant.
"""
from __future__ import annotations

import os
import warnings

import numpy as np
import pandas as pd

from tremor import windows
from tremor.basket import Basket

# Quorum of an hour (§2.3).
QUORUM_MIN_ASSETS = 8
QUORUM_MIN_TIER1 = 2
QUORUM_MIN_BLOCKS = 2
QUORUM_MIN_PER_BLOCK = 2

# PCA conditioning requirements (§3.3).
PCA_MIN_ASSETS = 3
PCA_MIN_ROWS = 60
PCA_ROWS_PER_ASSET = 3
CONSTANT_COLUMN_STD = 1e-12

# Margin added to the median in the synchrony threshold (§3.3).
PCA_SYNC_MARGIN = 0.05

# How many defined values of PC1_ratio must fall inside the W_cs window for the
# percentile threshold to mean anything.
#
# The spec's window is 1200 reference-calendar hours, and it must not change:
# that is the calendar span of the statistic, ten trading weeks. But PC1_ratio
# exists only in full-regime hours, and those are about 28% - a 1200-hour window
# contains roughly 335 values. Demanding 1200 OBSERVATIONS inside 1200 HOURS is
# demanding the impossible: under that condition the threshold is never computed
# in the whole history. So the span stays as specified while the requirement is
# placed on the number of values, chosen so that the 95th percentile rests on at
# least five points in the tail.
PCA_STAT_MIN_OBS = 100


def weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    """Weighted median: the value with half the weight lying to its left.

    A median, not a mean, because M_t must describe the basket AS A WHOLE and not
    yield to one asset that took off today. Weighted, because an asset's weight
    is set by the block-equality rule (§2.3), and without weights a block of six
    currency pairs would outvote a block of three crypto assets purely by
    headcount.
    """
    order = np.argsort(values)
    v, w = values[order], weights[order]
    cumulative = np.cumsum(w)
    half = cumulative[-1] / 2
    index = int(np.searchsorted(cumulative, half))
    if index > 0 and np.isclose(cumulative[index - 1], half):
        # The weight splits exactly in half - take the midpoint between
        # neighbours so the result does not depend on how equal weights sorted.
        return float((v[index - 1] + v[index]) / 2)
    return float(v[min(index, len(v) - 1)])


def build_panel(metrics: dict[str, pd.DataFrame], column: str = "r") -> pd.DataFrame:
    """Wide panel: rows are hours, columns are assets, values are `column`.

    Gaps stay gaps. §3.3 FORBIDS filling them with zeros: a zero asserts "the
    asset did not move", while its absence means "we do not know", and swapping
    one for the other inflates the basket's coherence exactly where there is no
    data.
    """
    series = {aid: df.set_index("hour_utc")[column] for aid, df in metrics.items()}
    return pd.DataFrame(series).sort_index()


def quorum(panel: pd.DataFrame, basket: Basket) -> pd.DataFrame:
    """Quorum of an hour per §2.3: whether there are enough assets, tiers and
    blocks for the hour to be worth assessing at all."""
    present = panel.notna()
    blocks = {a.asset_id: a.block for a in basket.assets}
    tier1 = [a.asset_id for a in basket.assets if a.tier == 1]
    columns = [c for c in panel.columns if c in blocks]
    present = present[columns]

    n_assets = present.sum(axis=1)
    n_tier1 = present[[c for c in columns if c in tier1]].sum(axis=1)

    per_block = {}
    for block in {blocks[c] for c in columns}:
        members = [c for c in columns if blocks[c] == block]
        per_block[block] = present[members].sum(axis=1)
    block_counts = pd.DataFrame(per_block)
    populated = (block_counts >= QUORUM_MIN_PER_BLOCK).sum(axis=1)

    ok = ((n_assets >= QUORUM_MIN_ASSETS)
          & (n_tier1 >= QUORUM_MIN_TIER1)
          & (populated >= QUORUM_MIN_BLOCKS))
    return pd.DataFrame({
        "quorum_ok": ok, "n_assets": n_assets, "n_tier1": n_tier1,
        "n_blocks": (block_counts > 0).sum(axis=1), "n_blocks_populated": populated,
    })


def basket_median(panel: pd.DataFrame, basket: Basket) -> pd.Series:
    """M_t - the weighted median return of the basket (§2.3)."""
    weights = basket.weights()
    columns = [c for c in panel.columns if c in weights]
    w = np.array([weights[c] for c in columns])
    values = panel[columns].to_numpy(dtype="float64")

    out = np.full(len(panel), np.nan)
    for i in range(len(panel)):
        mask = np.isfinite(values[i])
        if mask.any():
            out[i] = weighted_median(values[i][mask], w[mask])
    return pd.Series(out, index=panel.index)


def block_factors(panel: pd.DataFrame, basket: Basket,
                  sigma_panel: pd.DataFrame | None = None) -> pd.DataFrame:
    """The own-block factor for each instrument, EXCLUDING the instrument itself.

    THIS IS NOW THE ONLY REGRESSOR. It used to be the second of two, beside a
    weighted median of the whole basket; that one is gone, because a median
    cannot carry a factor whose members respond with opposite signs and the
    equity-rates sign is not even stable across the record. See basket.yaml.

    Why a block factor works where a basket factor did not is the same argument
    read forwards: in hours when four or more currency pairs fire, 97% of the
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
    the plain median of raw returns it used to be. A sigma that is missing, zero
    or negative becomes NaN rather than one: dividing by a scale we do not have
    would put that member into the median at its raw size, which is the very
    thing standardising is meant to stop.
    """
    if sigma_panel is None:
        return np.ones((len(index), len(columns)), dtype="float64")
    frame = sigma_panel.reindex(index=index, columns=columns)
    scale = frame.to_numpy(dtype="float64")
    return np.where(np.isfinite(scale) & (scale > 0), scale, np.nan)


def cross_sectional_volatility(panel: pd.DataFrame, sigma_panel: pd.DataFrame,
                               basket: Basket) -> pd.DataFrame:
    """CSV and CSV_norm per §3.2.

    CSV is the dispersion of asset returns WITHIN an hour. It is normalised by
    the mean own volatility of those same assets, otherwise the quantity would
    measure not coherence but simply how turbulent the market is: in a storm the
    dispersion is large for everyone.
    """
    columns = [c for c in panel.columns if c in {a.asset_id for a in basket.assets}]
    values = panel[columns]
    csv = values.std(axis=1, ddof=1)  # ddof=1 per §1.2
    ewma_volatility = sigma_panel[columns].where(values.notna()).mean(axis=1)
    return pd.DataFrame({
        "csv": csv,
        "ewma_volatility": ewma_volatility,
        "csv_norm": csv / ewma_volatility,
    })


def csv_compression(csv_norm: pd.Series, m: pd.Series,
                    window: int = windows.W_CS) -> pd.Series:
    """The compression sub-condition exactly as §3.2 writes it.

    Kept, computed and logged, but no longer part of the single-factor trigger:
    on this basket it fires in ZERO hours out of 29,532, and that is structural
    rather than unlucky. Its two halves are opposites. "CSV below its own 10th
    percentile" means the assets barely moved, because that percentile is set by
    quiet hours; "|M_t| above two sigma" means they moved a great deal. The
    condition asks for an hour that is simultaneously violent and becalmed.

    It is left in place because §3.4 requires both sub-conditions to be logged
    separately, and because an empty column is itself the evidence. What replaced
    it is `coherence` below. See docs/decisions.md.
    """
    rolling = csv_norm.shift(1).rolling(window, min_periods=window)
    q10 = rolling.quantile(0.10)
    m_std = m.shift(1).rolling(window, min_periods=window).std(ddof=1)
    result = (csv_norm < q10) & (m.abs() > 2 * m_std)
    return (result.where(q10.notna() & m_std.notna(), pd.NA).astype("boolean"), q10)


def coherence(panel: pd.DataFrame, quorum_ok: pd.Series) -> pd.Series:
    """How far the basket moved AS ONE THING in this hour.

    The cross-sectional mean of the Z-scores divided by their cross-sectional
    spread - a signal-to-noise ratio across assets. Every asset at +2 sigma gives
    a large mean over a small spread and a high value; unrelated wobble gives a
    mean near zero and a low one.

    On Z-scores rather than returns, and that is the point. The basket's assets
    differ in scale by a factor of forty - Solana moves 49 basis points in a
    typical hour where SHY moves one - so a spread taken on raw returns is
    dominated by whichever crypto asset is loudest: measured, the CSV of §3.2
    correlates 0.904 with Solana's own |r|. It is a Solana volatility gauge
    wearing the name of a cross-sectional statistic. On Z-scores that correlation
    falls to 0.365, because each asset is first expressed in units of its own
    normal.
    """
    values = panel.where(quorum_ok.reindex(panel.index, fill_value=False), np.nan)
    spread = values.std(axis=1, ddof=1)
    return (values.mean(axis=1).abs() / spread).where(spread > 0)


def coherence_compression(coherence_series: pd.Series, m: pd.Series,
                          window: int = windows.W_CS) -> tuple[pd.Series, pd.Series]:
    """The single-factor sub-condition that replaces §3.2's compression.

    Same second leg as the spec - the basket must actually have shifted, since
    assets agreeing on nothing much is not an event - and the same shape: an
    adaptive percentile of the quantity's own recent history, so it tracks the
    regime rather than a fixed number. Only the first leg changes, from "the
    dispersion is unusually narrow" to "the agreement is unusually strong".
    """
    threshold = (coherence_series.shift(1).rolling(window, min_periods=window)
                 .quantile(windows.COHERENCE_QUANTILE))
    m_std = m.shift(1).rolling(window, min_periods=window).std(ddof=1)
    result = (coherence_series > threshold) & (m.abs() > 2 * m_std)
    return (result.where(threshold.notna() & m_std.notna(), pd.NA).astype("boolean"),
            threshold)


def cluster_sigma_m(m: pd.Series, window: int) -> pd.Series:
    """sigma_M on the §5.2 definition, needed here before cluster runs."""
    return m.shift(1).rolling(window, min_periods=window).std(ddof=1)


def sustained_move(m: pd.Series, sigma_m: pd.Series,
                   horizon: int = windows.SUSTAINED_WINDOW,
                   window: int = windows.W_CS) -> tuple[pd.Series, pd.Series]:
    """The basket's move accumulated over the trailing `horizon` hours, in sigmas.

    Every trigger of §4.2 is a one-hour statistic, and §7 asks what the market
    does over the following twenty-four. Volatility clusters, so a one-hour shock
    carries some information about the day ahead - but it is the wrong instrument
    for the question, and measurably so: the same basket move read over 24
    reference hours reaches 80% precision on train where its one-hour form
    reaches 25%.

    Scaled by sigma * sqrt(horizon) rather than by sigma, because summing
    independent hourly moves grows the standard deviation by the square root of
    their number. Without it the quantity would drift with the horizon rather
    than staying comparable across it.

    Returns the series and its adaptive threshold, on the pattern of every other
    threshold here: a percentile of the quantity's own recent history, excluding
    the current hour.
    """
    accumulated = m.rolling(horizon, min_periods=horizon // 2).sum().abs()
    scaled = accumulated / (sigma_m * np.sqrt(horizon))
    threshold = (scaled.shift(1).rolling(window, min_periods=window)
                 .quantile(windows.SUSTAINED_QUANTILE))
    return scaled, threshold


def full_basket_regime(quorum_frame: pd.DataFrame, basket: Basket) -> pd.Series:
    """Hours in which the WHOLE basket trades - every block is represented.

    The basket has two regimes, a consequence of its composition: ETFs trade for
    6.5 hours, currency pairs 24/5, crypto around the clock. During the US session
    all five blocks are working; the other seventeen hours of the day only the
    currency pairs and crypto are.
    """
    return quorum_frame["n_blocks_populated"] >= len(basket.by_block())


def pc1_ratio(panel: pd.DataFrame, quorum_ok: pd.Series, basket: Basket,
              window: int = windows.W_PCA,
              regime: pd.Series | None = None) -> pd.Series:
    """Share of variance explained by the first principal component (§3.3).

    Computed on the CORRELATION matrix, not the covariance matrix: otherwise the
    most volatile asset alone would define the first component, and the quantity
    would measure its amplitude rather than how common the movement is.

    Only assets with a valid bar in ALL included hours enter the window: an
    incomplete column would make pairwise correlations incomparable, each computed
    on a different subset of time.

    From this follows a limitation §3.3 does not anticipate, because it assumes
    every asset shares one session. We have two. Any 120-hour window touches the
    night, when the ETFs are closed, so the completeness requirement throws ALL
    the ETFs out of the matrix - verified on real data: what remains is exactly
    six currency pairs and three crypto assets, in every single window. The
    synchrony of the equity, rates and commodities blocks would never be measured,
    although the cluster detector exists for precisely that.

    So the window is assembled from hours of ONE regime - those in which the whole
    basket trades. Then every asset is complete, the correlations are comparable,
    and PC1_ratio measures what it should: how common the movement is across all
    blocks. At night the value stays NULL, and per §3.4 the single-factor trigger
    rests on compression alone - a case the specification addresses directly.

    Mixing regimes in one series would be worse than not computing it at all: the
    synchrony threshold is a rolling percentile of PC1_ratio itself, and on a
    series alternating between two different typical levels it would describe the
    proportion of regimes rather than an anomaly.
    """
    columns = [c for c in panel.columns if c in {a.asset_id for a in basket.assets}]
    values = panel[columns]
    mask = quorum_ok.reindex(values.index, fill_value=False)
    if regime is not None:
        mask = mask & regime.reindex(values.index, fill_value=False)
    eligible = values[mask]

    out = pd.Series(np.nan, index=panel.index)
    # The mean pairwise correlation (§6.5) is computed right here: the
    # correlation matrix for it has already been built, and a separate pass over
    # the same windows would cost as much as the whole PCA.
    mean_corr = pd.Series(np.nan, index=panel.index)
    positions = {h: i for i, h in enumerate(eligible.index)}
    matrix = eligible.to_numpy(dtype="float64")

    for hour in panel.index:
        end = positions.get(hour)
        if end is None or end + 1 < window:
            continue
        block = matrix[end + 1 - window: end + 1]
        complete = ~np.isnan(block).any(axis=0)
        block = block[:, complete]
        if block.shape[1] < PCA_MIN_ASSETS:
            continue

        std = block.std(axis=0, ddof=1)
        block = block[:, std > CONSTANT_COLUMN_STD]
        n_assets = block.shape[1]
        if n_assets < PCA_MIN_ASSETS:
            continue
        if block.shape[0] < max(PCA_ROWS_PER_ASSET * n_assets, PCA_MIN_ROWS):
            continue

        correlation = np.corrcoef(block, rowvar=False)
        eigenvalues = np.linalg.eigvalsh(correlation)
        total = eigenvalues.sum()
        if total > 0:
            out[hour] = float(eigenvalues[-1] / total)
        # Arithmetic mean of the elements OFF the main diagonal. The diagonal is
        # an asset's correlation with itself, one by construction, and including
        # it would simply pull the mean upward, the more so the fewer assets are
        # in the window.
        off_diagonal = correlation[~np.eye(n_assets, dtype=bool)]
        if off_diagonal.size:
            mean_corr[hour] = float(np.nanmean(off_diagonal))
    return out, mean_corr


def pca_sync(ratio: pd.Series, window: int = windows.W_CS) -> pd.Series:
    """The synchrony sub-condition (§3.3). The threshold is the maximum of the
    percentile and the median plus a margin: the percentile alone is not enough,
    because in a prolonged period of high connectedness it drifts upward and stops
    cutting anything off."""
    rolling = ratio.shift(1).rolling(window, min_periods=PCA_STAT_MIN_OBS)
    threshold = np.maximum(rolling.quantile(0.95), rolling.median() + PCA_SYNC_MARGIN)
    result = ratio > threshold
    return (result.where(threshold.notna() & ratio.notna(), pd.NA).astype("boolean"),
            threshold)


def single_factor(compression: pd.Series, sync: pd.Series) -> pd.Series:
    """The single-factor trigger (§3.4): compression OR synchrony.

    If PC1_ratio was not assessed, synchrony is logged as NULL and the trigger's
    value equals compression alone - per §3.4 an hour that passed quorum must
    receive a definite value, or it contributes no term to the SI-Index.
    """
    filled_sync = sync.fillna(False).astype(bool)
    result = compression.fillna(False).astype(bool) | filled_sync
    # NULL remains only where NEITHER sub-condition was assessed.
    unknown = compression.isna() & sync.isna()
    return result.where(~unknown, pd.NA).astype("boolean")


def build_basket_metrics(metrics: dict[str, pd.DataFrame], basket: Basket,
                         reference_hours: pd.Index | None = None) -> pd.DataFrame:
    """Assembles metrics_basket_hour (§6.4): quorum, M_t, CSV, PCA and the
    single-factor trigger on a shared hourly grid."""
    panel = build_panel(metrics, "r")
    sigma_panel = build_panel(metrics, "sigma_eff")
    if reference_hours is not None:
        panel = panel.loc[panel.index.intersection(reference_hours)]
        sigma_panel = sigma_panel.reindex(panel.index)

    quorum_frame = quorum(panel, basket)
    ok = quorum_frame["quorum_ok"]
    m = basket_median(panel, basket)
    csv_frame = cross_sectional_volatility(panel, sigma_panel, basket)

    regime = full_basket_regime(quorum_frame, basket)
    ratio, mean_corr = pc1_ratio(panel, ok, basket, regime=regime)

    # An hour without quorum is not assessed at all: per §2.3 every cluster
    # trigger gets NULL, not False.
    compression, compression_threshold = csv_compression(csv_frame["csv_norm"], m)
    compression = compression.where(ok, pd.NA)
    # Basket assets only: an instrument outside the basket has a block (§8.1) but
    # takes no part in any basket aggregate.
    in_basket = {a.asset_id for a in basket.assets}
    z_panel = build_panel({aid: frame for aid, frame in metrics.items()
                           if aid in in_basket}, "z").reindex(panel.index)
    coherence_series = coherence(z_panel, ok)
    agreement, agreement_threshold = coherence_compression(coherence_series, m)
    agreement = agreement.where(ok, pd.NA)
    sync, sync_threshold = pca_sync(ratio)
    sync = sync.where(ok, pd.NA)

    out = pd.concat([quorum_frame, csv_frame], axis=1)
    out["m_weighted_median"] = m
    out["pc1_ratio"] = ratio
    out["pc1_threshold"] = sync_threshold
    out["mean_pairwise_corr"] = mean_corr
    out["in_full_regime"] = regime
    out["csv_norm_q10"] = compression_threshold
    out["csv_compression"] = compression.astype("boolean")
    sustained, sustained_threshold = sustained_move(
        m, cluster_sigma_m(m, windows.W_CS))
    out["sustained"] = sustained
    out["sustained_threshold"] = sustained_threshold
    out["trigger_sustained"] = ((sustained > sustained_threshold)
                                .where(ok & sustained_threshold.notna(), pd.NA)
                                .astype("boolean"))
    out["coherence"] = coherence_series
    out["coherence_threshold"] = agreement_threshold
    out["basket_coherence"] = agreement.astype("boolean")
    out["pca_sync"] = sync.astype("boolean")
    out["single_factor"] = single_factor(agreement, sync).where(ok, pd.NA).astype("boolean")
    return out


def subcondition_correlation(frame: pd.DataFrame) -> float:
    """Correlation between the firings of compression and synchrony (§3.4).

    The check is mandatory in the backtest: if the sub-conditions almost always
    fire together, the second adds no information and merely doubles the weight of
    one and the same observation in the SI-Index. Per the spec, above 0.7 one of
    them is dropped from the production configuration.

    Computed only over hours where BOTH were assessed - where PC1_ratio is
    undefined there is nothing to compare against.
    """
    first = "basket_coherence" if "basket_coherence" in frame else "csv_compression"
    both = frame[[first, "pca_sync"]].dropna()
    if both.empty or both.nunique().min() < 2:
        return float("nan")
    return float(both[first].astype(float).corr(both["pca_sync"].astype(float)))


DEFAULT_BASKET_METRICS_PATH = os.path.join("data", "tremor", "metrics_basket_hour.parquet")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging

    from tremor import pipeline, sessions, versioning
    from tremor.basket import load_basket

    parser = argparse.ArgumentParser(description="Hourly basket metrics (§3.2-3.4)")
    parser.add_argument("--metrics-dir", default=pipeline.DEFAULT_METRICS_DIR)
    parser.add_argument("--out", default=DEFAULT_BASKET_METRICS_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("tremor.cross_section")

    basket = load_basket()
    metrics = pipeline.load_all(basket, args.metrics_dir)
    if not metrics:
        log.error("No per-asset metrics - run python -m tremor.pipeline first")
        return 2

    panel_hours = build_panel(metrics, "r").index
    reference = pd.Index([h for h in panel_hours
                          if sessions.is_reference_hour(int(h), basket.anchor_exchange_tz)])
    frame = build_basket_metrics(metrics, basket, reference)

    # §6.3: the versions go into the metrics as well. cluster then rewrites this
    # same file with its own derived columns and re-stamps it - whoever wrote the
    # file last is who the stamp has to describe.
    config, run_id = versioning.versions_for()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    versioning.stamp(frame.reset_index(names="hour_utc"), config, run_id).to_parquet(
        args.out, index=False, compression="zstd")

    correlation = subcondition_correlation(frame)
    log.info("hours %d, quorum %d, compression %d, synchrony %d, single-factor %d",
             len(frame), int(frame["quorum_ok"].sum()),
             int(frame["csv_compression"].sum()), int(frame["pca_sync"].sum()),
             int(frame["single_factor"].sum()))
    log.info("sub-condition correlation (§3.4): %s",
             f"{correlation:.4f}" if correlation == correlation
             else "undefined - one of the sub-conditions never fired")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

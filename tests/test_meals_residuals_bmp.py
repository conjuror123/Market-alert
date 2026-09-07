"""The cross-sectional standardisation: the scale, its degrees of freedom, and
why the ratio has to be put on a common scale before a ladder reads it."""
import numpy as np
import pandas as pd
import pytest

from meals import residuals


def _panel(rows):
    return pd.DataFrame(rows, index=[3600 * (i + 1) for i in range(len(rows))])


def test_the_scale_leaves_the_asset_out_of_its_own_denominator():
    # A genuine single-asset move would otherwise inflate the very spread it is
    # measured against, and the asset would hide itself behind its own move.
    panel = _panel([{"a": 100.0, "b": 1.0, "c": -1.0, "d": 1.0, "e": -1.0}])
    scale, _ = residuals.cross_sectional_scale(panel, minimum=5)
    peers = np.array([1.0, -1.0, 1.0, -1.0])
    assert scale.loc[3600, "a"] == pytest.approx(peers.std(ddof=1))


def test_the_scale_reports_the_degrees_of_freedom_it_was_estimated_with():
    panel = _panel([{"a": 1.0, "b": 2.0, "c": -1.0, "d": 1.0, "e": -2.0, "f": 0.5}])
    _, dof = residuals.cross_sectional_scale(panel, minimum=5)
    # six assets present, five peers after leave-one-out, four degrees of freedom
    assert dof.loc[3600, "a"] == pytest.approx(4.0)


def test_a_thin_hour_is_not_assessed_at_all():
    panel = _panel([{"a": 1.0, "b": 2.0, "c": np.nan, "d": np.nan, "e": np.nan}])
    scale, dof = residuals.cross_sectional_scale(panel, minimum=5)
    assert np.isnan(scale.loc[3600, "a"]) and np.isnan(dof.loc[3600, "a"])


def test_the_transform_keeps_sign_and_fixes_zero():
    assert residuals.normalise_t(np.array([0.0]), np.array([10.0]))[0] == 0.0
    assert residuals.normalise_t(np.array([-3.0]), np.array([10.0]))[0] < 0
    assert residuals.normalise_t(np.array([3.0]), np.array([10.0]))[0] > 0


def test_the_transform_is_monotone_in_the_ratio():
    v = np.full(5, 8.0)
    out = residuals.normalise_t(np.array([0.5, 1.0, 2.0, 5.0, 20.0]), v)
    assert np.all(np.diff(out) > 0)


def test_the_same_ratio_means_more_when_more_peers_measured_it():
    # The heart of it: a ratio of 11.5 against five peers is an ordinary noisy
    # denominator; against twenty-two it is an extraordinary move. Before the
    # transform both were the number 11.5 and the ladder could not tell them
    # apart.
    thin, thick = residuals.normalise_t(np.array([11.5, 11.5]), np.array([5.0, 22.0]))
    assert thick > thin


def test_it_makes_the_tail_comparable_across_degrees_of_freedom():
    # The defect, reproduced: draw genuine t-variates with different degrees of
    # freedom - which is exactly what dividing by a peer standard deviation
    # produces - and compare the far tail before and after.
    rng = np.random.default_rng(11)
    n = 400_000
    tails_raw, tails_norm = [], []
    for dof in (4.0, 10.0, 22.0):
        t = rng.standard_t(dof, n)
        tails_raw.append(np.quantile(np.abs(t), 0.999))
        tails_norm.append(np.quantile(
            np.abs(residuals.normalise_t(t, np.full(n, dof))), 0.999))

    raw_spread = max(tails_raw) / min(tails_raw)
    norm_spread = max(tails_norm) / min(tails_norm)
    assert raw_spread > 1.7      # measured on the store: 2.03x
    assert norm_spread < 1.15    # measured on the store: 1.08x


def test_a_floor_on_the_denominator_would_not_have_done_this():
    # The rejected fix, stated as a test so the reasoning survives. Clipping the
    # scale caps the thin hours and leaves every other hour's calibration
    # untouched, so the tails stay incomparable.
    rng = np.random.default_rng(12)
    n = 200_000
    tails = []
    for dof in (4.0, 22.0):
        z = rng.standard_normal(n)
        s = np.sqrt(rng.chisquare(dof, n) / dof)
        floored = z / np.maximum(s, 0.25)
        tails.append(np.quantile(np.abs(floored), 0.999))
    assert max(tails) / min(tails) > 1.5


def test_standardise_wires_the_dof_through_to_the_score():
    frames = {}
    hours = [3600 * (i + 1) for i in range(3)]
    rng = np.random.default_rng(5)
    for name in "abcdef":
        frames[name] = pd.DataFrame({"hour_utc": hours,
                                     "z_resid": rng.standard_normal(3)})
    out = residuals.standardise_cross_section(frames, minimum=5)
    frame = out["a"]
    for column in ("bmp_scale", "bmp_dof", "t_resid", "z_resid_bmp"):
        assert column in frame
    good = frame["t_resid"].notna()
    assert np.allclose(
        frame.loc[good, "z_resid_bmp"],
        residuals.normalise_t(frame.loc[good, "t_resid"].to_numpy(),
                              frame.loc[good, "bmp_dof"].to_numpy()))


def test_an_asset_with_no_peers_in_the_panel_is_null_not_zero():
    frames = {"a": pd.DataFrame({"hour_utc": [3600], "z_resid": [2.0]})}
    out = residuals.standardise_cross_section(frames, minimum=5)
    assert np.isnan(out["a"]["z_resid_bmp"].iloc[0])


# --- Patell's prediction-error inflation -----------------------------------

def _est(n=200, f_mean=0.0, f_var=1e-4, index=None):
    index = range(3) if index is None else index
    return pd.DataFrame({"n_est": float(n), "f_mean": f_mean, "f_var": f_var},
                        index=index)


def test_the_inflation_is_never_below_one():
    # A forecast error cannot be tighter than the in-sample residual it is
    # measured against.
    factor = pd.Series([0.0, 0.0, 0.0])
    out = residuals.patell_scale(_est(), factor)
    assert (out >= 1.0).all()


def test_a_factor_at_its_average_costs_only_the_estimation_term():
    # At the window's centre the leverage vanishes and all that is left is
    # 1 + 1/L, the cost of having estimated the mean at all.
    factor = pd.Series([0.0, 0.0, 0.0])
    out = residuals.patell_scale(_est(n=200), factor)
    assert out.iloc[0] == pytest.approx(np.sqrt(1 + 1 / 200), rel=1e-9)


def test_an_extreme_factor_value_inflates_the_scale():
    # The term the plan says matters most: on a violent hour the factor is far
    # from its estimation-window average, the beta extrapolates, and the
    # residual is genuinely noisier than the in-sample sigma suggests.
    quiet = residuals.patell_scale(_est(), pd.Series([0.0, 0.0, 0.0]))
    wild = residuals.patell_scale(_est(), pd.Series([0.10, 0.10, 0.10]))
    assert wild.iloc[0] > quiet.iloc[0]


def test_the_inflation_matches_the_textbook_formula():
    n, f_var, value = 250.0, 4e-4, 0.05
    out = residuals.patell_scale(_est(n=n, f_var=f_var), pd.Series([value] * 3))
    expected = np.sqrt(1 + 1 / n + value ** 2 / ((n - 1) * f_var))
    assert out.iloc[0] == pytest.approx(expected, rel=1e-12)


def test_two_factors_use_the_full_quadratic_form_not_a_sum():
    # A block factor is part of the basket, so the two are correlated; adding
    # two one-factor terms would understate the leverage exactly when they move
    # together, which is the case of interest.
    index = range(1)
    est = pd.DataFrame({"n_est": 200.0, "f_mean": 0.0, "b_mean": 0.0,
                        "f_var": 1e-4, "b_var": 1e-4, "fb_cov": 9e-5},
                       index=index)
    f = pd.Series([0.02], index=index)
    b = pd.Series([0.02], index=index)
    full = residuals.patell_scale(est, f, b).iloc[0]

    naive = np.sqrt(1 + 1 / 200 + 0.02 ** 2 / (199 * 1e-4) * 2)
    assert full != pytest.approx(naive)
    assert full > 1.0


def test_a_collinear_window_falls_back_to_the_basket_factor_alone():
    # Same fallback the regression itself makes when the determinant collapses.
    index = range(1)
    est = pd.DataFrame({"n_est": 200.0, "f_mean": 0.0, "b_mean": 0.0,
                        "f_var": 1e-4, "b_var": 1e-4, "fb_cov": 1e-4},
                       index=index)
    f = pd.Series([0.02], index=index)
    out = residuals.patell_scale(est, f, pd.Series([0.02], index=index)).iloc[0]
    expected = np.sqrt(1 + 1 / 200 + 0.02 ** 2 / (199 * 1e-4))
    assert out == pytest.approx(expected, rel=1e-9)


def test_a_frame_without_the_moments_is_left_alone():
    # Older residual frames carry no n_est; they must not blow up, and an
    # uncorrected scale of one is the honest fallback.
    factor = pd.Series([0.01, 0.02])
    out = residuals.patell_scale(pd.DataFrame(index=factor.index), factor)
    assert list(out) == [1.0, 1.0]


# --- Corrado's rank test, the non-parametric third opinion ------------------

def test_the_most_extreme_bar_in_the_window_confirms():
    rng = np.random.default_rng(3)
    e = pd.Series(rng.standard_normal(1500) * 0.01)
    e.iloc[1200] = 5.0
    out = residuals.rank_statistic(e, window=500, minimum=200)
    assert bool(out["rank_confirms"].iloc[1200])
    assert out["rank_pct"].iloc[1200] == pytest.approx(1.0)


def test_the_statistic_saturates_which_is_why_it_cannot_be_a_tier():
    # The most extreme bar and a merely very extreme one score almost the same,
    # so the test can confirm that a bar is exceptional and never say by how
    # much. A return-period ladder fed by it would be meaningless.
    rng = np.random.default_rng(4)
    e = pd.Series(rng.standard_normal(1500) * 0.01)
    e.iloc[1200] = 5.0
    e.iloc[1300] = 500.0
    out = residuals.rank_statistic(e, window=500, minimum=200)
    big, enormous = out["t_rank"].iloc[1200], out["t_rank"].iloc[1300]
    assert enormous == pytest.approx(big, abs=0.01)
    assert enormous <= 1.73


def test_an_ordinary_bar_does_not_confirm():
    rng = np.random.default_rng(5)
    e = pd.Series(rng.standard_normal(1500) * 0.01)
    out = residuals.rank_statistic(e, window=500, minimum=200)
    rate = out["rank_confirms"].dropna().mean()
    # a top-one-percent rule should confirm about one percent of bars
    assert 0.005 < rate < 0.02


def test_it_is_immune_to_an_outlier_that_would_move_a_parametric_scale():
    # One absurd bar drags a standard deviation and with it every Z in the
    # window. A rank moves by one place.
    rng = np.random.default_rng(6)
    e = pd.Series(rng.standard_normal(1500) * 0.01)
    clean = residuals.rank_statistic(e, window=500, minimum=200)
    dirty_series = e.copy()
    dirty_series.iloc[1100] = 1000.0
    dirty = residuals.rank_statistic(dirty_series, window=500, minimum=200)
    later = slice(1200, 1500)
    assert (clean["t_rank"][later] - dirty["t_rank"][later]).abs().max() < 0.02


def test_it_ranks_magnitude_so_both_directions_can_confirm():
    rng = np.random.default_rng(8)
    e = pd.Series(rng.standard_normal(1500) * 0.01)
    e.iloc[1200] = -5.0
    out = residuals.rank_statistic(e, window=500, minimum=200)
    assert bool(out["rank_confirms"].iloc[1200])


def test_a_window_that_has_not_filled_says_nothing_rather_than_no():
    e = pd.Series(np.random.default_rng(9).standard_normal(300) * 0.01)
    out = residuals.rank_statistic(e, window=500, minimum=200)
    assert out["rank_confirms"].iloc[:199].isna().all()


# --- the Ornstein-Uhlenbeck residual and its reversion filter ---------------

def test_a_noise_residual_reverts_and_a_drifting_one_does_not():
    # The distinction the filter exists to make: noise returns to its
    # equilibrium, an unmodelled factor walks away from it.
    rng = np.random.default_rng(1)
    noise = pd.Series(rng.standard_normal(2000) * 0.01)
    drift = pd.Series(rng.standard_normal(2000) * 0.01 + 0.004)

    a = residuals.ou_fit(noise, window=500, minimum=200).iloc[600:]
    b = residuals.ou_fit(drift, window=500, minimum=200).iloc[600:]
    assert a["ou_reverts"].mean() > 0.5
    assert b["ou_reverts"].mean() < 0.05
    assert b["ou_reversion_bars"].median() > a["ou_reversion_bars"].median()


def test_a_drift_produces_a_huge_s_score_which_is_why_speed_is_checked():
    # The trap: a residual walking away from equilibrium looks extraordinary on
    # the s-score alone. Only the reversion speed says it is a missing factor
    # rather than an idiosyncratic move.
    rng = np.random.default_rng(2)
    drift = pd.Series(rng.standard_normal(2000) * 0.01 + 0.004)
    out = residuals.ou_fit(drift, window=500, minimum=200).iloc[600:]
    assert out["s_score"].abs().median() > 5
    assert not out["ou_reverts"].any()


def test_the_fit_is_invariant_to_where_the_cumulative_sum_starts():
    # Avellaneda and Lee restart the sum at each window; the slope, the
    # reversion speed and the s-score do not depend on that, which is what
    # lets the fit run vectorised on a global cumulative sum.
    rng = np.random.default_rng(3)
    e = pd.Series(rng.standard_normal(1200) * 0.01)
    base = residuals.ou_fit(e, window=400, minimum=200)
    shifted = residuals.ou_fit(e, window=400, minimum=200)
    # shifting the residual's level by a constant is the same as restarting the
    # sum somewhere else: the first residual absorbs the offset
    e2 = e.copy(); e2.iloc[0] += 7.5
    moved = residuals.ou_fit(e2, window=400, minimum=200)
    late = slice(600, 1200)
    pd.testing.assert_series_equal(base["ou_b"][late], moved["ou_b"][late])
    pd.testing.assert_series_equal(base["s_score"][late], moved["s_score"][late],
                                   rtol=1e-6)
    pd.testing.assert_series_equal(base["ou_b"][late], shifted["ou_b"][late])


def test_the_parameters_come_from_before_the_bar_they_judge():
    from meals import windows
    rng = np.random.default_rng(4)
    e = pd.Series(rng.standard_normal(1500) * 0.01)
    clean = residuals.ou_fit(e, window=500, minimum=200)
    spiked = e.copy(); spiked.iloc[1000] = 3.0
    dirty = residuals.ou_fit(spiked, window=500, minimum=200)
    for offset in range(windows.REGRESSION_GAP_BARS):
        i = 1000 + offset
        assert clean["ou_b"].iloc[i] == pytest.approx(dirty["ou_b"].iloc[i]), offset


def test_a_non_reverting_window_says_nothing_rather_than_no():
    # b outside (0, 1) is not "does not revert", it is a fit that did not
    # describe an OU process at all.
    e = pd.Series(np.zeros(800))
    out = residuals.ou_fit(e, window=400, minimum=200)
    assert out["s_score"].isna().all()

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

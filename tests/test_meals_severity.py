import numpy as np
import pandas as pd

from meals import severity as sv

HOUR = 3600


def gpd_sample(rng, n, shape, scale=2.0):
    u = rng.random(n)
    if shape == 0:
        return -scale * np.log(1 - u)
    return (scale / shape) * ((1 - u) ** (-shape) - 1)


def test_pwm_recovers_the_generalised_pareto_parameters():
    # The estimator is a closed form of the first two probability-weighted
    # moments (Hosking & Wallis 1987), so this is a direct check of the algebra
    # rather than of an optimiser's luck.
    rng = np.random.default_rng(7)
    for true_shape in (0.0, 0.1, 0.3, -0.2):
        fits = np.array([sv.fit_gpd(gpd_sample(rng, 2000, true_shape))
                         for _ in range(30)])
        assert abs(fits[:, 0].mean() - true_shape) < 0.03
        assert abs(fits[:, 1].mean() - 2.0) < 0.1


def test_the_exponential_case_comes_out_at_shape_zero():
    rng = np.random.default_rng(1)
    shape, scale = sv.fit_gpd(rng.exponential(3.0, 20000))
    assert abs(shape) < 0.02
    assert abs(scale - 3.0) < 0.1


def test_a_degenerate_sample_falls_back_to_the_exponential():
    # Not a hypothetical: an instrument halted for a stretch produces a tail of
    # identical values, and the PWM denominator goes to zero on it.
    # The shape saturates at the clip, and the scale must stay consistent with
    # it: a shape of -0.5 paired with the unclipped scale extrapolates a level
    # nothing ever reaches, which silences the instrument instead of failing.
    shape, scale = sv.fit_gpd(np.full(500, 4.0))
    assert shape == sv.SHAPE_MIN
    assert 0 < scale < 10 * 4.0
    assert sv.fit_gpd(np.array([])) == (0.0, 0.0)
    assert sv.fit_gpd(np.zeros(500)) == (0.0, 1e-12)


def test_the_shape_is_clipped_before_it_is_extrapolated():
    # A shape above 1 is a distribution with no finite mean. Extrapolated three
    # years out it produces a level nothing will ever reach, which silences the
    # instrument completely rather than failing loudly - so it is clipped.
    rng = np.random.default_rng(5)
    shape, _ = sv.fit_gpd(gpd_sample(rng, 300, 0.9))
    assert shape <= sv.SHAPE_MAX


def test_return_levels_track_a_known_fat_tail():
    # Student-t(4) is a harder case than the GPD itself - t only approaches
    # Pareto asymptotically - so it is the honest test of the extrapolation.
    rng = np.random.default_rng(3)
    sample = np.abs(rng.standard_t(4, 40000))
    truth = np.abs(rng.standard_t(4, 4_000_000))
    for days in sv.TIER_DAYS.values():
        m = days * sv.HOURS_PER_DAY
        got = sv.return_level(sample, m)
        expected = float(np.quantile(truth, 1 - 1 / m))
        assert abs(got - expected) / expected < 0.15


def test_inside_the_body_the_empirical_quantile_is_used():
    # Where the expected number of exceedances of the POT threshold is above
    # one there is nothing to extrapolate, and hundreds of observations to read
    # off instead. The two regimes have to agree, not just each be defensible.
    rng = np.random.default_rng(9)
    sample = np.abs(rng.standard_normal(50000))
    # Not exact equality: 1 - 1/100 is not 0.99 in binary, so the quantile
    # position differs in the last bits. A GPD extrapolation would miss by
    # orders of magnitude more than this.
    assert abs(sv.return_level(sample, 100) - np.quantile(sample, 0.99)) < 1e-4


def test_levels_do_not_decrease_across_the_ladder():
    # Imposed, not assumed: the routine level comes from an empirical quantile
    # and the rarer ones from a fitted tail, and nothing in either guarantees
    # they arrive in order. A "major" level below "notable" would let a move
    # land in the higher box while failing the lower one.
    rng = np.random.default_rng(2)
    for _ in range(20):
        levels = sv.tier_levels(np.abs(rng.standard_t(3, 4000)), 1.0)
        values = [levels[name] for name in sv.TIERS]
        assert values == sorted(values)


def test_bar_rate_separates_a_session_from_the_clock():
    # This is what turns the ladder, which is in calendar time, into the bar
    # counts the quantiles need. Round-the-clock is 1.0; a seven-hour session
    # five days in seven is about a fifth of that.
    continuous = pd.Series(np.arange(10000) * HOUR)
    assert abs(sv.bar_rate(continuous) - 1.0) < 0.001

    hours = [d * 24 + h for d in range(400) if d % 7 < 5 for h in range(14, 21)]
    assert abs(sv.bar_rate(pd.Series([h * HOUR for h in hours])) - 0.208) < 0.02


def test_rolling_levels_never_look_forward():
    # The reason the fit is expanding rather than full-sample. A full-sample fit
    # would label a 2016 move using the knowledge that 2020 was coming, which
    # makes every backtested tier optimistic and makes the live system behave
    # differently from the tested one.
    rng = np.random.default_rng(4)
    score = pd.Series(rng.standard_t(4, 30000))
    before = sv.rolling_levels(score, 1.0)

    disturbed = score.copy()
    disturbed.iloc[24000:] *= 50            # a violent, entirely future regime
    after = sv.rolling_levels(disturbed, 1.0)

    pd.testing.assert_frame_equal(before.iloc[:24000], after.iloc[:24000])
    assert not before.iloc[24000:].equals(after.iloc[24000:])


def test_nothing_is_assigned_during_the_warm_up():
    rng = np.random.default_rng(6)
    score = pd.Series(rng.standard_t(4, 30000))
    levels = sv.rolling_levels(score, 1.0)
    warmup = int(sv.WARMUP_DAYS * sv.HOURS_PER_DAY)

    assert levels.iloc[:warmup].isna().all().all()
    # Everything the two-year warm-up can back is available the moment it ends:
    # a fortnight, two months and a year are all claims two years of history
    # supports. Three years is not, and waits.
    assert levels.iloc[warmup:][["routine", "notable", "major"]].notna().all().all()


def test_a_tier_is_withheld_until_the_history_can_back_the_claim():
    # "The largest move in three years" cannot be said on two years of data.
    # Fitted at the warm-up boundary it was not merely unsupported but wrong:
    # eight "extreme" events landed in the single month where the boundary fell,
    # and nowhere else in five years.
    rng = np.random.default_rng(21)
    score = pd.Series(rng.standard_t(4, 8766 * 5))
    levels = sv.rolling_levels(score, 1.0)

    first = {name: levels[name].first_valid_index() for name in sv.TIERS}
    warmup = int(sv.WARMUP_DAYS * sv.HOURS_PER_DAY)
    # The three tiers the warm-up already covers arrive together with it; the
    # one it does not covers waits for its own return period to elapse.
    assert first["routine"] == first["notable"] == first["major"] == warmup
    assert first["extreme"] >= sv.TIER_DAYS["extreme"] * sv.HOURS_PER_DAY


def test_the_ladder_grows_a_rung_at_a_time():
    # An instrument that is only two and a half years old has no business
    # calling anything a once-in-three-years move, and says so by leaving the
    # rung empty rather than by lowering it.
    rng = np.random.default_rng(22)
    short = pd.Series(rng.standard_t(4, int(2.5 * 8766)))
    levels = sv.rolling_levels(short, 1.0)
    assert levels["major"].notna().any()
    assert levels["extreme"].isna().all()


def test_a_history_shorter_than_the_warm_up_yields_no_tiers():
    score = pd.Series(np.random.default_rng(8).standard_t(4, 500))
    levels = sv.rolling_levels(score, 1.0)
    assert levels.isna().all().all()
    assert sv.assign(score, levels).isna().all()


def test_assign_picks_the_rarest_level_cleared():
    score = pd.Series([0.5, 1.5, 2.5, 3.5, 4.5, -4.5])
    levels = pd.DataFrame({"routine": 1.0, "notable": 2.0, "major": 3.0,
                           "extreme": 4.0}, index=score.index)
    tier = sv.assign(score, levels)

    assert list(tier[1:]) == ["routine", "notable", "major", "extreme", "extreme"]
    assert pd.isna(tier.iloc[0])
    assert list(sv.rank(tier)[1:]) == [1, 2, 3, 4, 4]


def test_annotate_fires_at_roughly_the_advertised_rate():
    # The ladder is a promise about frequency, so it is worth checking that the
    # machinery keeps it. Loose bounds on purpose - this is one draw, and the
    # point is to catch a rate that is wrong by an order of magnitude.
    rng = np.random.default_rng(12)
    n = 8766 * 6
    frame = pd.DataFrame({"hour_utc": np.arange(n) * HOUR,
                          "z_resid_bmp": rng.standard_t(4, n)})
    out = sv.annotate(frame)
    scored_years = (n - sv.WARMUP_DAYS * sv.HOURS_PER_DAY) / 8766

    per_year = out["tier"].notna().sum() / scored_years
    nominal = sum(365.25 / days for days in sv.TIER_DAYS.values())
    assert nominal / 2.5 < per_year < nominal * 2.5
    assert set(out.columns) >= set(sv.LEVEL_COLUMNS) | {"tier"}


def test_the_same_ladder_can_be_asked_of_a_different_quantity():
    # The column is a parameter because "the market did not explain this" and
    # "this was a big move" are different events and both are wanted. Asked of
    # the raw return, the ladder is what makes the detector able to see a day
    # when everything moves together - which is what a macro event is, and
    # which the residual channel is blind to by construction.
    rng = np.random.default_rng(31)
    n = 8766 * 4
    frame = pd.DataFrame({"hour_utc": np.arange(n) * HOUR,
                          "z_resid_bmp": rng.standard_t(4, n),
                          "raw": rng.standard_t(3, n)})
    out = sv.annotate(frame, column="raw", prefix="abs_level",
                      tier_column="tier_absolute", fallback=None)
    assert set(sv.level_columns("abs_level")) <= set(out.columns)
    assert out["tier_absolute"].notna().any()
    assert "tier" not in out          # the original ladder is untouched


def test_a_missing_column_with_no_fallback_is_an_error_not_a_silent_default():
    frame = pd.DataFrame({"hour_utc": [0, HOUR], "z_resid": [1.0, 2.0]})
    try:
        sv.annotate(frame, column="raw", fallback=None)
    except KeyError:
        return
    raise AssertionError("expected a KeyError")


def test_combine_keeps_the_rarest_tier_and_records_where_it_came_from():
    frame = pd.DataFrame({
        "a": pd.array(["routine", "major", None, "notable"], dtype="string"),
        "b": pd.array(["extreme", None, None, "notable"], dtype="string"),
    })
    out = sv.combine(frame, {"abnormal": "a", "absolute": "b"})
    assert list(out["tier"][:2]) == ["extreme", "major"]
    assert pd.isna(out["tier"].iloc[2])
    # One event, delivered once: a move that was both enormous and unexplained
    # does not become two messages.
    assert list(out["basis"][:2]) == ["both", "abnormal"]
    assert out["basis"].iloc[3] == "both"


def test_combine_needs_at_least_one_of_its_sources():
    try:
        sv.combine(pd.DataFrame({"x": [1]}), {"abnormal": "a", "absolute": "b"})
    except KeyError:
        return
    raise AssertionError("expected a KeyError")


def test_a_one_sided_quantity_is_not_read_through_its_magnitude():
    # A volatility LEVEL is only an event when high; low is the calmest market
    # on record. The detector's forecast is a log volatility running from -8.26
    # to -4.98, and a ladder built on its magnitude ranked the quietest hours
    # as the rarest - an inversion, not a conservative default.
    rng = np.random.default_rng(41)
    n = 8766 * 4
    level = pd.Series(-6.0 + rng.standard_t(4, n) * 0.3)
    frame = pd.DataFrame({"hour_utc": np.arange(n) * HOUR, "level": level})

    one = sv.annotate(frame, column="level", prefix="m", tier_column="tier",
                      fallback=None, two_sided=False)
    fired = one.loc[one["tier"].notna(), "level"]
    assert (fired > level.median()).all()      # only the loud hours

    two = sv.annotate(frame, column="level", prefix="m", tier_column="tier",
                      fallback=None, two_sided=True)
    assert (two.loc[two["tier"].notna(), "level"] < level.median()).all()


def test_magnitudes_passes_a_one_sided_score_through_unchanged():
    score = pd.Series([-3.0, 1.0, 2.0])
    assert list(sv.magnitudes(score, two_sided=False)) == [-3.0, 1.0, 2.0]
    assert list(sv.magnitudes(score, two_sided=True)) == [3.0, 1.0, 2.0]

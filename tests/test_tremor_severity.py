import numpy as np
import pandas as pd
import pytest

from tremor import severity as sv

HOUR = 3600


def hourly(values, start=0, sigma=None):
    """A frame on a round-the-clock hourly grid, which is the simplest clock."""
    frame = pd.DataFrame({"hour_utc": np.arange(len(values)) * HOUR + start,
                          "score": np.asarray(values, dtype=float)})
    if sigma is not None:
        frame["sigma_lt"] = sigma
    return frame


def test_a_level_is_the_threshold_times_the_instruments_own_sigma():
    # The whole rule for a raw quantity: a return is in price units, so it means
    # nothing against a bare number until it is divided by what this instrument
    # usually does in an hour.
    rungs = sv.tier_sigma("equity")
    levels = sv.sigma_levels(pd.Series([0.01] * 5), pd.RangeIndex(5), "equity")
    assert levels["noticeable"].iloc[0] == pytest.approx(rungs["noticeable"] * 0.01)
    assert levels["extreme"].iloc[0] == pytest.approx(rungs["extreme"] * 0.01)


def test_an_already_standardised_score_is_not_divided_again():
    # The BMP residual is a t-statistic. Handing it a sigma too would apply the
    # normalisation twice and make every quiet hour look enormous.
    levels = sv.sigma_levels(None, pd.RangeIndex(3), "equity")
    assert levels["noticeable"].iloc[0] == pytest.approx(sv.tier_sigma("equity")["noticeable"])


def test_levels_do_not_decrease_across_the_ladder():
    for block in list(sv.BLOCK_SIGMA) + ["not-a-block"]:
        rungs = [sv.tier_sigma(block)[name] for name in sv.TIERS]
        assert rungs == sorted(rungs), block


def test_a_bigger_move_can_never_be_given_a_milder_word():
    # The property the rank rule could not offer, and the reason for the switch.
    # Under "the biggest in six years" a move sat in the shadow of any larger one
    # still inside the window: measured on the record, 395 moves LARGER than the
    # typical `extreme` went out as something milder, a 47x move in Bitcoin Cash
    # among them, and the dates were March 2020 and October 2008.
    n = 400
    rising = np.linspace(0.001, 0.30, n)
    f = hourly(rising, sigma=0.01)
    levels = sv.sigma_levels(f["sigma_lt"], f.index, "equity")
    ranks = sv.rank(sv.assign(f["score"], levels)).fillna(0).to_numpy()
    assert (np.diff(ranks) >= 0).all()

    # and the shadow case outright: a huge bar, then a bigger one right after.
    pair = hourly([0.20, 0.30], sigma=0.01)
    tiers = sv.assign(pair["score"], sv.sigma_levels(pair["sigma_lt"], pair.index,
                                                     "equity"))
    assert sv.rank(tiers).iloc[1] >= sv.rank(tiers).iloc[0]


def test_a_block_with_fatter_tails_is_held_to_a_higher_bar():
    # Not a fudge: measured on the archive a flat threshold put crypto at 36-43
    # messages a year and the quiet sector ETFs at about one. Instruments in a
    # block share a return shape, so the block is where that difference belongs.
    assert sv.tier_sigma("crypto")["noticeable"] > sv.tier_sigma("rates")["noticeable"]
    assert sv.tier_sigma("FX")["extreme"] > sv.tier_sigma("agriculture")["extreme"]


def test_an_unlisted_block_falls_back_rather_than_going_silent():
    assert sv.tier_sigma("a block nobody has added yet") == sv.tier_sigma(None)
    assert tuple(sv.tier_sigma(None).values()) == sv.DEFAULT_SIGMA


def test_a_bar_with_no_sigma_yet_takes_no_tier_rather_than_a_guessed_one():
    f = hourly([0.5, 0.5], sigma=[np.nan, 0.01])
    levels = sv.sigma_levels(f["sigma_lt"], f.index, "equity")
    assert np.isnan(levels["noticeable"].iloc[0])
    assert sv.assign(f["score"], levels).isna().iloc[0]


def test_naming_a_divisor_the_frame_does_not_carry_is_an_error():
    # Scoring a raw return against a sigma threshold without dividing would call
    # every bar extreme, and silence is the only worse answer than an exception.
    f = hourly([0.02] * 10)
    with pytest.raises(KeyError, match="sigma_lt"):
        sv.annotate(f, column="score", fallback=None, scale_column="sigma_lt")


def test_assign_picks_the_rarest_level_cleared():
    score = pd.Series([0.5, 1.5, 2.5, 3.5, 4.5, -4.5])
    levels = pd.DataFrame({"noticeable": 1.0, "high": 2.0, "major": 3.0,
                           "extreme": 4.0}, index=score.index)
    tier = sv.assign(score, levels)

    assert list(tier[1:]) == ["noticeable", "high", "major", "extreme", "extreme"]
    assert pd.isna(tier.iloc[0])
    assert list(sv.rank(tier)[1:]) == [1, 2, 3, 4, 4]


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
        "a": pd.array(["noticeable", "major", None, "high"], dtype="string"),
        "b": pd.array(["extreme", None, None, "high"], dtype="string"),
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
    n = 4000
    # A standardised one-sided score, so the rungs apply to it directly: high is
    # an event, low is the calmest market on record.
    level = pd.Series(rng.standard_t(4, n))
    frame = pd.DataFrame({"hour_utc": np.arange(n) * HOUR, "level": level})

    one = sv.annotate(frame, column="level", prefix="m", tier_column="tier",
                      fallback=None, two_sided=False)
    fired = one.loc[one["tier"].notna(), "level"]
    assert len(fired) and (fired > 0).all()      # only the loud hours

    two = sv.annotate(frame, column="level", prefix="m", tier_column="tier",
                      fallback=None, two_sided=True)
    both = two.loc[two["tier"].notna(), "level"]
    assert (both < 0).any()                      # the magnitude reads both ends


def test_magnitudes_passes_a_one_sided_score_through_unchanged():
    score = pd.Series([-3.0, 1.0, 2.0])
    assert list(sv.magnitudes(score, two_sided=False)) == [-3.0, 1.0, 2.0]
    assert list(sv.magnitudes(score, two_sided=True)) == [3.0, 1.0, 2.0]


def test_record_since_names_the_bar_the_move_actually_beat():
    # This is the message. The tier says which rung was cleared; this says what
    # the move was bigger than, and the difference between the two hours is how
    # far back the reader has to think.
    values = np.array([5.0, 1.0, 2.0, 9.0, 3.0])
    f = hourly(values)
    since = sv.record_since(f["score"], f["hour_utc"])
    assert pd.isna(since.iloc[0])              # nothing before it
    assert since.iloc[1] == f["hour_utc"].iloc[0]   # the 5.0
    assert since.iloc[2] == f["hour_utc"].iloc[0]   # still the 5.0, not the 1.0
    assert pd.isna(since.iloc[3])              # 9.0 beats everything on record
    assert since.iloc[4] == f["hour_utc"].iloc[3]   # the 9.0


def test_record_since_is_two_sided_like_the_ladder():
    # A large fall is as much an event as a large rise, so the lookback is over
    # magnitudes - otherwise a crash would be measured against rallies only.
    f = hourly([-8.0, 3.0])
    since = sv.record_since(f["score"], f["hour_utc"])
    assert since.iloc[1] == f["hour_utc"].iloc[0]


def test_the_sensitivity_knob_scales_every_rung_together(tmp_path):
    # One question - how rare before I want to know - so the rungs move as a
    # group. Moving one alone would silently re-rank a move from major to high,
    # which is a different thing from asking for fewer messages.
    import tremor.basket as tb

    cfg = tmp_path / "basket.yaml"
    try:
        for scale in (0.5, 2.0):
            cfg.write_text(f"sensitivity: {scale}\nmin_move_sigma: 1.0\n")
            tb.load_tuning.cache_clear()
            assert tb.load_tuning(str(cfg)).sensitivity == scale
        # A file with no knob in it behaves as 1.0 rather than failing.
        cfg.write_text("history_since: \"2015-01-01\"\n")
        tb.load_tuning.cache_clear()
        assert tb.load_tuning(str(cfg)).sensitivity == 1.0
    finally:
        tb.load_tuning.cache_clear()

    # At the shipped setting the live rungs are exactly the base ones, and they
    # keep their order whatever the scale - and the knob moves every block, not
    # just one, or turning it down would re-rank a move rather than reduce them.
    for block in ("equity", "crypto", None):
        rungs = sv.tier_sigma(block)
        assert list(rungs.values()) == sorted(rungs.values())
    assert tuple(sv.tier_sigma("equity").values()) == sv.BLOCK_SIGMA["equity"]



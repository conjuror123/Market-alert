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
    # None rather than a made-up name for the default row: an unknown block now
    # raises, and the default is reached by naming no block at all.
    for block in list(sv.BLOCK_SIGMA) + [None]:
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


def test_a_block_missing_from_a_ladder_table_raises():
    # It used to take DEFAULT_SIGMA silently, which is the worst shape this
    # failure can have: the levels come out, the tiers come out, every message
    # reads normally, and only a count over twenty years says one complex has
    # been calibrated on another's tail. All three tables are checked, because
    # forgetting exactly one of them is the likely mistake.
    unknown = "a block nobody has added yet"
    for ladder, table in ((sv.MEMBER, "BLOCK_SIGMA"),
                          (sv.RESIDUAL, "BLOCK_RESID_SIGMA"),
                          (sv.BLOCK_OWN, "BLOCK_MOVE_SIGMA")):
        with pytest.raises(KeyError, match=table):
            sv.tier_sigma(unknown, ladder)


def test_naming_no_block_at_all_still_takes_the_default():
    # Distinct from the case above: "no block was named" is a different statement
    # from "a block I have never heard of". Nothing on the live path passes None
    # - saed resolves every asset_id through basket.instruments, which is assets
    # plus outside - so this is for ad-hoc use rather than production.
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




# --- the three ladders, and why they cannot share numbers -------------------

def test_the_three_ladders_are_distinct_tables():
    from tremor import severity as sv

    equity = [sv.tier_sigma("equity", L) for L in (sv.MEMBER, sv.BLOCK_OWN, sv.RESIDUAL)]
    assert len({tuple(d.values()) for d in equity}) == 3


def test_an_unknown_ladder_is_refused_rather_than_defaulted():
    # Silently falling back to the member table is exactly the bug this
    # parameter exists to prevent, and it is invisible in every output.
    from tremor import severity as sv

    with pytest.raises(ValueError):
        sv.tier_sigma("equity", "raw")


def test_the_residual_rungs_sit_far_below_the_member_rungs():
    # NOT a style preference - a fact about the two scores. The member table is
    # calibrated on |r| / sigma_lt, which is a raw return over its own sigma and
    # keeps its fat tail: measured over 2,595,077 bars it reaches 112.9. The
    # residual table is calibrated on z_resid_bmp, a standardised t-statistic
    # whose tail standardising has already removed: it reaches 15.6, and the BMP
    # correction deflates it further exactly when the cross-section is widest.
    #
    # Giving the residual ladder the member numbers made it UNREACHABLE, not
    # strict: seven blocks of nine asked `major` for a value the statistic had
    # never taken. 698 events, one push, over the whole record.
    from tremor import severity as sv

    for block in sv.BLOCK_RESID_SIGMA:
        member = sv.tier_sigma(block, sv.MEMBER)
        resid = sv.tier_sigma(block, sv.RESIDUAL)
        for tier in sv.TIERS:
            assert resid[tier] < member[tier], (block, tier)


def test_every_residual_rung_is_reachable():
    # The observed maximum of |z_resid_bmp| across the whole store. A rung above
    # this is not a high bar, it is a channel that is switched off.
    from tremor import severity as sv

    OBSERVED_MAX = 15.56
    for block, rungs in sv.BLOCK_RESID_SIGMA.items():
        assert max(rungs) < OBSERVED_MAX, block
    assert max(sv.DEFAULT_RESID_SIGMA) < OBSERVED_MAX


def test_every_block_has_rungs_on_every_ladder():
    # A block missing from a table silently takes the default, which is how one
    # complex ends up calibrated for another.
    from tremor import severity as sv

    assert set(sv.BLOCK_RESID_SIGMA) == set(sv.BLOCK_SIGMA) == set(sv.BLOCK_MOVE_SIGMA)


# --- the record book: "biggest since" carried between runs --------------------

def _series(n=3000, seed=4):
    rng = np.random.default_rng(seed)
    score = pd.Series(rng.standard_t(3, n))
    hours = pd.Series(np.arange(n, dtype="int64") * 3600)
    return score, hours


def test_a_seeded_lookup_answers_exactly_what_the_whole_history_would():
    # The run that saved the book read everything up to the checkpoint; the
    # next one reads only what came after it, and must not be able to tell.
    score, hours = _series()
    whole = sv.record_since(score, hours)

    checkpoint = int(hours.iloc[1999])
    first = sv.RecordState(snapshot_at=checkpoint)
    sv.record_since(score.iloc[:2500], hours.iloc[:2500], state=first)

    later = sv.RecordState(first.snapshot, checkpoint)
    seeded = sv.record_since(score, hours, state=later)
    pd.testing.assert_series_equal(seeded.iloc[2000:], whole.iloc[2000:])
    # The bars the book already covered are not answered for again.
    assert seeded.iloc[:2000].isna().all()


def test_the_book_is_small():
    # A few dozen entries after thousands of bars: that is the whole saving.
    score, hours = _series(20000)
    state = sv.RecordState(snapshot_at=int(hours.iloc[-1]))
    sv.record_since(score, hours, state=state)
    assert 0 < len(state.snapshot) < 60
    mags = [m for _, m in state.snapshot]
    assert mags == sorted(mags, reverse=True)


def test_without_a_state_nothing_changes():
    score, hours = _series(500)
    plain = sv.record_since(score, hours)
    assert sv.record_since(score, hours, state=None).equals(plain)

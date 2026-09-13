import numpy as np
import pandas as pd

from tremor import severity as sv

HOUR = 3600


def hourly(values, start=0):
    """A frame on a round-the-clock hourly grid, which is the simplest clock."""
    return pd.DataFrame({"hour_utc": np.arange(len(values)) * HOUR + start,
                         "score": np.asarray(values, dtype=float)})


def test_levels_do_not_decrease_across_the_ladder():
    # Free now, where the fitted ladder had to impose it by hand: the windows
    # are nested, so the largest move in six years is at least the largest in
    # three. A "major" level below "high" would let a move land in the higher
    # box while failing the lower one.
    rng = np.random.default_rng(2)
    n = 8766 * 8
    f = hourly(rng.standard_t(3, n))
    levels = sv.rolling_levels(f["score"], hour_utc=f["hour_utc"])
    ordered = levels.dropna()
    assert len(ordered) > 1000
    for a, b in zip(sv.TIERS, sv.TIERS[1:]):
        assert (ordered[b] >= ordered[a]).all()


def test_bar_rate_separates_a_session_from_the_clock():
    # No longer used by the ladder, which windows in calendar time directly, but
    # saed still measures it to size a trailing slice. Round-the-clock is 1.0; a
    # seven-hour session five days in seven is about a fifth of that.
    continuous = pd.Series(np.arange(10000) * HOUR)
    assert abs(sv.bar_rate(continuous) - 1.0) < 0.001

    hours = [d * 24 + h for d in range(400) if d % 7 < 5 for h in range(14, 21)]
    assert abs(sv.bar_rate(pd.Series([h * HOUR for h in hours])) - 0.208) < 0.02


def test_a_level_is_the_biggest_move_in_its_own_lookback():
    # The whole rule, on a hand-made series. A month back from the last bar the
    # biggest thing is 3.0, so that is what the noticeable rung asks a move to
    # beat - and the current bar is never part of its own record.
    n = 8766
    values = np.full(n, 1.0)
    values[n - 200] = 3.0          # inside a 30-day lookback from the end
    values[n - 2000] = 9.0         # outside it, inside the 105-day one
    f = hourly(values)
    levels = sv.rolling_levels(f["score"], hour_utc=f["hour_utc"])
    assert levels["noticeable"].iloc[-1] == 3.0
    assert levels["high"].iloc[-1] == 9.0


def test_rolling_levels_never_look_forward():
    # A level is the maximum over bars STRICTLY BEFORE the one it describes, so
    # no bar can be labelled using knowledge of its own future. Structural now -
    # the window is closed on the left - rather than maintained by a refit
    # schedule.
    rng = np.random.default_rng(4)
    f = hourly(rng.standard_t(4, 8766 * 8))
    before = sv.rolling_levels(f["score"], hour_utc=f["hour_utc"])

    disturbed = f["score"].copy()
    disturbed.iloc[60000:] *= 50            # a violent, entirely future regime
    after = sv.rolling_levels(disturbed, hour_utc=f["hour_utc"])

    pd.testing.assert_frame_equal(before.iloc[:60000], after.iloc[:60000])
    assert not before.iloc[60000:].equals(after.iloc[60000:])


def test_a_rung_is_silent_until_the_instrument_has_lived_that_long():
    # "The largest move in six years" cannot be said on two years of data. The
    # fitted ladder needed a rule to stop it saying so; here the answer is a
    # lookback into a record that does not exist yet, so it cannot be got wrong.
    rng = np.random.default_rng(21)
    n = 8766 * 7
    f = hourly(rng.standard_t(4, n))
    levels = sv.rolling_levels(f["score"], hour_utc=f["hour_utc"])

    first = {name: levels[name].first_valid_index() for name in sv.TIERS}
    for name in sv.TIERS:
        lived = f["hour_utc"].iloc[first[name]] - f["hour_utc"].iloc[0]
        assert lived >= sv.tier_days()[name] * sv.SECONDS_PER_DAY
    assert first["extreme"] > first["major"] > first["high"] > first["noticeable"]


def test_the_ladder_grows_a_rung_at_a_time():
    # An instrument that is only four years old has no business calling anything
    # a once-in-six-years move, and says so by leaving the rung empty rather
    # than by lowering it. Four years sits BETWEEN two rungs, so one arrives and
    # the next is withheld on the same history.
    rng = np.random.default_rng(22)
    f = hourly(rng.standard_t(4, int(4 * 8766)))
    levels = sv.rolling_levels(f["score"], hour_utc=f["hour_utc"])
    assert levels["major"].notna().any()
    assert levels["extreme"].isna().all()


def test_a_history_shorter_than_the_shallowest_rung_yields_no_tiers():
    f = hourly(np.random.default_rng(8).standard_t(4, 500))
    levels = sv.rolling_levels(f["score"], hour_utc=f["hour_utc"])
    assert levels.isna().all().all()
    assert sv.assign(f["score"], levels).isna().all()


def test_without_a_clock_there_is_no_tier_rather_than_a_guessed_one():
    # The windows are calendar time. Handed no hours there is nothing to window
    # on, and inventing a bar-count window would make the same rung mean six
    # years in one instrument and four in another.
    score = pd.Series(np.random.default_rng(9).standard_t(4, 30000))
    assert sv.rolling_levels(score).isna().all().all()


def test_assign_picks_the_rarest_level_cleared():
    score = pd.Series([0.5, 1.5, 2.5, 3.5, 4.5, -4.5])
    levels = pd.DataFrame({"noticeable": 1.0, "high": 2.0, "major": 3.0,
                           "extreme": 4.0}, index=score.index)
    tier = sv.assign(score, levels)

    assert list(tier[1:]) == ["noticeable", "high", "major", "extreme", "extreme"]
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
    # Every rung but the deepest is live for most of the record; score over the
    # span the shallowest one covers, which is what dominates the count.
    scored_years = (n - sv.tier_days()["noticeable"] * 24) / 8766

    per_year = out["tier"].notna().sum() / scored_years
    nominal = sum(365.25 / days for days in sv.tier_days().values())
    # Tighter than the 2.5x the fitted ladder needed: a record is calibrated by
    # construction, so the only slack wanted is one draw's sampling noise.
    assert nominal / 1.5 < per_year < nominal * 1.5
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


def test_a_record_is_calibrated_without_any_distribution_assumption():
    # The argument for the whole rule. For ANY series, the chance that the newest
    # of N observations is the largest of those N is exactly 1/N - so the rung
    # fires at its claimed rate because there is no model to get wrong. Run it on
    # a fat tail and on a uniform, which no single fitted tail describes both of.
    for seed, draw in ((41, lambda r, n: r.standard_t(3, n)),
                       (42, lambda r, n: r.uniform(-1, 1, n))):
        rng = np.random.default_rng(seed)
        n = 8766 * 30
        f = hourly(draw(rng, n))
        levels = sv.rolling_levels(f["score"], hour_utc=f["hour_utc"])
        magnitude = sv.magnitudes(f["score"])
        for name in ("high", "major"):
            days = sv.tier_days()[name]
            eligible = (n / 8766) - days / 365.25
            fired = int((magnitude > levels[name]).sum())
            assert 0.6 < (fired / eligible) / (365.25 / days) < 1.6, name


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


def test_record_since_agrees_with_the_rungs_it_has_to_explain():
    # The two are computed separately - one pass of a monotonic stack against
    # four rolling windows - and a message that named a date the tier did not
    # support would be the worst kind of wrong, because both halves look right.
    rng = np.random.default_rng(43)
    n = 8766 * 10
    f = hourly(rng.standard_t(4, n))
    levels = sv.rolling_levels(f["score"], hour_utc=f["hour_utc"])
    since = sv.record_since(f["score"], f["hour_utc"])
    magnitude = sv.magnitudes(f["score"])
    hours = f["hour_utc"].to_numpy()

    age = np.where(since.isna().to_numpy(), np.inf,
                   hours - since.fillna(0).to_numpy())
    for name in sv.TIERS:
        window = sv.tier_days()[name] * sv.SECONDS_PER_DAY
        cleared = (magnitude > levels[name]).to_numpy()
        lived = hours - hours[0] >= window
        # STRICTLY older than the window, not "at least as old". A move matched
        # exactly one window ago is still inside the lookback - the window is
        # [t - D, t) - so it does not clear the rung. Three bars in this draw sit
        # on that boundary, and getting it wrong would date a message a rung out.
        assert np.array_equal(cleared[lived], (age > window)[lived]), name


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
    # keep their order whatever the scale.
    assert sv.tier_days() == dict(sv.TIER_DAYS)
    assert list(sv.tier_days().values()) == sorted(sv.tier_days().values())


def test_the_phrase_is_derived_from_the_number_not_written_beside_it():
    # A hard-coded phrase survives a retune silently and turns every message
    # into a lie about a number the reader cannot check.
    assert sv.period_phrase(14.0) == "about once in 2 weeks"   # not "a fortnight"
    assert sv.period_phrase(30.0) == "about once a month"
    assert sv.period_phrase(105.0) == "about once a quarter"
    assert sv.period_phrase(1095.75) == "about once in 3 years"
    assert sv.period_phrase(2191.5) == "about once in 6 years"
    # And it tracks the knob rather than the base.
    assert sv.period_phrase(sv.TIER_DAYS["extreme"] * 2) == "about once in 12 years"


def test_every_live_rung_has_a_phrase_a_person_would_say():
    for name, days in sv.tier_days().items():
        phrase = sv.period_phrase(days)
        assert phrase.startswith("about once")
        assert "fortnight" not in phrase

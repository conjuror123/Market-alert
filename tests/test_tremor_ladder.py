"""The cached rarity ladder - what makes a trailing slice of history enough."""
import numpy as np
import pandas as pd
import pytest

from tremor import ladder, severity

HOUR = 3600
NAMES = list(severity.TIERS)


def fitted(n, rate=1.0, levels=(1.0, 2.0, 3.0, 4.0), start=0):
    """A levels frame shaped like rolling_levels', constant after the warm-up."""
    warmup = int(severity.WARMUP_DAYS * severity.HOURS_PER_DAY * rate)
    frame = pd.DataFrame({name: np.full(n, np.nan) for name in NAMES})
    for position, name in enumerate(NAMES):
        frame.loc[warmup:, name] = levels[position]
    hours = pd.Series([(start + i) * HOUR for i in range(n)])
    return hours, frame, warmup


def test_a_fitted_ladder_round_trips_through_the_cache():
    # The whole claim: compressing a levels frame to its refit boundaries and
    # expanding it back gives the same numbers on every bar. If that is not
    # exact, nothing downstream of it means anything.
    rate = 1.0
    hours, frame, _ = fitted(20000, rate)
    step = 720
    rows = ladder.segments("x:A", "abnormal", hours, frame, rate, step, "cfg")
    back = ladder.levels_for(rows, "x:A", "abnormal", hours)

    assert len(rows) < len(frame) / 100          # and it is a compression
    for name in NAMES:
        assert np.array_equal(frame[name].to_numpy(), back[name].to_numpy(),
                              equal_nan=True)


def test_a_ladder_that_fitted_to_nothing_is_still_recorded():
    # An instrument alone in its block has no peers, so its abnormal residual is
    # its raw move less a drift and the tail fit has nothing to bite on: every
    # level is NaN. Recording no rows for it would make "fitted and empty"
    # indistinguishable from "never fitted", and every run would go to the full
    # history for ever. This is exactly what happened to USO.
    hours, frame, _ = fitted(20000, levels=(np.nan,) * 4)
    rows = ladder.segments("x:USO", "abnormal", hours, frame, 1.0, 720, "cfg")

    assert not rows.empty
    assert ladder.covers(rows, "x:USO", "abnormal", hours)
    assert ladder.levels_for(rows, "x:USO", "abnormal", hours).isna().all().all()


def test_the_cache_says_no_when_a_refit_has_come_due():
    rate, step = 1.0, 720
    hours, frame, _ = fitted(20000, rate)
    rows = ladder.segments("x:A", "abnormal", hours, frame, rate, step, "cfg")

    assert ladder.covers(rows, "x:A", "abnormal", hours)
    # A whole refit step further on and the answer is no longer cached.
    later = pd.Series(list(hours) + [(20000 + i) * HOUR for i in range(step)])
    assert not ladder.covers(rows, "x:A", "abnormal", later)


def test_an_instrument_the_cache_never_saw_is_not_covered():
    hours, frame, _ = fitted(20000)
    rows = ladder.segments("x:A", "abnormal", hours, frame, 1.0, 720, "cfg")
    assert not ladder.covers(rows, "x:B", "abnormal", hours)
    assert not ladder.covers(rows, "x:A", "absolute", hours)


def test_rows_fitted_under_another_configuration_are_dropped(tmp_path):
    # A cached level is only an answer to the question the code was asking when
    # it was fitted. Change the model and it is a number from a different system.
    hours, frame, _ = fitted(20000)
    mine = ladder.segments("x:A", "abnormal", hours, frame, 1.0, 720, "mine")
    theirs = ladder.segments("x:B", "abnormal", hours, frame, 1.0, 720, "theirs")
    path = tmp_path / "ladder.csv"
    ladder.save(pd.concat([mine, theirs], ignore_index=True), str(path))

    kept = ladder.load(str(path), "mine")
    assert set(kept.asset_id) == {"x:A"}
    assert ladder.load(str(path), "nobody").empty
    assert len(ladder.load(str(path))) == len(mine) + len(theirs)


def test_a_missing_or_broken_cache_is_simply_not_used(tmp_path):
    # The safety story: an absent, unreadable or half-written cache costs a run
    # some time and changes no number.
    assert ladder.load(str(tmp_path / "nothing.csv"), "cfg").empty
    broken = tmp_path / "broken.csv"
    broken.write_text("asset_id,nonsense\nx:A,1\n")
    assert ladder.load(str(broken), "cfg").empty


def test_a_bar_before_the_first_segment_has_no_levels():
    # Which is the same answer rolling_levels gives during the warm-up, and
    # means "no tier" rather than "no event".
    hours, frame, warmup = fitted(20000)
    rows = ladder.segments("x:A", "abnormal", hours, frame, 1.0, 720, "cfg")
    back = ladder.levels_for(rows, "x:A", "abnormal", hours)
    assert back.iloc[:warmup].isna().all().all()
    assert back.iloc[warmup:].notna().all().all()


# --- and the claim the whole thing rests on, on the real archive -------------

def test_a_warm_run_reproduces_a_cold_one_exactly():
    """The guarantee: over the span it publishes, warm == cold, column for column.

    Run on a handful of real instruments rather than synthetic ones, because the
    thing being checked is whether twenty-three years of rolling state can be
    rebuilt from a slice of it, and that is a property of real bar series -
    their gaps, their holidays and their changing session lengths - rather than
    of the arithmetic.
    """
    import os
    from dataclasses import replace

    from tremor import bars, cross_section, pipeline, saed, sessions, versioning
    from tremor.basket import load_basket

    basket = load_basket()
    picked = [a for a in basket.instruments
              if a.ticker in ("SPY", "QQQ", "IWM", "XLF")]
    if len(picked) < 4 or not all(
            os.path.isdir(bars.store_path(bars.DEFAULT_BARS_DIR, a.file_stem))
            for a in picked):
        pytest.skip("needs the real bar store")
    small = replace(basket, assets=tuple(picked), outside=())
    metrics = {a.asset_id: f for a, f in
               ((a, pipeline.load_all(small).get(a.asset_id)) for a in picked)
               if f is not None and not f.empty}
    if len(metrics) < 4:
        pytest.skip("needs per-asset metrics; run python -m tremor.pipeline")

    def panels(source):
        panel = cross_section.build_panel(source, "r")
        hours = panel.index[sessions.reference_hours_mask(
            pd.Series(panel.index), small.anchor_exchange_tz).to_numpy()]
        sigma = cross_section.build_panel(source, "sigma_eff").reindex(index=panel.index)
        return panel.loc[hours], sigma.loc[hours]

    config, _ = versioning.versions_for()
    panel, sigma = panels(metrics)
    hot: list = []
    cold, _, _ = saed.build_for_basket(
        small, metrics, cross_section.block_factors(panel, small, sigma),
        panel, sigma, fitted=hot)
    cache = pd.concat(hot, ignore_index=True)

    frames, warm = saed.plan_frames(small, metrics, cache)
    assert warm, "the cache the cold run just produced must cover the warm frames"
    wpanel, wsigma = panels(frames)
    assert saed.blocks_are_covered(small, wpanel, wsigma, cache)
    fresh, _, _ = saed.build_for_basket(
        small, frames, cross_section.block_factors(wpanel, small, wsigma),
        wpanel, wsigma, cache=cache)

    span = int(max(severity.TIER_DAYS.values()) * 86400)
    floor = int(fresh["hour_utc"].max()) - span
    fresh = fresh[fresh["hour_utc"] >= floor]
    older = cold[cold["hour_utc"] >= floor]

    assert set(older["event_id"]) == set(fresh["event_id"])
    merged = older.merge(fresh, on="event_id", suffixes=("_c", "_w"))
    for column in ("tier", "basis", "channel", "also_moved", "folded_into"):
        left, right = merged[f"{column}_c"].astype(str), merged[f"{column}_w"].astype(str)
        assert (left == right).all(), column
    for column in ("r", "e_resid", "z_resid", "co_block", "sigma_lt"):
        diff = (merged[f"{column}_c"] - merged[f"{column}_w"]).abs().max()
        assert not (diff > 1e-9), f"{column}: {diff}"

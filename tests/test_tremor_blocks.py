"""Block-level events: the days a whole complex moves and no single member is
abnormal, which widening the blocks made both more common and silent."""
import numpy as np
import pandas as pd
import pytest

from tremor import blocks, cross_section, routing, severity, windows
from tremor.basket import Asset, Basket, VolatilityIndex
from datetime import date

HOUR = 3600


def asset(ticker, block, tier=1, in_basket=True, template="crypto_24_7"):
    return Asset(ticker=ticker, source="x", tier=tier, block=block, has_volume=True,
                 session_template=template, fetch_interval="1h", label=ticker,
                 in_basket=in_basket, tick_size=0.01)


def basket_of(*assets, outside=()):
    return Basket(assets=tuple(assets), outside=tuple(outside),
                  volatility_index=VolatilityIndex("V", "fred", "1d", "V", date(1990, 1, 1)),
                  anchor_exchange_tz="America/New_York", history_since=date(2015, 1, 1),
                  session_templates={"crypto_24_7": {}, "us_equity": {}})


def panels(n, moves, sigma=0.01):
    """A wide panel and a matching sigma panel. `moves` maps ticker -> array."""
    index = pd.Index([(i + 1) * HOUR for i in range(n)], name="hour_utc")
    panel = pd.DataFrame({f"x:{k}": v for k, v in moves.items()}, index=index)
    sig = pd.DataFrame({c: sigma for c in panel.columns}, index=index)
    return panel, sig


def test_the_block_move_is_the_median_member_in_sigmas():
    rng = np.random.default_rng(0)
    n = 50
    common = rng.normal(0, 0.01, n)
    panel, sig = panels(n, {"A": common, "B": common, "C": common})
    b = basket_of(asset("A", "crypto"), asset("B", "crypto"), asset("C", "crypto"))

    moves = cross_section.block_moves(panel, b, sig)
    assert moves["crypto"].to_numpy() == pytest.approx(common / 0.01)


def test_an_hour_with_one_member_trading_is_not_a_block_move():
    # A median across one instrument is that instrument, and calling it "the
    # block moved" would put the overnight hours of a US block - when nothing in
    # it trades but one straggler - on the same footing as a real repricing.
    panel, sig = panels(3, {"A": [0.01, np.nan, 0.02], "B": [0.01, 0.05, np.nan]})
    b = basket_of(asset("A", "crypto"), asset("B", "crypto"))

    moves = cross_section.block_moves(panel, b, sig)["crypto"]
    assert moves.notna().iloc[0]
    assert moves.isna().iloc[1] and moves.isna().iloc[2]


def test_the_fx_block_is_oriented_before_the_median():
    # Three pairs quoted with the dollar as base and three as quote: a dollar
    # rally pushes half up and half down, so an unoriented median sees nothing.
    n = 40
    dollar = np.linspace(0.001, 0.01, n)
    panel, sig = panels(n, {"EUR/USD": -dollar, "GBP/USD": -dollar, "AUD/USD": -dollar,
                            "USD/JPY": dollar, "USD/CHF": dollar, "USD/CAD": dollar},
                        sigma=0.001)
    b = basket_of(*[asset(t, "FX") for t in
                    ("EUR/USD", "GBP/USD", "AUD/USD", "USD/JPY", "USD/CHF", "USD/CAD")])

    moves = cross_section.block_moves(panel, b, sig)["FX"]
    assert moves.to_numpy() == pytest.approx(dollar / 0.001)


def test_a_block_too_young_to_have_a_tail_gets_no_events():
    n = 100                                   # far short of the burn-in
    rng = np.random.default_rng(1)
    common = rng.normal(0, 0.01, n)
    panel, sig = panels(n, {"A": common, "B": common})
    b = basket_of(asset("A", "crypto"), asset("B", "crypto"))

    assert blocks.frames(b, panel, sig) == {}


def _long_block(n=30000, spike_at=29000, spike=0.25):
    # Long enough to clear the shallowest PUSH rung, which is what this module
    # emits: `major` is a three-year claim, so a fixture shorter than three
    # years leaves every hour untiered and every block event unborn. That is the
    # right answer and a useless fixture - the two-year warm-up alone is no
    # longer enough to reach the tiers being tested.
    rng = np.random.default_rng(2)
    common = rng.normal(0, 0.01, n)
    common[spike_at] = spike
    panel, sig = panels(n, {"A": common, "B": common * 1.01, "C": common * 0.99})
    b = basket_of(asset("A", "crypto"), asset("B", "crypto"), asset("C", "crypto"))
    return b, panel, sig, spike_at


def test_a_block_that_repriced_produces_an_event_no_member_would_have():
    # The case the whole module exists for. Every member moved the same way, so
    # each one's residual against its peers is nothing and no member fires - but
    # the block plainly moved.
    b, panel, sig, spike_at = _long_block()
    scored = blocks.frames(b, panel, sig)
    events = blocks.events_frame(scored, b, panel)

    assert not events.empty
    hours = set(events["hour_utc"])
    assert (spike_at + 1) * HOUR in hours
    row = events[events["hour_utc"] == (spike_at + 1) * HOUR].iloc[0]
    assert row["asset_id"] == "block:crypto"
    assert row["basis"] == "block"
    assert row["tier"] in ("major", "extreme")
    # Quoted back as a return through the median member's own sigma.
    assert row["r"] == pytest.approx(0.25, rel=0.05)
    assert row["n_members"] == 3


def test_a_block_at_high_becomes_a_digest_row():
    # It used to become nothing: the filter was the two PUSH tiers, on a
    # measurement that pooled `noticeable` and `high` into one 52.7-a-year
    # figure and so priced a choice nobody was making. Measured apart, `high`
    # is 11.4 rows a year - about one a month, 1.0 to 1.6 per block - and it
    # goes into the note rather than onto the phone.
    b, panel, sig, spike_at = _long_block(spike=0.13)
    events = blocks.events_frame(blocks.frames(b, panel, sig), b, panel)

    assert len(events) == 1
    row = events.iloc[0]
    assert row["tier"] == "high"
    assert row["asset_id"] == "block:crypto"
    assert row["hour_utc"] == (spike_at + 1) * HOUR
    # and the note is where it goes - a block never buzzes below `major`.
    assert routing.channel(events).iloc[0] == routing.DIGEST


def test_a_block_at_noticeable_is_still_dropped():
    # The rung that stays out. Nine blocks, and in any hour one of them is the
    # one that moved most, so the bottom rung is the ordinary background of a
    # market - 32.2 rows a year saying "energy moved a bit more than the rest".
    b, panel, sig, _ = _long_block(spike=0.10)
    scored = blocks.frames(b, panel, sig)

    assert set(scored["crypto"]["tier"].dropna()) == {"noticeable"}
    assert blocks.events_frame(scored, b, panel).empty


def test_the_event_names_the_members_that_led():
    # A block's figure is a median and names nobody; the reader's next question
    # is always which members did it.
    b, panel, sig, spike_at = _long_block()
    events = blocks.events_frame(blocks.frames(b, panel, sig), b, panel)
    row = events[events["hour_utc"] == (spike_at + 1) * HOUR].iloc[0]

    assert row["leaders"].startswith("B +25")     # B moves 1.01x the common move
    assert "A +25" in row["leaders"] and "C +24" in row["leaders"]


def test_a_block_move_is_not_put_to_the_single_asset_confirmations():
    # Corrado's rank test and the OU fit ask whether ONE instrument's residual
    # behaves idiosyncratically, and the block factor is what they measure that
    # against - asking them here is asking whether the yardstick is unusual by
    # its own yardstick.
    b, panel, sig, _ = _long_block()
    events = blocks.events_frame(blocks.frames(b, panel, sig), b, panel)
    assert events["rank_confirms"].isna().all()
    assert events["ou_reverts"].isna().all()


def _two_spikes(first, second, sizes=(0.25, 0.30), n=30000, template="crypto_24_7"):
    """The same long block, moving hard twice."""
    rng = np.random.default_rng(2)
    common = rng.normal(0, 0.01, n)
    common[first], common[second] = sizes
    panel, sig = panels(n, {"A": common, "B": common * 1.01, "C": common * 0.99})
    b = basket_of(*(asset(t, "crypto", template=template) for t in "ABC"))
    return b, panel, sig


def test_a_block_reports_once_a_day_however_many_legs_the_move_had():
    # Blocks had no pause of any kind: every qualifying bar became its own push,
    # so a complex that repriced in three legs sent three alerts saying the same
    # thing. One a day now, the same rule its members get.
    b, panel, sig = _two_spikes(29000, 29005)
    events = blocks.events_frame(blocks.frames(b, panel, sig), b, panel)

    assert len(events) == 1
    row = events.iloc[0]
    assert row["hour_utc"] == 29001 * HOUR        # identity stays at the first leg
    assert row["peak_hour_utc"] == 29006 * HOUR   # the description follows the biggest
    assert row["r"] == pytest.approx(0.30, rel=0.05)
    assert row["repeat_count"] == 1


def test_a_block_can_speak_again_once_its_day_has_turned():
    b, panel, sig = _two_spikes(29000, 29030)
    events = blocks.events_frame(blocks.frames(b, panel, sig), b, panel)
    assert len(events) == 2
    assert list(events["repeat_count"]) == [0, 0]


def test_an_exchange_listed_block_closes_its_day_with_the_exchange():
    # 23:00 and 01:00 UTC sit either side of midnight and are one New York
    # evening. A block of funds listed there speaks once; a block of coins,
    # whose day is the clock's, speaks twice for the same two bars.
    listed = _two_spikes(29014, 29016, template="us_equity")
    around = _two_spikes(29014, 29016, template="crypto_24_7")

    assert len(blocks.events_frame(blocks.frames(*listed), listed[0], listed[1])) == 1
    assert len(blocks.events_frame(blocks.frames(*around), around[0], around[1])) == 2


def test_a_block_size_floor_silences_a_move_that_is_rare_but_small(monkeypatch):
    # Same idea as an instrument floor: a push-tier hour that is only a couple
    # of usual hours is not a block line once the floor is raised.
    from types import SimpleNamespace

    b, panel, sig, _ = _long_block()
    scored = blocks.frames(b, panel, sig)
    monkeypatch.setattr("tremor.basket.load_tuning",
                        lambda *a, **k: SimpleNamespace(floor_for=lambda aid: 50.0))
    events = blocks.events_frame(scored, b, panel)
    assert events.empty


def test_a_block_row_the_gap_claimed_carries_the_kind_of_close():
    # The message quotes "a typical member's usual weekend gap", so the row has
    # to say which kind of close its yardstick was learned on.
    b, panel, sig, spike_at = _long_block()
    scored = blocks.frames(b, panel, sig)
    at = scored["crypto"]["hour_utc"] == (spike_at + 1) * HOUR
    scored["crypto"]["overnight"] = at.to_numpy()
    scored["crypto"]["gap_kind"] = pd.Series(np.where(at, "weekend", None),
                                             index=scored["crypto"].index)
    events = blocks.events_frame(scored, b, panel)

    row = events[events["hour_utc"] == (spike_at + 1) * HOUR].iloc[0]
    assert row["overnight"] and row["gap_kind"] == "weekend"
    assert events.loc[~events["overnight"].astype(bool), "gap_kind"].isna().all()

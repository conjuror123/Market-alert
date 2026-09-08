from datetime import datetime, timezone

import pandas as pd

from tremor import routing

HOUR = 3600
DAY = 24 * HOUR


def events(rows, assets=None):
    """rows: (hour_utc, tier, retention_6, retention_settled).

    `assets` names the instruments; distinct ones by default, because that is
    the case the collapse is about - one episode seen through several
    instruments.
    """
    frame = pd.DataFrame(rows, columns=["hour_utc", "tier", "retention_6",
                                        "retention_settled"])
    frame["tier"] = frame["tier"].astype("string")
    frame["asset_id"] = (assets if assets is not None
                         else [f"src:A{i}" for i in range(len(frame))])
    return frame


def test_the_rarest_tier_is_sent_without_waiting_to_see_if_it_held():
    # It cannot wait for the retention check and should not: a once-in-three-
    # years move is worth knowing about while it is happening, even if it turns
    # out to have been someone's liquidity.
    routed = routing.route(events([(DAY, "extreme", -2.0, -3.0)]))
    assert routed["channel"].iloc[0] == routing.PUSH


def test_a_once_a_year_move_is_sent_at_once_even_if_it_later_reverts():
    # It used to wait six bars and go only if it had held. That trades six
    # hours of lateness for a filter the follow-up edit now does better: the
    # message goes at once and is corrected in place when the answer arrives.
    routed = routing.route(events([
        (DAY, "major", 0.9, 0.9),        # held
        (30 * DAY, "major", -0.4, -0.4), # reverted, and still pushed
    ]))
    assert list(routed["channel"]) == [routing.PUSH, routing.PUSH]


def test_a_move_that_reverted_is_still_written_down():
    # It used to be dropped, and the price of that was a digest nobody could
    # see until every one of its events had been answered. The note is now
    # opened at the start of its period and the reversal is written onto the
    # line instead.
    routed = routing.route(events([(DAY, "routine", -0.2, -0.4),
                                   (30 * DAY, "notable", 0.2, 0.1)]))
    assert list(routed["channel"]) == [routing.DIGEST, routing.DIGEST]


def test_a_move_that_held_is_digested():
    routed = routing.route(events([(DAY, "routine", 0.9, 0.8),
                                   (30 * DAY, "notable", 0.6, 0.7)]))
    assert list(routed["channel"]) == [routing.DIGEST, routing.DIGEST]


def test_an_event_is_routed_before_its_retention_can_be_known():
    # Every event the live system has just produced has NaN retention, and it
    # goes into the open note that hour regardless - the answer arrives later
    # as an edit.
    routed = routing.route(events([(DAY, "routine", float("nan"), float("nan"))]))
    assert routed["channel"].iloc[0] == routing.DIGEST


def test_nothing_is_demoted_for_being_the_third_push_of_the_week():
    # The system does not count its own alerts. Five separate episodes, days
    # apart, all rare enough to push: all five push. A weekly budget used to
    # silence the last three, which is the detector answering a question about
    # the reader's patience with an instrument's price history.
    rows = [(DAY + i * 2 * DAY, "major", 0.9, 0.9) for i in range(5)]
    assert list(routing.route(events(rows))["channel"]) == [routing.PUSH] * 5


def test_the_slot_is_the_note_that_is_already_open():
    # Wednesday's move joins the note opened on Tuesday, which is live and on
    # the reader's phone - not one that will be written on Friday.
    wednesday = int(datetime(2026, 4, 1, 9, tzinfo=timezone.utc).timestamp())
    slot = datetime.fromtimestamp(routing.digest_slot(wednesday),
                                  tz=timezone.utc).astimezone(routing.DIGEST_TZ)
    assert slot.weekday() == 1 and slot.hour == routing.DIGEST_HOUR_LOCAL
    assert (slot.year, slot.month, slot.day) == (2026, 3, 31)


def test_a_move_an_hour_after_a_note_opens_joins_that_note():
    tuesday_noon = datetime(2026, 3, 31, 12, tzinfo=routing.DIGEST_TZ)
    just_after = int(tuesday_noon.timestamp()) + HOUR
    assert routing.digest_slot(just_after) == int(tuesday_noon.timestamp())


def test_the_window_runs_from_one_note_to_the_next():
    tuesday_noon = int(datetime(2026, 3, 31, 12, tzinfo=routing.DIGEST_TZ).timestamp())
    start, end = routing.digest_window(tuesday_noon)
    assert start == tuesday_noon
    closes = datetime.fromtimestamp(end, tz=timezone.utc).astimezone(routing.DIGEST_TZ)
    assert closes.weekday() == 4 and closes.hour == routing.DIGEST_HOUR_LOCAL


def test_the_digest_slot_is_local_noon_on_both_sides_of_daylight_saving():
    # A digest that lands at 04:00 twice a week is one nobody opens, so the
    # send time is local and the UTC hour is whatever that implies.
    winter = routing.digest_slot(int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp()))
    summer = routing.digest_slot(int(datetime(2026, 7, 6, tzinfo=timezone.utc).timestamp()))
    for moment in (winter, summer):
        local = datetime.fromtimestamp(moment, tz=timezone.utc).astimezone(routing.DIGEST_TZ)
        assert local.hour == routing.DIGEST_HOUR_LOCAL
    utc_hours = {datetime.fromtimestamp(m, tz=timezone.utc).hour
                 for m in (winter, summer)}
    assert len(utc_hours) == 2      # the zone moved, the local hour did not


def test_only_digested_events_carry_a_slot():
    routed = routing.route(events([(DAY, "extreme", 0.9, 0.9),
                                   (30 * DAY, "routine", 0.9, 0.9)]))
    slots = routed["digest_slot"]
    assert pd.isna(slots.iloc[0])
    # At or before the event, because the note it joins is already open.
    assert slots.iloc[1] <= routed["hour_utc"].iloc[1]


def test_an_empty_table_keeps_the_columns():
    routed = routing.route(events([]))
    assert routed.empty
    assert "channel" in routed.columns and "digest_slot" in routed.columns


def test_a_second_instrument_in_the_same_episode_does_not_buzz_again():
    # 2008-11-20 sent six pushes over two hours for one market event.
    rows = [(DAY, "extreme", 0.9, 0.9), (DAY + HOUR, "extreme", 0.9, 0.9),
            (DAY + 2 * HOUR, "extreme", 0.9, 0.9)]
    routed = routing.route(events(rows))
    assert list(routed["channel"]) == [routing.PUSH, routing.DIGEST, routing.DIGEST]


def test_the_window_reopens_the_next_day():
    # "if it continues to the next day, it is worth firing again"
    rows = [(DAY, "major", 0.9, 0.9), (DAY + 25 * HOUR, "major", 0.9, 0.9)]
    routed = routing.route(events(rows))
    assert list(routed["channel"]) == [routing.PUSH, routing.PUSH]


def test_a_rarer_move_inside_the_window_still_interrupts():
    # A once-a-year move at ten must not silence a once-in-three-years move at
    # one, or the window would invert the ladder the cap is careful to protect.
    rows = [(DAY, "major", 0.9, 0.9), (DAY + 3 * HOUR, "extreme", 0.9, 0.9)]
    routed = routing.route(events(rows))
    assert list(routed["channel"]) == [routing.PUSH, routing.PUSH]


def test_a_milder_move_inside_the_window_does_not():
    rows = [(DAY, "extreme", 0.9, 0.9), (DAY + 3 * HOUR, "major", 0.9, 0.9)]
    routed = routing.route(events(rows))
    assert list(routed["channel"]) == [routing.PUSH, routing.DIGEST]


def test_a_rarer_move_becomes_the_new_anchor():
    rows = [(DAY, "major", 0.9, 0.9), (DAY + 3 * HOUR, "extreme", 0.9, 0.9),
            (DAY + 6 * HOUR, "major", 0.9, 0.9)]
    routed = routing.route(events(rows))
    assert list(routed["channel"]) == [routing.PUSH, routing.PUSH, routing.DIGEST]


def test_collapse_leaves_events_that_were_never_pushes_alone():
    rows = [(DAY, "extreme", 0.9, 0.9), (DAY + HOUR, "routine", -0.5, -0.5)]
    frame = events(rows)
    given = pd.Series([routing.PUSH, routing.DIGEST])
    channels, folded, _ = routing.collapse(frame, given)
    assert list(channels) == [routing.PUSH, routing.DIGEST]
    assert list(folded) == ["", ""]


def test_the_surviving_push_names_the_instruments_it_speaks_for():
    # Collapsing six alerts into one must not understate the day, and a bare
    # count would: WHICH instruments moved together is the diagnosis.
    rows = [(DAY, "extreme", 0.9, 0.9), (DAY + HOUR, "extreme", 0.9, 0.9),
            (DAY + 2 * HOUR, "extreme", 0.9, 0.9)]
    routed = routing.route(events(rows, ["s:SPY", "s:XLF", "s:USO"]))
    assert list(routed["channel"]) == [routing.PUSH, routing.DIGEST, routing.DIGEST]
    assert routed["also_moved"].iloc[0] == "s:XLF s:USO"


def test_a_lone_push_speaks_for_nobody():
    routed = routing.route(events([(DAY, "extreme", 0.9, 0.9)]))
    assert routed["also_moved"].iloc[0] == ""


def test_the_same_instrument_is_not_named_twice():
    rows = [(DAY, "extreme", 0.9, 0.9), (DAY + HOUR, "extreme", 0.9, 0.9),
            (DAY + 2 * HOUR, "extreme", 0.9, 0.9)]
    routed = routing.route(events(rows, ["s:SPY", "s:XLF", "s:XLF"]))
    assert routed["also_moved"].iloc[0] == "s:XLF"


def test_the_names_follow_the_new_anchor_after_an_escalation():
    # major opens, extreme takes over, a later major folds into the EXTREME -
    # so the names belong to the extreme, not to the major that opened.
    rows = [(DAY, "major", 0.9, 0.9), (DAY + 3 * HOUR, "extreme", 0.9, 0.9),
            (DAY + 6 * HOUR, "major", 0.9, 0.9)]
    routed = routing.route(events(rows, ["s:A", "s:B", "s:C"]))
    assert list(routed["also_moved"]) == ["", "s:C", ""]


def test_a_push_does_not_name_its_own_instrument_as_a_companion():
    # A second event on the same instrument inside the window is the same move
    # continuing. Folding its id into the companion list made the biggest
    # messages read absurdly: "Dollar / franc - biggest move in about three
    # years ... with Dollar / franc within the day".
    rows = [(DAY, "extreme", 0.9, 0.9), (DAY + HOUR, "major", 0.9, 0.9),
            (DAY + 2 * HOUR, "major", 0.9, 0.9)]
    frame = events(rows)
    frame["asset_id"] = ["twelvedata:USD/CHF", "twelvedata:USD/CHF", "twelvedata:EUR/USD"]
    _, folded, _ = routing.collapse(frame, pd.Series([routing.PUSH] * len(rows)))
    named = folded.iloc[0].split(" ")
    assert "twelvedata:USD/CHF" not in named
    assert named == ["twelvedata:EUR/USD"]


def test_a_folded_event_records_which_push_it_belongs_to():
    # It does not buzz again, but it does take a row in the note - within the
    # hour, right under the push that already named it. Without this the row
    # cannot say which alert it belongs to, and the same news reads as arriving
    # twice.
    rows = [(DAY, "extreme", 0.9, 0.9), (DAY + HOUR, "extreme", 0.9, 0.9)]
    routed = routing.route(events(rows, assets=["src:SPY", "src:XLF"]))
    assert list(routed["channel"]) == [routing.PUSH, routing.DIGEST]
    assert routed["folded_into"].iloc[0] == ""
    assert routed["folded_into"].iloc[1] == "src:SPY"


def test_an_event_that_was_never_a_push_belongs_to_nothing():
    routed = routing.route(events([(DAY, "extreme", 0.9, 0.9),
                                   (DAY + HOUR, "routine", 0.9, 0.9)],
                                  assets=["src:SPY", "src:GLD"]))
    assert routed["folded_into"].iloc[1] == ""


def test_the_two_directions_agree():
    # The push names its companions, each companion names the push.
    rows = [(DAY, "extreme", 0.9, 0.9), (DAY + HOUR, "major", 0.9, 0.9)]
    routed = routing.route(events(rows, assets=["src:SPY", "src:XLF"]))
    assert routed["also_moved"].iloc[0] == "src:XLF"
    assert routed["folded_into"].iloc[1] == "src:SPY"

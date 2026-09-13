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
    routed = routing.route(events([(DAY, "noticeable", -0.2, -0.4),
                                   (30 * DAY, "high", 0.2, 0.1)]))
    assert list(routed["channel"]) == [routing.DIGEST, routing.DIGEST]


def test_a_move_that_held_is_digested():
    routed = routing.route(events([(DAY, "noticeable", 0.9, 0.8),
                                   (30 * DAY, "high", 0.6, 0.7)]))
    assert list(routed["channel"]) == [routing.DIGEST, routing.DIGEST]


def test_an_event_is_routed_before_its_retention_can_be_known():
    # Every event the live system has just produced has NaN retention, and it
    # goes into the open note that hour regardless - the answer arrives later
    # as an edit.
    routed = routing.route(events([(DAY, "noticeable", float("nan"), float("nan"))]))
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
                                   (30 * DAY, "noticeable", 0.9, 0.9)]))
    slots = routed["digest_slot"]
    assert pd.isna(slots.iloc[0])
    # At or before the event, because the note it joins is already open.
    assert slots.iloc[1] <= routed["hour_utc"].iloc[1]


def test_an_empty_table_keeps_the_columns():
    routed = routing.route(events([]))
    assert routed.empty
    assert "channel" in routed.columns and "digest_slot" in routed.columns



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













def test_the_collector_closes_at_midnight_rather_than_after_a_rolling_day():
    # A rolling window means the reader can never say when the next interruption
    # becomes possible. A day means they can: it fills until midnight, and the
    # next one opens with the first bar after it.
    midnight = 10 * DAY
    rows = [(midnight - HOUR, "extreme", 0.9, 0.9),   # late on day 9
            (midnight + HOUR, "extreme", 0.9, 0.9)]   # early on day 10
    routed = routing.route(events(rows))
    assert list(routed["channel"]) == [routing.PUSH, routing.PUSH]





def test_a_rarer_member_still_interrupts_a_block_push():
    # The one rule the collapse must never break: a later, rarer push is news
    # whoever it belongs to, and being a block does not make the anchor stick.
    HOUR = 3600
    frame = events([(10 * HOUR, "major", 1.0, 1.0),
                    (12 * HOUR, "extreme", 1.0, 1.0)],
                   assets=["block:equity", "src:XLF"])
    out = routing.route(frame)

    assert list(out[out["channel"] == routing.PUSH]["asset_id"]) == [
        "block:equity", "src:XLF"]


# --- a push is final when it arrives ---------------------------------------

def test_the_channel_is_a_function_of_the_tier_and_nothing_else():
    # There used to be a collapse: a second push inside the same UTC day was
    # folded under the first, so an event's channel depended on what else
    # happened that day. Measured on the record that put 199 of 9,069 events in
    # a channel their tier did not choose, and it made "which channel is this
    # in" a question with a time-dependent answer.
    rows = pd.DataFrame({
        "asset_id": ["a:1", "a:2", "a:3", "a:4"],
        "hour_utc": [DAY, DAY + HOUR, DAY + 2 * HOUR, DAY + 3 * HOUR],
        "tier": ["extreme", "major", "high", "noticeable"],
    })
    routed = routing.route(rows)
    assert list(routed["channel"]) == ["push", "push", "digest", "digest"]

    # Same tiers, all in one hour, and the answer does not move.
    together = rows.assign(hour_utc=[DAY] * 4)
    assert list(routing.route(together)["channel"]) == list(routed["channel"])


def test_a_whole_day_of_pushes_all_push():
    # 2008-11-20 sent six across two hours and 2020-03-12 five. Folding them
    # was an argument about message count, and the count is not what this is
    # for: six messages on the day the market breaks is the bot working.
    rows = pd.DataFrame({
        "asset_id": [f"a:{i}" for i in range(6)],
        "hour_utc": [DAY + i * HOUR for i in range(6)],
        "tier": ["extreme"] * 3 + ["major"] * 3,
    })
    routed = routing.route(rows)
    assert (routed["channel"] == "push").all()


def test_nothing_records_a_fold_any_more():
    rows = pd.DataFrame({"asset_id": ["a:1"], "hour_utc": [DAY], "tier": ["extreme"]})
    routed = routing.route(rows)
    assert "folded_into" not in routed.columns
    assert "also_moved" not in routed.columns

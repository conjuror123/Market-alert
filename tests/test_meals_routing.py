from datetime import datetime, timezone

import pandas as pd

from meals import routing

HOUR = 3600
DAY = 24 * HOUR


def events(rows):
    """rows: (hour_utc, tier, retention_6, retention_24)."""
    frame = pd.DataFrame(rows, columns=["hour_utc", "tier", "retention_6",
                                        "retention_24"])
    frame["tier"] = frame["tier"].astype("string")
    return frame


def test_the_rarest_tier_is_sent_without_waiting_to_see_if_it_held():
    # It cannot wait for the retention check and should not: a once-in-three-
    # years move is worth knowing about while it is happening, even if it turns
    # out to have been someone's liquidity.
    routed = routing.route(events([(DAY, "extreme", -2.0, -3.0)]))
    assert routed["channel"].iloc[0] == routing.PUSH


def test_a_once_a_year_move_is_sent_only_if_it_is_still_standing():
    routed = routing.route(events([
        (DAY, "major", 0.9, 0.9),        # held at six bars - sent
        (30 * DAY, "major", 0.1, 0.8),   # gave it back, then recovered - too late
    ]))
    assert list(routed["channel"]) == [routing.PUSH, routing.DIGEST]


def test_a_move_that_reverted_is_dropped_rather_than_digested():
    # Not a failure of the detector - it correctly found an unusual move - but
    # not something to spend a line on either.
    routed = routing.route(events([(DAY, "routine", -0.2, -0.4),
                                   (30 * DAY, "notable", 0.2, 0.1)]))
    assert list(routed["channel"]) == [routing.DROPPED, routing.DROPPED]


def test_a_move_that_held_is_digested():
    routed = routing.route(events([(DAY, "routine", 0.9, 0.8),
                                   (30 * DAY, "notable", 0.6, 0.7)]))
    assert list(routed["channel"]) == [routing.DIGEST, routing.DIGEST]


def test_retention_that_is_not_known_yet_is_not_a_reversal():
    # Every event the live system has just produced has NaN retention. Treating
    # that as "it reverted" would drop precisely the newest events, which is
    # the opposite of what the system is for.
    routed = routing.route(events([(DAY, "routine", float("nan"), float("nan"))]))
    assert routed["channel"].iloc[0] == routing.DIGEST


def test_the_rate_limit_demotes_the_extra_pushes():
    rows = [(DAY + i * HOUR, "major", 0.9, 0.9) for i in range(5)]
    routed = routing.route(events(rows), cap=2)
    assert list(routed["channel"]).count(routing.PUSH) == 2
    assert list(routed["channel"]).count(routing.DIGEST) == 3


def test_the_rate_limit_never_silences_the_rarest_tier():
    # Greedy and chronological, a Monday once-a-year move would otherwise spend
    # the budget a Wednesday once-in-three-years move needed - inverting the
    # whole ladder to save a message. Measured on the real basket that silenced
    # five of thirty-nine extremes.
    rows = [(DAY, "major", 0.9, 0.9), (DAY + HOUR, "major", 0.9, 0.9),
            (DAY + 2 * HOUR, "extreme", 0.9, 0.9)]
    channels = list(routing.route(events(rows), cap=2)["channel"])
    assert channels == [routing.PUSH, routing.PUSH, routing.PUSH]


def test_the_rate_limit_window_rolls_rather_than_resets():
    rows = [(DAY, "major", 0.9, 0.9),
            (DAY + 3 * DAY, "major", 0.9, 0.9),     # inside the week - demoted
            (DAY + 9 * DAY, "major", 0.9, 0.9)]     # clear of it - sent
    channels = list(routing.route(events(rows), cap=1)["channel"])
    assert channels == [routing.PUSH, routing.DIGEST, routing.PUSH]


def test_the_digest_slot_is_the_next_tuesday_or_friday():
    # Tuesday covers the weekend and Monday, which matters because crypto
    # trades straight through it and equities gap on the Monday open; Friday
    # closes the trading week.
    wednesday = int(datetime(2026, 4, 1, 9, tzinfo=timezone.utc).timestamp())
    slot = datetime.fromtimestamp(routing.digest_slot(wednesday),
                                  tz=timezone.utc).astimezone(routing.DIGEST_TZ)
    assert slot.weekday() == 4 and slot.hour == routing.DIGEST_HOUR_LOCAL
    assert (slot.year, slot.month, slot.day) == (2026, 4, 3)


def test_the_digest_slot_is_strictly_after_the_event():
    # An event at one minute past the Tuesday send goes to Friday, not into a
    # digest that has already gone out.
    tuesday_noon = datetime(2026, 3, 31, 12, tzinfo=routing.DIGEST_TZ)
    just_after = int(tuesday_noon.timestamp()) + HOUR
    slot = datetime.fromtimestamp(routing.digest_slot(just_after),
                                  tz=timezone.utc).astimezone(routing.DIGEST_TZ)
    assert slot.weekday() == 4


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
                                   (30 * DAY, "routine", 0.9, 0.9),
                                   (60 * DAY, "routine", -0.5, -0.5)]))
    slots = routed["digest_slot"]
    assert pd.isna(slots.iloc[0]) and pd.isna(slots.iloc[2])
    assert slots.iloc[1] > routed["hour_utc"].iloc[1]


def test_an_empty_table_keeps_the_columns():
    routed = routing.route(events([]))
    assert routed.empty
    assert "channel" in routed.columns and "digest_slot" in routed.columns

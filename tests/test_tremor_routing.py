"""The weekly note's slots: which note an hour belongs to and when it opens.
Which words push is tested with the events that carry it (test_tremor_jumps)."""
from datetime import datetime, timezone

from tremor import routing

HOUR = 3600


def test_the_slot_is_the_note_that_is_already_open():
    # Wednesday's move joins the note opened on Saturday, which is live and on
    # the reader's phone - not one that will be written next Saturday.
    wednesday = int(datetime(2026, 4, 1, 9, tzinfo=timezone.utc).timestamp())
    slot = datetime.fromtimestamp(routing.digest_slot(wednesday),
                                  tz=timezone.utc).astimezone(routing.DIGEST_TZ)
    assert slot.weekday() == 5 and slot.hour == routing.DIGEST_HOUR_LOCAL
    assert (slot.year, slot.month, slot.day) == (2026, 3, 28)


def test_a_move_an_hour_after_a_note_opens_joins_that_note():
    saturday = datetime(2026, 3, 28, routing.DIGEST_HOUR_LOCAL,
                        routing.DIGEST_MINUTE_LOCAL, tzinfo=routing.DIGEST_TZ)
    just_after = int(saturday.timestamp()) + HOUR
    assert routing.digest_slot(just_after) == int(saturday.timestamp())


def test_the_window_runs_from_one_note_to_the_next():
    # One note a week: opened Saturday, closed when the next Saturday's opens.
    saturday = int(datetime(2026, 3, 28, routing.DIGEST_HOUR_LOCAL,
                            routing.DIGEST_MINUTE_LOCAL,
                            tzinfo=routing.DIGEST_TZ).timestamp())
    start, end = routing.digest_window(saturday)
    assert start == saturday
    assert end - start == 7 * 24 * HOUR


def test_one_note_covers_the_whole_week():
    # The weekend, the working week and the next Friday all belong to the note
    # that opened on Saturday; the next Saturday starts the next one.
    saturday = int(datetime(2026, 4, 4, 6, tzinfo=timezone.utc).timestamp())
    for day in (5, 6, 9, 10):
        later = int(datetime(2026, 4, day, 18, tzinfo=timezone.utc).timestamp())
        assert routing.digest_slot(later) == routing.digest_slot(saturday)
    opens = datetime.fromtimestamp(routing.digest_slot(saturday),
                                   tz=timezone.utc).astimezone(routing.DIGEST_TZ)
    assert opens.weekday() == 5
    next_saturday = int(datetime(2026, 4, 11, 6, tzinfo=timezone.utc).timestamp())
    assert routing.digest_slot(next_saturday) != routing.digest_slot(saturday)


def test_a_note_opens_at_the_same_utc_hour_on_both_sides_of_daylight_saving():
    # The reverse of the rule this replaced. A note used to be the thing that
    # buzzed, so it had to land at a civilised LOCAL hour and its UTC hour moved
    # twice a year; the ping buzzes now and the note is a record, so it takes
    # the boundary the market uses and does not drift at all.
    winter = routing.digest_slot(int(datetime(2026, 1, 7, tzinfo=timezone.utc).timestamp()))
    summer = routing.digest_slot(int(datetime(2026, 7, 8, tzinfo=timezone.utc).timestamp()))
    stamps = {(datetime.fromtimestamp(m, tz=timezone.utc).hour,
               datetime.fromtimestamp(m, tz=timezone.utc).minute)
              for m in (winter, summer)}
    assert stamps == {(routing.DIGEST_HOUR_LOCAL, routing.DIGEST_MINUTE_LOCAL)}

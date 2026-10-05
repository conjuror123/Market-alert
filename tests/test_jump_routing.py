"""The weekly note's boundary: the run after the week's last funds close.
Which words push is tested with the events that carry it (test_jump_jumps)."""
from datetime import datetime, timezone

from jump import routing

HOUR = 3600


def utc(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def test_the_week_turns_at_the_run_after_fridays_close():
    # 16:00 New York is 20:00 UTC in summer; the run after it is 20:05.
    wednesday = utc(2026, 9, 30, 9)
    assert routing.digest_slot(wednesday) == utc(2026, 9, 25, 20, 5)
    assert routing.next_digest_slot(wednesday) == utc(2026, 10, 2, 20, 5)


def test_the_closing_hour_is_found_in_the_turn_run_and_joins_the_new_note():
    close = utc(2026, 10, 2, 20)
    assert routing.digest_slot(close + 300) == close + 300
    assert routing.digest_slot(close) == utc(2026, 9, 25, 20, 5)


def test_the_turn_follows_new_york_across_daylight_saving():
    assert routing.digest_slot(utc(2026, 1, 7)) == utc(2026, 1, 2, 21, 5)


def test_a_good_friday_week_turns_on_thursday():
    assert routing.next_digest_slot(utc(2026, 3, 31)) == utc(2026, 4, 2, 20, 5)


def test_a_half_day_turns_at_its_early_close():
    assert routing.next_digest_slot(utc(2026, 11, 24)) == utc(2026, 11, 27, 18, 5)


def test_the_next_close_skips_holidays_and_weekends():
    # Labor Day 2026 is Monday 7 September.
    assert routing.next_close(utc(2026, 9, 4, 20)) == utc(2026, 9, 8, 20)
    assert routing.next_close(utc(2026, 9, 4, 19)) == utc(2026, 9, 4, 20)

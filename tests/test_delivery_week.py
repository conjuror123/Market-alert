"""What is on the channel, and when: the week's curation (tremor_delivery).

A fake channel stands in for Telegram. It holds the messages by id, so each
test states what the reader would see, not which calls were made.
"""
from datetime import datetime, timedelta, timezone

import pytest

from price_monitor import notifier
from price_monitor import tremor_delivery as md
from price_monitor.config import Config
from price_monitor.notifier import TelegramError
from tremor import jumps, routing

HOUR = 3600
# The week under test: Sunday 6 September 2026, 00:05 UTC, to the next Sunday.
OPEN = datetime(2026, 9, 6, 0, 5, tzinfo=timezone.utc)
SLOT = int(OPEN.timestamp())
NEXT = routing.next_digest_slot(SLOT)
MON = datetime(2026, 9, 7, tzinfo=timezone.utc)
LABELS = {"twelvedata:GLD": "Gold", "coinbase:BTC-USD": "Bitcoin"}


class Channel:
    """The public channel: messages by id, and the ones that rang."""

    def __init__(self):
        self.messages, self.rang, self.next_id = {}, [], 1
        self.refuse_delete, self.fail_send = set(), False

    def send(self, token, chat, text, *a, **k):
        if self.fail_send:
            raise TelegramError("Telegram request failed")
        mid, self.next_id = self.next_id, self.next_id + 1
        self.messages[mid] = text
        self.rang.append(text)
        return mid

    def edit(self, token, chat, message_id, text, *a, **k):
        if message_id not in self.messages:
            raise TelegramError("Telegram API error 400: message to edit not found")
        self.messages[message_id] = text

    def delete(self, token, chat, message_id, *a, **k):
        if message_id in self.refuse_delete or message_id not in self.messages:
            return False
        del self.messages[message_id]
        return True

    # what the reader sees
    def notes(self):
        return [t for t in self.messages.values() if "<b>Digest</b>" in t]

    def note(self):
        return "\n".join(self.notes())

    def pushes(self):
        return [t for t in self.messages.values()
                if "<b>Digest</b>" not in t and "Added to digest" not in t]

    def pings(self):
        return [t for t in self.messages.values() if "Added to digest" in t]

    def rings_since(self, count):
        return self.rang[count:]


@pytest.fixture
def channel(monkeypatch, tmp_path):
    ch = Channel()
    monkeypatch.setattr(md, "send_telegram_message", ch.send)
    monkeypatch.setattr(md, "edit_telegram_message", ch.edit)
    monkeypatch.setattr(notifier, "delete_telegram_message", ch.delete)
    monkeypatch.setattr(md, "_labels", lambda: LABELS)
    monkeypatch.setattr(md, "_calendar", lambda cfg: None)
    monkeypatch.setattr(md, "vix_context", lambda hour: "")
    monkeypatch.setattr(jumps, "detector_version", lambda root=None: "v1")
    ch.path = str(tmp_path / "state.json")
    return ch


def cfg(channel, **over):
    base = dict(telegram_bot_token="t", telegram_chat_id="c",
                tremor_alerts_muted=False, state_path=channel.path)
    return Config(**(base | over))


def ev(event_id="e1", *, at, tier="noticeable", asset="twelvedata:GLD", kept=True,
       superseded_by=None, found=None, r=0.021):
    """An event as tremor.jumps writes it; `at` is the hour's start."""
    hour = int(at.timestamp())
    return dict(event_id=event_id, asset_id=asset, hour_utc=hour, tier=tier,
                basis="jump", r=r, sigma_lt=0.003, overnight=False, kept=kept,
                superseded_by=superseded_by,
                found_utc=hour + HOUR if found is None else int(found.timestamp()),
                day=hour // 86400)


def run(monkeypatch, channel, events, now, state, **over):
    monkeypatch.setattr(md, "load_events", lambda c: list(events))
    return md.maybe_deliver(cfg(channel, **over), state, now)


def at(day, hour, minute=0):
    return MON + timedelta(days=day, hours=hour, minutes=minute)


def run_at(day, hour):
    """The hourly run: five past."""
    return at(day, hour, 5)


# --- the note ---------------------------------------------------------------

def test_the_note_opens_on_sunday_even_with_nothing_in_it(monkeypatch, channel):
    state = {}
    run(monkeypatch, channel, [ev(at=OPEN - timedelta(days=3))], OPEN, state)
    assert len(channel.notes()) == 1
    assert "Nothing so far" in channel.note()
    assert "06.09.2026 to 12.09.2026" in channel.note()


def test_an_empty_events_table_changes_nothing(monkeypatch, channel):
    assert run(monkeypatch, channel, [], OPEN, {}) == 0
    assert channel.messages == {}


def test_nothing_goes_out_while_muted(monkeypatch, channel):
    run(monkeypatch, channel, [ev(at=OPEN)], OPEN, {}, tremor_alerts_muted=True)
    assert channel.messages == {}


def test_the_hour_checked_in_the_opening_run_goes_into_the_new_note(monkeypatch, channel):
    # Saturday 23:00-00:00 is found at Sunday 00:00, and scored in the run that
    # opens the note.
    last = ev(at=OPEN - timedelta(minutes=65))
    state = {}
    run(monkeypatch, channel, [last], OPEN, state)
    assert "Gold" in channel.note()
    assert len(channel.pings()) == 1


def test_a_move_found_before_the_note_opened_is_history(monkeypatch, channel):
    # Friday 23:00, found Saturday: it belonged to last week's note, and a bar
    # that heals after Sunday does not bring it into this one.
    friday = ev(at=OPEN - timedelta(hours=25), tier="high")
    state = {}
    run(monkeypatch, channel, [friday], OPEN, state)
    run(monkeypatch, channel, [friday | {"tier": "major"}], OPEN + timedelta(hours=1), state)
    assert channel.pushes() == [] and "Gold" not in channel.note()


# --- rows and pings ---------------------------------------------------------

def test_a_noticeable_move_is_a_row_with_one_ping(monkeypatch, channel):
    state = {}
    run(monkeypatch, channel, [], OPEN, state)
    row = ev(at=at(0, 10))
    run(monkeypatch, channel, [row], OPEN, state)
    run(monkeypatch, channel, [row], run_at(0, 11), state)
    rang = len(channel.rang)
    run(monkeypatch, channel, [row], run_at(0, 12), state)
    assert channel.rings_since(rang) == []
    assert "Gold" in channel.note()
    assert channel.pings() == ["⬜ <b>GLD</b> · Gold +2.10% · 7.0×σ\nAdded to digest👆🏻👆🏻"]
    assert channel.pushes() == []


def test_a_row_that_vanishes_leaves_the_note_and_takes_its_ping(monkeypatch, channel):
    state = {}
    row = ev(at=at(0, 10))
    run(monkeypatch, channel, [row], run_at(0, 11), state)
    run(monkeypatch, channel, [ev("other", at=at(0, 3), kept=False)], run_at(0, 12), state)
    assert "Gold" not in channel.note() and channel.pings() == []


def test_a_day_shows_one_row_even_when_an_earlier_hour_heals_in(monkeypatch, channel):
    # 10:00 was the day's first noticeable reading; 08:00 then heals into one,
    # and one_a_day keeps 08:00 instead. The day is one row, not two.
    state = {}
    ten = ev("ten", at=at(0, 10))
    run(monkeypatch, channel, [ten], run_at(0, 11), state)
    eight = ev("eight", at=at(0, 8))
    run(monkeypatch, channel, [eight, ten | {"kept": False}], run_at(0, 12), state)
    assert "1 event so far" in channel.note()
    assert len(channel.pings()) == 1


def test_a_row_that_turns_high_within_a_day_becomes_a_push(monkeypatch, channel):
    state = {}
    row = ev(at=at(0, 10))
    run(monkeypatch, channel, [row], run_at(0, 11), state)
    rang = len(channel.rang)
    run(monkeypatch, channel, [row | {"tier": "high"}], run_at(0, 12), state)
    assert channel.rings_since(rang) == [md.format_push(row | {"tier": "high"}, LABELS)]
    assert channel.pings() == [] and "Gold" not in channel.note()


def test_a_row_that_turns_high_after_a_day_is_only_recoloured(monkeypatch, channel):
    state = {}
    row = ev(at=at(0, 10))
    run(monkeypatch, channel, [row], run_at(0, 11), state)
    rang = len(channel.rang)
    run(monkeypatch, channel, [row | {"tier": "high"}], run_at(1, 12), state)
    assert channel.rings_since(rang) == []
    assert channel.pushes() == []
    assert "🟨 <b>GLD</b>" in channel.note()
    assert channel.pings()[0].startswith("🟨 <b>GLD</b>")


def test_a_move_found_more_than_a_day_ago_is_never_sent(monkeypatch, channel):
    state = {}
    run(monkeypatch, channel, [ev("x", at=at(0, 1))], run_at(0, 1), state)
    late = [ev("a", at=at(0, 1), tier="high"), ev("b", at=at(0, 2))]
    run(monkeypatch, channel, late, run_at(1, 5), state)
    assert channel.pushes() == [] and channel.pings() == []


# --- pushes -----------------------------------------------------------------

def test_a_push_rings_once(monkeypatch, channel):
    state = {}
    push = ev(at=at(0, 10), tier="high")
    run(monkeypatch, channel, [push], run_at(0, 11), state)
    rang = len(channel.rang)
    run(monkeypatch, channel, [push], run_at(0, 12), state)
    assert channel.rings_since(rang) == []
    assert channel.pushes() == [md.format_push(push, LABELS)]
    assert "Gold" not in channel.note()


def test_a_rarer_word_within_a_day_rings_again_and_the_old_push_goes(monkeypatch, channel):
    state = {}
    push = ev(at=at(0, 10), tier="high")
    run(monkeypatch, channel, [push], run_at(0, 11), state)
    rang = len(channel.rang)
    run(monkeypatch, channel, [push | {"tier": "major"}], run_at(0, 12), state)
    assert channel.rings_since(rang) == [md.format_push(push | {"tier": "major"}, LABELS)]
    assert channel.pushes() == [md.format_push(push | {"tier": "major"}, LABELS)]


def test_a_rarer_word_after_a_day_is_an_edit(monkeypatch, channel):
    state = {}
    push = ev(at=at(0, 10), tier="high")
    run(monkeypatch, channel, [push], run_at(0, 11), state)
    rang = len(channel.rang)
    run(monkeypatch, channel, [push | {"tier": "major"}], run_at(1, 12), state)
    assert channel.rings_since(rang) == []
    assert channel.pushes() == [md.format_push(push | {"tier": "major"}, LABELS)]


def test_a_push_that_falls_between_push_words_is_edited_silently(monkeypatch, channel):
    state = {}
    push = ev(at=at(0, 10), tier="major")
    run(monkeypatch, channel, [push], run_at(0, 11), state)
    rang = len(channel.rang)
    run(monkeypatch, channel, [push | {"tier": "high"}], run_at(0, 12), state)
    assert channel.rings_since(rang) == []
    assert channel.pushes()[0].startswith("🟨")


def test_a_push_that_falls_to_noticeable_turns_white_where_it_stands(monkeypatch, channel):
    state = {}
    push = ev(at=at(0, 10), tier="high")
    run(monkeypatch, channel, [push], run_at(0, 11), state)
    run(monkeypatch, channel, [push | {"tier": "noticeable"}], run_at(0, 12), state)
    assert [p[:1] for p in channel.pushes()] == ["⬜"]
    assert "Gold" not in channel.note() and channel.pings() == []


def test_a_push_that_flip_flops_is_edited_and_never_rings_twice(monkeypatch, channel):
    state = {}
    push = ev(at=at(0, 10), tier="high")
    run(monkeypatch, channel, [push], run_at(0, 11), state)
    rang = len(channel.rang)
    run(monkeypatch, channel, [push | {"tier": "noticeable"}], run_at(0, 12), state)
    run(monkeypatch, channel, [push], run_at(0, 13), state)
    assert channel.rings_since(rang) == []
    assert channel.pushes() == [md.format_push(push, LABELS)]


def test_a_push_whose_move_is_gone_is_deleted(monkeypatch, channel):
    state = {}
    push = ev(at=at(0, 10), tier="high")
    run(monkeypatch, channel, [push], run_at(0, 11), state)
    run(monkeypatch, channel, [ev("other", at=at(0, 3), kept=False)], run_at(0, 12), state)
    assert channel.pushes() == []


def test_a_move_that_vanishes_and_returns_within_a_day_rings_again(monkeypatch, channel):
    state = {}
    push = ev(at=at(0, 10), tier="high")
    other = ev("other", at=at(0, 3), kept=False)
    run(monkeypatch, channel, [push], run_at(0, 11), state)
    run(monkeypatch, channel, [other], run_at(0, 12), state)
    rang = len(channel.rang)
    run(monkeypatch, channel, [push], run_at(0, 13), state)
    assert channel.rings_since(rang) == [md.format_push(push, LABELS)]


def test_different_days_are_different_pushes(monkeypatch, channel):
    state = {}
    monday = ev("mon", at=at(0, 10), tier="high")
    tuesday = ev("tue", at=at(1, 10), tier="high")
    run(monkeypatch, channel, [monday], run_at(0, 11), state)
    run(monkeypatch, channel, [monday, tuesday], run_at(1, 11), state)
    assert len(channel.pushes()) == 2


# --- a day that grows -------------------------------------------------------

def test_a_day_that_grows_from_a_row_to_a_push_takes_the_row_away(monkeypatch, channel):
    state = {}
    morning = ev("am", at=at(0, 10))
    run(monkeypatch, channel, [morning], run_at(0, 11), state)
    afternoon = ev("pm", at=at(0, 14), tier="high")
    run(monkeypatch, channel, [morning | {"superseded_by": "pm"}, afternoon],
        run_at(0, 15), state)
    assert channel.pushes() == [md.format_push(afternoon, LABELS)]
    assert "Gold" not in channel.note() and channel.pings() == []


def test_a_day_that_grew_and_fell_back_keeps_its_push_white(monkeypatch, channel):
    state = {}
    morning = ev("am", at=at(0, 10))
    afternoon = ev("pm", at=at(0, 14), tier="high")
    run(monkeypatch, channel, [morning], run_at(0, 11), state)
    run(monkeypatch, channel, [morning | {"superseded_by": "pm"}, afternoon],
        run_at(0, 15), state)
    # 14:00 heals to noticeable: it is no longer rarer than 10:00, so the day
    # keeps 10:00 - but the push stays, white, and 10:00 does not come back.
    fell = afternoon | {"tier": "noticeable", "kept": False}
    run(monkeypatch, channel, [morning, fell], run_at(0, 16), state)
    assert [p[:1] for p in channel.pushes()] == ["⬜"]
    assert "Gold" not in channel.note() and channel.pings() == []


def test_a_day_that_grows_between_pushes_rings_and_deletes_the_lower(monkeypatch, channel):
    state = {}
    morning = ev("am", at=at(0, 10), tier="high")
    afternoon = ev("pm", at=at(0, 14), tier="major")
    run(monkeypatch, channel, [morning], run_at(0, 11), state)
    rang = len(channel.rang)
    run(monkeypatch, channel, [morning | {"superseded_by": "pm"}, afternoon],
        run_at(0, 15), state)
    assert channel.rings_since(rang) == [md.format_push(afternoon, LABELS)]
    assert channel.pushes() == [md.format_push(afternoon, LABELS)]


def test_a_lower_push_telegram_will_not_delete_is_struck_through(monkeypatch, channel):
    state = {}
    morning = ev("am", at=at(0, 10), tier="high")
    afternoon = ev("pm", at=at(0, 14), tier="major")
    run(monkeypatch, channel, [morning], run_at(0, 11), state)
    channel.refuse_delete.add(state[md.STATE_KEY][md.WEEK]["pushes"]["am"]["id"])
    run(monkeypatch, channel, [morning | {"superseded_by": "pm"}, afternoon],
        run_at(0, 15), state)
    struck = [t for t in channel.messages.values() if t.startswith("<s>")]
    assert struck == ["<s>" + md.format_push(morning, LABELS).split("\n")[0] + "</s>"]


# --- the week turns ---------------------------------------------------------

def test_the_next_note_clears_the_pings_and_leaves_the_rest_as_history(monkeypatch, channel):
    state = {}
    row = ev("row", at=at(5, 10))
    push = ev("push", at=at(5, 12), tier="high", asset="coinbase:BTC-USD")
    run(monkeypatch, channel, [row, push], run_at(5, 13), state)
    old_note = channel.note()
    sunday = datetime.fromtimestamp(NEXT, tz=timezone.utc)
    # Even a push whose move has since changed is last week's, and stays.
    run(monkeypatch, channel, [row, push | {"tier": "noticeable"}], sunday, state)
    assert channel.pings() == []
    assert channel.pushes() == [md.format_push(push, LABELS)]
    notes = channel.notes()
    assert len(notes) == 2 and notes[0] == old_note
    assert "Nothing so far" in notes[1]


def test_a_note_that_shrinks_deletes_its_surplus_parts(monkeypatch, channel):
    state = {}
    many = [ev(f"r{i}", at=at(0, 1), asset=f"twelvedata:X{i}") for i in range(120)]
    run(monkeypatch, channel, many, run_at(0, 2), state)
    parts = [t for t in channel.messages.values() if "<i>part " in t]
    assert len(parts) > 1, "the fixture must be long enough to split"
    run(monkeypatch, channel, many[:2], run_at(0, 3), state)
    assert [t for t in channel.messages.values() if "<i>part " in t] == []
    assert len(channel.notes()) == 1 and "2 events so far" in channel.note()


def test_a_failed_send_is_retried_on_the_next_run(monkeypatch, channel):
    state = {}
    run(monkeypatch, channel, [ev("x", at=at(0, 1))], run_at(0, 1), state)
    push = ev(at=at(0, 10), tier="high")
    channel.fail_send = True
    run(monkeypatch, channel, [push], run_at(0, 11), state)
    channel.fail_send = False
    run(monkeypatch, channel, [push], run_at(0, 12), state)
    assert channel.pushes() == [md.format_push(push, LABELS)]


# --- a detector update --------------------------------------------------------

def test_a_detector_update_restarts_the_week_under_the_same_note(monkeypatch, channel):
    state = {}
    row = ev("row", at=at(1, 10))
    push = ev("push", at=at(1, 12), tier="high", asset="coinbase:BTC-USD")
    run(monkeypatch, channel, [row, push], run_at(1, 13), state)
    note_id = state[md.STATE_KEY][md.WEEK]["note"]["ids"][0]

    monkeypatch.setattr(jumps, "detector_version", lambda root=None: "v2")
    fresh = ev("new", at=at(2, 9), tier="high")
    rang = len(channel.rang)
    run(monkeypatch, channel, [row, push | {"tier": "major"}, fresh], run_at(2, 10), state)
    # Everything of the week goes but the note, which carries on; what was found
    # before the update never rings again, what is found from it on does.
    assert channel.rings_since(rang) == [md.format_push(fresh, LABELS)]
    assert channel.pushes() == [md.format_push(fresh, LABELS)]
    assert channel.pings() == []
    assert state[md.STATE_KEY][md.WEEK]["note"]["ids"][0] == note_id
    assert "Nothing so far" in channel.messages[note_id]


def test_the_previous_delivery_state_is_taken_over_as_an_update(monkeypatch, channel):
    # What production's state.json holds before the switch.
    old_note = channel.send("", "", "📋 <b>Digest</b> - old")
    old_push = channel.send("", "", "🟨 <b>GLD</b> · Gold")
    older_push = channel.send("", "", "🟨 <b>SPY</b>")
    ping = channel.send("", "", "⬜ x\nAdded to digest👆🏻👆🏻")
    state = {md.STATE_KEY: {
        "digests": {str(SLOT): {"ids": [old_note], "hashes": ["h"]}},
        "sent": {"a": {"hour": SLOT + 5 * HOUR, "id": old_push},
                 "b": {"hour": SLOT - 5 * HOUR, "id": older_push}},
        "tracked": {}, "pings": {"p": {"id": ping, "hash": "h"}}}}
    run(monkeypatch, channel, [ev(at=at(0, 1))], run_at(1, 10), state)
    assert set(channel.messages) == {old_note, older_push}
    assert "Nothing so far" in channel.messages[old_note]
    assert set(state[md.STATE_KEY]) == {md.WEEK}

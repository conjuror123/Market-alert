"""What is on the channel, and when: the week's curation (jump_delivery).

A fake channel stands in for Telegram. It holds the messages by id, so each
test states what the reader would see, not which calls were made.
"""
import copy
import html
import re
from datetime import datetime, timedelta, timezone

import pytest

from price_monitor import notifier
from price_monitor import jump_delivery as md
from price_monitor import telegram_ledger as ledger
from price_monitor.config import Config
from price_monitor.notifier import TelegramError, TelegramUnsure
from jump import jumps, routing, verify

HOUR = 3600
# The week under test: from the run after Friday 4 September 2026's close
# (16:00 New York, 20:00 UTC) to the run after the next Friday's.
OPEN = datetime(2026, 9, 4, 20, 5, tzinfo=timezone.utc)
SLOT = int(OPEN.timestamp())
NEXT = routing.next_digest_slot(SLOT)
MON = datetime(2026, 9, 7, tzinfo=timezone.utc)
LABELS = {"twelvedata:GLD": "Gold", "coinbase:BTC-USD": "Bitcoin"}
SIGMA = 0.003
SIZE = {"noticeable": 4.5, "high": 6.0, "major": 8.5, "extreme": 12.0}


HEALTH = "h"


def as_telegram_gives_it(text):
    """A message's text as the Bot API returns it: no tags, entities read."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class Channel:
    """The channel: messages by id, and the ones that rang. Like Telegram on
    2026-11-03 it can hold a send without answering (`hang`) and put it up
    later under the next id (`publish_late`), and hold an edit the same way."""

    def __init__(self):
        self.messages, self.rang, self.next_id = {}, [], 1
        self.refuse_delete, self.fail_send = set(), False
        self.hang, self.held = 0, []
        self.hang_edit, self.held_edits = 0, []
        self.unforwardable, self.health, self.calls = set(), {}, []
        self.clock, self.dates = 0, {}

    def send(self, token, chat, text, *a, **k):
        self.calls.append("send")
        if self.fail_send:
            raise TelegramError("Telegram request failed")
        if self.hang:
            self.hang -= 1
            self.held.append((text, k.get("silent")))
            raise TelegramUnsure("Telegram did not answer")
        return self._put(text, k.get("silent"))

    def _put(self, text, silent=False, late=0):
        mid, self.next_id = self.next_id, self.next_id + 1
        self.messages[mid] = text
        self.dates[mid] = self.clock + late
        if not silent:
            self.rang.append(text)
        return mid

    def publish_late(self):
        """Telegram puts up what it held, under the next ids, half an hour on."""
        ids = [self._put(text, silent, late=1800) for text, silent in self.held]
        self.held = []
        return ids

    def post(self, text):
        """Somebody else's post on the channel."""
        return self._put(text, silent=True, late=60)

    def edit(self, token, chat, message_id, text, *a, **k):
        self.calls.append("edit")
        if message_id not in self.messages:
            raise TelegramError("Telegram API error 400: message to edit not found")
        if self.hang_edit:
            self.hang_edit -= 1
            self.held_edits.append((message_id, text))
            raise TelegramUnsure("Telegram did not answer")
        self.messages[message_id] = text

    def apply_late_edits(self):
        for message_id, text in self.held_edits:
            self.messages[message_id] = text
        self.held_edits = []

    def delete(self, token, chat, message_id, *a, **k):
        self.calls.append("delete")
        if chat == HEALTH:
            return self.health.pop(message_id, None) is not None
        if message_id in self.refuse_delete or message_id not in self.messages:
            return False
        del self.messages[message_id]
        return True

    def forward(self, token, from_chat, message_id, to_chat, *a, **k):
        self.calls.append("forward")
        if message_id in self.unforwardable:
            return {"state": "unforwardable"}
        if message_id not in self.messages:
            return {"state": "missing"}
        copy = 100_000 + len(self.health) + 1
        self.health[copy] = self.messages[message_id]
        return {"state": "found", "text": as_telegram_gives_it(self.messages[message_id]),
                "copy": copy, "date": self.dates.get(message_id, 0)}

    # what the reader sees
    def notes(self):
        return [t for t in self.messages.values() if "<b>Digest</b>" in t]

    def note(self):
        return "\n".join(self.notes())

    def pushes(self):
        return [t for t in self.messages.values()
                if "<b>Digest</b>" not in t and "🕐" in t]

    def pings(self):
        return [t for t in self.messages.values()
                if "<b>Digest</b>" not in t and "🕐" not in t]

    def rings_since(self, count):
        return self.rang[count:]


@pytest.fixture
def channel(monkeypatch, tmp_path):
    ch = Channel()
    monkeypatch.setattr(md, "send_telegram_message", ch.send)
    monkeypatch.setattr(md, "edit_telegram_message", ch.edit)
    monkeypatch.setattr(notifier, "delete_telegram_message", ch.delete)
    monkeypatch.setattr(notifier, "forward_telegram_message", ch.forward)
    monkeypatch.setattr(notifier, "edit_telegram_message", ch.edit)
    monkeypatch.setattr(notifier, "send_telegram_message", ch.send)
    monkeypatch.setattr(md, "_labels", lambda: LABELS)
    monkeypatch.setattr(md, "_calendar", lambda cfg: None)
    monkeypatch.setattr(md, "vix_context", lambda hour: "")
    monkeypatch.setattr(jumps, "detector_version", lambda root=None: "v1")
    monkeypatch.setattr(verify, "VERIFIED_PATH", str(tmp_path / "verified.csv"))
    ch.path = str(tmp_path / "state.json")
    return ch


def cfg(channel, **over):
    base = dict(telegram_bot_token="t", telegram_chat_id="c", telegram_health_chat_id=HEALTH,
                jump_alerts_muted=False, state_path=channel.path)
    return Config(**(base | over))


def ev(at, tier="noticeable", *, asset="twelvedata:GLD", size=None, found=None,
       sigma=SIGMA, reading="hour", check=None, held=None):
    """A flagged reading as jump.jumps writes it; `at` is the hour's start."""
    hour = int(at.timestamp())
    size = SIZE[tier] if size is None else size
    return dict(reading_id=f"jump:{asset}:{reading}:{hour}", asset_id=asset,
                hour_utc=hour, tier=tier, basis="jump", r=size * sigma, z=size,
                sigma_lt=sigma, overnight=reading != "hour", reading=reading,
                found_utc=hour + HOUR if found is None else int(found.timestamp()),
                check_utc=None if check is None else int(check.timestamp()), held=held)


def run(monkeypatch, channel, readings, now, state, **over):
    """One hourly run, as __main__ makes it: a new process (the breaker closed),
    the unsure sends looked for, then the delivery."""
    monkeypatch.setattr(md, "load_events", lambda c: [dict(r) for r in readings])
    config = cfg(channel, **over)
    channel.clock = int(now.timestamp())
    ledger.reset()
    channel.found = ledger.reconcile(config, state, now)
    sent = md.maybe_deliver(config, state, now)
    channel.found += ledger.settle_strays(config)
    channel.news = ledger.news()
    return sent


def at(day, hour, minute=0):
    return MON + timedelta(days=day, hours=hour, minutes=minute)


def run_at(day, hour):
    """The hourly run: five past."""
    return at(day, hour, 5)


def story(text):
    lines = [line for line in text.split("\n") if line.startswith("✏️")]
    return lines[0] if lines else ""


@pytest.fixture
def week(monkeypatch, channel):
    """A week whose note is already open."""
    state = {}
    run(monkeypatch, channel, [ev(OPEN - timedelta(days=3))], OPEN, state)
    return state


# --- the note ---------------------------------------------------------------

def test_the_note_opens_after_the_weeks_last_close_even_with_nothing_in_it(
        monkeypatch, channel, week):
    assert NEXT == int(datetime(2026, 9, 11, 20, 5, tzinfo=timezone.utc).timestamp())
    assert len(channel.notes()) == 1
    assert "Nothing so far" in channel.note()
    assert "04.09.2026 to 11.09.2026" in channel.note()


def test_an_empty_events_table_changes_nothing(monkeypatch, channel):
    assert run(monkeypatch, channel, [], OPEN, {}) == 0
    assert channel.messages == {}


def test_nothing_goes_out_while_muted(monkeypatch, channel):
    run(monkeypatch, channel, [ev(OPEN)], OPEN, {}, jump_alerts_muted=True)
    assert channel.messages == {}


def test_the_hour_checked_in_the_opening_run_goes_into_the_new_note(monkeypatch, channel):
    # Friday's closing hour, 19:00-20:00 UTC, is found at the close and scored
    # in the run that opens the note.
    state = {}
    run(monkeypatch, channel, [ev(OPEN - timedelta(minutes=65))], OPEN, state)
    assert "Gold" in channel.note() and len(channel.pings()) == 1


def test_a_move_found_before_the_note_opened_is_history(monkeypatch, channel):
    friday = ev(OPEN - timedelta(hours=25), "high")
    state = {}
    run(monkeypatch, channel, [friday], OPEN, state)
    run(monkeypatch, channel, [dict(friday, tier="major", z=9.0)], OPEN + timedelta(hours=1),
        state)
    assert channel.pushes() == [] and "Gold" not in channel.note()


# --- a new event --------------------------------------------------------------

def test_a_noticeable_move_is_a_row_with_one_ping_and_no_story(monkeypatch, channel, week):
    row = ev(at(0, 10))
    run(monkeypatch, channel, [row], run_at(0, 11), week)
    rang = len(channel.rang)
    run(monkeypatch, channel, [row], run_at(0, 12), week)
    assert channel.rings_since(rang) == []
    assert "Gold" in channel.note() and story(channel.note()) == ""
    assert channel.pings() == ["⬜ <b>GLD</b> · Gold +1.35% · 4.5×σ"]


def test_a_high_move_is_a_push_that_rings_once_and_says_no_story(monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    run(monkeypatch, channel, [push], run_at(0, 11), week)
    rang = len(channel.rang)
    run(monkeypatch, channel, [push], run_at(0, 12), week)
    assert channel.rings_since(rang) == []
    assert channel.pushes() == [md.format_push(push, LABELS)]
    assert "Gold" not in channel.note()


def test_a_move_found_more_than_a_day_ago_is_never_sent(monkeypatch, channel, week):
    late = [ev(at(0, 1), "high"), ev(at(0, 2), asset="coinbase:BTC-USD")]
    run(monkeypatch, channel, late, run_at(1, 5), week)
    assert channel.pushes() == [] and channel.pings() == []


# --- voted not real --------------------------------------------------------------

# Some other instrument's old move, so the table is not empty (an empty one is
# "the pipeline did not run" and changes nothing).
BYSTANDER = ev(at(0, 3), asset="coinbase:BTC-USD", found=at(-3, 0))


def _unseen(at_hour, asset="twelvedata:GLD", check="close", verifier="yahoo,marketwatch",
            seen="", verdict=None, moves="0.000300,0.000200"):
    """Most sources did not see the move: not real (or another vote). An
    empty move is an outage."""
    hour = int(at_hour.timestamp())
    record = verify.load()
    record[(asset, hour, check)] = {"asset_id": asset, "hour_utc": hour, "check": check,
                                    "verdict": verdict or verify.NOT_REAL,
                                    "provider": "twelvedata", "verifier": verifier,
                                    "seen": seen, "stored_move": "0.018",
                                    "verifier_move": moves}
    verify.write(record, hour)


def test_a_push_not_seen_by_a_second_source_stays_marked_silently(monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    run(monkeypatch, channel, [push], run_at(0, 11), week)
    rang = len(channel.rang)
    _unseen(at(0, 10))
    run(monkeypatch, channel, [BYSTANDER], run_at(0, 12), week)
    marked = md.format_push(push, LABELS) + "\n❌ Twelve Data(+1.82%), Yahoo(+0.03%), MarketWatch(+0.02%)"
    assert channel.pushes() == [marked] and channel.rings_since(rang) == []
    # It stays so while nothing of it comes back.
    run(monkeypatch, channel, [BYSTANDER], run_at(0, 13), week)
    assert channel.pushes() == [marked] and channel.rings_since(rang) == []


def test_a_mark_shows_every_sources_move_and_an_outage_as_such(monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    run(monkeypatch, channel, [push], run_at(0, 11), week)
    _unseen(at(0, 10), verifier="yahoo,sina,marketwatch,alpaca_sip", seen="sina",
            moves=",0.015,0.000200,0.000100")
    run(monkeypatch, channel, [BYSTANDER], run_at(0, 12), week)
    assert channel.pushes()[0].endswith(
        "\n❌ Twelve Data(+1.82%), Yahoo(outage), Sina(+1.51%), MarketWatch(+0.02%), "
        "Alpaca_SIP(+0.01%)")


def test_a_row_not_seen_leaves_the_note_and_its_ping_line_is_marked(monkeypatch, channel, week):
    row = ev(at(0, 10))
    run(monkeypatch, channel, [row], run_at(0, 11), week)
    _unseen(at(0, 10))
    run(monkeypatch, channel, [BYSTANDER], run_at(1, 14), week)    # after its 24 hours too
    assert "Gold" not in channel.note()
    assert channel.pings() == ["⬜ <b>GLD</b> · Gold +1.35% · 4.5×σ ❌ Twelve Data(+1.82%), Yahoo(+0.03%), MarketWatch(+0.02%)"]


def test_a_gap_is_marked_by_the_opening_check_not_the_hours(monkeypatch, channel, week):
    gap = ev(at(0, 13), "high", reading="night")
    run(monkeypatch, channel, [gap], run_at(0, 14), week)
    _unseen(at(0, 13), check="close")
    run(monkeypatch, channel, [BYSTANDER], run_at(0, 15), week)
    assert channel.pushes() == []                                 # just gone
    run(monkeypatch, channel, [gap], run_at(0, 16), week)
    _unseen(at(0, 13), check="open")
    run(monkeypatch, channel, [BYSTANDER], run_at(0, 17), week)
    assert channel.pushes()[0].endswith("❌ Twelve Data(+1.82%), Yahoo(+0.03%), MarketWatch(+0.02%)")


def test_an_uncertain_move_says_so_and_loses_the_line_once_the_vote_turns(
        monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    _unseen(at(0, 10), verifier="yahoo,sina,marketwatch", seen="sina",
            verdict=verify.UNCERTAIN, moves="0.000300,0.015,0.000200")
    run(monkeypatch, channel, [push], run_at(0, 11), week)
    rang = len(channel.rang)
    assert channel.pushes() == [md.format_push(push, LABELS) + "\n⚠️ Twelve Data(+1.82%), "
                                "Yahoo(+0.03%), Sina(+1.51%), MarketWatch(+0.02%)"]
    # A recount: MarketWatch saw it after all. Real, said by saying nothing.
    _unseen(at(0, 10), verifier="yahoo,sina,marketwatch", seen="sina,marketwatch",
            verdict=verify.REAL)
    run(monkeypatch, channel, [push], run_at(0, 12), week)
    assert channel.pushes() == [md.format_push(push, LABELS)]
    assert channel.rings_since(rang) == []


def test_a_tie_with_a_source_down_says_so_in_the_note(monkeypatch, channel, week):
    # The store and Sina for it; Yahoo down and MarketWatch against: a tie.
    row = ev(at(0, 10))
    _unseen(at(0, 10), verifier="yahoo,sina,marketwatch", seen="sina",
            verdict=verify.UNCERTAIN, moves=",0.015,0.000200")
    run(monkeypatch, channel, [row], run_at(0, 11), week)
    assert "Gold +1.35%" in channel.note()
    assert "⚠️ Twelve Data(+1.82%), Yahoo(outage), Sina(+1.51%), MarketWatch(+0.02%)" \
        in channel.note()


def test_a_reading_without_a_vote_says_nothing_of_one(monkeypatch, channel, week):
    # No row in the record - under 4σ, or no source to ask: nothing said.
    row = ev(at(0, 10), asset="sina:AHD")
    run(monkeypatch, channel, [row], run_at(0, 11), week)
    note = channel.note()
    assert "AHD" in note and "⚠️" not in note and "❌" not in note and "single" not in note


def test_a_real_move_inside_a_marked_events_day_takes_it_over_and_rings(
        monkeypatch, channel, week):
    run(monkeypatch, channel, [ev(at(0, 10), "high")], run_at(0, 11), week)
    _unseen(at(0, 10))
    run(monkeypatch, channel, [BYSTANDER], run_at(0, 12), week)
    rang = len(channel.rang)
    real = ev(at(0, 15), "major")
    run(monkeypatch, channel, [real], run_at(0, 16), week)
    assert len(channel.rings_since(rang)) == 1
    (push,) = channel.pushes()
    assert push.startswith(md.format_push(real, LABELS)) and "not real" in story(push)
    assert "❌" not in push


def test_a_marked_move_confirmed_after_all_is_unmarked_silently(monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    run(monkeypatch, channel, [push], run_at(0, 11), week)
    _unseen(at(0, 10))
    run(monkeypatch, channel, [BYSTANDER], run_at(1, 12), week)          # after its 24 hours
    rang = len(channel.rang)
    run(monkeypatch, channel, [push], run_at(1, 13), week)               # healed, confirmed
    assert channel.rings_since(rang) == []
    (shown,) = channel.pushes()
    assert "⚠️" not in shown and shown.startswith(md._first(md.format_push(push, LABELS)))


def test_a_doubt_lifted_inside_its_24_hours_is_unmarked_silently(monkeypatch, channel, week):
    # F4: it rang a second time, as if the move were new.
    push = ev(at(0, 10), "high")
    run(monkeypatch, channel, [push], run_at(0, 11), week)
    _unseen(at(0, 10))
    run(monkeypatch, channel, [BYSTANDER], run_at(0, 12), week)
    rang = len(channel.rang)
    run(monkeypatch, channel, [push], run_at(0, 13), week)                # healed, confirmed
    assert channel.rings_since(rang) == []
    (shown,) = channel.pushes()
    assert "⚠️" not in shown and shown.startswith(md._first(md.format_push(push, LABELS)))


# --- inside its 24 hours --------------------------------------------------------

def test_a_bigger_hour_of_the_same_word_is_edited_in_place(monkeypatch, channel, week):
    first = ev(at(0, 10))
    run(monkeypatch, channel, [first], run_at(0, 11), week)
    rang = len(channel.rang)
    bigger = ev(at(0, 14), size=5.0)
    run(monkeypatch, channel, [first, bigger], run_at(0, 15), week)
    assert channel.rings_since(rang) == []
    assert "5.0×σ" in channel.note() and "4.5×σ" not in channel.note().split("✏️")[0]
    assert story(channel.note()) == "✏️ ⬜ 4.5×σ 10:00 → ⬜ 5.0×σ 14:00 bigger jump"
    assert channel.pings()[0].startswith("⬜ <b>GLD</b> · Gold +1.50% · 5.0×σ")


def test_a_row_that_turns_high_is_deleted_and_rings_as_a_push(monkeypatch, channel, week):
    first = ev(at(0, 10))
    run(monkeypatch, channel, [first], run_at(0, 11), week)
    rang = len(channel.rang)
    rarer = ev(at(0, 14), "high")
    run(monkeypatch, channel, [first, rarer], run_at(0, 15), week)
    assert len(channel.rings_since(rang)) == 1
    assert channel.pings() == [] and "Gold" not in channel.note()
    push = channel.pushes()[0]
    assert push.startswith("🟨 <b>GLD</b>")
    assert story(push) == "✏️ ⬜ 4.5×σ 10:00 → 🟨 6.0×σ 14:00 bigger jump"


def test_a_push_that_turns_rarer_is_deleted_and_rings_again(monkeypatch, channel, week):
    high = ev(at(0, 10), "high")
    run(monkeypatch, channel, [high], run_at(0, 11), week)
    rang = len(channel.rang)
    major = ev(at(0, 12), "major")
    run(monkeypatch, channel, [high, major], run_at(0, 13), week)
    assert len(channel.rings_since(rang)) == 1
    assert [p[:1] for p in channel.pushes()] == ["🟧"]


def test_a_push_that_falls_to_noticeable_turns_white_and_says_why(monkeypatch, channel, week):
    high = ev(at(0, 10), "high")
    run(monkeypatch, channel, [high], run_at(0, 11), week)
    rang = len(channel.rang)
    run(monkeypatch, channel, [ev(at(0, 10), size=4.6)], run_at(0, 12), week)
    assert channel.rings_since(rang) == []
    push = channel.pushes()[0]
    assert push.startswith("⬜ <b>GLD</b>")
    assert story(push) == "✏️ 🟨 6.0×σ 10:00 → ⬜ 4.6×σ 10:00 price corrected"
    assert channel.pings() == [] and "Gold" not in channel.note()


def test_a_white_push_that_turns_high_again_rings_again(monkeypatch, channel, week):
    run(monkeypatch, channel, [ev(at(0, 10), "high")], run_at(0, 11), week)
    run(monkeypatch, channel, [ev(at(0, 10), size=4.6)], run_at(0, 12), week)
    rang = len(channel.rang)
    run(monkeypatch, channel, [ev(at(0, 10), "high", size=6.1)], run_at(0, 13), week)
    assert len(channel.rings_since(rang)) == 1
    assert len(channel.pushes()) == 1 and channel.pushes()[0].startswith("🟨")


def test_a_push_that_falls_between_push_words_is_edited_silently(monkeypatch, channel, week):
    run(monkeypatch, channel, [ev(at(0, 10), "major")], run_at(0, 11), week)
    rang = len(channel.rang)
    run(monkeypatch, channel, [ev(at(0, 10), "high", size=7.0)], run_at(0, 12), week)
    assert channel.rings_since(rang) == []
    assert channel.pushes()[0].startswith("🟨")


def test_a_row_that_falls_away_takes_its_ping(monkeypatch, channel, week):
    run(monkeypatch, channel, [ev(at(0, 10))], run_at(0, 11), week)
    run(monkeypatch, channel, [ev(at(0, 3), asset="coinbase:BTC-USD")], run_at(0, 12), week)
    assert "Gold" not in channel.note() and not any("GLD" in p for p in channel.pings())


def test_a_push_that_vanishes_and_returns_within_a_day_rings_again(monkeypatch, channel, week):
    high = ev(at(0, 10), "high")
    other = ev(at(0, 3), asset="coinbase:BTC-USD")
    run(monkeypatch, channel, [high], run_at(0, 11), week)
    run(monkeypatch, channel, [other], run_at(0, 12), week)
    assert channel.pushes() == []
    rang = len(channel.rang)
    run(monkeypatch, channel, [other, high], run_at(0, 13), week)
    assert len(channel.rings_since(rang)) == 1
    assert story(channel.pushes()[0]) == (
        "✏️ 🟨 6.0×σ 10:00 → ✖ corrected away → 🟨 6.0×σ 10:00 price corrected")


def test_a_late_hour_says_it_arrived_late(monkeypatch, channel, week):
    first = ev(at(0, 10))
    run(monkeypatch, channel, [first], run_at(0, 13), week)
    late = ev(at(0, 11), size=5.0)                   # found 12:00, seen only at 14:05
    run(monkeypatch, channel, [first, late], run_at(0, 14), week)
    assert story(channel.note()).endswith("⬜ 5.0×σ 11:00 arrived late")


def test_a_moved_yardstick_says_sigma_corrected(monkeypatch, channel, week):
    run(monkeypatch, channel, [ev(at(0, 10))], run_at(0, 11), week)
    moved = dict(ev(at(0, 10)), sigma_lt=SIGMA * 0.9, z=5.0)
    run(monkeypatch, channel, [moved], run_at(0, 12), week)
    assert story(channel.note()).endswith("⬜ 5.0×σ 10:00 σ corrected")


def test_a_revised_price_says_price_corrected(monkeypatch, channel, week):
    run(monkeypatch, channel, [ev(at(0, 10))], run_at(0, 11), week)
    revised = dict(ev(at(0, 10)), r=SIGMA * 5.0, z=5.0)
    run(monkeypatch, channel, [revised], run_at(0, 12), week)
    assert story(channel.note()).endswith("⬜ 5.0×σ 10:00 price corrected")


def test_repeated_price_revisions_for_the_same_hour_do_not_accumulate(monkeypatch, channel, week):
    run(monkeypatch, channel, [ev(at(0, 10))], run_at(0, 11), week)
    revised1 = dict(ev(at(0, 10)), r=SIGMA * 5.0, z=5.0)
    run(monkeypatch, channel, [revised1], run_at(0, 12), week)
    revised2 = dict(ev(at(0, 10)), r=SIGMA * 4.7, z=4.7)
    run(monkeypatch, channel, [revised2], run_at(0, 13), week)
    revised3 = dict(ev(at(0, 10)), r=SIGMA * 5.0, z=5.0)
    run(monkeypatch, channel, [revised3], run_at(0, 14), week)
    assert story(channel.note()) == "✏️ ⬜ 4.5×σ 10:00 → ⬜ 5.0×σ 10:00 price corrected"


def test_story_line_compacts_oscillating_and_repeated_steps():
    story_steps = [
        ["noticeable", 5.5, 1793210400, ""],
        ["noticeable", 4.7, 1793210400, "price corrected"],
        ["noticeable", 5.0, 1793210400, "price corrected"],
        ["noticeable", 4.7, 1793210400, "price corrected"],
        ["noticeable", 5.2, 1793235600, "price corrected"],
        ["noticeable", 4.7, 1793210400, "1793235600 corrected away"],
        ["noticeable", 5.0, 1793210400, "price corrected"],
        ["noticeable", 4.7, 1793210400, "price corrected"],
        ["noticeable", 5.0, 1793210400, "price corrected"],
    ]
    line = md.story_line(story_steps, 1793210400)
    assert line == "✏️ ⬜ 5.5×σ 18:00 → ⬜ 5.0×σ 18:00 price corrected"


def test_an_event_is_24_hours_so_the_next_move_is_a_new_event(monkeypatch, channel, week):
    run(monkeypatch, channel, [ev(at(0, 10))], run_at(0, 11), week)
    again = [ev(at(0, 10)), ev(at(1, 12))]           # 26 hours on
    run(monkeypatch, channel, again, run_at(1, 13), week)
    assert len(channel.pings()) == 2
    assert "2 events so far" in channel.note()


def test_a_funds_next_morning_is_the_same_event(monkeypatch, channel, week):
    # 19:00 UTC Monday and Tuesday's open are inside one 24 hours.
    evening = ev(at(0, 19))
    morning = ev(at(1, 13), size=5.0, reading="night")
    run(monkeypatch, channel, [evening], run_at(0, 20), week)
    run(monkeypatch, channel, [evening, morning], run_at(1, 14), week)
    assert len(channel.pings()) == 1 and "1 event so far" in channel.note()


# --- after its 24 hours -------------------------------------------------------

def test_after_a_day_a_row_that_turns_high_becomes_its_ping_silently(
        monkeypatch, channel, week):
    run(monkeypatch, channel, [ev(at(0, 10))], run_at(0, 11), week)
    rang = len(channel.rang)
    run(monkeypatch, channel, [ev(at(0, 10), "high")], run_at(1, 12), week)
    assert channel.rings_since(rang) == []
    assert channel.pings() == [] and "Gold" not in channel.note()
    push = channel.pushes()[0]
    assert push.startswith("🟨 <b>GLD</b>")
    assert story(push) == "✏️ ⬜ 4.5×σ 10:00 → 🟨 6.0×σ 10:00 price corrected"


def test_after_a_day_a_rarer_push_is_an_edit(monkeypatch, channel, week):
    run(monkeypatch, channel, [ev(at(0, 10), "high")], run_at(0, 11), week)
    rang = len(channel.rang)
    run(monkeypatch, channel, [ev(at(0, 10), "major")], run_at(1, 12), week)
    assert channel.rings_since(rang) == []
    assert [p[:1] for p in channel.pushes()] == ["🟧"]


def test_after_a_day_an_event_corrected_away_stays_gone(monkeypatch, channel, week):
    high = ev(at(0, 10), "high")
    other = ev(at(0, 3), asset="coinbase:BTC-USD")
    run(monkeypatch, channel, [high], run_at(0, 11), week)
    run(monkeypatch, channel, [other], run_at(1, 12), week)
    assert channel.pushes() == []
    rang = len(channel.rang)
    run(monkeypatch, channel, [other, high], run_at(1, 13), week)
    assert channel.rings_since(rang) == [] and channel.pushes() == []


def test_an_event_gone_inside_its_day_that_returns_after_it_stays_gone(
        monkeypatch, channel, week):
    # Nothing can ring after the 24 hours, and there is no message left to edit.
    high = ev(at(0, 10), "high")
    other = ev(at(0, 3), asset="coinbase:BTC-USD")
    run(monkeypatch, channel, [high], run_at(0, 11), week)
    run(monkeypatch, channel, [other], run_at(0, 12), week)
    rang = len(channel.rang)
    run(monkeypatch, channel, [other, high], run_at(1, 13), week)
    run(monkeypatch, channel, [other, high], run_at(1, 14), week)
    assert channel.rings_since(rang) == [] and channel.pushes() == []


def test_after_a_day_a_new_move_inside_the_old_event_is_not_possible(
        monkeypatch, channel, week):
    # A move found after the 24 hours opens its own event, which can ring.
    run(monkeypatch, channel, [ev(at(0, 10))], run_at(0, 11), week)
    rang = len(channel.rang)
    run(monkeypatch, channel, [ev(at(0, 10)), ev(at(1, 11), "high")], run_at(1, 12), week)
    assert len(channel.rings_since(rang)) == 1
    assert "Gold" in channel.note()                  # the old row stays


# --- the week turns -----------------------------------------------------------

def test_the_next_note_clears_the_pings_and_leaves_the_rest_as_history(
        monkeypatch, channel, week):
    row = ev(at(4, 10))
    push = ev(at(4, 12), "high", asset="coinbase:BTC-USD")
    run(monkeypatch, channel, [row, push], run_at(4, 13), week)
    turn = datetime.fromtimestamp(NEXT, tz=timezone.utc)
    run(monkeypatch, channel, [row, push], turn, week)
    old_note = channel.notes()[0]
    assert "1 event\n" in old_note                   # its last render: no longer "so far"
    # From the turn on it is history: a later change to its move changes nothing.
    run(monkeypatch, channel, [row, dict(push, tier="noticeable", z=4.0)],
        turn + timedelta(hours=1), week)
    assert channel.pings() == []
    assert channel.pushes() == [md.format_push(push, LABELS)]
    notes = channel.notes()
    assert len(notes) == 2 and notes[0] == old_note and "Nothing so far" in notes[1]


def test_a_move_after_the_note_opens_is_a_new_event_even_inside_24_hours(
        monkeypatch, channel, week):
    friday = ev(at(4, 17), "high", asset="coinbase:BTC-USD")
    run(monkeypatch, channel, [friday], run_at(4, 18), week)
    turn = datetime.fromtimestamp(NEXT, tz=timezone.utc)
    run(monkeypatch, channel, [friday], turn, week)
    rang = len(channel.rang)
    later = ev(at(4, 22), "high", asset="coinbase:BTC-USD", size=6.5)
    run(monkeypatch, channel, [friday, later], run_at(4, 23), week)
    assert len(channel.rings_since(rang)) == 1
    assert len(channel.pushes()) == 2


def test_a_note_that_shrinks_deletes_its_surplus_parts(monkeypatch, channel, week):
    many = [ev(at(0, 1), asset=f"twelvedata:X{i}") for i in range(120)]
    run(monkeypatch, channel, many, run_at(0, 2), week)
    assert len(channel.notes()) > 1
    run(monkeypatch, channel, many[:2], run_at(0, 3), week)
    assert len(channel.notes()) == 1 and "2 events so far" in channel.note()
    assert "Part " not in channel.note()


def test_a_failed_send_is_retried_on_the_next_run(monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    channel.fail_send = True
    run(monkeypatch, channel, [push], run_at(0, 11), week)
    channel.fail_send = False
    run(monkeypatch, channel, [push], run_at(0, 12), week)
    assert channel.pushes() == [md.format_push(push, LABELS)]


def test_a_push_telegram_will_not_delete_is_struck_through(monkeypatch, channel, week):
    high = ev(at(0, 10), "high")
    run(monkeypatch, channel, [high], run_at(0, 11), week)
    old = max(channel.messages, key=lambda m: channel.messages[m].startswith("🟨"))
    channel.refuse_delete.add(old)
    run(monkeypatch, channel, [high, ev(at(0, 12), "major")], run_at(0, 13), week)
    assert channel.messages[old] == "<s>" + md.format_push(high, LABELS).split("\n")[0] + "</s>"


# --- a detector update --------------------------------------------------------

def test_a_detector_update_keeps_the_week_and_edits_it_silently(monkeypatch, channel, week):
    # 2026-11-04: an update deleted the week's two pushes and six pings and
    # emptied the note's ten rows. Now what is on the channel stays, edited.
    row = ev(at(1, 10))
    push = ev(at(1, 12), "high", asset="coinbase:BTC-USD")
    run(monkeypatch, channel, [row, push], run_at(1, 13), week)
    note_id = week[md.STATE_KEY][md.WEEK]["note"]["ids"][0]
    pushed = {m for m, t in channel.messages.items() if t in channel.pushes()}

    monkeypatch.setattr(jumps, "detector_version", lambda root=None: "v2")
    fresh = ev(at(2, 9), "high", asset="binance:ETH/USDT")   # found in this run's hour
    rang = len(channel.rang)
    run(monkeypatch, channel, [row, dict(push, tier="major", z=9.0), fresh], run_at(2, 10),
        week)
    # Only the new move rings; the push re-read as `major` is edited where it is.
    assert channel.rings_since(rang) == [md.format_push(fresh, LABELS)]
    assert pushed <= set(channel.messages)
    assert any("BTC" in t and "9.0×σ" in t for t in channel.pushes())
    assert len(channel.pings()) == 1                # the row's ping stays
    assert week[md.STATE_KEY][md.WEEK]["note"]["ids"][0] == note_id
    assert "Nothing so far" not in channel.messages[note_id]


def test_a_detector_update_that_changes_no_event_leaves_the_week_alone(
        monkeypatch, channel, week):
    # A refactor or an error path moves the code's hash, not the events: the
    # channel must not lose correct messages over it.
    row = ev(at(1, 10))
    push = ev(at(1, 12), "high", asset="coinbase:BTC-USD")
    run(monkeypatch, channel, [row, push], run_at(1, 13), week)
    before = dict(channel.messages)

    monkeypatch.setattr(jumps, "detector_version", lambda root=None: "v2")
    rang = len(channel.rang)
    run(monkeypatch, channel, [row, push], run_at(1, 14), week)
    assert channel.messages == before
    assert channel.rings_since(rang) == []
    assert week[md.STATE_KEY][md.WEEK]["detector"] == "v2"


def test_a_move_an_update_finds_in_hours_already_read_is_not_sent_late(
        monkeypatch, channel, week):
    # The new detector flags a move a previous run saw unflagged: it is not
    # sent, and what is on the channel stays.
    push = ev(at(1, 12), "high", asset="coinbase:BTC-USD")
    run(monkeypatch, channel, [push], run_at(1, 13), week)
    before = dict(channel.messages)
    monkeypatch.setattr(jumps, "detector_version", lambda root=None: "v2")
    rang = len(channel.rang)
    run(monkeypatch, channel, [push, ev(at(1, 9)), ev(at(1, 11), "high")], run_at(1, 14), week)
    run(monkeypatch, channel, [push, ev(at(1, 9)), ev(at(1, 11), "high")], run_at(1, 15), week)
    assert channel.rings_since(rang) == []
    assert channel.pushes() == [before[m] for m in before if before[m] in channel.pushes()]
    assert channel.pings() == []


# --- held at the funds' close ------------------------------------------

def time_line(text):
    return [line for line in text.split("\n") if line.startswith("🕐")][0]


def test_a_move_counts_down_to_its_close_and_then_says_how_much_held(
        monkeypatch, channel, week):
    close = at(1, 20)                                  # Tuesday 16:00 New York
    push = ev(at(1, 14), "high", check=close)
    run(monkeypatch, channel, [push], run_at(1, 15), week)
    assert time_line(channel.pushes()[0]).endswith(" · close in 5h")
    run(monkeypatch, channel, [push], run_at(1, 19), week)
    assert time_line(channel.pushes()[0]).endswith(" · close in 1h")
    rang = len(channel.rang)
    run(monkeypatch, channel, [dict(push, held=0.8)], run_at(1, 20), week)
    assert time_line(channel.pushes()[0]).endswith(" · close 80%")
    assert channel.rings_since(rang) == [] and story(channel.pushes()[0]) == ""


def test_the_turn_fills_in_the_old_week_then_opens_the_new_note_then_the_closing_hour(
        monkeypatch, channel, week):
    friday_close = datetime.fromtimestamp(NEXT - 300, tz=timezone.utc)
    monday_close = at(7, 20)
    before = ev(at(4, 18), "high", check=friday_close)            # 14:00-15:00 New York
    run(monkeypatch, channel, [before], run_at(4, 19), week)
    assert time_line(channel.pushes()[0]).endswith(" · close in 1h")

    closing = ev(at(4, 19), "high", asset="coinbase:BTC-USD", check=monday_close)
    rang = len(channel.rang)
    turn = datetime.fromtimestamp(NEXT, tz=timezone.utc)
    run(monkeypatch, channel, [dict(before, held=-0.2), closing], turn, week)
    old = [p for p in channel.pushes() if "GLD" in p][0]
    assert time_line(old).endswith(" · close -20%")
    new = channel.rings_since(rang)
    assert "<b>Digest</b>" in new[0] and "Nothing so far" in new[0]
    assert new[1].startswith("🟨 <b>BTC-USD</b>")
    assert time_line(new[1]).endswith(" · next close in 72h")


# --- one message a run ----------------------------------------------------------

FUNDS = [f"twelvedata:F{i}" for i in range(6)]


def alerts(channel):
    """The alert messages on the channel: everything but the note."""
    return [t for t in channel.messages.values() if "<b>Digest</b>" not in t]


def test_the_pushes_of_one_run_are_one_message_biggest_first_ringing_once(
        monkeypatch, channel, week):
    found = [ev(at(0, 10), "high", asset=FUNDS[0], size=6.0),
             ev(at(0, 10), "extreme", asset=FUNDS[1]),
             ev(at(0, 10), "major", asset=FUNDS[2])]
    rang = len(channel.rang)
    run(monkeypatch, channel, found, run_at(0, 11), week)
    assert len(channel.rings_since(rang)) == 1
    [message] = alerts(channel)
    assert [line[:1] for line in message.split("\n") if line[:1] and line[:1] in "🟥🟧🟨"] == \
        ["🟥", "🟧", "🟨"]


def test_pushes_and_pings_of_one_run_are_two_messages_and_only_the_first_rings(
        monkeypatch, channel, week):
    found = [ev(at(0, 10), "high", asset=FUNDS[0]),
             ev(at(0, 10), asset=FUNDS[1], size=4.0),
             ev(at(0, 10), asset=FUNDS[2], size=5.0)]
    rang = len(channel.rang)
    run(monkeypatch, channel, found, run_at(0, 11), week)
    assert len(channel.rings_since(rang)) == 1
    assert channel.rings_since(rang)[0].startswith("🟨 <b>F0</b>")
    [ping] = channel.pings()
    assert ping == ("⬜ <b>F2</b> · F2 +1.50% · 5.0×σ\n"
                    "⬜ <b>F1</b> · F1 +1.20% · 4.0×σ")


def test_news_shared_by_every_push_is_said_once_at_the_end(monkeypatch, channel, week):
    monkeypatch.setattr(md, "calendar_context", lambda hour, cal: (
        f"Nearby economic events (-2h+1h):\n     news at "
        f"{datetime.fromtimestamp(hour, timezone.utc):%H}"))
    run(monkeypatch, channel, [ev(at(0, 10), "high", asset=FUNDS[0]),
                               ev(at(0, 10), "major", asset=FUNDS[1])], run_at(0, 11), week)
    [message] = alerts(channel)
    assert message.count("news at 10") == 1
    assert message.endswith("\n\nNearby economic events (-2h+1h):\n     news at 10")
    # Moves of different hours: each list once, after all the moves, naming its hour.
    run(monkeypatch, channel, [ev(at(1, 9), "high", asset=FUNDS[2]),
                               ev(at(1, 10), "major", asset=FUNDS[3], found=at(1, 11))],
        run_at(1, 11), week)
    newest = alerts(channel)[-1]
    assert newest.index("F3") < newest.index("F2") < newest.index("around 09:00 UTC")
    assert newest.index("news at 09") < newest.index("around 10:00 UTC (-2h+1h):\n     news at 10")


def test_a_move_that_turns_rarer_leaves_its_message_and_rings_in_a_new_one(
        monkeypatch, channel, week):
    first = [ev(at(0, 10), "high", asset=FUNDS[0]), ev(at(0, 10), "high", asset=FUNDS[1])]
    run(monkeypatch, channel, first, run_at(0, 11), week)
    rang = len(channel.rang)
    rarer = ev(at(0, 12), "major", asset=FUNDS[1])
    run(monkeypatch, channel, first + [rarer], run_at(0, 13), week)
    assert len(channel.rings_since(rang)) == 1
    old, new = alerts(channel)
    assert "F0" in old and "F1" not in old
    assert new.startswith("🟧 <b>F1</b>") and "F0" not in new


def test_a_message_loses_a_move_corrected_away_and_goes_with_its_last(
        monkeypatch, channel, week):
    both = [ev(at(0, 10), "high", asset=FUNDS[0]), ev(at(0, 10), "high", asset=FUNDS[1])]
    run(monkeypatch, channel, both, run_at(0, 11), week)
    rang = len(channel.rang)
    run(monkeypatch, channel, both[:1], run_at(0, 12), week)
    [message] = alerts(channel)
    assert "F0" in message and "F1" not in message
    run(monkeypatch, channel, [ev(at(0, 3), asset="coinbase:BTC-USD")], run_at(0, 13), week)
    assert not any("F0" in t for t in alerts(channel))
    assert len(channel.rings_since(rang)) == 1          # BTC's ping, nothing else


def test_a_flood_is_cut_into_few_messages_and_rings_once(monkeypatch, channel, week):
    flood = [ev(at(0, 20), "high", asset=f"twelvedata:X{i}", size=5.5 + i / 100)
             for i in range(108)]
    rang = len(channel.rang)
    run(monkeypatch, channel, flood, run_at(0, 21), week)
    messages = alerts(channel)
    assert 1 < len(messages) <= 12
    assert all(len(m) <= md.MESSAGE_BUDGET for m in messages)
    assert len(channel.rings_since(rang)) == 1
    assert sum(m.count("🟨") for m in messages) == 108
    assert messages[0].startswith("🟨 <b>X107</b>")         # the biggest leads


def test_the_turn_takes_the_pings_out_of_a_message_and_keeps_its_pushes(
        monkeypatch, channel, week):
    rows = [ev(at(3, 10), asset=FUNDS[0]), ev(at(3, 10), asset=FUNDS[1], size=4.0)]
    run(monkeypatch, channel, rows, run_at(3, 11), week)
    # After its 24 hours F0 turns high: its line becomes a push, silently.
    later = [dict(rows[0], tier="high", z=6.0, r=6.0 * SIGMA), rows[1]]
    run(monkeypatch, channel, later, run_at(4, 12), week)
    [message] = alerts(channel)
    assert message.startswith("🟨 <b>F0</b>") and message.endswith("⬜ <b>F1</b> · F1 +1.20% · 4.0×σ")
    run(monkeypatch, channel, later, datetime.fromtimestamp(NEXT, tz=timezone.utc), week)
    [message] = alerts(channel)
    assert message.startswith("🟨 <b>F0</b>") and "F1" not in message


def test_the_note_runs_by_time_and_by_size_inside_an_hour(monkeypatch, channel, week):
    rows = [ev(at(0, 10), asset=FUNDS[0], size=4.0), ev(at(0, 10), asset=FUNDS[1], size=5.0),
            ev(at(0, 9), asset=FUNDS[2], size=3.95, found=at(0, 11))]
    run(monkeypatch, channel, rows, run_at(0, 11), week)
    note = channel.note()
    assert note.index("F2") < note.index("F1") < note.index("F0")


def test_a_part_the_note_grows_is_silent(monkeypatch, channel, week):
    many = [ev(at(0, 1), asset=f"twelvedata:X{i}") for i in range(120)]
    rang = len(channel.rang)
    run(monkeypatch, channel, many, run_at(0, 2), week)
    assert len(channel.notes()) > 1
    assert len(channel.rings_since(rang)) == 1 and channel.rang[-1].startswith("⬜")


def test_news_of_one_hour_under_pushes_of_two_says_which_hour(monkeypatch, channel, week):
    monkeypatch.setattr(md, "calendar_context", lambda hour, cal: (
        "Nearby economic events (-2h+1h):\n     CPI"
        if datetime.fromtimestamp(hour, timezone.utc).hour == 9 else ""))
    run(monkeypatch, channel, [ev(at(1, 9), "high", asset=FUNDS[2]),
                               ev(at(1, 10), "major", asset=FUNDS[3], found=at(1, 11))],
        run_at(1, 11), week)
    [message] = alerts(channel)
    assert "Nearby economic events around 09:00 UTC (-2h+1h):\n     CPI" in message


# --- early ping deletion -------------------------------------------------------

def test_a_row_ping_is_deleted_after_40_hours_while_the_note_keeps_it(
        monkeypatch, channel, week):
    """A ROW ping older than PING_MAX_AGE_HOURS is proactively deleted so that
    the channel never shows struck-through leftovers (Telegram's 48-hour
    bot-deletion limit). The event stays in the digest note."""
    row = ev(at(0, 10))
    run(monkeypatch, channel, [row], run_at(0, 11), week)
    assert "Gold" in channel.note() and len(channel.pings()) == 1
    # 35 hours after discovery (run_at(0, 11) -> run_at(1, 22)): the ping is still there.
    run(monkeypatch, channel, [row], run_at(1, 22), week)
    assert len(channel.pings()) == 1
    # 41 hours after discovery (run_at(0, 11) -> run_at(2, 4)): the ping is gone, but the note still shows Gold.
    run(monkeypatch, channel, [row], run_at(2, 4), week)
    assert channel.pings() == []
    assert "Gold" in channel.note()


def test_a_push_is_not_affected_by_early_ping_deletion(monkeypatch, channel, week):
    """PUSH messages are not subject to early deletion - only ROW pings are."""
    push = ev(at(0, 10), "high")
    run(monkeypatch, channel, [push], run_at(0, 11), week)
    run(monkeypatch, channel, [push], run_at(2, 0), week)
    assert len(channel.pushes()) == 1


def test_a_multi_event_ping_message_keeps_younger_ping_when_older_expires(
        monkeypatch, channel, week):
    """When a ping message carries multiple events, expiring one edits the message
    to keep the younger event rather than discarding the whole message."""
    first = ev(at(0, 10))
    run(monkeypatch, channel, [first], run_at(0, 11), week)
    second = ev(at(1, 10), asset="coinbase:BTC-USD")
    run(monkeypatch, channel, [first, second], run_at(1, 11), week)
    # At run_at(2, 4): first is 41 hours old (expired), second is only 17 hours old.
    run(monkeypatch, channel, [first, second], run_at(2, 4), week)
    assert len(channel.pings()) == 1
    assert "Bitcoin" in channel.pings()[0]
    assert "Gold" not in channel.pings()[0]
    assert "Gold" in channel.note() and "Bitcoin" in channel.note()


# --- Telegram takes a message without answering (2026-11-03) ----------------------
# The 14:05 run's sends timed out, the run recorded them unsent, and Telegram put
# them up at 14:40 (ids 137, 138); the 15:05 run sent the push again (139). Now a
# send without an answer is written down, and the next runs read the channel for it.

def _held(monkeypatch, channel, week, readings, when):
    """One run whose first send Telegram holds without answering."""
    channel.hang = 1
    run(monkeypatch, channel, readings, when, week)
    assert channel.held and channel.hang == 0


def test_a_push_telegram_put_up_late_is_taken_in_not_sent_again(monkeypatch, channel, week):
    first = ev(at(0, 13), "high", size=6.5)
    _held(monkeypatch, channel, week, [first], run_at(0, 14))
    assert channel.pushes() == []
    late = channel.publish_late()                   # 14:40
    rang = len(channel.rang)
    corrected = ev(at(0, 13), "high", size=6.2)     # the bar revised before 15:05
    run(monkeypatch, channel, [corrected], run_at(0, 15), week)
    assert channel.pushes() == [channel.messages[late[0]]]
    assert "-1.86%" in channel.messages[late[0]] or "1.86%" in channel.messages[late[0]]
    assert channel.rings_since(rang) == []          # it rang once, at 14:40
    assert any(f"Found late message {late[0]}" in line for line in channel.found)
    assert channel.health == {}                     # the read copies are gone


def test_a_late_copy_that_appears_after_the_repeat_is_deleted(monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    _held(monkeypatch, channel, week, [push], run_at(0, 11))
    run(monkeypatch, channel, [push], run_at(0, 12), week)
    assert len(channel.pushes()) == 1               # not there yet: sent again, not lost
    late = channel.publish_late()
    run(monkeypatch, channel, [push], run_at(0, 13), week)
    assert len(channel.pushes()) == 1 and late[0] not in channel.messages
    assert any(f"Deleted late duplicate {late[0]}" in line for line in channel.found)


def test_a_send_that_never_appears_is_sent_again_and_forgotten_in_a_week(
        monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    _held(monkeypatch, channel, week, [push], run_at(0, 11))
    channel.held = []                               # Telegram dropped it after all
    run(monkeypatch, channel, [push], run_at(0, 12), week)
    assert channel.pushes() == [md.format_push(push, LABELS)]
    assert len(week["telegram_ledger"]["pending"]) == 1
    run(monkeypatch, channel, [push], run_at(8, 12), week)
    assert week["telegram_ledger"]["pending"] == []


def test_what_is_not_ours_is_read_and_left_alone(monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    _held(monkeypatch, channel, week, [push], run_at(0, 11))
    by_hand = channel.post("<b>A note from the owner</b>")
    pin = channel.post("pinned a message")
    channel.unforwardable.add(pin)
    passed_on = channel.post("🟨 an alert of another channel's, forwarded here")
    channel.dates[passed_on] = 0                    # it carries its origin's date
    run(monkeypatch, channel, [push], run_at(0, 12), week)
    assert channel.messages[by_hand] == "<b>A note from the owner</b>"
    assert pin in channel.messages and passed_on in channel.messages
    assert len(channel.pushes()) == 1


def test_the_first_unanswered_request_stops_the_run_and_its_ping_goes_next_run(
        monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    ping = ev(at(0, 10), asset="coinbase:BTC-USD")
    channel.hang = 1
    channel.calls.clear()
    run(monkeypatch, channel, [push, ping], run_at(0, 11), week)
    asked = channel.calls[channel.calls.index("send"):]
    assert asked == ["send"]                        # nothing more, after the silence
    channel.held = []
    run(monkeypatch, channel, [push, ping], run_at(0, 12), week)
    assert len(channel.pushes()) == 1 and len(channel.pings()) == 1
    assert "Bitcoin" in channel.note()


def test_a_note_telegram_put_up_late_is_the_weeks_note(monkeypatch, channel):
    state = {}
    old = [ev(OPEN - timedelta(days=3))]
    channel.hang = 1
    run(monkeypatch, channel, old, OPEN, state)
    assert channel.notes() == []
    channel.publish_late()
    run(monkeypatch, channel, old, OPEN + timedelta(hours=1), state)
    assert len(channel.notes()) == 1


def test_a_note_sent_again_before_its_late_copy_appears_stays_single(monkeypatch, channel):
    state = {}
    old = [ev(OPEN - timedelta(days=3))]
    channel.hang = 1
    run(monkeypatch, channel, old, OPEN, state)
    run(monkeypatch, channel, old, OPEN + timedelta(hours=1), state)
    late = channel.publish_late()
    run(monkeypatch, channel, old, OPEN + timedelta(hours=2), state)
    assert len(channel.notes()) == 1 and late[0] not in channel.messages


def test_a_ping_that_appears_after_its_week_closed_goes_with_the_week(
        monkeypatch, channel, week):
    ping = ev(at(4, 18))                            # Friday, before the week's last close
    _held(monkeypatch, channel, week, [ping], run_at(4, 19))
    later = datetime.fromtimestamp(NEXT, tz=timezone.utc)
    run(monkeypatch, channel, [ping], later, week)  # the week closes
    late = channel.publish_late()
    run(monkeypatch, channel, [ping], later + timedelta(hours=1), week)
    assert late[0] not in channel.messages


def test_a_late_copy_across_a_detector_update_is_taken_in(monkeypatch, channel, week):
    # The update keeps the week, so the push Telegram held is still its own.
    push = ev(at(0, 10), "high")
    _held(monkeypatch, channel, week, [push], run_at(0, 11))
    monkeypatch.setattr(jumps, "detector_version", lambda root=None: "v2")
    run(monkeypatch, channel, [push], run_at(0, 12), week)
    late = channel.publish_late()
    run(monkeypatch, channel, [push], run_at(0, 13), week)
    assert late[0] in channel.messages and len(channel.pushes()) == 1


def test_an_edit_telegram_did_not_answer_is_asserted_again(monkeypatch, channel, week):
    run(monkeypatch, channel, [ev(at(0, 10), "high", size=6.5)], run_at(0, 11), week)
    [mid] = [k for k, t in channel.messages.items() if t in channel.pushes()]
    channel.hang_edit = 1
    run(monkeypatch, channel, [ev(at(0, 10), "high", size=6.2)], run_at(0, 12), week)
    run(monkeypatch, channel, [ev(at(0, 10), "high", size=6.3)], run_at(0, 13), week)
    newest = channel.messages[mid]
    channel.apply_late_edits()                      # the older edit lands last
    assert channel.messages[mid] != newest
    run(monkeypatch, channel, [ev(at(0, 10), "high", size=6.3)], run_at(0, 14), week)
    assert channel.messages[mid] == newest


def test_without_a_way_to_read_the_channel_it_says_so_and_sends_again(
        monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    channel.unforwardable.update(range(1, 50))      # the channel protects its content
    _held(monkeypatch, channel, week, [push], run_at(0, 11))
    told = channel.news + channel.found
    late = channel.publish_late()
    run(monkeypatch, channel, [push], run_at(0, 12), week)
    assert week["telegram_ledger"]["read_ok"] is False
    assert any("cannot be forwarded" in n for n in told)
    assert any("duplicate to delete by hand" in n for n in channel.found)
    assert late[0] in channel.messages              # nothing deleted unread


def test_without_a_health_chat_it_sends_again_as_before(monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    channel.hang = 1
    run(monkeypatch, channel, [push], run_at(0, 11), week, telegram_health_chat_id="")
    run(monkeypatch, channel, [push], run_at(0, 12), week, telegram_health_chat_id="")
    assert len(channel.pushes()) == 1


# --- a run whose state was not saved (its commit failed) ------------------------
# The next run starts from the state before it, knowing nothing of what it sent.

def _lost(week, saved):
    week.clear()
    week.update(copy.deepcopy(saved))


def test_a_lost_runs_push_is_taken_over_not_sent_again(monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    saved = copy.deepcopy(week)
    run(monkeypatch, channel, [push], run_at(0, 11), week)
    rang = len(channel.rang)
    _lost(week, saved)
    run(monkeypatch, channel, [push], run_at(0, 12), week)
    assert len(channel.pushes()) == 1
    assert channel.rings_since(rang) == []
    assert any("Took over" in line for line in channel.news)


def test_a_lost_runs_push_corrected_since_is_replaced(monkeypatch, channel, week):
    saved = copy.deepcopy(week)
    run(monkeypatch, channel, [ev(at(0, 10), "high", size=6.5)], run_at(0, 11), week)
    [stale] = [k for k, t in channel.messages.items() if t in channel.pushes()]
    _lost(week, saved)
    run(monkeypatch, channel, [ev(at(0, 10), "high", size=6.2)], run_at(0, 12), week)
    assert len(channel.pushes()) == 1 and stale not in channel.messages
    assert any(f"Deleted {stale}" in line for line in channel.found)


def test_a_lost_runs_ping_is_taken_over_and_the_owners_post_left(monkeypatch, channel, week):
    ping = ev(at(0, 10))
    saved = copy.deepcopy(week)
    run(monkeypatch, channel, [ping], run_at(0, 11), week)
    mine = channel.post("Back on Monday")
    rang = len(channel.rang)
    _lost(week, saved)
    run(monkeypatch, channel, [ping], run_at(0, 12), week)
    assert len(channel.pings()) == 2                # the ping, and the owner's post
    assert channel.messages[mine] == "Back on Monday"
    assert channel.rings_since(rang) == []


def test_a_state_started_afresh_deletes_nothing(monkeypatch, channel):
    old = channel.post("🟨 <b>GLD</b> · Gold +1.80% · 6.0×σ")
    run(monkeypatch, channel, [ev(OPEN - timedelta(days=3))], OPEN, {})
    assert old in channel.messages


def test_a_stray_is_kept_when_delivery_did_not_run_through(monkeypatch, channel, week):
    push = ev(at(0, 10), "high")
    saved = copy.deepcopy(week)
    run(monkeypatch, channel, [push], run_at(0, 11), week)
    [mid] = [k for k, t in channel.messages.items() if t in channel.pushes()]
    _lost(week, saved)

    def broken(*a, **k):
        raise RuntimeError("delivery broke part way")

    monkeypatch.setattr(md, "_pass", broken)
    with pytest.raises(RuntimeError):
        run(monkeypatch, channel, [push], run_at(0, 12), week)
    assert ledger.settle_strays(cfg(channel)) == []   # as __main__ goes on after it
    assert mid in channel.messages


def test_a_stray_telegram_will_no_longer_delete_is_struck_through(monkeypatch, channel, week):
    saved = copy.deepcopy(week)
    run(monkeypatch, channel, [ev(at(0, 10), "high", size=6.5)], run_at(0, 11), week)
    [stale] = [k for k, t in channel.messages.items() if t in channel.pushes()]
    channel.refuse_delete.add(stale)                # past Telegram's 48 hours
    _lost(week, saved)
    run(monkeypatch, channel, [ev(at(0, 10), "high", size=6.2)], run_at(0, 12), week)
    assert channel.messages[stale].startswith("<s>")
    assert any(f"Struck through {stale}" in line for line in channel.found)


# --- what tells one message from another ----------------------------------------
# A message is the bot's by its id in the state; by its whole text when Telegram
# did not answer; and, sent by a run whose state was lost, by its first line
# (telegram_ledger.claim).

def _rows(n, hour=1):
    return [ev(at(0, hour), asset=f"twelvedata:X{i}") for i in range(n)]


def test_a_lost_runs_note_part_is_taken_over_when_the_note_has_grown_since(
        monkeypatch, channel, week):
    # The lost run grew the note to two parts; the next has three. Every part's
    # first line is the same, so its second part is still recognised.
    run(monkeypatch, channel, _rows(10), run_at(0, 2), week)
    saved = copy.deepcopy(week)
    run(monkeypatch, channel, _rows(70), run_at(0, 3), week)
    assert len(channel.notes()) == 2
    stray = max(m for m, t in channel.messages.items() if "<b>Digest</b>" in t)
    _lost(week, saved)
    run(monkeypatch, channel, _rows(150), run_at(0, 4), week)
    notes = [m for m, t in sorted(channel.messages.items()) if "<b>Digest</b>" in t]
    assert len(notes) == 3 and stray in notes
    assert [channel.messages[m].split("\n")[1].split(" · ")[0] for m in notes] == [
        "Part 1 of 3", "Part 2 of 3", "Part 3 of 3"]              # in channel order
    assert any(f"Took over {stray}" in line for line in channel.news)


def test_two_identical_events_are_two_messages_each_kept_by_its_own(
        monkeypatch, channel, week):
    # Gold +1.80% at 6.0σ twice, 30 hours apart: two events, two pings saying
    # the same - each tracked by its message id, so a correction to the first
    # edits only the first.
    first, second = ev(at(0, 10)), ev(at(1, 16))
    run(monkeypatch, channel, [first], run_at(0, 11), week)
    run(monkeypatch, channel, [first, second], run_at(1, 17), week)
    pings = sorted(m for m, t in channel.messages.items() if t in channel.pings())
    assert len(pings) == 2 and channel.messages[pings[0]] == channel.messages[pings[1]]
    run(monkeypatch, channel, [ev(at(0, 10), size=5.0), second], run_at(1, 18), week)
    assert channel.messages[pings[0]] != channel.messages[pings[1]]
    assert channel.messages[pings[1]] == md.ping_line(second, LABELS)


def test_a_lost_runs_ping_identical_to_an_earlier_one_is_taken_over(
        monkeypatch, channel, week):
    # The earlier ping is known by its id, so only the lost run's copy is a
    # stray: it is taken over, not sent again, and the earlier one is untouched.
    first, second = ev(at(0, 10)), ev(at(1, 16))
    run(monkeypatch, channel, [first], run_at(0, 11), week)
    [known] = [m for m, t in channel.messages.items() if t in channel.pings()]
    saved = copy.deepcopy(week)
    run(monkeypatch, channel, [first, second], run_at(1, 17), week)
    rang = len(channel.rang)
    _lost(week, saved)
    run(monkeypatch, channel, [first, second], run_at(1, 18), week)
    assert len(channel.pings()) == 2 and known in channel.messages
    assert channel.rings_since(rang) == []

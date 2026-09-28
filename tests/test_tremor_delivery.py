from datetime import date, datetime, timedelta, timezone
import os

import pandas as pd

import pytest

from price_monitor import follow_up as follow_up_module
from price_monitor import tremor_delivery as md
from price_monitor.config import Config
from price_monitor.notifier import TelegramError

HOUR = 3600
NOW = datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc)   # a Saturday, inside the weekly note's opening window
LABELS = {"twelvedata:GLD": "Gold", "coinbase:BTC-USD": "Bitcoin"}

# The note that is open at NOW. A digest row names the note it joins, and that
# note was opened at the START of the period covering it.
from tremor import routing                                       # noqa: E402

SLOT = routing.digest_slot(int(NOW.timestamp()))


def notes(sender):
    """The digest notes among what was sent, later parts of a long one included.

    A later part opens with an event line like a push does, so it is the header
    or the part marker that tells them apart.
    """
    return [t for t in sender.texts if "<b>Digest</b>" in t or "<i>part " in t]


def alerts(sender):
    """The pushes: everything that is not part of a note."""
    written = notes(sender)
    return [t for t in sender.texts if t not in written]


# Where a push's record goes. Without this the suite writes its fixtures into
# the repository's real alerts log, which is how ninety-six imaginary Gold
# alerts came to be committed. The sent map is the same: a successful send
# now writes state.json immediately, so the tests must not touch data/state.json.
_STATE_PATH = ""


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Nothing here reaches Telegram or reads the real calendar archive. A
    stranded ping used to be deleted through the real API - two hundred HTTPS
    round trips in one test - and every row parsed the 180,000-event archive.
    A test that cares about either puts its own stand-in over these."""
    monkeypatch.setattr("price_monitor.notifier.delete_telegram_message",
                        lambda *a, **k: False)
    monkeypatch.setattr(follow_up_module, "delete_telegram_message",
                        lambda *a, **k: False)
    monkeypatch.setattr(md, "edit_telegram_message", lambda *a, **k: None)
    monkeypatch.setattr(follow_up_module, "edit_telegram_message", lambda *a, **k: None)
    monkeypatch.setattr(md, "_calendar", lambda cfg: None)


@pytest.fixture(autouse=True)
def state_path(tmp_path):
    global _STATE_PATH
    _STATE_PATH = str(tmp_path / "state.json")
    yield
    _STATE_PATH = ""


def cfg(**over):
    base = dict(telegram_bot_token="t", telegram_chat_id="c",
                tremor_alerts_muted=False, state_path=_STATE_PATH)
    return Config(**(base | over))


def event(**over):
    """A jump event as tremor.jumps.for_delivery writes it: |r| / sigma_lt = 7.0."""
    base = dict(event_id="e1", asset_id="twelvedata:GLD", block="precious_metals",
                hour_utc=int(NOW.timestamp()) - HOUR,
                tier="major", basis="jump", channel="push", r=0.021,
                sigma_lt=0.003, overnight=False, digest_slot=None)
    return base | over


class Sent:
    """Stands in for notifier.send_telegram_message."""

    def __init__(self, fail=False):
        self.texts, self.fail = [], fail

    def __call__(self, token, chat, text, *a, **k):
        if self.fail:
            raise TelegramError("nope")
        self.texts.append(text)
        return len(self.texts)


class Edited:
    """Stands in for notifier.edit_telegram_message."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, token, chat, message_id, text, *a, **k):
        if self.fail:
            raise TelegramError("nope")
        self.calls.append((message_id, text))


@pytest.fixture
def sender(monkeypatch):
    s = Sent()
    monkeypatch.setattr(md, "send_telegram_message", s)
    monkeypatch.setattr(md, "_labels", lambda: LABELS)
    return s


@pytest.fixture
def editor(monkeypatch):
    """Both editors: the notes are edited from here, the pushes from follow_up."""
    e = Edited()
    monkeypatch.setattr(md, "edit_telegram_message", e)
    monkeypatch.setattr(follow_up_module, "edit_telegram_message", e)
    return e


def deliver(monkeypatch, events, state=None, now=NOW, **over):
    monkeypatch.setattr(md, "load_events", lambda c: events)
    state = {} if state is None else state
    return md.maybe_deliver(cfg(**over), state, now), state


def test_nothing_goes_out_while_muted(monkeypatch, sender):
    sent, _ = deliver(monkeypatch, [event()], tremor_alerts_muted=True)
    assert sent == 0 and sender.texts == []


def test_a_push_goes_out_once(monkeypatch, sender):
    _, state = deliver(monkeypatch, [event()])
    assert len(alerts(sender)) == 1 and "Gold" in alerts(sender)[0]

    # the same run again sends nothing: the id is remembered, and the note that
    # was opened alongside it has not changed
    again, _ = deliver(monkeypatch, [event()], state=state)
    assert again == 0 and len(alerts(sender)) == 1


def test_stale_events_are_not_delivered(monkeypatch, sender):
    # The events table holds the whole history, so without this the first run
    # after the mute comes off would deliver five years of alerts at once.
    old = event(hour_utc=int(NOW.timestamp()) - (md.STALE_AFTER_HOURS + 1) * HOUR)
    deliver(monkeypatch, [old])
    assert alerts(sender) == []


def test_an_event_from_the_future_is_not_delivered(monkeypatch, sender):
    ahead = event(hour_utc=int(NOW.timestamp()) + HOUR)
    deliver(monkeypatch, [ahead])
    assert alerts(sender) == []


def test_the_note_is_opened_even_before_it_has_anything_in_it(monkeypatch, sender):
    # It is opened at the START of the period it covers, so the reader has one
    # message to watch and every event after that arrives as a silent edit.
    elsewhere = event(event_id="old", channel="digest", tier="noticeable",
                      hour_utc=SLOT - 30 * 24 * HOUR)
    deliver(monkeypatch, [elsewhere])
    assert len(notes(sender)) == 1
    assert "Nothing so far" in notes(sender)[0]
    assert "updated as moves are found" in notes(sender)[0]


def test_a_move_joins_the_note_that_is_already_open(monkeypatch, sender):
    row = event(event_id="d1", channel="digest", tier="noticeable")
    deliver(monkeypatch, [row])
    assert "Gold" in notes(sender)[0]


def test_a_move_from_before_the_note_opened_is_not_in_it(monkeypatch, sender):
    # Before the previous week's period too, which a note that never opened
    # would otherwise carry into this one.
    stale = event(event_id="d1", channel="digest", tier="noticeable",
                  hour_utc=SLOT - 8 * 24 * HOUR)
    deliver(monkeypatch, [stale])
    assert len(notes(sender)) == 1 and "Gold" not in notes(sender)[0]


def test_the_note_is_one_message_for_many_events(monkeypatch, sender):
    rows = [event(event_id=f"d{i}", channel="digest", tier="noticeable",
                  hour_utc=SLOT + i * HOUR) for i in range(3)]
    deliver(monkeypatch, rows)
    assert len(notes(sender)) == 1
    assert notes(sender)[0].count(md.TIER_EMOJI["noticeable"]) == 3


def test_a_failed_send_is_retried_rather_than_lost(monkeypatch):
    # An event is marked sent only once its message has actually gone.
    failing = Sent(fail=True)
    monkeypatch.setattr(md, "send_telegram_message", failing)
    monkeypatch.setattr(md, "_labels", lambda: LABELS)
    sent, state = deliver(monkeypatch, [event()])
    assert sent == 0
    assert not state[md.STATE_KEY][md._SENT]
    # The note counts as opened only once a message is behind it.
    assert not state[md.STATE_KEY][md.DIGEST_STATE][str(SLOT)]["ids"]
    assert not os.path.exists(_STATE_PATH)

    working = Sent()
    monkeypatch.setattr(md, "send_telegram_message", working)
    deliver(monkeypatch, [event()], state=state)
    assert len(alerts(working)) == 1


def test_a_successful_send_is_written_to_disk_immediately(monkeypatch, sender):
    from price_monitor.state import load_state

    deliver(monkeypatch, [event()])
    on_disk = load_state(_STATE_PATH)
    assert "e1" in on_disk[md.STATE_KEY][md._SENT]


def test_an_event_promoted_to_a_push_later_is_still_sent(monkeypatch, sender):
    # A once-a-year move is routed to the digest while its retention is unknown
    # and becomes a push six bars later, when the answer arrives. A high-water
    # mark on the hour would have stepped over it in between.
    slot = int(NOW.timestamp()) + 3 * HOUR          # its digest has not run yet
    _, state = deliver(monkeypatch, [event(channel="digest", digest_slot=slot,
                                           retention_settled=None)])
    assert not state[md.STATE_KEY][md._SENT]

    deliver(monkeypatch, [event(channel="push")], state=state)
    assert len(alerts(sender)) == 1


def test_the_state_does_not_grow_without_bound(monkeypatch, sender):
    ancient = {"old": int(NOW.timestamp()) - 400 * 24 * HOUR}
    state = {md.STATE_KEY: {md._SENT: dict(ancient)}}
    _, state = deliver(monkeypatch, [event()], state=state)
    assert "old" not in state[md.STATE_KEY][md._SENT]
    assert "e1" in state[md.STATE_KEY][md._SENT]


def test_the_severity_leads_the_digest(monkeypatch, sender):
    # A digest read only as far as its notification preview should still
    # deliver its most important line.
    rows = [
        event(event_id="a", channel="digest", tier="noticeable", digest_slot=SLOT),
        event(event_id="b", channel="digest", tier="major",
              asset_id="coinbase:BTC-USD", digest_slot=SLOT),
    ]
    deliver(monkeypatch, rows)
    text = notes(sender)[0]
    assert text.index("Bitcoin") < text.index("Gold")


def test_a_long_digest_is_split_within_telegrams_limit(monkeypatch, sender):
    rows = [event(event_id=f"d{i}", channel="digest", tier="noticeable",
                  hour_utc=int(NOW.timestamp()) - HOUR,
                  digest_slot=SLOT) for i in range(200)]
    deliver(monkeypatch, rows)
    assert len(notes(sender)) > 1
    assert all(len(t) <= 4096 for t in sender.texts)
    assert "part 1 of" in notes(sender)[0]


def test_labels_fall_back_to_the_ticker(monkeypatch, sender):
    monkeypatch.setattr(md, "_labels", lambda: {})
    deliver(monkeypatch, [event()])
    assert "GLD" in sender.texts[0]


def test_a_missing_parquet_file_is_not_an_error(tmp_path):
    quiet = cfg(tremor_events_path=str(tmp_path / "nope.parquet"))
    assert md.load_events(quiet) == []
    assert md.maybe_deliver(quiet, {}) == 0


def _cal(rows):
    """rows: (iso date, country, title, impact)."""
    return [{"date": d, "country": c, "title": t, "impact": i,
             "actual": "", "forecast": "", "previous": ""} for d, c, t, i in rows]


def test_a_push_names_the_scheduled_news_behind_it():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "USD", "Core CPI m/m", "High"),
                ("2026-06-10T13:00:00+00:00", "USD", "Fed Chair Speaks", "High")])
    out = md.calendar_context(hour, cal)
    assert out.startswith("Nearby economic events (-2h+1h):")
    assert "USD Core CPI m/m" in out and "USD Fed Chair Speaks" in out


def test_a_push_with_nothing_scheduled_prints_no_calendar_line_at_all():
    # It used to say "none scheduled", and the statistic behind that is real:
    # 55% of pushes in the record have no Medium or High release in the window.
    # Which is exactly why the line went - on more than half of all messages it
    # was a line saying nothing had happened, and a line that usually says
    # nothing stops being read. The absence is carried by the absence.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    elsewhere = _cal([("2026-05-01T12:00:00+00:00", "USD", "Old CPI", "High")])
    assert md.calendar_context(hour, elsewhere) == ""


def test_an_empty_archive_claims_nothing_rather_than_claiming_silence():
    # An empty archive cannot tell "nothing was scheduled" from "nothing was
    # loaded". Now that a quiet window prints nothing either, the two agree on
    # the output - but for different reasons, and this is the one that would
    # have to change first if the line ever came back.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    assert md.calendar_context(hour, []) == ""


def test_low_impact_news_is_not_named():
    # High and Medium are shown, the same two the Saturday calendar shows. Low
    # is dominated by bank holidays and minor prints, and naming those would
    # turn the most important line of the most important message into noise.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    only_low = _cal([("2026-06-10T12:30:00+00:00", "CHF", "Bank Holiday", "Low")])
    assert md.calendar_context(hour, only_low) == ""

    medium = _cal([("2026-06-10T12:45:00+00:00", "EUR", "Trade Balance", "Medium")])
    assert "Trade Balance" in md.calendar_context(hour, medium)


def test_each_named_release_carries_its_impact_colour():
    # The same circles the Saturday calendar uses, against the squares a move
    # carries: the shape says which kind of thing the line is.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "USD", "CPI", "High"),
                ("2026-06-10T12:45:00+00:00", "EUR", "Trade Balance", "Medium")])
    out = md.calendar_context(hour, cal)
    assert "\U0001F534 \U0001F1FA\U0001F1F8 USD CPI" in out
    assert "\U0001F7E0 \U0001F1EA\U0001F1FA EUR Trade Balance" in out


def test_a_release_carries_its_country_flag_beside_the_code():
    # The flag is what is caught at a glance; the code is what makes it certain.
    # Several of these flags are the same two colours in nearly the same
    # arrangement at the size a phone draws them.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "AUD", "Employment Change", "High"),
                ("2026-06-10T12:40:00+00:00", "NZD", "Official Cash Rate", "High"),
                ("2026-06-10T12:50:00+00:00", "All", "G7 Meetings", "High")])
    out = md.calendar_context(hour, cal)
    assert "\U0001F1E6\U0001F1FA AUD" in out
    assert "\U0001F1F3\U0001F1FF NZD" in out
    # No country at all is the source's own answer, and a globe is the honest
    # rendering of it rather than a stand-in for a missing flag.
    assert "\U0001F310 All" in out


def test_a_currency_with_no_flag_still_prints_its_code():
    # The source can add a currency whenever it likes and the message must not
    # sprout a placeholder box when it does.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "XYZ", "Rate Decision", "High")])
    out = md.calendar_context(hour, cal)
    assert "XYZ Rate Decision" in out


def test_news_outside_the_window_is_not_claimed_as_context():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T09:00:00+00:00", "USD", "Old News", "High"),
                ("2026-06-10T16:30:00+00:00", "USD", "Much Later News", "High")])
    out = md.calendar_context(hour, cal)
    assert "Old News" not in out and "Much Later News" not in out


def test_a_release_just_after_the_move_is_named():
    # The window used to end exactly where the move did, which excluded the
    # releases a reader would blame first: a print five minutes after the hour
    # closed is a cause, not a coincidence.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T14:30:00+00:00", "USD", "FOMC Statement", "High")])
    assert "FOMC Statement" in md.calendar_context(hour, cal)


def test_a_crowded_window_is_listed_in_full():
    # The High filter is what keeps the line short. On a busy morning the tail
    # is the half worth reading, so it is not traded away to save two lines.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([(f"2026-06-10T12:{m:02d}:00+00:00", "USD", f"Print {m}", "High")
                for m in range(0, 60, 10)])
    out = md.calendar_context(hour, cal)
    assert out.count("\U0001F534") == 6
    assert "more" not in out
    for m in range(0, 60, 10):
        assert f"Print {m}" in out


def test_the_events_are_listed_in_the_order_they_happened():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T14:30:00+00:00", "USD", "Later", "High"),
                ("2026-06-10T12:30:00+00:00", "USD", "Earlier", "High")])
    out = md.calendar_context(hour, cal)
    assert out.index("Earlier") < out.index("Later")


def test_a_missing_calendar_never_costs_the_alert():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    assert md.calendar_context(hour, None) == ""
    text = md.format_push({"hour_utc": hour, "asset_id": "a:SPY", "tier": "major",
                           "basis": "jump", "r": 0.02, "sigma_lt": 0.004}, {}, None)
    assert text.splitlines()[0].endswith("+2.00% · 5.0×σ")


def test_the_comparison_is_skipped_when_the_yardstick_is_missing():
    # sigma_LT is NaN through an instrument's first 720 bars, and a live event
    # there must still render rather than raise.
    event = {"asset_id": "twelvedata:SPY", "tier": "major", "basis": "jump",
             "hour_utc": 1767225600, "r": 0.02, "sigma_lt": float("nan")}
    text = md.format_push(event, {})
    assert "×σ" not in text and "+2.00%" in text


# --- when the next check-in is due -----------------------------------------
#
# The horizons are counted in the instrument's own bars, so the wait for one is
# a question about the trading calendar rather than about the clock. Counting it
# in hours - which it used to - told a Friday-afternoon push it was "coming
# within the hour" all through the weekend, because the hours passed and the
# bars did not.


# --- the note is written into, not written up -------------------------------
#
# The digest is opened at the start of the period it covers and edited in place
# as events are found. Telegram notifies on a new message and stays silent on an
# edit, so the reader is interrupted twice a week and everything after that
# arrives quietly in a message they already have.

def digest_row(**over):
    return event(event_id="d1", channel="digest", tier="noticeable") | over


def test_a_later_move_edits_the_open_note_rather_than_sending_another(
        monkeypatch, sender, editor):
    _, state = deliver(monkeypatch, [digest_row()])
    assert len(notes(sender)) == 1 and editor.calls == []

    second = digest_row(event_id="d2", asset_id="coinbase:BTC-USD")
    deliver(monkeypatch, [digest_row(), second], state=state)
    assert len(notes(sender)) == 1          # nothing new arrived on the phone
    assert len(editor.calls) == 1
    assert "Bitcoin" in editor.calls[0][1] and "Gold" in editor.calls[0][1]


def test_a_note_that_has_not_changed_is_not_edited(monkeypatch, sender, editor):
    # Telegram rejects an edit whose text matches what is already there, and
    # most hours change nothing.
    _, state = deliver(monkeypatch, [digest_row()])
    deliver(monkeypatch, [digest_row()], state=state)
    deliver(monkeypatch, [digest_row()], state=state)
    assert editor.calls == []


def test_a_note_is_forgotten_once_nothing_about_it_can_change(
        monkeypatch, sender, editor):
    _, state = deliver(monkeypatch, [digest_row()])
    assert state[md.STATE_KEY][md.DIGEST_STATE]
    much_later = NOW + timedelta(hours=md.DIGEST_TRACK_HOURS + 1)
    _, state = deliver(monkeypatch, [digest_row()], state=state, now=much_later)
    assert str(SLOT) not in state[md.STATE_KEY][md.DIGEST_STATE]


def test_a_failed_edit_is_retried_rather_than_lost(monkeypatch, sender):
    failing = Edited(fail=True)
    monkeypatch.setattr(md, "edit_telegram_message", failing)
    _, state = deliver(monkeypatch, [digest_row()])
    # The bar healed: the move the row was written with has changed.
    deliver(monkeypatch, [digest_row(r=0.024)], state=state)

    working = Edited()
    monkeypatch.setattr(md, "edit_telegram_message", working)
    deliver(monkeypatch, [digest_row(r=0.024)], state=state)
    assert len([t for _, t in working.calls if "Digest" in t]) == 1


def test_a_move_carries_its_tier_as_a_colour(monkeypatch, sender):
    # Squares for moves against the calendar's circles, so the shape says which
    # kind of thing a coloured line is before the words do.
    for tier, square in md.TIER_EMOJI.items():
        assert md.describe(event(tier=tier), LABELS).startswith(square)


def test_a_note_that_loses_a_part_does_not_leave_a_stale_one(monkeypatch, sender, editor):
    # A recompute that no longer produces an event takes its lines with it, and
    # Telegram cannot delete a message - so the surplus part is emptied instead
    # of being left saying "part 3 of 5" under a note that now has two.
    many = [event(event_id=f"d{i}", channel="digest", tier="noticeable",
                  hour_utc=int(NOW.timestamp()) - HOUR, digest_slot=SLOT)
            for i in range(200)]
    _, state = deliver(monkeypatch, many)
    parts = len(notes(sender))
    assert parts > 2

    deliver(monkeypatch, many[:2], state=state)
    emptied = [t for _, t in editor.calls if md._EMPTIED_PART in t]
    assert len(emptied) == parts - 1


def test_an_hour_that_has_not_happened_yet_is_not_written_down(monkeypatch, sender):
    # A bar has to close before it is scored, so this should not arise - but a
    # clock skew must not put tomorrow in today's note.
    ahead = digest_row(hour_utc=int(NOW.timestamp()) + 2 * HOUR)
    deliver(monkeypatch, [ahead])
    assert "Gold" not in notes(sender)[0]


# --- the opening hour is the one thing that cannot slip ---------------------
#
# The whole arrangement is worth having because the two interruptions land at
# noon on a Tuesday and a Friday. A note opened whenever the system happened to
# next run is an ordinary unscheduled buzz wearing a schedule's clothes.

LATE = datetime.fromtimestamp(SLOT + 10 * HOUR, tz=timezone.utc)


def test_a_note_is_not_opened_hours_after_its_hour(monkeypatch, sender):
    deliver(monkeypatch, [event(channel="digest")], now=LATE)
    assert notes(sender) == []


def test_one_missed_run_does_not_cost_the_note(monkeypatch, sender):
    # The trigger is an external service. Three hours of grace, because 15:00 is
    # still an afternoon and 22:00 is not.
    late_but_ok = datetime.fromtimestamp(SLOT + 3 * HOUR, tz=timezone.utc)
    deliver(monkeypatch, [event(channel="digest")], now=late_but_ok)
    assert len(notes(sender)) == 1


def test_a_period_whose_note_never_opened_is_carried_into_the_next(monkeypatch, sender):
    # Both halves have to be true at once: the buzz is always at noon, and no
    # move is silently dropped for want of a scheduler.
    missed = event(event_id="d1", channel="digest", tier="noticeable",
                   hour_utc=SLOT + 5 * HOUR)
    _, state = deliver(monkeypatch, [missed], now=LATE)
    assert notes(sender) == []

    next_slot = routing.next_digest_slot(SLOT)
    opens = datetime.fromtimestamp(next_slot, tz=timezone.utc)
    deliver(monkeypatch, [missed], state=state, now=opens)
    assert len(notes(sender)) == 1
    assert "Gold" in notes(sender)[0]


def test_the_carried_note_says_which_period_it_covers(monkeypatch, sender):
    # The header states the period the note speaks for, not the day it was
    # posted, because those come apart exactly when it matters.
    elsewhere = [event(channel="digest", hour_utc=SLOT - 40 * 24 * HOUR)]
    _, state = deliver(monkeypatch, elsewhere, now=LATE)
    opens = datetime.fromtimestamp(routing.next_digest_slot(SLOT), tz=timezone.utc)
    deliver(monkeypatch, elsewhere, state=state, now=opens)
    # Longer than its own period: it picked up the one that never opened.
    covers = datetime.fromtimestamp(SLOT, tz=timezone.utc)
    ends = datetime.fromtimestamp(routing.next_digest_slot(int(opens.timestamp())),
                                  tz=timezone.utc)
    last = ends - timedelta(hours=1)      # the last day it can hold an hour of
    assert f"{md.format_day(covers)} to {md.format_day(last)}" in notes(sender)[0]
    assert (ends - covers).days > (ends - opens).days


def test_an_ordinary_note_covers_only_its_own_period(monkeypatch, sender):
    elsewhere = [event(channel="digest", hour_utc=SLOT - 40 * 24 * HOUR)]
    opens = datetime.fromtimestamp(SLOT, tz=timezone.utc)
    _, state = deliver(monkeypatch, elsewhere, now=opens)
    later = datetime.fromtimestamp(routing.next_digest_slot(SLOT), tz=timezone.utc)
    deliver(monkeypatch, elsewhere, state=state, now=later)
    ends = datetime.fromtimestamp(routing.next_digest_slot(int(later.timestamp())),
                                  tz=timezone.utc) - timedelta(hours=1)
    # Only its own stretch, because the note before it did open.
    assert f"{md.format_day(later)} to {md.format_day(ends)}" in notes(sender)[1]


def test_a_note_whose_first_post_failed_does_not_cover_its_period(monkeypatch):
    # It is a post that will be retried, not a note the reader has - so the next
    # note must still pick those rows up if it never lands.
    failing = Sent(fail=True)
    monkeypatch.setattr(md, "send_telegram_message", failing)
    monkeypatch.setattr(md, "_labels", lambda: LABELS)
    row = event(event_id="d1", channel="digest", tier="noticeable",
                hour_utc=SLOT + 2 * HOUR)
    _, state = deliver(monkeypatch, [row], now=datetime.fromtimestamp(SLOT, tz=timezone.utc))

    working = Sent()
    monkeypatch.setattr(md, "send_telegram_message", working)
    opens = datetime.fromtimestamp(routing.next_digest_slot(SLOT), tz=timezone.utc)
    deliver(monkeypatch, [row], state=state, now=opens)
    assert any("Gold" in t for t in working.texts)


def test_a_move_that_belongs_to_no_push_keeps_its_row(monkeypatch, sender):
    deliver(monkeypatch, [digest_row(folded_into="")])
    assert "Gold" in notes(sender)[0]


# --- saying it in terms nobody needs statistics for -------------------------


def test_the_headline_leads_with_the_rarity_the_ticker_and_the_move():
    # The rarity is a colour so it reads before any word does; the ticker is
    # what a reader types into a chart; the move is the number they came for and
    # it used to be on the second line.
    text = md.describe(event(asset_id="twelvedata:GLD"), LABELS)
    first = text.split("\n")[0]
    assert first == md.TIER_EMOJI["major"] + " <b>GLD</b> · Gold +2.10% · 7.0×σ"


def test_the_hour_is_the_last_line_and_is_bold():
    # Everything above it is what happened; this is when. Bold because it is the
    # one thing a reader cross-checks against a chart.
    text = md.describe(event(asset_id="twelvedata:GLD"), LABELS)
    last = text.split("\n")[-1]
    assert last.startswith(md.TIME_EMOJI)
    stamp = (NOW - timedelta(hours=1)).strftime("%d.%m.%Y %H:%M")
    assert last.endswith("UTC</b>") and f"<b>{stamp}" in last


# --- a block's own move ------------------------------------------------------


# --- the regime the move happened in ----------------------------------------

def vix_frame(rows, spikes=()):
    """rows: (observed date, known date, close). `spikes` indexes into rows."""
    frame = pd.DataFrame({
        "day": [int(datetime(*d, tzinfo=timezone.utc).timestamp()) for d, _, _ in rows],
        "available_at": [int(datetime(*k, tzinfo=timezone.utc).timestamp())
                         for _, k, _ in rows],
        "close": [c for _, _, c in rows],
    })
    frame["is_spike"] = [i in spikes for i in range(len(rows))]
    return frame


def use_vix(monkeypatch, frame):
    monkeypatch.setattr(md, "_vix_scored", lambda: frame)


def test_the_comparison_names_yesterday_and_a_week_ago(monkeypatch):
    use_vix(monkeypatch, vix_frame([
        ((2026, 9, 1), (2026, 9, 2, 15), 14.32),
        ((2026, 9, 4), (2026, 9, 7, 15), 15.10),
        ((2026, 9, 10), (2026, 9, 11, 15), 17.84),
    ]))
    at = int(datetime(2026, 9, 12, 9, tzinfo=timezone.utc).timestamp())
    text = md.vix_context(at)
    assert "Fear gauge VIX" in text
    assert "17.84 1 day ago" in text
    assert "15.10 2 days ago" in text
    assert "14.32 7 days ago" in text
    assert "close" not in text
    assert "7 Sep" not in text


def test_the_comparison_still_prints_the_previous_close_when_it_is_flat(monkeypatch):
    rows = [((2026, 9, 1), (2026, 9, 2, 15), 20.00),
            ((2026, 9, 10), (2026, 9, 11, 15), 14.00)]
    use_vix(monkeypatch, vix_frame(rows))
    at = int(datetime(2026, 9, 12, 9, tzinfo=timezone.utc).timestamp())
    assert "14.00 1 day ago" in md.vix_context(at)
    assert "20.00 2 days ago" in md.vix_context(at)

    rows[1] = ((2026, 9, 10), (2026, 9, 11, 15), 20.05)
    use_vix(monkeypatch, vix_frame(rows))
    assert "20.05 1 day ago" in md.vix_context(at)
    assert "20.00 2 days ago" in md.vix_context(at)


def test_the_gauge_moves_on_as_soon_as_a_reading_is_published(monkeypatch):
    # A live note is re-rendered every run, so it must not sit on a stale gauge:
    # the moment FRED publishes the next close, the line follows it.
    use_vix(monkeypatch, vix_frame([
        ((2026, 9, 1), (2026, 9, 2, 15), 14.32),
        ((2026, 9, 9), (2026, 9, 10, 15), 16.46),
        ((2026, 9, 10), (2026, 9, 11, 15), 17.84),
    ]))
    before = int(datetime(2026, 9, 11, 10, tzinfo=timezone.utc).timestamp())
    after = int(datetime(2026, 9, 11, 16, tzinfo=timezone.utc).timestamp())
    assert "16.46 1 day ago" in md.vix_context(before)
    assert "17.84 1 day ago" in md.vix_context(after)


def test_the_regime_line_never_quotes_a_reading_that_did_not_exist_yet(monkeypatch):
    # FRED publishes VIX one to two business days late. A message about Monday's
    # move that quoted Monday's close would be reading a number the system could
    # not have had, which is the one thing a replayable record must not do.
    use_vix(monkeypatch, vix_frame([
        ((2020, 3, 9), (2020, 3, 10, 15), 54.46),
        ((2020, 3, 10), (2020, 3, 11, 15), 47.30),
        ((2020, 3, 11), (2020, 3, 12, 15), 53.90),
        ((2020, 3, 12), (2020, 3, 13, 15), 75.47),
    ]))
    text = md.vix_context(int(datetime(2020, 3, 12, 19, tzinfo=timezone.utc).timestamp()))

    assert "53.90" in text and "1 day ago" in text
    assert "75.47" not in text


def test_the_regime_line_is_silent_before_any_reading_is_known(monkeypatch):
    use_vix(monkeypatch, vix_frame([((2026, 9, 3), (2026, 9, 4, 15), 14.32)]))
    assert md.vix_context(int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())) == ""


def test_the_level_is_placed_in_its_own_history(monkeypatch):
    # 16 and 54 are both just numbers until one of them is "calmer than three
    # days in five" and the other "higher than all but one day in a hundred".
    rows = [((2020, 1, d // 24 + 1, d % 24), (2020, 2, 1, 15), 10.0 + d)
            for d in range(0, 40)]
    use_vix(monkeypatch, vix_frame(rows))
    at = int(datetime(2020, 3, 1, tzinfo=timezone.utc).timestamp())
    assert "higher than 100% of days" not in md.vix_context(at)
    assert "the highest it has been since" in md.vix_context(at)


def test_a_stress_episode_is_named_while_it_is_running_and_not_after(monkeypatch):
    # This is the multiplier finally becoming visible: it raises how seriously
    # clustered moves are taken for twenty-four REFERENCE hours after a spike,
    # and until now it has fed a channel nobody reads.
    use_vix(monkeypatch, vix_frame([
        ((2020, 2, 24), (2020, 2, 25, 15), 25.0),
        ((2020, 2, 27), (2020, 2, 28, 15), 39.16),
    ], spikes=(1,)))

    inside = md.vix_context(int(datetime(2020, 2, 28, 18, tzinfo=timezone.utc).timestamp()))
    assert "stress episode" in inside and "28.02.2020" in inside

    later = md.vix_context(int(datetime(2020, 3, 20, 18, tzinfo=timezone.utc).timestamp()))
    assert "39.16" in later                      # still the latest known reading
    assert "stress episode" not in later         # but the window closed long ago


def test_a_push_does_not_carry_the_regime_the_note_does(monkeypatch):
    use_vix(monkeypatch, vix_frame([((2026, 9, 3), (2026, 9, 4, 15), 14.32)]))
    later = event(hour_utc=int(datetime(2026, 9, 8, 14, tzinfo=timezone.utc).timestamp()))
    push = md.format_push(later, LABELS)
    assert "Fear gauge" not in push

    window = (int(datetime(2026, 9, 8, 9, tzinfo=timezone.utc).timestamp()),
              int(datetime(2026, 9, 11, 9, tzinfo=timezone.utc).timestamp()))
    note = md.format_digest([event()], LABELS, window,
                            now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc))
    assert "Fear gauge" in note[0] and "14.32" in note[0]


# --- the throwaway ping ----------------------------------------------------
#
# A digest row is written the hour its move is found, but a note stays silent
# because Telegram does not notify on an edit. The ping is the buzz that says a
# row appeared; it is deleted when the next note opens, so the record left
# behind is a clean run of notes.

class Deleted:
    """Stands in for notifier.delete_telegram_message."""

    def __init__(self, refuse=()):
        self.ids, self.refuse = [], set(refuse)

    def __call__(self, token, chat, message_id, *a, **k):
        self.ids.append(int(message_id))
        return int(message_id) not in self.refuse


def test_a_digest_row_buzzes_once_with_ticker_size_and_a_pointer(monkeypatch, sender):
    row = event(event_id="p1", tier="noticeable", channel="digest",
                digest_slot=int(NOW.timestamp()) + 3 * HOUR,
                sigma_lt=0.0105)
    _, state = deliver(monkeypatch, [row])

    pings = [t for t in sender.texts if t.startswith("⬜")]
    assert pings == ["⬜ <b>GLD</b> · Gold +2.10% · 2.0×σ\nAdded to digest👆🏻👆🏻"]
    stored = state[md.STATE_KEY][md.PINGS]["p1"]
    assert md._ping_message_id(stored) == 1

    # And not again on the next run: the buzz is once per move, not per hour.
    before = len(sender.texts)
    deliver(monkeypatch, [row], state=state)
    assert [t for t in sender.texts[before:] if t.startswith("⬜")] == []


def test_a_ticker_ping_puts_percent_and_size_after_the_name():
    text = md.format_ping(
        event(event_id="p", tier="noticeable", channel="digest",
              asset_id="twelvedata:BKLN", r=-0.008, sigma_lt=0.008 / 2.7),
        {"twelvedata:BKLN": "Senior bank loans"})
    assert text == (
        "⬜ <b>BKLN</b> · Senior bank loans -0.80% · 2.7×σ\n"
        "Added to digest👆🏻👆🏻")
    assert " · -0.80%" not in text


def test_a_push_tier_never_buzzes_even_while_it_sits_in_the_digest():
    # A once-a-year move waits in the digest until its retention is known and is
    # promoted six bars later. Keyed on the channel it would buzz and THEN push,
    # interrupting twice for one move.
    waiting = event(tier="major", channel="digest")
    assert md.pending_pings([waiting], {}, NOW) == []


def test_the_pings_are_cleared_as_the_next_note_opens(monkeypatch, sender):
    killer = Deleted()
    monkeypatch.setattr("price_monitor.notifier.delete_telegram_message", killer)
    store = {md.PINGS: {"old1": 11, "old2": 12}}
    assert md.sweep_pings(cfg(), store) == 2
    assert killer.ids == [11, 12]
    assert store[md.PINGS] == {}


def test_a_ping_telegram_refuses_to_delete_is_struck_through(monkeypatch, editor):
    # The bot is an admin of a public channel and can delete any message there;
    # should Telegram refuse anyway, the ping must not go on claiming a row, and
    # the bot can always edit its own message.
    killer = Deleted(refuse=[12])
    monkeypatch.setattr("price_monitor.notifier.delete_telegram_message", killer)
    text = "⬜ <b>GLD</b> · Gold +1.00% · 4.0×σ\nAdded to digest👆🏻👆🏻"
    store = {md.PINGS: {"a": 11, "b": {"id": 12, "hash": "h", "text": text}}}
    assert md.sweep_pings(cfg(), store) == 2
    assert editor.calls == [(12, "<s>⬜ <b>GLD</b> · Gold +1.00% · 4.0×σ</s>")]
    assert store[md.PINGS] == {}


def test_nothing_stale_is_ever_buzzed(monkeypatch):
    # Without this the first run after the mute comes off buzzes once for every
    # row in the history instead of for what just happened.
    old = event(event_id="ancient", tier="noticeable", channel="digest",
                hour_utc=int(NOW.timestamp()) - 40 * 24 * HOUR)
    assert md.pending_pings([old], {}, NOW) == []


def header_for(y, m, d):
    opens = int(datetime(y, m, d, 0, 5, tzinfo=timezone.utc).timestamp())
    return md.format_digest([], LABELS, routing.digest_window(opens),
                            None, NOW)[0].splitlines()[0]


def test_the_header_names_the_last_day_the_note_can_hold_an_hour_of():
    # A note runs to the instant the next one opens, and that instant is 00:05
    # on the next Saturday - so a header taken from the boundary would name that
    # Saturday, a day the note carries none of. The note is a list of hourly
    # bars: a five-minute sliver cannot hold one.
    assert "19.09.2026 to 25.09.2026" in header_for(2026, 9, 19)


def test_the_note_header_uses_day_month_year_on_both_ends():
    inside = header_for(2026, 3, 7)
    assert "07.03.2026 to 13.03.2026" in inside

    across = header_for(2026, 3, 28)     # Saturday 28 March into April
    assert "28.03.2026 to 03.04.2026" in across


def test_the_note_runs_in_time_order_across_all_its_parts():
    # A long note is cut into several messages. Sorting each part on its own
    # would restart the clock at every cut, so the rows are ordered once and the
    # cut falls wherever the character budget runs out.
    rows = [event(event_id=f"d{i}", channel="digest", tier="noticeable",
                  hour_utc=SLOT + i * HOUR, asset_id="twelvedata:GLD")
            for i in range(60)]
    texts = md.format_digest(rows, LABELS, (SLOT, SLOT + 200 * HOUR), None, NOW)
    assert len(texts) > 1, "the fixture must be long enough to split"

    stamps = []
    for part in texts:
        for line in part.split("\n"):
            if line.startswith(md.TIME_EMOJI):
                stamps.append(line)
    assert stamps == sorted(stamps), "the hours must ascend across the parts"
    assert len(stamps) == len(rows)


def test_two_moves_in_one_hour_put_the_rarer_first():
    # The one case time cannot separate.
    same = SLOT + 5 * HOUR
    rows = [event(event_id="mild", channel="digest", tier="noticeable",
                  hour_utc=same, asset_id="twelvedata:GLD"),
            event(event_id="rare", channel="digest", tier="high",
                  hour_utc=same, asset_id="coinbase:BTC-USD")]
    text = md.format_digest(rows, LABELS, (SLOT, SLOT + 200 * HOUR), None, NOW)[0]
    assert text.index("Bitcoin") < text.index("Gold")

    # and reversing the input does not change the answer
    text = md.format_digest(rows[::-1], LABELS, (SLOT, SLOT + 200 * HOUR), None, NOW)[0]
    assert text.index("Bitcoin") < text.index("Gold")


def test_a_note_never_un_says_what_the_reader_already_read(monkeypatch, sender, editor):
    # It is rendered whole from the events table every run, which is what lets a
    # late event appear and a recomputed-away one go. That is right for one row
    # among several and wrong for ALL of them: a retuned ladder can empty a note
    # the reader has read and been pinged about, which reads as the bot
    # forgetting rather than correcting. Seen live - a note showing two moves
    # went back to "Nothing so far" the run after the rungs changed.
    row = event(event_id="d1", channel="digest", tier="noticeable",
                hour_utc=SLOT + 2 * HOUR)
    _, state = deliver(monkeypatch, [row])
    assert "Gold" in notes(sender)[0]

    # the recompute now produces nothing at all for that period
    before = len(editor.calls)
    deliver(monkeypatch, [], state=state)
    assert len(editor.calls) == before, "the note was edited down to nothing"


def test_a_note_still_drops_one_row_of_several(monkeypatch, sender, editor):
    # Only the all-or-nothing case is held. A genuine recompute that removes one
    # row of two still shows, because the note is not being emptied.
    rows = [event(event_id="d1", channel="digest", tier="noticeable",
                  hour_utc=SLOT + HOUR, asset_id="twelvedata:GLD"),
            event(event_id="d2", channel="digest", tier="noticeable",
                  hour_utc=SLOT + 2 * HOUR, asset_id="coinbase:BTC-USD")]
    _, state = deliver(monkeypatch, rows)
    assert "Bitcoin" in notes(sender)[0]

    deliver(monkeypatch, rows[:1], state=state)
    assert editor.calls, "the surviving row should have been rewritten"
    assert "Bitcoin" not in editor.calls[-1][1]


def test_notes_never_overlap_even_when_the_schedule_moves(monkeypatch, sender):
    # Seen live, moving the notes from Tuesday/Friday to Monday/Saturday: a note
    # opened under the old boundaries was still running when the new one opened
    # inside it, and carried_from - looking for the last end that had been
    # REACHED - skipped past the open note to the one before it. Both then
    # claimed the same hours, and the reader got two notes listing one move.
    old_slot = SLOT - 3 * 24 * HOUR
    digests = {
        str(old_slot - 3 * 24 * HOUR): {"ids": [1], "hashes": ["a"],
                                        "from": old_slot - 3 * 24 * HOUR,
                                        "to": old_slot},
        str(old_slot): {"ids": [2], "hashes": ["b"], "from": old_slot,
                        "to": SLOT + 24 * HOUR},          # still open past SLOT
        str(SLOT): {"ids": [3], "hashes": ["c"], "from": old_slot,  # overlaps it
                    "to": routing.next_digest_slot(SLOT)},
    }
    assert md.tidy_windows(digests)

    windows = [md.note_window(int(k), v) for k, v in
               sorted(digests.items(), key=lambda kv: int(kv[0]))]
    for earlier, later in zip(windows, windows[1:]):
        assert earlier[1] == later[0], "notes must meet exactly, never overlap"


def test_straightening_a_window_drops_the_rows_it_no_longer_owns(monkeypatch):
    # The rows marker guards a note against being emptied by a change to what
    # qualifies. A straightened window is different: those rows belong to the
    # note beside it now, and holding them would print the move twice.
    digests = {
        str(SLOT - 3 * 24 * HOUR): {"ids": [1], "hashes": ["a"], "rows": 2,
                                    "from": SLOT - 3 * 24 * HOUR,
                                    "to": SLOT + 24 * HOUR},
        str(SLOT): {"ids": [2], "hashes": ["b"], "rows": 2,
                    "from": SLOT - 3 * 24 * HOUR,
                    "to": routing.next_digest_slot(SLOT)},
    }
    md.tidy_windows(digests)
    assert "rows" not in digests[str(SLOT)]
    assert "rows" not in digests[str(SLOT - 3 * 24 * HOUR)]


def test_tidying_leaves_a_healthy_set_of_notes_alone():
    slots = [SLOT - 3 * 24 * HOUR, SLOT]
    digests = {str(slots[0]): {"ids": [1], "hashes": ["a"], "rows": 2,
                               "from": slots[0], "to": slots[1]},
               str(slots[1]): {"ids": [2], "hashes": ["b"], "rows": 1,
                               "from": slots[1],
                               "to": routing.next_digest_slot(slots[1])}}
    assert md.tidy_windows(digests) == 0
    assert digests[str(slots[1])]["rows"] == 1      # the guard survives


def test_a_format_change_rewrites_pushes_and_pings_since_the_open_note(
        monkeypatch, sender, editor):
    # A copy tweak must land on the next run, not wait for a retention check-in.
    live = event(event_id="live", channel="push", retention_settled=None,
                 hour_utc=int(NOW.timestamp()) - HOUR)
    ping = event(event_id="p1", tier="noticeable", channel="digest",
                 hour_utc=int(NOW.timestamp()) - HOUR)
    _, state = deliver(monkeypatch, [live, ping])

    old = event(event_id="old", channel="push", retention_settled=None,
                hour_utc=SLOT - 10 * 24 * HOUR)
    state[md.STATE_KEY][follow_up_module.TRACKED]["old"] = {
        "message_id": 99, "hour_utc": int(old["hour_utc"]), "written": [],
        "text_hash": "from-last-week",
    }

    real_push = md.format_push
    monkeypatch.setattr(
        md, "format_push",
        lambda *a, **k: "NEWSTYLE\n" + real_push(*a, **k))
    real_ping = md.format_ping
    monkeypatch.setattr(
        md, "format_ping",
        lambda *a, **k: real_ping(*a, **k) + "\n.")

    before = len(editor.calls)
    deliver(monkeypatch, [live, ping, old], state=state)
    changed = editor.calls[before:]
    assert any(text.startswith("NEWSTYLE") for _, text in changed)
    assert any(text.endswith("\n.") for _, text in changed)
    assert any(message_id == 99 for message_id, _ in changed)


def test_a_sent_push_is_edited_in_place_when_its_bar_heals(
        monkeypatch, sender, editor):
    # The hour is scored a few minutes in and the bar heals on the next fetch,
    # so the move - and its size in sigma - can change after the push went out.
    live = event(event_id="e1", channel="push", hour_utc=int(NOW.timestamp()) - HOUR)
    _, state = deliver(monkeypatch, [live])
    store = state[md.STATE_KEY]
    mid = store[follow_up_module.TRACKED]["e1"]["message_id"]

    before = len(editor.calls)
    deliver(monkeypatch, [live], state=state)
    assert editor.calls[before:] == []                   # nothing changed, nothing edited

    healed = live | {"r": 0.027}
    deliver(monkeypatch, [healed], state=state)
    changed = editor.calls[before:]
    assert [i for i, _ in changed] == [mid]
    assert "+2.70% · 9.0×σ" in changed[0][1]
    assert len([t for t in sender.texts if "GLD" in t]) == 1   # no second message


def test_a_push_sent_before_tracking_is_picked_up_from_its_sent_record(
        monkeypatch, sender, editor):
    live = event(event_id="e1", channel="push", hour_utc=int(NOW.timestamp()) - HOUR)
    _, state = deliver(monkeypatch, [live])
    store = state[md.STATE_KEY]
    mid = store[follow_up_module.TRACKED]["e1"]["message_id"]
    store[follow_up_module.TRACKED] = {}

    deliver(monkeypatch, [live | {"r": 0.027}], state=state)
    assert any(i == mid for i, _ in editor.calls)
    assert "e1" in store[follow_up_module.TRACKED]


def test_a_ping_whose_row_left_the_digest_is_deleted(
        monkeypatch, sender, editor):
    # Live: BKLN's ping still said "Added to digest" after a floor raise dropped the
    # row, and restyle invented ticker · name with no size. It was never in
    # the note, so the ping is deleted rather than rewritten as a lie.
    killer = Deleted()
    monkeypatch.setattr("price_monitor.notifier.delete_telegram_message", killer)
    ping = event(event_id="twelvedata_BKLN:1789498800", tier="noticeable",
                 channel="digest", asset_id="twelvedata:BKLN",
                 hour_utc=int(NOW.timestamp()) - HOUR, r=-0.0080, sigma_lt=0.003)
    other = event(event_id="twelvedata_DBB:1", tier="noticeable",
                  channel="digest", asset_id="twelvedata:DBB",
                  hour_utc=int(NOW.timestamp()) - HOUR, r=0.0012, sigma_lt=0.001)
    _, state = deliver(monkeypatch, [ping, other])
    bkln_id = md._ping_message_id(
        state[md.STATE_KEY][md.PINGS]["twelvedata_BKLN:1789498800"])
    deliver(monkeypatch, [other], state=state)
    assert killer.ids == [bkln_id]
    assert "twelvedata_BKLN:1789498800" not in state[md.STATE_KEY][md.PINGS]
    assert "twelvedata_DBB:1" in state[md.STATE_KEY][md.PINGS]


def test_an_empty_events_table_does_not_delete_pings(monkeypatch, sender):
    killer = Deleted()
    monkeypatch.setattr("price_monitor.notifier.delete_telegram_message", killer)
    ping = event(event_id="p1", tier="noticeable", channel="digest",
                 hour_utc=int(NOW.timestamp()) - HOUR, sigma_lt=0.0105)
    _, state = deliver(monkeypatch, [ping])
    deliver(monkeypatch, [], state=state)
    assert killer.ids == []
    assert "p1" in state[md.STATE_KEY][md.PINGS]


def test_a_row_from_the_closed_period_does_not_buzz_again(monkeypatch, sender):
    """The 19 September 2026 bug, in one test.

    `sweep_pings` clears the ledger as the next note opens, and `pending_pings`
    used to read straight down the events table afterwards - so every digest row
    still inside the 48-hour freshness rule buzzed a second time, beside a new
    note that correctly showed none of them because they belong to the period
    that just closed. Live, two rows from the 17th and 18th re-announced
    themselves next to an empty note for the 19th-21st.
    """
    monkeypatch.setattr("price_monitor.notifier.delete_telegram_message",
                        Deleted())
    from tremor import routing

    opens = routing.digest_slot(int(NOW.timestamp()))
    previous = routing.digest_slot(opens - 1)
    # The note that just closed, with a message behind it - so the new note
    # starts exactly where it stopped rather than reaching back over it. This
    # is what production looked like: a note for 14-19 Sep with five rows in it,
    # then the note for 19-21 Sep opening beside it.
    state = {md.STATE_KEY: {md.DIGEST_STATE: {
        str(previous): {"ids": [13], "hashes": ["x"], "from": previous,
                        "to": opens, "rows": 5}}}}
    # A row from BEFORE this note's period: already published by the note that
    # closed, and already swept.
    old = event(event_id="closed", tier="noticeable", channel="digest",
                hour_utc=opens - 6 * HOUR)
    # And one inside it, which must still buzz.
    fresh = event(event_id="open", tier="noticeable", channel="digest",
                  asset_id="twelvedata:BKLN", hour_utc=opens + HOUR)

    now = datetime.fromtimestamp(opens + 2 * HOUR, tz=timezone.utc)
    _, state = deliver(monkeypatch, [old, fresh], state=state, now=now)

    buzzed = set(state[md.STATE_KEY][md.PINGS])
    assert buzzed == {"open"}, "a closed period's row must not buzz again"
    assert not any("GLD" in t and "Added to digest" in t for t in sender.texts)


def test_no_open_note_means_no_ping(monkeypatch, sender):
    """A ping points at a note. With none open there is nothing to point at, and
    the stretch is carried into the next note, which buzzes it then."""
    row = event(event_id="p1", tier="noticeable", channel="digest")
    assert md.pending_pings([row], {}, NOW, None) == []


def test_a_ping_left_behind_by_a_closed_period_is_deleted(monkeypatch, sender):
    """The other half of the 19 September bug.

    Bounding `pending_pings` stops a closed period's row buzzing AGAIN, but it
    does nothing about the two that already went out. They sat in the ledger
    pointing at a note whose window cannot contain them, and `sweep_pings` had
    already run this period - so nothing would have removed them until the next
    note opened two days later. A ping exists only while the note beneath it
    shows its row, so it goes as soon as that stops being true.

    The note here is ALREADY open, which is the whole point: this is every run
    after the one that created the mess, where no sweep is due.
    """
    killer = Deleted()
    monkeypatch.setattr("price_monitor.notifier.delete_telegram_message", killer)
    from tremor import routing

    opens = routing.digest_slot(int(NOW.timestamp()))
    previous = routing.digest_slot(opens - 1)
    stranded = event(event_id="stranded", tier="noticeable", channel="digest",
                     hour_utc=opens - 6 * HOUR)
    state = {md.STATE_KEY: {
        md.DIGEST_STATE: {
            str(previous): {"ids": [13], "hashes": ["x"], "from": previous,
                            "to": opens, "rows": 1},
            # already open, so sweep_pings is not what removes the ping
            str(opens): {"ids": [26], "hashes": ["y"], "from": opens,
                         "to": routing.next_digest_slot(opens)}},
        md.PINGS: {"stranded": {"id": 24, "hash": "whatever"}}}}

    now = datetime.fromtimestamp(opens + 2 * HOUR, tz=timezone.utc)
    _, state = deliver(monkeypatch, [stranded], state=state, now=now)

    assert 24 in killer.ids, "the stranded ping must be deleted from Telegram"
    assert "stranded" not in state[md.STATE_KEY][md.PINGS]
    # and it must not come straight back
    assert not any("Added to digest" in t for t in sender.texts)


# --- a closed note is a record, not a live feed -------------------------------
#
# Every note still inside DIGEST_TRACK_HOURS is re-rendered from the events
# table on every run, for ten days. That is what lets a late event appear and a
# recomputed-away one go. What it must not do is INTERRUPT: a new part is a
# Telegram notification, and a period that ended days ago has nothing to notify
# anybody about.

LATER = NOW + timedelta(hours=6)        # past SLOT's opening window, so nothing new opens


def _crowded(window, count=200):
    """Enough digest rows inside `window` to need more parts than one."""
    start = window[0]
    return [event(event_id=f"d{i}", channel="digest", tier="noticeable",
                  hour_utc=start + HOUR, digest_slot=None)
            for i in range(count)]


def _one_note(to):
    """A note with a single part already posted, covering up to `to`."""
    slot = to - 3 * 24 * HOUR
    return slot, {md.STATE_KEY: {md.DIGEST_STATE: {
        str(slot): {"ids": [13], "hashes": ["stale"], "rows": 1,
                    "from": slot, "to": to}}}}


def test_a_closed_note_is_corrected_but_never_posts_another_part(
        monkeypatch, sender, editor):
    # THE ONE THAT WENT WRONG. A cold rebuild grew the note for Mon 14 -> Sat 19
    # from 5 rows to 19, seven hours after that period had closed, and the
    # fourteen it gained went out as two new messages: a burst of alerts, at
    # 10:06, for moves that happened two days earlier.
    to = int(LATER.timestamp()) - (md.DIGEST_GROW_AFTER_CLOSE_HOURS + 1) * HOUR
    slot, state = _one_note(to)
    rows = _crowded((slot, to))

    deliver(monkeypatch, rows, state=state, now=LATER)

    assert notes(sender) == [], "a closed note must not send a new message"
    assert state[md.STATE_KEY][md.DIGEST_STATE][str(slot)]["ids"] == [13]
    # It is still a record kept true: the part that exists was rewritten.
    assert [call for call in editor.calls if call[0] == 13], \
        "a closed note is still corrected in place, silently"


def test_a_note_still_posts_the_tail_of_the_period_it_just_closed(
        monkeypatch, sender, editor):
    # The other half of the same bound, and the reason it is not simply "closed
    # means frozen": the last hour of a period is scored by the run AFTER that
    # period ends, so a note that froze on its own boundary would drop it.
    to = int(LATER.timestamp()) - (md.DIGEST_GROW_AFTER_CLOSE_HOURS - 1) * HOUR
    slot, state = _one_note(to)
    rows = _crowded((slot, to))

    deliver(monkeypatch, rows, state=state, now=LATER)

    assert notes(sender), "a note just past its boundary must still post its tail"
    assert len(state[md.STATE_KEY][md.DIGEST_STATE][str(slot)]["ids"]) > 1


def test_a_note_whose_first_post_failed_is_still_retried_after_it_closes(
        monkeypatch, sender):
    # A note with no message behind it is not a closed note gaining a row, it is
    # a post that never landed. Refusing it would lose the only copy of that
    # period - carried_from already treats it this way when it decides where the
    # next note starts covering.
    to = int(LATER.timestamp()) - (md.DIGEST_GROW_AFTER_CLOSE_HOURS + 1) * HOUR
    slot = to - 3 * 24 * HOUR
    state = {md.STATE_KEY: {md.DIGEST_STATE: {
        str(slot): {"ids": [], "hashes": [], "from": slot, "to": to}}}}

    deliver(monkeypatch, _crowded((slot, to), count=2), state=state, now=LATER)

    assert notes(sender), "a note that never posted must still be retried"


# --- a closed note shows what it showed ---------------------------------------

def _closed(events_published=None):
    """A note whose period ended well before LATER, with one part posted."""
    to = int(LATER.timestamp()) - (md.DIGEST_GROW_AFTER_CLOSE_HOURS + 1) * HOUR
    slot = to - 3 * 24 * HOUR
    record = {"ids": [13], "hashes": ["stale"], "from": slot, "to": to}
    if events_published is not None:
        record["events"] = events_published
    return slot, record, (slot, to)


def test_a_closed_note_shows_only_what_it_published(monkeypatch, sender, editor):
    # THE ONE THAT WENT WRONG, and the deeper half of it. Scoring was fixed so an
    # hour is judged on its whole bar rather than its first five minutes, and
    # fourteen moves it had missed appeared inside a note for a week that was
    # already over - which then claimed to have reported nineteen moves that week
    # when it had reported five.
    slot, record, window = _closed(events_published=["kept"])
    rows = [event(event_id="kept", channel="digest", hour_utc=window[0] + HOUR),
            event(event_id="late", channel="digest", hour_utc=window[0] + 2 * HOUR)]

    shown = md.published_only(record, rows, window, LATER)

    assert [e["event_id"] for e in shown] == ["kept"]


def test_a_note_that_can_still_grow_shows_everything(monkeypatch):
    # While the period is open a late row appearing IS the feature - that is how
    # a move found at 10:00 reaches a note opened at 00:05.
    to = int(LATER.timestamp()) + 24 * HOUR
    record = {"ids": [13], "hashes": ["h"], "from": to - 3 * 24 * HOUR, "to": to,
              "events": ["kept"]}
    rows = [event(event_id="kept", channel="digest"),
            event(event_id="new", channel="digest")]

    shown = md.published_only(record, rows, (record["from"], to), LATER)

    assert [e["event_id"] for e in shown] == ["kept", "new"]


def test_a_note_from_before_this_existed_is_left_alone():
    # No record of what it published, and no way to work it out afterwards -
    # the table has already changed, which is the whole problem. Reconcile can
    # state it by hand; guessing here would drop real rows.
    slot, record, window = _closed(events_published=None)
    rows = [event(event_id="a", channel="digest"), event(event_id="b", channel="digest")]

    assert md.published_only(record, rows, window, LATER) == rows


def test_an_open_note_records_the_rows_it_is_showing(monkeypatch, sender):
    rows = [event(event_id="d1", channel="digest", tier="noticeable",
                  hour_utc=int(NOW.timestamp()) - HOUR, digest_slot=SLOT)]
    _, state = deliver(monkeypatch, rows)

    record = state[md.STATE_KEY][md.DIGEST_STATE][str(SLOT)]
    assert record["events"] == ["d1"], "a note must remember what it has said"


def jump(**over):
    return event(tier="high", r=0.012, sigma_lt=0.002) | over


def test_a_jump_says_its_size_in_sigma_on_the_first_line_and_no_word():
    # The colour of the square is the word; the size is |move| / half-year σ.
    text = md.format_push(jump(), LABELS)
    lines = text.splitlines()
    assert lines[0] == "🟨 <b>GLD</b> · Gold +1.20% · 6.0×σ"
    assert "high" not in text and "usual" not in text


def test_a_jump_is_dated_day_dot_month_dot_year():
    text = md.format_push(jump(), LABELS)
    assert text.splitlines()[-1] == "🕐 <b>12.09.2026 02:00 UTC</b>"


def test_a_jump_ping_says_its_size_in_sigma():
    ping = md.format_ping(jump(tier="noticeable", r=0.0106), LABELS)
    assert ping.splitlines()[0] == "⬜ <b>GLD</b> · Gold +1.06% · 5.3×σ"


def test_a_gap_jump_names_no_yardstick_either():
    # Hour or gap shows in the "biggest since" line (stage 3), not here.
    text = md.format_push(jump(overnight=True, gap_kind="weekend"), LABELS)
    assert text.splitlines()[0].endswith(" · 6.0×σ")
    assert "usual" not in text


# --- the sweep: what is deleted, and what is corrected instead ----------------
#
# Delivery deletes pings, and a day's lower messages once the day's rarer one is
# on the channel. Nothing else: every other change is an edit.

def _killer(monkeypatch, refuse=()):
    killer = Deleted(refuse=refuse)
    monkeypatch.setattr(follow_up_module, "delete_telegram_message", killer)
    monkeypatch.setattr("price_monitor.notifier.delete_telegram_message", killer)
    return killer


def test_a_day_that_grew_deletes_its_lower_push_once_the_rarer_one_is_out(
        monkeypatch, sender, editor):
    killer = _killer(monkeypatch)
    early = event(event_id="high", tier="high", hour_utc=int(NOW.timestamp()) - 5 * HOUR)
    _, state = deliver(monkeypatch, [early])
    store = state[md.STATE_KEY]
    lower_id = store[follow_up_module.TRACKED]["high"]["message_id"]

    later = event(event_id="major", tier="major", r=0.03)
    deliver(monkeypatch, [early | {"superseded_by": "major"}, later], state=state)
    assert killer.ids == [lower_id]
    assert "high" not in store[follow_up_module.TRACKED]
    assert store[md._SENT]["high"]["gone"] is True

    # and it is neither sent again nor deleted twice
    before = len(sender.texts)
    deliver(monkeypatch, [early | {"superseded_by": "major"}, later], state=state)
    assert sender.texts[before:] == [] and killer.ids == [lower_id]


def test_a_lower_push_telegram_will_not_delete_is_struck_through(
        monkeypatch, sender, editor):
    early = event(event_id="high", tier="high", hour_utc=int(NOW.timestamp()) - 5 * HOUR)
    _, state = deliver(monkeypatch, [early])
    lower_id = state[md.STATE_KEY][follow_up_module.TRACKED]["high"]["message_id"]
    _killer(monkeypatch, refuse=[lower_id])

    deliver(monkeypatch, [early | {"superseded_by": "major"},
                          event(event_id="major", tier="major")], state=state)
    struck = [t for i, t in editor.calls if i == lower_id]
    assert struck and struck[0].startswith("<s>🟨 <b>GLD</b> · Gold +2.10% · 7.0×σ</s>")
    assert "the day grew" in struck[0]


def test_a_lower_push_is_never_sent_once_its_day_grew(monkeypatch, sender, editor):
    _killer(monkeypatch)
    early = event(event_id="high", tier="high", superseded_by="major",
                  hour_utc=int(NOW.timestamp()) - 5 * HOUR)
    deliver(monkeypatch, [early, event(event_id="major", tier="major")])
    assert len([t for t in sender.texts if "GLD" in t]) == 1


def test_a_day_that_grew_takes_its_note_row_and_its_ping_with_it(
        monkeypatch, sender, editor):
    killer = _killer(monkeypatch)
    row = digest_row(hour_utc=int(NOW.timestamp()) - 2 * HOUR)    # inside the open note
    _, state = deliver(monkeypatch, [row])
    ping_id = md._ping_message_id(state[md.STATE_KEY][md.PINGS]["d1"])
    assert "Gold" in notes(sender)[0]

    pushed = event(event_id="major", tier="major")
    deliver(monkeypatch, [row | {"superseded_by": "major"}, pushed], state=state)
    assert killer.ids == [ping_id]
    note_edits = [t for _, t in editor.calls if "Digest" in t]
    assert note_edits and "Gold" not in note_edits[-1]      # an edit, not a delete


def test_a_push_whose_event_is_gone_stays_as_sent(monkeypatch, sender, editor):
    # Nothing else of its day is on the channel: it is not a lower message, and
    # a push is never deleted for anything else.
    killer = _killer(monkeypatch)
    live = event(event_id="e1", day=7)
    _, state = deliver(monkeypatch, [live])
    mid = state[md.STATE_KEY][follow_up_module.TRACKED]["e1"]["message_id"]
    other = event(event_id="other", asset_id="coinbase:BTC-USD", channel="digest",
                  tier="noticeable", day=7)
    deliver(monkeypatch, [other], state=state)
    assert killer.ids == []
    assert all(i != mid for i, _ in editor.calls)


def test_a_push_whose_word_fell_stays_the_one_message_for_its_move(
        monkeypatch, sender, editor):
    killer = _killer(monkeypatch)
    live = event(event_id="e1", tier="high")
    _, state = deliver(monkeypatch, [live])
    mid = state[md.STATE_KEY][follow_up_module.TRACKED]["e1"]["message_id"]

    fallen = live | {"tier": "noticeable", "channel": "digest", "r": 0.012,
                     "digest_slot": SLOT}
    deliver(monkeypatch, [fallen], state=state)
    assert killer.ids == []
    assert not any("Added to digest" in t for t in sender.texts)   # no ping
    assert all("Gold" not in t for t in notes(sender))             # not in the note
    assert all("Gold" not in t for _, t in editor.calls if "Digest" in t)
    assert any(i == mid and t.startswith("⬜ <b>GLD</b> · Gold +1.20% · 4.0×σ")
               for i, t in editor.calls)                          # the push, corrected


def test_a_push_whose_day_now_has_another_message_on_the_channel_goes(
        monkeypatch, sender, editor):
    # The bar healed and an EARLIER hour of the same day became as rare: that one
    # is now the day's event, and this push is a lower message of the same day.
    killer = _killer(monkeypatch)
    late = event(event_id="late", tier="high", day=7)
    _, state = deliver(monkeypatch, [late])
    mid = state[md.STATE_KEY][follow_up_module.TRACKED]["late"]["message_id"]

    early = event(event_id="early", tier="high", day=7,
                  hour_utc=int(NOW.timestamp()) - 3 * HOUR)
    deliver(monkeypatch, [early], state=state)
    assert killer.ids == [mid]

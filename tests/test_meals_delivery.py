from datetime import datetime, timezone

import pytest

from price_monitor import meals_delivery as md
from price_monitor.config import Config
from price_monitor.notifier import TelegramError

HOUR = 3600
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)   # a Friday
LABELS = {"twelvedata:GLD": "Gold", "coinbase:BTC-USD": "Bitcoin"}


def cfg(**over):
    base = dict(assets=[], telegram_bot_token="t", telegram_chat_id="c",
                meals_alerts_muted=False)
    return Config(**(base | over))


def event(**over):
    base = dict(event_id="e1", asset_id="twelvedata:GLD", hour_utc=int(NOW.timestamp()) - HOUR,
                tier="major", basis="abnormal", channel="push", r=0.021,
                retention_24=0.9, digest_slot=None)
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


@pytest.fixture
def sender(monkeypatch):
    s = Sent()
    monkeypatch.setattr(md, "send_telegram_message", s)
    monkeypatch.setattr(md, "_labels", lambda: LABELS)
    return s


def deliver(monkeypatch, events, state=None, now=NOW, **over):
    monkeypatch.setattr(md, "load_events", lambda c: events)
    state = {} if state is None else state
    return md.maybe_deliver(cfg(**over), state, now), state


def test_nothing_goes_out_while_muted(monkeypatch, sender):
    sent, _ = deliver(monkeypatch, [event()], meals_alerts_muted=True)
    assert sent == 0 and sender.texts == []


def test_a_push_goes_out_once(monkeypatch, sender):
    sent, state = deliver(monkeypatch, [event()])
    assert sent == 1 and len(sender.texts) == 1
    assert "Gold" in sender.texts[0]

    # the same run again sends nothing: the id is remembered
    again, _ = deliver(monkeypatch, [event()], state=state)
    assert again == 0 and len(sender.texts) == 1


def test_stale_events_are_not_delivered(monkeypatch, sender):
    # The events table holds the whole history, so without this the first run
    # after the mute comes off would deliver five years of alerts at once.
    old = event(hour_utc=int(NOW.timestamp()) - (md.STALE_AFTER_HOURS + 1) * HOUR)
    sent, _ = deliver(monkeypatch, [old])
    assert sent == 0 and sender.texts == []


def test_an_event_from_the_future_is_not_delivered(monkeypatch, sender):
    ahead = event(hour_utc=int(NOW.timestamp()) + HOUR)
    assert deliver(monkeypatch, [ahead])[0] == 0


def test_a_digest_waits_for_its_slot(monkeypatch, sender):
    ahead = event(event_id="d1", channel="digest", tier="routine",
                  digest_slot=int(NOW.timestamp()) + HOUR)
    assert deliver(monkeypatch, [ahead])[0] == 0

    due = event(event_id="d1", channel="digest", tier="routine",
                digest_slot=int(NOW.timestamp()) - HOUR)
    sent, _ = deliver(monkeypatch, [due])
    assert sent == 1 and "MEALS digest" in sender.texts[0]


def test_the_digest_is_one_message_for_many_events(monkeypatch, sender):
    rows = [event(event_id=f"d{i}", channel="digest", tier="routine",
                  hour_utc=int(NOW.timestamp()) - (i + 1) * HOUR,
                  digest_slot=int(NOW.timestamp()) - HOUR) for i in range(5)]
    sent, _ = deliver(monkeypatch, rows)
    assert sent == 1
    assert sender.texts[0].count("Gold") == 5


def test_a_failed_send_is_retried_rather_than_lost(monkeypatch):
    # An event is marked sent only once its message has actually gone.
    failing = Sent(fail=True)
    monkeypatch.setattr(md, "send_telegram_message", failing)
    monkeypatch.setattr(md, "_labels", lambda: LABELS)
    sent, state = deliver(monkeypatch, [event()])
    assert sent == 0
    assert not state[md.STATE_KEY][md._SENT]

    working = Sent()
    monkeypatch.setattr(md, "send_telegram_message", working)
    again, _ = deliver(monkeypatch, [event()], state=state)
    assert again == 1


def test_an_event_promoted_to_a_push_later_is_still_sent(monkeypatch, sender):
    # A once-a-year move is routed to the digest while its retention is unknown
    # and becomes a push six bars later, when the answer arrives. A high-water
    # mark on the hour would have stepped over it in between.
    slot = int(NOW.timestamp()) + 3 * HOUR          # its digest has not run yet
    _, state = deliver(monkeypatch, [event(channel="digest", digest_slot=slot,
                                           retention_24=None)])
    assert not state[md.STATE_KEY][md._SENT]

    sent, _ = deliver(monkeypatch, [event(channel="push")], state=state)
    assert sent == 1


def test_the_state_does_not_grow_without_bound(monkeypatch, sender):
    ancient = {"old": int(NOW.timestamp()) - 400 * 24 * HOUR}
    state = {md.STATE_KEY: {md._SENT: dict(ancient)}}
    _, state = deliver(monkeypatch, [event()], state=state)
    assert "old" not in state[md.STATE_KEY][md._SENT]
    assert "e1" in state[md.STATE_KEY][md._SENT]


def test_a_market_event_reads_as_market_wide(monkeypatch, sender):
    row = event(event_id="m1", basis="market", asset_id=None, r=None,
                retention_24=None, tier="notable")
    deliver(monkeypatch, [row])
    assert "Market-wide" in sender.texts[0]
    assert "most disorderly" in sender.texts[0]


def test_the_severity_leads_the_digest(monkeypatch, sender):
    # A digest read only as far as its notification preview should still
    # deliver its most important line.
    rows = [
        event(event_id="a", channel="digest", tier="routine",
              digest_slot=int(NOW.timestamp()) - HOUR),
        event(event_id="b", channel="digest", tier="major",
              asset_id="coinbase:BTC-USD", digest_slot=int(NOW.timestamp()) - HOUR),
    ]
    deliver(monkeypatch, rows)
    text = sender.texts[0]
    assert text.index("Bitcoin") < text.index("Gold")


def test_a_long_digest_is_split_within_telegrams_limit(monkeypatch, sender):
    rows = [event(event_id=f"d{i}", channel="digest", tier="routine",
                  hour_utc=int(NOW.timestamp()) - HOUR,
                  digest_slot=int(NOW.timestamp()) - HOUR) for i in range(200)]
    sent, _ = deliver(monkeypatch, rows)
    assert sent > 1
    assert all(len(t) <= 4096 for t in sender.texts)
    assert "part 1 of" in sender.texts[0]


def test_labels_fall_back_to_the_ticker(monkeypatch, sender):
    monkeypatch.setattr(md, "_labels", lambda: {})
    deliver(monkeypatch, [event()])
    assert "GLD" in sender.texts[0]


def test_missing_parquet_files_are_not_an_error(tmp_path):
    quiet = cfg(meals_events_path=str(tmp_path / "nope.parquet"),
                meals_market_events_path=str(tmp_path / "also-nope.parquet"))
    assert md.load_events(quiet) == []
    assert md.maybe_deliver(quiet, {}) == 0


def test_a_move_that_kept_going_does_not_read_as_a_percentage_still_standing():
    # A ratio above one means the move CONTINUED. Rendered as a percentage it
    # produced "360% of it still standing", which reads as an error rather than
    # as the strongest thing the system can say about an event.
    assert "3.6x" in md._retention_note(3.6)
    assert "kept going" in md._retention_note(1.4)
    assert "%" not in md._retention_note(3.6)


def test_the_retention_wording_covers_the_whole_range():
    assert md._retention_note(0.95) == "still there a day later"
    assert "60%" in md._retention_note(0.6)
    assert "reversed" in md._retention_note(-0.2)
    assert "reversed" in md._retention_note(0.0)


def test_an_unexplained_move_is_not_called_the_biggest_move():
    # The instrument may well have had larger hours that the market accounted
    # for perfectly; "biggest move" would overstate what was detected.
    assert md._headline("major", "abnormal") == "biggest unexplained move in about a year"
    assert md._headline("major", "absolute") == "biggest move in about a year"
    assert md._headline("major", "both") == "biggest move in about a year"
    assert md._headline("notable", "market") == "most disorderly hour in about two months"


def test_the_basis_note_is_not_repeated_when_the_headline_carries_it(sender, monkeypatch):
    deliver(monkeypatch, [event(basis="abnormal")])
    assert "unexplained move" in sender.texts[0]
    assert sender.texts[0].count("explain") == 1

    sender.texts.clear()
    deliver(monkeypatch, [event(event_id="x", basis="both")])
    assert "did not explain" in sender.texts[0]

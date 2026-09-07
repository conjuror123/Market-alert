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
    assert sent == 1 and "Digest" in sender.texts[0]


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


def test_an_unexplained_move_is_not_called_simply_the_biggest_move():
    # The instrument may well have had larger hours that the rest of the market
    # accounted for perfectly; an unqualified "biggest move" would overstate
    # what was detected. The qualifier carries it rather than a different noun.
    assert md._headline("major", "abnormal") == (
        "biggest move in about a year (not explained by the rest of the market)")
    assert md._headline("major", "absolute") == "biggest move in about a year"
    assert md._headline("major", "both") == "biggest move in about a year"
    assert md._headline("notable", "market") == "most disorderly hour in about two months"


def test_the_routine_period_is_said_in_weeks():
    assert md._headline("routine", "absolute") == "biggest move in two weeks"


def test_no_alert_claims_the_economic_calendar_explained_anything():
    # The residual is r minus what the basket and block factors predicted; the
    # calendar enters only the SI-Index, never this basis. An alert naming it
    # would be reporting a test the system never ran.
    for tier in ("routine", "notable", "major", "extreme"):
        for basis in ("abnormal", "absolute", "both", "market"):
            assert "calendar" not in md._headline(tier, basis).lower()
    for note in md.BASIS_NOTE.values():
        assert "calendar" not in note.lower()


def test_the_basis_note_is_not_repeated_when_the_headline_carries_it(sender, monkeypatch):
    deliver(monkeypatch, [event(basis="abnormal")])
    assert "not explained by the rest of the market" in sender.texts[0]
    assert sender.texts[0].count("explain") == 1

    sender.texts.clear()
    deliver(monkeypatch, [event(event_id="x", basis="both")])
    assert "did not explain" in sender.texts[0]


def test_the_push_names_the_instruments_that_moved_with_it():
    labels = {"twelvedata:SPY": "S&P 500", "twelvedata:XLF": "US financial sector",
              "twelvedata:USO": "WTI crude oil"}
    line = md._also_moved({"also_moved": "twelvedata:SPY twelvedata:XLF"}, labels)
    assert line == "S&P 500 and US financial sector within the day"


def test_three_companions_read_as_a_list():
    labels = {"a:1": "Gold", "a:2": "Silver", "a:3": "WTI crude oil"}
    line = md._also_moved({"also_moved": "a:1 a:2 a:3"}, labels)
    assert line == "Gold, Silver and WTI crude oil within the day"


def test_an_unlabelled_instrument_falls_back_to_its_ticker():
    assert md._also_moved({"also_moved": "twelvedata:EUR/USD"}, {}) == \
        "EUR/USD within the day"


def test_no_companions_produces_no_line():
    for value in ("", None, float("nan")):
        assert md._also_moved({"also_moved": value}, {}) == ""
    assert md._also_moved({}, {}) == ""


def test_a_very_long_list_is_cut_rather_than_running_off_the_screen():
    ids = " ".join(f"a:{i}" for i in range(9))
    line = md._also_moved({"also_moved": ids}, {})
    assert line.endswith("and 3 more within the day")
    assert line.count(",") == md.MAX_NAMED_COMPANIONS - 2


def _cal(rows):
    """rows: (iso date, country, title, impact)."""
    return [{"date": d, "country": c, "title": t, "impact": i,
             "actual": "", "forecast": "", "previous": ""} for d, c, t, i in rows]


def test_a_push_names_the_scheduled_news_behind_it():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "USD", "Core CPI m/m", "High"),
                ("2026-06-10T13:00:00+00:00", "USD", "Fed Chair Speaks", "High")])
    out = md.calendar_context(hour, cal)
    assert out.startswith("Economic events in the previous 3 hours:")
    assert "USD Core CPI m/m" in out and "USD Fed Chair Speaks" in out


def test_a_push_with_no_news_behind_it_says_so():
    # The more interesting half: 55% of pushes in the record have no
    # high-impact event in the previous three hours, and an unexplained move
    # with nothing scheduled is what the system exists to find.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    elsewhere = _cal([("2026-05-01T12:00:00+00:00", "USD", "Old CPI", "High")])
    assert md.calendar_context(hour, elsewhere) == \
        "Economic events in the previous 3 hours: none scheduled."


def test_an_empty_archive_claims_nothing_rather_than_claiming_silence():
    # "none scheduled" is a claim about the world and needs an archive behind
    # it. An empty one cannot tell "nothing happened" from "nothing was
    # loaded", so it says neither.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    assert md.calendar_context(hour, []) == ""


def test_only_high_impact_news_is_named():
    # The same window holds a median of one Low event, almost all bank
    # holidays; naming those would turn the most important line into noise.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "CHF", "Bank Holiday", "Low"),
                ("2026-06-10T12:45:00+00:00", "EUR", "Trade Balance", "Medium")])
    assert md.calendar_context(hour, cal).endswith("none scheduled.")


def test_news_outside_the_window_is_not_claimed_as_context():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T09:00:00+00:00", "USD", "Old News", "High"),
                ("2026-06-10T15:00:00+00:00", "USD", "Later News", "High")])
    out = md.calendar_context(hour, cal)
    assert "Old News" not in out and "Later News" not in out


def test_a_crowded_window_is_cut_rather_than_listed_in_full():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([(f"2026-06-10T12:{m:02d}:00+00:00", "USD", f"Print {m}", "High")
                for m in range(0, 60, 10)])
    out = md.calendar_context(hour, cal)
    assert out.count("     - ") == md.MAX_NAMED_EVENTS + 1
    assert "and 2 more" in out


def test_a_missing_calendar_never_costs_the_alert():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    assert md.calendar_context(hour, None) == ""
    text = md.format_push({"hour_utc": hour, "asset_id": "a:SPY", "tier": "major",
                           "basis": "abnormal", "r": 0.02}, {}, None)
    assert "biggest move" in text
